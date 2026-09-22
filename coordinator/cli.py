"""ForgeFlow CLI: create/resume/inspect runs, clarify, approve, revise, stop, export, metrics, serve."""

from __future__ import annotations

import argparse
import json
import sys

from .bootstrap import build_coordinator
from .export import export_bundle
from .metrics import aggregate_metrics, run_metrics
from .scenarios import list_scenarios


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_scenarios(args, coord) -> None:
    _print([s.as_dict() for s in list_scenarios()])


def cmd_create(args, coord) -> None:
    run_id = coord.create_run(args.scenario, requirement_text=args.requirement, inject_fault=args.inject_fault)
    _print({"run_id": run_id})


def cmd_resume(args, coord) -> None:
    run = coord.resume(args.run_id)
    _print(
        {
            k: run[k]
            for k in ("id", "status", "requirement_revision", "graph_revision", "candidate_revision", "candidate_hash", "stop_reason")
        }
    )


def cmd_status(args, coord) -> None:
    v = coord.snapshot_view(args.run_id)
    _print(
        {
            "run": {
                k: v["run"][k]
                for k in (
                    "id",
                    "scenario",
                    "mode",
                    "status",
                    "requirement_revision",
                    "graph_revision",
                    "candidate_revision",
                    "candidate_hash",
                    "stop_reason",
                )
            },
            "states": v["states"],
            "pending_approvals": [a for a in v["approvals"] if a["status"] == "pending"],
            "pending_clarifications": [c for c in v["clarifications"] if c["status"] == "pending"],
        }
    )


def cmd_list(args, coord) -> None:
    _print([{k: r[k] for k in ("id", "scenario", "mode", "status", "created_at")} for r in coord.store.list_runs()])


def cmd_clarify(args, coord) -> None:
    answers = json.loads(args.answers)
    coord.answer_clarification(args.run_id, args.clarification_id, answers)
    _print({"ok": True})


def cmd_revise(args, coord) -> None:
    rev = coord.revise_requirement(args.run_id, args.text, args.reason)
    _print({"requirement_revision": rev})


def cmd_approve(args, coord) -> None:
    d = coord.decide_approval(args.run_id, args.approval_id, not args.reject, args.rationale or "")
    _print({k: d[k] for k in ("id", "scope", "status")})


def cmd_stop(args, coord) -> None:
    coord.stop(args.run_id, args.reason)
    _print({"ok": True})


def cmd_export(args, coord) -> None:
    out = export_bundle(coord, args.run_id)
    _print({"exported_to": str(out)})


def cmd_metrics(args, coord) -> None:
    per_run = []
    for r in coord.store.list_runs():
        events = coord.store.list_events(r["id"])
        attempts = coord.store.list_attempts(r["id"])
        per_run.append(run_metrics(r, events, attempts))
    _print(aggregate_metrics(per_run, mode=args.mode))


def cmd_serve(args, coord) -> None:
    import uvicorn

    from .api import build_app

    uvicorn.run(build_app(coord), host=coord.settings.bind_host, port=coord.settings.bind_port)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="forgeflow")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scenarios").set_defaults(func=cmd_scenarios)

    c = sub.add_parser("create")
    c.add_argument("scenario")
    c.add_argument("--requirement")
    c.add_argument("--inject-fault", action="store_true")
    c.set_defaults(func=cmd_create)

    r = sub.add_parser("resume")
    r.add_argument("run_id")
    r.set_defaults(func=cmd_resume)

    s = sub.add_parser("status")
    s.add_argument("run_id")
    s.set_defaults(func=cmd_status)

    sub.add_parser("list").set_defaults(func=cmd_list)

    cl = sub.add_parser("clarify")
    cl.add_argument("run_id")
    cl.add_argument("clarification_id")
    cl.add_argument("--answers", required=True, help="JSON list of {topic, answer}")
    cl.set_defaults(func=cmd_clarify)

    rv = sub.add_parser("revise")
    rv.add_argument("run_id")
    rv.add_argument("--text", required=True)
    rv.add_argument("--reason", required=True)
    rv.set_defaults(func=cmd_revise)

    ap = sub.add_parser("approve")
    ap.add_argument("run_id")
    ap.add_argument("approval_id")
    ap.add_argument("--reject", action="store_true")
    ap.add_argument("--rationale")
    ap.set_defaults(func=cmd_approve)

    st = sub.add_parser("stop")
    st.add_argument("run_id")
    st.add_argument("--reason", default="human requested stop")
    st.set_defaults(func=cmd_stop)

    ex = sub.add_parser("export")
    ex.add_argument("run_id")
    ex.set_defaults(func=cmd_export)

    m = sub.add_parser("metrics")
    m.add_argument("--mode", choices=["live", "fixture"])
    m.set_defaults(func=cmd_metrics)

    sv = sub.add_parser("serve")
    sv.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    coord = build_coordinator()
    args.func(args, coord)
    return 0


if __name__ == "__main__":
    sys.exit(main())
