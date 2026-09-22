"""Evidence bundle export (BUILD-AND-DEMO §6). Works for any run state; release/ states non-release explicitly."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .metrics import run_metrics
from .util import iso, looks_secret, redact


def export_bundle(coord, run_id: str) -> Path:
    store = coord.store
    run = store.get_run(run_id)
    run_dir = coord.run_dir(run_id)
    out = run_dir / "export"
    out.mkdir(parents=True, exist_ok=True)
    events = store.list_events(run_id)
    attempts = store.list_attempts(run_id)
    ok, msg = store.verify_chain(events)
    metrics = run_metrics(run, events, attempts)
    # events.jsonl
    with (out / "events.jsonl").open("w") as f:
        for e in events:
            f.write(json.dumps(e, sort_keys=True) + "\n")
    (out / "requirement-revisions.json").write_text(json.dumps(store.list_requirements(run_id), indent=2))
    (out / "graph-revisions.json").write_text(json.dumps(store.list_graphs(run_id), indent=2))
    (out / "approvals.json").write_text(json.dumps(store.list_approvals(run_id), indent=2))
    (out / "clarifications.json").write_text(json.dumps(store.list_clarifications(run_id), indent=2))
    decisions = [
        {
            "seq": e["seq"],
            "ts": e["ts"],
            "task_id": e["task_id"],
            "decisions": e["payload"].get("decisions"),
            "risks": e["payload"].get("risks"),
            "impact_map": e["payload"].get("impact_map"),
            "artifact": e["payload"].get("artifact"),
        }
        for e in events
        if e["type"] == "decision"
    ]
    (out / "decisions.json").write_text(json.dumps(decisions, indent=2))
    arts = store.list_artifacts(run_id)
    (out / "artifact-manifest.json").write_text(json.dumps(arts, indent=2))
    ws = coord.workspace(run_id)
    (out / "candidate.diff").write_text(ws.diff() if ws.candidate_dir.exists() else "")
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2))
    vsrc = run_dir / "validation"
    vdst = out / "validation"
    if vdst.exists():
        shutil.rmtree(vdst)
    if vsrc.exists():
        shutil.copytree(vsrc, vdst)
    rel = out / "release"
    if not (rel / "release-manifest.json").exists():
        rel.mkdir(exist_ok=True)
        (rel / "STATUS.txt").write_text(
            f"NOT RELEASED. Run status: {run['status']}. Reason: {run.get('stop_reason') or 'no release approval/export completed'}\n"
        )
    summary = _summary(coord, run, events, attempts, metrics, ok, msg)
    (out / "run-summary.md").write_text(summary)
    # secret scan across the bundle (heuristic; documented limitation)
    leaks = []
    for p in out.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl", ".md", ".txt", ".diff", ".py"):
            try:
                txt = p.read_text(errors="replace")
            except OSError:
                continue
            if looks_secret(txt):
                p.write_text(redact(txt))
                leaks.append(str(p.relative_to(out)))
    (out / "export-meta.json").write_text(
        json.dumps(
            {
                "exported_at": iso(),
                "event_chain_verified": ok,
                "event_chain_message": msg,
                "redacted_files": leaks,
                "execution_mode": run["mode"],
            },
            indent=2,
        )
    )
    store.append_event(
        run_id,
        "bundle_exported",
        {"path": str(out.relative_to(run_dir.parent)), "events": len(events), "chain_verified": ok, "redacted_files": leaks},
    )
    return out


def _summary(coord, run: dict, events: list[dict], attempts: list[dict], metrics: dict, chain_ok: bool, chain_msg: str) -> str:
    sc = run["config"].get("scenario", {})
    lines = [
        f"# Run summary — {run['id']}",
        "",
        f"- Scenario: **{sc.get('title', run['scenario'])}** (stage {sc.get('stage')})",
        f"- Execution mode: **{run['mode'].upper()}**"
        + (
            "  ← deterministic fixture adapter; not live model evidence"
            if run["mode"] == "fixture"
            else f"  (model: {run['config'].get('openai_model')})"
        ),
        f"- Result: **{run['status']}**" + (f" — {run['stop_reason']}" if run.get("stop_reason") else ""),
        f"- Requirement revision: {run['requirement_revision']}; graph revision: {run['graph_revision']}; candidate revision: {run['candidate_revision']}",
        f"- Baseline hash: `{run['baseline_hash']}`",
        f"- Candidate hash: `{run['candidate_hash']}`",
        f"- Fault injection: {'YES (labeled FORGEFLOW_INJECTED_FAULT; not a natural defect)' if run['config'].get('inject_fault') else 'no'}",
        f"- Event chain: {'verified' if chain_ok else 'FAILED'} ({chain_msg})",
        "",
        "## Node attempts",
        "",
        "| task | attempt | status | candidate rev | started | ended | error |",
        "|---|---|---|---|---|---|---|",
    ]
    for a in attempts:
        lines.append(
            f"| {a['task_id']} | {a['attempt']} | {a['status']} | {a['candidate_revision']} | {a['started_at']} | {a.get('ended_at') or ''} | {(a.get('error_category') or '')} {(a.get('error_text') or '')[:80]} |"
        )
    lines += ["", "## Approvals", ""]
    for ap in coord.store.list_approvals(run["id"]):
        lines.append(
            f"- {ap['scope']}: **{ap['status']}** subject `{ap['subject_hash'][:16]}` by {ap.get('human_label') or '-'} at {ap.get('decided_at') or '-'}; rationale: {ap.get('rationale') or '-'}"
            + (f"; invalidated: {ap['invalidated_reason']}" if ap.get("invalidated_reason") else "")
        )
    lines += ["", "## Metrics (computed from events)", "", "```json", json.dumps(metrics, indent=2), "```", ""]
    lines += [
        "## Limitations",
        "",
        "- Local single-user prototype; identity labels are not authentication.",
        "- Event hash chain detects edits relative to an exported head; it is not tamper-proof storage.",
        "- Secret redaction is heuristic.",
        "- Metrics are small-sample demo measurements.",
    ]
    if run["mode"] == "fixture":
        lines.append(
            "- FIXTURE MODE: agent outputs came from deterministic files under fixtures/; this run is not evidence of live model behavior."
        )
    return "\n".join(lines) + "\n"
