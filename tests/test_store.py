from __future__ import annotations

from coordinator.store import Store


def test_event_chain_verifies_and_detects_tamper(tmp_path):
    store = Store(tmp_path / "t.db")
    rid = store.create_run("A_greenfield", "fixture", {}, "req text", None)
    store.append_event(rid, "x", {"a": 1})
    store.append_event(rid, "y", {"b": 2})
    events = store.list_events(rid)
    ok, _ = Store.verify_chain(events)
    assert ok
    tampered = [dict(e) for e in events]
    tampered[0]["payload"] = {"a": 999}
    ok2, msg = Store.verify_chain(tampered)
    assert not ok2 and "seq" in msg


def test_secrets_are_redacted_in_events(tmp_path):
    store = Store(tmp_path / "t.db")
    rid = store.create_run("A_greenfield", "live", {}, "req", None)
    store.append_event(rid, "model_request", {"note": "key=sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX"})
    ev = store.list_events(rid)[-1]
    assert "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX" not in str(ev["payload"])


def test_approval_lifecycle_and_invalidation(tmp_path):
    store = Store(tmp_path / "t.db")
    rid = store.create_run("A_greenfield", "fixture", {}, "req", None)
    aid = store.request_approval(rid, "plan_approval", "plan", "hash1", "approve plan")
    a = store.get_approval(aid)
    assert a["status"] == "pending"
    store.decide_approval(aid, True, "owner", "looks good")
    assert store.get_approval(aid)["status"] == "approved"
    invalidated = store.invalidate_approvals(rid, "requirement changed")
    assert aid in invalidated
    assert store.get_approval(aid)["status"] == "invalidated"


def test_idempotent_operation_guard(tmp_path):
    store = Store(tmp_path / "t.db")
    rid = store.create_run("A_greenfield", "fixture", {}, "req", None)
    assert store.operation("op1", rid, "export", "hashA") is None
    store.record_operation("op1", rid, "export", "hashA", {"ok": True})
    assert store.operation("op1", rid, "export", "hashA") == {"ok": True}
    try:
        store.operation("op1", rid, "export", "hashB")
        raise AssertionError("should have rejected a mismatched precondition hash")
    except ValueError:
        pass


def test_node_attempt_lifecycle_and_reruns(tmp_path):
    store = Store(tmp_path / "t.db")
    rid = store.create_run("A_greenfield", "fixture", {}, "req", None)
    aid1 = store.start_attempt(rid, "t1", 1, 0, {})
    store.finish_attempt(aid1, "FAILED", error_category="validation_failed")
    aid2 = store.start_attempt(rid, "t1", 1, 0, {})
    store.finish_attempt(aid2, "SUCCEEDED")
    attempts = store.list_attempts(rid)
    assert [a["attempt"] for a in attempts] == [1, 2]
    latest = store.latest_attempts(rid)
    assert latest["t1"]["status"] == "SUCCEEDED"
