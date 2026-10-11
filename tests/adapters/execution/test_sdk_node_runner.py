from __future__ import annotations

from unittest.mock import patch

from forge.adapters.execution.sdk_execution_adapter import ExecutionResult
from forge.adapters.execution.sdk_node_runner import run_sdk_node
from forge.models.permissions import PermissionProfile


def _profile() -> PermissionProfile:
    return PermissionProfile(name="test", allowed_tools=["Read", "Edit"])


async def _fake_run_execution_attempt(*, prompt, profile, worktree_path, model, max_turns=None):
    return ExecutionResult(messages=[object(), object()], decisions=[])


def test_successful_session_reports_completed():
    with patch(
        "forge.adapters.execution.sdk_node_runner.run_execution_attempt",
        side_effect=_fake_run_execution_attempt,
    ):
        outcome = run_sdk_node("claude-sonnet-5", "do the thing", _profile(), "/tmp/wt")

    assert outcome.completed is True
    assert "2 message" in outcome.summary


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
