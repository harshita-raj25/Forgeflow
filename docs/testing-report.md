# Testing report

## Coordinator unit + integration tests

```
.venv/bin/python -m pytest -q tests
```

Result: **54 passed** (0 skipped, 0 failed), ~21s including real Docker container invocations.

Breakdown:
- `tests/test_graph.py` — 9 tests: topological order, cycle/unknown-dependency/duplicate-id/missing-gate
  rejection, task-count limit, standard-graph gate wiring (with and without migration), empty-plan
  rejection, transitive-dependent computation.
- `tests/test_policy.py` — 12 tests: path traversal/absolute-path/backslash/protected-root rejection
  (parametrized), allowed-path acceptance, symlink escape, edit size/count limits, task-scope
  enforcement, fixed-command allowlist, untrusted-text marker detection, credential-leak refusal in
  `worker_env`.
- `tests/test_store.py` — 5 tests: event chain verification and tamper detection, secret redaction in
  events, approval lifecycle and invalidation, idempotent-operation guard, node-attempt rerun lifecycle.
- `tests/test_metrics.py` — 5 tests: zero-sample `N/A` handling, provider-retry vs code-repair counting,
  MTTR computation, human-wait-time exclusion/inclusion, rollback/unrecovered-incident tracking.
- `tests/test_orchestration_e2e.py` — 15 tests, run against the real `Coordinator` + real Docker worker
  with the deterministic `FixtureAdapter` (never a live model call, so still fully deterministic):
  greenfield success with overlapping parallel branches, complete/verified evidence export, pending
  approval blocking implementation, rejected approval failing the run, approval invalidation on a
  requirement change, out-of-workspace/symlink write rejection, trusted-tests immutability, labeled
  fault injection with bounded repair and recovery, repair-budget exhaustion triggering rollback with
  hash evidence, safe stop preventing further dispatch, interrupted-attempt reconciliation on resume
  without duplicate mutation, idempotent export, untrusted-text injection attempt being flagged and
  still blocked by policy, clarification pause before any code change, and a mid-flight requirement
  revision invalidating downstream work/approvals and producing a genuinely different candidate hash.

## Trusted product tests (run inside the isolated Docker worker per candidate)

Verified directly against the reference implementations that ship in `fixtures/reference/`:

```
docker run ... forgeflow-worker:latest python -m pytest -q -p no:cacheprovider --timeout=100 /work/trusted_tests
```

| Stage | Reference | Result |
|---|---|---|
| A (core) | `fixtures/reference/A` | 20 passed, 23 skipped (stage B/C tests correctly skipped) |
| B (+ custom alias) | `fixtures/reference/B` | 34 passed, 9 skipped (stage C tests correctly skipped) |
| C (+ expiry, `FORGEFLOW_EXPIRED_STATUS=410`) | `fixtures/reference/C` | 43 passed, 0 skipped |

These are the same tests the coordinator's `validate` node runs against every model-generated candidate;
running them here against the hand-written reference confirms the trusted suite itself is correct and that
lint/test both execute inside the real isolation boundary with the real network/user/filesystem
restrictions.

## Live-service smoke test (scenario A export)

Started the exported greenfield candidate directly and exercised it over real HTTP:

```
POST /api/links {"url":"https://example.com/demo"} -> 201 {"code":"NgFvBCy3", ...}
GET  /r/NgFvBCy3                                    -> 302 Location: https://example.com/demo
GET  /api/links/NgFvBCy3/stats                      -> 200 {"click_count":1, ...}
```

Repeated for the scenario C candidate with an expiry boundary: link created 2s in the future, `GET /r/{code}`
returns `302` before the boundary and `410` after it, with `click_count` unchanged by the expired request
and `stats.status == "expired"`.

## Live model evidence

See `docs/scenarios.md` for the OpenAI-backed (`FORGEFLOW_MODE=live`, model `gpt-5.4-mini`) run: what
succeeded, and one honestly-included live failure (a non-additive-migration policy rejection on a
greenfield plan, recorded as recovery/limitation evidence per the brief's own allowance) alongside a
successful live run.

## Unexecuted checks

- The specification's `--comment`/multi-agent orchestrator review tooling is unrelated to this build and
  was not exercised.
- Load/concurrency testing beyond the trusted `test_concurrent_increments_do_not_lose_updates` case (8
  threads × 10 requests) was not performed; this is a local demo, not a load-tested service.
