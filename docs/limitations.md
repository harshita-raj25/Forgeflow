# Limitations and trade-offs

This is a bounded local prototype built in one day. It demonstrates governed orchestration and the
required controls; it does not claim enterprise production readiness.

## Two unresolved architectural gaps (stated plainly, not incrementally narrowed)

Four rounds of adversarial code review converged on two design-level gaps that remain open. Each round's
fix closed the *specific reproduced case* without closing the *underlying design problem*, and that
pattern is stated here explicitly rather than left to be inferred from a trail of narrower and narrower
fixes:

1. **The test evaluator shares a process with the generated candidate.** Every fix so far (rejecting
   empty output, requiring a real summary line, requiring a non-zero passed count, requiring at least the
   stage's known minimum passed count) narrows what a candidate can get away with, but none of them
   change the fact that pytest and the candidate's own code run in the same process and share the same
   output stream. A sufficiently motivated candidate can still influence what that process ultimately
   prints. The only fix that removes this boundary is running the trusted evaluator in a process genuinely
   separate from the candidate, with its own completion record the candidate cannot touch. That has not
   been implemented. Do not read the current `MIN_EXPECTED_PASSED` threshold as having closed this; it has
   not, and no further regex or count threshold in this same process will close it either.
2. **Lease ownership can change between the ownership check and the filesystem write.** The promote-lock
   fix in T-004 correctly blocks a stale scheduler whose lease had *already* transferred before it re-
   checked ownership. A fourth review found a narrower window inside that same fix: a scheduler can pass
   its ownership check, then lose its lease to a takeover *while still holding the publication lock and
   before finishing its write*, and that write still lands and reports success under the new owner's
   lease. The file lock only orders writers against each other; it does not make "check ownership" and
   "write" a single atomic step against a concurrent lease transfer. Closing this needs either an
   immutable staged snapshot with a transactional accepted-pointer update conditioned on owner/generation/
   lease-validity/revision together, or a lock that lease transfer itself must also acquire, consistently
   ordered with publication. Neither has been implemented.

Both are genuine, reproducible gaps in a governance system whose whole purpose is enforcing exactly these
guarantees. They are disclosed here as open, not as "mostly closed" or "narrowed to an edge case." Treat
every validation result and every publish-under-lease event this prototype produces as good evidence of
normal operation, not as a machine-checked proof against an adversarial candidate or a genuinely
adversarial multi-scheduler deployment.

## Local identity and authentication
- There is no authentication. Approvals and clarifications carry a configurable local human label
  (`FORGEFLOW_HUMAN`, default `owner`) recorded on every decision — this is an audit label, not an
  identity system. Anyone with access to the loopback UI/CLI can approve anything.
- The HTTP UI binds to `127.0.0.1` by default and checks a same-origin CSRF token on every mutating form;
  it has no session/login layer.

## SQLite and scale
- One SQLite database (WAL mode) per coordinator instance; one scheduler lease per run. This is a
  single-process, single-machine design. It is not intended for concurrent multi-writer or distributed use.
- The generated shortener's own SQLite database is likewise a local, single-file store — fine for the
  demo, not for public production scale (documented in the generated app's own README).

## Event chain
- The event hash chain (`prev_hash` + `sha256`) detects an edit made *after* it was chained, relative to
  an exported head you compare against. It is not tamper-proof storage: anyone with direct database access
  could rewrite the whole chain consistently. Its purpose here is to make an accidental or careless edit
  detectable during review, not to defeat a determined attacker with local filesystem access.

## Secret redaction
- `coordinator/util.py::redact` is a heuristic regex scan (common API key/token shapes, `key=value`
  patterns). It catches the credential shapes used by this prototype (OpenAI `sk-...` keys, bearer
  tokens, generic `api_key=`/`token=` assignments) but is not a general-purpose secret scanner.

## Container boundary
- The isolated runner assumes a working local Docker daemon with the pinned `forgeflow-worker:latest`
  image already built (`docker build -f docker/Dockerfile.worker docker/`). If Docker is unavailable,
  generated-code execution is refused outright (`RunnerUnavailable`) — there is no unsandboxed fallback.
- The container has no network, drops all capabilities, runs as an unprivileged uid, and gets no
  provider credentials or Docker socket. It is a single-host container boundary, not a hardened multi-tenant
  sandbox (no seccomp/AppArmor profile beyond Docker's defaults, no gVisor/Firecracker).

## Concurrency and cross-process coordination
- Two separate `forgeflow` CLI invocations (or a CLI invocation racing the HTTP API) safely order their
  writes to a shared run through a real OS-level file lock (`fcntl.flock` on a per-run lock file), not
  just an in-process `threading.Lock`. `fcntl` is POSIX-only; this assumes macOS or Linux, matching the
  existing Docker/`os.getuid()` platform assumptions elsewhere in the coordinator. There is no Windows
  support.
- The lease TTL (default 60s) is kept alive by a background heartbeat thread for the duration of
  `resume()`, independent of any single node's completion. A process that crashes without releasing its
  lease is still correctly reclaimed once the TTL lapses (no heartbeat thread left to renew it).
- Stop is ordered against an in-flight implement/plan write through the same file lock: it can prevent a
  write that has not yet started its critical section, or prevent a fresh attempt from starting one after
  Stop lands, but it cannot abort an OS-level write already in progress mid-`flock` (i.e., it is
  check-then-act at the lock boundary, not true interruption of an in-progress filesystem write once that
  write's own critical section has already begun).

## Test-completion trust boundary (residual gap, disclosed and deferred)
- `_pytest_last_line()` (`coordinator/scheduler.py`) parses only pytest's actual final summary line, not
  the whole stdout blob, closing the forgery an independent checker demonstrated: a candidate-triggered
  `warnings.warn("999 passed in 0.00s")` leaking a fake count out of pytest's own warnings-summary
  section. That checker then found a further, deeper bypass of the same general kind: candidate code that
  registers `atexit.register(lambda: print("999 passed in 0.00s"))` at import time prints *after*
  pytest's own genuine summary line but *before* the worker process exits, becoming the new last line and
  forging an accepted pass on a genuinely all-failing or zero-assertion run. This was verified empirically
  against the real, unmodified `_node_validate` handler.
- This is not fixed in code. The evaluator (pytest) and the generated candidate application currently
  share one process, so a candidate can always attempt to control what that process prints after pytest
  itself is done, no matter how the summary line is parsed. The only fix that closes this class of attack
  completely is running acceptance assertions in a process genuinely separate from the candidate — the
  same "fully separate supervised evaluator process" architecture the original code review suggested and
  every fix pass in this repository has explicitly kept out of scope as a larger architectural change.
  A cheaper partial mitigation exists (skip Python's own `atexit` machinery by having the worker's pytest
  invocation exit via `os._exit(pytest.main(...))` instead of a normal interpreter shutdown) but was not
  implemented in T-003; it remains a candidate for a future, explicitly scoped fix pass.
- Net position: the test-completion trust boundary is a genuinely bounded prototype control, not a
  complete one. It defends against the specific reproduced attacks (silent early exit, a bare zero-test
  or all-skipped result, and warning-message forgery mid-stream) but not against a sufficiently determined
  candidate willing to hook process-exit machinery. Treat any "validation passed" result from this system
  as strong but not airtight evidence, and do not rely on it alone for a genuinely adversarial candidate
  source in a higher-stakes deployment than this local prototype.
- **Minimum expected passed-test count (T-004, third code review round).** A third review found that
  `pytest_passed_count() >= 1` alone still accepted `1 passed, 42 skipped in 0.01s` — a candidate that
  suppresses nearly the entire trusted suite (via a conftest hack, markers, or environment tampering)
  while leaving one trivial test passing. `coordinator/scheduler.py::MIN_EXPECTED_PASSED` now requires at
  least the trusted suite's known minimum passed count for the active stage (20/34/43 for A/B/C) instead
  of a bare non-zero count. This is a hardcoded, disclosed trade-off: it must be kept in sync by hand if
  `trusted_tests/` grows or shrinks, and it still does not establish that the *specific expected* tests
  ran versus some other combination totalling the same count — it raises the bar substantially without
  being the complete fix. As with every other version of this gap, only a fully separate evaluator process
  closes it completely, and that remains out of scope.

## Model variability and budgets
- Live model calls are bounded (90s timeout, ≤2 retries, ≤1 structured-output correction, ≤2 code-repair
  cycles, ≤30 provider calls and ≤20 minutes active execution per run). A model that produces working code
  on the first attempt is not guaranteed; that variability is why the deterministic fixture path exists
  for engine testing, and why live evidence is captured and labeled separately in every export.
- Schema-valid output is not semantically correct output; every model proposal is still checked by real
  tool execution (lint, trusted tests) and a human approval gate before it can affect anything.

## Metrics
- `metrics.json`/`coordinator/metrics.py` numbers are computed from this prototype's own small number of
  demo runs. They demonstrate the instrumentation and formulas (including correct zero-sample `N/A`
  handling), not a statistically established reliability or performance claim.

## Scope not implemented
- No authentication, multi-tenancy, custom domains, or public abuse protection in the generated shortener
  (documented in its own README/limitations, per the assignment's explicit scope).
- No hosted deployment; everything here runs and is reviewed locally.
- The reviewer/documenter/architect roles are single-pass per candidate revision; there is no
  multi-round internal debate between roles beyond the repair loop.
