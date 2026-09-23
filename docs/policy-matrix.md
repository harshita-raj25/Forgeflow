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
| Safe stop terminates workers, preserves state | Yes | `tests/test_orchestration_e2e.py::test_stop_prevents_further_dispatch`; `tests/test_review_fixes_4.py::test_decide_approval_cannot_resurrect_a_run_stopped_mid_call`, `::test_revise_requirement_cannot_resurrect_a_run_stopped_mid_call`, `::test_answer_clarification_cannot_resurrect_a_run_stopped_mid_call` | kills active containers via `DockerRunner.kill_all`; a fifth review found `decide_approval`/`revise_requirement`/`answer_clarification` could silently resurrect a just-`STOPPED` run back to `PENDING` (stale read, no compare-and-set) — fixed via `Store.set_status_if` under the same `_promote_lock` `stop()` holds |
| Host-side write during migration cannot follow a candidate-planted symlink | Yes | `tests/test_review_fixes_4.py::test_migration_result_write_refuses_to_follow_a_candidate_planted_symlink` | `coordinator/policy.py::write_text_no_symlink` (`O_NOFOLLOW`), used at `_apply_demo_migration`'s host-side result write; fifth review round finding |
| Expired-link status oracle is not flippable by requirement/clarification prose | Yes | `tests/test_review_fixes_4.py::test_expiry_oracle_reads_the_structured_field_not_prose`, `::test_expiry_oracle_is_not_flipped_by_the_reviewer_negation_trap_sentences`, `::test_expiry_oracle_end_to_end_through_real_clarification_and_revision` | structured, schema-required `expired_link_status` field on the analyst's output, read directly by `_expected_expired_status`; replaces a prior "404"/"410" substring search over freeform text that a fifth review showed could be flipped by a negated or out-of-context mention |
| Resume/restart reconciliation, no duplicate mutation | Yes | `tests/test_orchestration_e2e.py::test_resume_after_interrupt_reconciles_without_duplicate_mutation`, `test_export_is_idempotent_for_same_candidate_hash` | `operations` table + `INTERRUPTED` reconciliation |
| Event hash chain | Yes | `tests/test_store.py::test_event_chain_verifies_and_detects_tamper` | not tamper-proof storage; see `docs/limitations.md` |
| Secret redaction in events/exports | Yes | `tests/test_store.py::test_secrets_are_redacted_in_events` | heuristic; see `docs/limitations.md` |
| Worker container: no network, no credentials, no Docker socket, unprivileged, read-only root | Yes | `tests/test_orchestration_e2e.py::test_worker_container_isolation_holds_at_runtime` (a probe, `trusted_tests/tools/check_isolation.py`, runs *inside* the container and asserts each property at runtime, not just the docker-run flags) | `docker/Dockerfile.worker`, `DockerRunner.docker_args`, `coordinator/policy.py::worker_env` |
| Metrics computed from events, zero-sample N/A | Yes | `tests/test_metrics.py` | no hard-coded favorable numbers |
| Fixture vs live mode visibly distinct everywhere | Yes | `run.mode`, every export's `run-summary.md`/`export-meta.json`, UI badge | `Coordinator._call_model` refuses a mode mismatch |
| Test result cannot be a false pass from candidate-controlled output | Partial | `tests/test_review_fixes.py::test_evaluator_early_exit_cannot_produce_false_pass`, `tests/test_review_fixes_2.py::test_zero_assertion_results_rejected_by_validate_node`, `tests/test_review_fixes_3.py::test_suspiciously_low_passed_count_rejected_for_stage_a` | requires a pytest summary line AND at least the stage's known minimum passed-test count (not just ≥1) for exit-code-0; still runs the evaluator in the same process as the candidate (not a fully separate supervised process) — see `docs/limitations.md` |
| Stale lease holder cannot publish after ownership *already* transferred before its check | Partial | `tests/test_review_fixes_3.py::test_stale_lease_holder_cannot_publish_after_takeover`, `::test_concurrent_resume_after_real_takeover_full_pipeline` | `_promotion_blocked` checks `lease_owner == self.owner` under `_promote_lock`; a fourth review found ownership can still transfer *between* that check and the write completing (check-then-act is not atomic with a concurrent lease transfer) — open, see `docs/limitations.md` |
| Cross-process stale-write / stop ordering | Yes | `tests/test_review_fixes_2.py::test_cross_process_lock_provides_real_mutual_exclusion`, `::test_stop_prevents_in_flight_write_from_landing` | `Coordinator._promote_lock` (`fcntl.flock`), shared by implement/plan promote, `revise_requirement`, and `stop()` |
| Lease survives an in-flight long dispatch | Yes | `tests/test_review_fixes_2.py::test_lease_survives_in_flight_dispatch_via_heartbeat` | dedicated heartbeat thread renews independent of node completion |
| A stopped run's containers are the only ones killed | Partial | — | a fifth review found `stop()` kills *every* active container in the coordinator process (`DockerRunner.kill_all` has no run-id filter); only manifests with two runs' containers active at once in one `serve` process — see `docs/limitations.md` |
| Candidate cannot disable its own lint gate | No | — | fifth review: `pyproject.toml` is an allowed candidate file and the lint command has no `--isolated`/`--no-config`, so a candidate-written `extend-exclude` self-mutes ruff — see `docs/limitations.md` |
| A failed release cannot export looking like a successful one | No | — | fifth review: `release-manifest.json` is written before the stage-C migration runs; a migration failure after that point still leaves a manifest that suppresses the "NOT RELEASED" marker — see `docs/limitations.md` |
| Generated candidate tests are executed, not just present | No | — | fifth review: the fixed `test` command only ever runs `/work/trusted_tests`, never the candidate's own `tests/` — see `docs/limitations.md` |
| Parallel implementation tasks cannot silently overwrite each other | Partial | — | fifth review: `_promote_lock` orders writes but does not detect that two parallel tasks' whole-file edits conflicted; not reachable by current scenario fixtures (always dependency-chained) but possible for a live plan with genuinely independent parallel tasks — see `docs/limitations.md` |

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
