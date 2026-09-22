# Scenario evidence

All commands below use the CLI shown in the README. Run ids are from an actual local rehearsal in this
repository's `runs/` directory (fixture mode unless noted) — exported bundles are under
`runs/<run-id>/export/`.

## Scenario A — greenfield

Run: `run_qy667qpcjvwf` (fixture) — `runs/run_qy667qpcjvwf/export/`

```
forgeflow create A_greenfield
forgeflow resume <run-id>                       # -> WAITING_FOR_APPROVAL (plan)
forgeflow approve <run-id> <plan-approval-id>
forgeflow resume <run-id>                       # -> WAITING_FOR_APPROVAL (release), after
                                                 #    t1/t2 implement, freeze, validate+review+docs (parallel), join
forgeflow approve <run-id> <release-approval-id>
forgeflow resume <run-id>                       # -> SUCCEEDED, exports the release
```

Evidence: `requirement-revisions.json` (1 revision, 7 acceptance criteria, zero blocking questions —
the target contract fully specifies greenfield behavior), `graph-revisions.json` (skeleton then the
planner-generated graph with the coordinator-inserted gates), `approvals.json` (plan + release, both
approved), `decisions.json` (architect's ORM-vs-stdlib-sqlite rationale), `candidate.diff` (the full
generated `app/main.py` + tests + README), `validation/` (real `ruff` + `pytest` output from inside the
Docker worker against the trusted stage-A suite — 20 passed), `events.jsonl` with a `join_passed` event
whose `overlap.overlapping_pairs` proves `validate`/`review`/`docs` actually ran concurrently, and
`release/release-manifest.json` (candidate hash, baseline hash `null` for greenfield).

The exported candidate was launched directly (`uvicorn app.main:app`) and exercised over real HTTP:
create → 302 redirect → `click_count` incremented to 1 (see `docs/testing-report.md`).

## Scenario B — brownfield custom aliases

Normal run: `run_ncmdyd87g2r7` (fixture) — baseline is scenario A's own release
(`runs/run_qy667qpcjvwf/export/release/candidate`), auto-resolved by
`Coordinator.latest_release_dir("A_greenfield")`.

Evidence: `runs/run_ncmdyd87g2r7/export/decisions.json` impact map cites real paths/symbols from the
baseline (`app/main.py::create_link`, `create_app`) via the `baseline_analysis` node's `ast`-based
symbol inventory (`runs/f.../artifacts/baseline-inventory.json`); `release-manifest.json`'s
`baseline_hash` equals scenario A's `candidate_hash`, proving lineage; `validation/` shows 34 passed
(regression + new alias tests) for stage B.

**Controlled recovery demonstration** (separate, clearly labeled run):
`run_r9fxxc5hzw2u` (fixture, `--inject-fault`). `events.jsonl` shows, in order: a `fault_injected` event
(`label: FORGEFLOW_INJECTED_FAULT`, one-time, on `app/main.py`), a real `tool_result` for `test` with
`exit_code: 1` from inside the Docker worker, a `node_finished` (`validate`, `FAILED`,
`repairable: true`), a `repair_started` event (`cycle: 1`, `limit: 2`), then a clean re-implementation
and a `join_passed` event, ending `SUCCEEDED` with `repair_cycles: 1`. The fault is explicitly labeled in
every event and in `run-summary.md` (`Fault injection: YES`) — never presented as a naturally discovered
defect. A normal (non-injected) run (`run_ncmdyd87g2r7`) is kept as the product evidence.

## Scenario C — ambiguous requirement + upstream replanning

Run: `run_wjqzzwfbyt85` (fixture) — baseline is scenario B's release.

1. **Clarification pause.** `forgeflow resume` on the fresh run returns `WAITING_FOR_INPUT` before any
   plan or code exists (`clarifications.json`, 4 blocking questions: what makes a link old, what happens
   to existing links, what status code, what analytics). `states` at this point contains only
   `intake/baseline_analysis/requirements` — no `t1`/`t2`/`freeze` node exists yet.
2. **Answers submitted**, stored as a versioned clarification record
   (`forgeflow clarify <run-id> <clarification-id> --answers '[...]'`) choosing an explicit `expires_at`,
   existing links never expiring, **404** for expired links, and aggregate-only analytics.
3. Plan approval, then a **migration approval** (additive `expires_at` column) — a gate scenario A/B never
   need, inserted by the coordinator because the architect's `migration.required=true`.
4. Implementation reaches the release-approval checkpoint with the **404** contract built and validated
   (candidate revision 1) — `checkpoint candidate_revision: 1` — and is left there, not yet released.
5. **Requirement revision** submitted while checkpointed: *"expired link must return HTTP 410 Gone, not
   404."* `replan_impact` event shows `t1`/`t2`/`freeze`/`validate`/`review`/`docs`/`plan` invalidated
   (cascaded via `TaskGraph.affected_by_inputs`/`dependents`) and `approvals.json` shows the plan,
   migration, and release approvals for the 404 candidate flipped to `invalidated` with a reason.
6. Fresh plan/migration/release approvals are requested against a **new graph revision**
   (`graph-revisions.json` revision 2, with a `diff`), re-execution produces a **new candidate hash**
   (revision 2), and the final release manifest is for the 410 candidate.
7. Final trusted tests (`FORGEFLOW_EXPIRED_STATUS=410`, stage C) pass: before/at/after the expiry
   boundary using an injected clock, existing (non-expiring) links unaffected, additive migration
   preserves baseline rows/click counts (`trusted_tests/tools/migrate_demo.py` backup+verify evidence
   under `demo/migration-result.json`).

The exported 410 candidate was launched directly and exercised over real HTTP: a link with a 2-second
future expiry redirected `302` before the boundary and returned `410` after it, with `click_count`
unchanged and `stats.status == "expired"` (see `docs/testing-report.md`).

## Live model evidence

`FORGEFLOW_MODE=live`, `OPENAI_MODEL=gpt-5.4-mini`, real `OPENAI_API_KEY`. See
`docs/live-evidence.md` for the exact runs, including one honestly-included live failure.
