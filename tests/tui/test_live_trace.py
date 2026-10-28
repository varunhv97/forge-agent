"""
End-to-end test for the live trace window (post-M9 phase 3): a *separate*
panel from `#trace-log`'s abstracted DISPATCHED/PASSED replay, showing an
abbreviated rendering of the real Claude Agent SDK messages a chattable
EXECUTION attempt produces while it's still running. Driven through the
real Textual `Pilot`, same boundary every other TUI test in this package
enforces (a monkeypatched `run_sdk_node` standing in for the real `claude`
CLI session — no network, no real SDK call).
"""

from __future__ import annotations

import queue
import time
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage
from textual.widgets import RichLog

from forge import project
from forge.controller.execution_registry import ExecutionRunOutcome
from forge.models.common import EffortLevel
from forge.tui import runner as runner_module
from forge.tui.app import ForgeApp
from tests.tui.conftest import fake_execution_runner, write_dag
from tests.tui.test_app import _wait_until
from tests.tui.test_chat import _open_live_run


def _live_trace_emitting_runner(
    model: str,
    prompt: str,
    profile: object,
    worktree_path: str,
    *,
    chat_queue: queue.Queue[str] | None = None,
    effort: EffortLevel | None = None,
    live_messages: queue.Queue[object] | None = None,
) -> ExecutionRunOutcome:
    """
    Stands in for `run_sdk_node` for the one EXECUTION task
    (`implement_exec`) in the `implement-and-verify` fixture DAG, the same
    way `tests/tui/test_chat.py::_chat_capturing_runner` does for chat —
    but pushes real `claude_agent_sdk` message objects onto `live_messages`
    instead of waiting on `chat_queue`, simulating a few turns of a real
    session: assistant text, a tool call, and that tool's result.
    """
    if live_messages is not None:
        live_messages.put(
            AssistantMessage(content=[TextBlock(text="implementing the feature now")], model=model)
        )
        live_messages.put(
            AssistantMessage(
                content=[ToolUseBlock(id="tu_1", name="Bash", input={"command": "pytest -q"})],
                model=model,
            )
        )
        live_messages.put(
            UserMessage(
                content=[ToolResultBlock(tool_use_id="tu_1", content="1 passed", is_error=False)]
            )
        )
        # Generous, deliberate delay before returning — `LiveRunScreen`
        # polls every 0.75s; without a real window here the attempt could
        # finish (and `RunWorker` clear `live_messages` back to `None` in
        # its own `finally`) before any poll tick ever drains it, same
        # timing lesson this phase's own brief called out.
        time.sleep(1.0)
    Path(worktree_path, "test_ok.py").write_text("def test_ok():\n    assert True\n")
    return ExecutionRunOutcome(completed=True, summary="wrote a passing test")


def _rendered_text(log: RichLog) -> str:
    """The live `RichLog`'s actual rendered visual lines, joined — what a
    person watching the panel would actually see, not an internal buffer
    this test peeks at instead of the real render."""
    return "\n".join(strip.text for strip in log.lines)


async def test_live_trace_panel_shows_real_sdk_message_content(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)
    write_dag(git_repo)
    monkeypatch.setattr(runner_module, "run_sdk_node", _live_trace_emitting_runner)

    app = ForgeApp()
    async with app.run_test() as pilot:
        live_screen = await _open_live_run(app, pilot)
        log = live_screen.query_one("#live-trace-log", RichLog)

        # Nothing rendered here is the abstracted DISPATCHED/PASSED kind of
        # line `#trace-log` shows — this is the real message stream.
        await _wait_until(lambda: len(log.lines) > 0, timeout=10.0)
        rendered = _rendered_text(log)
        assert "implementing the feature now" in rendered
        assert "pytest -q" in rendered
        assert "1 passed" in rendered

        # The two panels read differently even for a human skimming: the
        # abstracted trace never mentions a shell command or tool result
        # text verbatim, only the live one does.
        trace_log = live_screen.query_one("#trace-log", RichLog)
        assert "pytest -q" not in _rendered_text(trace_log)

        await _wait_until(
            lambda: app.store.load_run(live_screen.run_id).status.value
            in ("completed_verified", "failed_exhausted"),
            timeout=30.0,
        )


async def test_a_message_left_in_the_queue_when_the_attempt_ends_is_still_drained(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    """
    Deterministic (no real-time racing) proof of `_refresh_live_trace`'s
    own stated guarantee: a message still sitting on the queue the moment
    `RunWorker` clears its `live_messages` attribute back to `None` is
    still drained and rendered, not stranded. Drives `LiveRunScreen.
    _refresh_live_trace` directly (a plain synchronous method — no
    `await` anywhere in between these calls, so Textual's own real
    0.75s poll timer cannot interleave and race this sequence) with a
    hand-controlled fake in place of `ForgeApp.live_messages_for`, rather
    than trying to land a real background thread in this exact window —
    the real cross-thread version of "does the message ever arrive at
    all" is already covered by `tests/tui/test_runner.py` and
    `test_live_trace_panel_shows_real_sdk_message_content` above.
    """
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)
    write_dag(git_repo)
    monkeypatch.setattr(runner_module, "run_sdk_node", fake_execution_runner)

    app = ForgeApp()
    async with app.run_test() as pilot:
        live_screen = await _open_live_run(app, pilot)
        log = live_screen.query_one("#live-trace-log", RichLog)
        edge_case_queue: queue.Queue[object] = queue.Queue()

        # Tick A: a chattable attempt is "in flight" (current queue is
        # non-None) but hasn't produced anything yet.
        monkeypatch.setattr(app, "live_messages_for", lambda run_id: edge_case_queue)
        live_screen._refresh_live_trace()
        assert len(log.lines) == 0

        # The attempt produces one message, then finishes — `RunWorker`
        # would clear its own `live_messages` attribute to `None` right
        # about now, in its `finally`.
        edge_case_queue.put(
            AssistantMessage(content=[TextBlock(text="edge case message")], model="claude-sonnet-5")
        )
        monkeypatch.setattr(app, "live_messages_for", lambda run_id: None)

        # Tick B: `ForgeApp.live_messages_for` already reports `None`, but
        # the message is still rendered — drained from `self._live_queue`,
        # the previous tick's reference, not lost.
        live_screen._refresh_live_trace()
        rendered = _rendered_text(log)
        assert "edge case message" in rendered

        # Tick C: idempotent — nothing left to drain, nothing re-rendered.
        live_screen._refresh_live_trace()
        assert _rendered_text(log) == rendered


async def test_no_live_trace_content_for_a_mechanical_only_run(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    """
    Scope boundary check: a run with only MECHANICAL work in flight (the
    `unit_verify_exec` task, once `implement_exec` has already finished)
    never gets anything pushed into `#live-trace-log` — mirrors the
    existing chat-input scope boundary ("no chattable attempt, nothing to
    show") for this second channel.
    """
    monkeypatch.chdir(git_repo)
    project.ensure_forge_dir(git_repo)
    write_dag(git_repo)
    monkeypatch.setattr(runner_module, "run_sdk_node", _live_trace_emitting_runner)

    app = ForgeApp()
    async with app.run_test() as pilot:
        live_screen = await _open_live_run(app, pilot)
        run_id = live_screen.run_id

        await _wait_until(
            lambda: app.store.load_run(run_id).status.value
            in ("completed_verified", "failed_exhausted"),
            timeout=30.0,
        )
        # The live queue is gone once the run is fully terminal — nothing
        # left to drain, same as the chat input going back to disabled.
        assert app.live_messages_for(run_id) is None
