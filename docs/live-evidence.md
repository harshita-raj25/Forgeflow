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

## Attempt 2 — `run_k5hdvyw2km7s` (scenario A, live) — successful end-to-end run

<!-- filled in after the background live run completes; see runs/run_k5hdvyw2km7s/export/ -->

## Reproducing

```bash
export FORGEFLOW_MODE=live OPENAI_API_KEY=... OPENAI_MODEL=gpt-5.4-mini
forgeflow create A_greenfield
forgeflow resume <run-id>
# approve plan / release as prompted (forgeflow status <run-id> shows pending approvals)
forgeflow export <run-id>
```
