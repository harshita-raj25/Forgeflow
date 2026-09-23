# Submission index — ForgeFlow

Prepared 23 September 2026, against `Assignment Agentic-Proficient Software Engineer.pdf`. This file maps
directly to the assignment's deliverable list (§5) so a reviewer can find each required item without
searching.

**Five-minute version:** [the ForgeFlow Dossier](https://claude.ai/artifact/5W5bnrH8UNmvo2XrJTZVgd) —
one page, the assignment's own 8 core requirements checked off against real evidence, the orchestration
graph, all three scenarios, the four-round adversarial hardening timeline, and both open limitations
stated as plainly as everything else. Start there; everything below is the full paper trail behind it.

## Working prototype (runnable end-to-end)

- Setup: [`README.md`](README.md) (`## Setup`, `## Configuration`, `## Running the coordinator`).
- The coordinator is the orchestration layer; the URL shortener is what its runtime agents generate.
  Neither was hand-built and narrated — see `docs/architecture.md` for the separation.

## Architecture overview

[`docs/architecture.md`](docs/architecture.md): components, control flow, state model, replanning
semantics, runtime agents, adapters, isolated runner, policy engine, evidence export, UI/CLI.

## Three scenarios (greenfield, brownfield, ambiguous)

[`docs/scenarios.md`](docs/scenarios.md) walks all three with exact commands and what each evidence bundle
shows (decomposition, orchestration gates, parallel-branch overlap, replanning). Evidence bundles live
under `runs/<run-id>/export/`.

- **Greenfield**: fixture-mode bundle plus at least one complete, successful **live** OpenAI run
  (`docs/live-evidence.md`).
- **Brownfield**: fixture-mode bundle including a separately-labeled fault-injection recovery
  demonstration. Live evidence for this scenario is not currently packaged — disclosed below, not hidden.
- **Ambiguous**: fixture-mode bundle showing the clarification pause, a migration approval gate, and a
  mid-flight requirement revision (404→410) that invalidates and re-executes downstream work. Live
  evidence for this scenario is not currently packaged — disclosed below, not hidden.

## Setup instructions

[`README.md`](README.md).

## Testing approach, limitations, and trade-offs

- Testing: [`docs/testing-report.md`](docs/testing-report.md) — 94 coordinator tests, trusted-suite results
  per stage, live-service HTTP smoke tests.
- Limitations and trade-offs: [`docs/limitations.md`](docs/limitations.md) — **read the "Two unresolved
  architectural gaps" section first**; it states plainly what is not closed, ahead of the routine
  local-prototype limitations (auth, SQLite scale, container hardening, etc.) below it.
- Control-by-control status: [`docs/policy-matrix.md`](docs/policy-matrix.md).

## Final engineering summary

[`docs/final-engineering-summary.md`](docs/final-engineering-summary.md) — what was asked, what was built,
the governance controls implemented and tested, the two open architectural gaps stated directly, live
evidence status, assumptions, validation performed, and an explicit recommendation on how to read this
submission.

## Review history (for context, not required reading)

Five rounds of external adversarial code review shaped this prototype (the fifth run independently, in a
fresh session with no access to the prior four). The pattern across rounds — each fix
closing the specific reproduced case without closing the underlying design problem for the two hardest
findings — is what `docs/limitations.md`'s top section and `docs/final-engineering-summary.md` state
directly.

## How to read this submission honestly

This is a bounded prototype demonstrating the required agentic orchestration, governance controls, and
engineering outputs with real, reproducible evidence. It is not adversarially hardened against a
determined attacker in the two specific ways `docs/limitations.md` describes. Read the final engineering
summary's closing recommendation before drawing conclusions from any single passing test or green
evidence bundle in isolation.
