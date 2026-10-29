from __future__ import annotations

import asyncio
import queue
from typing import Any

from forge.adapters.execution.sdk_execution_adapter import run_execution_attempt
from forge.controller.execution_registry import EffortSwapRequest, ExecutionRunOutcome
from forge.models.common import EffortLevel
from forge.models.permissions import PermissionProfile


def run_sdk_node(
    model: str,
    prompt: str,
    profile: PermissionProfile,
    worktree_path: str,
    *,
    chat_queue: queue.Queue[str] | None = None,
    effort: EffortLevel | None = None,
    live_messages: queue.Queue[Any] | None = None,
    model_swap_queue: queue.Queue[str] | None = None,
    effort_swap_queue: queue.Queue[EffortSwapRequest] | None = None,
    resume_session_id: str | None = None,
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

    `effort`, when given, is handed straight through to
    `run_execution_attempt` too, keyword-only and `None` by default, same
    shape as `chat_queue` — the scheduler's `select_effort` is what
    actually decides the value; this bridge just forwards it unchanged.

    `live_messages`, when given, is handed straight through too — the
    live-trace counterpart to `chat_queue` (opposite direction: this is
    where messages flow *out* of the session as they arrive, not where a
    chat message flows in), same keyword-only, `None`-by-default shape.
    This bridge has no logic of its own for any of these three; it just
    forwards them.

    `ExecutionRunOutcome.session_id` is threaded straight through from
    `result.session_id` (the real Claude Agent SDK session id off the
    turn's terminal `ResultMessage`, or `None` if there wasn't one) — this
    is the one new thing this bridge adds for the `forge inspect`
    session-id feature; everything else about this function is unchanged.

    `model_swap_queue`/`effort_swap_queue`/`resume_session_id` are each
    handed straight through too, same keyword-only, `None`-by-default
    shape as the three above — this bridge has no swap logic of its own,
    it just forwards them to `run_execution_attempt`. `ExecutionRunOutcome.
    swap_requested` is threaded straight through from `result.
    swap_requested` the same way `session_id` already is — `None` for an
    ordinary pass/fail attempt, set only when this attempt was interrupted
    for a user-requested effort swap (see `EffortSwapRequest`'s own
    docstring).
    """
    try:
        result = asyncio.run(
            run_execution_attempt(
                prompt=prompt,
                profile=profile,
                worktree_path=worktree_path,
                model=model,
                chat_queue=chat_queue,
                effort=effort,
                live_messages=live_messages,
                model_swap_queue=model_swap_queue,
                effort_swap_queue=effort_swap_queue,
                resume_session_id=resume_session_id,
            )
        )
    except Exception as exc:  # noqa: BLE001 - any adapter failure is a failed attempt, by contract
        return ExecutionRunOutcome(completed=False, summary=f"SDK execution failed: {exc}")

    summary = f"session completed with {len(result.messages)} message(s)"
    if result.denied_calls:
        summary += f"; {len(result.denied_calls)} tool call(s) denied by permission profile"
    return ExecutionRunOutcome(
        completed=True,
        summary=summary,
        session_id=result.session_id,
        swap_requested=result.swap_requested,
    )
