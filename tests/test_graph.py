from __future__ import annotations

import pytest

from coordinator.graph import GraphValidationError, TaskGraph, TaskNode, build_standard_graph


def test_valid_minimal_graph_topo_order():
    g = TaskGraph(
        [
            TaskNode("a", "intake"),
            TaskNode("b", "requirements", dependencies=("a",)),
            TaskNode("c", "plan", dependencies=("b",)),
        ]
    )
    order = g.validate(require_gates=False)
    assert order == ["a", "b", "c"]


def test_cycle_rejected():
    g = TaskGraph([TaskNode("a", "intake", dependencies=("b",)), TaskNode("b", "requirements", dependencies=("a",))])
    with pytest.raises(GraphValidationError, match="cycle"):
        g.validate(require_gates=False)


def test_unknown_dependency_rejected():
    g = TaskGraph([TaskNode("a", "intake", dependencies=("ghost",))])
    with pytest.raises(GraphValidationError, match="unknown dependency"):
        g.validate(require_gates=False)


def test_duplicate_task_id_rejected():
    g = TaskGraph([TaskNode("a", "intake"), TaskNode("a", "requirements")])
    with pytest.raises(GraphValidationError, match="duplicate"):
        g.validate(require_gates=False)


def test_missing_required_gates_rejected():
    g = TaskGraph([TaskNode("a", "intake")])
    with pytest.raises(GraphValidationError, match="missing required gate"):
        g.validate(require_gates=True)


def test_too_many_tasks_rejected():
    g = TaskGraph([TaskNode(f"t{i}", "intake") for i in range(25)])
    with pytest.raises(GraphValidationError, match="limit"):
        g.validate(max_tasks=20, require_gates=False)


def test_build_standard_graph_wires_gates_and_validates():
    plan_tasks = [
        {
            "task_id": "t1",
            "title": "impl",
            "dependencies": [],
            "allowed_paths": ["app/*"],
            "acceptance_criteria_ids": ["AC-1"],
            "risk_level": "low",
        }
    ]
    g = build_standard_graph(plan_tasks, brownfield=False, needs_migration_approval=False)
    ids = g.by_id()
    assert ids["plan_approval"].kind == "approval" and ids["plan_approval"].approval_scope == "plan"
    assert ids["release_approval"].kind == "approval" and ids["release_approval"].approval_scope == "release"
    assert "migration_approval" not in ids
    assert ids["t1"].dependencies == ("plan_approval",)
    assert set(ids["join"].dependencies) == {"validate", "review", "docs"}


def test_build_standard_graph_with_migration_inserts_migration_approval():
    plan_tasks = [
        {
            "task_id": "t1",
            "title": "impl",
            "dependencies": [],
            "allowed_paths": ["app/*"],
            "acceptance_criteria_ids": ["AC-1"],
            "risk_level": "high",
        }
    ]
    g = build_standard_graph(plan_tasks, brownfield=True, needs_migration_approval=True)
    ids = g.by_id()
    assert ids["migration_approval"].kind == "approval" and ids["migration_approval"].approval_scope == "migration"
    assert ids["t1"].dependencies == ("migration_approval",)


def test_build_standard_graph_no_tasks_rejected():
    with pytest.raises(GraphValidationError, match="no implementation tasks"):
        build_standard_graph([], brownfield=False, needs_migration_approval=False)


def test_affected_by_inputs_includes_transitive_dependents():
    g = TaskGraph(
        [
            TaskNode("a", "requirements", inputs=("requirement",)),
            TaskNode("b", "plan", dependencies=("a",), inputs=("requirement",)),
            TaskNode("c", "implement", dependencies=("b",), inputs=("baseline",)),
            TaskNode("d", "validate", dependencies=("c",), inputs=("candidate",)),
        ]
    )
    affected = g.affected_by_inputs({"requirement"})
    assert affected == {"a", "b", "c", "d"}
