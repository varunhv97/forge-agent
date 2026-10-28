"""
Pilot-driven assertions that the restyled chrome (`Chrome`, in place of
Textual's stock `Header()`), the labeled-panel pattern (`LabeledPanel`),
and the dot-plus-text status convention (`_status_dot_text`) actually
render — not just "it didn't crash". This is a pure visual restyle (see
`tasks.md`'s post-M9 entry): no screen's behavior, keybindings, or data
flow changed, which is exactly what makes "does it look different now"
worth checking for real rather than taking on faith.

Reuses the same `Pilot`/fake-execution-runner boundary
`tests/tui/test_app.py` already enforces (no real `claude` CLI session,
no network).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest
from rich.text import Text
from textual.widgets import DataTable, Static

from forge import project
from forge.models.run import RunStatus
from forge.tui import runner as runner_module
from forge.tui.app import ForgeApp
from forge.tui.chrome import Chrome
from forge.tui.screens import ConfirmModal, DashboardScreen, LiveRunScreen, NewSessionScreen
from tests.tui.conftest import fake_execution_runner, write_dag


async def _wait_until(predicate: object, *, timeout: float = 10.0) -> None:
    """Same helper, same generous default, as `tests/tui/test_app.py`'s."""
    assert callable(predicate)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"condition never became true within {timeout}s")
        await asyncio.sleep(0.05)


def _chrome_plain(screen: object) -> str:
    chrome = cast(Chrome, screen.query_one(Chrome))  # type: ignore[attr-defined]
    return cast(Text, chrome.content).plain


def _panel_label(screen: object, panel_id: str) -> str:
    label = screen.query_one(f"#{panel_id} .panel-label", Static)  # type: ignore[attr-defined]
    return str(label.content)


# -- chrome: dots + title -------------------------------------------------------


async def test_new_session_chrome_shows_dots_and_title(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)

    app = ForgeApp()
    async with app.run_test():
        await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))
        plain = _chrome_plain(app.screen)
        assert plain.count("●") == 3  # three dot glyphs, not decoration text
        assert "forge" in plain
        assert "new session" in plain


async def test_dashboard_chrome_shows_repo_name(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)
    write_dag(git_repo)
    monkeypatch.setattr(runner_module, "run_sdk_node", fake_execution_runner)

    app = ForgeApp()
    async with app.run_test() as pilot:
        # Get one run onto the dashboard so it doesn't auto-advance to
        # New Session, then come back to it after the run finishes.
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
            lambda: app.store.load_run(run_id).status == RunStatus.COMPLETED_VERIFIED,
            timeout=30.0,
        )

        # The chrome title on the live-run screen names the run itself.
        assert run_id in _chrome_plain(live_screen)

        await pilot.press("d")
        await _wait_until(lambda: isinstance(app.screen, DashboardScreen))
        dashboard_plain = _chrome_plain(app.screen)
        assert dashboard_plain.count("●") == 3
        assert git_repo.name in dashboard_plain


# -- labeled panels: RUNS / TASKS / TRACE / CHAT --------------------------------


async def test_dashboard_runs_table_is_in_a_labeled_panel(
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
        run_id = live_screen.run_id

        # Labeled panels around the task list / trace log / chat input.
        assert _panel_label(live_screen, "tasks-panel") == "TASKS"
        assert _panel_label(live_screen, "trace-panel") == "TRACE"
        assert _panel_label(live_screen, "chat-panel") == "CHAT"
        assert _panel_label(live_screen, "graph-panel") == "GRAPH"

        await _wait_until(
            lambda: app.store.load_run(run_id).status == RunStatus.COMPLETED_VERIFIED,
            timeout=30.0,
        )
        await pilot.press("d")
        await _wait_until(lambda: isinstance(app.screen, DashboardScreen))
        assert _panel_label(app.screen, "runs-panel") == "RUNS"


# -- status as a dot plus text, not bare colored text ---------------------------


async def test_live_run_task_status_renders_as_dot_plus_text(
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
        run_id = live_screen.run_id
        await _wait_until(
            lambda: app.store.load_run(run_id).status == RunStatus.COMPLETED_VERIFIED,
            timeout=30.0,
        )

        table = live_screen.query_one("#tasks-table", DataTable)
        assert table.row_count
        for row_index in range(table.row_count):
            # STATUS is column 1 (TASK, STATUS, NODE TYPE, ATTEMPT) — the
            # post-M9 restyle fix-up moved it right after TASK so it's
            # never the column a too-narrow table pushes out of view; see
            # forge/tui/screens.py's "DataTable column widths" section.
            cell = cast(Text, table.get_row_at(row_index)[1])
            assert cell.plain.startswith("● ")

        header_plain = cast(Text, live_screen.query_one("#run-header", Static).content).plain
        assert "●" in header_plain

        await pilot.press("d")
        await _wait_until(lambda: isinstance(app.screen, DashboardScreen))
        dashboard = app.screen
        assert isinstance(dashboard, DashboardScreen)
        await _wait_until(lambda: run_id in dashboard._row_run_ids)
        runs_table = dashboard.query_one("#runs-table", DataTable)
        assert runs_table.row_count
        for row_index in range(runs_table.row_count):
            # STATUS is column 1 (RUN ID, STATUS, MODE, OBJECTIVE,
            # UPDATED) — moved right after RUN ID for the same reason as
            # the task table above.
            cell = cast(Text, runs_table.get_row_at(row_index)[1])
            assert cell.plain.startswith("● ")


# -- DAG builder: palette/inspector/canvas panels -------------------------------


async def test_dag_builder_panels_are_labeled(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)

    app = ForgeApp()
    async with app.run_test() as pilot:
        await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))
        await pilot.press("escape")
        await _wait_until(lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("b")
        from forge.tui.dag_builder_screen import DagBuilderScreen

        await _wait_until(lambda: isinstance(app.screen, DagBuilderScreen))
        builder = app.screen
        assert _chrome_plain(builder).count("●") == 3
        assert "build DAG" in _chrome_plain(builder)
        assert _panel_label(builder, "palette-panel") == "NODE TYPE PALETTE"
        assert _panel_label(builder, "inspector-panel") == "INSPECTOR"
        assert _panel_label(builder, "canvas-panel") == "CANVAS"
