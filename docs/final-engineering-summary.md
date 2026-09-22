# Final engineering summary

## What was asked

Build, in one day, a local agentic software-engineering workbench whose runtime agents generate and
evolve a URL shortener through governed, stateful, non-linear orchestration: an explicit dependency
graph, durable state, human approval gates, real isolated tool execution, bounded recovery, dynamic
replanning, and reviewable evidence — demonstrated across greenfield, brownfield, and ambiguous
scenarios (`PROJECT-SPEC.md`, `BUILD-AND-DEMO.md`).

## What was built

- **Coordinator** (`coordinator/`): a small explicit graph executor (not a general agent framework) with
  a validated `TaskGraph`, durable SQLite state (`Store`, hash-chained events, leased single-scheduler
  ownership), a policy engine (`policy.py`) that is the only thing standing between model proposals and
  real filesystem/process effects, a disposable candidate `Workspace` with snapshot/restore, a Docker
  `DockerRunner` isolation boundary, two model adapters (`OpenAIAdapter` live, `FixtureAdapter`
  deterministic), event-derived `metrics.py`, and an evidence `export.py`.
- **Five logical runtime roles** (analyst, architect, implementer, reviewer, documenter), one shared
  adapter, each with a strict JSON schema (`schemas/`) and a role prompt (`prompts/`) that tells the model
  it is proposing, not acting, and that embedded repository text is data, never instructions.
- **The generated target application**: a FastAPI/SQLite URL shortener, evolved through three additive
  contract stages (`scenarios/contract_*.md`) — this is what the runtime agents actually produce, not
  something hand-built and merely narrated as agent output. Reference implementations exist under
  `fixtures/reference/` solely to back the deterministic fixture adapter and to give the trusted test
  suite something correct to validate itself against; the scenario evidence in `docs/scenarios.md` is
  from the coordinator actually running the pipeline, in both fixture and live modes.
- **Minimal UI + full-parity CLI** (`coordinator/api.py`, `coordinator/cli.py`, `ui/templates/`).
- **Trusted acceptance tests** (`trusted_tests/`, mounted read-only into the worker) and the
  **coordinator's own test suite** (`tests/`, 54 tests) covering every orchestration control the brief
  lists — cycles/gates, approval blocking and invalidation, parallel-branch overlap and join, bounded
  repair and rollback with hash evidence, safe stop, resume/reconciliation without duplicate mutation,
  replanning invalidation, policy escape attempts, untrusted-text injection, and metric-formula
  correctness on a fixed tiny dataset including zero-sample `N/A`.

## Key decisions (see `docs/architecture.md` for detail)

- A small purpose-built graph executor over an off-the-shelf workflow framework — matches the proposed
  spec, keeps the whole control plane inspectable in one sitting.
- The coordinator, not the model, inserts the fixed gates (`plan_approval`, `migration_approval` when
  needed, `join`, `release_approval`) around whatever implementation tasks the planner proposes — a model
  cannot omit a gate by leaving it out of its own plan.
- Structured JSON output validated both by the provider's schema-constrained decoding and locally
  (`jsonschema`) — schema conformance is treated as necessary, never sufficient; every proposal is still
  gated by real tool execution and human approval.
- Docker isolation is a hard prerequisite for any generated-code execution, not a soft fallback; the
  coordinator refuses rather than executing unsandboxed.

## Assumptions

See `PROJECT-SPEC.md §2`'s adopted defaults, and the task spec's own Assumptions section: OpenAI Chat
Completions with `gpt-5.4-mini` as the default live model (overridable via `OPENAI_MODEL`), a local Docker
daemon as the sole isolation boundary, a single local human identity label rather than real
authentication, and the five roles sharing one adapter process.

## Validation performed

- 54 coordinator unit/integration tests, all passing (`docs/testing-report.md`).
- All three trusted test stages (A/B/C, 20+34+43 = 97 assertions worth of scenarios) passing against
  hand-written reference implementations, inside the real Docker isolation boundary.
- Fixture-mode end-to-end runs for scenario A, scenario B (normal + labeled fault-injection recovery),
  and scenario C (clarification pause, migration approval, mid-flight requirement revision from 404 to
  410 with full invalidation/replanning), each exported to a complete evidence bundle.
- Live-service smoke tests: the exported scenario A and scenario C candidates launched for real and
  exercised over HTTP (create/redirect/stats, and the expiry boundary crossing 302→410).
- A live OpenAI run of scenario A: see `docs/live-evidence.md` for the exact outcome, including one
  honestly-included live failure recorded as recovery/limitation evidence.

## Risks and trade-offs

See `docs/limitations.md` and `docs/policy-matrix.md`.

## What is not done / explicitly deferred

<!-- finalized after the live run and EAOS finish; see the closing message for the authoritative list -->
