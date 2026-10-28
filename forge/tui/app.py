"""
`ForgeApp`: the Textual app launched by running bare `forge`. Owns exactly
one read-only `Store` (its own SQLite connection, opened once discovery
resolves `.forge/forge.db` — see `forge/project.py`) that every screen
polls from; it never writes run state itself. Starting or resuming a run
hands off to `forge/tui/runner.py`'s `RunWorker`, which drives the real
`run_to_completion` loop (`forge/controller/scheduler.py`) on its own
background thread with its own separate connection — the one and only
writer, exactly as `scripts/forge_cli.py`'s `forge run`/`resume` already
drive it, just moved off the main thread so Textual's event loop is never
blocked by it.

The one write this module does perform directly, on the main thread, is
`start_new_run`'s one-time `start_run` call (persist the new Request/
GraphVersion/Run row) — the same single, non-concurrent write
`scripts/forge_cli.py::_cmd_run` already does before handing off to
`run_to_completion`. It happens over its own short-lived connection,
before any `RunWorker` for that run exists, so there is never a moment
where two things are writing to the database at once.
"""

from __future__ import annotations

import queue
from pathlib import Path
from typing import Any, ClassVar

from textual.app import App
from textual.binding import Binding, BindingType

from forge import project
from forge.controller import db
from forge.controller.model_registry import build_default_model_registry
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.controller.scheduler import start_run
from forge.controller.store import NotFoundError, Store
from forge.controller.validator import compile_and_validate
from forge.models.permissions import Budgets
from forge.models.run import RunStatus
from forge.modes.custom_dag import (
    DagTemplateInfo,
    build_custom_dag_request_and_graph,
    list_dag_templates,
)
from forge.modes.feature_blast import build_feature_blast_request_and_graph
from forge.nodes import register_all
from forge.tui.runner import RunWorker
from forge.tui.screens import ConfirmModal, DashboardScreen, LiveRunScreen, RunSummaryScreen

# Mirrors scripts/forge_cli.py's own DEFAULT_RUN_BUDGETS: the CLI's default
# Budgets for a run started with no dedicated budget flag yet. Duplicated
# here (not imported) since that script is the T20-exempt entry point, not
# a library this layer should depend on.
_DEFAULT_RUN_BUDGETS = Budgets(max_cost_usd=10.0, max_wall_seconds=1800)

_LIVE_STATUSES = (RunStatus.ACTIVE, RunStatus.BLOCKED_AWAITING_INPUT)


class ForgeApp(App[None]):
    """The interactive Forge shell: dashboard, new-session, and live-run screens."""

    TITLE = "Forge"
    BINDINGS: ClassVar[list[BindingType]] = [Binding("t", "toggle_theme", "Toggle light/dark")]

    # Restyles the stock `Footer` so each binding reads as a visually
    # distinct `[key] label` — a themed chip for the key (bold, its own
    # background/foreground) next to a muted description — entirely
    # through CSS on `Footer`'s own documented component classes
    # (`footer-key--key`/`footer-key--description`, confirmed from
    # `textual.widgets._footer`'s source for this installed version
    # rather than assumed). No behavior changes: `Footer`'s own real
    # binding-discovery/`check_action`-driven show/hide logic
    # (`compose`/`bindings_changed`) is untouched, so every screen's
    # existing `check_action` gating (e.g. `LiveRunScreen`'s graph-only
    # actions) still controls what the footer shows exactly as before.
    CSS = """
    Footer {
        background: $panel;
    }
    .footer-key--key {
        background: $panel-lighten-1;
        color: $accent;
        text-style: bold;
    }
    .footer-key--description {
        color: $text-muted;
    }

    /* Every DataTable in this app (DashboardScreen's run list,
       LiveRunScreen's task list) gets this instead of the stock
       `datatable--cursor` look, which is a flat, full-opacity
       `$block-cursor-background` (solid `$primary`, bold text by default
       on most themes) — a hard-edged solid block, confirmed from
       `textual.widgets._data_table.DataTable.DEFAULT_CSS` for this
       installed version rather than guessed. `$primary-muted` is already
       a real theme token (`primary.blend(background, 0.7)`, confirmed
       from `textual.design.ColorSystem`) — a soft, accent-tinted
       background rather than a solid block, and `text-style: none`
       keeps the selected row's text the same normal weight as every
       other row (see the general weight pass below) instead of flipping
       bold on select. The *un*focused cursor (`$block-cursor-blurred-*`,
       already an alpha-blended tint) is left exactly as Textual draws
       it — only the harsh, focused state needed overriding. */
    DataTable:focus > .datatable--cursor {
        background: $primary-muted;
        color: $text;
        text-style: none;
    }
    """

    def __init__(self, *, db_path_override: Path | None = None) -> None:
        super().__init__()
        self._db_path_override = db_path_override
        self.repo_root: Path = Path.cwd()
        self.db_path: Path = Path("forge.db")
        self._store: Store | None = None
        self.node_registry = NodeTypeRegistry()
        self.permission_registry = PermissionProfileRegistry()
        register_all(self.node_registry, self.permission_registry)
        # The real registry (see build_default_model_registry's docstring)
        # — needed here to validate a custom-DAG task's `model` override at
        # build_custom_dag_request_and_graph time, same as
        # self.permission_registry already is for permission_profile.
        self.model_registry = build_default_model_registry()
        self._live_workers: dict[str, RunWorker] = {}

    @property
    def store(self) -> Store:
        """
        The app's one read-only `Store`. Only ever valid once project
        discovery (`_discover_and_start`) has resolved `.forge/forge.db` —
        every screen that reads it is only ever pushed after that point.
        """
        if self._store is None:
            raise RuntimeError("ForgeApp.store accessed before project discovery completed")
        return self._store

    def on_mount(self) -> None:
        # Light by default; `t` toggles to dark. `catppuccin-latte`/
        # `catppuccin-mocha` (confirmed still registered in this Textual
        # version via `App().available_themes`) replace the stock
        # `textual-light`/`textual-dark` pair a prior restyle pass left in
        # place — that pair is Textual's own bland default (a dull,
        # minimal-variety gray), not an actual theme choice. This project's
        # own theme-gallery comparison settled on this catppuccin pair
        # (soft pastel, purple-forward, vivid without being garish) for
        # both halves of the light/dark toggle.
        self.theme = "catppuccin-latte"
        self.run_worker(self._discover_and_start(), exclusive=True, name="discovery")

    async def _discover_and_start(self) -> None:
        try:
            repo_root = project.find_repo_root()
        except project.NotAGitRepoError as exc:
            confirmed = await self.push_screen_wait(
                ConfirmModal(
                    f"Not a git repo: {exc.cwd}\n\n"
                    "Forge needs a git repo to isolate work in worktrees. Work out "
                    "of this folder and run `git init` here?",
                    yes_label="Run git init",
                    no_label="Cancel",
                )
            )
            if not confirmed:
                self.exit()
                return
            repo_root = project.init_repo(exc.cwd)

        paths = project.ensure_forge_dir(repo_root)
        self.repo_root = repo_root
        self.db_path = self._db_path_override or paths.db_path
        self._store = Store(db.connect(self.db_path))
        self.push_screen(DashboardScreen())

    def action_toggle_theme(self) -> None:
        self.theme = "catppuccin-mocha" if self.theme == "catppuccin-latte" else "catppuccin-latte"

    # -- data the screens need, without reaching past this layer -----------------

    def list_modes(self) -> list[DagTemplateInfo]:
        """Every `.forge/dags/*.json` template available alongside Feature Blast."""
        return list_dag_templates(self.repo_root / ".forge" / "dags")

    def start_new_run(
        self, mode: str, template: DagTemplateInfo | None, values: dict[str, str]
    ) -> str:
        """
        Builds the request/graph exactly the way `forge run` already does
        (`build_feature_blast_request_and_graph` or
        `build_custom_dag_request_and_graph`), validates it, and persists
        it via `start_run` — the same one-time setup write
        `scripts/forge_cli.py::_cmd_run` performs before handing off to
        `run_to_completion`. Only ever called after the caller's own
        deploy-authorization confirmation, so `deploy_authorized=True` is
        always correct here: Feature Blast hardcodes it for the same
        reason (requirements.md's key decision 6 — it's a one-time flag,
        not a live per-deploy confirmation), and a custom DAG only reaches
        this call once its own confirmation already succeeded.
        """
        if mode == "feature-blast":
            request, draft = build_feature_blast_request_and_graph(
                objective=values["objective"], journey_description=values["journey"]
            )
        else:
            if template is None:
                raise ValueError("custom DAG session started with no template selected")
            request, draft = build_custom_dag_request_and_graph(
                template.path,
                values,
                self.node_registry,
                self.permission_registry,
                self.model_registry,
                _DEFAULT_RUN_BUDGETS,
                deploy_authorized=True,
            )
        graph = compile_and_validate(draft, request, self.node_registry, self.permission_registry)

        conn = db.connect(self.db_path)
        write_store = Store(conn)
        try:
            run = start_run(request, graph, write_store)
        finally:
            write_store.close()
        return run.id

    def open_run(self, run_id: str, *, force_resume: bool) -> None:
        """
        Opens `run_id`: a live view if it's ACTIVE/BLOCKED_AWAITING_INPUT
        (starting a fresh `RunWorker` first if none is already driving it
        — this is also exactly what resuming an orphaned run looks like,
        since `run_to_completion` reconciles orphaned attempts itself),
        otherwise a static inspect-style summary.
        """
        try:
            run = self.store.load_run(run_id)
        except NotFoundError as exc:
            self.notify(str(exc), severity="error")
            return

        if run.status not in _LIVE_STATUSES:
            if force_resume:
                self.notify(
                    f"Run {run_id} already finished ({run.status.value}); nothing to resume."
                )
            self.push_screen(RunSummaryScreen(run_id))
            return

        worker = self._live_workers.get(run_id)
        if worker is None or not worker.is_alive():
            worker = RunWorker(run_id, self.db_path, self.repo_root)
            self._live_workers[run_id] = worker
            worker.start()
        self.push_screen(LiveRunScreen(run_id))

    def active_chat_queue_for(self, run_id: str) -> queue.Queue[str] | None:
        """
        The live chat queue for `run_id`'s currently in-flight EXECUTION
        attempt, if one exists right now — `None` whenever there's
        nothing to chat with: no `RunWorker` for this run at all, the
        worker exists but is between attempts, or its current attempt is
        MECHANICAL (`deploy_exec` etc. never get a chat queue — see
        `forge/tui/runner.py`'s own docstring for why that needs no
        separate check here). `LiveRunScreen`'s chat input polls this
        directly rather than reaching into a `RunWorker` itself, same as
        every other screen -> app data access in this module.
        """
        worker = self._live_workers.get(run_id)
        if worker is None:
            return None
        return worker.active_chat_queue

    def live_messages_for(self, run_id: str) -> queue.Queue[Any] | None:
        """
        The live-trace message queue for `run_id`'s currently in-flight
        EXECUTION attempt, if one exists right now — `None` under the
        exact same conditions as `active_chat_queue_for` (no `RunWorker`
        for this run at all, the worker is between attempts, or the
        current attempt is MECHANICAL). `LiveRunScreen`'s live-trace panel
        polls this directly, same shape as the chat input polling
        `active_chat_queue_for`.
        """
        worker = self._live_workers.get(run_id)
        if worker is None:
            return None
        return worker.live_messages


def run_tui(db_path_override: Path | None = None) -> int:
    """Entry point `scripts/forge_cli.py` calls for bare `forge` (no subcommand)."""
    ForgeApp(db_path_override=db_path_override).run()
    return 0
