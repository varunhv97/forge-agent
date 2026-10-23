"""
Unit tests for `forge/tui/dag_layout.py` — pure structural assertions
(parents strictly lower rank than children, both branches of a fan-out
sharing a rank, etc.), not exact pixel-equivalent positions. Builds
`TaskSpec`/`EdgeSpec` directly rather than going through
`compile_and_validate`: this module only needs the task/edge shape, so it
has none of the registry/permission-profile machinery a real graph needs.
"""

from __future__ import annotations

import pytest

from forge.models.common import NodeKind
from forge.models.graph import EdgeSpec, TaskSpec
from forge.tui.dag_layout import DagLayoutError, compute_dag_layout


def _task(task_id: str, depends_on: list[str] | None = None) -> TaskSpec:
    depends_on = depends_on or []
    return TaskSpec(
        id=task_id,
        description=task_id,
        node_kind=NodeKind.EXECUTION,
        node_type="implement_exec",
        depends_on=depends_on,
        permission_profile="code_edit_default",
    )


def _edges(tasks: list[TaskSpec]) -> list[EdgeSpec]:
    return [EdgeSpec(from_task=dep, to_task=task.id) for task in tasks for dep in task.depends_on]


def test_linear_chain_ranks_increase_by_one() -> None:
    tasks = [_task("a"), _task("b", ["a"]), _task("c", ["b"])]
    layout = compute_dag_layout(tasks, _edges(tasks))

    assert layout.positions["a"].rank == 0
    assert layout.positions["b"].rank == 1
    assert layout.positions["c"].rank == 2
    # A single-lane chain: everything in lane 0.
    assert all(p.lane == 0 for p in layout.positions.values())
    assert layout.num_ranks == 3
    assert layout.lane_counts == {0: 1, 1: 1, 2: 1}


def test_fan_out_fan_in_matches_the_feature_blast_shape() -> None:
    # implement -> {unit_verify, integration_write -> integration_verify}
    #           -> deploy -> manual_test
    tasks = [
        _task("implement"),
        _task("unit_verify", ["implement"]),
        _task("integration_write", ["implement"]),
        _task("integration_verify", ["integration_write"]),
        _task("deploy", ["unit_verify", "integration_verify"]),
        _task("manual_test", ["deploy"]),
    ]
    layout = compute_dag_layout(tasks, _edges(tasks))
    positions = layout.positions

    # Every parent is strictly lower rank than every child.
    for task in tasks:
        for dep in task.depends_on:
            assert positions[dep].rank < positions[task.id].rank

    # Both branches of the fan-out out of `implement` land at the same rank.
    assert positions["unit_verify"].rank == positions["integration_write"].rank

    # The fan-in's rank is forced past the longer branch (integration_write
    # -> integration_verify is two hops from implement, unit_verify is one).
    assert positions["integration_verify"].rank > positions["unit_verify"].rank
    assert positions["deploy"].rank == positions["integration_verify"].rank + 1

    # Distinct tasks placed at the same rank get distinct lanes.
    same_rank = [p for p in positions.values() if p.rank == positions["unit_verify"].rank]
    assert len({p.lane for p in same_rank}) == len(same_rank)

    assert layout.num_ranks == positions["manual_test"].rank + 1
    for r in range(layout.num_ranks):
        assert r in layout.lane_counts


def test_disconnected_tasks_are_each_their_own_root() -> None:
    tasks = [_task("a"), _task("b")]
    layout = compute_dag_layout(tasks, _edges(tasks))
    assert layout.positions["a"].rank == 0
    assert layout.positions["b"].rank == 0
    assert {layout.positions["a"].lane, layout.positions["b"].lane} == {0, 1}


def test_context_only_edges_do_not_affect_rank() -> None:
    a = _task("a")
    b = _task("b")  # no depends_on, but a context_only edge from a
    layout = compute_dag_layout(
        [a, b], [EdgeSpec(from_task="a", to_task="b", relation="context_only")]
    )
    assert layout.positions["a"].rank == 0
    assert layout.positions["b"].rank == 0


def test_cycle_raises() -> None:
    a = TaskSpec(
        id="a",
        description="a",
        node_kind=NodeKind.EXECUTION,
        node_type="implement_exec",
        depends_on=["b"],
        permission_profile="code_edit_default",
    )
    b = TaskSpec(
        id="b",
        description="b",
        node_kind=NodeKind.EXECUTION,
        node_type="implement_exec",
        depends_on=["a"],
        permission_profile="code_edit_default",
    )
    edges = [EdgeSpec(from_task="b", to_task="a"), EdgeSpec(from_task="a", to_task="b")]
    with pytest.raises(DagLayoutError):
        compute_dag_layout([a, b], edges)
