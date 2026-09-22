"""Metrics computed from events only (PROJECT-SPEC §11). No hard-coded favorable numbers.

Definitions:
- success_rate: succeeded runs / terminal runs (terminal = SUCCEEDED, FAILED, STOPPED).
- retry_frequency: retry attempts / all execution attempts, split into provider retries and code repairs.
- rollback_frequency: runs with a rollback event / terminal runs; rollback attempts and successful restores shown separately.
- mttr: mean over recovered incidents of (first failure event -> first successful validation afterwards). N/A when none.
- end_to_end latency: run creation -> terminal event, including human waiting; active execution and human wait shown separately.
"""

from __future__ import annotations

from .util import parse_iso

TERMINAL = ("SUCCEEDED", "FAILED", "STOPPED")
WAITING = ("WAITING_FOR_INPUT", "WAITING_FOR_APPROVAL")


def _ratio(num: float, den: float):
    return None if den == 0 else round(num / den, 4)


def run_metrics(run: dict, events: list[dict], attempts: list[dict]) -> dict:
    created = parse_iso(run["created_at"])
    terminal_ev = next((e for e in events if e["type"] == "run_status" and e["payload"].get("to") in TERMINAL), None)
    end = parse_iso(terminal_ev["ts"]) if terminal_ev else None
    # human wait: sum of intervals spent in WAITING_* statuses
    wait_s = 0.0
    cur_wait_start = None
    for e in events:
        if e["type"] != "run_status":
            continue
        to = e["payload"].get("to")
        ts = parse_iso(e["ts"])
        if to in WAITING and cur_wait_start is None:
            cur_wait_start = ts
        elif to not in WAITING and cur_wait_start is not None:
            wait_s += (ts - cur_wait_start).total_seconds()
            cur_wait_start = None
    provider_retries = sum(int(e["payload"].get("retries") or 0) for e in events if e["type"] == "model_response")
    corrections = sum(1 for e in events if e["type"] == "model_output_invalid")
    repairs = sum(1 for e in events if e["type"] == "repair_started")
    model_calls = sum(1 for e in events if e["type"] == "model_request")
    exec_attempts = len(attempts)
    rollbacks = [e for e in events if e["type"] == "rollback"]
    restores_ok = sum(1 for e in rollbacks if e["payload"].get("restore_ok"))
    # incidents: a node_finished FAILED (repairable) followed by a validate SUCCEEDED
    incidents = []
    open_failure = None
    for e in events:
        if e["type"] == "node_finished" and e["payload"].get("status") == "FAILED" and e["payload"].get("repairable"):
            if open_failure is None:
                open_failure = parse_iso(e["ts"])
        if (
            e["type"] == "node_finished"
            and e["task_id"] == "validate"
            and e["payload"].get("status") == "SUCCEEDED"
            and open_failure is not None
        ):
            incidents.append((parse_iso(e["ts"]) - open_failure).total_seconds())
            open_failure = None
    unrecovered = 1 if open_failure is not None else 0
    fault_injected = any(e["type"] == "fault_injected" for e in events)
    return {
        "run_id": run["id"],
        "execution_mode": run["mode"],
        "status": run["status"],
        "terminal": run["status"] in TERMINAL,
        "end_to_end_seconds": round((end - created).total_seconds(), 3) if end else None,
        "active_execution_seconds": round(run["active_seconds"], 3),
        "human_wait_seconds": round(wait_s, 3),
        "model_calls": model_calls,
        "provider_retries": provider_retries,
        "structured_output_corrections": corrections,
        "code_repair_cycles": repairs,
        "execution_attempts": exec_attempts,
        "retry_frequency": _ratio(provider_retries + repairs, exec_attempts + provider_retries),
        "rollback_attempts": len(rollbacks),
        "rollback_successful_restores": restores_ok,
        "recovered_incidents": len(incidents),
        "unrecovered_incidents": unrecovered,
        "mttr_seconds": round(sum(incidents) / len(incidents), 3) if incidents else None,
        "fault_injected": fault_injected,
    }


def aggregate_metrics(per_run: list[dict], mode: str | None = None) -> dict:
    rows = [m for m in per_run if mode is None or m["execution_mode"] == mode]
    terminal = [m for m in rows if m["terminal"]]
    succeeded = [m for m in terminal if m["status"] == "SUCCEEDED"]
    attempts = sum(m["execution_attempts"] for m in rows)
    retries = sum(m["provider_retries"] + m["code_repair_cycles"] for m in rows)
    rollbacks = [m for m in terminal if m["rollback_attempts"] > 0]
    incidents = [m["mttr_seconds"] * m["recovered_incidents"] for m in rows if m["recovered_incidents"]]
    n_inc = sum(m["recovered_incidents"] for m in rows)
    e2e = [m["end_to_end_seconds"] for m in terminal if m["end_to_end_seconds"] is not None]
    return {
        "execution_mode": mode or "all",
        "samples": {"runs": len(rows), "terminal_runs": len(terminal), "execution_attempts": attempts, "recovered_incidents": n_inc},
        "success_rate": _ratio(len(succeeded), len(terminal)),
        "retry_frequency": _ratio(retries, attempts + sum(m["provider_retries"] for m in rows)),
        "provider_retries": sum(m["provider_retries"] for m in rows),
        "code_repair_cycles": sum(m["code_repair_cycles"] for m in rows),
        "rollback_frequency": _ratio(len(rollbacks), len(terminal)),
        "rollback_attempts": sum(m["rollback_attempts"] for m in rows),
        "rollback_successful_restores": sum(m["rollback_successful_restores"] for m in rows),
        "mttr_seconds": round(sum(incidents) / n_inc, 3) if n_inc else None,
        "unrecovered_incidents": sum(m["unrecovered_incidents"] for m in rows),
        "mean_end_to_end_seconds": round(sum(e2e) / len(e2e), 3) if e2e else None,
        "mean_active_execution_seconds": round(sum(m["active_execution_seconds"] for m in terminal) / len(terminal), 3)
        if terminal
        else None,
        "mean_human_wait_seconds": round(sum(m["human_wait_seconds"] for m in terminal) / len(terminal), 3) if terminal else None,
        "note": "Small-sample demo measurements; they demonstrate instrumentation, not statistical reliability.",
    }
