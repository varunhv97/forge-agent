"""
Every screen (and modal) the Forge TUI shows. These are the UI layer: they
read `self.app.store` (a `Store` opened strictly for reads — see
`forge/tui/app.py`'s docstring and `forge/tui/runner.py`) to render
run/attempt/evidence/trace data, and otherwise only call back into
`ForgeApp`'s own methods (`list_modes`, `start_new_run`, `open_run`) rather
than touching the controller, a registry, or a write-capable `Store`
themselves. Nothing here ever mutates run state directly — that's the
controller's job alone (`forge/controller/scheduler.py`, driven from
`forge/tui/runner.py`'s background thread), per this project's core
invariant.

Status coloring reuses the active Textual theme's own semantic variables
(`$success`/`$warning`/`$error`, resolved via `App.get_css_variables()`)
rather than hand-rolled hex codes, so PASSED/RUNNING/FAILED-style colors
stay correct whether the light or dark theme is active.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    RichLog,
    Static,
)
from textual.widgets.option_list import Option

from forge.cli import (
    _latest_attempts_by_task,
    _task_status,
    build_inspect_report,
    build_trace_report,
)
from forge.controller.store import NotFoundError
from forge.controller.validator import GraphValidationError
from forge.models.attempt import Attempt
from forge.modes.custom_dag import CustomDagError, DagTemplateInfo
from forge.tui.dag_canvas import DagDetail, canvas_size, render_dag_canvas, visible_rank_range
from forge.tui.dag_layout import DagLayout, compute_dag_layout

if TYPE_CHECKING:
    from forge.tui.app import ForgeApp

_GREEN_STATUSES = {"passed", "completed_verified"}
_AMBER_STATUSES = {"running", "active", "pending"}
_RED_STATUSES = {"failed", "failed_exhausted", "blocked_awaiting_input", "blocked", "cancelled"}


def _get_app(screen: Screen[Any]) -> ForgeApp:
    # Screen[T] is invariant in T, so a helper shared by screens with
    # different result types (Screen[None], ModalScreen[bool], ...) has to
    # accept Any here rather than a fixed type parameter.
    return cast("ForgeApp", screen.app)


def _status_style(colors: Mapping[str, str], status: str) -> str:
    if status in _GREEN_STATUSES:
        return f"bold {colors.get('success', 'green')}"
    if status in _AMBER_STATUSES:
        return f"bold {colors.get('warning', 'yellow')}"
    if status in _RED_STATUSES:
        return f"bold {colors.get('error', 'red')}"
    return ""


def _fmt_dt(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _fmt_elapsed(delta: timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _task_duration(attempt: Attempt | None) -> str:
    """A task's elapsed wall time for the DAG canvas's "full"-detail
    duration line: its latest attempt's `started_at` through `finished_at`
    (or now, if it's still running). "-" if it has no attempt yet, or one
    that hasn't actually started (dispatched but not yet running)."""
    if attempt is None or attempt.started_at is None:
        return "-"
    end = attempt.finished_at or datetime.now(UTC)
    return _fmt_elapsed(end - attempt.started_at)


def _minimap_bar(offset: int, viewport: int, total: int, width: int = 24) -> str:
    """A compact proportional "you are here" bar: `width` cells, with the
    segment currently in view filled in. Driven entirely by the caller's
    actual scroll offset/viewport/content-size numbers — never a static
    placeholder. `total` is widened to at least `viewport` so a graph that
    already fits entirely on screen renders as one fully-filled bar rather
    than dividing by a content size smaller than what's visible."""
    total = max(total, viewport, 1)
    if viewport >= total:
        return "[" + ("▓" * width) + "]"
    start_frac = min(max(offset, 0), total) / total
    visible_frac = min(1.0, viewport / total)
    start_cell = min(round(start_frac * width), width - 1)
    visible_cells = max(1, round(visible_frac * width))
    end_cell = min(width, start_cell + visible_cells)
    cells = ["▓" if start_cell <= i < end_cell else "·" for i in range(width)]
    return "[" + "".join(cells) + "]"


# -- modals -------------------------------------------------------------------


class ConfirmModal(ModalScreen[bool]):
    """
    A generic yes/no confirmation. Default focus is always on the "no"
    button — every caller of this (the git-init prompt, the deploy
    authorization gate) needs an explicit affirmative action, never a
    default-to-yes a stray Enter key press could trigger.
    """

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal #confirm-dialog {
        width: 70%;
        max-width: 90;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    ConfirmModal #confirm-buttons {
        margin-top: 1;
        align: right middle;
    }
    ConfirmModal #confirm-buttons Button {
        margin-left: 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "decline", "Cancel"),
        Binding("y", "confirm", "Yes"),
        Binding("n", "decline", "No"),
    ]

    def __init__(self, message: str, *, yes_label: str = "Yes", no_label: str = "No") -> None:
        super().__init__()
        self._message = message
        self._yes_label = yes_label
        self._no_label = no_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(self._message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Button(self._no_label, id="no-button")
                yield Button(self._yes_label, id="yes-button", variant="warning")

    def on_mount(self) -> None:
        self.query_one("#no-button", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes-button")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_decline(self) -> None:
        self.dismiss(False)


# -- dashboard ------------------------------------------------------------------


class DashboardScreen(Screen[None]):
    """
    Home screen: every run from `Store.list_runs()`. Pushed once at
    startup and never popped for the app's lifetime — every other screen
    is pushed on top of it and popped back to it.
    """

    DEFAULT_CSS = """
    DashboardScreen #runs-table {
        height: 1fr;
    }
    DashboardScreen #empty-state {
        content-align: center middle;
        height: 1fr;
        color: $text-muted;
    }
    """
    # "enter" isn't bound here: DataTable's own BINDINGS already claim it
    # (action_select_cursor, which posts RowSelected) while the table has
    # focus, so "open" is wired through on_data_table_row_selected below
    # instead of a same-named Binding that would never actually fire.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("n", "new_session", "New session"),
        Binding("r", "resume_selected", "Resume"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._row_run_ids: list[str] = []
        self._auto_opened_new_session = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="runs-table", cursor_type="row")
        yield Static("no runs yet", id="empty-state")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#runs-table", DataTable)
        table.add_columns("RUN ID", "MODE", "OBJECTIVE", "STATUS", "UPDATED")
        table.focus()
        self.query_one("#empty-state", Static).display = False
        self.refresh_runs()
        self.set_interval(1.5, self.refresh_runs)

    def refresh_runs(self) -> None:
        app = _get_app(self)
        runs = app.store.list_runs()
        colors = app.get_css_variables()

        table = self.query_one("#runs-table", DataTable)
        table.clear()
        self._row_run_ids = []
        for run in runs:
            try:
                request = app.store.load_request(run.request_id)
                objective = request.objective_text
                mode_label = request.template_name or request.mode.value
            except NotFoundError:
                objective = "(request not found)"
                mode_label = "-"
            table.add_row(
                run.id,
                mode_label,
                objective,
                Text(run.status.value, style=_status_style(colors, run.status.value)),
                _fmt_dt(run.updated_at),
                key=run.id,
            )
            self._row_run_ids.append(run.id)

        empty = not runs
        table.display = not empty
        self.query_one("#empty-state", Static).display = empty
        if empty and not self._auto_opened_new_session:
            self._auto_opened_new_session = True
            self.app.push_screen(NewSessionScreen())

    def _selected_run_id(self) -> str | None:
        table = self.query_one("#runs-table", DataTable)
        if not self._row_run_ids or table.cursor_row is None:
            return None
        try:
            return self._row_run_ids[table.cursor_row]
        except IndexError:
            return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        run_id = event.row_key.value
        if run_id is not None:
            _get_app(self).open_run(str(run_id), force_resume=False)

    def action_new_session(self) -> None:
        self.app.push_screen(NewSessionScreen())

    def action_resume_selected(self) -> None:
        run_id = self._selected_run_id()
        if run_id is not None:
            _get_app(self).open_run(run_id, force_resume=True)

    def action_quit_app(self) -> None:
        self.app.exit()


# -- new session ------------------------------------------------------------------


class NewSessionScreen(Screen[None]):
    """
    Pick Feature Blast or a custom DAG template, fill in its declared
    inputs, then confirm deploy authorization before building the exact
    same `(Request, GraphVersion)` draft `forge run` builds and handing it
    to `compile_and_validate` + `start_run`.
    """

    DEFAULT_CSS = """
    NewSessionScreen #mode-picker, NewSessionScreen #session-form {
        padding: 1 2;
    }
    NewSessionScreen .section-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    NewSessionScreen #mode-list {
        height: 1fr;
    }
    NewSessionScreen #form-fields Label {
        margin-top: 1;
    }
    NewSessionScreen #continue-button {
        margin-top: 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    _FEATURE_BLAST_OPTION_ID = "feature-blast"
    _CUSTOM_PREFIX = "custom:"

    def __init__(self) -> None:
        super().__init__()
        self._templates: dict[str, DagTemplateInfo] = {}
        self._selected_mode: str | None = None
        self._custom_template: DagTemplateInfo | None = None
        self._field_names: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="mode-picker"):
            yield Static("Choose what to run:", classes="section-title")
            yield OptionList(id="mode-list")
        with Vertical(id="session-form"):
            yield Static("", id="form-title", classes="section-title")
            yield Vertical(id="form-fields")
            yield Button("Continue", id="continue-button", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#session-form").display = False
        option_list = self.query_one("#mode-list", OptionList)
        option_list.add_option(
            Option(
                "Feature Blast — build, test, deploy, and verify one bounded feature",
                id=self._FEATURE_BLAST_OPTION_ID,
            )
        )
        for template in _get_app(self).list_modes():
            self._templates[template.name] = template
            description = template.description or "(no description)"
            option_list.add_option(
                Option(
                    f"{template.name} — {description}", id=f"{self._CUSTOM_PREFIX}{template.name}"
                )
            )
        option_list.highlighted = 0  # OptionList starts with nothing highlighted otherwise
        option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option_id
        if option_id is None:
            return
        if option_id == self._FEATURE_BLAST_OPTION_ID:
            self._selected_mode = "feature-blast"
            self._custom_template = None
            self._build_form(
                ["objective", "journey"],
                {
                    "objective": "Objective — what should implement_exec build?",
                    "journey": "Journey to verify once deployed",
                },
            )
            title = "Feature Blast"
        elif option_id.startswith(self._CUSTOM_PREFIX):
            name = option_id.removeprefix(self._CUSTOM_PREFIX)
            template = self._templates[name]
            self._selected_mode = "custom"
            self._custom_template = template
            self._build_form(
                [field.name for field in template.inputs],
                {field.name: field.prompt for field in template.inputs},
            )
            title = f"Custom DAG: {template.name}"
        else:
            return

        self.query_one("#form-title", Static).update(title)
        self.query_one("#mode-picker").display = False
        self.query_one("#session-form").display = True
        if self._field_names:
            self.query_one(f"#field-{self._field_names[0]}", Input).focus()
        else:
            self.query_one("#continue-button", Button).focus()

    def _build_form(self, field_names: list[str], labels: Mapping[str, str]) -> None:
        self._field_names = field_names
        container = self.query_one("#form-fields", Vertical)
        container.remove_children()
        for name in field_names:
            container.mount(Label(labels.get(name, name)))
            container.mount(Input(id=f"field-{name}"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "continue-button":
            self._submit_form()

    def _submit_form(self) -> None:
        values: dict[str, str] = {}
        for name in self._field_names:
            field = self.query_one(f"#field-{name}", Input)
            value = field.value.strip()
            if not value:
                self.app.notify(f"'{name}' is required", severity="warning")
                field.focus()
                return
            values[name] = value
        self.run_worker(self._confirm_and_start(values), exclusive=True)

    async def _confirm_and_start(self, values: dict[str, str]) -> None:
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(
                "Authorize this run to deploy?\n\nOnce started, this run is fully "
                "autonomous through to a real deploy — there is no further "
                "per-deploy confirmation.",
                yes_label="Authorize deploy",
                no_label="Cancel",
            )
        )
        if not confirmed:
            return

        app = _get_app(self)
        try:
            run_id = app.start_new_run(
                self._selected_mode or "feature-blast", self._custom_template, values
            )
        except (CustomDagError, GraphValidationError) as exc:
            self.app.notify(str(exc), severity="error")
            return

        self.app.pop_screen()
        app.open_run(run_id, force_resume=False)

    def action_cancel(self) -> None:
        self.app.pop_screen()


# -- live run ------------------------------------------------------------------


class LiveRunScreen(Screen[None]):
    """
    The live view for one run. Polls `self.app.store` (read-only) on a
    timer — the run itself progresses on its own `RunWorker` background
    thread (`forge/tui/runner.py`), started by `ForgeApp.open_run` before
    this screen is pushed. `d` pops back to the dashboard without touching
    that thread at all; the run keeps going.

    Two renderings of the exact same poll tick's data, toggled with `g`:
    the original flat task list + trace tail (`#list-view`), and a DAG
    canvas (`#graph-view`, `forge/tui/dag_canvas.py`) laid out once per
    graph version (`forge/tui/dag_layout.py` — the shape doesn't change
    mid-run) and re-rendered every tick with each task's current status.
    Both containers are kept up to date every tick regardless of which is
    visible, so toggling never shows stale content.

    The graph view additionally supports zoom (level of detail — see
    `forge/tui/dag_canvas.py`'s `DagDetail`) and pan (`#graph-view`'s own
    real scroll position), plus a minimap/overview readout
    (`#graph-minimap`) answering "where am I" from that same scroll
    position. All three are only active — and only shown in the footer —
    while the graph view itself is visible (`check_action`, below).
    """

    # Panning moves by a fixed character step per keypress rather than the
    # single-cell default `ScrollableContainer` bindings give you — small
    # enough to feel incremental, large enough that a `GAP_WIDTH`-ish jump
    # doesn't take a dozen presses.
    _PAN_STEP_X = 8
    _PAN_STEP_Y = 3
    _MINIMAP_BAR_WIDTH = 24

    _GRAPH_ONLY_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {
            "pan_left",
            "pan_right",
            "pan_up",
            "pan_down",
            "zoom_full",
            "zoom_compact",
            "zoom_reset",
        }
    )

    DEFAULT_CSS = """
    LiveRunScreen #run-header {
        padding: 0 1;
        height: auto;
        border-bottom: solid $primary;
    }
    LiveRunScreen #list-view {
        height: 1fr;
    }
    LiveRunScreen #tasks-table {
        width: 45%;
    }
    LiveRunScreen #trace-log {
        width: 55%;
        border-left: solid $panel;
    }
    LiveRunScreen #graph-view {
        height: 1fr;
        overflow-x: auto;
        overflow-y: auto;
    }
    LiveRunScreen #dag-canvas {
        padding: 1 2;
        width: auto;
    }
    LiveRunScreen #graph-minimap {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        border-top: solid $panel;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("g", "toggle_graph_view", "Graph view"),
        Binding("d", "back_to_dashboard", "Dashboard (run keeps going)"),
        Binding("q", "quit_app", "Quit"),
        Binding("up", "pan_up", "Pan", priority=True, show=False),
        Binding("down", "pan_down", "Pan", priority=True, show=False),
        Binding("left", "pan_left", "Pan", priority=True, show=False),
        Binding("right", "pan_right", "Pan", priority=True, show=False),
        Binding("+", "zoom_full", "More detail"),
        Binding("-", "zoom_compact", "Less detail (fit wider)"),
        Binding("0", "zoom_reset", "Reset zoom/pan"),
    ]

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id
        self._trace_lines_written = 0
        self._showing_graph = False
        self._layout: DagLayout | None = None
        self._layout_graph_version_id: str | None = None
        self._detail: DagDetail = "full"

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="run-header")
        with Horizontal(id="list-view"):
            yield DataTable(id="tasks-table", cursor_type="row")
            yield RichLog(id="trace-log", wrap=True, highlight=False, markup=False)
        with VerticalScroll(id="graph-view"):
            yield Static("", id="dag-canvas")
        yield Static("", id="graph-minimap")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tasks-table", DataTable)
        table.add_columns("TASK", "NODE TYPE", "STATUS", "ATTEMPT")
        table.focus()
        self.query_one("#graph-view").display = False
        self.query_one("#graph-minimap").display = False
        self.refresh_run()
        self.set_interval(0.75, self.refresh_run)

    def refresh_run(self) -> None:
        app = _get_app(self)
        try:
            run = app.store.load_run(self.run_id)
        except NotFoundError:
            return
        graph = app.store.load_graph_version(run.active_graph_version_id)
        attempts = app.store.load_attempts(self.run_id)
        latest_by_task = _latest_attempts_by_task(attempts)
        colors = app.get_css_variables()
        statuses = {task.id: _task_status(task, latest_by_task) for task in graph.tasks}

        elapsed = datetime.now(UTC) - run.created_at
        header_text = Text()
        header_text.append(f"Run {run.id}   ", style="bold")
        header_text.append(run.status.value, style=_status_style(colors, run.status.value))
        header_text.append(f"   elapsed {_fmt_elapsed(elapsed)}")
        if run.blocker_message:
            header_text.append(
                f"\nblocked on: {run.blocker_message}", style=_status_style(colors, "failed")
            )
        self.query_one("#run-header", Static).update(header_text)

        table = self.query_one("#tasks-table", DataTable)
        table.clear()
        for task in graph.tasks:
            status = statuses[task.id]
            attempt = latest_by_task.get(task.id)
            attempt_label = f"#{attempt.attempt_number}" if attempt is not None else "-"
            table.add_row(
                task.id,
                f"{task.node_kind.value}/{task.node_type}",
                Text(status, style=_status_style(colors, status)),
                attempt_label,
            )

        report = build_trace_report(app.store, self.run_id)
        lines = report.split("\n")
        if len(lines) > self._trace_lines_written:
            log = self.query_one("#trace-log", RichLog)
            for line in lines[self._trace_lines_written :]:
                log.write(line)
            self._trace_lines_written = len(lines)

        if self._layout is None or self._layout_graph_version_id != graph.id:
            self._layout = compute_dag_layout(graph.tasks, graph.edges)
            self._layout_graph_version_id = graph.id
        durations = {
            task_id: _task_duration(attempt) for task_id, attempt in latest_by_task.items()
        }
        canvas_text = render_dag_canvas(
            graph.tasks,
            graph.edges,
            self._layout,
            statuses,
            lambda status: _status_style(colors, status),
            detail=self._detail,
            durations=durations,
        )
        self.query_one("#dag-canvas", Static).update(canvas_text)
        self._update_minimap()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return not (action in self._GRAPH_ONLY_ACTIONS and not self._showing_graph)

    def action_toggle_graph_view(self) -> None:
        self._showing_graph = not self._showing_graph
        self.query_one("#list-view").display = not self._showing_graph
        self.query_one("#graph-view").display = self._showing_graph
        self.query_one("#graph-minimap").display = self._showing_graph
        if self._showing_graph:
            self.query_one("#graph-view").focus()
            # The container's `display` flip above hasn't been laid out
            # yet at this point in the same call stack — `container_size`
            # would still read its old (hidden, zero) value — so defer the
            # minimap's first real read of it to just after the next
            # refresh rather than showing a stale/empty bar until the next
            # 0.75s poll tick happens to fire.
            self.call_after_refresh(self._update_minimap)
        else:
            self.query_one("#tasks-table", DataTable).focus()
        self.refresh_bindings()  # footer hints for the now-(in)active graph-only actions

    def action_back_to_dashboard(self) -> None:
        self.app.pop_screen()

    def action_quit_app(self) -> None:
        self.app.exit()

    # -- zoom ---------------------------------------------------------------

    def action_zoom_full(self) -> None:
        self._detail = "full"
        self.refresh_run()

    def action_zoom_compact(self) -> None:
        self._detail = "compact"
        self.refresh_run()

    def action_zoom_reset(self) -> None:
        """Snap pan back to the origin, and pick whichever detail level
        gives the graph the best chance of fitting the current terminal
        width without horizontal scrolling — not always possible for a
        very wide graph, which is an acceptable terminal limitation."""
        container = self.query_one("#graph-view")
        if self._layout is not None:
            viewport_width = container.container_size.width or container.size.width
            full_width, _ = canvas_size(self._layout, "full")
            self._detail = "full" if full_width <= viewport_width else "compact"
        else:
            self._detail = "full"
        container.scroll_to(x=0, y=0, animate=False, immediate=True, force=True)
        self.refresh_run()

    # -- pan ------------------------------------------------------------------

    def action_pan_left(self) -> None:
        self._pan(-self._PAN_STEP_X, 0)

    def action_pan_right(self) -> None:
        self._pan(self._PAN_STEP_X, 0)

    def action_pan_up(self) -> None:
        self._pan(0, -self._PAN_STEP_Y)

    def action_pan_down(self) -> None:
        self._pan(0, self._PAN_STEP_Y)

    def _pan(self, dx: int, dy: int) -> None:
        container = self.query_one("#graph-view")
        container.scroll_relative(x=dx, y=dy, animate=False, immediate=True, force=True)
        self._update_minimap()

    # -- minimap/overview ------------------------------------------------------

    def _update_minimap(self) -> None:
        """Renders `#graph-minimap` from `#graph-view`'s *actual* current
        scroll position and content size — never a static placeholder.
        Safe to call before the container has a real size yet (e.g. the
        very first mount tick): a zero-size viewport just shows an empty
        bar rather than raising."""
        minimap = self.query_one("#graph-minimap", Static)
        if not self._showing_graph or self._layout is None:
            return
        container = self.query_one("#graph-view")
        canvas_width, canvas_height = canvas_size(self._layout, self._detail)
        viewport_width = container.container_size.width or container.size.width
        viewport_height = container.container_size.height or container.size.height
        if viewport_width <= 0:
            minimap.update("")
            return

        scroll_x = int(container.scroll_x)
        scroll_y = int(container.scroll_y)
        bar = _minimap_bar(scroll_x, viewport_width, canvas_width, width=self._MINIMAP_BAR_WIDTH)
        first_rank, last_rank = visible_rank_range(
            self._layout, self._detail, scroll_x, viewport_width
        )
        text = f"graph {bar} ranks {first_rank + 1}-{last_rank + 1}/{self._layout.num_ranks}"
        if canvas_height > max(viewport_height, 1):
            v_bar = _minimap_bar(
                scroll_y, viewport_height, canvas_height, width=self._MINIMAP_BAR_WIDTH
            )
            text += f"   rows {v_bar}"
        minimap.update(text)


# -- run summary (terminal runs, opened from the dashboard) ----------------------


class RunSummaryScreen(Screen[None]):
    """
    A static, scrollable `build_inspect_report` snapshot for a run that's
    already terminal (not ACTIVE/BLOCKED_AWAITING_INPUT) — reuses the exact
    same report `forge inspect` prints, never re-derives it.
    """

    DEFAULT_CSS = """
    RunSummaryScreen #summary-scroll {
        padding: 1 2;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "Back"),
        Binding("d", "back", "Back"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="summary-scroll"):
            yield Static("", id="summary-text")
        yield Footer()

    def on_mount(self) -> None:
        app = _get_app(self)
        try:
            report = build_inspect_report(app.store, app.node_registry, self.run_id)
        except NotFoundError as exc:
            report = str(exc)
        self.query_one("#summary-text", Static).update(report)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit_app(self) -> None:
        self.app.exit()
