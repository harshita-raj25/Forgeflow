## Target contract addition: optional expiry (stage C)

- Schema: additive nullable column `expires_at TEXT NULL` on `links`. On app creation, if the column is missing, run `ALTER TABLE links ADD COLUMN expires_at TEXT` (additive only; existing rows keep NULL = never expires). No other schema change.
- `POST /api/links` accepts optional `"expires_at"`: ISO-8601 timestamp with timezone (e.g. `2030-01-01T00:00:00Z`). It must be strictly later than `now()` at creation, else 422. Stored normalized as UTC ISO-8601 with `Z`. Absent or null means never expires.
- Response of `POST /api/links` and `GET /api/links/{code}/stats` includes `"expires_at"` (string or null) and stats also includes `"status"`: `"active"` or `"expired"`.
- `GET /r/{code}` and `HEAD /r/{code}`: when `expires_at` is not null and `now() >= expires_at`, respond with the expired status code defined by the current requirement and DO NOT increment `click_count`. Before that instant the link redirects normally. The comparison uses the injected `now` callable so the boundary is testable.
- Stats remain readable for expired links (200) with the aggregate click count preserved.
- No visitor identity data is collected. Analytics stay aggregate: total successful redirects per link.
