"""Metric formulas verified on a tiny fixed event dataset, including zero-sample N/A (BUILD-AND-DEMO §5)."""

from __future__ import annotations

from coordinator.metrics import aggregate_metrics, run_metrics


def _ev(seq, ts, type_, task_id=None, **payload):
    return {
        "seq": seq,
        "run_id": "r1",
        "ts": ts,
        "task_id": task_id,
        "attempt_id": None,
        "actor": "c",
        "type": type_,
        "payload": payload,
        "prev_hash": "",
        "hash": "",
    }


def test_run_metrics_zero_samples_are_none_not_crash():
    run = {"id": "r1", "mode": "fixture", "status": "SUCCEEDED", "created_at": "2026-01-01T00:00:00Z", "active_seconds": 0.0}
    m = run_metrics(run, [], [])
    assert m["mttr_seconds"] is None
    assert m["retry_frequency"] is None
    agg = aggregate_metrics([m])
    assert agg["mttr_seconds"] is None
    assert agg["success_rate"] == 1.0


def test_retry_and_repair_counted_separately():
    run = {"id": "r1", "mode": "fixture", "status": "SUCCEEDED", "created_at": "2026-01-01T00:00:00Z", "active_seconds": 5.0}
    events = [
        _ev(1, "2026-01-01T00:00:00Z", "model_request"),
        _ev(2, "2026-01-01T00:00:01Z", "model_response", retries=2),
        _ev(3, "2026-01-01T00:00:02Z", "repair_started", cycle=1),
        _ev(4, "2026-01-01T00:00:05Z", "run_status", to="SUCCEEDED"),
    ]
    attempts = [{"task_id": "t1"}, {"task_id": "t1"}]
    m = run_metrics(run, events, attempts)
    assert m["provider_retries"] == 2
    assert m["code_repair_cycles"] == 1
    assert m["execution_attempts"] == 2


def test_mttr_computed_from_failure_to_recovered_validate():
    run = {"id": "r1", "mode": "fixture", "status": "SUCCEEDED", "created_at": "2026-01-01T00:00:00Z", "active_seconds": 1.0}
    events = [
        _ev(1, "2026-01-01T00:00:00Z", "node_finished", task_id="validate", status="FAILED", repairable=True),
        _ev(2, "2026-01-01T00:00:10Z", "node_finished", task_id="validate", status="SUCCEEDED"),
    ]
    m = run_metrics(run, events, [])
    assert m["recovered_incidents"] == 1
    assert m["mttr_seconds"] == 10.0
    assert m["unrecovered_incidents"] == 0


def test_human_wait_excluded_then_included_correctly():
    run = {"id": "r1", "mode": "fixture", "status": "SUCCEEDED", "created_at": "2026-01-01T00:00:00Z", "active_seconds": 2.0}
    events = [
        _ev(1, "2026-01-01T00:00:00Z", "run_status", to="RUNNING"),
        _ev(2, "2026-01-01T00:00:01Z", "run_status", to="WAITING_FOR_APPROVAL"),
        _ev(3, "2026-01-01T00:00:11Z", "run_status", to="RUNNING"),
        _ev(4, "2026-01-01T00:00:13Z", "run_status", to="SUCCEEDED"),
    ]
    m = run_metrics(run, events, [])
    assert m["human_wait_seconds"] == 10.0
    assert m["end_to_end_seconds"] == 13.0


def test_rollback_and_unrecovered_incident_tracked():
    run = {"id": "r1", "mode": "fixture", "status": "FAILED", "created_at": "2026-01-01T00:00:00Z", "active_seconds": 1.0}
    events = [
        _ev(1, "2026-01-01T00:00:00Z", "node_finished", task_id="validate", status="FAILED", repairable=True),
        _ev(2, "2026-01-01T00:00:05Z", "rollback", restore_ok=True),
        _ev(3, "2026-01-01T00:00:06Z", "run_status", to="FAILED"),
    ]
    m = run_metrics(run, events, [])
    assert m["rollback_attempts"] == 1 and m["rollback_successful_restores"] == 1
    assert m["unrecovered_incidents"] == 1 and m["recovered_incidents"] == 0
    agg = aggregate_metrics([m])
    assert agg["rollback_frequency"] == 1.0
    assert agg["success_rate"] == 0.0
