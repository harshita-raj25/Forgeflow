# Coordinator API/CLI reference

The coordinator exposes the same operations through CLI subcommands (`coordinator/cli.py`) and an HTTP
UI (`coordinator/api.py`), loopback-only by default. An OpenAPI schema for the *generated shortener*
itself is produced by FastAPI automatically at `/openapi.json` on the running candidate service
(`uvicorn app.main:app`) — see each exported candidate's own `API.md`.

## Coordinator routes (`coordinator/api.py`)

| Method | Path | Action |
|---|---|---|
| GET | `/` | List runs, create a new one |
| POST | `/runs` | Create + immediately resume a run for a scenario (CSRF-protected form) |
| GET | `/runs/{run_id}` | Run detail: graph state, approvals, clarifications, attempts |
| GET | `/runs/{run_id}/events.json` | Raw event log for the run |
| POST | `/runs/{run_id}/resume` | Resume dispatch |
| POST | `/runs/{run_id}/stop` | Safe stop |
| POST | `/runs/{run_id}/clarify` | Submit clarification answers |
| POST | `/runs/{run_id}/revise` | Submit a requirement revision (triggers replanning) |
| POST | `/runs/{run_id}/approvals/{approval_id}` | Approve or reject a pending approval |
| POST | `/runs/{run_id}/export` | Export the evidence bundle |
| GET | `/metrics.json` | Aggregate metrics (`?mode=live|fixture`) |
| GET | `/healthz` | Liveness |

Every mutating route requires a single-use CSRF token issued on the page that renders the form, checked
against the request's `Origin` header when present. The server binds to `127.0.0.1` unless
`FORGEFLOW_HOST` is explicitly overridden.

## CLI (`coordinator/cli.py`)

```
forgeflow scenarios
forgeflow create <scenario_id> [--requirement TEXT] [--inject-fault]
forgeflow resume <run_id>
forgeflow status <run_id>
forgeflow list
forgeflow clarify <run_id> <clarification_id> --answers '[{"id":"Q1","answer":"..."}]'
forgeflow revise <run_id> --text "..." --reason "..."
forgeflow approve <run_id> <approval_id> [--reject] [--rationale TEXT]
forgeflow stop <run_id> [--reason TEXT]
forgeflow export <run_id>
forgeflow metrics [--mode live|fixture]
forgeflow serve
```

## Generated shortener contract

See [`scenarios/contract_base.md`](../scenarios/contract_base.md),
[`contract_alias.md`](../scenarios/contract_alias.md), and
[`contract_expiry.md`](../scenarios/contract_expiry.md) for the authoritative, additive target contract
each scenario builds against. These are the same documents given to the runtime `architect`/`implementer`
roles as ground truth, and the same documents the trusted tests under `trusted_tests/` encode.
