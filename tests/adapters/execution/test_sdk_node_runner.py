from __future__ import annotations

import queue
from unittest.mock import patch

from forge.adapters.execution.sdk_execution_adapter import ExecutionResult
from forge.adapters.execution.sdk_node_runner import run_sdk_node
from forge.controller.execution_registry import EffortSwapRequest
from forge.models.permissions import PermissionProfile


def _profile() -> PermissionProfile:
    return PermissionProfile(name="test", allowed_tools=["Read", "Edit"])


async def _fake_run_execution_attempt(
    *,
    prompt,
    profile,
    worktree_path,
    model,
    max_turns=None,
    chat_queue=None,
    effort=None,
    live_messages=None,
    model_swap_queue=None,
    effort_swap_queue=None,
    resume_session_id=None,
):
    return ExecutionResult(messages=[object(), object()], decisions=[])


def test_successful_session_reports_completed():
    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_fake_run_execution_attempt,
    ):
        outcome = run_sdk_node("claude-sonnet-5", "do the thing", _profile(), "/tmp/wt")

    assert outcome.completed is True
    assert "2 message" in outcome.summary


def test_session_id_is_threaded_through_to_the_outcome():
    async def _fake_with_session_id(
        *,
        prompt,
        profile,
        worktree_path,
        model,
        max_turns=None,
        chat_queue=None,
        effort=None,
        live_messages=None,
        model_swap_queue=None,
        effort_swap_queue=None,
        resume_session_id=None,
    ):
        return ExecutionResult(messages=[object()], decisions=[], session_id="sess-xyz-789")

    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_fake_with_session_id,
    ):
        outcome = run_sdk_node("claude-sonnet-5", "do the thing", _profile(), "/tmp/wt")

    assert outcome.session_id == "sess-xyz-789"


def test_adapter_exception_reports_not_completed():
    async def _raising(**kwargs):
        raise RuntimeError("network exploded")

    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_raising,
    ):
        outcome = run_sdk_node("claude-sonnet-5", "do the thing", _profile(), "/tmp/wt")

    assert outcome.completed is False
    assert "network exploded" in outcome.summary


def test_no_chat_queue_defaults_to_none_on_run_execution_attempt():
    captured: dict[str, object] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return ExecutionResult(messages=[], decisions=[])

    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_capture,
    ):
        run_sdk_node("claude-sonnet-5", "do the thing", _profile(), "/tmp/wt")

    assert captured["chat_queue"] is None


def test_chat_queue_is_forwarded_through_to_run_execution_attempt():
    captured: dict[str, object] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return ExecutionResult(messages=[], decisions=[])

    chat_queue: queue.Queue[str] = queue.Queue()
    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_capture,
    ):
        run_sdk_node(
            "claude-sonnet-5", "do the thing", _profile(), "/tmp/wt", chat_queue=chat_queue
        )

    assert captured["chat_queue"] is chat_queue


def test_no_effort_defaults_to_none_on_run_execution_attempt():
    captured: dict[str, object] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return ExecutionResult(messages=[], decisions=[])

    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_capture,
    ):
        run_sdk_node("claude-sonnet-5", "do the thing", _profile(), "/tmp/wt")

    assert captured["effort"] is None


def test_effort_is_forwarded_through_to_run_execution_attempt():
    captured: dict[str, object] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return ExecutionResult(messages=[], decisions=[])

    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_capture,
    ):
        run_sdk_node("claude-sonnet-5", "do the thing", _profile(), "/tmp/wt", effort="high")

    assert captured["effort"] == "high"


def test_no_live_messages_defaults_to_none_on_run_execution_attempt():
    captured: dict[str, object] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return ExecutionResult(messages=[], decisions=[])

    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_capture,
    ):
        run_sdk_node("claude-sonnet-5", "do the thing", _profile(), "/tmp/wt")

    assert captured["live_messages"] is None


def test_live_messages_is_forwarded_through_to_run_execution_attempt():
    captured: dict[str, object] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return ExecutionResult(messages=[], decisions=[])

    live_messages: queue.Queue[object] = queue.Queue()
    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_capture,
    ):
        run_sdk_node(
            "claude-sonnet-5",
            "do the thing",
            _profile(),
            "/tmp/wt",
            live_messages=live_messages,
        )

    assert captured["live_messages"] is live_messages


# -- model_swap_queue/effort_swap_queue/resume_session_id (post-M9 phase 4) --


def test_no_swap_queues_or_resume_session_id_default_to_none_on_run_execution_attempt():
    captured: dict[str, object] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return ExecutionResult(messages=[], decisions=[])

    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_capture,
    ):
        run_sdk_node("claude-sonnet-5", "do the thing", _profile(), "/tmp/wt")

    assert captured["model_swap_queue"] is None
    assert captured["effort_swap_queue"] is None
    assert captured["resume_session_id"] is None


def test_model_swap_queue_is_forwarded_through_to_run_execution_attempt():
    captured: dict[str, object] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return ExecutionResult(messages=[], decisions=[])

    model_swap_queue: queue.Queue[str] = queue.Queue()
    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_capture,
    ):
        run_sdk_node(
            "claude-sonnet-5",
            "do the thing",
            _profile(),
            "/tmp/wt",
            model_swap_queue=model_swap_queue,
        )

    assert captured["model_swap_queue"] is model_swap_queue


def test_effort_swap_queue_is_forwarded_through_to_run_execution_attempt():
    captured: dict[str, object] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return ExecutionResult(messages=[], decisions=[])

    effort_swap_queue: queue.Queue[EffortSwapRequest] = queue.Queue()
    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_capture,
    ):
        run_sdk_node(
            "claude-sonnet-5",
            "do the thing",
            _profile(),
            "/tmp/wt",
            effort_swap_queue=effort_swap_queue,
        )

    assert captured["effort_swap_queue"] is effort_swap_queue


def test_resume_session_id_is_forwarded_through_to_run_execution_attempt():
    captured: dict[str, object] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return ExecutionResult(messages=[], decisions=[])

    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_capture,
    ):
        run_sdk_node(
            "claude-sonnet-5",
            "do the thing",
            _profile(),
            "/tmp/wt",
            resume_session_id="sess-resume-1",
        )

    assert captured["resume_session_id"] == "sess-resume-1"


def test_no_swap_requested_on_the_adapter_result_leaves_outcome_swap_requested_none():
    async def _capture(**kwargs):
        return ExecutionResult(messages=[], decisions=[])

    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_capture,
    ):
        outcome = run_sdk_node("claude-sonnet-5", "do the thing", _profile(), "/tmp/wt")

    assert outcome.swap_requested is None


def test_swap_requested_is_threaded_through_to_the_outcome():
    swap_request = EffortSwapRequest(effort="max", model="claude-opus-5")

    async def _capture(**kwargs):
        return ExecutionResult(
            messages=[], decisions=[], session_id="sess-live-1", swap_requested=swap_request
        )

    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_capture,
    ):
        outcome = run_sdk_node("claude-sonnet-5", "do the thing", _profile(), "/tmp/wt")

    assert outcome.swap_requested == swap_request
    assert outcome.session_id == "sess-live-1"
