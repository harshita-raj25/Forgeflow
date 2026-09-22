"""Stage A: core shortener contract."""

from __future__ import annotations

import os
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient


def test_create_and_redirect(client):
    r = client.post("/api/links", json={"url": "https://example.com/path?q=1"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) >= {"code", "short_url", "url", "created_at"}
    assert len(body["code"]) == 8
    assert body["short_url"] == f"http://short.test/r/{body['code']}"
    assert body["url"] == "https://example.com/path?q=1"
    red = client.get(f"/r/{body['code']}")
    assert red.status_code == 302
    assert red.headers["location"] == "https://example.com/path?q=1"


def test_short_url_uses_configured_base_not_host_header(client):
    r = client.post("/api/links", json={"url": "https://example.com/"}, headers={"Host": "evil.example"})
    assert r.status_code == 201
    assert r.json()["short_url"].startswith("http://short.test/r/")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "javascript:alert(1)",
        "example.com/no-scheme",
        "https://user:pass@example.com/",
        "https://example.com/with\x00control",
        "https://exa mple.com/",
        "https:///nohost",
        "https://" + "a" * 2100 + ".com/",
        "",
    ],
)
def test_invalid_urls_rejected(client, url):
    r = client.post("/api/links", json={"url": url})
    assert r.status_code == 422, r.text
    assert "detail" in r.json()


def test_missing_or_non_string_url_rejected(client):
    assert client.post("/api/links", json={}).status_code == 422
    assert client.post("/api/links", json={"url": 123}).status_code == 422


def test_unknown_code_404(client):
    assert client.get("/r/nopenope").status_code == 404
    assert client.get("/api/links/nopenope/stats").status_code == 404


def test_redirect_increments_once_and_head_does_not(client):
    code = client.post("/api/links", json={"url": "https://example.com/a"}).json()["code"]
    assert client.get(f"/api/links/{code}/stats").json()["click_count"] == 0
    client.get(f"/r/{code}")
    client.get(f"/r/{code}")
    h = client.head(f"/r/{code}")
    assert h.status_code == 302
    assert h.headers["location"] == "https://example.com/a"
    stats = client.get(f"/api/links/{code}/stats").json()
    assert stats["click_count"] == 2
    assert stats["url"] == "https://example.com/a"
    assert stats["code"] == code
    assert "created_at" in stats
    assert client.head("/r/nopenope").status_code == 404
    assert client.post(f"/r/{code}").status_code == 405


def test_health_and_ready(client):
    assert client.get("/healthz").status_code == 200
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_data_survives_restart(make_app, tmp_path):
    with TestClient(make_app("persist.db"), follow_redirects=False) as c1:
        code = c1.post("/api/links", json={"url": "https://example.com/p"}).json()["code"]
        c1.get(f"/r/{code}")
    with TestClient(make_app("persist.db"), follow_redirects=False) as c2:
        assert c2.get(f"/r/{code}").status_code == 302
        assert c2.get(f"/api/links/{code}/stats").json()["click_count"] == 2


def test_concurrent_increments_do_not_lose_updates(make_app):
    app = make_app("concurrent.db")
    with TestClient(app, follow_redirects=False) as c:
        code = c.post("/api/links", json={"url": "https://example.com/c"}).json()["code"]
    n_threads, per_thread = 8, 10
    errors = []

    def worker():
        try:
            with TestClient(app, follow_redirects=False) as tc:
                for _ in range(per_thread):
                    r = tc.get(f"/r/{code}")
                    if r.status_code != 302:
                        errors.append(r.status_code)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    with TestClient(app, follow_redirects=False) as c:
        assert c.get(f"/api/links/{code}/stats").json()["click_count"] == n_threads * per_thread


def test_code_collision_bounded_retry_then_503(make_app):
    calls = {"n": 0}

    def same_code():
        calls["n"] += 1
        return "SAMECODE"

    app = make_app("collide.db", code_generator=same_code)
    with TestClient(app, follow_redirects=False) as c:
        first = c.post("/api/links", json={"url": "https://example.com/1"})
        assert first.status_code == 201 and first.json()["code"] == "SAMECODE"
        calls["n"] = 0
        second = c.post("/api/links", json={"url": "https://example.com/2"})
        assert second.status_code == 503, second.text
        assert calls["n"] == 5, f"expected exactly 5 generator attempts, got {calls['n']}"
        assert c.get("/api/links/SAMECODE/stats").json()["url"] == "https://example.com/1"


def test_database_failure_gives_controlled_error(make_app, tmp_path):
    app = make_app("broken.db")
    with TestClient(app, follow_redirects=False) as c:
        code = c.post("/api/links", json={"url": "https://example.com/d"}).json()["code"]
        db = tmp_path / "broken.db"
        # Replace the database file with a directory so new connections fail.
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = tmp_path / f"broken.db{suffix}"
            if p.exists():
                p.unlink()
        os.mkdir(db)
        assert c.get("/readyz").status_code == 503
        assert c.get(f"/r/{code}").status_code == 503
        assert c.post("/api/links", json={"url": "https://example.com/e"}).status_code == 503


def test_no_visitor_data_stored(make_app, tmp_path):
    app = make_app("privacy.db")
    with TestClient(app, follow_redirects=False) as c:
        code = c.post("/api/links", json={"url": "https://example.com/v"}).json()["code"]
        c.get(f"/r/{code}", headers={"User-Agent": "UNIQUE-AGENT-STRING-9f3", "X-Forwarded-For": "203.0.113.77"})
    conn = sqlite3.connect(tmp_path / "privacy.db")
    dump = "\n".join(conn.iterdump())
    conn.close()
    assert "UNIQUE-AGENT-STRING-9f3" not in dump
    assert "203.0.113.77" not in dump
