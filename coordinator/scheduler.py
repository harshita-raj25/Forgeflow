"""Coordinator: graph scheduler with durable state, gates, parallel branches, bounded recovery,
replanning and safe stop. Models propose; this module validates and acts.
"""

from __future__ import annotations

import ast
import json
import shutil
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from .adapters.base import AdapterError, BudgetExhausted, InvalidModelOutput, ModelAdapter, ModelRequest, ModelResponse
from .config import Settings
from .graph import GraphValidationError, TaskGraph, TaskNode, build_standard_graph, diff_graphs
from .policy import PolicyViolation, scan_untrusted_text, validate_edits
from .roles import files_block, load_schema, system_prompt, untrusted
from .runner import DockerRunner, RunnerUnavailable, RunResult
from .scenarios import Scenario, load_scenario
from .store import TERMINAL_RUN, Store
from .util import canonical_json, iso, sha256_json, sha256_text, utcnow
from .workspace import Workspace

RERUNNABLE = {"PENDING", "INVALIDATED", "INTERRUPTED"}
INJECTED_FAULT_MARKER = "FORGEFLOW_INJECTED_FAULT"


class NodeFailure(Exception):
    def __init__(self, category: str, message: str, repairable: bool = False, payload: dict | None = None):
        super().__init__(message)
        self.category = category
        self.repairable = repairable
        self.payload = payload or {}


class StopRequested(Exception):
    pass


class Coordinator:
    def __init__(self, settings: Settings, store: Store, adapter: ModelAdapter, runner: DockerRunner | None):
        self.settings = settings
        self.store = store
        self.adapter = adapter
        self.runner = runner
        self.owner = f"sched-{uuid.uuid4().hex[:8]}"
        self._stop_flags: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ paths
    def run_dir(self, run_id: str) -> Path:
        return self.settings.runs_dir / run_id

    def workspace(self, run_id: str) -> Workspace:
        return Workspace(self.run_dir(run_id))

    def scenario_of(self, run: dict) -> Scenario:
        return load_scenario(run["scenario"])

    # ------------------------------------------------------------ run creation
    def create_run(
        self,
        scenario_id: str,
        requirement_text: str | None = None,
        baseline_source: Path | None = None,
        inject_fault: bool = False,
    ) -> str:
        sc = load_scenario(scenario_id)
        if baseline_source is None and sc.baseline_from:
            baseline_source = self.latest_release_dir(sc.baseline_from)
            if baseline_source is None:
                raise ValueError(f"scenario {scenario_id} needs an approved release of scenario {sc.baseline_from} as baseline; none found")
        config = {
            **self.settings.public_dict(),
            "scenario": sc.as_dict(),
            "inject_fault": inject_fault,
            "baseline_source": str(baseline_source) if baseline_source else None,
        }
        text = requirement_text or sc.requirement
        run_id = self.store.create_run(sc.id, self.adapter.execution_mode, config, text, None)
        ws = self.workspace(run_id)
        baseline_hash = ws.init_baseline(baseline_source)
        ws.reset_candidate_from_baseline()
        self.store.update_run(run_id, baseline_hash=baseline_hash)
        self.store.append_event(
            run_id,
            "baseline_initialized",
            {"baseline_hash": baseline_hash, "files": len(ws.list_files(ws.baseline_dir)), "source": config["baseline_source"]},
        )
        if sc.seed_links:
            self._seed_demo_db(run_id, sc)
        skeleton = self._skeleton_graph(sc)
        self.store.add_graph_revision(run_id, skeleton.to_dict(), 1, "initial skeleton (pre-plan)", None)
        return run_id

    def latest_release_dir(self, scenario_id: str) -> Path | None:
        for r in self.store.list_runs():
            if r["scenario"] == scenario_id and r["status"] == "SUCCEEDED" and r["mode"] == self.adapter.execution_mode:
                p = self.run_dir(r["id"]) / "export" / "release" / "candidate"
                if p.exists():
                    return p
        for r in self.store.list_runs():  # fall back to any mode, but record it
            if r["scenario"] == scenario_id and r["status"] == "SUCCEEDED":
                p = self.run_dir(r["id"]) / "export" / "release" / "candidate"
                if p.exists():
                    return p
        return None

    def _seed_demo_db(self, run_id: str, sc: Scenario) -> None:
        """Copy demo data into the run: a SQLite file with the baseline schema and seeded rows."""
        import sqlite3

        demo_dir = self.run_dir(run_id) / "demo"
        demo_dir.mkdir(parents=True, exist_ok=True)
        db = demo_dir / "demo.db"
        if db.exists():
            db.unlink()
        c = sqlite3.connect(db)
        c.execute(
            "CREATE TABLE IF NOT EXISTS links(code TEXT PRIMARY KEY, target_url TEXT NOT NULL, created_at TEXT NOT NULL, click_count INTEGER NOT NULL DEFAULT 0)"
        )
        for s in sc.seed_links:
            c.execute(
                "INSERT INTO links(code,target_url,created_at,click_count) VALUES(?,?,?,?)",
                (s["code"], s["url"], s.get("created_at", iso()), s.get("clicks", 0)),
            )
        c.commit()
        rows = c.execute("SELECT code, click_count FROM links ORDER BY code").fetchall()
        c.close()
        h = sha256_text(db.read_bytes().hex())
        self.store.append_event(run_id, "demo_db_seeded", {"path": "demo/demo.db", "rows": rows, "db_hash": h})

    def _skeleton_graph(self, sc: Scenario) -> TaskGraph:
        nodes = [TaskNode("intake", "intake", inputs=("input",), title="Intake")]
        deps = ["requirements"]
        if sc.brownfield:
            nodes.append(
                TaskNode(
                    "baseline_analysis",
                    "baseline_analysis",
                    role="coordinator",
                    dependencies=("intake",),
                    inputs=("baseline",),
                    title="Inventory baseline repository",
                )
            )
            deps.append("baseline_analysis")
        nodes.append(
            TaskNode(
                "requirements",
                "requirements",
                role="analyst",
                dependencies=("intake",),
                inputs=("input", "requirement"),
                title="Normalize requirements",
                exit_gates=("acceptance_criteria_present", "no_blocking_questions"),
            )
        )
        nodes.append(
            TaskNode(
                "plan",
                "plan",
                role="architect",
                dependencies=tuple(deps),
                inputs=("requirement", "baseline"),
                title="Design and task graph",
                exit_gates=("valid_graph",),
            )
        )
        g = TaskGraph(nodes)
        g.validate(require_gates=False)
        return g

    # --------------------------------------------------------------- controls
    def stop(self, run_id: str, reason: str, actor: str = "human") -> None:
        with self._lock:
            self._stop_flags.setdefault(run_id, threading.Event()).set()
        run = self.store.get_run(run_id)
        if run["status"] in TERMINAL_RUN:
            return
        killed = self.runner.kill_all() if self.runner else []
        latest = self.store.latest_attempts(run_id)
        running = [t for t, a in latest.items() if a["status"] == "RUNNING"]
        if running:
            self.store.set_node_status(run_id, running, "CANCELLED", f"safe stop: {reason}", actor=actor)
        self.store.update_run(run_id, stop_reason=reason)
        self.store.set_status(run_id, "STOPPED", actor=actor, reason=reason, killed_workers=killed)
        self.store.append_event(
            run_id,
            "safe_stop",
            {"reason": reason, "killed_workers": killed, "baseline_hash": run["baseline_hash"], "candidate_hash": run["candidate_hash"]},
            actor=actor,
        )

    def _check_stop(self, run_id: str) -> None:
        ev = self._stop_flags.get(run_id)
        if ev and ev.is_set():
            raise StopRequested()

    def answer_clarification(self, run_id: str, clarification_id: str, answers: list[dict]) -> int:
        run = self.store.get_run(run_id)
        if run["status"] != "WAITING_FOR_INPUT":
            raise ValueError(f"run is {run['status']}, not waiting for input")
        cl = self.store.answer_clarification(clarification_id, answers, self.settings.human_label)
        req = self.store.get_requirement(run_id)
        clar = list(req["clarifications"]) + [
            {"clarification_id": cl["id"], "questions": json.loads(cl["questions_json"]), "answers": answers}
        ]
        rev = self._revise(
            run_id, req["text"], f"clarification {clarification_id} answered", clar, actor=f"human:{self.settings.human_label}"
        )
        self.store.set_status(run_id, "PENDING", actor="human", reason="clarification answered")
        return rev

    def revise_requirement(self, run_id: str, text: str, reason: str) -> int:
        run = self.store.get_run(run_id)
        if run["status"] in TERMINAL_RUN:
            raise ValueError(f"run is {run['status']}; cannot revise")
        req = self.store.get_requirement(run_id)
        rev = self._revise(run_id, text, reason, req["clarifications"], actor=f"human:{self.settings.human_label}")
        if run["status"] in ("WAITING_FOR_APPROVAL", "WAITING_FOR_INPUT", "RUNNING"):
            self.store.set_status(run_id, "PENDING", actor="human", reason="requirement revised; replanning")
        return rev

    def _revise(self, run_id: str, text: str, reason: str, clarifications: list, actor: str) -> int:
        """Replanning semantics (PROJECT-SPEC §8): new revision, pause, invalidate affected + dependents, invalidate approvals."""
        with self.store.conn():
            rev = self.store.add_requirement_revision(run_id, text, reason, clarifications, actor=actor)
            g = TaskGraph.from_dict(self.store.get_graph(run_id)["graph"])
            affected = g.affected_by_inputs({"requirement"})
            latest = self.store.latest_attempts(run_id)
            to_cancel = [t for t in affected if t in latest and latest[t]["status"] == "RUNNING"]
            to_invalidate = [t for t in affected if t in latest and latest[t]["status"] not in ("RUNNING", "INVALIDATED", "CANCELLED")]
            preserved = sorted(t for t in latest if t not in affected and latest[t]["status"] == "SUCCEEDED")
            if to_cancel:
                self.store.set_node_status(run_id, to_cancel, "CANCELLED", f"requirement revision {rev}: in-flight result obsolete")
            if to_invalidate:
                self.store.set_node_status(run_id, to_invalidate, "INVALIDATED", f"requirement revision {rev}: {reason}")
            inv = self.store.invalidate_approvals(run_id, f"requirement revision {rev}: {reason}")
            self.store.append_event(
                run_id,
                "replan_impact",
                {
                    "requirement_revision": rev,
                    "affected": sorted(affected),
                    "invalidated": sorted(to_invalidate),
                    "cancelled": to_cancel,
                    "preserved": preserved,
                    "approvals_invalidated": inv,
                },
            )
        return rev

    def decide_approval(self, run_id: str, approval_id: str, approve: bool, rationale: str) -> dict:
        a = self.store.get_approval(approval_id)
        if a["run_id"] != run_id:
            raise ValueError("approval does not belong to run")
        run = self.store.get_run(run_id)
        if a["graph_revision"] != run["graph_revision"] or a["requirement_revision"] != run["requirement_revision"]:
            raise ValueError("approval refers to a stale revision; it cannot be decided")
        d = self.store.decide_approval(approval_id, approve, self.settings.human_label, rationale)
        if run["status"] == "WAITING_FOR_APPROVAL":
            self.store.set_status(run_id, "PENDING", actor=f"human:{self.settings.human_label}", reason=f"approval {d['status']}")
        return d

    # ------------------------------------------------------------- execution
    def resume(self, run_id: str) -> dict:
        """Execute until the run waits for a human, finishes, or stops. Safe to call repeatedly."""
        self.store.acquire_lease(run_id, self.owner)
        with self._lock:
            self._stop_flags.setdefault(run_id, threading.Event()).clear()
        try:
            self._reconcile(run_id)
            run = self.store.get_run(run_id)
            if run["status"] in TERMINAL_RUN:
                return run
            if run["status"] in ("WAITING_FOR_INPUT", "WAITING_FOR_APPROVAL"):
                if not self._wait_resolved(run_id, run):
                    return run
            self.store.set_status(run_id, "RUNNING", reason="resume")
            self._loop(run_id)
        except StopRequested:
            pass
        finally:
            self.store.release_lease(run_id, self.owner)
        return self.store.get_run(run_id)

    def _wait_resolved(self, run_id: str, run: dict) -> bool:
        if run["status"] == "WAITING_FOR_INPUT":
            return self.store.pending_clarification(run_id) is None
        return not any(a["status"] == "pending" for a in self.store.list_approvals(run_id))

    def _reconcile(self, run_id: str) -> None:
        """After a crash: attempts still RUNNING are marked INTERRUPTED; successful artifacts are preserved."""
        latest = self.store.latest_attempts(run_id)
        stuck = [t for t, a in latest.items() if a["status"] == "RUNNING"]
        if stuck:
            ws = self.workspace(run_id)
            self.store.set_node_status(run_id, stuck, "INTERRUPTED", "found RUNNING at resume; previous scheduler did not finish")
            self.store.append_event(
                run_id,
                "reconciled",
                {"interrupted": stuck, "candidate_hash_now": ws.candidate_hash() if ws.candidate_dir.exists() else None},
            )

    def _node_states(self, run_id: str, graph: TaskGraph) -> dict[str, str]:
        latest = self.store.latest_attempts(run_id)
        return {n.task_id: latest.get(n.task_id, {}).get("status", "PENDING") for n in graph.nodes}

    def _ready(self, graph: TaskGraph, states: dict[str, str]) -> list[TaskNode]:
        out = []
        for n in graph.nodes:
            if states[n.task_id] in RERUNNABLE and all(states[d] == "SUCCEEDED" for d in n.dependencies):
                out.append(n)
        return out

    def _loop(self, run_id: str) -> None:
        budget = self.settings.budget
        pool = ThreadPoolExecutor(max_workers=budget.concurrency, thread_name_prefix=f"ff-{run_id[-6:]}")
        futures: dict[Future, TaskNode] = {}
        try:
            while True:
                self._check_stop(run_id)
                run = self.store.get_run(run_id)
                if run["status"] != "RUNNING":
                    break
                if run["active_seconds"] > budget.max_active_seconds:
                    self._safe_stop(
                        run_id, f"active execution budget exhausted ({run['active_seconds']:.0f}s > {budget.max_active_seconds:.0f}s)"
                    )
                    break
                graph = TaskGraph.from_dict(self.store.get_graph(run_id)["graph"])
                states = self._node_states(run_id, graph)
                running_ids = {n.task_id for n in futures.values()}
                ready = [n for n in self._ready(graph, states) if n.task_id not in running_ids]
                # Approval nodes are synchronous gates: decide before dispatching anything else.
                gate = next((n for n in ready if n.kind == "approval"), None)
                if gate and not futures:
                    if not self._approval_gate(run_id, gate, graph):
                        break
                    continue
                ready = [n for n in ready if n.kind != "approval"]
                while ready and len(futures) < budget.concurrency:
                    n = ready.pop(0)
                    futures[pool.submit(self._run_node, run_id, n, graph)] = n
                if not futures:
                    if all(s == "SUCCEEDED" for s in states.values()):
                        self.store.set_status(run_id, "SUCCEEDED")
                    elif any(s == "FAILED" for s in states.values()):
                        self._finalize_failure(run_id, graph, states)
                    elif self.store.get_run(run_id)["status"] == "RUNNING":
                        # nothing ready, nothing running, not all done: blocked (waiting states are set inside nodes)
                        self.store.set_status(run_id, "FAILED", reason="no runnable nodes; graph blocked")
                    break
                done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                for f in done:
                    node = futures.pop(f)
                    exc = f.exception()
                    if isinstance(exc, StopRequested):
                        continue
                    if exc is not None:
                        self.store.append_event(run_id, "scheduler_error", {"task": node.task_id, "error": repr(exc)})
                        self._safe_stop(run_id, f"unexpected scheduler error in {node.task_id}: {exc!r}")
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def _safe_stop(self, run_id: str, reason: str) -> None:
        self.stop(run_id, reason, actor="coordinator")

    # --------------------------------------------------------------- gates
    def _approval_gate(self, run_id: str, node: TaskNode, graph: TaskGraph) -> bool:
        run = self.store.get_run(run_id)
        subject_hash, summary = self._approval_subject(run_id, node, run)
        a = self.store.find_approval(run_id, node.task_id, subject_hash, run["graph_revision"], run["requirement_revision"])
        if a is None or a["status"] == "invalidated":
            self.store.request_approval(run_id, node.task_id, node.approval_scope, subject_hash, summary)
            self.store.set_status(run_id, "WAITING_FOR_APPROVAL", reason=f"{node.task_id} pending", subject_hash=subject_hash)
            return False
        if a["status"] == "pending":
            self.store.set_status(run_id, "WAITING_FOR_APPROVAL", reason=f"{node.task_id} pending", subject_hash=subject_hash)
            return False
        aid = self.store.start_attempt(
            run_id, node.task_id, run["graph_revision"], run["candidate_revision"], {"subject_hash": subject_hash, "approval_id": a["id"]}
        )
        if a["status"] == "approved":
            self.store.finish_attempt(aid, "SUCCEEDED", payload={"approval_id": a["id"], "human": a["human_label"]})
            return True
        self.store.finish_attempt(aid, "BLOCKED", error_category="approval_rejected", error_text=a.get("rationale") or "rejected")
        self.store.update_run(run_id, stop_reason=f"{node.approval_scope} approval rejected")
        self.store.set_status(run_id, "FAILED", reason=f"{node.approval_scope} approval rejected: {a.get('rationale')}")
        return False

    def _approval_subject(self, run_id: str, node: TaskNode, run: dict) -> tuple[str, str]:
        req = self.store.get_requirement(run_id)
        g = self.store.get_graph(run_id)
        if node.approval_scope == "plan":
            h = sha256_json({"graph_hash": g["hash"], "requirement_hash": req["hash"]})
            return (
                h,
                f"Approve plan: graph revision {g['revision']} ({len(g['graph']['nodes'])} nodes) for requirement revision {req['revision']}",
            )
        if node.approval_scope == "migration":
            plan = self._latest_artifact_json(run_id, "plan")
            h = sha256_json({"migration": plan.get("migration"), "requirement_hash": req["hash"]})
            return h, f"Approve additive schema migration on copied demo database: {plan.get('migration', {}).get('description', '')}"
        h = run["candidate_hash"] or ""
        return (
            h,
            f"Approve release of candidate revision {run['candidate_revision']} manifest {h[:16]} for requirement revision {req['revision']}",
        )

    # ----------------------------------------------------------- node runner
    def _run_node(self, run_id: str, node: TaskNode, graph: TaskGraph) -> None:
        self._check_stop(run_id)
        run = self.store.get_run(run_id)
        req = self.store.get_requirement(run_id)
        inputs = {
            "requirement_revision": req["revision"],
            "requirement_hash": req["hash"],
            "graph_revision": run["graph_revision"],
            "candidate_hash": run["candidate_hash"],
        }
        aid = self.store.start_attempt(run_id, node.task_id, run["graph_revision"], run["candidate_revision"], inputs)
        started = time.monotonic()
        try:
            handler = getattr(self, f"_node_{node.kind}")
            result = handler(run_id, node, aid, run, req) or {}
            self._check_stop(run_id)
            if self._stale(run_id, run, req, skip_graph_check=node.kind == "plan"):
                self.store.finish_attempt(
                    aid, "CANCELLED", error_category="stale", error_text="inputs changed while running; result discarded"
                )
                return
            self.store.finish_attempt(
                aid, result.pop("_status", "SUCCEEDED"), output_artifact_ids=result.pop("_artifacts", []), payload=result
            )
        except StopRequested:
            self.store.finish_attempt(aid, "CANCELLED", error_category="stopped", error_text="safe stop")
            raise
        except NodeFailure as e:
            self.store.finish_attempt(
                aid, "FAILED", error_category=e.category, error_text=str(e), payload={"repairable": e.repairable, **e.payload}
            )
        except BudgetExhausted as e:
            self.store.finish_attempt(aid, "FAILED", error_category="budget_exhausted", error_text=str(e))
            self._safe_stop(run_id, f"provider budget exhausted: {e}")
        except AdapterError as e:
            self.store.finish_attempt(aid, "FAILED", error_category=e.category, error_text=str(e))
            self._safe_stop(
                run_id,
                f"provider failure in {node.task_id} ({e.category}): checkpointed for human intervention. Live mode never falls back to fixtures.",
            )
        except (PolicyViolation, GraphValidationError) as e:
            self.store.finish_attempt(
                aid, "FAILED", error_category="policy_violation" if isinstance(e, PolicyViolation) else "invalid_graph", error_text=str(e)
            )
        except RunnerUnavailable as e:
            self.store.finish_attempt(aid, "FAILED", error_category="runner_unavailable", error_text=str(e))
            self._safe_stop(run_id, f"isolated runner unavailable: {e}")
        finally:
            self.store.add_active_seconds(run_id, time.monotonic() - started)

    def _stale(self, run_id: str, run_at_start: dict, req_at_start: dict, skip_graph_check: bool = False) -> bool:
        """True when the run's inputs changed while this node was executing, so its result must be discarded.

        The `plan` node itself publishes a new graph revision as its normal output, so its own attempt
        never treats that self-authored bump as staleness; it still discards its result if a *requirement*
        revision landed concurrently (a genuine replan racing the same plan attempt).
        """
        now = self.store.get_run(run_id)
        if now["requirement_revision"] != req_at_start["revision"]:
            return True
        if skip_graph_check:
            return False
        return now["graph_revision"] != run_at_start["graph_revision"]

    # ------------------------------------------------------------ model call
    def _call_model(self, run_id: str, role: str, task_id: str, user: str, attempt_id: str, revision: int | None = None) -> ModelResponse:
        budget = self.settings.budget
        run = self.store.get_run(run_id)
        if run["provider_calls"] >= budget.max_provider_calls:
            raise BudgetExhausted(f"provider call budget {budget.max_provider_calls} reached before dispatching {role}/{task_id}")
        schema = load_schema(role)
        req = ModelRequest(
            role=role,
            system=system_prompt(role),
            user=user,
            schema=schema,
            schema_name=f"forgeflow_{role}",
            scenario=run["scenario"],
            task_id=task_id,
        )
        corrections = 0
        while True:
            self._check_stop(run_id)
            n = self.store.add_provider_call(run_id)
            self.store.append_event(
                run_id,
                "model_request",
                {"role": role, "call_number": n, "mode": self.adapter.execution_mode, "correction": corrections, "prompt_chars": len(user)},
                task_id=task_id,
                attempt_id=attempt_id,
            )
            try:
                if revision is not None and hasattr(self.adapter, "fixtures_dir"):
                    resp = self.adapter.complete(req, revision=revision)  # type: ignore[call-arg]
                else:
                    resp = self.adapter.complete(req)
            except InvalidModelOutput as e:
                self.store.append_event(
                    run_id,
                    "model_output_invalid",
                    {"role": role, "error": str(e), "correction": corrections},
                    task_id=task_id,
                    attempt_id=attempt_id,
                )
                if corrections >= budget.structured_output_corrections:
                    raise AdapterError(
                        f"invalid model output after {corrections} correction attempt(s): {e}", category="invalid_output"
                    ) from e
                corrections += 1
                req.correction_of = e.raw or "(unparseable output)"
                continue
            self.store.append_event(
                run_id,
                "model_response",
                {
                    "role": role,
                    "mode": resp.execution_mode,
                    "model": resp.model,
                    "request_id": resp.request_id,
                    "usage": resp.usage,
                    "retries": resp.retries,
                    "latency_ms": resp.latency_ms,
                },
                task_id=task_id,
                attempt_id=attempt_id,
            )
            if resp.execution_mode != self.adapter.execution_mode:
                raise AdapterError("adapter returned a response with a different execution mode; refusing", category="mode_mismatch")
            return resp

    # ---------------------------------------------------------- node handlers
    def _node_intake(self, run_id: str, node: TaskNode, aid: str, run: dict, req: dict) -> dict:
        sc = self.scenario_of(run)
        markers = scan_untrusted_text(req["text"])
        ws = self.workspace(run_id)
        ids = []
        p, h = ws.write_artifact("requirement-r1.txt", req["text"])
        ids.append(self.store.add_artifact(run_id, "requirement", "artifacts/requirement-r1.txt", h, aid, [], req["revision"], None))
        if markers:
            self.store.append_event(
                run_id, "untrusted_text_flagged", {"source": "requirement", "markers": markers}, task_id=node.task_id, attempt_id=aid
            )
        return {"_artifacts": ids, "scenario": sc.id, "stage": sc.stage, "brownfield": sc.brownfield, "untrusted_markers": markers}

    def _node_baseline_analysis(self, run_id: str, node: TaskNode, aid: str, run: dict, req: dict) -> dict:
        ws = self.workspace(run_id)
        inventory = []
        flagged = {}
        for rel, content in ws.read_files(ws.baseline_dir).items():
            symbols = []
            if rel.endswith(".py"):
                try:
                    tree = ast.parse(content)
                    symbols = [n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)]
                    symbols += [
                        f"{c.name}.{m.name}"
                        for c in tree.body
                        if isinstance(c, ast.ClassDef)
                        for m in c.body
                        if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
                    ]
                except SyntaxError:
                    symbols = ["<syntax error>"]
            markers = scan_untrusted_text(content)
            if markers:
                flagged[rel] = markers
            inventory.append({"path": rel, "bytes": len(content.encode()), "symbols": symbols})
        if flagged:
            self.store.append_event(
                run_id, "untrusted_text_flagged", {"source": "baseline", "files": flagged}, task_id=node.task_id, attempt_id=aid
            )
        doc = {"baseline_hash": run["baseline_hash"], "files": inventory}
        p, h = ws.write_artifact("baseline-inventory.json", json.dumps(doc, indent=2))
        aid_art = self.store.add_artifact(
            run_id, "baseline_inventory", "artifacts/baseline-inventory.json", h, aid, [], req["revision"], None
        )
        return {"_artifacts": [aid_art], "files": len(inventory), "flagged": flagged}

    def _node_requirements(self, run_id: str, node: TaskNode, aid: str, run: dict, req: dict) -> dict:
        sc = self.scenario_of(run)
        ws = self.workspace(run_id)
        user = "\n\n".join(
            [
                "Scenario: " + sc.title,
                "Target application contract (authoritative):\n" + sc.contract_text(),
                "Raw requirement:\n" + untrusted("requirement", req["text"]),
                "Clarification history (authoritative human answers):\n" + json.dumps(req["clarifications"], indent=2),
                "Existing baseline files: " + ", ".join(ws.list_files(ws.baseline_dir))
                if sc.brownfield
                else "Greenfield: empty workspace.",
            ]
        )
        resp = self._call_model(run_id, "analyst", node.task_id, user, aid, revision=req["revision"])
        data = resp.data
        rel = f"requirements-r{req['revision']}.json"
        _, h = ws.write_artifact(rel, json.dumps(data, indent=2))
        art = self.store.add_artifact(run_id, "requirements", f"artifacts/{rel}", h, aid, [], req["revision"], None)
        if data["blocking_questions"]:
            cid = self.store.ask_clarification(run_id, data["blocking_questions"])
            self.store.set_status(run_id, "WAITING_FOR_INPUT", reason="blocking clarification questions", clarification_id=cid)
            return {"_status": "BLOCKED", "_artifacts": [art], "clarification_id": cid, "questions": len(data["blocking_questions"])}
        if not data["acceptance_criteria"]:
            raise NodeFailure("exit_gate", "analyst produced no acceptance criteria and no blocking questions")
        return {"_artifacts": [art], "criteria": len(data["acceptance_criteria"]), "risk_tags": data["risk_tags"]}

    def _latest_artifact_json(self, run_id: str, kind: str) -> dict:
        arts = [a for a in self.store.list_artifacts(run_id) if a["kind"] == kind]
        if not arts:
            return {}
        p = self.run_dir(run_id) / arts[-1]["rel_path"]
        return json.loads(p.read_text())

    def _node_plan(self, run_id: str, node: TaskNode, aid: str, run: dict, req: dict) -> dict:
        sc = self.scenario_of(run)
        ws = self.workspace(run_id)
        reqs = self._latest_artifact_json(run_id, "requirements")
        parts = [
            "Scenario: " + sc.title,
            "Target application contract (authoritative):\n" + sc.contract_text(),
            "Normalized requirements and acceptance criteria:\n" + json.dumps(reqs, indent=2),
        ]
        if sc.brownfield:
            inv = self._latest_artifact_json(run_id, "baseline_inventory")
            parts.append("Baseline inventory (real files and symbols):\n" + json.dumps(inv, indent=2))
            parts.append("Baseline file contents:\n" + untrusted("baseline", files_block(ws.read_files(ws.baseline_dir))))
        else:
            parts.append("Greenfield: the workspace is empty.")
        resp = self._call_model(run_id, "architect", node.task_id, "\n\n".join(parts), aid, revision=req["revision"])
        plan = resp.data
        if not plan["tasks"]:
            raise NodeFailure("exit_gate", "planner produced no tasks")
        if sc.brownfield:
            inv_paths = {f["path"] for f in self._latest_artifact_json(run_id, "baseline_inventory").get("files", [])}
            cited = {m["path"] for m in plan["impact_map"]}
            if not cited & inv_paths:
                raise NodeFailure("exit_gate", f"impact map cites no real baseline path (cited={sorted(cited)})")
        needs_migration = bool(plan["migration"]["required"])
        if needs_migration and not plan["migration"]["additive_only"]:
            raise NodeFailure("policy", "non-additive migrations are not permitted in this prototype")
        try:
            graph = build_standard_graph(plan["tasks"], sc.brownfield, needs_migration, max_tasks=self.settings.budget.max_tasks)
        except GraphValidationError as e:
            raise NodeFailure("invalid_graph", f"planner graph rejected: {e}") from e
        rel = f"plan-r{req['revision']}.json"
        _, h = ws.write_artifact(rel, json.dumps(plan, indent=2))
        art = self.store.add_artifact(run_id, "plan", f"artifacts/{rel}", h, aid, [], req["revision"], None)
        prev = self.store.get_graph(run_id)
        old = TaskGraph.from_dict(prev["graph"])
        diff = diff_graphs(old, graph)
        self.store.add_graph_revision(run_id, graph.to_dict(), req["revision"], f"plan for requirement revision {req['revision']}", diff)
        self.store.append_event(
            run_id,
            "decision",
            {"decisions": plan["decisions"], "risks": plan["risks"], "impact_map": plan["impact_map"], "artifact": art},
            task_id=node.task_id,
            attempt_id=aid,
        )
        return {"_artifacts": [art], "tasks": [t["task_id"] for t in plan["tasks"]], "migration": plan["migration"], "graph_diff": diff}

    def _node_implement(self, run_id: str, node: TaskNode, aid: str, run: dict, req: dict) -> dict:
        sc = self.scenario_of(run)
        ws = self.workspace(run_id)
        plan = self._latest_artifact_json(run_id, "plan")
        task = next((t for t in plan.get("tasks", []) if t["task_id"] == node.task_id), None)
        if task is None:
            raise NodeFailure("plan_mismatch", f"task {node.task_id} not in approved plan")
        reqs = self._latest_artifact_json(run_id, "requirements")
        repair_ctx = self._repair_context(run_id)
        parts = [
            "Scenario: " + sc.title,
            "Target application contract (authoritative):\n" + sc.contract_text(),
            "Acceptance criteria:\n" + json.dumps(reqs.get("acceptance_criteria", []), indent=2),
            "Your task:\n" + json.dumps(task, indent=2),
            "Design summary:\n" + plan.get("design_summary", ""),
            "Current candidate workspace files:\n" + untrusted("workspace", files_block(ws.read_files())),
        ]
        if repair_ctx:
            parts.append(
                "REPAIR CONTEXT: the previous candidate failed validation or review. Fix the cause.\n" + untrusted("validation", repair_ctx)
            )
        resp = self._call_model(run_id, "implementer", node.task_id, "\n\n".join(parts), aid, revision=run["candidate_revision"] + 1)
        edits = validate_edits(resp.data["edits"], self.settings.budget, node.allowed_paths)
        pre_hash = ws.candidate_hash()
        records = ws.apply_edits(edits)
        post_hash = ws.candidate_hash()
        rel = f"edits-{node.task_id}-c{run['candidate_revision'] + 1}-{aid[-6:]}.json"
        _, h = ws.write_artifact(
            rel, json.dumps({"rationale": resp.data["rationale"], "notes": resp.data["notes"], "records": records}, indent=2)
        )
        art = self.store.add_artifact(run_id, "edit_set", f"artifacts/{rel}", h, aid, [], req["revision"], None)
        self.store.append_event(
            run_id,
            "edits_applied",
            {
                "task": node.task_id,
                "files": [r["path"] for r in records],
                "pre_hash": pre_hash,
                "post_hash": post_hash,
                "rationale": resp.data["rationale"][:500],
            },
            task_id=node.task_id,
            attempt_id=aid,
        )
        return {"_artifacts": [art], "files": len(records), "post_hash": post_hash}

    def _repair_context(self, run_id: str) -> str:
        arts = [a for a in self.store.list_artifacts(run_id) if a["kind"] in ("validation", "review")]
        run = self.store.get_run(run_id)
        if run["repair_cycles"] == 0 or not arts:
            return ""
        out = []
        for a in arts[-2:]:
            p = self.run_dir(run_id) / a["rel_path"]
            out.append(f"--- {a['rel_path']} ---\n" + p.read_text()[:12000])
        return "\n".join(out)

    def _node_freeze(self, run_id: str, node: TaskNode, aid: str, run: dict, req: dict) -> dict:
        ws = self.workspace(run_id)
        sc = self.scenario_of(run)
        rev = run["candidate_revision"] + 1
        if run["config"].get("inject_fault") and rev == 1:
            self._inject_fault(run_id, ws, aid)
        label = f"candidate-r{rev}"
        _, h = ws.snapshot(label)
        manifest = ws.candidate_manifest()
        diff = ws.diff()
        _, mh = ws.write_artifact(f"manifest-c{rev}.json", json.dumps({"candidate_revision": rev, "hash": h, "files": manifest}, indent=2))
        _, dh = ws.write_artifact(f"candidate-c{rev}.diff", diff)
        a1 = self.store.add_artifact(run_id, "manifest", f"artifacts/manifest-c{rev}.json", mh, aid, [], req["revision"], h)
        a2 = self.store.add_artifact(run_id, "diff", f"artifacts/candidate-c{rev}.diff", dh, aid, [a1], req["revision"], h)
        self.store.update_run(run_id, candidate_revision=rev, candidate_hash=h)
        self.store.append_event(
            run_id,
            "candidate_frozen",
            {"candidate_revision": rev, "candidate_hash": h, "files": len(manifest), "stage": sc.stage},
            task_id=node.task_id,
            attempt_id=aid,
        )
        return {"_artifacts": [a1, a2], "candidate_revision": rev, "candidate_hash": h}

    def _inject_fault(self, run_id: str, ws: Workspace, aid: str) -> None:
        """Labeled, one-time fault for the controlled recovery demonstration. Never a hidden defect."""
        target = ws.candidate_dir / "app" / "main.py"
        if not target.exists():
            return
        pre = sha256_text(target.read_text())
        target.write_text(
            target.read_text()
            + f"\n\n# {INJECTED_FAULT_MARKER}: deliberate one-time fault injected by the coordinator for the recovery demo\nraise RuntimeError('{INJECTED_FAULT_MARKER}')\n"
        )
        self.store.append_event(
            run_id,
            "fault_injected",
            {
                "label": INJECTED_FAULT_MARKER,
                "path": "app/main.py",
                "pre_hash": pre,
                "post_hash": sha256_text(target.read_text()),
                "one_time": True,
            },
            task_id="freeze",
            attempt_id=aid,
        )

    def _expected_expired_status(self, run_id: str) -> int:
        """The redirect status trusted tests must expect for expired links, read from the current
        (coordinator-authoritative) normalized requirement rather than trusted by model wording alone.
        Defaults to 410 (the contract's default) when the requirement does not mention expiry at all."""
        reqs = self._latest_artifact_json(run_id, "requirements")
        text = " ".join([reqs.get("normalized_requirement", "")] + [c.get("statement", "") for c in reqs.get("acceptance_criteria", [])])
        if "404" in text and "410" not in text:
            return 404
        return 410

    def _node_validate(self, run_id: str, node: TaskNode, aid: str, run: dict, req: dict) -> dict:
        if self.runner is None:
            raise RunnerUnavailable("no isolated runner configured")
        ws = self.workspace(run_id)
        sc = self.scenario_of(run)
        chash = self.store.get_run(run_id)["candidate_hash"]
        results: dict[str, RunResult] = {}
        vdir = self.run_dir(run_id) / "validation" / f"c{run['candidate_revision']}-{aid[-6:]}"
        vdir.mkdir(parents=True, exist_ok=True)
        extra_env = {"FORGEFLOW_EXPIRED_STATUS": str(self._expected_expired_status(run_id))} if sc.stage == "C" else None
        ids = []
        for name in ("lint", "test"):
            self._check_stop(run_id)
            r = self.runner.run(
                ws.candidate_dir,
                name,
                stage=sc.stage,
                timeout_s=self.settings.budget.test_timeout_s,
                on_start=lambda cname, pid: self.store.set_attempt_worker(aid, pid=pid, container_id=cname),
                extra_env=extra_env,
            )
            results[name] = r
            (vdir / f"{name}.json").write_text(json.dumps(r.as_dict(), indent=2))
            (vdir / f"{name}.stdout.txt").write_text(r.stdout)
            (vdir / f"{name}.stderr.txt").write_text(r.stderr)
            rel = str((vdir / f"{name}.json").relative_to(self.run_dir(run_id)))
            ids.append(
                self.store.add_artifact(run_id, "validation", rel, sha256_text(json.dumps(r.as_dict())), aid, [], req["revision"], chash)
            )
            self.store.append_event(
                run_id,
                "tool_result",
                {
                    "tool": name,
                    "exit_code": r.exit_code,
                    "timed_out": r.timed_out,
                    "duration_ms": r.duration_ms,
                    "container": r.container_id,
                    "candidate_hash": chash,
                },
                task_id=node.task_id,
                attempt_id=aid,
            )
            if not r.ok and name == "lint":
                break
        failed = [n for n, r in results.items() if not r.ok]
        if failed:
            raise NodeFailure(
                "validation_failed",
                f"{', '.join(failed)} failed (exit codes {[results[n].exit_code for n in failed]})",
                repairable=True,
                payload={"_artifacts": ids, "candidate_hash": chash},
            )
        return {"_artifacts": ids, "candidate_hash": chash, "lint_ms": results["lint"].duration_ms, "test_ms": results["test"].duration_ms}

    def _node_review(self, run_id: str, node: TaskNode, aid: str, run: dict, req: dict) -> dict:
        ws = self.workspace(run_id)
        sc = self.scenario_of(run)
        chash = self.store.get_run(run_id)["candidate_hash"]
        reqs = self._latest_artifact_json(run_id, "requirements")
        parts = [
            "Scenario: " + sc.title,
            "Target application contract (authoritative):\n" + sc.contract_text(),
            "Acceptance criteria:\n" + json.dumps(reqs.get("acceptance_criteria", []), indent=2),
            f"Frozen candidate hash: {chash}",
            "Diff against baseline:\n" + untrusted("diff", ws.diff()[:60000]),
            "Candidate files:\n" + untrusted("workspace", files_block(ws.read_files())),
        ]
        resp = self._call_model(run_id, "reviewer", node.task_id, "\n\n".join(parts), aid, revision=run["candidate_revision"])
        rel = f"review-c{run['candidate_revision']}-{aid[-6:]}.json"
        _, h = ws.write_artifact(rel, json.dumps(resp.data, indent=2))
        art = self.store.add_artifact(run_id, "review", f"artifacts/{rel}", h, aid, [], req["revision"], chash)
        blocking = [f for f in resp.data["findings"] if f["severity"] in ("high", "blocking")]
        if resp.data["verdict"] == "block":
            raise NodeFailure(
                "review_blocked",
                f"reviewer blocked with {len(blocking)} high/blocking findings",
                repairable=True,
                payload={"_artifacts": [art], "candidate_hash": chash},
            )
        return {"_artifacts": [art], "verdict": resp.data["verdict"], "findings": len(resp.data["findings"]), "candidate_hash": chash}

    def _node_docs(self, run_id: str, node: TaskNode, aid: str, run: dict, req: dict) -> dict:
        ws = self.workspace(run_id)
        sc = self.scenario_of(run)
        chash = self.store.get_run(run_id)["candidate_hash"]
        parts = [
            "Scenario: " + sc.title,
            "Target application contract:\n" + sc.contract_text(),
            f"Frozen candidate hash: {chash}",
            "Candidate files:\n" + untrusted("workspace", files_block(ws.read_files())),
            "Validation results so far: none for this revision (validation runs in parallel; do not claim results).",
        ]
        resp = self._call_model(run_id, "documenter", node.task_id, "\n\n".join(parts), aid, revision=run["candidate_revision"])
        ids = []
        for name, content in (("README.md", resp.data["readme_markdown"]), ("API.md", resp.data["api_markdown"])):
            rel = f"docs-c{run['candidate_revision']}/{name}"
            _, h = ws.write_artifact(rel, content)
            ids.append(self.store.add_artifact(run_id, "doc", f"artifacts/{rel}", h, aid, [], req["revision"], chash))
        rel = f"docs-c{run['candidate_revision']}/summary.json"
        _, h = ws.write_artifact(rel, json.dumps({"limitations": resp.data["limitations"], "summary": resp.data["summary"]}, indent=2))
        ids.append(self.store.add_artifact(run_id, "doc", f"artifacts/{rel}", h, aid, [], req["revision"], chash))
        return {"_artifacts": ids, "candidate_hash": chash}

    def _node_join(self, run_id: str, node: TaskNode, aid: str, run: dict, req: dict) -> dict:
        latest = self.store.latest_attempts(run_id)
        chash = self.store.get_run(run_id)["candidate_hash"]
        branches = {t: latest.get(t) for t in node.dependencies}
        problems = []
        for t, a in branches.items():
            if a is None or a["status"] != "SUCCEEDED":
                problems.append(f"{t}: {a['status'] if a else 'missing'}")
            elif a["input_hashes"].get("candidate_hash") != chash:
                problems.append(f"{t}: ran against {str(a['input_hashes'].get('candidate_hash'))[:12]} not current {chash[:12]}")
        if problems:
            raise NodeFailure("join_failed", "; ".join(problems), repairable=True, payload={"candidate_hash": chash})
        overlap = self._branch_overlap(branches)
        self.store.append_event(
            run_id,
            "join_passed",
            {"candidate_hash": chash, "branches": {t: a["id"] for t, a in branches.items()}, "overlap": overlap},
            task_id=node.task_id,
            attempt_id=aid,
        )
        return {"candidate_hash": chash, "overlap": overlap}

    @staticmethod
    def _branch_overlap(branches: dict[str, dict]) -> dict:
        from .util import parse_iso

        spans = {t: (parse_iso(a["started_at"]), parse_iso(a["ended_at"])) for t, a in branches.items() if a and a.get("ended_at")}
        overlapping = []
        names = list(spans)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                (s1, e1), (s2, e2) = spans[names[i]], spans[names[j]]
                if s1 < e2 and s2 < e1:
                    overlapping.append([names[i], names[j], (min(e1, e2) - max(s1, s2)).total_seconds()])
        return {"spans": {t: [s.isoformat(), e.isoformat()] for t, (s, e) in spans.items()}, "overlapping_pairs": overlapping}

    def _node_export(self, run_id: str, node: TaskNode, aid: str, run: dict, req: dict) -> dict:
        from .export import export_bundle

        ws = self.workspace(run_id)
        chash = ws.candidate_hash()
        if chash != run["candidate_hash"]:
            raise NodeFailure("hash_mismatch", f"candidate changed after approval: {chash[:12]} != {run['candidate_hash'][:12]}")
        op_id = f"{run_id}:export:{chash}"
        prior = self.store.operation(op_id, run_id, "export", chash)
        if prior:
            self.store.append_event(run_id, "export_skipped_duplicate", {"op_id": op_id}, task_id=node.task_id, attempt_id=aid)
            return prior
        release = self.run_dir(run_id) / "export" / "release"
        if release.exists():
            shutil.rmtree(release)
        shutil.copytree(ws.candidate_dir, release / "candidate", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
        docs = self.run_dir(run_id) / "artifacts" / f"docs-c{run['candidate_revision']}"
        if docs.exists():
            shutil.copytree(docs, release / "docs")
        manifest = {
            "candidate_revision": run["candidate_revision"],
            "candidate_hash": chash,
            "baseline_hash": run["baseline_hash"],
            "requirement_revision": req["revision"],
            "execution_mode": run["mode"],
            "released_at": iso(),
            "files": ws.candidate_manifest(),
        }
        (release / "release-manifest.json").write_text(json.dumps(manifest, indent=2))
        migration = None
        if self.scenario_of(run).stage == "C" and self.runner is not None:
            migration = self._apply_demo_migration(run_id, aid)
        result = {"release_dir": str(release.relative_to(self.run_dir(run_id))), "candidate_hash": chash, "migration": migration}
        self.store.record_operation(op_id, run_id, "export", chash, result)
        self.store.append_event(
            run_id, "release_exported", {**result, "manifest_hash": sha256_json(manifest)}, task_id=node.task_id, attempt_id=aid
        )
        export_bundle(self, run_id)
        return result

    def _apply_demo_migration(self, run_id: str, aid: str) -> dict:
        """Run the trusted migration tool inside the container against the copied demo DB, with backup evidence."""
        demo = self.run_dir(run_id) / "demo"
        ws = self.workspace(run_id)
        r = self.runner.run_migration(
            ws.candidate_dir, demo, on_start=lambda cname, pid: self.store.set_attempt_worker(aid, pid=pid, container_id=cname)
        )
        (demo / "migration-result.json").write_text(json.dumps(r.as_dict(), indent=2))
        try:
            summary = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
        except json.JSONDecodeError:
            summary = {}
        self.store.append_event(
            run_id, "migration_applied", {"exit_code": r.exit_code, "summary": summary}, task_id="export", attempt_id=aid
        )
        if not r.ok:
            raise NodeFailure("migration_failed", f"demo migration failed: exit {r.exit_code}")
        return summary

    # -------------------------------------------------------------- recovery
    def _finalize_failure(self, run_id: str, graph: TaskGraph, states: dict[str, str]) -> None:
        latest = self.store.latest_attempts(run_id)
        failed = [t for t, s in states.items() if s == "FAILED"]
        repairable = [t for t in failed if latest[t].get("error_category") in ("validation_failed", "review_blocked", "join_failed")]
        run = self.store.get_run(run_id)
        budget = self.settings.budget
        if repairable and run["repair_cycles"] < budget.repair_cycles:
            cycle = run["repair_cycles"] + 1
            impl = [n.task_id for n in graph.nodes if n.kind == "implement"]
            downstream = sorted(graph.dependents(set(impl)))
            self.store.update_run(run_id, repair_cycles=cycle)
            self.store.set_node_status(
                run_id, impl + downstream, "INVALIDATED", f"repair cycle {cycle}/{budget.repair_cycles}: {', '.join(failed)} failed"
            )
            self.store.append_event(
                run_id,
                "repair_started",
                {
                    "cycle": cycle,
                    "limit": budget.repair_cycles,
                    "failed": failed,
                    "reissued": impl + downstream,
                    "from_candidate_hash": run["candidate_hash"],
                },
            )
            self.store.set_status(run_id, "RUNNING", reason=f"repair cycle {cycle}")
            self._loop(run_id)
            return
        if repairable:
            self._rollback(run_id, reason=f"repair budget exhausted ({budget.repair_cycles} cycles); failures: {', '.join(failed)}")
            return
        reason = "; ".join(f"{t}: {latest[t].get('error_category')} {latest[t].get('error_text')}" for t in failed)
        self.store.update_run(run_id, stop_reason=reason)
        self.store.set_status(run_id, "FAILED", reason=reason)

    def _rollback(self, run_id: str, reason: str) -> None:
        ws = self.workspace(run_id)
        verified = self._last_verified_snapshot(run_id)
        pre = ws.candidate_hash()
        if verified:
            r = ws.restore(verified)
        else:
            failed_copy = ws.snapshots_dir / f"failed-{iso().replace(':', '')}"
            shutil.copytree(ws.candidate_dir, failed_copy, ignore=shutil.ignore_patterns("__pycache__"))
            ws.reset_candidate_from_baseline()
            r = {
                "snapshot": "baseline",
                "failed_hash": pre,
                "failed_copy": failed_copy.name,
                "backup_hash": ws.baseline_hash(),
                "restored_hash": ws.candidate_hash(),
                "restore_ok": ws.candidate_hash() == ws.baseline_hash(),
            }
        self.store.append_event(run_id, "rollback", {"reason": reason, **r})
        self.store.update_run(run_id, stop_reason=reason, candidate_hash=r["restored_hash"])
        self.store.set_status(run_id, "FAILED", reason=f"rolled back to {r['snapshot']}: {reason}")

    def _last_verified_snapshot(self, run_id: str) -> str | None:
        """The most recent candidate revision whose validate attempt succeeded."""
        ok = [a for a in self.store.list_attempts(run_id) if a["task_id"] == "validate" and a["status"] == "SUCCEEDED"]
        if not ok:
            return None
        return f"candidate-r{ok[-1]['candidate_revision']}"

    # ------------------------------------------------------------ inspection
    def snapshot_view(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        g = self.store.get_graph(run_id)
        graph = TaskGraph.from_dict(g["graph"]) if g else TaskGraph([])
        states = self._node_states(run_id, graph)
        return {
            "run": run,
            "graph": g,
            "states": states,
            "approvals": self.store.list_approvals(run_id),
            "clarifications": self.store.list_clarifications(run_id),
            "requirements": self.store.list_requirements(run_id),
            "artifacts": self.store.list_artifacts(run_id),
            "attempts": self.store.list_attempts(run_id),
        }


def now_iso() -> str:
    return iso(utcnow())


__all__ = ["Coordinator", "NodeFailure", "StopRequested", "canonical_json"]
