"""
End-to-end Textual tests for `ForgeApp`, driven via `App.run_test()`'s
`Pilot` — real screen mounts and real key/button events, not just the data
layer underneath. Every run started here goes through a monkeypatched
`run_sdk_node` (see `tests/tui/conftest.py::fake_execution_runner`), so
nothing here ever calls a real `claude` CLI session or touches the
network — same boundary `tests/cli/test_run_resume_list.py` already
enforces for the one-shot commands.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest
from rich.text import Text
from textual.pilot import Pilot
from textual.widgets import DataTable, Static

from forge import project
from forge.controller import db
from forge.controller.store import Store
from forge.models.run import RunStatus
from forge.tui import runner as runner_module
from forge.tui.app import ForgeApp
from forge.tui.screens import (
    ConfirmModal,
    DashboardScreen,
    LiveRunScreen,
    NewSessionScreen,
    RunSummaryScreen,
)
from tests.tui.conftest import fake_execution_runner, write_branching_dag, write_dag


def _canvas_plain(static: Static) -> str:
    """The DAG canvas's rendered plain text — `Static.content` is typed as
    the broad `VisualType` union `.update()` accepts, but `LiveRunScreen`
    only ever passes it a `render_dag_canvas` result, which is always a
    `rich.text.Text`."""
    return cast(Text, static.content).plain


async def _wait_until(predicate: object, *, timeout: float = 10.0) -> None:
    """Polls a zero-arg callable until it's truthy or `timeout` elapses."""
    assert callable(predicate)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"condition never became true within {timeout}s")
        await asyncio.sleep(0.05)


# -- discovery / git-init prompt ------------------------------------------------


async def test_declining_git_init_exits_without_creating_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    monkeypatch.chdir(not_a_repo)

    app = ForgeApp()
    async with app.run_test() as pilot:
        await _wait_until(lambda: isinstance(app.screen, ConfirmModal))
        assert isinstance(app.screen, ConfirmModal)
        app.screen.query_one("#no-button").focus()
        await pilot.press("enter")
        await _wait_until(lambda: not app.is_running)

    assert not (not_a_repo / ".forge").exists()
    assert not (not_a_repo / ".git").exists()


async def test_accepting_git_init_creates_forge_dir_and_shows_dashboard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)

    app = ForgeApp()
    async with app.run_test() as pilot:
        await _wait_until(lambda: isinstance(app.screen, ConfirmModal))
        app.screen.query_one("#yes-button").focus()
        await pilot.press("enter")
        # Empty dashboard auto-advances straight to the new-session screen.
        await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))

    assert (fresh / ".git").is_dir()
    assert (fresh / ".forge" / "forge.db").exists()


# -- dashboard: empty state --------------------------------------------------


async def test_empty_dashboard_auto_opens_new_session(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)

    app = ForgeApp()
    async with app.run_test():
        await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))


# -- new session: pick Feature Blast, confirm modal defaults to "no" --------


async def test_confirm_modal_defaults_focus_to_no(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)

    app = ForgeApp()
    async with app.run_test() as pilot:
        await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))
        # Feature Blast is the first (and, with no custom DAGs, only) option.
        await pilot.press("enter")
        await _wait_until(lambda: app.screen.query_one("#field-objective") is not None)
        await pilot.click("#field-objective")
        await pilot.press(*"do a thing", "tab")
        await pilot.click("#field-journey")
        await pilot.press(*"check it works")
        await pilot.click("#continue-button")
        await _wait_until(lambda: isinstance(app.screen, ConfirmModal))
        assert app.screen.query_one("#no-button").has_focus


async def test_declining_deploy_authorization_does_not_start_a_run(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.chdir(git_repo)
    paths = project.ensure_forge_dir(git_repo)

    app = ForgeApp()
    async with app.run_test() as pilot:
        await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))
        await pilot.press("enter")
        await _wait_until(lambda: app.screen.query_one("#field-objective") is not None)
        await pilot.click("#field-objective")
        await pilot.press(*"do a thing", "tab")
        await pilot.click("#field-journey")
        await pilot.press(*"check it works")
        await pilot.click("#continue-button")
        await _wait_until(lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("escape")  # decline
        await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))

    conn = db.connect(paths.db_path)
    store = Store(conn)
    try:
        assert store.list_runs() == []
    finally:
        store.close()


# -- new session: a custom DAG end to end, through the live run screen -------


async def test_custom_dag_run_goes_live_then_completes(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)
    write_dag(git_repo)
    monkeypatch.setattr(runner_module, "run_sdk_node", fake_execution_runner)

    app = ForgeApp()
    async with app.run_test() as pilot:
        await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))
        # Options: [Feature Blast, implement-and-verify] — select the second.
        await pilot.press("down", "enter")
        await _wait_until(lambda: app.screen.query_one("#field-objective") is not None)
        await pilot.click("#field-objective")
        await pilot.press(*"build a tiny thing")
        await pilot.click("#continue-button")
        await _wait_until(lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        await _wait_until(lambda: isinstance(app.screen, LiveRunScreen))
        live_screen = app.screen
        assert isinstance(live_screen, LiveRunScreen)
        run_id = live_screen.run_id
        await _wait_until(
            lambda: app.store.load_run(run_id).status == RunStatus.COMPLETED_VERIFIED, timeout=30.0
        )

        # 'd' returns to the dashboard without stopping anything (the run
        # already finished by this point, but the keybinding itself must
        # still just pop the screen, not touch the worker/thread).
        await pilot.press("d")
        await _wait_until(lambda: isinstance(app.screen, DashboardScreen))
        dashboard_screen = app.screen
        assert isinstance(dashboard_screen, DashboardScreen)
        await _wait_until(lambda: run_id in dashboard_screen._row_run_ids)

        # Re-opening a finished run shows the inspect-style summary, not
        # the live screen.
        await pilot.press("enter")
        await _wait_until(lambda: isinstance(app.screen, RunSummaryScreen))


# -- theme toggle --------------------------------------------------------------


async def test_theme_defaults_light_and_toggles_dark(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)

    app = ForgeApp()
    async with app.run_test() as pilot:
        await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))
        assert app.theme == "textual-light"
        await pilot.press("t")
        assert app.theme == "textual-dark"
        await pilot.press("t")
        assert app.theme == "textual-light"


# -- live run: DAG view toggle -------------------------------------------------


async def test_g_toggles_between_list_and_graph_view(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)
    write_dag(git_repo)
    monkeypatch.setattr(runner_module, "run_sdk_node", fake_execution_runner)

    app = ForgeApp()
    async with app.run_test() as pilot:
        await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))
        await pilot.press("down", "enter")
        await _wait_until(lambda: app.screen.query_one("#field-objective") is not None)
        await pilot.click("#field-objective")
        await pilot.press(*"build a tiny thing")
        await pilot.click("#continue-button")
        await _wait_until(lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        await _wait_until(lambda: isinstance(app.screen, LiveRunScreen))
        live_screen = app.screen
        assert isinstance(live_screen, LiveRunScreen)

        # List view is the default.
        assert live_screen.query_one("#list-view").display is True
        assert live_screen.query_one("#graph-view").display is False

        await pilot.press("g")
        assert live_screen.query_one("#list-view").display is False
        assert live_screen.query_one("#graph-view").display is True
        canvas = live_screen.query_one("#dag-canvas", Static)
        plain = _canvas_plain(canvas)
        assert "implement" in plain
        assert "verify" in plain
        # Box-drawing borders for both tasks' boxes, joined by a connector.
        assert "┌" in plain
        assert "┘" in plain
        assert "─" in plain or "│" in plain

        await pilot.press("g")
        assert live_screen.query_one("#list-view").display is True
        assert live_screen.query_one("#graph-view").display is False

        run_id = live_screen.run_id
        await _wait_until(
            lambda: app.store.load_run(run_id).status == RunStatus.COMPLETED_VERIFIED, timeout=30.0
        )


async def test_graph_view_renders_a_branching_custom_dag(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)
    write_branching_dag(git_repo)
    monkeypatch.setattr(runner_module, "run_sdk_node", fake_execution_runner)

    app = ForgeApp()
    async with app.run_test() as pilot:
        await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))
        await pilot.press("down", "enter")
        await _wait_until(lambda: app.screen.query_one("#field-objective") is not None)
        await pilot.click("#field-objective")
        await pilot.press(*"build a tiny thing")
        await pilot.click("#continue-button")
        await _wait_until(lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        await _wait_until(lambda: isinstance(app.screen, LiveRunScreen))
        live_screen = app.screen
        assert isinstance(live_screen, LiveRunScreen)
        run_id = live_screen.run_id

        await _wait_until(
            lambda: app.store.load_run(run_id).status == RunStatus.COMPLETED_VERIFIED, timeout=30.0
        )
        await pilot.press("g")

        plain = _canvas_plain(live_screen.query_one("#dag-canvas", Static))
        for task_id in ("implement", "unit_verify", "integration_write", "integration_verify"):
            assert task_id in plain
        # The fan-out needs at least one vertical run to join a lane change.
        assert "│" in plain


# -- live run: DAG view zoom/pan/minimap ---------------------------------------


def _setup_branching_dag(monkeypatch: pytest.MonkeyPatch, git_repo: Path) -> None:
    """Chdir into `git_repo` and write the fan-out custom DAG fixture —
    must run *before* `ForgeApp().run_test()` is entered, since project
    discovery (which resolves `.forge/` from the current working
    directory) happens as soon as the app mounts."""
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)
    write_branching_dag(git_repo)
    monkeypatch.setattr(runner_module, "run_sdk_node", fake_execution_runner)


async def _open_branching_run_in_graph_view(app: ForgeApp, pilot: Pilot[None]) -> LiveRunScreen:
    """Drives the already-mounted app (fan-out custom DAG already written
    to `.forge/dags/`, see `_setup_branching_dag`) from the new-session
    screen through to the live-run screen with the graph view showing.
    Three ranks is wide enough that even "full" detail overflows a narrow
    (60-column) terminal — exactly the scenario zoom/pan exist for."""
    await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))
    await pilot.press("down", "enter")
    await _wait_until(lambda: app.screen.query_one("#field-objective") is not None)
    await pilot.click("#field-objective")
    await pilot.press(*"build a tiny thing")
    await pilot.click("#continue-button")
    await _wait_until(lambda: isinstance(app.screen, ConfirmModal))
    await pilot.press("y")
    await _wait_until(lambda: isinstance(app.screen, LiveRunScreen))
    live_screen = app.screen
    assert isinstance(live_screen, LiveRunScreen)

    await pilot.press("g")
    await pilot.pause()
    assert live_screen.query_one("#graph-view").display is True
    # The minimap's first real render is deferred to just after the next
    # layout refresh (see `LiveRunScreen.action_toggle_graph_view`) — wait
    # for it rather than assuming one `pilot.pause()` already covered it.
    await _wait_until(lambda: live_screen.query_one("#graph-minimap", Static).content != "")
    return live_screen


async def test_zoom_and_pan_bindings_only_active_while_graph_view_shown(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    _setup_branching_dag(monkeypatch, git_repo)

    app = ForgeApp()
    async with app.run_test(size=(60, 20)) as pilot:
        await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))
        await pilot.press("down", "enter")
        await _wait_until(lambda: app.screen.query_one("#field-objective") is not None)
        await pilot.click("#field-objective")
        await pilot.press(*"build a tiny thing")
        await pilot.click("#continue-button")
        await _wait_until(lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        await _wait_until(lambda: isinstance(app.screen, LiveRunScreen))
        live_screen = app.screen
        assert isinstance(live_screen, LiveRunScreen)

        # List view showing: the graph-only zoom/pan actions are disabled
        # (hidden from the footer), and a plain "up"/"down" press still
        # moves the task table's own row cursor rather than being
        # swallowed by a graph-only pan binding.
        assert live_screen.check_action("zoom_full", ()) is False
        assert live_screen.check_action("pan_left", ()) is False
        table = live_screen.query_one("#tasks-table", DataTable)
        table.focus()
        await pilot.pause()
        start_row = table.cursor_row
        await pilot.press("down")
        await pilot.pause()
        assert table.cursor_row != start_row

        # Graph view showing: they're active.
        await pilot.press("g")
        assert live_screen.check_action("zoom_full", ()) is True
        assert live_screen.check_action("pan_left", ()) is True


def _current_detail(screen: LiveRunScreen) -> str:
    # A plain `str` return erases the `Literal["full", "compact"]` type so
    # mypy doesn't (incorrectly) treat an equality assert on this attribute
    # as narrowing it for the rest of the test, across awaits that can
    # actually change it.
    return screen._detail


async def test_minus_switches_to_compact_detail_and_plus_back_to_full(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    _setup_branching_dag(monkeypatch, git_repo)

    app = ForgeApp()
    async with app.run_test(size=(60, 20)) as pilot:
        live_screen = await _open_branching_run_in_graph_view(app, pilot)
        canvas = live_screen.query_one("#dag-canvas", Static)

        full_plain = _canvas_plain(canvas)
        full_width = max(len(line) for line in full_plain.split("\n"))
        assert _current_detail(live_screen) == "full"

        await pilot.press("-")
        compact_plain = _canvas_plain(canvas)
        compact_width = max(len(line) for line in compact_plain.split("\n"))
        assert _current_detail(live_screen) == "compact"
        # Compact detail is deliberately narrower per rank — that's what
        # lets a wide graph fit in less horizontal space.
        assert compact_width < full_width

        await pilot.press("+")
        assert _current_detail(live_screen) == "full"
        restored_plain = _canvas_plain(canvas)
        assert max(len(line) for line in restored_plain.split("\n")) == full_width


async def test_arrow_keys_pan_the_graph_view_and_zero_resets(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    _setup_branching_dag(monkeypatch, git_repo)

    app = ForgeApp()
    async with app.run_test(size=(60, 20)) as pilot:
        live_screen = await _open_branching_run_in_graph_view(app, pilot)
        container = live_screen.query_one("#graph-view")

        assert container.scroll_x == 0
        await pilot.press("right")
        await pilot.press("right")
        after_right = container.scroll_x
        assert after_right > 0

        await pilot.press("left")
        assert container.scroll_x < after_right

        # "0" snaps back to the origin and picks whichever detail fits the
        # (narrow, 60-column) terminal best.
        await pilot.press("+")  # force "full" first, so reset has real work to do
        await pilot.press("right")
        await pilot.press("0")
        assert container.scroll_x == 0
        assert container.scroll_y == 0


async def test_minimap_reflects_the_real_scroll_position(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    _setup_branching_dag(monkeypatch, git_repo)

    app = ForgeApp()
    async with app.run_test(size=(60, 20)) as pilot:
        live_screen = await _open_branching_run_in_graph_view(app, pilot)
        minimap = live_screen.query_one("#graph-minimap", Static)

        before = cast(str, minimap.content)
        assert before  # driven by a real scroll position, not blank/absent
        assert "ranks" in before

        await pilot.press("right")
        await pilot.press("right")
        await pilot.press("right")
        after = cast(str, minimap.content)
        assert after != before
