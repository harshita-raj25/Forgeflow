"""Build deterministic fixture JSON files (execution_mode=fixture) from the reference implementations.

Run: python fixtures/build_fixtures.py
Writes fixtures/<scenario>/<role>[.<task_id>][.rev<N>].json matching schemas/<role>.json.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
REF = HERE / "reference"


def read_files(root: Path, prefix: str = "") -> list[dict]:
    """prefix e.g. 'app' or 'tests' so output paths match the candidate workspace layout (app/..., tests/...)."""
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in (".py", ".md", ".txt"):
            rel = p.relative_to(root).as_posix()
            out.append({"path": f"{prefix}/{rel}" if prefix else rel, "op": "write", "content": p.read_text()})
    return out


def write(rel: str, data: dict) -> None:
    p = HERE / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n")
    print("wrote", rel)


# ---------------------------------------------------------------- Scenario A
write(
    "A_greenfield/analyst.json",
    {
        "normalized_requirement": "Provide a local URL shortener with link creation, redirect, aggregate click analytics, persistent SQLite storage, health/readiness endpoints, tests and setup docs.",
        "assumptions": ["No authentication or multi-tenancy.", "8-character codes with a cryptographically secure generator.", "Analytics are aggregate click counts only, no visitor identity."],
        "acceptance_criteria": [
            {"id": "AC-1", "statement": "POST /api/links creates a link and returns code, short_url, url, created_at with 201."},
            {"id": "AC-2", "statement": "GET /r/{code} returns 302 to the target and increments click_count once; unknown code returns 404."},
            {"id": "AC-3", "statement": "HEAD /r/{code} returns 302/404 without incrementing click_count."},
            {"id": "AC-4", "statement": "GET /api/links/{code}/stats returns code, url, created_at, click_count; unknown returns 404."},
            {"id": "AC-5", "statement": "Invalid URLs (bad scheme, credentials, control chars, overlong) are rejected with 422."},
            {"id": "AC-6", "statement": "healthz and readyz report liveness and database readiness; readyz is 503 when the database is unavailable."},
            {"id": "AC-7", "statement": "Data persists across restarts and concurrent redirects do not lose click updates."},
        ],
        "blocking_questions": [],
        "non_blocking_questions": [],
        "risk_tags": ["new-service", "public-api"],
        "notes": "Greenfield; target contract fully specifies behavior, no clarification needed.",
        "expired_link_status": None,
    },
)
write(
    "A_greenfield/architect.json",
    {
        "design_summary": "Single FastAPI app package app/ backed by sqlite3 (stdlib), per-request connections, atomic click increments, secure random codes with bounded collision retry.",
        "impact_map": [{"path": "app/main.py", "symbol": "create_app", "change": "create", "reason": "New service; no existing baseline."}],
        "migration": {"required": False, "description": "", "additive_only": True},
        "tasks": [
            {
                "task_id": "t1",
                "title": "Implement the shortener service",
                "dependencies": [],
                "allowed_paths": ["app/*"],
                "acceptance_criteria_ids": ["AC-1", "AC-2", "AC-3", "AC-4", "AC-5", "AC-6", "AC-7"],
                "risk_level": "medium",
                "instructions": "Implement app/main.py exactly per the target contract: create_app factory, sqlite3 schema, all endpoints, validation, secure code generation with 5-attempt bounded retry, atomic increment, health/ready checks.",
            },
            {
                "task_id": "t2",
                "title": "Add agent-authored smoke tests and README",
                "dependencies": ["t1"],
                "allowed_paths": ["tests/*", "README.md"],
                "acceptance_criteria_ids": ["AC-1", "AC-2"],
                "risk_level": "low",
                "instructions": "Add a small pytest smoke test under tests/ exercising create+redirect+stats, and a README with run/test instructions.",
            },
        ],
        "decisions": [{"decision": "Use stdlib sqlite3 with per-request connections instead of an ORM.", "rationale": "Keeps the dependency surface minimal and matches the trusted test import contract."}],
        "required_approvals": ["plan", "release"],
        "risks": ["SQLite is single-file and not horizontally scalable; acceptable for a local prototype."],
    },
)
write("A_greenfield/implementer.t1.json", {"rationale": "Implement app/main.py per the target contract.", "edits": read_files(REF / "A" / "app", "app"), "tests_added": [], "notes": "Reference implementation of the stage A contract."})
write("A_greenfield/implementer.t2.json", {"rationale": "Add smoke tests and README.", "edits": read_files(REF / "A" / "tests", "tests") + [{"path": "README.md", "op": "write", "content": (REF / "A" / "README.md").read_text()}], "tests_added": ["tests/test_app.py"], "notes": ""})
write("A_greenfield/reviewer.json", {"verdict": "pass", "findings": [], "security_notes": ["No destination fetching; no visitor identity stored; parameterized SQL throughout."], "compatibility_notes": ["Greenfield service; no prior contract to break."]})
write(
    "A_greenfield/documenter.json",
    {
        "readme_markdown": "# URL Shortener\n\nRun: `SHORTENER_DB_PATH=/tmp/shortener.db BASE_URL=http://localhost:8000 uvicorn app.main:app`\n\nTest: `python -m pytest -q tests`\n\nEndpoints: POST /api/links, GET /r/{code}, GET /api/links/{code}/stats, GET /healthz, GET /readyz.\n",
        "api_markdown": "# API\n\n- `POST /api/links` `{url}` -> 201 `{code, short_url, url, created_at}`\n- `GET /r/{code}` -> 302 or 404\n- `GET /api/links/{code}/stats` -> 200 or 404\n- `GET /healthz` -> 200\n- `GET /readyz` -> 200 or 503\n",
        "limitations": ["No authentication or abuse protection.", "SQLite is local, single-file, not for public scale.", "No destination fetching or link previews."],
        "summary": "Greenfield URL shortener implementing creation, redirect, stats, and health endpoints over SQLite.",
    },
)

# ---------------------------------------------------------------- Scenario B
write(
    "B_brownfield/analyst.json",
    {
        "normalized_requirement": "Add optional user-selected custom aliases to link creation without breaking existing links or clients; duplicate alias returns a clear conflict.",
        "assumptions": ["Aliases share the same code column/namespace as generated codes.", "Alias pattern ^[A-Za-z0-9_-]{4,32}$, case-sensitive.", "A fixed reserved-name list is rejected."],
        "acceptance_criteria": [
            {"id": "AC-1", "statement": "POST /api/links accepts optional custom_alias; valid alias is stored as code and returned."},
            {"id": "AC-2", "statement": "Invalid alias pattern or reserved alias returns 422."},
            {"id": "AC-3", "statement": "Duplicate alias returns 409."},
            {"id": "AC-4", "statement": "Requests without custom_alias behave exactly as before."},
            {"id": "AC-5", "statement": "Existing rows, generated codes, and click counts are preserved unchanged."},
        ],
        "blocking_questions": [],
        "non_blocking_questions": [],
        "risk_tags": ["breaking-change-risk", "public-api"],
        "notes": "Brownfield; contract for aliases is fully specified, no clarification needed.",
        "expired_link_status": None,
    },
)
write(
    "B_brownfield/architect.json",
    {
        "design_summary": "Extend create_link to accept an optional custom_alias, validate it, and attempt the same insert-if-absent transaction used for generated codes, sharing the links.code namespace. No schema change.",
        "impact_map": [
            {"path": "app/main.py", "symbol": "create_link", "change": "extend", "reason": "Add custom_alias handling to the existing creation endpoint."},
            {"path": "app/main.py", "symbol": "create_app", "change": "keep", "reason": "Schema and other endpoints are unaffected; no migration needed."},
        ],
        "migration": {"required": False, "description": "custom_alias reuses the existing code column; no schema change.", "additive_only": True},
        "tasks": [
            {
                "task_id": "t1",
                "title": "Add custom alias support to link creation",
                "dependencies": [],
                "allowed_paths": ["app/*"],
                "acceptance_criteria_ids": ["AC-1", "AC-2", "AC-3", "AC-4", "AC-5"],
                "risk_level": "medium",
                "instructions": "Extend app/main.py per the alias addendum: validate custom_alias (pattern + reserved list), insert it as code inside the existing transaction, return 409 on conflict, 422 on invalid/reserved. Keep all stage-A behavior identical when custom_alias is absent.",
            },
            {
                "task_id": "t2",
                "title": "Add alias tests",
                "dependencies": ["t1"],
                "allowed_paths": ["tests/*"],
                "acceptance_criteria_ids": ["AC-1", "AC-2", "AC-3"],
                "risk_level": "low",
                "instructions": "Add agent-authored pytest coverage for a valid alias, an invalid alias, and a duplicate alias.",
            },
        ],
        "decisions": [{"decision": "Reuse the code column and existing uniqueness constraint for aliases rather than a separate table.", "rationale": "Matches the requirement that aliases share the generated-code namespace and avoids a schema change."}],
        "required_approvals": ["plan", "release"],
        "risks": ["A malicious alias could collide with a reserved route name; mitigated by the fixed reserved-name list."],
    },
)
write("B_brownfield/implementer.t1.json", {"rationale": "Add custom_alias handling to app/main.py, preserving stage-A behavior.", "edits": read_files(REF / "B" / "app", "app"), "tests_added": [], "notes": "Reference implementation of the stage B contract."})
write("B_brownfield/implementer.t2.json", {"rationale": "Add alias tests.", "edits": read_files(REF / "B" / "tests", "tests"), "tests_added": ["tests/test_app.py"], "notes": ""})
write("B_brownfield/reviewer.json", {"verdict": "pass", "findings": [], "security_notes": ["Reserved-name list prevents alias collision with API routes."], "compatibility_notes": ["Requests without custom_alias are unaffected; existing rows untouched by an additive code path."]})
write(
    "B_brownfield/documenter.json",
    {
        "readme_markdown": "# URL Shortener\n\nNow supports optional `custom_alias` on creation (pattern `^[A-Za-z0-9_-]{4,32}$`, case-sensitive, shares the code namespace).\n\nRun: `SHORTENER_DB_PATH=/tmp/shortener.db BASE_URL=http://localhost:8000 uvicorn app.main:app`\nTest: `python -m pytest -q tests`\n",
        "api_markdown": "# API\n\n- `POST /api/links` `{url, custom_alias?}` -> 201 or 422 (invalid/reserved alias) or 409 (duplicate)\n- other endpoints unchanged from stage A\n",
        "limitations": ["Reserved-alias list is fixed and may need extension for future routes.", "No authentication or abuse protection."],
        "summary": "Added custom alias support without a schema migration; existing links and clients are unaffected.",
    },
)

# ---------------------------------------------------------------- Scenario C
write(
    "C_ambiguous/analyst.json",
    {
        "normalized_requirement": "",
        "assumptions": [],
        "acceptance_criteria": [],
        "blocking_questions": [
            {"id": "Q1", "question": "What makes a link 'old' — automatic age-based expiry, or an explicit expires_at set per link?", "why_blocking": "Determines whether expiry is a scheduled job or a stored per-link timestamp; changes the schema and logic."},
            {"id": "Q2", "question": "What should happen to links created before this change — do they expire too?", "why_blocking": "Determines default value and migration behavior for existing rows."},
            {"id": "Q3", "question": "What HTTP status should an expired link return?", "why_blocking": "Directly changes the redirect endpoint's contract and trusted tests."},
            {"id": "Q4", "question": "What analytics are needed beyond the existing aggregate click count?", "why_blocking": "Determines whether new fields or endpoints are required."},
        ],
        "non_blocking_questions": [],
        "risk_tags": ["ambiguous-requirement", "breaking-change-risk"],
        "notes": "Requirement text does not define expiry semantics or the expired response code; pausing for clarification before any design.",
        "expired_link_status": None,
    },
)
write(
    "C_ambiguous/analyst.rev2.json",
    {
        "normalized_requirement": "Add optional per-link expiry (expires_at, UTC, set at creation, future timestamps only). Existing links never expire. An expired link's redirect returns 404 and does not increment the click count; stats remain readable and expose expiry status. No new visitor-identity analytics.",
        "assumptions": ["Additive nullable expires_at column; NULL means never expires.", "Only aggregate click counts are required; no visitor tracking."],
        "acceptance_criteria": [
            {"id": "AC-1", "statement": "POST /api/links accepts optional future expires_at; past/invalid timestamps return 422."},
            {"id": "AC-2", "statement": "Existing links (expires_at NULL) never expire."},
            {"id": "AC-3", "statement": "An expired link's GET/HEAD /r/{code} returns 404 and does not increment click_count."},
            {"id": "AC-4", "statement": "Stats remain readable for expired links and expose status active/expired."},
            {"id": "AC-5", "statement": "The additive migration preserves existing rows and click counts."},
        ],
        "blocking_questions": [],
        "non_blocking_questions": [],
        "risk_tags": ["data-migration"],
        "notes": "Human clarification received; initial contract targets 404 for expired links.",
        "expired_link_status": 404,
    },
)
write(
    "C_ambiguous/analyst.rev3.json",
    {
        "normalized_requirement": "Add optional per-link expiry (expires_at, UTC, set at creation, future timestamps only). Existing links never expire. An expired link's redirect returns 410 Gone and does not increment the click count; stats remain readable and expose expiry status. No new visitor-identity analytics.",
        "assumptions": ["Additive nullable expires_at column; NULL means never expires.", "Only aggregate click counts are required; no visitor tracking."],
        "acceptance_criteria": [
            {"id": "AC-1", "statement": "POST /api/links accepts optional future expires_at; past/invalid timestamps return 422."},
            {"id": "AC-2", "statement": "Existing links (expires_at NULL) never expire."},
            {"id": "AC-3", "statement": "An expired link's GET/HEAD /r/{code} returns 410 and does not increment click_count."},
            {"id": "AC-4", "statement": "Stats remain readable for expired links and expose status active/expired."},
            {"id": "AC-5", "statement": "The additive migration preserves existing rows and click counts."},
        ],
        "blocking_questions": [],
        "non_blocking_questions": [],
        "risk_tags": ["data-migration"],
        "notes": "Requirement revised: expired-link response changed from 404 to 410 Gone.",
        "expired_link_status": 410,
    },
)
for rev, status in ((2, 404), (3, 410)):
    write(
        f"C_ambiguous/architect.rev{rev}.json",
        {
            "design_summary": f"Add a nullable expires_at column via additive migration; check expiry using an injected clock; return {status} for expired redirects without incrementing the counter.",
            "impact_map": [
                {"path": "app/main.py", "symbol": "create_app", "change": "extend", "reason": "Add additive ALTER TABLE for expires_at on schema init."},
                {"path": "app/main.py", "symbol": "create_link", "change": "extend", "reason": "Accept and validate optional expires_at."},
                {"path": "app/main.py", "symbol": "redirect", "change": "extend", "reason": f"Return {status} and skip the increment when expired."},
                {"path": "app/main.py", "symbol": "stats", "change": "extend", "reason": "Expose expires_at and active/expired status."},
            ],
            "migration": {"required": True, "description": "Additive nullable ALTER TABLE links ADD COLUMN expires_at TEXT; existing rows default to NULL (never expires).", "additive_only": True},
            "tasks": [
                {
                    "task_id": "t1",
                    "title": "Add optional expiry with additive migration",
                    "dependencies": [],
                    "allowed_paths": ["app/*"],
                    "acceptance_criteria_ids": ["AC-1", "AC-2", "AC-3", "AC-4", "AC-5"],
                    "risk_level": "high",
                    "instructions": f"Extend app/main.py per the expiry addendum: additive expires_at column, validate future-only timestamps, injected-clock expiry check, return {status} without incrementing when expired, expose status in stats. Preserve alias and stage-A behavior.",
                },
                {
                    "task_id": "t2",
                    "title": "Add expiry boundary tests",
                    "dependencies": ["t1"],
                    "allowed_paths": ["tests/*"],
                    "acceptance_criteria_ids": ["AC-3", "AC-5"],
                    "risk_level": "low",
                    "instructions": "Add agent-authored pytest coverage for before/at/after the expiry boundary using an injected clock.",
                },
            ],
            "decisions": [{"decision": "Use an injected now() callable rather than wall-clock time.", "rationale": "Makes the expiry boundary deterministically testable."}],
            "required_approvals": ["plan", "migration", "release"],
            "risks": ["Schema change requires migration approval and a demo-data backup/restore rehearsal."],
        },
    )
# implementer/reviewer/documenter are keyed by CANDIDATE revision (1 = first pass/404, 2 = second pass/410),
# not requirement revision, because the coordinator calls them with revision=candidate_revision.
write("C_ambiguous/implementer.t1.rev1.json", {"rationale": "Implement expiry with 404 response.", "edits": [e for e in read_files(REF / "C" / "app", "app") if e["path"] != "app/main.py"] + [{"path": "app/main.py", "op": "write", "content": (REF / "C" / "app" / "main.py").read_text().replace('int(os.environ.get("FORGEFLOW_EXPIRED_STATUS", "410"))', "404")}], "tests_added": [], "notes": "Initial clarified contract: 404 for expired links."})
write("C_ambiguous/implementer.t1.rev2.json", {"rationale": "Revise expiry response to 410 per the requirement revision.", "edits": read_files(REF / "C" / "app", "app"), "tests_added": [], "notes": "Revised contract: 410 Gone for expired links."})
write("C_ambiguous/implementer.t2.rev1.json", {"rationale": "Add expiry boundary tests.", "edits": read_files(REF / "C" / "tests", "tests"), "tests_added": ["tests/test_app.py"], "notes": ""})
write("C_ambiguous/implementer.t2.rev2.json", {"rationale": "Add expiry boundary tests.", "edits": read_files(REF / "C" / "tests", "tests"), "tests_added": ["tests/test_app.py"], "notes": ""})
for candidate_rev, status in ((1, 404), (2, 410)):
    write(f"C_ambiguous/reviewer.rev{candidate_rev}.json", {"verdict": "pass", "findings": [], "security_notes": ["No visitor identity collected; migration is additive and backed up before apply."], "compatibility_notes": ["Existing links (NULL expires_at) never expire; alias and stage-A behavior preserved."]})
    write(
        f"C_ambiguous/documenter.rev{candidate_rev}.json",
        {
            "readme_markdown": f"# URL Shortener\n\nLinks may now carry an optional future `expires_at`. Expired links respond with {status} and do not increment the click count.\n\nRun: `SHORTENER_DB_PATH=/tmp/shortener.db BASE_URL=http://localhost:8000 uvicorn app.main:app`\nTest: `python -m pytest -q tests`\n",
            "api_markdown": f"# API\n\n- `POST /api/links` `{{url, custom_alias?, expires_at?}}`\n- `GET /r/{{code}}` -> 302, {status} if expired, 404 if unknown\n- `GET /api/links/{{code}}/stats` now includes expires_at and status\n",
            "limitations": ["No visitor-identity analytics collected.", "Expiry is per-link only, no bulk/campaign expiry."],
            "summary": f"Added optional per-link expiry via an additive migration; expired links return {status}.",
        },
    )

print("done")
