"""
Unit tests for `forge/tui/dag_canvas.py`'s `render_dag_canvas` — checked on
the rendered plain-text grid (box borders, ids, status glyphs/words, and
the box-drawing line characters joining them), not pixel-exact output. Also
covers `canvas_size`/`visible_rank_range`, the pure geometry helpers the
live-run screen's zoom/pan/minimap wiring depends on.
"""

from __future__ import annotations

from forge.models.common import NodeKind
from forge.models.graph import EdgeSpec, TaskSpec
from forge.tui.dag_canvas import canvas_size, render_dag_canvas, visible_rank_range
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
    assert "● passed" in plain  # full detail: status glyph + status word
    assert "● running" in plain
    # Box borders present.
    assert "┌" in plain
    assert "┐" in plain
    assert "└" in plain
    assert "┘" in plain
    # A horizontal connector joins the two boxes on their shared (only)
    # lane, at the box's vertical center (content_row).
    middle_row = lines[2]
    assert "─" in middle_row

    # The "passed" status style was actually applied to task a's box.
    a_row = lines[2]
    a_col = a_row.index("● passed")
    offset = sum(len(line) + 1 for line in lines[:2]) + a_col
    style_at_a = next(span.style for span in text.spans if span.start <= offset < span.end)
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
        assert task.id in plain

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


# -- detail levels (zoom) -----------------------------------------------------


def test_compact_detail_is_narrower_and_drops_the_status_word() -> None:
    tasks = [_task("implement"), _task("unit_verify", ["implement"])]
    edges = _edges(tasks)
    layout = compute_dag_layout(tasks, edges)
    statuses = {"implement": "passed", "unit_verify": "running"}

    full_text = render_dag_canvas(tasks, edges, layout, statuses, _style_for_status, detail="full")
    compact_text = render_dag_canvas(
        tasks, edges, layout, statuses, _style_for_status, detail="compact"
    )

    full_width, full_height = canvas_size(layout, "full")
    compact_width, compact_height = canvas_size(layout, "compact")
    assert compact_width < full_width
    assert compact_height < full_height

    full_lines = full_text.plain.split("\n")
    compact_lines = compact_text.plain.split("\n")
    assert max(len(line) for line in full_lines) == full_width
    assert max(len(line) for line in compact_lines) == compact_width

    # Full detail shows the status word; compact shows only the dot + id.
    assert "● passed" in full_text.plain
    assert "● passed" not in compact_text.plain
    assert "● implement" in compact_text.plain


def test_full_detail_shows_a_duration_when_provided() -> None:
    tasks = [_task("implement")]
    layout = compute_dag_layout(tasks, [])
    text = render_dag_canvas(
        tasks,
        [],
        layout,
        {"implement": "running"},
        _style_for_status,
        detail="full",
        durations={"implement": "0:02:14"},
    )
    assert "0:02:14" in text.plain


def test_full_detail_duration_defaults_to_a_dash_when_not_supplied() -> None:
    tasks = [_task("implement")]
    layout = compute_dag_layout(tasks, [])
    text = render_dag_canvas(tasks, [], layout, {"implement": "running"}, _style_for_status)
    lines = text.plain.split("\n")
    assert lines[3].strip("│ ") == "-"


# -- visible_rank_range (minimap/overview geometry) ---------------------------


def test_visible_rank_range_covers_the_viewport_at_scroll_zero() -> None:
    tasks = [_task("a"), _task("b", ["a"]), _task("c", ["b"])]
    layout = compute_dag_layout(tasks, _edges(tasks))
    full_width, _ = canvas_size(layout, "full")

    first, last = visible_rank_range(layout, "full", scroll_x=0, viewport_width=full_width)
    assert (first, last) == (0, 2)

    # A narrow viewport at scroll 0 only reaches the first rank or two.
    first, last = visible_rank_range(layout, "full", scroll_x=0, viewport_width=24)
    assert first == 0
    assert last < 2


def test_visible_rank_range_shifts_with_scroll_and_clamps_to_the_last_rank() -> None:
    tasks = [_task("a"), _task("b", ["a"]), _task("c", ["b"])]
    layout = compute_dag_layout(tasks, _edges(tasks))

    first, last = visible_rank_range(layout, "full", scroll_x=30, viewport_width=20)
    assert first == 1
    assert last <= 2

    # Scrolled well past the content: clamps to the final rank, never raises
    # or returns an out-of-range index.
    first, last = visible_rank_range(layout, "full", scroll_x=10_000, viewport_width=20)
    assert (first, last) == (2, 2)


def test_visible_rank_range_on_an_empty_graph_is_a_degenerate_zero_range() -> None:
    layout = compute_dag_layout([], [])
    assert visible_rank_range(layout, "full", scroll_x=0, viewport_width=80) == (0, 0)
