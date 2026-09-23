# Limitations and trade-offs

This is a bounded local prototype built in one day. It demonstrates governed orchestration and the
required controls; it does not claim enterprise production readiness.

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
