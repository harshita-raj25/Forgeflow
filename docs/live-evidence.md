# Live model evidence

Provider: OpenAI Chat Completions, `OPENAI_MODEL=gpt-5.4-mini`, real API key read from the coordinator
process environment only (never logged, never passed to the worker container —
`coordinator/policy.py::worker_env` asserts this).

## Attempt 1 — `run_393myqg9szwq` (scenario A, live) — honest failure, included as recovery evidence

`intake` and `requirements` succeeded with real model output (the live analyst produced 15 acceptance
criteria from the greenfield requirement, `criteria: 15` in the `node_finished` event, `usage` recorded:
2588 prompt / 3356 completion tokens, `latency_ms: 20540`). The `plan` node's live architect returned a
plan with `migration.required=true` and `migration.additive_only=false` for a **greenfield** run (nothing
to migrate from). The coordinator's policy correctly refused it:

```
node_finished plan {'status': 'FAILED', 'error_category': 'policy',
  'error': 'non-additive migrations are not permitted in this prototype', 'repairable': False}
```

This is genuine live-model variance, not a coordinator bug: schema-valid output is not semantically
correct output, and the policy check did exactly its job — it stopped a plan that misclassified a
greenfield build as a non-additive migration, rather than silently accepting it. The run correctly ended
`FAILED` with the reason preserved in `runs/run_393myqg9szwq/events.jsonl`. Per the brief: *"A failed live
run may be honestly included as recovery evidence, but it does not replace a successful end-to-end live
run."* A successful live run follows below.

## Attempt 2 — `run_k5hdvyw2km7s` (scenario A, live)

The live run progressed further: `intake`, `requirements`, `plan`, `plan_approval`, `migration_approval`
(the live architect asked for a migration gate again; this time additive, so policy accepted it) and
`t1` (an implementation task) all `SUCCEEDED` with real model output — 253 lines of generated
`app/main.py` following the contract (writable `/tmp` default DB path, the specified code alphabet, the
2048-char URL length limit; see `docs/live-artifacts/salvaged-live-t1-main.py` and
`docs/live-artifacts/salvaged-live-plan-r1.json`/`salvaged-live-requirements-r1.json`, copied out of the
run's surviving candidate workspace). `t2` then failed with a genuine transient provider error after the
configured retries (`error_category: provider_error`), and the run correctly stopped
(`STOPPED`, reason `"provider failure in t2 (provider_error): checkpointed for human intervention. Live
mode never falls back to fixtures."`) rather than substituting anything.

**Operational mistake, disclosed honestly:** while this run was still active in the background, I ran a
housekeeping `rm -f forgeflow.db*` for an unrelated EAOS check step on the *same* database file the live
run's process had open. SQLite unlinks the path but a process with the file already open keeps writing to
the deleted inode; a new process opening the same path afterward gets an empty database. The run's
`events.jsonl`/approvals/artifact rows for this specific attempt are therefore not recoverable from the
database, and no full evidence bundle could be exported for it. Its *filesystem* workspace
(`runs/run_k5hdvyw2km7s/candidate/`, `artifacts/`) survived independently of the database and was used to
salvage the generated code and plan above. This was my error, not a defect in the coordinator's own
isolation (the mistake was an operator action against the coordinator's own state file, exactly the kind
of thing `docs/limitations.md`'s "single scheduler lease, single database" design assumes won't happen
concurrently with housekeeping). A third, fully isolated attempt follows.

## Attempt 3 — `run_y5bhsca8tqau` (scenario A, live) — successful end-to-end run

Run in its own database and runs directory (`FORGEFLOW_DB=/tmp/forgeflow-live-final.db`,
`FORGEFLOW_RUNS_DIR=/tmp/forgeflow-live-final-runs`), untouched by any other command while in flight, then
copied into `runs/run_y5bhsca8tqau/` for permanent evidence.

**Result: `SUCCEEDED`**, `execution_mode: live`, event chain verified (115 events,
`"115 events verified"`), zero redacted secrets (none present to redact).

The live implementer's **first** candidate (revision 1) failed one trusted test inside the real Docker
isolation boundary — `validation/c1-saf5e8/test.json`: `1 failed, 19 passed, 23 skipped` — a genuine bug
in live-generated code, not a scripted fault. This triggered a real bounded-repair cycle
(`repair_started`, cycle 1 of 2): the live implementer was re-invoked with the failing test output as
context, produced a second candidate (revision 2), and `validation/c2-htx5j3/test.json` shows
`0 exit, 20 passed, 23 skipped` — clean. `join` then passed, `docs`/`review` completed, release was
approved, and the candidate was exported.

The exported live-generated candidate was launched for real (`uvicorn app.main:app`,
`runs/run_y5bhsca8tqau/export/release/candidate`) and exercised over real HTTP:

```
POST /api/links {"url":"https://example.com/live-final-demo"}
  -> 201 {"code":"R4YHTWSC","short_url":"http://localhost:18355/r/R4YHTWSC", ...}
GET  /r/R4YHTWSC -> 302 Location: https://example.com/live-final-demo
GET  /api/links/R4YHTWSC/stats -> 200 {"click_count":1, ...}
```

This is the required successful end-to-end live run: real model calls for every role, a real failure and
real bounded repair (not scripted), real trusted-test validation inside the isolation boundary, a real
human approval gate, and a real working generated service. Full bundle:
`runs/run_y5bhsca8tqau/export/`.

## Reproducing

```bash
export FORGEFLOW_MODE=live OPENAI_API_KEY=... OPENAI_MODEL=gpt-5.4-mini
forgeflow create A_greenfield
forgeflow resume <run-id>
# approve plan / release as prompted (forgeflow status <run-id> shows pending approvals)
forgeflow export <run-id>
```
