"""
End-to-end tests for mid-run chat (M9 phase 6): a free-form message into a
*live* EXECUTION attempt's session, driven through the real Textual `Pilot`
the same way `tests/tui/test_app.py` drives everything else. These go
through the actual chat `Input` widget on `LiveRunScreen`, `ForgeApp.
active_chat_queue_for`, and `RunWorker.active_chat_queue` end to end — the
only thing standing in for the real world is `run_sdk_node` itself (see
`_chat_capturing_runner`, below), same boundary every other TUI test in
this package enforces: no real `claude` CLI session, no network.
"""

from __future__ import annotations

import contextlib
import queue
from pathlib import Path

import pytest
from textual.widgets import Input

from forge import project
from forge.controller.execution_registry import ExecutionRunOutcome
from forge.models.run import RunStatus
from forge.tui import runner as runner_module
from forge.tui.app import ForgeApp
from forge.tui.screens import ConfirmModal, LiveRunScreen, NewSessionScreen
from tests.tui.conftest import fake_execution_runner, write_dag
from tests.tui.test_app import _wait_until


def _chat_capturing_runner(received: list[str]):
    """
    Stands in for `run_sdk_node` for the one EXECUTION task
    (`implement_exec`) in the `implement-and-verify` fixture DAG. Blocks,
    with a generous bound, for a chat message to arrive on the queue
    `RunWorker` hands it, records it, then finishes the attempt normally —
    real work in the worktree for the dispatcher's own git-based pass/fail
    derivation to see, same as `tests/tui/conftest.py`'s
    `fake_execution_runner`. The 15s bound only matters on failure (no
    message ever arrives); the success path is expected to take a small
    fraction of that even on a loaded machine.
    """

    def _runner(
        model: str,
        prompt: str,
        profile: object,
        worktree_path: str,
        *,
        chat_queue: queue.Queue[str] | None = None,
    ) -> ExecutionRunOutcome:
        if chat_queue is not None:
            with contextlib.suppress(queue.Empty):
                received.append(chat_queue.get(timeout=15))
        Path(worktree_path, "test_ok.py").write_text("def test_ok():\n    assert True\n")
        return ExecutionRunOutcome(completed=True, summary="wrote a passing test")

    return _runner


async def _open_live_run(app: ForgeApp, pilot) -> LiveRunScreen:
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
    return live_screen


async def test_chat_input_enables_mid_attempt_sends_a_message_then_disables_again(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)
    write_dag(git_repo)
    received: list[str] = []
    monkeypatch.setattr(runner_module, "run_sdk_node", _chat_capturing_runner(received))

    app = ForgeApp()
    async with app.run_test() as pilot:
        # Before any run exists there's nothing to chat with.
        await _wait_until(lambda: isinstance(app.screen, NewSessionScreen))
        assert app.active_chat_queue_for("no-such-run") is None

        live_screen = await _open_live_run(app, pilot)
        run_id = live_screen.run_id
        chat_input = live_screen.query_one("#chat-input", Input)

        # The screen can mount before the worker thread has actually
        # reached the EXECUTION attempt (thread startup is async relative
        # to push_screen) — poll rather than assume tick one already shows
        # it enabled.
        await _wait_until(lambda: not chat_input.disabled, timeout=10.0)
        assert app.active_chat_queue_for(run_id) is not None

        chat_input.focus()
        await pilot.press(*"please also double check the edge case")
        await pilot.press("enter")

        await _wait_until(lambda: received, timeout=20.0)
        assert received == ["please also double check the edge case"]
        assert chat_input.value == ""  # submitting clears it, same as other Input forms

        await _wait_until(
            lambda: app.store.load_run(run_id).status == RunStatus.COMPLETED_VERIFIED,
            timeout=30.0,
        )
        # No live attempt left once the run (and its one EXECUTION
        # attempt) has finished — disabled again, not left stuck "on".
        await _wait_until(lambda: chat_input.disabled, timeout=10.0)
        assert app.active_chat_queue_for(run_id) is None


async def test_submitting_with_no_live_attempt_is_rejected_not_silently_dropped(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    """
    `Input.disabled` already blocks keyboard interaction in the normal
    case, but `on_input_submitted` guards independently too — defense
    against exactly the race this phase's brief calls out: a message
    landing right as the attempt finishes, after `active_chat_queue_for`
    has already gone back to `None` but before the next poll tick has
    re-disabled the widget. Exercise that guard directly once the run has
    gone fully terminal (a run with no live attempt left at all, the
    simplest case of "nothing to chat with"), posting `Input.Submitted`
    straight at the widget rather than going through a real keypress on a
    by-then-disabled control.
    """
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)
    write_dag(git_repo)
    monkeypatch.setattr(runner_module, "run_sdk_node", fake_execution_runner)

    app = ForgeApp()
    async with app.run_test() as pilot:
        live_screen = await _open_live_run(app, pilot)
        run_id = live_screen.run_id
        chat_input = live_screen.query_one("#chat-input", Input)

        await _wait_until(
            lambda: app.store.load_run(run_id).status == RunStatus.COMPLETED_VERIFIED,
            timeout=30.0,
        )
        await _wait_until(lambda: chat_input.disabled, timeout=10.0)
        assert app.active_chat_queue_for(run_id) is None

        chat_input.value = "anybody there?"
        chat_input.post_message(Input.Submitted(chat_input, "anybody there?"))
        await pilot.pause()

        # Rejected, not silently swallowed: the text is left in place
        # (never cleared as if it had been sent), and the screen still
        # shows chat as unavailable.
        assert chat_input.value == "anybody there?"
        assert chat_input.disabled
