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
