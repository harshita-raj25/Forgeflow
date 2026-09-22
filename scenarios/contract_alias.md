## Target contract addition: custom aliases (stage B)

- `POST /api/links` accepts optional `"custom_alias"`. When present and non-null it must match `^[A-Za-z0-9_-]{4,32}$` (case-sensitive) else 422.
- Reserved aliases (exact match, case-insensitive) return 422: `api`, `r`, `healthz`, `readyz`, `admin`, `static`, `docs`, `openapi`, `login`, `logout`, `metrics`.
- The alias is stored as the link `code` (same column and namespace as generated codes). Duplicate alias (already used by any link, generated or custom) → 409 `{"detail": "alias already in use"}`.
- Requests without `custom_alias` behave exactly as before. Existing rows and existing generated codes keep working; no schema migration.
- Response and stats shapes are unchanged (`code` carries the alias).
