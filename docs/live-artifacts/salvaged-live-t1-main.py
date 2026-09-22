from __future__ import annotations

import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response

DEFAULT_DB_PATH = "/tmp/shortener.db"
DEFAULT_BASE_URL = "http://localhost:8000"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
CODE_LENGTH = 8
MAX_URL_LENGTH = 2048


def _is_control_character(ch: str) -> bool:
    codepoint = ord(ch)
    return codepoint < 32 or codepoint == 127


def _contains_control_characters(value: str) -> bool:
    return any(_is_control_character(ch) for ch in value)


def _timestamp(now: Optional[Callable[[], datetime]]) -> str:
    current = now() if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.isoformat().replace("+00:00", "Z")


def _default_code_generator() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _validate_target_url(url_value: object) -> str:
    if not isinstance(url_value, str):
        raise HTTPException(status_code=422, detail="url must be a string")

    if len(url_value) > MAX_URL_LENGTH:
        raise HTTPException(status_code=422, detail="url must be at most 2048 characters")

    if _contains_control_characters(url_value):
        raise HTTPException(status_code=422, detail="url contains control characters")

    parsed = urlsplit(url_value)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise HTTPException(status_code=422, detail="url must be an absolute http or https URL")

    if not parsed.netloc:
        raise HTTPException(status_code=422, detail="url must include a host")

    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(status_code=422, detail="url must not include userinfo")

    host = parsed.hostname or ""
    if not host:
        raise HTTPException(status_code=422, detail="url must include a host")

    if any(ch.isspace() or _is_control_character(ch) for ch in host):
        raise HTTPException(status_code=422, detail="url host contains invalid characters")

    return url_value


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _open_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            code TEXT PRIMARY KEY,
            target_url TEXT NOT NULL,
            created_at TEXT NOT NULL,
            click_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def _get_connection_factory(db_path: str) -> Callable[[], Iterator[sqlite3.Connection]]:
    @contextmanager
    def _connection() -> Iterator[sqlite3.Connection]:
        conn = _open_connection(db_path)
        try:
            _ensure_schema(conn)
            yield conn
        except Exception:
            raise
        finally:
            conn.close()

    return _connection


def create_app(
    db_path: str,
    base_url: str = DEFAULT_BASE_URL,
    now: Optional[Callable[[], datetime]] = None,
    code_generator: Optional[Callable[[], str]] = None,
) -> FastAPI:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    app = FastAPI()
    app.state.db_path = db_path
    app.state.base_url = _normalize_base_url(base_url)
    app.state.now = now
    app.state.code_generator = code_generator or _default_code_generator

    with _open_connection(db_path) as conn:
        _ensure_schema(conn)
        conn.commit()

    get_connection = _get_connection_factory(db_path)

    def _database_unavailable() -> HTTPException:
        return HTTPException(status_code=503, detail="database unavailable")

    @app.post("/api/links", status_code=201)
    def create_link(payload: object = Body(...)) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="url must be a string")

        url_value = _validate_target_url(payload.get("url"))
        created_at = _timestamp(now)
        generator = app.state.code_generator

        with get_connection() as conn:
            for _ in range(5):
                code = generator()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO links (code, target_url, created_at, click_count) VALUES (?, ?, ?, 0)",
                        (code, url_value, created_at),
                    )
                    conn.commit()
                    break
                except sqlite3.IntegrityError:
                    conn.rollback()
                except sqlite3.Error as exc:
                    conn.rollback()
                    raise _database_unavailable() from exc
            else:
                raise HTTPException(status_code=503, detail="could not allocate code")

        return {
            "code": code,
            "short_url": f"{app.state.base_url}/r/{code}",
            "url": url_value,
            "created_at": created_at,
        }

    @app.api_route("/r/{code}", methods=["GET", "HEAD"])
    def resolve_link(code: str, request: Request) -> Response:
        with get_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT target_url FROM links WHERE code = ?",
                    (code,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    raise HTTPException(status_code=404, detail="not found")

                target_url = row["target_url"]
                if request.method == "HEAD":
                    conn.rollback()
                    return Response(status_code=302, headers={"Location": target_url})

                conn.execute(
                    "UPDATE links SET click_count = click_count + 1 WHERE code = ?",
                    (code,),
                )
                conn.commit()
                return Response(status_code=302, headers={"Location": target_url})
            except HTTPException:
                raise
            except sqlite3.Error as exc:
                conn.rollback()
                raise _database_unavailable() from exc

    @app.get("/api/links/{code}/stats")
    def link_stats(code: str) -> dict:
        with get_connection() as conn:
            try:
                row = conn.execute(
                    "SELECT code, target_url, created_at, click_count FROM links WHERE code = ?",
                    (code,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise _database_unavailable() from exc

            if row is None:
                raise HTTPException(status_code=404, detail="not found")

            return {
                "code": row["code"],
                "url": row["target_url"],
                "created_at": row["created_at"],
                "click_count": row["click_count"],
            }

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> Response:
        try:
            conn = _open_connection(db_path)
        except sqlite3.Error:
            return Response(content='{"status":"unavailable"}', media_type="application/json", status_code=503)

        try:
            conn.execute("SELECT 1")
        except sqlite3.Error:
            return Response(content='{"status":"unavailable"}', media_type="application/json", status_code=503)
        finally:
            conn.close()

        return Response(content='{"status":"ready"}', media_type="application/json", status_code=200)

    return app


app = create_app(
    os.environ.get("SHORTENER_DB_PATH", DEFAULT_DB_PATH),
    os.environ.get("BASE_URL", DEFAULT_BASE_URL),
)
