"""URL shortener service (reference implementation, stage B: custom aliases)."""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
MAX_URL_LEN = 2048
CODE_ATTEMPTS = 5
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{4,32}$")
RESERVED_ALIASES = frozenset({"api", "r", "healthz", "readyz", "admin", "static", "docs", "openapi", "login", "logout", "metrics"})


def default_code_generator() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(8))


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return "url must be a non-empty string"
    if len(value) > MAX_URL_LEN:
        return f"url exceeds {MAX_URL_LEN} characters"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or " " in value:
        return "url contains control characters or whitespace"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "url could not be parsed"
    if parts.scheme not in ("http", "https"):
        return "url must use http or https"
    if not parts.netloc or not parts.hostname:
        return "url must have a host"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "credentials are not allowed in url"
    return None


def validate_alias(value: Any) -> str | None:
    """Return an error message or None. None value means 'not provided'."""
    if value is None:
        return None
    if not isinstance(value, str) or not ALIAS_RE.match(value):
        return "custom_alias must match ^[A-Za-z0-9_-]{4,32}$"
    if value.lower() in RESERVED_ALIASES:
        return "custom_alias is reserved"
    return None


def create_app(db_path: str, base_url: str = "http://localhost:8000", now=None, code_generator=None) -> FastAPI:
    now = now or utcnow
    code_generator = code_generator or default_code_generator
    base = base_url.rstrip("/")

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, timeout=5, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def ensure_schema() -> None:
        conn = connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS links("
                "code TEXT PRIMARY KEY, target_url TEXT NOT NULL, created_at TEXT NOT NULL, "
                "click_count INTEGER NOT NULL DEFAULT 0)"
            )
        finally:
            conn.close()

    ensure_schema()
    app = FastAPI(title="URL shortener", version="1.1.0")

    def error(status: int, detail: str) -> JSONResponse:
        return JSONResponse({"detail": detail}, status_code=status)

    def insert_link(conn: sqlite3.Connection, code: str, url: str, created_at: str) -> bool:
        """Insert inside a transaction; False when the code already exists."""
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM links WHERE code=?", (code,)).fetchone():
                conn.execute("ROLLBACK")
                return False
            conn.execute("INSERT INTO links(code, target_url, created_at, click_count) VALUES(?,?,?,0)", (code, url, created_at))
            conn.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return False

    @app.post("/api/links", status_code=201)
    async def create_link(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return error(422, "invalid JSON body")
        if not isinstance(body, dict):
            return error(422, "body must be an object")
        url = body.get("url")
        msg = validate_url(url)
        if msg:
            return error(422, msg)
        alias = body.get("custom_alias")
        msg = validate_alias(alias)
        if msg:
            return error(422, msg)
        created_at = _iso(now())
        try:
            conn = connect()
        except sqlite3.Error:
            return error(503, "database unavailable")
        try:
            if alias is not None:
                try:
                    ok = insert_link(conn, alias, url, created_at)
                except sqlite3.Error:
                    return error(503, "database unavailable")
                if not ok:
                    return error(409, "alias already in use")
                code = alias
            else:
                code = None
                for _ in range(CODE_ATTEMPTS):
                    candidate = code_generator()
                    try:
                        if insert_link(conn, candidate, url, created_at):
                            code = candidate
                            break
                    except sqlite3.Error:
                        return error(503, "database unavailable")
                if code is None:
                    return error(503, "could not allocate code")
            return JSONResponse(
                {"code": code, "short_url": f"{base}/r/{code}", "url": url, "created_at": created_at},
                status_code=201,
            )
        finally:
            conn.close()

    @app.api_route("/r/{code}", methods=["GET", "HEAD"])
    async def redirect(code: str, request: Request):
        try:
            conn = connect()
        except sqlite3.Error:
            return error(503, "database unavailable")
        try:
            if request.method == "HEAD":
                row = conn.execute("SELECT target_url FROM links WHERE code=?", (code,)).fetchone()
                if row is None:
                    return Response(status_code=404)
                return Response(status_code=302, headers={"Location": row[0]})
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT target_url FROM links WHERE code=?", (code,)).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return error(404, "unknown code")
                conn.execute("UPDATE links SET click_count = click_count + 1 WHERE code=?", (code,))
                conn.execute("COMMIT")
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                return error(503, "database unavailable")
            return Response(status_code=302, headers={"Location": row[0]})
        finally:
            conn.close()

    @app.get("/api/links/{code}/stats")
    async def stats(code: str):
        try:
            conn = connect()
        except sqlite3.Error:
            return error(503, "database unavailable")
        try:
            row = conn.execute("SELECT code, target_url, created_at, click_count FROM links WHERE code=?", (code,)).fetchone()
        except sqlite3.Error:
            return error(503, "database unavailable")
        finally:
            conn.close()
        if row is None:
            return error(404, "unknown code")
        return {"code": row[0], "url": row[1], "created_at": row[2], "click_count": row[3]}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        try:
            conn = connect()
            try:
                conn.execute("SELECT 1 FROM links LIMIT 1")
            finally:
                conn.close()
        except sqlite3.Error:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready"}

    return app


app = create_app(os.environ.get("SHORTENER_DB_PATH", "/tmp/shortener.db"), os.environ.get("BASE_URL", "http://localhost:8000"))
