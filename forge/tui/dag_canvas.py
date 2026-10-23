"""
Turns a `DagLayout` (see `dag_layout.py`) plus live per-task status into a
character-grid rendering of the run's DAG: a box per task, connected by
box-drawing/Unicode line characters that trace the real dependency edges.
This is deliberately a 2D character buffer, not an SVG/pixel approach — it
builds a grid of styled cells and only converts to a Rich `Text` (one
multi-line renderable) at the very end, for a plain `Static` widget to
display (`forge/tui/screens.py`'s `LiveRunScreen` scrolls/pans it in a
scrollable container rather than this module trying to clip anything
itself).

`detail` is the "zoom" level ("full" vs "compact" — see `DagDetail`): a
character grid has no literal pixel scaling, so zoom here means level of
detail, not magnification. `"full"` draws a taller box with the task id,
a status word, and a duration line; `"compact"` draws a narrower, shorter
box with just the id and a single status dot, trading per-node detail for
fitting more ranks across a terminal width. Only this module's own sizing
constants and per-box content change between the two — `dag_layout.py`'s
rank/lane computation stays purely topological and detail-agnostic.

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
from typing import Literal

from rich.text import Text

from forge.models.graph import EdgeSpec, TaskSpec
from forge.tui.dag_layout import DagLayout

DagDetail = Literal["full", "compact"]

_STATUS_GLYPH = "●"  # "●"


@dataclass(frozen=True)
class _DetailConfig:
    box_width: int
    box_height: int
    gap_width: int
    lane_gap: int

    @property
    def content_row(self) -> int:
        """Row offset (within a box) that edges connect to — the vertical
        middle of the box, so a connector always leaves/enters a box at
        its visual center regardless of how many content rows it has."""
        return self.box_height // 2


# "full": a 3-content-row box (id / status glyph+word / duration) inside a
# border — the richest rendering, closest to the original mockups.
_FULL = _DetailConfig(box_width=24, box_height=5, gap_width=6, lane_gap=1)
# "compact": a single-content-row box (status dot + id only), narrower and
# shorter — this is what actually makes a wide, many-rank graph fit in less
# horizontal space, at the cost of the status word/duration.
_COMPACT = _DetailConfig(box_width=14, box_height=3, gap_width=4, lane_gap=1)

_DETAIL_CONFIGS: dict[DagDetail, _DetailConfig] = {"full": _FULL, "compact": _COMPACT}


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


def _col_for_rank(rank: int, cfg: _DetailConfig) -> int:
    return rank * (cfg.box_width + cfg.gap_width)


def _row_for_lane(lane: int, cfg: _DetailConfig) -> int:
    return lane * (cfg.box_height + cfg.lane_gap)


def canvas_size(layout: DagLayout, detail: DagDetail = "full") -> tuple[int, int]:
    """(width, height) in characters this layout will render at, at the
    given detail level."""
    cfg = _DETAIL_CONFIGS[detail]
    if layout.num_ranks == 0:
        return (0, 0)
    width = layout.num_ranks * cfg.box_width + (layout.num_ranks - 1) * cfg.gap_width
    max_lanes = max(layout.lane_counts.values(), default=0)
    height = max_lanes * cfg.box_height + max(0, max_lanes - 1) * cfg.lane_gap
    return (width, height)


def visible_rank_range(
    layout: DagLayout, detail: DagDetail, scroll_x: int, viewport_width: int
) -> tuple[int, int]:
    """The (first, last) rank indices (0-indexed, inclusive) currently
    visible given a horizontal scroll offset and viewport width — what a
    minimap/overview readout needs to answer "where am I" without
    re-deriving box geometry itself."""
    if layout.num_ranks == 0:
        return (0, 0)
    cfg = _DETAIL_CONFIGS[detail]
    step = cfg.box_width + cfg.gap_width
    last_rank_idx = layout.num_ranks - 1
    first = min(max(scroll_x, 0) // step, last_rank_idx)
    last_col = max(scroll_x, 0) + max(viewport_width - 1, 0)
    last = min(last_col // step, last_rank_idx)
    return (first, max(first, last))


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + "…"


def _center(text: str, width: int) -> str:
    pad = max(0, width - len(text))
    left = pad // 2
    right = pad - left
    return (" " * left) + text + (" " * right)


def _compact_content_rows(task: TaskSpec, inner_width: int) -> list[str]:
    label = _truncate(f"{_STATUS_GLYPH} {task.id}", inner_width)
    return [_center(label, inner_width)]


def _full_content_rows(task: TaskSpec, status: str, duration: str, inner_width: int) -> list[str]:
    id_line = _center(_truncate(task.id, inner_width), inner_width)
    status_line = _center(_truncate(f"{_STATUS_GLYPH} {status}", inner_width), inner_width)
    duration_line = _center(_truncate(duration, inner_width), inner_width)
    return [id_line, status_line, duration_line]


def _draw_box(
    grid: _Grid, row0: int, col0: int, cfg: _DetailConfig, content_rows: list[str], style: str
) -> None:
    top = "┌" + ("─" * (cfg.box_width - 2)) + "┐"
    bot = "└" + ("─" * (cfg.box_width - 2)) + "┘"
    for col_offset, char in enumerate(top):
        grid.set(row0, col0 + col_offset, char, style, force=True)
    for row_offset, content in enumerate(content_rows):
        line = "│" + content + "│"
        for col_offset, char in enumerate(line):
            grid.set(row0 + 1 + row_offset, col0 + col_offset, char, style, force=True)
    for col_offset, char in enumerate(bot):
        grid.set(row0 + cfg.box_height - 1, col0 + col_offset, char, style, force=True)


def _draw_edge(
    grid: _Grid,
    *,
    parent_rank: int,
    parent_lane: int,
    child_rank: int,
    child_lane: int,
    cfg: _DetailConfig,
    style: str | None,
) -> None:
    row_p = _row_for_lane(parent_lane, cfg) + cfg.content_row
    row_c = _row_for_lane(child_lane, cfg) + cfg.content_row
    col_from = _col_for_rank(parent_rank, cfg) + cfg.box_width  # first blank col after parent
    col_to = _col_for_rank(child_rank, cfg) - 1  # last blank col before child

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
    *,
    detail: DagDetail = "full",
    durations: Mapping[str, str] | None = None,
    highlight_styles: Mapping[str, str] | None = None,
) -> Text:
    """
    `statuses` maps task id -> status string, same source `LiveRunScreen`
    already derives via `forge.cli._task_status`. `style_for_status` is
    `screens._status_style` partially applied over the active theme's
    `get_css_variables()` — this module never picks colors itself. A
    caller with no real run status yet (the DAG builder, authoring a
    template before any run exists) can pass a placeholder status the
    caller's own `style_for_status` doesn't recognize (e.g. "authoring")
    — this module already renders any unrecognized status neutrally,
    same as a real "not_started" task today, so no separate rendering
    path is needed for that case.

    `detail` picks the box's level of detail (see module docstring).
    `durations` optionally maps task id -> a short human string (e.g.
    "0:02:14") shown on a `"full"`-detail box's third content line; a task
    with no entry (or when `durations` is omitted entirely) shows "-".

    `highlight_styles` optionally maps task id -> a Rich style string that
    *replaces* (rather than blends with) that task's status-derived style
    for this render — the DAG builder's own "selected" / "connect source" /
    "connect target" cursor states, which aren't a run status at all. The
    caller still owns the actual color (built the same way
    `style_for_status` is, from the active theme's variables) — this
    module just applies whichever style string it's handed, same as it
    already does for `style_for_status`'s result.
    """
    cfg = _DETAIL_CONFIGS[detail]
    width, height = canvas_size(layout, detail)
    if width == 0 or height == 0:
        return Text("(no tasks)")
    grid = _Grid(width=width, height=height)
    durations = durations or {}
    highlight_styles = highlight_styles or {}
    inner_width = cfg.box_width - 2

    tasks_by_id = {t.id: t for t in tasks}
    for task in tasks:
        position = layout.positions.get(task.id)
        if position is None:
            continue
        status = statuses.get(task.id, "not_started")
        style = highlight_styles.get(task.id) or style_for_status(status)
        content_rows = (
            _compact_content_rows(task, inner_width)
            if detail == "compact"
            else _full_content_rows(task, status, durations.get(task.id, "-"), inner_width)
        )
        _draw_box(
            grid,
            _row_for_lane(position.lane, cfg),
            _col_for_rank(position.rank, cfg),
            cfg,
            content_rows,
            style,
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
            cfg=cfg,
            style=None,
        )

    return grid.to_text()
