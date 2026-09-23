"""Regression tests for the 23 September 2026 second code review findings (T-003).

Each test reproduces the reviewer's exact repro (a zero-assertion pytest result, a lease shorter than a
paused task, two separate Coordinator instances racing a write against a revision, Stop racing a write)
before asserting the fix.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from conftest import approve_all

from coordinator.config import Settings
from coordinator.runner import DockerRunner
from coordinator.scheduler import Coordinator, pytest_passed_count
from coordinator.store import LeaseError, Store

pytestmark = pytest.mark.docker


# ---------------------------------------------------------------- finding #1
def test_pytest_passed_count_helper():
    assert pytest_passed_count("no tests ran in 0.00s") == 0
    assert pytest_passed_count("43 skipped in 0.01s") == 0
    assert pytest_passed_count("20 passed, 23 skipped, 1 warning in 0.25s") == 20
    assert pytest_passed_count("") == 0


def test_zero_assertion_results_rejected_by_validate_node(sandbox):
    """Reproduces both of the reviewer's exact acceptances through the real _node_validate handler."""
    from coordinator.policy import validate_edits
    from coordinator.runner import RunResult
    from coordinator.scheduler import NodeFailure

    rid = sandbox.create_run("A_greenfield")
    ws = sandbox.workspace(rid)
    ws.apply_edits(
        validate_edits(
            [{"path": "app/__init__.py", "op": "write", "content": ""}, {"path": "app/main.py", "op": "write", "content": ""}],
            sandbox.settings.budget,
        )
    )
    run = sandbox.store.get_run(rid)
    req = sandbox.store.get_requirement(rid)

    class FakeRunner:
        def __init__(self, stdout):
            self._stdout = stdout

        def run(self, *a, **kw):
            return RunResult(
                command="pytest", exit_code=0, stdout=self._stdout, stderr="", duration_ms=1, timed_out=False, container_id="fake"
            )

        def check_available(self):
            return "ok"

    real_runner = sandbox.runner
    try:
        for stdout in ("no tests ran in 0.00s", "43 skipped in 0.01s"):
            sandbox.runner = FakeRunner(stdout)
            with pytest.raises(NodeFailure) as exc_info:
                sandbox._node_validate(rid, type("N", (), {"task_id": "validate"})(), "att_test", run, req)
            assert exc_info.value.category == "validation_failed"
    finally:
        sandbox.runner = real_runner


def test_real_docker_correct_candidate_reports_passed(tmp_path):
    """Sanity anchor: the helper genuinely integrates with a real Docker-backed passing run."""
    candidate = tmp_path / "correct-candidate"
    reference = Path(__file__).parent.parent / "fixtures" / "reference" / "A"
    import shutil

    shutil.copytree(reference / "app", candidate / "app")
    runner = DockerRunner("forgeflow-worker:latest", trusted_tests_dir=Path(__file__).parent.parent / "trusted_tests")
    res = runner.run(candidate, "test", stage="A")
    assert res.exit_code == 0
    assert pytest_passed_count(res.stdout) >= 1


# ---------------------------------------------------------------- finding #2
def test_lease_survives_in_flight_dispatch_via_heartbeat(sandbox, monkeypatch):
    rid = sandbox.create_run("A_greenfield")
    release = threading.Event()
    entered = threading.Event()

    real_call_model = sandbox._call_model

    def slow_call_model(*a, **kw):
        entered.set()
        release.wait(timeout=10)
        return real_call_model(*a, **kw)

    monkeypatch.setattr(sandbox, "_call_model", slow_call_model)

    orig_heartbeat = sandbox._lease_heartbeat

    def fast_heartbeat(run_id, stop, ttl_s=60.0, interval_s=15.0):
        orig_heartbeat(run_id, stop, ttl_s=0.3, interval_s=0.05)

    monkeypatch.setattr(sandbox, "_lease_heartbeat", fast_heartbeat)

    t = threading.Thread(target=sandbox.resume, args=(rid,))
    t.start()
    entered.wait(timeout=10)
    time.sleep(0.5)  # longer than the 0.3s ttl, but the heartbeat renews every 0.05s
    with pytest.raises(LeaseError):
        sandbox.store.acquire_lease(rid, "other-owner", ttl_s=0.3)
    release.set()
    t.join(timeout=10)


def test_lease_heartbeat_stops_cleanly_after_resume_returns(sandbox):
    rid = sandbox.create_run("A_greenfield")
    sandbox.resume(rid)
    sandbox.store.release_lease(rid, sandbox.owner)
    sandbox.store.acquire_lease(rid, "someone-else", ttl_s=1.0)  # no leftover heartbeat thread contests this


# ---------------------------------------------------------------- finding #3
def _make_two_coordinators(root: Path):
    """Two independent Coordinator instances, each its own Store connection, sharing one SQLite file and
    one runs directory -- exactly how two separate `forgeflow` CLI invocations relate to each other."""
    import shutil

    from coordinator.adapters.fixture import FixtureAdapter
    from coordinator.runner import DockerRunner as DR

    (root / "runs").mkdir()
    shutil.copytree(Path(__file__).parent.parent / "scenarios", root / "scenarios")
    shutil.copytree(Path(__file__).parent.parent / "fixtures", root / "fixtures", ignore=shutil.ignore_patterns("build_fixtures.py"))
    settings = Settings(db_path=root / "forgeflow.db", runs_dir=root / "runs", execution_mode="fixture")
    trusted = Path(__file__).parent.parent / "trusted_tests"
    coords = [
        Coordinator(
            settings,
            Store(settings.db_path),
            FixtureAdapter(fixtures_dir=root / "fixtures"),
            DR("forgeflow-worker:latest", trusted_tests_dir=trusted),
        )
        for _ in range(2)
    ]
    return coords[0], coords[1]


def test_cross_process_lock_provides_real_mutual_exclusion(tmp_path, monkeypatch):
    """Reproduces the reviewer's exact repro shape: coordinator A paused inside its critical section
    (past its freshness check) while coordinator B (a separate instance/process) commits a requirement
    revision. Before the fix (an in-process threading.Lock), B's revise() ran fully concurrently with A's
    critical section -- no ordering at all across the two instances. After the fix (a real flock), B's
    revise() must block for the whole time A holds the lock."""
    monkeypatch.setattr("coordinator.config.SCENARIOS_DIR", tmp_path / "scenarios")
    monkeypatch.setattr("coordinator.scenarios.SCENARIOS_DIR", tmp_path / "scenarios")
    coord_a, coord_b = _make_two_coordinators(tmp_path)

    rid = coord_a.create_run("A_greenfield")
    coord_a.resume(rid)
    approve_all(coord_a, rid, stop_before="release")  # keep the run revisable, not yet SUCCEEDED
    ws = coord_a.workspace(rid)
    pre_hash = ws.candidate_hash()

    order: list[str] = []
    a_in_critical_section = threading.Event()
    let_a_finish = threading.Event()

    def a_holds_the_lock():
        req = coord_a.store.get_requirement(rid)
        run = coord_a.store.get_run(rid)
        with coord_a._promote_lock(rid):
            order.append("a-acquired")
            a_in_critical_section.set()
            let_a_finish.wait(timeout=10)
            blocked = coord_a._promotion_blocked(rid, run, req)
            order.append(f"a-check:{blocked}")
        order.append("a-released")

    ta = threading.Thread(target=a_holds_the_lock)
    ta.start()
    a_in_critical_section.wait(timeout=10)

    b_started = threading.Event()
    b_done = threading.Event()

    def b_revises():
        b_started.set()
        coord_b.revise_requirement(rid, "REVISED while A held the lock", "cross-process race test")
        b_done.set()

    tb = threading.Thread(target=b_revises)
    tb.start()
    b_started.wait(timeout=10)
    time.sleep(0.3)
    assert not b_done.is_set(), "B's revise() must block while A holds the cross-process lock, not run concurrently"
    let_a_finish.set()
    ta.join(timeout=10)
    tb.join(timeout=10)

    assert order == ["a-acquired", "a-check:None", "a-released"], order
    assert ws.candidate_hash() == pre_hash, "neither side wrote candidate bytes in this reproduction"

    # A fresh attempt using the requirement snapshot captured before B's revision must now be blocked.
    stale_req = coord_a.store.get_requirement(rid, rev=1)
    run_now = coord_a.store.get_run(rid)
    with coord_a._promote_lock(rid):
        blocked_now = coord_a._promotion_blocked(rid, run_now, stale_req)
    assert blocked_now is not None


# ---------------------------------------------------------------- finding #4
def test_stop_prevents_in_flight_write_from_landing(sandbox, monkeypatch):
    """Reproduces the reviewer's exact repro: the implementation handler is blocked at its model response
    -- the point where the review paused it, before the promote lock is ever acquired, since `_call_model`
    runs strictly before `with self._promote_lock(...)`. `_check_stop` is patched to a no-op for this test
    so the pre-existing early check (which fires the instant `_call_model` is re-entered) doesn't mask
    whether the NEW promote-lock/status check specifically closes the gap -- this isolates finding #4's
    fix from the unrelated, already-existing `_check_stop` call at the top of `_call_model`, matching a
    real live call that blocks *inside* a network request, past that initial check, and is never
    re-checked before the response returns. Calls the unmodified `_node_implement` handler."""
    from coordinator.scheduler import NodeFailure

    rid = sandbox.create_run("A_greenfield")
    sandbox.resume(rid)
    approve_all(sandbox, rid, stop_before="release")
    ws = sandbox.workspace(rid)
    pre_hash = ws.candidate_hash()

    entered = threading.Event()
    release = threading.Event()
    real_call_model = sandbox._call_model
    monkeypatch.setattr(sandbox, "_check_stop", lambda run_id: None)

    def paused_call_model(*a, **kw):
        entered.set()
        release.wait(timeout=10)
        return real_call_model(*a, **kw)

    monkeypatch.setattr(sandbox, "_call_model", paused_call_model)

    run = sandbox.store.get_run(rid)
    req = sandbox.store.get_requirement(rid)
    node = type("N", (), {"task_id": "t1", "allowed_paths": ()})()
    result: dict = {}

    def implement_attempt():
        try:
            sandbox._node_implement(rid, node, "att_test", run, req)
            result["outcome"] = "succeeded"
        except NodeFailure as e:
            result["outcome"] = e.category

    t = threading.Thread(target=implement_attempt)
    t.start()
    entered.wait(timeout=10)
    sandbox.stop(rid, "operator requested stop mid-implement")  # runs while the model call is paused, before any lock is held
    release.set()
    t.join(timeout=10)

    assert result["outcome"] == "stale", result  # the promotion check now folds in run status; stop -> blocked
    assert ws.candidate_hash() == pre_hash
    assert not list((ws.candidate_dir / "app").glob("malicious*"))


# --------------------------------------------------------- checker follow-up
def test_injected_warning_text_cannot_forge_a_passed_count():
    """Reproduces the independent checker's own bypass of the finding #1 fix: candidate code that
    triggers a warning whose message text itself looks like a pytest summary line (e.g.
    `warnings.warn("999 passed in 0.00s")`) leaked into pytest's warnings-summary section and was
    miscounted by an unanchored whole-stdout search. Parsing only the actual last line closes it."""
    forged_stdout = (
        "=============================== warnings summary ===============================\n"
        "tests/foo.py::test_x\n"
        "  /path/to/file.py:5: UserWarning: 999 passed in 0.00s\n"
        '    warnings.warn("999 passed in 0.00s")\n'
        "\n"
        "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
        "1 failed, 1 warning in 0.01s"
    )
    assert pytest_passed_count(forged_stdout) == 0, "the forged '999 passed' text must not be counted"


def test_zero_assertion_result_with_injected_warning_rejected_by_validate_node(sandbox):
    """End-to-end: the real _node_validate handler must reject this exact forged blob, not just the
    helper function in isolation."""
    from coordinator.runner import RunResult
    from coordinator.scheduler import NodeFailure

    rid = sandbox.create_run("A_greenfield")
    run = sandbox.store.get_run(rid)
    req = sandbox.store.get_requirement(rid)
    forged_stdout = (
        "=============================== warnings summary ===============================\n"
        "tests/foo.py::test_x\n"
        "  /path/to/file.py:5: UserWarning: 999 passed in 0.00s\n"
        '    warnings.warn("999 passed in 0.00s")\n'
        "\n"
        "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
        "1 failed, 1 warning in 0.01s"
    )

    class FakeRunner:
        def run(self, *a, **kw):
            return RunResult(
                command="pytest", exit_code=0, stdout=forged_stdout, stderr="", duration_ms=1, timed_out=False, container_id="fake"
            )

        def check_available(self):
            return "ok"

    real_runner = sandbox.runner
    sandbox.runner = FakeRunner()
    try:
        with pytest.raises(NodeFailure) as exc_info:
            sandbox._node_validate(rid, type("N", (), {"task_id": "validate"})(), "att_test", run, req)
        assert exc_info.value.category == "validation_failed"
    finally:
        sandbox.runner = real_runner
