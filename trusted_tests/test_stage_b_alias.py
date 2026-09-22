"""Stage B: custom aliases without breaking existing links."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.stage_b

BASELINE_ROWS = [
    ("Ab3dEf7h", "https://example.com/campaign/spring", "2026-09-01T10:00:00Z", 12),
    ("Zy9xWv5u", "https://example.org/docs", "2026-09-02T11:30:00Z", 3),
]


def seed_baseline_db(path):
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE links(code TEXT PRIMARY KEY, target_url TEXT NOT NULL, created_at TEXT NOT NULL, click_count INTEGER NOT NULL DEFAULT 0)"
    )
    c.executemany("INSERT INTO links(code,target_url,created_at,click_count) VALUES(?,?,?,?)", BASELINE_ROWS)
    c.commit()
    c.close()


def test_custom_alias_accepted(client):
    r = client.post("/api/links", json={"url": "https://example.com/x", "custom_alias": "my-Link_1"})
    assert r.status_code == 201, r.text
    assert r.json()["code"] == "my-Link_1"
    assert r.json()["short_url"] == "http://short.test/r/my-Link_1"
    assert client.get("/r/my-Link_1").status_code == 302
    assert client.get("/api/links/my-Link_1/stats").json()["click_count"] == 1


@pytest.mark.parametrize("alias", ["abc", "a" * 33, "has space", "dot.dot", "slash/x", "ünïcode", "api", "READYZ", "admin"])
def test_invalid_or_reserved_alias_rejected(client, alias):
    r = client.post("/api/links", json={"url": "https://example.com/x", "custom_alias": alias})
    assert r.status_code == 422, r.text


def test_duplicate_alias_409(client):
    assert client.post("/api/links", json={"url": "https://example.com/1", "custom_alias": "taken1"}).status_code == 201
    r = client.post("/api/links", json={"url": "https://example.com/2", "custom_alias": "taken1"})
    assert r.status_code == 409, r.text
    assert "detail" in r.json()
    # aliases are case-sensitive: different case is a different code
    assert client.post("/api/links", json={"url": "https://example.com/3", "custom_alias": "TAKEN1"}).status_code == 201


def test_alias_shares_namespace_with_generated_codes(make_app):
    app = make_app("ns.db", code_generator=lambda: "GENCODE1")
    with TestClient(app, follow_redirects=False) as c:
        assert c.post("/api/links", json={"url": "https://example.com/g"}).status_code == 201
        assert c.post("/api/links", json={"url": "https://example.com/h", "custom_alias": "GENCODE1"}).status_code == 409


def test_requests_without_alias_unchanged(client):
    r = client.post("/api/links", json={"url": "https://example.com/plain"})
    assert r.status_code == 201
    assert len(r.json()["code"]) == 8
    r2 = client.post("/api/links", json={"url": "https://example.com/plain", "custom_alias": None})
    assert r2.status_code == 201 and len(r2.json()["code"]) == 8


def test_existing_generated_codes_and_counts_survive(app_module, tmp_path):
    db = tmp_path / "legacy.db"
    seed_baseline_db(db)
    app = app_module.create_app(db_path=str(db), base_url="http://short.test")
    with TestClient(app, follow_redirects=False) as c:
        for code, url, _, clicks in BASELINE_ROWS:
            s = c.get(f"/api/links/{code}/stats")
            assert s.status_code == 200
            assert s.json()["click_count"] == clicks and s.json()["url"] == url
            assert c.get(f"/r/{code}").headers["location"] == url
        assert c.post("/api/links", json={"url": "https://example.com/n", "custom_alias": "Ab3dEf7h"}).status_code == 409
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT code, click_count FROM links ORDER BY code").fetchall()
    conn.close()
    assert dict(rows)["Ab3dEf7h"] == 13 and dict(rows)["Zy9xWv5u"] == 4
