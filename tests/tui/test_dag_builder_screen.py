"""
End-to-end Textual tests for the visual DAG builder (`DagBuilderScreen`),
driven via `App.run_test()`'s `Pilot` — real screen mounts and real key/
button events, same boundary `tests/tui/test_app.py` already enforces (no
real `claude` CLI session, no network; this screen never needs either,
since it only authors a template file, it doesn't run one).

The actual acceptance criterion for this phase isn't "the file looks
right" — it's `test_building_and_saving_a_branching_template_round_trips`
actually feeding the file the UI wrote straight into
`build_custom_dag_request_and_graph` + `compile_and_validate`, the exact
same gate a real `forge run --mode <template>` would go through.

Every field in a text-entry modal (task id, template metadata, a new
input, a new acceptance criterion) is typed and saved through the real
`_TextFieldModal`; waits on modal-interior widgets query `app.screen`
(the currently *active* screen), not `live_screen` — a pushed modal is a
sibling `Screen`, not a descendant of the screen that pushed it, so
`live_screen.query(...)` would never see anything inside one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Input, OptionList

from forge import project
from forge.controller.validator import compile_and_validate
from forge.models.permissions import Budgets
from forge.modes.custom_dag import build_custom_dag_request_and_graph
from forge.tui.app import ForgeApp
from forge.tui.dag_builder_screen import DagBuilderScreen
from forge.tui.screens import DashboardScreen, NewSessionScreen


async def _wait_until(predicate: object, *, timeout: float = 10.0) -> None:
    """Polls a zero-arg callable until it's truthy or `timeout` elapses —
    same helper, same generous default, as `tests/tui/test_app.py`'s."""
    assert callable(predicate)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"condition never became true within {timeout}s")
        await asyncio.sleep(0.05)


def _capture_notifications(monkeypatch: pytest.MonkeyPatch, app: ForgeApp) -> list[tuple[str, str]]:
    """Records every `app.notify(...)` call instead of actually toasting
    it, so a test can assert on the builder's own error/success messages
    (e.g. the cycle-rejection one) without reaching into Textual's own
    notification-widget internals."""
    captured: list[tuple[str, str]] = []

    def _fake_notify(message: object, *, severity: str = "information", **_kwargs: object) -> None:
        captured.append((str(message), severity))

    monkeypatch.setattr(app, "notify", _fake_notify)
    return captured


async def _fill_text_modal(app: ForgeApp, pilot: Pilot[None], values: list[str]) -> None:
    """Types `values` into a freshly-opened `_TextFieldModal`'s `#tf-0`,
    `#tf-1`, ... fields (tabbing between them) and clicks Save. Waits for
    the modal to actually be up first, since it's opened by an action
    bound to a keypress the caller just sent."""
    await _wait_until(lambda: app.screen.query(Input))
    await pilot.click("#tf-0")
    for i, value in enumerate(values):
        if i:
            await pilot.press("tab")
        if value:
            await pilot.press(*value)
    await pilot.click("#save-button")


async def _add_task(app: ForgeApp, pilot: Pilot[None], palette_index: int, task_id: str) -> None:
    """Highlights the `palette_index`-th node type in the palette (0-
    indexed, in `register_all`'s own registration order) and places it
    on the canvas under `task_id` via the real add-task modal flow."""
    screen = app.screen
    assert isinstance(screen, DagBuilderScreen)
    palette = screen.query_one("#palette-list", OptionList)
    palette.focus()
    palette.highlighted = palette_index
    await pilot.press("enter")
    await _fill_text_modal(app, pilot, [task_id])
    await _wait_until(lambda: task_id in {t.id for t in screen._tasks})


async def _open_builder(app: ForgeApp, pilot: Pilot[None]) -> DagBuilderScreen:
    """Navigates from app startup to the builder screen. A freshly
    created `.forge/` has no runs yet, so `DashboardScreen` auto-advances
    straight to `NewSessionScreen` (same as `test_app.py`'s own empty-
    dashboard test) before this test ever gets a chance to see the
    dashboard itself — escaping back out of it is what actually reveals
    `DashboardScreen`, and `DashboardScreen` only auto-opens it once per
    instance, so it stays put after that."""
    await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))
    await pilot.press("escape")
    await _wait_until(lambda: isinstance(app.screen, DashboardScreen))
    await pilot.press("b")
    await _wait_until(lambda: isinstance(app.screen, DagBuilderScreen))
    screen = app.screen
    assert isinstance(screen, DagBuilderScreen)
    return screen


async def test_b_opens_dag_builder_from_dashboard(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)

    app = ForgeApp()
    async with app.run_test() as pilot:
        await _open_builder(app, pilot)
        # escape with nothing placed yet just leaves, no save.
        await pilot.press("escape")
        await _wait_until(lambda: isinstance(app.screen, DashboardScreen))


async def test_building_and_saving_a_branching_template_round_trips(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)

    app = ForgeApp()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await _open_builder(app, pilot)
        notifications = _capture_notifications(monkeypatch, app)

        # Place two tasks: implement_exec (palette index 0) and
        # unit_verify_exec (palette index 3 — see register_all's own
        # registration order in forge/nodes/__init__.py).
        await _add_task(app, pilot, 0, "implement")
        await _add_task(app, pilot, 3, "verify")
        # The task just added is always the newly selected one.
        assert screen._selected_id == "verify"

        # Both tasks are still at rank 0 (no edges yet) — "[" moves to
        # the first-placed one, by insertion-order lane.
        await pilot.press("[")
        assert screen._selected_id == "implement"

        # Connect: implement -> verify (verify depends_on implement).
        await pilot.press("c")
        assert screen._connecting is True
        await pilot.press("]")
        assert screen._selected_id == "verify"
        await pilot.press("enter")
        assert screen._connecting is False
        assert screen._task_by_id("verify").depends_on == ["implement"]
        # action_confirm_connect leaves the target selected.
        assert screen._selected_id == "verify"

        # Template metadata.
        await pilot.press("m")
        await _fill_text_modal(
            app, pilot, ["fan-out-builder-test", "built by the DAG builder's own test"]
        )
        await _wait_until(lambda: screen._template_name == "fan-out-builder-test")

        # One declared input.
        await pilot.press("f")
        await _fill_text_modal(app, pilot, ["objective", "what should implement_exec build?"])
        await _wait_until(lambda: len(screen._inputs) == 1)

        # One acceptance criterion, which "verify" will cover.
        await pilot.press("v")
        await _fill_text_modal(app, pilot, ["tests_pass", "unit tests pass"])
        await _wait_until(lambda: app.screen.query(OptionList))
        await pilot.press("enter")  # "automated_test", highlighted by default
        await _wait_until(lambda: len(screen._criteria) == 1)

        # Attach the criterion to "verify" (still selected) via its
        # inspector, and give it a real description.
        await pilot.press("i")
        await _wait_until(lambda: app.screen.query("#field-criteria"))
        await pilot.click("#field-description")
        await pilot.press(*"run the unit tests implement wrote")
        await pilot.click("#field-criteria")
        await pilot.press(*"tests_pass")
        await pilot.click("#save-button")
        await _wait_until(lambda: screen._task_by_id("verify").acceptance_criterion_ids)

        # Save the template.
        await pilot.press("s")
        await _wait_until(lambda: screen.last_saved_path is not None)

        assert not any(sev == "error" for _msg, sev in notifications), notifications
        saved_path = screen.last_saved_path
        assert saved_path is not None
        assert saved_path == git_repo / ".forge" / "dags" / "fan-out-builder-test.json"
        assert saved_path.is_file()

        # The actual acceptance criterion for this whole phase: the file
        # the UI wrote has to survive the exact same gate a real
        # `forge run --mode <template>` would put it through, not just
        # "look right".
        request, draft = build_custom_dag_request_and_graph(
            saved_path,
            {"objective": "a tiny test feature"},
            app.node_registry,
            app.permission_registry,
            app.model_registry,
            Budgets(max_cost_usd=1.0, max_wall_seconds=60),
            deploy_authorized=False,
        )
        graph = compile_and_validate(draft, request, app.node_registry, app.permission_registry)
        assert graph.status == "active"
        assert {t.id for t in graph.tasks} == {"implement", "verify"}
        verify_task = next(t for t in graph.tasks if t.id == "verify")
        assert verify_task.depends_on == ["implement"]
        assert verify_task.acceptance_criterion_ids == ["tests_pass"]


async def test_connect_rejects_a_cycle(monkeypatch: pytest.MonkeyPatch, git_repo: Path) -> None:
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)

    app = ForgeApp()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await _open_builder(app, pilot)
        notifications = _capture_notifications(monkeypatch, app)

        await _add_task(app, pilot, 0, "a")
        await _add_task(app, pilot, 0, "b")
        assert screen._selected_id == "b"

        await pilot.press("[")
        assert screen._selected_id == "a"

        # a -> b (b depends on a) — accepted, no cycle.
        await pilot.press("c")
        await pilot.press("]")
        assert screen._selected_id == "b"
        await pilot.press("enter")
        assert screen._task_by_id("b").depends_on == ["a"]
        assert screen._selected_id == "b"

        # Now try b -> a (which would make a depend on b too): a cycle.
        await pilot.press("c")
        assert screen._connecting is True
        await pilot.press("[")
        assert screen._selected_id == "a"
        await pilot.press("enter")

        # Rejected: 'a's depends_on is untouched, connect mode ended, and
        # a clear, visible error was shown — not a silent no-op.
        assert screen._task_by_id("a").depends_on == []
        assert screen._connecting is False
        assert any(
            sev == "error" and "cycle" in msg.lower() for msg, sev in notifications
        ), notifications
