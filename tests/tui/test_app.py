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
from textual.widgets import Static

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
