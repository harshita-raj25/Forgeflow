"""Task graph: typed nodes, validation (cycles, unknown deps, duplicates, required gates), topological order.

A graph revision is immutable once stored. Recovery and replanning create new attempts or new
revisions; the persisted DAG never contains a cycle.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

NODE_KINDS = {
    "intake",
    "baseline_analysis",
    "requirements",
    "clarification",
    "plan",
    "approval",
    "implement",
    "freeze",
    "validate",
    "review",
    "docs",
    "join",
    "export",
}

# Gates that every executable graph must contain, in dependency order.
REQUIRED_GATES = ("plan_approval", "join", "release_approval")

# Which declared inputs a node kind consumes. Used for dependency-driven invalidation.
DEFAULT_INPUTS = {
    "intake": ("input",),
    "baseline_analysis": ("baseline",),
    "requirements": ("input", "requirement"),
    "clarification": ("requirement",),
    "plan": ("requirement", "baseline"),
    "approval": ("requirement",),
    "implement": ("requirement", "baseline"),
    "freeze": ("requirement",),
    "validate": ("candidate",),
    "review": ("requirement", "candidate"),
    "docs": ("requirement", "candidate"),
    "join": ("candidate",),
    "export": ("candidate",),
}


@dataclass(frozen=True)
class TaskNode:
    task_id: str
    kind: str
    role: str = "coordinator"
    dependencies: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    acceptance_criteria_ids: tuple[str, ...] = ()
    risk_level: str = "low"
    entry_gates: tuple[str, ...] = ()
    exit_gates: tuple[str, ...] = ()
    timeout_s: float = 300.0
    retry_limit: int = 0
    title: str = ""
    approval_scope: str = ""  # for approval nodes: "plan" | "release" | "migration"
    input_artifact_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> TaskNode:
        d = dict(d)
        for k in (
            "dependencies",
            "inputs",
            "allowed_paths",
            "acceptance_criteria_ids",
            "entry_gates",
            "exit_gates",
            "input_artifact_ids",
        ):
            d[k] = tuple(d.get(k, ()))
        return cls(**d)


class GraphValidationError(ValueError):
    pass


@dataclass
class TaskGraph:
    nodes: list[TaskNode] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"nodes": [n.to_dict() for n in self.nodes]}

    @classmethod
    def from_dict(cls, d: dict) -> TaskGraph:
        return cls(nodes=[TaskNode.from_dict(n) for n in d["nodes"]])

    def by_id(self) -> dict[str, TaskNode]:
        return {n.task_id: n for n in self.nodes}

    def validate(self, max_tasks: int = 20, require_gates: bool = True) -> list[str]:
        """Validate structure. Returns topological order or raises GraphValidationError."""
        if not self.nodes:
            raise GraphValidationError("graph has no nodes")
        if len(self.nodes) > max_tasks:
            raise GraphValidationError(f"graph has {len(self.nodes)} nodes, limit is {max_tasks}")
        seen: set[str] = set()
        for n in self.nodes:
            if not n.task_id or "/" in n.task_id or " " in n.task_id:
                raise GraphValidationError(f"invalid task id {n.task_id!r}")
            if n.task_id in seen:
                raise GraphValidationError(f"duplicate task id {n.task_id!r}")
            seen.add(n.task_id)
            if n.kind not in NODE_KINDS:
                raise GraphValidationError(f"node {n.task_id}: unknown kind {n.kind!r}")
            if n.kind == "approval" and n.approval_scope not in ("plan", "release", "migration"):
                raise GraphValidationError(f"approval node {n.task_id} needs approval_scope plan|release|migration")
        ids = self.by_id()
        for n in self.nodes:
            for d in n.dependencies:
                if d not in ids:
                    raise GraphValidationError(f"node {n.task_id}: unknown dependency {d!r}")
                if d == n.task_id:
                    raise GraphValidationError(f"node {n.task_id}: depends on itself")
        order = self._topo(ids)
        if require_gates:
            for gate in REQUIRED_GATES:
                if gate not in ids:
                    raise GraphValidationError(f"missing required gate node {gate!r}")
            if ids["plan_approval"].kind != "approval" or ids["plan_approval"].approval_scope != "plan":
                raise GraphValidationError("plan_approval must be an approval node with scope plan")
            if ids["release_approval"].kind != "approval" or ids["release_approval"].approval_scope != "release":
                raise GraphValidationError("release_approval must be an approval node with scope release")
            if ids["join"].kind != "join":
                raise GraphValidationError("join must be a join node")
            pos = {t: i for i, t in enumerate(order)}
            for n in self.nodes:
                if n.kind == "implement" and not self._reaches(ids, "plan_approval", n.task_id):
                    raise GraphValidationError(f"implement node {n.task_id} is not gated by plan_approval")
                if n.kind == "export" and not self._reaches(ids, "release_approval", n.task_id):
                    raise GraphValidationError(f"export node {n.task_id} is not gated by release_approval")
                if n.kind in ("validate", "review", "docs") and not self._reaches(ids, n.task_id, "join"):
                    raise GraphValidationError(f"branch {n.task_id} does not feed join")
            if pos["join"] > pos["release_approval"]:
                raise GraphValidationError("join must precede release_approval")
        return order

    @staticmethod
    def _topo(ids: dict[str, TaskNode]) -> list[str]:
        indeg = {t: len(n.dependencies) for t, n in ids.items()}
        children: dict[str, list[str]] = {t: [] for t in ids}
        for t, n in ids.items():
            for d in n.dependencies:
                children[d].append(t)
        ready = sorted(t for t, d in indeg.items() if d == 0)
        order: list[str] = []
        while ready:
            t = ready.pop(0)
            order.append(t)
            for c in sorted(children[t]):
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
            ready.sort()
        if len(order) != len(ids):
            stuck = sorted(t for t in ids if t not in order)
            raise GraphValidationError(f"graph contains a cycle among {stuck}")
        return order

    @staticmethod
    def _reaches(ids: dict[str, TaskNode], src: str, dst: str) -> bool:
        """True if dst transitively depends on src."""
        stack = [dst]
        seen = set()
        while stack:
            cur = stack.pop()
            if cur == src:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(ids[cur].dependencies)
        return False

    def dependents(self, task_ids: set[str]) -> set[str]:
        """Transitive dependents of the given nodes (excluding themselves)."""
        ids = self.by_id()
        out: set[str] = set()
        changed = True
        while changed:
            changed = False
            for t, n in ids.items():
                if t in out or t in task_ids:
                    continue
                if any(d in task_ids or d in out for d in n.dependencies):
                    out.add(t)
                    changed = True
        return out

    def affected_by_inputs(self, changed_inputs: set[str]) -> set[str]:
        """Nodes consuming any changed input plus all their transitive dependents."""
        direct = {n.task_id for n in self.nodes if set(n.inputs) & changed_inputs}
        return direct | self.dependents(direct)


def diff_graphs(old: TaskGraph, new: TaskGraph) -> dict:
    o, n = old.by_id(), new.by_id()
    added = sorted(set(n) - set(o))
    removed = sorted(set(o) - set(n))
    changed = sorted(t for t in set(o) & set(n) if o[t].to_dict() != n[t].to_dict())
    return {"added": added, "removed": removed, "changed": changed}


def build_standard_graph(
    plan_tasks: list[dict],
    brownfield: bool,
    needs_migration_approval: bool,
    max_tasks: int = 20,
) -> TaskGraph:
    """Compose the governed pipeline around the planner's implementation tasks.

    The planner only proposes implementation tasks (ids, dependencies among themselves, allowed paths,
    criteria, risk). The coordinator owns the gates. This keeps the model unable to remove a gate.
    """
    nodes: list[TaskNode] = [
        TaskNode("intake", "intake", inputs=DEFAULT_INPUTS["intake"], title="Intake"),
    ]
    plan_deps = ["requirements"]
    if brownfield:
        nodes.append(
            TaskNode(
                "baseline_analysis",
                "baseline_analysis",
                role="architect",
                dependencies=("intake",),
                inputs=DEFAULT_INPUTS["baseline_analysis"],
                title="Analyze baseline repository impact",
            )
        )
        plan_deps.append("baseline_analysis")
    nodes.append(
        TaskNode(
            "requirements",
            "requirements",
            role="analyst",
            dependencies=("intake",),
            inputs=DEFAULT_INPUTS["requirements"],
            title="Normalize requirements",
            exit_gates=("acceptance_criteria_present", "no_blocking_questions"),
        )
    )
    nodes.append(
        TaskNode(
            "plan",
            "plan",
            role="architect",
            dependencies=tuple(plan_deps),
            inputs=DEFAULT_INPUTS["plan"],
            title="Design and task graph",
            entry_gates=("requirements_accepted",),
            exit_gates=("valid_graph",),
        )
    )
    nodes.append(
        TaskNode(
            "plan_approval",
            "approval",
            dependencies=("plan",),
            inputs=("requirement", "plan"),
            approval_scope="plan",
            risk_level="high",
            title="Human plan approval",
        )
    )
    impl_root = "plan_approval"
    if needs_migration_approval:
        nodes.append(
            TaskNode(
                "migration_approval",
                "approval",
                dependencies=("plan_approval",),
                inputs=("requirement", "plan", "baseline"),
                approval_scope="migration",
                risk_level="high",
                title="Human approval: additive schema migration on demo copy",
            )
        )
        impl_root = "migration_approval"
    impl_ids: list[str] = []
    for t in plan_tasks:
        tid = str(t["task_id"])
        deps = [impl_root] + [str(d) for d in t.get("dependencies", [])]
        nodes.append(
            TaskNode(
                tid,
                "implement",
                role="implementer",
                dependencies=tuple(deps),
                inputs=DEFAULT_INPUTS["implement"],
                allowed_paths=tuple(t.get("allowed_paths", ())),
                acceptance_criteria_ids=tuple(t.get("acceptance_criteria_ids", ())),
                risk_level=str(t.get("risk_level", "medium")),
                title=str(t.get("title", tid)),
                entry_gates=("plan_approved",),
                exit_gates=("edits_valid", "no_forbidden_paths"),
            )
        )
        impl_ids.append(tid)
    if not impl_ids:
        raise GraphValidationError("planner produced no implementation tasks")
    nodes.append(
        TaskNode(
            "freeze",
            "freeze",
            dependencies=tuple(impl_ids),
            inputs=DEFAULT_INPUTS["freeze"],
            title="Freeze candidate revision (manifest hash)",
            exit_gates=("candidate_manifest",),
        )
    )
    for tid, kind, role, title in (
        ("validate", "validate", "coordinator", "Lint + trusted tests in isolated runner"),
        ("review", "review", "reviewer", "Security / policy review"),
        ("docs", "docs", "documenter", "Documentation draft"),
    ):
        nodes.append(
            TaskNode(
                tid,
                kind,
                role=role,
                dependencies=("freeze",),
                inputs=DEFAULT_INPUTS[kind],
                title=title,
                entry_gates=("immutable_candidate",),
            )
        )
    nodes.append(
        TaskNode(
            "join",
            "join",
            dependencies=("validate", "review", "docs"),
            inputs=DEFAULT_INPUTS["join"],
            title="Join results for same candidate revision",
            exit_gates=("all_branches_pass", "same_candidate_hash"),
        )
    )
    nodes.append(
        TaskNode(
            "release_approval",
            "approval",
            dependencies=("join",),
            inputs=("requirement", "candidate"),
            approval_scope="release",
            risk_level="high",
            title="Human release approval of exact manifest",
        )
    )
    nodes.append(
        TaskNode(
            "export",
            "export",
            dependencies=("release_approval",),
            inputs=DEFAULT_INPUTS["export"],
            title="Export approved candidate + summary",
        )
    )
    g = TaskGraph(nodes)
    g.validate(max_tasks=max_tasks)
    return g
