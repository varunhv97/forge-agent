from __future__ import annotations

import queue
from unittest.mock import patch

from forge.adapters.execution.sdk_execution_adapter import ExecutionResult
from forge.adapters.execution.sdk_node_runner import run_sdk_node
from forge.models.permissions import PermissionProfile


def _profile() -> PermissionProfile:
    return PermissionProfile(name="test", allowed_tools=["Read", "Edit"])


async def _fake_run_execution_attempt(
    *, prompt, profile, worktree_path, model, max_turns=None, chat_queue=None, effort=None
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
        *, prompt, profile, worktree_path, model, max_turns=None, chat_queue=None, effort=None
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
