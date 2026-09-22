# Policy-to-control matrix

| Control | Implemented | Tested | Notes |
|---|---|---|---|
| Path traversal / absolute path rejection | Yes | `tests/test_policy.py` | `normalize_candidate_path` |
| Symlink escape rejection | Yes | `tests/test_policy.py::test_symlink_escape_blocked` | `resolve_inside` walks every existing ancestor |
| Trusted-tests / coordinator paths never agent-writable | Yes | `tests/test_orchestration_e2e.py::test_trusted_tests_are_never_writable_by_candidate_edits` | allowlist is `app/*`, `tests/*` + a few root files only |
| Edit size/count limits | Yes | `tests/test_policy.py` | `Budget.max_file_bytes/max_files_per_edit/max_total_edit_bytes` |
| Task-scoped `allowed_paths` from approved plan | Yes | `tests/test_policy.py::test_validate_edits_rejects_task_scope_violation` | plan is human-approved before any edit |
| Fixed command allowlist (no arbitrary shell) | Yes | `tests/test_policy.py::test_fixed_command_rejects_arbitrary_names` | `lint`, `test`, `migrate` only |
| Untrusted repository/requirement text cannot override policy | Yes | `tests/test_orchestration_e2e.py::test_untrusted_repository_text_cannot_override_policy` | flagged via `untrusted_text_flagged` event; system prompt states the same rule; coordinator enforcement does not depend on the model |
| Human approval for plan / migration / release | Yes | `tests/test_orchestration_e2e.py::test_pending_approval_blocks_implementation`, `test_rejected_approval_blocks_release_and_fails_run` | synchronous gate nodes in the graph |
| Approval bound to exact hash; invalidated on change | Yes | `tests/test_orchestration_e2e.py::test_approval_invalidated_by_hash_change_requires_new_decision`, `test_requirement_revision_invalidates_stale_downstream_and_reruns` | subject hash = graph/requirement/candidate hash per scope |
| Bounded provider retries / structured-output correction | Yes | `coordinator/adapters/openai_live.py`, exercised live | 2 retries, 1 correction attempt |
| Bounded code-repair cycles | Yes | `tests/test_orchestration_e2e.py::test_fault_injection_triggers_bounded_repair_and_recovers` | 2 cycles, then rollback |
| Rollback with pre/post hash + failure evidence | Yes | `tests/test_orchestration_e2e.py::test_repair_budget_exhaustion_triggers_rollback` | `coordinator/workspace.py::restore` |
| Safe stop terminates workers, preserves state | Yes | `tests/test_orchestration_e2e.py::test_stop_prevents_further_dispatch` | kills active containers via `DockerRunner.kill_all` |
| Resume/restart reconciliation, no duplicate mutation | Yes | `tests/test_orchestration_e2e.py::test_resume_after_interrupt_reconciles_without_duplicate_mutation`, `test_export_is_idempotent_for_same_candidate_hash` | `operations` table + `INTERRUPTED` reconciliation |
| Event hash chain | Yes | `tests/test_store.py::test_event_chain_verifies_and_detects_tamper` | not tamper-proof storage; see `docs/limitations.md` |
| Secret redaction in events/exports | Yes | `tests/test_store.py::test_secrets_are_redacted_in_events` | heuristic; see `docs/limitations.md` |
| Worker container: no network, no credentials, no Docker socket, unprivileged | Yes | manual container inspection + `coordinator/policy.py::worker_env` assertion | `docker/Dockerfile.worker`, `DockerRunner.docker_args` |
| Metrics computed from events, zero-sample N/A | Yes | `tests/test_metrics.py` | no hard-coded favorable numbers |
| Fixture vs live mode visibly distinct everywhere | Yes | `run.mode`, every export's `run-summary.md`/`export-meta.json`, UI badge | `Coordinator._call_model` refuses a mode mismatch |

## Deferred / out of scope (documented, not implemented)

| Item | Status |
|---|---|
| Enterprise authentication / SSO | Deferred — local human label only |
| Multi-tenant identity, RBAC | Deferred |
| Regulatory compliance claims | Not claimed |
| Hosted/public deployment | Out of scope (local only, explicitly not authorized) |
| Tamper-proof external audit log | Deferred — local hash-chain only |
| General-purpose secret scanning | Deferred — heuristic regex only |
| Hardened multi-tenant sandbox (gVisor/Firecracker) | Deferred — Docker container boundary only |
