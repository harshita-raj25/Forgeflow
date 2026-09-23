# Final engineering summary

Written 23 September 2026, updated after a fifth and sixth round of external adversarial code review (both
run independently, each in a fresh session with no access to the others). This is the accurate, current
state of the prototype — not a snapshot from an earlier round.

## What was asked

Build a working prototype (per `Assignment Agentic-Proficient Software Engineer.pdf`) that turns a
requirement into a reviewable engineering outcome through an agentic execution model: requirement
understanding, task decomposition, codebase reasoning for brownfield work, a stateful non-linear
orchestration layer with an explicit dependency graph, entry/exit gates, parallel paths with
synchronization, decision lineage, human approval checkpoints, bounded retry/fallback/rollback/safe-stop,
policy guardrails, audit-grade observability, reliability metrics, and dynamic replanning — demonstrated
across a greenfield, a brownfield, and an ambiguous scenario, with real code/tests/docs as output and a
final engineering summary covering plan, artifacts, risks, assumptions, and limitations.

## What was built

- **Coordinator** (`coordinator/`): a small explicit graph executor (not a general agent framework) with a
  validated `TaskGraph`, durable SQLite state (hash-chained events, a leased scheduler with a real
  cross-process file lock and a background lease-renewal heartbeat), a policy engine that is the only
  thing standing between model proposals and real filesystem/process effects, a disposable candidate
  `Workspace` with snapshot/restore, a Docker isolation boundary for all generated-code execution, two
  model adapters (`OpenAIAdapter` live, `FixtureAdapter` deterministic), event-derived metrics, and
  evidence export.
- **Five logical runtime roles** (analyst, architect, implementer, reviewer, documenter) sharing one
  adapter, each with a strict JSON schema and a role prompt that tells the model it is proposing, not
  acting, and that embedded repository text is data, never instructions.
- **The generated target application**: a FastAPI/SQLite URL shortener evolved through three additive
  contract stages — core, custom aliases, optional expiry. This is what the runtime agents actually
  produce, not something hand-built and narrated as agent output.
- **Minimal UI + full-parity CLI**, trusted acceptance tests mounted read-only into the worker, and the
  coordinator's own test suite: **96 tests**, all passing, run inside the real Docker isolation boundary
  where relevant.

## Governance controls implemented and tested

Explicit dependency graph with coordinator-inserted (not model-proposed) gates; parallel validation/
review/docs branches with measured execution overlap; human approval bound to an exact content hash,
invalidated on any change; bounded provider retries and structured-output correction; bounded code-repair
cycles with rollback and hash evidence on exhaustion; safe stop that terminates active workers; crash
reconciliation without duplicate mutation; dynamic replanning that invalidates affected work and stale
approvals and produces a genuinely different candidate on a requirement change; a policy engine blocking
path traversal, symlink escape, out-of-scope writes, arbitrary commands, and untrusted-text instruction
injection; a Docker worker with no network, no credentials, no Docker socket, an unprivileged user, and a
read-only mount, verified from *inside* the container at runtime; CSRF/Origin protection against a fixed
trusted-host allowlist; event-derived metrics with correct zero-sample `N/A` handling.

## Two known, unresolved architectural gaps — stated plainly

Four rounds of adversarial review converged on two design-level gaps that remain open after several
rounds of fixes. Each fix closed the specific reproduced case, not the underlying design problem, and
that is stated here directly rather than left implicit (see `docs/limitations.md` for full detail):

1. **The test evaluator shares a process with the generated candidate.** Validation has been strengthened
   repeatedly (reject empty output → require a real summary line → require a non-zero passed count →
   require at least the stage's known minimum passed count), and each strengthening closed a real,
   reproduced bypass. None of them close the underlying problem: pytest and the candidate's own code run
   in the same process and share the same output stream, so a sufficiently motivated candidate can still
   influence what that process ultimately reports. The complete fix — a genuinely separate, supervised
   evaluator process — has not been implemented.
2. **Lease ownership can change between the ownership check and the filesystem write completing.** A
   cross-process file lock and a lease-ownership check were added to stop a stale scheduler from
   publishing after a takeover. A fourth review found a narrower window inside that same fix: ownership
   can still transfer *while* a scheduler holds the publication lock, between its ownership check and its
   write finishing, and that write still lands under the new owner's lease. Closing this needs either a
   transactional accepted-snapshot pointer conditioned on owner/generation/lease-validity together, or a
   lock that lease transfer itself must also acquire — neither implemented.

Both remain open by explicit decision, not oversight: closing either completely is a larger architectural
change than this prototype's scope, and further narrowing the same fix with another regex, count
threshold, or check would only produce another round finding a narrower window still. The honest position
is that this system demonstrates the required governance controls and is not adversarially hardened
against a determined attacker in either of these two specific ways.

## Fifth and sixth review rounds (independent sessions)

A fifth review, run independently with no access to the first four, found 8 further issues by reading the
source and reproducing each one. Three were fixed (a host-side symlink-following write during the stage-C
migration; a Safe Stop that a concurrent approval/revision/clarification call could silently undo; an
expired-link status oracle that trusted prose instead of a structured field) and independently
checker-verified. The other five were judged real but narrower in blast radius and are disclosed, not
fixed, for the same reason feature work was frozen after round four: chasing every adversarial finding to
closure has diminishing returns against a submission deadline.

A sixth review, independent of the fifth, checked that commit's three fixes by reproducing each from
scratch (real Docker, real concurrent threads, real jsonschema validation) and confirmed all three hold —
then found the Safe Stop fix itself was too narrow: it guarded the three human-triggered call sites but not
the scheduler's own internal status writes (the approval gate, a blocking clarification, a repair cycle),
which had the same unconditional-write shape and could resurrect a just-stopped run the same way. Fixed at
the root in `Store.set_status`/`set_status_if` instead of auditing each call site, so no future caller can
reopen the same class of gap either.

Full detail, including exactly which of the two architectural gaps above these rounds does and does not
touch (none of the findings from either round are the same class of issue as either gap), is in
`docs/limitations.md`'s "Fifth review round" section.

## Live model evidence

A complete, real OpenAI-backed run of the greenfield scenario has been demonstrated end to end at least
once: every role's model call was real, the first live-generated candidate had a genuine bug caught by
the trusted test suite, a real bounded-repair cycle produced a clean second candidate, a human approval
gate was exercised, and the exported service was launched and used over real HTTP. See
`docs/live-evidence.md` for the full trail, including two honestly-disclosed operational mistakes (this
session repeatedly destroyed its own `runs/` evidence directory during test iteration) and several further
live attempts that hit real, transient OpenAI provider instability rather than a coordinator defect.

Brownfield and ambiguous scenario evidence is currently **fixture-mode only** — it exercises the same
orchestration engine, gates, and trusted-test validation, but not live model reasoning for those two
scenarios specifically. This is disclosed because it weakens the demonstration of live agentic reasoning
on brownfield/ambiguous work, even though the orchestration machinery itself is identical and independently
tested in both modes. See `docs/scenarios.md` for the current fixture-mode evidence bundles and
`docs/live-evidence.md` for what live evidence exists and how to reproduce more.

## Assumptions

OpenAI Chat Completions with `gpt-5.4-mini` as the default live model; a local Docker daemon as the sole
isolation boundary; a single local human identity label rather than real authentication; the five roles
sharing one adapter process; POSIX (`fcntl`) for the cross-process lock, no Windows support.

## Validation performed

- 96 coordinator unit/integration tests, all passing, across six fix passes responding to six review
  rounds (`docs/testing-report.md`).
- All three trusted test stages passing against hand-written reference implementations inside the real
  Docker isolation boundary.
- Fixture-mode end-to-end runs for all three scenarios, each exported to a complete, hash-verified
  evidence bundle (`docs/scenarios.md`).
- At least one complete, successful live OpenAI run of the greenfield scenario, independently verified by
  a checker (`docs/live-evidence.md`).
- Live-service smoke tests: exported candidates launched for real and exercised over HTTP.
- Four independent checkers (clean context, no access to the builder's reasoning), each re-running tests,
  constructing their own adversarial reproductions — including genuinely separate OS processes, not just
  threads — and in two cases finding real bypasses in the prior fix that were then disclosed and
  addressed rather than hidden.

## Risks and trade-offs

See `docs/limitations.md` (all limitations, including the two open architectural gaps above) and
`docs/policy-matrix.md` (control-by-control implementation and test status).

## Recommendation

This is a bounded, credible prototype that demonstrates the required governance controls with real,
reproducible evidence — not a system with every guarantee adversarially proven. It is defensible to submit
with the two gaps above disclosed exactly as stated, not papered over. It should not be submitted, or
read, as claiming complete protection against an adversarial candidate or a genuinely adversarial
multi-scheduler deployment.
