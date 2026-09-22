"""Stage C: optional expiry with injected clock, additive migration, legacy links, analytics."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from conftest import Clock
from fastapi.testclient import TestClient

pytestmark = pytest.mark.stage_c

# The expired status code is part of the *current requirement*; the coordinator passes it in.
EXPIRED_STATUS = int(os.environ.get("FORGEFLOW_EXPIRED_STATUS", "410"))
T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def cclient(make_app, clock):
    with TestClient(make_app("expiry.db", now=clock), follow_redirects=False) as c:
        yield c


def test_create_with_future_expiry_and_fields(cclient):
    exp = (T0 + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    r = cclient.post("/api/links", json={"url": "https://example.com/e", "expires_at": exp})
    assert r.status_code == 201, r.text
    assert r.json()["expires_at"] == exp
    s = cclient.get(f"/api/links/{r.json()['code']}/stats").json()
    assert s["expires_at"] == exp and s["status"] == "active"
    r2 = cclient.post("/api/links", json={"url": "https://example.com/n"})
    assert r2.status_code == 201 and r2.json()["expires_at"] is None


@pytest.mark.parametrize("bad", ["2020-01-01T00:00:00Z", "not-a-date", "2030-01-01T12:00:00Z", "2030-06-01T00:00:00"])
def test_past_equal_or_invalid_expiry_rejected(cclient, bad):
    r = cclient.post("/api/links", json={"url": "https://example.com/e", "expires_at": bad})
    assert r.status_code == 422, r.text


def test_expiry_boundary_before_at_after(make_app, clock):
    exp = T0 + timedelta(minutes=10)
    with TestClient(make_app("boundary.db", now=clock), follow_redirects=False) as c:
        code = c.post("/api/links", json={"url": "https://example.com/b", "expires_at": exp.isoformat().replace("+00:00", "Z")}).json()[
            "code"
        ]
        clock.at = exp - timedelta(seconds=1)
        assert c.get(f"/r/{code}").status_code == 302
        clock.at = exp
        assert c.get(f"/r/{code}").status_code == EXPIRED_STATUS
        assert c.head(f"/r/{code}").status_code == EXPIRED_STATUS
        clock.at = exp + timedelta(days=30)
        assert c.get(f"/r/{code}").status_code == EXPIRED_STATUS
        s = c.get(f"/api/links/{code}/stats")
        assert s.status_code == 200
        assert s.json()["click_count"] == 1, "expired redirects must not increment"
        assert s.json()["status"] == "expired"


def test_links_without_expiry_never_expire(make_app, clock):
    with TestClient(make_app("forever.db", now=clock), follow_redirects=False) as c:
        code = c.post("/api/links", json={"url": "https://example.com/f"}).json()["code"]
        clock.at = T0 + timedelta(days=36500)
        assert c.get(f"/r/{code}").status_code == 302


LEGACY_ROWS = [
    ("Ab3dEf7h", "https://example.com/campaign/spring", "2026-09-01T10:00:00Z", 12),
    ("spring-sale", "https://example.com/campaign/spring-sale", "2026-09-05T10:00:00Z", 40),
]


def test_additive_migration_preserves_rows_and_counts(app_module, tmp_path, clock):
    db = tmp_path / "legacy.db"
    c = sqlite3.connect(db)
    c.execute(
        "CREATE TABLE links(code TEXT PRIMARY KEY, target_url TEXT NOT NULL, created_at TEXT NOT NULL, click_count INTEGER NOT NULL DEFAULT 0)"
    )
    c.executemany("INSERT INTO links VALUES(?,?,?,?)", LEGACY_ROWS)
    c.commit()
    c.close()
    app = app_module.create_app(db_path=str(db), base_url="http://short.test", now=clock)
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(links)")]
    rows = conn.execute("SELECT code, target_url, created_at, click_count, expires_at FROM links ORDER BY code").fetchall()
    conn.close()
    assert "expires_at" in cols
    assert set(cols) >= {"code", "target_url", "created_at", "click_count"}
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == sorted(LEGACY_ROWS)
    assert all(r[4] is None for r in rows)
    with TestClient(app, follow_redirects=False) as tc:
        clock.at = T0 + timedelta(days=3650)
        assert tc.get("/r/spring-sale").status_code == 302
        s = tc.get("/api/links/spring-sale/stats").json()
        assert s["click_count"] == 41 and s["expires_at"] is None and s["status"] == "active"


def test_no_visitor_identity_collected(make_app, tmp_path, clock):
    with TestClient(make_app("priv.db", now=clock), follow_redirects=False) as c:
        code = c.post("/api/links", json={"url": "https://example.com/p"}).json()["code"]
        c.get(f"/r/{code}", headers={"User-Agent": "TRACKME-UA-42", "X-Forwarded-For": "198.51.100.9"})
    conn = sqlite3.connect(tmp_path / "priv.db")
    dump = "\n".join(conn.iterdump())
    conn.close()
    assert "TRACKME-UA-42" not in dump and "198.51.100.9" not in dump
