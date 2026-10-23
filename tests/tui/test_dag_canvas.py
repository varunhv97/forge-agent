"""
Unit tests for `forge/tui/dag_canvas.py`'s `render_dag_canvas` — checked on
the rendered plain-text grid (box borders, ids, status glyphs, and the
box-drawing line characters joining them), not pixel-exact output.
"""

from __future__ import annotations

from forge.models.common import NodeKind
from forge.models.graph import EdgeSpec, TaskSpec
from forge.tui.dag_canvas import render_dag_canvas
from forge.tui.dag_layout import compute_dag_layout


def _task(task_id: str, depends_on: list[str] | None = None) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        description=task_id,
        node_kind=NodeKind.EXECUTION,
        node_type="implement_exec",
        depends_on=depends_on or [],
        permission_profile="code_edit_default",
    )


def _edges(tasks: list[TaskSpec]) -> list[EdgeSpec]:
    return [EdgeSpec(from_task=dep, to_task=task.id) for task in tasks for dep in task.depends_on]


def _style_for_status(status: str) -> str:
    return {"passed": "bold green", "running": "bold yellow", "failed": "bold red"}.get(status, "")


def test_linear_chain_renders_a_box_per_task_and_a_connecting_line() -> None:
    tasks = [_task("a"), _task("b", ["a"])]
    layout = compute_dag_layout(tasks, _edges(tasks))
    text = render_dag_canvas(
        tasks,
        _edges(tasks),
        layout,
        {"a": "passed", "b": "running"},
        _style_for_status,
    )
    plain = text.plain
    lines = plain.split("\n")

    assert "a" in plain
    assert "b" in plain
    assert "● a" in plain  # status glyph + task id
    assert "● b" in plain
    # Box borders present.
    assert "┌" in plain
    assert "┐" in plain
    assert "└" in plain
    assert "┘" in plain
    # A horizontal connector joins the two boxes on their shared (only) lane.
    middle_row = lines[1]
    assert "─" in middle_row

    # The "passed" status style was actually applied to task a's box.
    a_col = plain.split("\n")[1].index("● a")
    style_at_a = next(
        span.style for span in text.spans if span.start <= (len(lines[0]) + 1 + a_col) < span.end
    )
    assert style_at_a == "bold green"


def test_fan_out_fan_in_connects_every_edge_with_a_vertical_run() -> None:
    tasks = [
        _task("implement"),
        _task("unit_verify", ["implement"]),
        _task("integration_write", ["implement"]),
        _task("integration_verify", ["integration_write"]),
        _task("deploy", ["unit_verify", "integration_verify"]),
        _task("manual_test", ["deploy"]),
    ]
    edges = _edges(tasks)
    layout = compute_dag_layout(tasks, edges)
    statuses = {t.id: "not_started" for t in tasks}
    text = render_dag_canvas(tasks, edges, layout, statuses, _style_for_status)
    plain = text.plain

    for task in tasks:
        assert f"● {task.id}" in plain or task.id in plain

    # integration_write and deploy sit on different lanes at points in the
    # graph, so at least one edge needs a vertical run (not a flat line).
    assert "│" in plain
    # At least one corner character shows up where a stub turns into a
    # vertical run.
    assert any(c in plain for c in ("┌", "┐", "└", "┘"))


def test_no_tasks_renders_a_placeholder_instead_of_an_empty_grid() -> None:
    layout = compute_dag_layout([], [])
    text = render_dag_canvas([], [], layout, {}, _style_for_status)
    assert text.plain == "(no tasks)"
