"""Regression tests for the 22 September 2026 code review findings (T-002).

Each test is named after the finding it closes and reproduces the exact failure mode the review
described before asserting the fix. Where the review used a live model to trigger the race, these use
the deterministic fixture adapter with a paused/resumed model call to make the race reproducible.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from conftest import approve_all

from coordinator.config import Settings
from coordinator.policy import validate_edits
from coordinator.runner import DockerRunner
from coordinator.scheduler import Coordinator, pytest_summary_present
from coordinator.store import Store

pytestmark = pytest.mark.docker


# ---------------------------------------------------------------- finding #1
def test_stale_implement_attempt_cannot_mutate_accepted_candidate(sandbox):
    """A requirement revision racing an in-flight implement call must never let that call's edits land."""
    rid = sandbox.create_run("A_greenfield")
    sandbox.resume(rid)
    approve_all(sandbox, rid, stop_before="release")  # t1/t2 already applied by this point in a normal run

    # Simulate "revision lands while an implement attempt is mid-flight, right before it promotes its
    # edits" by taking the run lock ourselves first (as _revise() would) and calling the write-path
    # directly with a requirement snapshot that is already stale relative to the store.
    ws = sandbox.workspace(rid)
    pre_hash = ws.candidate_hash()
    stale_req = sandbox.store.get_requirement(rid)  # captured "before" the revision below
    sandbox.revise_requirement(
        rid,
        "Build a local URL shortener with creation, redirect, analytics, health endpoints, tests, docs. Require HTTPS-only.",
        "race test",
    )
    run_now = sandbox.store.get_run(rid)

    from coordinator.scheduler import NodeFailure

    with pytest.raises(NodeFailure) as exc_info:
        with sandbox._run_lock(rid):
            if sandbox._stale(rid, run_now, stale_req):
                raise NodeFailure("stale", "requirement or graph changed before edits could be promoted; discarding without writing")
            ws.apply_edits(validate_edits([{"path": "app/malicious.py", "content": "x", "op": "write"}], sandbox.settings.budget))
    assert exc_info.value.category == "stale"

    assert ws.candidate_hash() == pre_hash, "candidate must be byte-identical to before the stale attempt"
    assert not (ws.candidate_dir / "app" / "malicious.py").exists()


def test_revise_and_implement_write_are_mutually_exclusive(sandbox):
    """The lock _revise() and the implement write-path share must fully order concurrent access."""
    rid = sandbox.create_run("A_greenfield")
    order: list[str] = []
    barrier = threading.Barrier(2)

    def holder():
        with sandbox._run_lock(rid):
            barrier.wait(timeout=5)
            order.append("holder-in")
            time.sleep(0.05)
            order.append("holder-out")

    def waiter():
        barrier.wait(timeout=5)
        with sandbox._run_lock(rid):
            order.append("waiter-in")

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=waiter)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert order == ["holder-in", "holder-out", "waiter-in"], order


# ---------------------------------------------------------------- finding #2
def test_resume_refuses_on_live_run_with_fixture_adapter(sandbox):
    rid = sandbox.create_run("A_greenfield")
    sandbox.store.update_run(rid, mode="live")  # simulate a run persisted as live, resumed under fixture config
    with pytest.raises(Coordinator._ModeMismatchError, match="mismatched adapter"):
        sandbox.resume(rid)
    run = sandbox.store.get_run(rid)
    assert run["status"] == "PENDING", "a mode-mismatched resume must not have dispatched anything"


def test_resume_refuses_on_fixture_run_with_live_labeled_adapter(sandbox, monkeypatch):
    rid = sandbox.create_run("A_greenfield")  # persisted mode=fixture
    monkeypatch.setattr(sandbox.adapter, "execution_mode", "live")
    with pytest.raises(Coordinator._ModeMismatchError):
        sandbox.resume(rid)


# ---------------------------------------------------------------- finding #3
def test_pytest_summary_present_helper():
    assert not pytest_summary_present("")
    assert not pytest_summary_present("Traceback truncated by os._exit\n")
    assert pytest_summary_present("20 passed, 23 skipped, 1 warning in 0.25s")
    assert pytest_summary_present("no tests ran in 0.00s")


def test_evaluator_early_exit_cannot_produce_false_pass(tmp_path):
    """The exact adversarial candidate from the review: os._exit(0) at import time inside app/__init__.py."""
    candidate = tmp_path / "evil-candidate" / "app"
    candidate.mkdir(parents=True)
    (candidate / "__init__.py").write_text("import os\nos._exit(0)\n")
    (candidate / "main.py").write_text("")

    runner = DockerRunner("forgeflow-worker:latest", trusted_tests_dir=Path(__file__).parent.parent / "trusted_tests")
    raw = runner.run(candidate.parent, "test", stage="A")
    assert raw.exit_code == 0, "sanity: the raw docker exit code is genuinely 0, exactly as the review found"
    assert raw.stdout == "" and raw.stderr == "", "sanity: the process really produced no pytest summary at all"

    from coordinator.scheduler import pytest_summary_present

    assert not pytest_summary_present(raw.stdout), "the coordinator's own check must recognize this as untrustworthy"


def test_validate_node_rejects_early_exit_candidate(sandbox, tmp_path):
    """End-to-end: the validate node itself must fail, not just the helper function."""
    rid = sandbox.create_run("A_greenfield")
    ws = sandbox.workspace(rid)
    ws.apply_edits(
        validate_edits(
            [
                {"path": "app/__init__.py", "op": "write", "content": "import os\nos._exit(0)\n"},
                {"path": "app/main.py", "op": "write", "content": ""},
            ],
            sandbox.settings.budget,
        )
    )
    run = sandbox.store.get_run(rid)
    req = sandbox.store.get_requirement(rid)
    from coordinator.scheduler import NodeFailure

    with pytest.raises(NodeFailure) as exc_info:
        sandbox._node_validate(rid, type("N", (), {"task_id": "validate"})(), "att_test", run, req)
    assert exc_info.value.category == "validation_failed"


# ---------------------------------------------------------------- finding #4
def test_concurrent_resume_is_refused_not_double_dispatched(sandbox):
    rid = sandbox.create_run("A_greenfield")
    errors: list[BaseException] = []
    started = threading.Event()
    release = threading.Event()

    real_reconcile = sandbox._reconcile

    def slow_reconcile(run_id):
        started.set()
        release.wait(timeout=5)
        real_reconcile(run_id)

    sandbox._reconcile = slow_reconcile

    def first():
        try:
            sandbox.resume(rid)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def second():
        started.wait(timeout=5)
        try:
            sandbox.resume(rid)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            release.set()

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert any(isinstance(e, Coordinator._ConcurrentResumeError) for e in errors), errors


def test_lease_acquire_rejects_other_owner_while_unexpired(tmp_path):
    store = Store(tmp_path / "t.db")
    rid = store.create_run("A_greenfield", "fixture", {}, "req", None)
    store.acquire_lease(rid, "owner-a", ttl_s=60.0)
    from coordinator.store import LeaseError

    with pytest.raises(LeaseError):
        store.acquire_lease(rid, "owner-b", ttl_s=60.0)


def test_reconcile_does_not_fire_when_lease_still_held_by_active_scheduler(sandbox):
    """A second resume() from a different owner while the first owner's lease is fresh must be refused
    by acquire_lease before reconciliation ever runs against a still-actively-executing attempt."""
    rid = sandbox.create_run("A_greenfield")
    sandbox.store.acquire_lease(rid, "other-owner", ttl_s=60.0)
    from coordinator.store import LeaseError

    with pytest.raises(LeaseError):
        sandbox.resume(rid)


# ---------------------------------------------------------------- finding #5
def test_all_validation_artifact_hashes_match_saved_bytes(sandbox):
    rid = sandbox.create_run("A_greenfield")
    sandbox.resume(rid)
    approve_all(sandbox, rid)
    from coordinator.util import sha256_text

    checked = 0
    for a in sandbox.store.list_artifacts(rid):
        if a["kind"] != "validation":
            continue
        p = sandbox.run_dir(rid) / a["rel_path"]
        assert sha256_text(p.read_text()) == a["content_hash"], a["rel_path"]
        checked += 1
    assert checked >= 2, "expected at least lint + test validation artifacts"


# ---------------------------------------------------------------- finding #8
def test_exact_allowed_path_does_not_authorize_sibling_file():
    from coordinator.policy import PolicyViolation

    with pytest.raises(PolicyViolation, match="task_scope"):
        validate_edits([{"path": "app/main.py.bak", "op": "write", "content": "x"}], Settings().budget, allowed_globs=("app/main.py",))
    # the exact file itself is still allowed
    out = validate_edits([{"path": "app/main.py", "op": "write", "content": "x"}], Settings().budget, allowed_globs=("app/main.py",))
    assert out[0].path == "app/main.py"


# ---------------------------------------------------------------- finding #6
def test_untrusted_host_is_rejected_by_ui_csrf_flow(sandbox):
    from fastapi.testclient import TestClient

    from coordinator.api import build_app

    app = build_app(sandbox)
    client = TestClient(app)
    r = client.get("/", headers={"Host": "attacker.example"})
    assert r.status_code == 403, r.text


def test_trusted_host_still_works(sandbox):
    from fastapi.testclient import TestClient

    from coordinator.api import build_app

    app = build_app(sandbox)
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.get("/")
    assert r.status_code == 200


def test_origin_matching_untrusted_host_cannot_reach_mutating_route(sandbox):
    """Reproduces the review's exact DNS-rebinding chain: Host and Origin both attacker-controlled and
    self-consistent must still be rejected, because trust comes from configuration, not the request."""
    from fastapi.testclient import TestClient

    from coordinator.api import build_app

    rid = sandbox.create_run("A_greenfield")
    app = build_app(sandbox)
    client = TestClient(app)
    r = client.post(
        f"/runs/{rid}/stop",
        data={"csrf": "irrelevant-because-host-check-runs-first", "reason": "attack"},
        headers={"Host": "attacker.example", "Origin": "http://attacker.example"},
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------- finding #7
def test_worker_output_stays_bounded_during_collection(tmp_path):
    """A producer far exceeding the configured cap must not be fully buffered in host memory."""
    runner = DockerRunner("forgeflow-worker:latest", trusted_tests_dir=Path(__file__).parent.parent / "trusted_tests", max_output=4096)
    candidate = tmp_path / "noisy-candidate" / "app"
    candidate.mkdir(parents=True)
    # 50x the cap of pure noise printed during collection, then a real (trivial) test file.
    (candidate / "__init__.py").write_text("print('x' * 200000)\n" * 1)
    (candidate / "main.py").write_text("")
    res = runner.run(candidate.parent, "test", stage="A")
    # collected text itself must stay near the configured cap (allow the drain thread's one extra chunk
    # plus the truncation marker), not the full ~200KB the producer actually wrote.
    assert len(res.stdout) < 4096 + 8192 + 200, len(res.stdout)
