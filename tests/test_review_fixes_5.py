"""Regression tests for the sixth adversarial review (a follow-up check of commit c006c55's fifth-round
fixes, run in a fresh session). It confirmed the three fifth-round fixes hold, but found the Safe Stop fix
was too narrow: it covered the three human-triggered call sites (decide_approval, revise_requirement,
answer_clarification) but not the scheduler's own internal `set_status` calls (the approval gate, a
blocking clarification, a repair cycle re-entering RUNNING), which have the identical unconditional-write
shape and can resurrect a just-STOPPED run in the same narrow window.

Fixed at the root instead of at each call site: `Store.set_status` and `Store.set_status_if` now refuse to
leave a terminal state (SUCCEEDED/FAILED/STOPPED) no matter who calls them or why.
"""

from __future__ import annotations

from conftest import approve_all


def run_scenario_a(coord):
    rid = coord.create_run("A_greenfield")
    coord.resume(rid)
    approve_all(coord, rid)
    return rid


def test_set_status_never_leaves_a_terminal_state(sandbox):
    # A fresh run per terminal state: once a run reaches a terminal state this guard is exactly what stops
    # any further set_status call -- including a same-status "reset" -- from moving it, so reusing one run
    # id across cases would itself be blocked by the very invariant under test.
    for terminal in ("SUCCEEDED", "FAILED", "STOPPED"):
        rid = sandbox.create_run("A_greenfield")
        sandbox.store.set_status(rid, "RUNNING", actor="test")
        sandbox.store.set_status(rid, terminal, actor="test")
        sandbox.store.set_status(rid, "RUNNING", actor="test")  # attempt to leave the terminal state
        assert sandbox.store.get_run(rid)["status"] == terminal
        assert sandbox.store.set_status_if(rid, terminal, "RUNNING", actor="test") is False
        assert sandbox.store.get_run(rid)["status"] == terminal


def test_approval_gate_cannot_resurrect_a_run_stopped_mid_call(sandbox):
    """Reproduces the sixth reviewer's exact repro: Stop lands just as the scheduler reaches the plan
    approval gate. Before this fix, `_approval_gate`'s own `set_status(..., "WAITING_FOR_APPROVAL")` right
    after requesting the approval would silently overwrite STOPPED, and a later approval would let
    t1/t2/freeze/review/validate all dispatch after the stop."""
    rid = sandbox.create_run("A_greenfield")

    real_request_approval = sandbox.store.request_approval

    def request_then_concurrent_stop(*a, **kw):
        r = real_request_approval(*a, **kw)
        sandbox.store.set_status(rid, "STOPPED", actor="test", reason="concurrent stop landed mid-call")
        return r

    sandbox.store.request_approval = request_then_concurrent_stop
    try:
        sandbox.resume(rid)
    finally:
        sandbox.store.request_approval = real_request_approval

    assert sandbox.store.get_run(rid)["status"] == "STOPPED", "approval gate resurrected the run back to WAITING_FOR_APPROVAL"

    # A later resume() (standing in for "the human approves anyway") must not dispatch anything further.
    run_after = sandbox.resume(rid)
    assert run_after["status"] == "STOPPED"
    v = sandbox.snapshot_view(rid)
    assert v["states"].get("t1") not in ("SUCCEEDED", "RUNNING")
    assert v["states"].get("t2") not in ("SUCCEEDED", "RUNNING")
    assert v["states"].get("freeze") not in ("SUCCEEDED", "RUNNING")
