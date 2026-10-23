from __future__ import annotations

import asyncio
import queue

from forge.adapters.execution.sdk_execution_adapter import run_execution_attempt
from forge.controller.execution_registry import ExecutionRunOutcome
from forge.models.permissions import PermissionProfile


def run_sdk_node(
    model: str,
    prompt: str,
    profile: PermissionProfile,
    worktree_path: str,
    *,
    chat_queue: queue.Queue[str] | None = None,
) -> ExecutionRunOutcome:
    """
    Bridges the scheduler's synchronous `ExecutionRunner` contract to the
    Claude Agent SDK adapter's async `run_execution_attempt` — the only
    place in this project that calls `asyncio.run` to cross that boundary,
    same pattern used for the decision adapter. `completed=False` here
    means the session itself failed to run at all (network, auth, the SDK
    raising); it says nothing about whether the work was any good — the
    dispatcher derives that mechanically from git afterward, not from
    this function's own judgment.

    `chat_queue`, when given, is handed straight through to
    `run_execution_attempt` so a message put on it from another thread
    (see `forge/tui/runner.py::RunWorker`) can be forwarded into this
    same attempt's live session mid-turn. `None` (the default, and the
    only thing every caller outside the TUI ever passes) behaves exactly
    as before this parameter existed.
    """
    try:
        result = asyncio.run(
            run_execution_attempt(
                prompt=prompt,
                profile=profile,
                worktree_path=worktree_path,
                model=model,
                chat_queue=chat_queue,
            )
        )
    except Exception as exc:  # noqa: BLE001 - any adapter failure is a failed attempt, by contract
        return ExecutionRunOutcome(completed=False, summary=f"SDK execution failed: {exc}")

    summary = f"session completed with {len(result.messages)} message(s)"
    if result.denied_calls:
        summary += f"; {len(result.denied_calls)} tool call(s) denied by permission profile"
    return ExecutionRunOutcome(completed=True, summary=summary)
