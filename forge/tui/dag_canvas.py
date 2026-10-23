"""
Turns a `DagLayout` (see `dag_layout.py`) plus live per-task status into a
character-grid rendering of the run's DAG: a box per task (status glyph +
id), connected by box-drawing/Unicode line characters that trace the real
dependency edges. This is deliberately a 2D character buffer, not an
SVG/pixel approach — it builds a grid of styled cells and only converts to
a Rich `Text` (one multi-line renderable) at the very end, for a plain
`Static` widget to display (`forge/tui/screens.py`'s `LiveRunScreen`
scrolls it in a `VerticalScroll` rather than this module trying to
zoom/pan/clip anything itself — that's explicitly a later phase).

Routing is intentionally simple, not pixel-precise: every task's lane maps
to the same row band regardless of rank, so most edges are a flat
horizontal run; when an edge's parent and child land in different lanes
(a real fan-out/fan-in), it gets a stub out of the parent, one vertical
run in the gap column right after the parent to reach the child's row,
then a stub into the child. Every write to the grid is "only if this cell
is still blank", so a multi-rank edge that flies over an intervening
rank's box simply stops drawing across that box's footprint and resumes in
the next gap column — it does not overwrite someone else's box.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from rich.text import Text

from forge.models.graph import EdgeSpec, TaskSpec
from forge.tui.dag_layout import DagLayout

BOX_WIDTH = 24
BOX_HEIGHT = 3
GAP_WIDTH = 6
LANE_GAP = 1

_STATUS_GLYPH = "●"  # "●"


@dataclass
class _Cell:
    char: str = " "
    style: str | None = None


@dataclass
class _Grid:
    width: int
    height: int
    cells: list[list[_Cell]] = field(init=False)

    def __post_init__(self) -> None:
        self.cells = [[_Cell() for _ in range(self.width)] for _ in range(self.height)]

    def set(self, row: int, col: int, char: str, style: str | None, *, force: bool = False) -> None:
        if not (0 <= row < self.height and 0 <= col < self.width):
            return
        cell = self.cells[row][col]
        if force or cell.char == " ":
            cell.char = char
            cell.style = style

    def hline(self, row: int, col_start: int, col_end: int, style: str | None) -> None:
        for col in range(min(col_start, col_end), max(col_start, col_end) + 1):
            self.set(row, col, "─", style)

    def vline(self, row_start: int, row_end: int, col: int, style: str | None) -> None:
        for row in range(min(row_start, row_end), max(row_start, row_end) + 1):
            self.set(row, col, "│", style, force=True)

    def to_text(self) -> Text:
        text = Text()
        for row_index, row in enumerate(self.cells):
            if row_index:
                text.append("\n")
            for cell in row:
                text.append(cell.char, style=cell.style or "")
        return text


def _col_for_rank(rank: int) -> int:
    return rank * (BOX_WIDTH + GAP_WIDTH)


def _row_for_lane(lane: int) -> int:
    return lane * (BOX_HEIGHT + LANE_GAP)


def canvas_size(layout: DagLayout) -> tuple[int, int]:
    """(width, height) in characters this layout will render at."""
    if layout.num_ranks == 0:
        return (0, 0)
    width = layout.num_ranks * BOX_WIDTH + (layout.num_ranks - 1) * GAP_WIDTH
    max_lanes = max(layout.lane_counts.values(), default=0)
    height = max_lanes * BOX_HEIGHT + max(0, max_lanes - 1) * LANE_GAP
    return (width, height)


def _box_label(task: TaskSpec, status: str) -> str:
    inner_width = BOX_WIDTH - 2
    label = f"{_STATUS_GLYPH} {task.id}"
    if len(label) > inner_width:
        label = label[: inner_width - 1] + "…"
    pad = inner_width - len(label)
    left = pad // 2
    right = pad - left
    return (" " * left) + label + (" " * right)


def _draw_box(grid: _Grid, row0: int, col0: int, task: TaskSpec, status: str, style: str) -> None:
    top = "┌" + ("─" * (BOX_WIDTH - 2)) + "┐"
    mid = "│" + _box_label(task, status) + "│"
    bot = "└" + ("─" * (BOX_WIDTH - 2)) + "┘"
    for col_offset, char in enumerate(top):
        grid.set(row0, col0 + col_offset, char, style, force=True)
    for col_offset, char in enumerate(mid):
        grid.set(row0 + 1, col0 + col_offset, char, style, force=True)
    for col_offset, char in enumerate(bot):
        grid.set(row0 + 2, col0 + col_offset, char, style, force=True)


def _draw_edge(
    grid: _Grid,
    *,
    parent_rank: int,
    parent_lane: int,
    child_rank: int,
    child_lane: int,
    style: str | None,
) -> None:
    row_p = _row_for_lane(parent_lane) + 1  # the box's middle (content) row
    row_c = _row_for_lane(child_lane) + 1
    col_from = _col_for_rank(parent_rank) + BOX_WIDTH  # first blank col after parent's box
    col_to = _col_for_rank(child_rank) - 1  # last blank col before child's box

    if row_p == row_c:
        grid.hline(row_p, col_from, col_to, style)
        return

    routing_col = col_from
    top_row, bottom_row = sorted((row_p, row_c))
    grid.hline(row_p, col_from, routing_col, style)
    grid.vline(top_row, bottom_row, routing_col, style)
    grid.hline(row_c, routing_col, col_to, style)
    # Corners overwrite whatever the straight lines above just drew at the
    # turning point, so they're drawn last and unconditionally.
    grid.set(row_p, routing_col, "┐" if row_p == top_row else "┘", style, force=True)
    grid.set(row_c, routing_col, "└" if row_c == bottom_row else "┌", style, force=True)


def render_dag_canvas(
    tasks: list[TaskSpec],
    edges: list[EdgeSpec],
    layout: DagLayout,
    statuses: Mapping[str, str],
    style_for_status: Callable[[str], str],
) -> Text:
    """
    `statuses` maps task id -> status string, same source `LiveRunScreen`
    already derives via `forge.cli._task_status`. `style_for_status` is
    `screens._status_style` partially applied over the active theme's
    `get_css_variables()` — this module never picks colors itself.
    """
    width, height = canvas_size(layout)
    if width == 0 or height == 0:
        return Text("(no tasks)")
    grid = _Grid(width=width, height=height)

    tasks_by_id = {t.id: t for t in tasks}
    for task in tasks:
        position = layout.positions.get(task.id)
        if position is None:
            continue
        status = statuses.get(task.id, "not_started")
        _draw_box(
            grid,
            _row_for_lane(position.lane),
            _col_for_rank(position.rank),
            task,
            status,
            style_for_status(status),
        )

    for edge in edges:
        if edge.relation != "dependency":
            continue
        parent = layout.positions.get(edge.from_task)
        child = layout.positions.get(edge.to_task)
        if parent is None or child is None or edge.from_task not in tasks_by_id:
            continue
        _draw_edge(
            grid,
            parent_rank=parent.rank,
            parent_lane=parent.lane,
            child_rank=child.rank,
            child_lane=child.lane,
            style=None,
        )

    return grid.to_text()
