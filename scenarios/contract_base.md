## Target contract: URL shortener core (stage A)

Layout and factory (trusted tests import exactly this):
- Package `app/` with `app/__init__.py` and `app/main.py`.
- `app/main.py` exposes `create_app(db_path: str, base_url: str = "http://localhost:8000", now=None, code_generator=None) -> fastapi.FastAPI`
  and a module-level `app = create_app(os.environ.get("SHORTENER_DB_PATH", "/tmp/shortener.db"), os.environ.get("BASE_URL", "http://localhost:8000"))`.
  The default path must be writable even when the working directory is read-only (as it is inside the isolated test runner); `/tmp` is always writable.
  - `now`: optional zero-argument callable returning an aware UTC `datetime`; default uses `datetime.now(timezone.utc)`. Every time read goes through it.
  - `code_generator`: optional zero-argument callable returning a candidate code string; default generates 8-character codes with `secrets.choice` over the alphabet `ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789` (no 0, O, I, l, 1).
- Open a new `sqlite3` connection per request (a helper that connects, sets `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000`, and closes). Do not keep a global connection. Create the schema on app creation and again lazily if missing.
- Schema: `links(code TEXT PRIMARY KEY, target_url TEXT NOT NULL, created_at TEXT NOT NULL, click_count INTEGER NOT NULL DEFAULT 0)`. Timestamps are ISO-8601 UTC strings.

Endpoints:
- `POST /api/links` body `{"url": "..."}` → 201 `{"code","short_url","url","created_at"}`. `short_url` = `{base_url}/r/{code}` using the configured base_url, never the Host header.
  - Validation → 422 with a JSON body containing `detail`: must be absolute http/https URL; max 2048 characters; no userinfo (credentials) in the URL; host must be non-empty and contain no whitespace/control characters; no control characters anywhere; missing or non-string `url` also 422.
  - Code collisions: try `code_generator()` up to 5 times (inserting with a uniqueness check inside a transaction); after 5 collisions return 503 `{"detail": "could not allocate code"}`.
- `GET /r/{code}` → 302 with `Location: <target_url>`; unknown code → 404. Increment `click_count` atomically (`UPDATE ... SET click_count = click_count + 1 WHERE code = ?`) inside the same transaction before responding; if the database write fails return 503, never a redirect.
- `HEAD /r/{code}` → 302 with Location for known codes (404 unknown) and does NOT increment the counter. Other methods on `/r/{code}` → 405.
- `GET /api/links/{code}/stats` → 200 `{"code","url","created_at","click_count"}`; unknown → 404.
- `GET /healthz` → 200 `{"status":"ok"}`.
- `GET /readyz` → 200 `{"status":"ready"}` when `SELECT 1` succeeds on a fresh connection to the database; otherwise 503 `{"status":"unavailable"}`.

Rules: never fetch the destination URL; never store visitor IP or user agent; parameterized SQL only; standard library `sqlite3`; the app must not hold a database connection open between requests; data must survive process restarts (same db_path).
