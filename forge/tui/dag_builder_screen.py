"""
The visual, keyboard-driven DAG builder: a screen that lets a user place
and connect *existing* node types (from the real `NodeTypeRegistry`) into
a new graph shape, then saves it as `.forge/dags/<name>.json` in exactly
the schema `forge/modes/custom_dag.py`'s loader (`load_dag_definition`/
`build_custom_dag_request_and_graph`) already expects.

This is authoring-time, not a live run: there is no background worker, no
poll loop, and nothing else mutates this screen's state concurrently — so
unlike `LiveRunScreen` this module deliberately has no `set_interval`. The
canvas is recomputed synchronously, right after whatever keypress or modal
just changed the authored graph, from `forge/tui/dag_layout.py` (the exact
same `compute_dag_layout`/`DagLayoutError` the live-run view and
`compile_and_validate` itself are built on) and rendered with
`forge/tui/dag_canvas.py`'s `render_dag_canvas` — the same renderer, not a
second one. The only two things that renderer needed to gain for this
screen (see its own docstring) are an optional per-task `highlight_styles`
override (for "selected" / "connect source" / "connect target", none of
which are a run status) and the fact that an unrecognized status string —
this screen always passes "authoring" — already renders neutrally with no
further change needed.

Loading an existing `.forge/dags/*.json` template back into the builder
for further editing is explicitly out of scope for this pass; only
creating a new template is supported.
"""

from __future__ import annotations

import json
import re
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from pydantic import ValidationError
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from forge.models.graph import EdgeSpec, TaskSpec
from forge.models.request import AcceptanceCriterion
from forge.modes.custom_dag import (
    CustomDagError,
    DagAcceptanceCriterionSpec,
    DagDefinition,
    DagInputSpec,
    DagTaskSpec,
    load_dag_definition,
    resolve_permission_profile,
)
from forge.tui.chrome import Chrome
from forge.tui.dag_canvas import render_dag_canvas
from forge.tui.dag_layout import DagLayoutError, compute_dag_layout
from forge.tui.panel import LabeledPanel

if TYPE_CHECKING:
    from forge.tui.app import ForgeApp

# Every literal value AcceptanceCriterion.verification_method actually
# accepts, read straight off the pydantic model rather than hand-copied —
# so this screen can never *offer* a value forge/models/request.py
# doesn't (the OptionList in _VerificationMethodModal is built from this
# tuple, not a hardcoded list).
_VERIFICATION_METHODS: tuple[str, ...] = typing.get_args(
    AcceptanceCriterion.model_fields["verification_method"].annotation
)
# mypy needs a static Literal to type-check DagAcceptanceCriterionSpec's
# own `verification_method: Literal[...]` field — a runtime tuple like
# _VERIFICATION_METHODS above can't serve as a type annotation. This
# repeats the same four strings for that reason only; a real drift
# between this and the model (e.g. a 5th value added to one but not the
# other) would still show up immediately, either as a mypy error here or
# a pydantic ValidationError at save time (see action_save_template),
# never silently.
_VerificationMethodLiteral = typing.Literal[
    "automated_test", "manual_journey", "mechanical_check", "deployed_probe"
]

# Task ids, the template name (used verbatim as a filename), and
# acceptance-criterion ids all share this: safe to put in a filename and
# in error messages, no surprises. Input names are stricter (see below) —
# they're substituted into `{{ name }}` placeholders by
# forge/modes/custom_dag.py's own `_PLACEHOLDER_RE`, which only matches
# `\w+`.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_INPUT_NAME_RE = re.compile(r"^\w+$")


def _get_app(screen: Screen[Any]) -> ForgeApp:
    # Duplicated from forge/tui/screens.py's own identical helper rather
    # than imported, so this module has no import-time dependency on
    # screens.py (which imports *this* module to wire the one new
    # Dashboard binding) — importing it back would be a cycle.
    return cast("ForgeApp", screen.app)


# -- authoring-time task state -----------------------------------------------


@dataclass
class _TaskDraft:
    """
    One task as the builder screen holds it mid-authoring — intentionally
    not a `DagTaskSpec`/`TaskSpec` itself, since neither of those is meant
    to exist in a half-filled-in state (e.g. a task with no description
    yet). Converted to both at the points that actually need them: a
    `TaskSpec` for rendering (`_render_tasks_and_edges`), a `DagTaskSpec`
    for saving (`action_save_template`).
    """

    id: str
    node_type: str
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    acceptance_criterion_ids: list[str] = field(default_factory=list)
    permission_profile: str | None = None
    required: bool = True


@dataclass
class _InputDecl:
    name: str
    prompt: str


@dataclass
class _CriterionDecl:
    id: str
    description: str
    verification_method: str
    required: bool = True


@dataclass(frozen=True)
class _TaskEditResult:
    description: str
    acceptance_criterion_ids: list[str]
    permission_profile: str | None


# -- small input modals -------------------------------------------------------


class _TextFieldModal(ModalScreen[tuple[str, ...] | None]):
    """
    A generic "fill in N text fields, Save or Cancel" modal — every
    metadata/declaration prompt in this screen (task id, template
    name/description, a new input, a new acceptance criterion's id/
    description) is a thin instance of this, parameterized by its field
    labels/initial values and its own validator. Validation happens here,
    before dismissing, so an invalid entry re-shows the error and keeps
    the modal open rather than handing the caller something it has to
    re-validate (and re-prompt for) itself.
    """

    DEFAULT_CSS = """
    _TextFieldModal {
        align: center middle;
    }
    _TextFieldModal #field-dialog {
        width: 70%;
        max-width: 90;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    _TextFieldModal Label {
        margin-top: 1;
    }
    _TextFieldModal #field-buttons {
        margin-top: 1;
        align: right middle;
    }
    _TextFieldModal #field-buttons Button {
        margin-left: 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        title: str,
        labels: list[str],
        initial: list[str],
        validator: typing.Callable[[list[str]], str | None],
    ) -> None:
        super().__init__()
        self._title = title
        self._labels = labels
        self._initial = initial
        self._validator = validator

    def compose(self) -> ComposeResult:
        with Vertical(id="field-dialog"):
            yield Static(self._title, classes="section-title")
            for i, label in enumerate(self._labels):
                yield Label(label)
                yield Input(value=self._initial[i] if i < len(self._initial) else "", id=f"tf-{i}")
            with Horizontal(id="field-buttons"):
                yield Button("Cancel", id="cancel-button")
                yield Button("Save", id="save-button", variant="primary")

    def on_mount(self) -> None:
        if self._labels:
            self.query_one("#tf-0", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-button":
            self._try_submit()
        else:
            self.dismiss(None)

    def _try_submit(self) -> None:
        values = [self.query_one(f"#tf-{i}", Input).value.strip() for i in range(len(self._labels))]
        error = self._validator(values)
        if error:
            self.app.notify(error, severity="error")
            return
        self.dismiss(tuple(values))

    def action_cancel(self) -> None:
        self.dismiss(None)


class _VerificationMethodModal(ModalScreen[str | None]):
    """Picks one of `_VERIFICATION_METHODS` for a new acceptance
    criterion — an `OptionList`, not free text, so a typo can never
    produce a value `AcceptanceCriterion.verification_method` rejects."""

    DEFAULT_CSS = """
    _VerificationMethodModal {
        align: center middle;
    }
    _VerificationMethodModal #method-dialog {
        width: 60%;
        max-width: 70;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="method-dialog"):
            yield Static("verification_method", classes="section-title")
            option_list = OptionList(*(Option(m, id=m) for m in _VERIFICATION_METHODS))
            yield option_list

    def on_mount(self) -> None:
        option_list = self.query_one(OptionList)
        option_list.highlighted = 0
        option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class _TaskInspectorModal(ModalScreen[_TaskEditResult | None]):
    """
    The inspector for one placed task. `id` and `node type` are read-only
    (node type is derived from the registry, never free text — the task
    was placed from the palette in the first place); `depends_on` is also
    shown read-only here since connect/remove are this screen's only
    dependency-authoring actions. `description` and
    `acceptance_criterion_ids` are editable; the latter is validated
    against `valid_criterion_ids` (every id already declared in the
    ACCEPTANCE CRITERIA list) before this modal ever dismisses, so the
    caller never receives a dangling reference. `permission_profile` is
    free text here — validated against the real
    `PermissionProfileRegistry` at template-save time, not here, per this
    task's own spec.
    """

    DEFAULT_CSS = """
    _TaskInspectorModal {
        align: center middle;
    }
    _TaskInspectorModal #inspector-dialog {
        width: 80%;
        max-width: 100;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    _TaskInspectorModal Label {
        margin-top: 1;
    }
    _TaskInspectorModal #inspector-buttons {
        margin-top: 1;
        align: right middle;
    }
    _TaskInspectorModal #inspector-buttons Button {
        margin-left: 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self, task_draft: _TaskDraft, node_type_kind: str, valid_criterion_ids: set[str]
    ) -> None:
        # Named `_task_draft`, not `_task`: `textual.screen.Screen` already
        # has its own `_task` attribute (the screen's asyncio mount task),
        # and shadowing it with an unrelated `_TaskDraft` is exactly the
        # kind of same-name collision mypy caught here.
        super().__init__()
        self._task_draft = task_draft
        self._node_type_kind = node_type_kind
        self._valid_criterion_ids = valid_criterion_ids

    def compose(self) -> ComposeResult:
        task = self._task_draft
        with Vertical(id="inspector-dialog"):
            yield Static(f"Task: {task.id}", classes="section-title")
            yield Label(f"node_type: {task.node_type} ({self._node_type_kind})")
            yield Label(f"depends_on: {', '.join(task.depends_on) or '(none)'}")
            yield Label("description (may contain {{ input_name }} placeholders)")
            yield Input(value=task.description, id="field-description")
            yield Label("acceptance_criterion_ids (comma-separated)")
            yield Input(value=",".join(task.acceptance_criterion_ids), id="field-criteria")
            yield Label("permission_profile override (blank = node type default)")
            yield Input(value=task.permission_profile or "", id="field-profile")
            with Horizontal(id="inspector-buttons"):
                yield Button("Cancel", id="cancel-button")
                yield Button("Save", id="save-button", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#field-description", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-button":
            self._try_submit()
        else:
            self.dismiss(None)

    def _try_submit(self) -> None:
        description = self.query_one("#field-description", Input).value
        raw_criteria = self.query_one("#field-criteria", Input).value
        criterion_ids = [c.strip() for c in raw_criteria.split(",") if c.strip()]
        unknown = [c for c in criterion_ids if c not in self._valid_criterion_ids]
        if unknown:
            self.app.notify(
                f"unknown acceptance criterion id(s): {', '.join(unknown)} "
                "(add them above first)",
                severity="error",
            )
            return
        profile = self.query_one("#field-profile", Input).value.strip() or None
        self.dismiss(
            _TaskEditResult(
                description=description,
                acceptance_criterion_ids=criterion_ids,
                permission_profile=profile,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


# -- the builder screen itself -------------------------------------------------


class DagBuilderScreen(Screen[None]):
    """
    Create a new custom-DAG template by placing and connecting existing
    node types, then save it to `.forge/dags/<name>.json`. See the module
    docstring for what this screen deliberately does and doesn't do.
    """

    _CONNECT_ONLY_ACTIONS: ClassVar[frozenset[str]] = frozenset({"confirm_connect"})

    DEFAULT_CSS = """
    DagBuilderScreen #builder-meta {
        padding: 0 1;
        height: auto;
        border-bottom: solid $primary;
    }
    DagBuilderScreen .section-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    DagBuilderScreen #builder-body {
        height: 1fr;
    }
    DagBuilderScreen #builder-sidebar {
        width: 38%;
    }
    DagBuilderScreen #palette-panel {
        height: auto;
    }
    DagBuilderScreen #palette-list {
        height: 10;
    }
    DagBuilderScreen #inspector-panel {
        margin-top: 1;
        height: 1fr;
    }
    DagBuilderScreen #builder-inspector {
        height: 1fr;
    }
    DagBuilderScreen #canvas-panel {
        width: 62%;
        height: 1fr;
    }
    DagBuilderScreen #builder-graph-view {
        height: 1fr;
        overflow-x: auto;
        overflow-y: auto;
    }
    DagBuilderScreen #builder-canvas {
        padding: 1 2;
        width: auto;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("]", "select_next", "Next task"),
        Binding("[", "select_prev", "Prev task"),
        Binding("c", "connect", "Connect"),
        Binding("enter", "confirm_connect", "Confirm connect", priority=True),
        Binding("x", "remove_task", "Remove task"),
        Binding("i", "edit_task", "Edit task"),
        Binding("m", "edit_metadata", "Edit name/description"),
        Binding("f", "add_input", "Add input"),
        Binding("v", "add_criterion", "Add criterion"),
        Binding("s", "save_template", "Save template"),
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._template_name = ""
        self._template_description = ""
        self._inputs: list[_InputDecl] = []
        self._criteria: list[_CriterionDecl] = []
        self._tasks: list[_TaskDraft] = []
        self._selected_id: str | None = None
        self._connecting = False
        self._connect_source: str | None = None
        # Test/verification hook: the last path this screen actually
        # wrote a template to, so a test can round-trip exactly what the
        # UI produced without having to guess a path from `self._template_name`.
        self.last_saved_path: Path | None = None

    def compose(self) -> ComposeResult:
        yield Chrome(title="forge — build DAG")
        yield Static("", id="builder-meta")
        with Horizontal(id="builder-body"):
            with Vertical(id="builder-sidebar"):
                with LabeledPanel("NODE TYPE PALETTE", id="palette-panel"):
                    yield OptionList(id="palette-list")
                with LabeledPanel("INSPECTOR", id="inspector-panel"):
                    yield Static("", id="builder-inspector")
            with LabeledPanel("CANVAS", id="canvas-panel"), VerticalScroll(id="builder-graph-view"):
                yield Static("", id="builder-canvas")
        yield Footer()

    def on_mount(self) -> None:
        app = _get_app(self)
        palette = self.query_one("#palette-list", OptionList)
        for node_type in app.node_registry.all_types():
            palette.add_option(
                Option(f"{node_type.name} ({node_type.kind.value})", id=node_type.name)
            )
        if palette.option_count:
            palette.highlighted = 0
        palette.focus()
        self._refresh_summaries()
        self._refresh_canvas()

    # -- derived state --------------------------------------------------------

    def _task_by_id(self, task_id: str) -> _TaskDraft:
        for t in self._tasks:
            if t.id == task_id:
                return t
        raise KeyError(task_id)

    def _render_tasks_and_edges(self) -> tuple[list[TaskSpec], list[EdgeSpec]]:
        """
        Converts every `_TaskDraft` into a real `TaskSpec` (deriving
        `node_kind`/the effective `permission_profile` from the registry,
        exactly the way `forge/modes/custom_dag.py`'s own loader does —
        never free text) plus the `EdgeSpec`s its `depends_on` implies.
        This is what both the canvas renderer and the connect action's
        own cycle check are built from.
        """
        app = _get_app(self)
        tasks: list[TaskSpec] = []
        edges: list[EdgeSpec] = []
        for t in self._tasks:
            node_type = app.node_registry.get(t.node_type)
            tasks.append(
                TaskSpec(
                    id=t.id,
                    description=t.description,
                    node_kind=node_type.kind,
                    node_type=t.node_type,
                    depends_on=list(t.depends_on),
                    acceptance_criterion_ids=list(t.acceptance_criterion_ids),
                    permission_profile=t.permission_profile or node_type.permission_profile,
                    required=t.required,
                )
            )
            edges.extend(
                EdgeSpec(from_task=dep, to_task=t.id, relation="dependency") for dep in t.depends_on
            )
        return tasks, edges

    def _ordered_task_ids(self) -> list[str]:
        """Task ids in left-to-right, top-to-bottom canvas order (rank
        then lane) — what makes `]`/`[` navigation feel like moving across
        the picture actually on screen, not an arbitrary insertion order."""
        tasks, edges = self._render_tasks_and_edges()
        if not tasks:
            return []
        try:
            layout = compute_dag_layout(tasks, edges)
        except DagLayoutError:
            # Shouldn't happen: the connect action never lets a cycle get
            # committed in the first place. Fall back to insertion order
            # rather than crashing navigation if it somehow did.
            return [t.id for t in self._tasks]
        return [
            task_id
            for task_id, _position in sorted(
                layout.positions.items(), key=lambda kv: (kv[1].rank, kv[1].lane)
            )
        ]

    # -- rendering -------------------------------------------------------------

    def _highlight_styles(self) -> dict[str, str]:
        colors = self.app.get_css_variables()
        highlights: dict[str, str] = {}
        if self._connecting and self._connect_source is not None:
            highlights[self._connect_source] = f"bold {colors.get('warning', 'yellow')}"
            if self._selected_id is not None and self._selected_id != self._connect_source:
                highlights[self._selected_id] = f"bold {colors.get('success', 'green')}"
        elif self._selected_id is not None:
            highlights[self._selected_id] = f"bold {colors.get('accent', 'cyan')}"
        return highlights

    def _refresh_canvas(self) -> None:
        tasks, edges = self._render_tasks_and_edges()
        try:
            layout = compute_dag_layout(tasks, edges)
        except DagLayoutError as exc:
            # Unreachable in practice (see _ordered_task_ids), but this
            # screen stands on its own rather than assuming that.
            self.app.notify(f"internal: {exc}", severity="error")
            return
        statuses = {t.id: "authoring" for t in tasks}
        canvas_text = render_dag_canvas(
            tasks,
            edges,
            layout,
            statuses,
            lambda _status: "",
            highlight_styles=self._highlight_styles(),
        )
        self.query_one("#builder-canvas", Static).update(canvas_text)
        self._refresh_inspector_panel()

    def _refresh_summaries(self) -> None:
        header = Text()
        header.append(f"NAME: {self._template_name or '(unset — press m)'}\n", style="bold")
        header.append(f"DESCRIPTION: {self._template_description or '(none)'}\n")
        inputs_line = ", ".join(i.name for i in self._inputs) or "(none — press f)"
        header.append(f"INPUTS: {inputs_line}\n")
        criteria_line = ", ".join(c.id for c in self._criteria) or "(none — press v)"
        header.append(f"ACCEPTANCE CRITERIA: {criteria_line}")
        self.query_one("#builder-meta", Static).update(header)

    def _refresh_inspector_panel(self) -> None:
        panel = self.query_one("#builder-inspector", Static)
        if self._selected_id is None:
            panel.update("(no task selected — add one from the palette on the left)")
            return
        task = self._task_by_id(self._selected_id)
        node_type = _get_app(self).node_registry.get(task.node_type)
        text = Text()
        text.append(f"id: {task.id}\n", style="bold")
        text.append(f"node_type: {task.node_type} ({node_type.kind.value})\n")
        text.append(f"depends_on: {', '.join(task.depends_on) or '(none)'}\n")
        text.append(f"description: {task.description or '(none — press i to edit)'}\n")
        text.append(
            f"acceptance_criterion_ids: {', '.join(task.acceptance_criterion_ids) or '(none)'}\n"
        )
        profile_label = task.permission_profile or f"(default: {node_type.permission_profile})"
        text.append(f"permission_profile: {profile_label}")
        if self._connecting:
            text.append(
                f"\n\nCONNECT MODE — source: '{self._connect_source}'\n"
                "navigate with ]/[ to the dependent task, enter to confirm, escape to cancel"
            )
        panel.update(text)

    # -- node-type palette: add to canvas --------------------------------------

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        node_type_name = event.option_id
        if node_type_name is None:
            return
        self.run_worker(self._add_task_flow(node_type_name), exclusive=False)

    async def _add_task_flow(self, node_type_name: str) -> None:
        if self._connecting:
            self.app.notify("Finish or cancel connect mode first (escape)", severity="warning")
            return
        existing_ids = {t.id for t in self._tasks}

        def _validate(values: list[str]) -> str | None:
            task_id = values[0]
            if not task_id:
                return "task id is required"
            if not _IDENTIFIER_RE.match(task_id):
                return "task id may only contain letters, digits, '-', '_'"
            if task_id in existing_ids:
                return f"task id '{task_id}' is already in use"
            return None

        result = await self.app.push_screen_wait(
            _TextFieldModal(f"Add '{node_type_name}' task", ["task id"], [], _validate)
        )
        if result is None:
            return
        (task_id,) = result
        self._tasks.append(_TaskDraft(id=task_id, node_type=node_type_name))
        self._selected_id = task_id
        self._refresh_canvas()

    # -- selection navigation ---------------------------------------------------

    def action_select_next(self) -> None:
        self._move_selection(1)

    def action_select_prev(self) -> None:
        self._move_selection(-1)

    def _move_selection(self, step: int) -> None:
        ordered = self._ordered_task_ids()
        if not ordered:
            self._selected_id = None
            self._refresh_canvas()
            return
        if self._selected_id not in ordered:
            self._selected_id = ordered[0]
        else:
            index = ordered.index(self._selected_id)
            self._selected_id = ordered[(index + step) % len(ordered)]
        self._refresh_canvas()

    # -- connect / cycle rejection ----------------------------------------------

    def action_connect(self) -> None:
        if self._connecting:
            return
        if self._selected_id is None:
            self.app.notify("Select a task first", severity="warning")
            return
        self._connecting = True
        self._connect_source = self._selected_id
        self.refresh_bindings()
        self._refresh_canvas()
        self.app.notify(
            f"Connect mode: navigate to the task that should depend on "
            f"'{self._connect_source}', then press enter"
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return not (action in self._CONNECT_ONLY_ACTIONS and not self._connecting)

    def action_confirm_connect(self) -> None:
        if not self._connecting or self._connect_source is None:
            return
        source_id = self._connect_source
        target_id = self._selected_id
        if target_id is None or target_id == source_id:
            self.app.notify("Navigate to a different task to connect to", severity="warning")
            return
        target = self._task_by_id(target_id)
        if source_id in target.depends_on:
            self.app.notify(f"'{target_id}' already depends on '{source_id}'", severity="warning")
            self._end_connect_mode()
            return

        # Reuse dag_layout's own cycle detection rather than writing a
        # second one: build the prospective edge list (current edges plus
        # this one candidate) and see whether compute_dag_layout still
        # accepts it as a DAG.
        tasks, edges = self._render_tasks_and_edges()
        edges.append(EdgeSpec(from_task=source_id, to_task=target_id, relation="dependency"))
        try:
            compute_dag_layout(tasks, edges)
        except DagLayoutError:
            self.app.notify(
                f"Connecting '{source_id}' -> '{target_id}' would create a cycle; not applied.",
                severity="error",
            )
            self._end_connect_mode()
            return

        target.depends_on.append(source_id)
        self.app.notify(f"'{target_id}' now depends on '{source_id}'")
        self._selected_id = target_id
        self._end_connect_mode()

    def _end_connect_mode(self) -> None:
        self._connecting = False
        self._connect_source = None
        self.refresh_bindings()
        self._refresh_canvas()

    # -- remove ------------------------------------------------------------------

    def action_remove_task(self) -> None:
        if self._connecting:
            self.app.notify("Finish or cancel connect mode first (escape)", severity="warning")
            return
        if self._selected_id is None:
            self.app.notify("Select a task first", severity="warning")
            return
        removed_id = self._selected_id
        self._tasks = [t for t in self._tasks if t.id != removed_id]
        for t in self._tasks:
            if removed_id in t.depends_on:
                t.depends_on = [d for d in t.depends_on if d != removed_id]
        self._selected_id = self._tasks[0].id if self._tasks else None
        self.app.notify(f"Removed task '{removed_id}'")
        self._refresh_canvas()

    # -- task inspector ------------------------------------------------------------

    def action_edit_task(self) -> None:
        if self._connecting:
            self.app.notify("Finish or cancel connect mode first (escape)", severity="warning")
            return
        if self._selected_id is None:
            self.app.notify("Select a task first", severity="warning")
            return
        self.run_worker(self._edit_task_flow(self._selected_id), exclusive=False)

    async def _edit_task_flow(self, task_id: str) -> None:
        task = self._task_by_id(task_id)
        node_type = _get_app(self).node_registry.get(task.node_type)
        valid_criterion_ids = {c.id for c in self._criteria}
        result = await self.app.push_screen_wait(
            _TaskInspectorModal(task, node_type.kind.value, valid_criterion_ids)
        )
        if result is None:
            return
        task.description = result.description
        task.acceptance_criterion_ids = result.acceptance_criterion_ids
        task.permission_profile = result.permission_profile
        self._refresh_canvas()

    # -- template metadata / inputs / acceptance criteria ---------------------------

    def action_edit_metadata(self) -> None:
        self.run_worker(self._edit_metadata_flow(), exclusive=False)

    async def _edit_metadata_flow(self) -> None:
        def _validate(values: list[str]) -> str | None:
            name = values[0]
            if not name:
                return "NAME is required"
            if not _IDENTIFIER_RE.match(name):
                return "NAME may only contain letters, digits, '-', '_' (it becomes the filename)"
            return None

        result = await self.app.push_screen_wait(
            _TextFieldModal(
                "Template metadata",
                ["NAME", "DESCRIPTION"],
                [self._template_name, self._template_description],
                _validate,
            )
        )
        if result is None:
            return
        self._template_name, self._template_description = result
        self._refresh_summaries()

    def action_add_input(self) -> None:
        self.run_worker(self._add_input_flow(), exclusive=False)

    async def _add_input_flow(self) -> None:
        existing_names = {i.name for i in self._inputs}

        def _validate(values: list[str]) -> str | None:
            name, _prompt = values
            if not name:
                return "input name is required"
            if not _INPUT_NAME_RE.match(name):
                return "input name may only contain letters, digits, '_' ({{ name }} placeholders)"
            if name in existing_names:
                return f"input '{name}' already declared"
            return None

        result = await self.app.push_screen_wait(
            _TextFieldModal("Add input", ["name", "prompt"], [], _validate)
        )
        if result is None:
            return
        name, prompt = result
        self._inputs.append(_InputDecl(name=name, prompt=prompt))
        self._refresh_summaries()

    def action_add_criterion(self) -> None:
        self.run_worker(self._add_criterion_flow(), exclusive=False)

    async def _add_criterion_flow(self) -> None:
        existing_ids = {c.id for c in self._criteria}

        def _validate(values: list[str]) -> str | None:
            cid, description = values
            if not cid:
                return "criterion id is required"
            if not _IDENTIFIER_RE.match(cid):
                return "criterion id may only contain letters, digits, '-', '_'"
            if cid in existing_ids:
                return f"criterion id '{cid}' already declared"
            if not description:
                return "description is required"
            return None

        result = await self.app.push_screen_wait(
            _TextFieldModal("Add acceptance criterion", ["id", "description"], [], _validate)
        )
        if result is None:
            return
        cid, description = result
        method = await self.app.push_screen_wait(_VerificationMethodModal())
        if method is None:
            self.app.notify("Acceptance criterion not added: no verification_method chosen")
            return
        self._criteria.append(
            _CriterionDecl(id=cid, description=description, verification_method=method)
        )
        self._refresh_summaries()

    # -- save -----------------------------------------------------------------------

    def action_save_template(self) -> None:
        if self._connecting:
            self.app.notify("Finish or cancel connect mode first (escape)", severity="warning")
            return
        app = _get_app(self)
        if not self._template_name.strip():
            self.app.notify("Set a NAME before saving (press m)", severity="error")
            return
        if not self._tasks:
            self.app.notify("Add at least one task before saving", severity="error")
            return

        dags_dir = app.repo_root / ".forge" / "dags"
        target_path = dags_dir / f"{self._template_name}.json"

        dag_tasks = [
            DagTaskSpec(
                id=t.id,
                node_type=t.node_type,
                description=t.description,
                depends_on=list(t.depends_on),
                acceptance_criterion_ids=list(t.acceptance_criterion_ids),
                permission_profile=t.permission_profile,
                required=t.required,
            )
            for t in self._tasks
        ]
        # Same permission_profile validation forge/modes/custom_dag.py's
        # own loader performs — reused directly, not reimplemented.
        for dag_task in dag_tasks:
            try:
                resolve_permission_profile(
                    dag_task,
                    dag_task.node_type,
                    app.node_registry,
                    app.permission_registry,
                    file_path=target_path,
                )
            except CustomDagError as exc:
                self.app.notify(str(exc), severity="error")
                return

        try:
            definition = DagDefinition(
                schema_version=1,
                name=self._template_name,
                description=self._template_description,
                inputs=[DagInputSpec(name=i.name, prompt=i.prompt) for i in self._inputs],
                acceptance_criteria=[
                    DagAcceptanceCriterionSpec(
                        id=c.id,
                        description=c.description,
                        verification_method=cast(_VerificationMethodLiteral, c.verification_method),
                        required=c.required,
                    )
                    for c in self._criteria
                ],
                tasks=dag_tasks,
            )
        except ValidationError as exc:
            self.app.notify(f"Template is invalid: {exc}", severity="error")
            return

        dags_dir.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(definition.model_dump(mode="json"), indent=2))

        try:
            load_dag_definition(target_path)  # re-parse what was just written as a sanity check
        except CustomDagError as exc:
            self.app.notify(f"saved file failed to reload: {exc}", severity="error")
            return

        self.last_saved_path = target_path
        self.app.notify(f"Saved {target_path}")

    # -- leaving / quitting -----------------------------------------------------------

    def action_cancel(self) -> None:
        if self._connecting:
            self._end_connect_mode()
            return
        self.app.pop_screen()

    def action_quit_app(self) -> None:
        self.app.exit()
