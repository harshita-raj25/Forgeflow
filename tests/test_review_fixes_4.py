"""Regression tests for the fifth (independent, different-session) adversarial review.

Covers the three findings from that round judged crucial enough to fix before submission (the other five
were judged real but narrower, and are disclosed in docs/limitations.md instead of fixed): a host-side
symlink-following write during the stage-C migration, a Safe Stop that a concurrent approval/revision/
clarification call could silently undo, and an expired-link status oracle that trusted a text search over
model prose instead of a structured field. Each test reproduces the reviewer's exact failure mode before
asserting the fix.
"""

from __future__ import annotations

import json

import pytest
from conftest import approve_all

from coordinator.scheduler import NodeFailure


def run_scenario_a(coord):
    rid = coord.create_run("A_greenfield")
    coord.resume(rid)
    approve_all(coord, rid)
    return rid


def run_scenario_b(coord):
    run_scenario_a(coord)
    rid = coord.create_run("B_brownfield")
    coord.resume(rid)
    approve_all(coord, rid)
    return rid


# ---------------------------------------------------------------- finding #1: migration symlink
@pytest.mark.docker
def test_migration_result_write_refuses_to_follow_a_candidate_planted_symlink(sandbox, tmp_path):
    """Reproduces the reviewer's repro: a candidate's create_app(), running inside the container with
    /work/demo mounted read-write for the migration, replaces migration-result.json with a symlink to an
    arbitrary host path. Before the fix, the coordinator's own host-side write_text() after the container
    exits would follow it and write there. After the fix, the write refuses and the run fails cleanly."""
    run_scenario_a(sandbox)
    run_scenario_b(sandbox)
    rid = sandbox.create_run("C_ambiguous")
    run = sandbox.store.get_run(rid)
    sc = sandbox.scenario_of(run)
    assert sc.stage == "C"
    sandbox._seed_demo_db(rid, sc)

    ws = sandbox.workspace(rid)
    (ws.candidate_dir / "app").mkdir(parents=True, exist_ok=True)
    (ws.candidate_dir / "app" / "__init__.py").write_text("")
    pwn_target = tmp_path / "outside_the_run_dir_pwned.json"
    (ws.candidate_dir / "app" / "main.py").write_text(
        "from pathlib import Path\n"
        "def create_app(db_path, base_url):\n"
        "    demo_dir = Path(db_path).parent\n"
        "    target = demo_dir / 'migration-result.json'\n"
        "    if target.exists() or target.is_symlink():\n"
        "        target.unlink()\n"
        f"    target.symlink_to({str(pwn_target)!r})\n"
        "    raise RuntimeError('malicious create_app: planted symlink instead of migrating')\n"
    )

    with pytest.raises(NodeFailure) as exc_info:
        sandbox._apply_demo_migration(rid, "att_fake")
    assert exc_info.value.category == "policy"
    assert not pwn_target.exists(), "host-side write followed the candidate's symlink outside the run directory"


# ---------------------------------------------------------------- finding #2: stop can be undone
def test_decide_approval_cannot_resurrect_a_run_stopped_mid_call(sandbox):
    """Reproduces the reviewer's repro: decide_approval reads the run's status, does the (possibly slow)
    approval write, then acted on the stale read when moving the run back to PENDING. A stop() landing in
    between used to get silently overwritten. Simulated deterministically: flip the run to STOPPED right
    after the approval decision is recorded but before decide_approval's own status transition runs."""
    rid = sandbox.create_run("A_greenfield")
    sandbox.resume(rid)
    v = sandbox.snapshot_view(rid)
    pend = next(a for a in v["approvals"] if a["status"] == "pending")
    assert sandbox.store.get_run(rid)["status"] == "WAITING_FOR_APPROVAL"

    real_decide = sandbox.store.decide_approval

    def decide_then_concurrent_stop(*a, **kw):
        d = real_decide(*a, **kw)
        sandbox.store.set_status(rid, "STOPPED", actor="test", reason="concurrent stop landed mid-call")
        return d

    sandbox.store.decide_approval = decide_then_concurrent_stop
    try:
        sandbox.decide_approval(rid, pend["id"], True, "late approval racing a stop")
    finally:
        sandbox.store.decide_approval = real_decide

    assert sandbox.store.get_run(rid)["status"] == "STOPPED", "stop() was silently undone back to PENDING"


def test_revise_requirement_cannot_resurrect_a_run_stopped_mid_call(sandbox):
    """Same race, different call site: revise_requirement acted on the `run` status read at function entry
    when deciding whether to move to PENDING, after _revise's own (possibly slow) work."""
    rid = sandbox.create_run("A_greenfield")
    sandbox.resume(rid)
    assert sandbox.store.get_run(rid)["status"] == "WAITING_FOR_APPROVAL"

    real_revise = sandbox._revise

    def revise_then_concurrent_stop(*a, **kw):
        rev = real_revise(*a, **kw)
        sandbox.store.set_status(rid, "STOPPED", actor="test", reason="concurrent stop landed mid-call")
        return rev

    sandbox._revise = revise_then_concurrent_stop
    try:
        sandbox.revise_requirement(rid, "a revised requirement text", "test revision racing a stop")
    finally:
        sandbox._revise = real_revise

    assert sandbox.store.get_run(rid)["status"] == "STOPPED", "stop() was silently undone back to PENDING"


def test_answer_clarification_cannot_resurrect_a_run_stopped_mid_call(sandbox):
    """Same race, third call site: answer_clarification unconditionally set PENDING after _revise."""
    run_scenario_a(sandbox)
    run_scenario_b(sandbox)
    rid = sandbox.create_run("C_ambiguous")
    sandbox.resume(rid)
    assert sandbox.store.get_run(rid)["status"] == "WAITING_FOR_INPUT"
    v = sandbox.snapshot_view(rid)
    cl = next(c for c in v["clarifications"] if c["status"] == "pending")
    answers = [{"id": q["id"], "answer": "n/a"} for q in cl["questions"]]

    real_revise = sandbox._revise

    def revise_then_concurrent_stop(*a, **kw):
        rev = real_revise(*a, **kw)
        sandbox.store.set_status(rid, "STOPPED", actor="test", reason="concurrent stop landed mid-call")
        return rev

    sandbox._revise = revise_then_concurrent_stop
    try:
        sandbox.answer_clarification(rid, cl["id"], answers)
    finally:
        sandbox._revise = real_revise

    assert sandbox.store.get_run(rid)["status"] == "STOPPED", "stop() was silently undone back to PENDING"


def test_set_status_if_is_a_real_compare_and_set(sandbox):
    rid = sandbox.create_run("A_greenfield")
    sandbox.store.set_status(rid, "RUNNING", actor="test")
    assert sandbox.store.set_status_if(rid, "PENDING", "WAITING_FOR_APPROVAL", actor="test") is False
    assert sandbox.store.get_run(rid)["status"] == "RUNNING"
    assert sandbox.store.set_status_if(rid, "RUNNING", "WAITING_FOR_APPROVAL", actor="test") is True
    assert sandbox.store.get_run(rid)["status"] == "WAITING_FOR_APPROVAL"


# ---------------------------------------------------------------- finding #5: expiry status oracle
def _write_requirements_artifact(coord, run_id, revision, data):
    ws = coord.workspace(run_id)
    p, h = ws.write_artifact("synthetic-requirements.json", json.dumps(data))
    coord.store.add_artifact(run_id, "requirements", "artifacts/synthetic-requirements.json", h, None, [], revision, None)


def _base_requirements(**overrides):
    data = {
        "normalized_requirement": "base text",
        "acceptance_criteria": [],
        "expired_link_status": None,
    }
    data.update(overrides)
    return data


def test_expiry_oracle_reads_the_structured_field_not_prose(sandbox):
    rid = sandbox.create_run("A_greenfield")

    _write_requirements_artifact(sandbox, rid, 1, _base_requirements(expired_link_status=None))
    assert sandbox._expected_expired_status(rid) == 410  # documented default when undecided

    _write_requirements_artifact(sandbox, rid, 1, _base_requirements(expired_link_status=404))
    assert sandbox._expected_expired_status(rid) == 404

    _write_requirements_artifact(sandbox, rid, 1, _base_requirements(expired_link_status=410))
    assert sandbox._expected_expired_status(rid) == 410


def test_expiry_oracle_is_not_flipped_by_the_reviewer_negation_trap_sentences(sandbox):
    """The exact two sentences the reviewer showed flip the old text-scanning heuristic. Now the field
    alone decides, regardless of what the surrounding prose happens to say."""
    rid = sandbox.create_run("A_greenfield")

    _write_requirements_artifact(
        sandbox,
        rid,
        1,
        _base_requirements(
            expired_link_status=404,
            normalized_requirement="404 (not 410 Gone)",
        ),
    )
    assert sandbox._expected_expired_status(rid) == 404

    _write_requirements_artifact(
        sandbox,
        rid,
        1,
        _base_requirements(
            expired_link_status=410,
            acceptance_criteria=[{"id": "AC-3", "statement": "Gone; unknown codes still return 404."}],
        ),
    )
    assert sandbox._expected_expired_status(rid) == 410


def test_expiry_oracle_end_to_end_through_real_clarification_and_revision(sandbox):
    """Confirms the schema/prompt/fixture wiring, not just the reader function: driving scenario C through
    a real clarification round (404) and a real requirement revision (410) via the actual coordinator."""
    run_scenario_a(sandbox)
    run_scenario_b(sandbox)
    rid = sandbox.create_run("C_ambiguous")
    sandbox.resume(rid)
    v = sandbox.snapshot_view(rid)
    cl = next(c for c in v["clarifications"] if c["status"] == "pending")
    answers = [
        {"id": q["id"], "answer": a}
        for q, a in zip(
            cl["questions"],
            [
                "Expiry is optional and explicitly set per link at creation.",
                "Existing links remain valid forever.",
                "Return 404 for an expired link.",
                "Only aggregate click counts.",
            ],
            strict=False,
        )
    ]
    sandbox.answer_clarification(rid, cl["id"], answers)
    sandbox.resume(rid)
    assert sandbox._expected_expired_status(rid) == 404

    approve_all(sandbox, rid, stop_before="release")
    sandbox.revise_requirement(rid, "REVISED: expired link must return HTTP 410 Gone, not 404.", "product decision")
    sandbox.resume(rid)
    assert sandbox._expected_expired_status(rid) == 410
