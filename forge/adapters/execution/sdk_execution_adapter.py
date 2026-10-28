from __future__ import annotations

import asyncio
import queue
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookInput,
    HookMatcher,
    ResultMessage,
)
from claude_agent_sdk.types import SyncHookJSONOutput

from forge.isolation.hooks import (
    HookDecision,
    PreToolUseHook,
    ToolCallEvent,
    build_pretooluse_hook,
)
from forge.models.permissions import PermissionProfile

# Per built-in tool, which key in its `tool_input` dict holds the file path
# it touches. Must track the Claude Agent SDK's actual tool schemas —
# NotebookEdit uses "notebook_path", unlike Edit/Write/Read, which is why
# this can't just be a single constant key name.
_PATH_INPUT_KEY_BY_TOOL = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}


@dataclass(frozen=True)
class DecisionLogEntry:
    """One recorded PreToolUse decision, independent of the session transcript."""

    event: ToolCallEvent
    decision: HookDecision


def tool_call_event_from_hook_input(hook_input: Mapping[str, Any]) -> ToolCallEvent:
    """
    Translates a Claude Agent SDK `PreToolUseHookInput` payload (a plain
    dict at the point a hook callback receives it) into the adapter-agnostic
    `ToolCallEvent` the policy engine in `forge.isolation.hooks` understands.

    This is the one place that needs to know the real SDK's tool_input
    field names (`command` for Bash, `file_path`/`notebook_path` for the
    filesystem tools) — the policy engine itself never does.
    """
    tool_name = hook_input["tool_name"]
    tool_input = hook_input.get("tool_input") or {}
    command = tool_input.get("command") if tool_name == "Bash" else None
    path_key = _PATH_INPUT_KEY_BY_TOOL.get(tool_name)
    path = tool_input.get(path_key) if path_key else None
    return ToolCallEvent(tool_name=tool_name, command=command, path=path)


def hook_json_output_from_decision(decision: HookDecision) -> SyncHookJSONOutput:
    """
    Translates a `HookDecision` back into the SDK's expected PreToolUse hook
    response shape: `{"hookSpecificOutput": {"hookEventName": "PreToolUse",
    "permissionDecision": "allow" | "deny", ...}}`.
    """
    if decision.allowed:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": decision.reason or "denied by permission profile",
        }
    }


def make_pretooluse_hook_callback(
    policy: PreToolUseHook,
    decision_log: list[DecisionLogEntry] | None = None,
) -> Callable[[HookInput, str | None, HookContext], Coroutine[Any, Any, SyncHookJSONOutput]]:
    """
    Wraps a policy-engine hook (`ToolCallEvent -> HookDecision`, built by
    `forge.isolation.hooks.build_pretooluse_hook`) as a Claude Agent SDK
    `HookCallback`: an async function taking `(hook_input, tool_use_id,
    context)` and returning the SDK's PreToolUse hook JSON output.

    If `decision_log` is given, every decision is appended to it as it's
    made — this is what lets a caller later assert on exactly what was
    allowed or denied, independent of whatever the model's own transcript
    says happened.
    """

    async def callback(
        hook_input: HookInput,
        _tool_use_id: str | None,
        _context: HookContext,
    ) -> SyncHookJSONOutput:
        event = tool_call_event_from_hook_input(hook_input)
        decision = policy(event)
        if decision_log is not None:
            decision_log.append(DecisionLogEntry(event=event, decision=decision))
        return hook_json_output_from_decision(decision)

    return callback


def build_claude_agent_options(
    *,
    profile: PermissionProfile,
    worktree_path: str,
    model: str,
    max_turns: int | None = None,
    decision_log: list[DecisionLogEntry] | None = None,
) -> ClaudeAgentOptions:
    """
    Builds the `ClaudeAgentOptions` for one execution attempt: scoped to its
    worktree, restricted to the task's allowed tools, and with a PreToolUse
    hook that enforces the full permission profile on every tool call.

    The hook is registered with `matcher=None`, which the SDK documents as
    matching every tool — this is deliberate: PreToolUse hooks are the only
    point in the SDK's permission pipeline that runs unconditionally,
    before `permission_mode` or `allowed_tools` are even consulted, which is
    exactly the property this project's enforcement story depends on. The
    task's own prompt is never trusted to stay within `profile` on its own.
    """
    policy = build_pretooluse_hook(profile, worktree_path)
    callback = make_pretooluse_hook_callback(policy, decision_log=decision_log)

    # The SDK's own `allowed_tools` works on plain tool names, not the
    # `Bash(<glob>)` command-pattern entries `profile.allowed_tools` can
    # contain — that finer-grained matching is enforced by the hook above,
    # not by the SDK's native allowlist. Here we only need the tool names.
    tool_names = sorted({entry.split("(", 1)[0] for entry in profile.allowed_tools})

    return ClaudeAgentOptions(
        cwd=worktree_path,
        allowed_tools=tool_names,
        permission_mode="default",
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[callback])]},
        model=model,
        max_turns=max_turns,
    )


@dataclass
class ExecutionResult:
    """What one execution attempt produced, plus its independent decision log."""

    messages: list[Any] = field(default_factory=list)
    decisions: list[DecisionLogEntry] = field(default_factory=list)
    session_id: str | None = None

    @property
    def denied_calls(self) -> list[DecisionLogEntry]:
        return [entry for entry in self.decisions if not entry.decision.allowed]


def _session_id_from_messages(messages: list[Any]) -> str | None:
    """
    Pulls the real Claude Agent SDK session id off the terminal
    `ResultMessage` in a drained message list, if one is present.
    `ResultMessage.session_id` is a typed field the SDK's `ResultMessage`
    dataclass carries on every completed turn (confirmed from
    `claude_agent_sdk.types`); searched from the end since a `ResultMessage`
    is expected to be the last message of a turn, but nothing here assumes
    exactly one is present — the first one found scanning backward is used,
    and `None` is returned if the turn produced no `ResultMessage` at all
    (shouldn't normally happen, but this must never raise for it).
    """
    for message in reversed(messages):
        if isinstance(message, ResultMessage):
            return message.session_id
    return None


# How often `_forward_chat_messages` polls the cross-thread `queue.Queue`
# for a new chat message, in seconds. A short non-blocking `get_nowait()`
# poll rather than `queue.Queue.get(timeout=...)` — that call blocks the
# *calling* thread (here, the event loop thread the receive loop also
# runs on) for the whole timeout regardless of what else needs to run,
# which is exactly the "must not block the receive loop" constraint this
# is standing in for. Short enough that a chat message sent mid-turn
# reaches `client.query()` well before an ordinary turn's own response
# finishes.
_CHAT_POLL_INTERVAL_SECONDS = 0.05


async def _forward_chat_messages(
    client: ClaudeSDKClient,
    chat_queue: queue.Queue[str],
    stop_event: asyncio.Event,
) -> None:
    """
    Polls `chat_queue` for a free-form chat message and sends each one into
    `client`'s already-open session via `client.query()` — the *same*
    client instance the receive loop in `run_execution_attempt` is reading
    from, which is what actually lets a chat message join the live session
    rather than starting a separate one. `chat_queue` is a plain
    `queue.Queue`, filled from a different thread than the one running
    this coroutine's event loop (`forge/tui/runner.py`'s `RunWorker` sets
    it up per attempt; the TUI's main thread puts messages onto it) —
    inherently thread-safe for put/get, so no extra locking is needed here.

    Stops as soon as `stop_event` is set, which happens the moment the
    receive loop's own turn finishes (see `run_execution_attempt`).
    Anything still sitting on the queue after that is left alone — it's
    `RunWorker`'s job to decide what happens to a message that loses this
    race, not this function's.
    """
    while not stop_event.is_set():
        try:
            message = chat_queue.get_nowait()
        except queue.Empty:
            await asyncio.sleep(_CHAT_POLL_INTERVAL_SECONDS)
            continue
        await client.query(message)


async def run_execution_attempt(
    *,
    prompt: str,
    profile: PermissionProfile,
    worktree_path: str,
    model: str,
    max_turns: int | None = None,
    chat_queue: queue.Queue[str] | None = None,
) -> ExecutionResult:
    """
    Runs one execution node's prompt through a live Claude Agent SDK
    session, scoped to `worktree_path` and enforced by `profile`.

    Uses `ClaudeSDKClient` (persistent, bidirectional) rather than the
    one-shot `query()` function specifically so a chat message can be
    injected into the *same* open session mid-attempt: `connect(prompt)`
    opens it and sends the initial message, `receive_response()` streams
    that turn's output to completion — draining its generator the exact
    same way `query()`'s was drained before this swap — and `disconnect()`
    closes it afterward, all inside the one `asyncio.run()` call
    `sdk_node_runner.run_sdk_node` already wraps this in; nothing here
    changes when that event loop starts or stops relative to the
    scheduler's dispatch call.

    The returned `ExecutionResult.session_id` is this session's real
    Claude Agent SDK session id, pulled from the terminal `ResultMessage`
    in `messages` by `_session_id_from_messages` (`None` if no
    `ResultMessage` was ever produced). Forge only captures and persists
    this value for later use with `claude --resume <session_id>` run by a
    person themselves — nothing here acts on it.

    When `chat_queue` is given, `_forward_chat_messages` runs concurrently
    with the receive loop (two tasks, one event loop) and calls
    `client.query()` on this same client for every message that arrives on
    it before the turn finishes. With no queue at all (the default), this
    is a plain `ClaudeSDKClient` turn with nothing injected — the
    observably-identical baseline this swap was proven against before chat
    was layered on top of it. A failure in the chat-forwarding task itself
    (e.g. the transport having already gone away) is swallowed rather than
    allowed to replace whatever the receive loop itself produced — chat is
    additive to the attempt, never a way for it to fail.

    This makes a real call to the Anthropic API via the bundled Claude Code
    CLI subprocess and therefore requires network access and either
    `ANTHROPIC_API_KEY` or an authenticated `claude` CLI session, which is
    why this function has no accompanying live-call test in the offline
    suite (`scripts/live_smoke_test.py` exercises it live). Every other
    piece of this module (the translation functions and
    `build_claude_agent_options`) is tested without the network, since
    they're pure logic over plain data; the concurrency this function adds
    is tested instead against a fake `ClaudeSDKClient` double — see
    `tests/adapters/execution/test_sdk_execution_adapter.py` for what is
    and isn't covered, and why.
    """
    decision_log: list[DecisionLogEntry] = []
    options = build_claude_agent_options(
        profile=profile,
        worktree_path=worktree_path,
        model=model,
        max_turns=max_turns,
        decision_log=decision_log,
    )
    messages: list[Any] = []
    client = ClaudeSDKClient(options=options)
    await client.connect(prompt)
    try:
        stop_event = asyncio.Event()

        async def _receive() -> None:
            try:
                async for message in client.receive_response():
                    messages.append(message)
            finally:
                stop_event.set()

        if chat_queue is None:
            await _receive()
        else:
            receive_task = asyncio.ensure_future(_receive())
            chat_task = asyncio.ensure_future(
                _forward_chat_messages(client, chat_queue, stop_event)
            )
            try:
                await receive_task
            finally:
                chat_task.cancel()
                try:
                    await chat_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001, S110 - a chat-forwarding failure
                    # (e.g. the transport already gone) must never mask or
                    # replace the receive loop's own result for this attempt;
                    # see this function's docstring.
                    pass
    finally:
        await client.disconnect()
    return ExecutionResult(
        messages=messages,
        decisions=decision_log,
        session_id=_session_id_from_messages(messages),
    )
