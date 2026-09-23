"""Durable SQLite state: runs, requirement revisions, graph revisions, node attempts, artifacts,
hash-chained events, approvals, clarifications, and the scheduler lease.

All mutations go through short transactions. Events are appended with a hash chain so an exported
head can detect later edits (not tamper-proof storage; see docs/limitations.md).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .util import canonical_json, iso, new_id, redact_obj, sha256_text

RUN_STATUSES = ("PENDING", "RUNNING", "WAITING_FOR_INPUT", "WAITING_FOR_APPROVAL", "SUCCEEDED", "FAILED", "STOPPED")
TERMINAL_RUN = ("SUCCEEDED", "FAILED", "STOPPED")
NODE_STATUSES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "BLOCKED", "INVALIDATED", "CANCELLED", "INTERRUPTED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  scenario TEXT NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  requirement_revision INTEGER NOT NULL DEFAULT 0,
  graph_revision INTEGER NOT NULL DEFAULT 0,
  candidate_revision INTEGER NOT NULL DEFAULT 0,
  baseline_hash TEXT,
  candidate_hash TEXT,
  provider_calls INTEGER NOT NULL DEFAULT 0,
  active_seconds REAL NOT NULL DEFAULT 0,
  repair_cycles INTEGER NOT NULL DEFAULT 0,
  stop_reason TEXT,
  config_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  lease_owner TEXT,
  lease_expires TEXT
);
CREATE TABLE IF NOT EXISTS requirement_revisions (
  run_id TEXT NOT NULL, revision INTEGER NOT NULL, text TEXT NOT NULL, reason TEXT NOT NULL,
  clarifications_json TEXT NOT NULL, hash TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, revision)
);
CREATE TABLE IF NOT EXISTS graph_revisions (
  run_id TEXT NOT NULL, revision INTEGER NOT NULL, requirement_revision INTEGER NOT NULL,
  graph_json TEXT NOT NULL, hash TEXT NOT NULL, diff_json TEXT, reason TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, revision)
);
CREATE TABLE IF NOT EXISTS node_attempts (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL, graph_revision INTEGER NOT NULL,
  candidate_revision INTEGER NOT NULL, attempt INTEGER NOT NULL, status TEXT NOT NULL,
  input_hashes_json TEXT NOT NULL, output_artifact_ids_json TEXT NOT NULL, error_category TEXT,
  error_text TEXT, started_at TEXT, ended_at TEXT, worker_pid INTEGER, container_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_run ON node_attempts(run_id, task_id, attempt);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, rel_path TEXT NOT NULL,
  content_hash TEXT NOT NULL, producer_attempt_id TEXT, parent_ids_json TEXT NOT NULL,
  requirement_revision INTEGER NOT NULL, candidate_hash TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts TEXT NOT NULL, task_id TEXT,
  attempt_id TEXT, actor TEXT NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL, status TEXT NOT NULL,
  scope TEXT NOT NULL, graph_revision INTEGER NOT NULL, requirement_revision INTEGER NOT NULL,
  subject_hash TEXT NOT NULL, summary TEXT NOT NULL, human_label TEXT, rationale TEXT,
  requested_at TEXT NOT NULL, decided_at TEXT, invalidated_reason TEXT
);
CREATE TABLE IF NOT EXISTS clarifications (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, requirement_revision INTEGER NOT NULL, status TEXT NOT NULL,
  questions_json TEXT NOT NULL, answers_json TEXT, asked_at TEXT NOT NULL, answered_at TEXT
);
CREATE TABLE IF NOT EXISTS operations (
  op_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, precondition_hash TEXT NOT NULL,
  result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


class LeaseError(RuntimeError):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._local = threading.local()
        bootstrap = self._connect()
        bootstrap.executescript(SCHEMA)  # executescript manages its own transaction; keep it out of conn()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    @contextmanager
    def conn(self):
        """One transaction per block, serialized in-process; BEGIN IMMEDIATE for cross-process safety."""
        with self._lock:
            c = getattr(self._local, "conn", None)
            if c is None:
                c = self._connect()
                self._local.conn = c
            if getattr(self._local, "depth", 0) == 0:
                c.execute("BEGIN IMMEDIATE")
            self._local.depth = getattr(self._local, "depth", 0) + 1
            try:
                yield c
                self._local.depth -= 1
                if self._local.depth == 0:
                    c.execute("COMMIT")
            except BaseException:
                self._local.depth -= 1
                if self._local.depth == 0:
                    c.execute("ROLLBACK")
                raise

    # ---- runs -------------------------------------------------------------
    def create_run(self, scenario: str, mode: str, config: dict, requirement_text: str, baseline_hash: str | None) -> str:
        run_id = new_id("run")
        now = iso()
        with self.conn() as c:
            c.execute(
                "INSERT INTO runs(id,scenario,mode,status,requirement_revision,graph_revision,candidate_revision,"
                "baseline_hash,config_json,created_at,updated_at) VALUES(?,?,?,?,1,0,0,?,?,?,?)",
                (run_id, scenario, mode, "PENDING", baseline_hash, json.dumps(config), now, now),
            )
            self._insert_requirement(c, run_id, 1, requirement_text, "initial", [])
            self.append_event(run_id, "run_created", {"scenario": scenario, "mode": mode, "config": config}, actor="human")
        return run_id

    def get_run(self, run_id: str) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            raise KeyError(run_id)
        d = dict(r)
        d["config"] = json.loads(d.pop("config_json"))
        return d

    def list_runs(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["config"] = json.loads(d.pop("config_json"))
            out.append(d)
        return out

    def update_run(self, run_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def set_status(self, run_id: str, status: str, actor: str = "coordinator", **payload) -> None:
        """A terminal run (SUCCEEDED/FAILED/STOPPED) never leaves that state through this method, no matter
        who calls it or why. The fifth review round fixed three human-triggered call sites that could
        resurrect a just-STOPPED run via a stale-status-then-write race; a sixth review found the guard was
        too narrow -- the scheduler's own internal set_status calls (the approval gate, a blocking
        clarification, a repair cycle re-entering RUNNING) had the identical unconditional-write shape and
        could resurrect STOPPED too, since Stop can land in the same narrow window relative to any of them.
        Enforcing "terminal is terminal" once, here, closes it for every caller -- present and future --
        instead of auditing and patching each call site individually."""
        assert status in RUN_STATUSES, status
        with self.conn():
            old = self.get_run(run_id)["status"]
            if old in TERMINAL_RUN:
                return
            if old == status:
                return
            self.update_run(run_id, status=status)
            self.append_event(run_id, "run_status", {"from": old, "to": status, **payload}, actor=actor)

    def set_status_if(self, run_id: str, expected: str, status: str, actor: str = "coordinator", **payload) -> bool:
        """Compare-and-set: only transitions if the run's *current* status is still `expected`, checked and
        written inside the same transaction. Returns whether it transitioned. Used wherever a caller read
        the run's status earlier and wants to act on it later (e.g. after a slow model call or another
        lock's critical section) without silently resurrecting a run that a concurrent stop() already moved
        to STOPPED in between (fifth review round, finding: stale-status-then-write could undo Safe Stop).
        Also refuses out of a terminal state even if `expected` somehow matched it, for the same reason as
        `set_status` above (sixth review round)."""
        assert status in RUN_STATUSES, status
        with self.conn():
            old = self.get_run(run_id)["status"]
            if old in TERMINAL_RUN:
                return False
            if old != expected:
                return False
            if old == status:
                return True
            self.update_run(run_id, status=status)
            self.append_event(run_id, "run_status", {"from": old, "to": status, **payload}, actor=actor)
            return True

    def add_provider_call(self, run_id: str) -> int:
        with self.conn() as c:
            c.execute("UPDATE runs SET provider_calls=provider_calls+1, updated_at=? WHERE id=?", (iso(), run_id))
            return c.execute("SELECT provider_calls FROM runs WHERE id=?", (run_id,)).fetchone()[0]

    def add_active_seconds(self, run_id: str, seconds: float) -> float:
        with self.conn() as c:
            c.execute("UPDATE runs SET active_seconds=active_seconds+?, updated_at=? WHERE id=?", (seconds, iso(), run_id))
            return c.execute("SELECT active_seconds FROM runs WHERE id=?", (run_id,)).fetchone()[0]

    # ---- lease -----------------------------------------------------------
    def acquire_lease(self, run_id: str, owner: str, ttl_s: float = 60.0) -> None:
        from datetime import timedelta

        from .util import parse_iso, utcnow

        with self.conn() as c:
            r = c.execute("SELECT lease_owner, lease_expires FROM runs WHERE id=?", (run_id,)).fetchone()
            if r is None:
                raise KeyError(run_id)
            if r["lease_owner"] and r["lease_owner"] != owner and r["lease_expires"] and parse_iso(r["lease_expires"]) > utcnow():
                raise LeaseError(f"run {run_id} is owned by scheduler {r['lease_owner']} until {r['lease_expires']}")
            exp = iso(utcnow() + timedelta(seconds=ttl_s))
            c.execute("UPDATE runs SET lease_owner=?, lease_expires=? WHERE id=?", (owner, exp, run_id))

    def release_lease(self, run_id: str, owner: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE runs SET lease_owner=NULL, lease_expires=NULL WHERE id=? AND lease_owner=?", (run_id, owner))

    # ---- requirements ----------------------------------------------------
    def _insert_requirement(self, c, run_id: str, rev: int, text: str, reason: str, clarifications: list) -> str:
        h = sha256_text(canonical_json({"text": text, "clarifications": clarifications}))
        c.execute(
            "INSERT INTO requirement_revisions(run_id,revision,text,reason,clarifications_json,hash,created_at) VALUES(?,?,?,?,?,?,?)",
            (run_id, rev, text, reason, json.dumps(clarifications), h, iso()),
        )
        return h

    def add_requirement_revision(self, run_id: str, text: str, reason: str, clarifications: list, actor: str = "human") -> int:
        with self.conn() as c:
            rev = self.get_run(run_id)["requirement_revision"] + 1
            h = self._insert_requirement(c, run_id, rev, text, reason, clarifications)
            self.update_run(run_id, requirement_revision=rev)
            self.append_event(run_id, "requirement_revised", {"revision": rev, "reason": reason, "hash": h}, actor=actor)
        return rev

    def get_requirement(self, run_id: str, rev: int | None = None) -> dict:
        with self.conn() as c:
            if rev is None:
                rev = self.get_run(run_id)["requirement_revision"]
            r = c.execute("SELECT * FROM requirement_revisions WHERE run_id=? AND revision=?", (run_id, rev)).fetchone()
        d = dict(r)
        d["clarifications"] = json.loads(d.pop("clarifications_json"))
        return d

    def list_requirements(self, run_id: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM requirement_revisions WHERE run_id=? ORDER BY revision", (run_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["clarifications"] = json.loads(d.pop("clarifications_json"))
            out.append(d)
        return out

    # ---- graphs ----------------------------------------------------------
    def add_graph_revision(self, run_id: str, graph: dict, requirement_revision: int, reason: str, diff: dict | None) -> int:
        with self.conn() as c:
            rev = self.get_run(run_id)["graph_revision"] + 1
            h = sha256_text(canonical_json(graph))
            c.execute(
                "INSERT INTO graph_revisions(run_id,revision,requirement_revision,graph_json,hash,diff_json,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, rev, requirement_revision, json.dumps(graph), h, json.dumps(diff) if diff else None, reason, iso()),
            )
            self.update_run(run_id, graph_revision=rev)
            self.append_event(run_id, "graph_revision", {"revision": rev, "hash": h, "reason": reason, "diff": diff}, actor="coordinator")
        return rev

    def get_graph(self, run_id: str, rev: int | None = None) -> dict | None:
        with self.conn() as c:
            if rev is None:
                rev = self.get_run(run_id)["graph_revision"]
            if rev == 0:
                return None
            r = c.execute("SELECT * FROM graph_revisions WHERE run_id=? AND revision=?", (run_id, rev)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["graph"] = json.loads(d.pop("graph_json"))
        d["diff"] = json.loads(d["diff_json"]) if d.get("diff_json") else None
        d.pop("diff_json", None)
        return d

    def list_graphs(self, run_id: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT revision FROM graph_revisions WHERE run_id=? ORDER BY revision", (run_id,)).fetchall()
        return [self.get_graph(run_id, r["revision"]) for r in rows]

    # ---- attempts --------------------------------------------------------
    def start_attempt(self, run_id: str, task_id: str, graph_revision: int, candidate_revision: int, input_hashes: dict) -> str:
        with self.conn() as c:
            n = c.execute("SELECT COUNT(*) FROM node_attempts WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()[0]
            aid = new_id("att")
            c.execute(
                "INSERT INTO node_attempts(id,run_id,task_id,graph_revision,candidate_revision,attempt,status,input_hashes_json,"
                "output_artifact_ids_json,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (aid, run_id, task_id, graph_revision, candidate_revision, n + 1, "RUNNING", json.dumps(input_hashes), "[]", iso()),
            )
            self.append_event(
                run_id,
                "node_started",
                {
                    "attempt": n + 1,
                    "graph_revision": graph_revision,
                    "candidate_revision": candidate_revision,
                    "input_hashes": input_hashes,
                },
                task_id=task_id,
                attempt_id=aid,
            )
        return aid

    def finish_attempt(
        self,
        attempt_id: str,
        status: str,
        output_artifact_ids: list[str] | None = None,
        error_category: str | None = None,
        error_text: str | None = None,
        payload: dict | None = None,
    ) -> None:
        assert status in NODE_STATUSES
        with self.conn() as c:
            r = c.execute("SELECT run_id, task_id, attempt FROM node_attempts WHERE id=?", (attempt_id,)).fetchone()
            c.execute(
                "UPDATE node_attempts SET status=?, output_artifact_ids_json=?, error_category=?, error_text=?, ended_at=? WHERE id=?",
                (status, json.dumps(output_artifact_ids or []), error_category, error_text, iso(), attempt_id),
            )
            self.append_event(
                r["run_id"],
                "node_finished",
                {"status": status, "attempt": r["attempt"], "error_category": error_category, "error": error_text, **(payload or {})},
                task_id=r["task_id"],
                attempt_id=attempt_id,
            )

    def set_attempt_worker(self, attempt_id: str, pid: int | None = None, container_id: str | None = None) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE node_attempts SET worker_pid=COALESCE(?,worker_pid), container_id=COALESCE(?,container_id) WHERE id=?",
                (pid, container_id, attempt_id),
            )

    def list_attempts(self, run_id: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM node_attempts WHERE run_id=? ORDER BY started_at, attempt", (run_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["input_hashes"] = json.loads(d.pop("input_hashes_json"))
            d["output_artifact_ids"] = json.loads(d.pop("output_artifact_ids_json"))
            out.append(d)
        return out

    def latest_attempts(self, run_id: str) -> dict[str, dict]:
        """Latest attempt per task_id."""
        out: dict[str, dict] = {}
        for a in self.list_attempts(run_id):
            out[a["task_id"]] = a
        return out

    def set_node_status(self, run_id: str, task_ids: list[str], status: str, reason: str, actor: str = "coordinator") -> None:
        """Mark the latest attempt of each task with a status (INVALIDATED, CANCELLED, INTERRUPTED)."""
        assert status in NODE_STATUSES
        with self.conn() as c:
            latest = self.latest_attempts(run_id)
            for t in task_ids:
                a = latest.get(t)
                if not a:
                    continue
                c.execute("UPDATE node_attempts SET status=?, ended_at=COALESCE(ended_at,?) WHERE id=?", (status, iso(), a["id"]))
                self.append_event(
                    run_id,
                    "node_" + status.lower(),
                    {"reason": reason, "previous_status": a["status"]},
                    task_id=t,
                    attempt_id=a["id"],
                    actor=actor,
                )

    # ---- artifacts -------------------------------------------------------
    def add_artifact(
        self,
        run_id: str,
        kind: str,
        rel_path: str,
        content_hash: str,
        producer_attempt_id: str | None,
        parent_ids: list[str],
        requirement_revision: int,
        candidate_hash: str | None,
    ) -> str:
        aid = new_id("art")
        with self.conn() as c:
            c.execute(
                "INSERT INTO artifacts(id,run_id,kind,rel_path,content_hash,producer_attempt_id,parent_ids_json,requirement_revision,candidate_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    run_id,
                    kind,
                    rel_path,
                    content_hash,
                    producer_attempt_id,
                    json.dumps(parent_ids),
                    requirement_revision,
                    candidate_hash,
                    iso(),
                ),
            )
            self.append_event(
                run_id,
                "artifact_created",
                {"artifact_id": aid, "kind": kind, "rel_path": rel_path, "content_hash": content_hash, "parents": parent_ids},
                attempt_id=producer_attempt_id,
            )
        return aid

    def list_artifacts(self, run_id: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["parent_ids"] = json.loads(d.pop("parent_ids_json"))
            out.append(d)
        return out

    # ---- events ----------------------------------------------------------
    def append_event(
        self,
        run_id: str,
        type_: str,
        payload: dict,
        actor: str = "coordinator",
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> int:
        payload = redact_obj(payload)
        with self.conn() as c:
            prev = c.execute("SELECT hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
            prev_hash = prev["hash"] if prev else "0" * 64
            ts = iso()
            body = canonical_json(
                {
                    "run_id": run_id,
                    "ts": ts,
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "actor": actor,
                    "type": type_,
                    "payload": payload,
                    "prev": prev_hash,
                }
            )
            h = sha256_text(body)
            cur = c.execute(
                "INSERT INTO events(run_id,ts,task_id,attempt_id,actor,type,payload_json,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, ts, task_id, attempt_id, actor, type_, json.dumps(payload), prev_hash, h),
            )
            return cur.lastrowid

    def list_events(self, run_id: str, since_seq: int = 0) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, since_seq)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json"))
            out.append(d)
        return out

    @staticmethod
    def verify_chain(events: list[dict]) -> tuple[bool, str]:
        prev = "0" * 64
        for e in events:
            if e["prev_hash"] != prev:
                return False, f"seq {e['seq']}: prev_hash mismatch"
            body = canonical_json(
                {
                    "run_id": e["run_id"],
                    "ts": e["ts"],
                    "task_id": e["task_id"],
                    "attempt_id": e["attempt_id"],
                    "actor": e["actor"],
                    "type": e["type"],
                    "payload": e["payload"],
                    "prev": e["prev_hash"],
                }
            )
            if sha256_text(body) != e["hash"]:
                return False, f"seq {e['seq']}: hash mismatch"
            prev = e["hash"]
        return True, f"{len(events)} events verified"

    # ---- approvals -------------------------------------------------------
    def request_approval(self, run_id: str, task_id: str, scope: str, subject_hash: str, summary: str) -> str:
        with self.conn() as c:
            run = self.get_run(run_id)
            existing = c.execute(
                "SELECT id FROM approvals WHERE run_id=? AND task_id=? AND status='pending' AND subject_hash=? AND graph_revision=? AND requirement_revision=?",
                (run_id, task_id, subject_hash, run["graph_revision"], run["requirement_revision"]),
            ).fetchone()
            if existing:
                return existing["id"]
            aid = new_id("apr")
            c.execute(
                "INSERT INTO approvals(id,run_id,task_id,status,scope,graph_revision,requirement_revision,subject_hash,summary,requested_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (aid, run_id, task_id, "pending", scope, run["graph_revision"], run["requirement_revision"], subject_hash, summary, iso()),
            )
            self.append_event(
                run_id,
                "approval_requested",
                {"approval_id": aid, "scope": scope, "subject_hash": subject_hash, "summary": summary},
                task_id=task_id,
            )
        return aid

    def decide_approval(self, approval_id: str, approve: bool, human_label: str, rationale: str) -> dict:
        with self.conn() as c:
            a = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if a is None:
                raise KeyError(approval_id)
            if a["status"] != "pending":
                raise ValueError(f"approval {approval_id} is {a['status']}, not pending")
            status = "approved" if approve else "rejected"
            c.execute(
                "UPDATE approvals SET status=?, human_label=?, rationale=?, decided_at=? WHERE id=?",
                (status, human_label, rationale, iso(), approval_id),
            )
            self.append_event(
                a["run_id"],
                "approval_decided",
                {
                    "approval_id": approval_id,
                    "scope": a["scope"],
                    "decision": status,
                    "subject_hash": a["subject_hash"],
                    "rationale": rationale,
                    "human": human_label,
                },
                actor=f"human:{human_label}",
                task_id=a["task_id"],
            )
            return dict(c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())

    def invalidate_approvals(self, run_id: str, reason: str, scopes: tuple[str, ...] | None = None) -> list[str]:
        with self.conn() as c:
            rows = c.execute("SELECT id, scope FROM approvals WHERE run_id=? AND status IN ('pending','approved')", (run_id,)).fetchall()
            ids = []
            for r in rows:
                if scopes and r["scope"] not in scopes:
                    continue
                c.execute("UPDATE approvals SET status='invalidated', invalidated_reason=? WHERE id=?", (reason, r["id"]))
                ids.append(r["id"])
                self.append_event(run_id, "approval_invalidated", {"approval_id": r["id"], "scope": r["scope"], "reason": reason})
        return ids

    def find_approval(self, run_id: str, task_id: str, subject_hash: str, graph_revision: int, requirement_revision: int) -> dict | None:
        with self.conn() as c:
            r = c.execute(
                "SELECT * FROM approvals WHERE run_id=? AND task_id=? AND subject_hash=? AND graph_revision=? AND requirement_revision=? ORDER BY requested_at DESC LIMIT 1",
                (run_id, task_id, subject_hash, graph_revision, requirement_revision),
            ).fetchone()
        return dict(r) if r else None

    def list_approvals(self, run_id: str) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM approvals WHERE run_id=? ORDER BY requested_at", (run_id,)).fetchall()]

    def get_approval(self, approval_id: str) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not r:
            raise KeyError(approval_id)
        return dict(r)

    # ---- clarifications --------------------------------------------------
    def ask_clarification(self, run_id: str, questions: list[dict]) -> str:
        with self.conn() as c:
            run = self.get_run(run_id)
            cid = new_id("clr")
            c.execute(
                "INSERT INTO clarifications(id,run_id,requirement_revision,status,questions_json,asked_at) VALUES(?,?,?,?,?,?)",
                (cid, run_id, run["requirement_revision"], "pending", json.dumps(questions), iso()),
            )
            self.append_event(run_id, "clarification_requested", {"clarification_id": cid, "questions": questions}, task_id="requirements")
        return cid

    def answer_clarification(self, clarification_id: str, answers: list[dict], human_label: str) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT * FROM clarifications WHERE id=?", (clarification_id,)).fetchone()
            if r is None:
                raise KeyError(clarification_id)
            if r["status"] != "pending":
                raise ValueError("clarification already answered")
            c.execute(
                "UPDATE clarifications SET status='answered', answers_json=?, answered_at=? WHERE id=?",
                (json.dumps(answers), iso(), clarification_id),
            )
            self.append_event(
                r["run_id"],
                "clarification_answered",
                {"clarification_id": clarification_id, "answers": answers},
                actor=f"human:{human_label}",
                task_id="requirements",
            )
            return dict(c.execute("SELECT * FROM clarifications WHERE id=?", (clarification_id,)).fetchone())

    def list_clarifications(self, run_id: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM clarifications WHERE run_id=? ORDER BY asked_at", (run_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["questions"] = json.loads(d.pop("questions_json"))
            d["answers"] = json.loads(d["answers_json"]) if d.get("answers_json") else None
            d.pop("answers_json", None)
            out.append(d)
        return out

    def pending_clarification(self, run_id: str) -> dict | None:
        for cl in self.list_clarifications(run_id):
            if cl["status"] == "pending":
                return cl
        return None

    # ---- idempotent operations ------------------------------------------
    def operation(self, op_id: str, run_id: str, kind: str, precondition_hash: str) -> dict | None:
        """Return prior result if this exact operation already ran (idempotent mutation guard)."""
        with self.conn() as c:
            r = c.execute("SELECT result_json, precondition_hash FROM operations WHERE op_id=?", (op_id,)).fetchone()
        if r is None:
            return None
        if r["precondition_hash"] != precondition_hash:
            raise ValueError(f"operation {op_id} was recorded with a different precondition hash")
        return json.loads(r["result_json"])

    def record_operation(self, op_id: str, run_id: str, kind: str, precondition_hash: str, result: dict) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO operations(op_id,run_id,kind,precondition_hash,result_json,created_at) VALUES(?,?,?,?,?,?)",
                (op_id, run_id, kind, precondition_hash, json.dumps(result), iso()),
            )
