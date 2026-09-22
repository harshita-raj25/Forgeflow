"""Agent-authored tests (reference fixture, stage A)."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_roundtrip(tmp_path):
    app = create_app(str(tmp_path / "t.db"), base_url="http://s.test")
    with TestClient(app, follow_redirects=False) as c:
        r = c.post("/api/links", json={"url": "https://example.com/"})
        assert r.status_code == 201
        code = r.json()["code"]
        assert c.get(f"/r/{code}").status_code == 302
        assert c.get(f"/api/links/{code}/stats").json()["click_count"] == 1
