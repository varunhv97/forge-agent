from __future__ import annotations

import asyncio
import queue
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookInput,
    HookMatcher,
    Message,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import ServerToolResultBlock, ServerToolUseBlock, SyncHookJSONOutput

from forge.controller.execution_registry import EffortSwapRequest
from forge.isolation.hooks import (
    HookDecision,
    PreToolUseHook,
    ToolCallEvent,
    build_pretooluse_hook,
)
from forge.models.common import EffortLevel
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
    effort: EffortLevel | None = None,
    resume: str | None = None,
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

    `effort`, when given, is passed straight through to the real SDK's own
    `ClaudeAgentOptions.effort` field. This is a deliberate correction of
    an initial assumption going into this feature (that effort lived
    *inside* `ClaudeAgentOptions.thinking: ThinkingConfig`) — confirmed
    false by reading `claude_agent_sdk.types` directly:
    `ClaudeAgentOptions.effort: EffortLevel | None` is its own top-level
    field, entirely separate from `thinking` (which only ever controls
    adaptive/fixed-budget/disabled extended thinking, with no `effort`
    sub-key at all). The SDK's own `EffortLevel` is
    `Literal["low", "medium", "high", "xhigh", "max"]` — exactly
    `forge.models.common.EffortLevel`'s literal values, so this is a
    trivial identity mapping, not a translation into a different shape.
    `thinking` itself is untouched here; with `effort=None` (no
    `default_effort` configured for the node type), this call is
    byte-for-byte what it was before this feature existed.

    `resume`, when given, is passed straight through to the real SDK's own
    `ClaudeAgentOptions.resume: str | None` field — "Session ID to resume.
    Loads the conversation history from the specified session," confirmed
    from `claude_agent_sdk.types` directly. It's a plain top-level field,
    independent of `model`/`effort` (no mutual-exclusivity or coupling
    with either, unlike `continue_conversation`, which `resume` *is*
    documented as mutually exclusive with — never set here). `None` (the
    default) leaves it unset, same as before this parameter existed; this
    is what a fresh (non-resumed) attempt always passes.
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
        effort=effort,
        resume=resume,
    )


@dataclass
class ExecutionResult:
    """What one execution attempt produced, plus its independent decision log."""

    messages: list[Any] = field(default_factory=list)
    decisions: list[DecisionLogEntry] = field(default_factory=list)
    session_id: str | None = None
    # Set when this attempt was interrupted by a user-requested effort (and
    # optionally model) swap — see `run_execution_attempt`'s own docstring
    # and `EffortSwapRequest`'s. `None` for every ordinary attempt.
    swap_requested: EffortSwapRequest | None = None

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


# -- live trace rendering -----------------------------------------------------
#
# `render_live_message` turns one real `claude_agent_sdk` message into a
# short list of human-legible lines, for the TUI's live trace window (M9
# post-M9 phase 3) — a window into the running session, not a raw protocol
# dump. Kept in this module rather than `forge/tui/*` specifically so the
# project's existing rule holds: only `forge/adapters/execution/*` ever
# imports `claude_agent_sdk` types directly (see `forge/models/common.py`'s
# own `EffortLevel` docstring for the same principle applied to a type
# instead of a function); `forge/tui/screens.py` only imports this plain
# function, never a real SDK type.

_LIVE_LABEL_WIDTH = 11
_LIVE_TEXT_LIMIT = 160


def _collapse_and_truncate(text: str, limit: int = _LIVE_TEXT_LIMIT) -> str:
    """Collapses embedded newlines/repeated whitespace to single spaces (so
    one message always renders as one line here, matching this window's own
    "abbreviated, not a transcript" goal) and truncates past `limit`."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _live_line(label: str, rest: str) -> str:
    # The explicit trailing space (not folded into `_LIVE_LABEL_WIDTH`'s own
    # padding) is deliberate: "tool result" is itself exactly 11 characters,
    # so a plain `f"{label:<11}{rest}"` would butt right up against `rest`
    # with no gap at all for that one label — confirmed by actually looking
    # at a rendered screenshot, not assumed. This guarantees at least one
    # space between every label and its content, even the longest one.
    return f"{label:<{_LIVE_LABEL_WIDTH}} {rest}"


def _tool_call_summary(name: str, tool_input: Mapping[str, Any]) -> str:
    """
    A short one-line summary of a tool call's *key* argument, not its full
    raw JSON input — the command for Bash, the path for a file tool, or
    (for anything else) just its first argument, truncated. Reuses
    `_PATH_INPUT_KEY_BY_TOOL`, the same per-tool key map
    `tool_call_event_from_hook_input` already uses for the real permission
    check, so this never drifts from what that enforcement path considers
    "the" argument for a given tool.
    """
    if name == "Bash":
        command = tool_input.get("command")
        return _collapse_and_truncate(str(command)) if command else ""
    path_key = _PATH_INPUT_KEY_BY_TOOL.get(name)
    if path_key and tool_input.get(path_key):
        return _collapse_and_truncate(str(tool_input[path_key]))
    if not tool_input:
        return ""
    first_key, first_value = next(iter(tool_input.items()))
    return _collapse_and_truncate(f"{first_key}={first_value}")


def _tool_result_text(content: str | list[dict[str, Any]] | dict[str, Any] | None) -> str:
    """Flattens a tool result's `content` (a bare string, or the API's
    list-of-content-block-dicts shape) down to its text, if any — opaque
    non-text content (e.g. an image block) contributes nothing rather than
    a raw dict dump."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text", "")) if content.get("type") == "text" else ""
    parts = [str(block.get("text", "")) for block in content if block.get("type") == "text"]
    return " ".join(part for part in parts if part)


def render_live_message(message: Message) -> list[str]:
    """
    Renders one message yielded by `ClaudeSDKClient.receive_response()` as
    zero or more short, legible lines — the abbreviated view the live trace
    window shows, not the full transcript (that's still one `claude --resume
    <session_id>` away). Deliberately narrow: assistant text, a tool call (name
    + key-argument summary), and a tool result (pass/fail + a short summary)
    are the three kinds explicitly called for; a `ThinkingBlock` gets a
    one-word marker (its content is rarely useful abbreviated, and showing
    none at all would look like a stall); a terminal `ResultMessage` gets a
    one-line "turn finished" marker; everything else (e.g. a `SystemMessage`,
    internal CLI plumbing) renders nothing, not an empty/placeholder line.
    """
    lines: list[str] = []
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                lines.append(_live_line("text", _collapse_and_truncate(block.text)))
            elif isinstance(block, ThinkingBlock):
                lines.append(_live_line("thinking", "…"))
            elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                summary = _tool_call_summary(block.name, block.input)
                rest = f"{block.name} — {summary}" if summary else block.name
                lines.append(_live_line("tool call", rest))
    elif isinstance(message, UserMessage):
        content = message.content
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            if isinstance(block, (ToolResultBlock, ServerToolResultBlock)):
                is_error = getattr(block, "is_error", None)
                status = "failed" if is_error else "ok"
                text = _collapse_and_truncate(_tool_result_text(block.content))
                rest = f"{status} - {text}" if text else status
                lines.append(_live_line("tool result", rest))
    elif isinstance(message, ResultMessage):
        lines.append(_live_line("turn done", message.subtype))
    return lines


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


async def _watch_model_swap(
    client: ClaudeSDKClient,
    model_swap_queue: queue.Queue[str],
    stop_event: asyncio.Event,
    current_model: list[str],
) -> None:
    """
    Polls `model_swap_queue` for a user-requested model change and applies
    it to `client`'s already-open session via `client.set_model()` — a
    real, public, documented SDK method ("Change the AI model during
    conversation (only works with streaming mode)"), confirmed from
    `claude_agent_sdk.client.ClaudeSDKClient.set_model` directly: no
    interrupt, no disconnect, no reconnect — it applies going forward
    within this same session, exactly like `_forward_chat_messages` calls
    `client.query()` on the same live client. This is the fast path; an
    effort change has no such in-place method at all (see
    `_watch_effort_swap`) and needs the much heavier interrupt-and-
    reconnect path instead.

    Same non-blocking `get_nowait()` poll shape and interval as
    `_forward_chat_messages`, for the identical reason: this must never
    block the receive loop. Keeps polling for the rest of the attempt
    (not just once) — a person can change their mind about the model more
    than once within one attempt, and each one is a fresh, independent
    `set_model()` call.

    `current_model` is a one-element mutable list (plain closures can't be
    reassigned from inside a coroutine the way a `nonlocal` local could,
    and this needs to be shared with `_watch_effort_swap`, which isn't a
    nested closure) holding this attempt's currently-live model, seeded
    with its original dispatched model by the caller and updated here
    after every successful `set_model()` call. This is what lets a model
    swap that fires before an effort swap (see `run_execution_attempt`'s
    own docstring on the two queues racing) survive into the effort
    swap's own reconnect instead of being silently lost.
    """
    while not stop_event.is_set():
        try:
            model = model_swap_queue.get_nowait()
        except queue.Empty:
            await asyncio.sleep(_CHAT_POLL_INTERVAL_SECONDS)
            continue
        await client.set_model(model)
        current_model[0] = model


# How long `_watch_effort_swap` waits, after calling `client.interrupt()`,
# for the receive loop to end on its own via a terminal `ResultMessage`
# before giving up and letting `run_execution_attempt`'s own
# `finally: await client.disconnect()` proceed regardless. This is a
# documented-behavior assumption with a bounded fallback, not blind trust:
# `ClaudeSDKClient.interrupt()`'s docstring and `ResultMessage.
# terminal_reason`'s own docstring (confirmed from `claude_agent_sdk.types`
# directly — a value of "aborted_streaming"/"aborted_tools" "indicates the
# turn was cancelled") both say an interrupted turn still ends with a
# terminal ResultMessage the same way an ordinary turn does, which is what
# this code is built to rely on — and which the fake ClaudeSDKClient double
# in this module's test suite is built to model, not just assert. 30
# seconds is a generous multiple of how long a real interrupt should ever
# actually take (the CLI aborting whatever tool call/turn is in flight and
# writing its final result frame — a local, in-process operation, not a
# network round trip) while still bounding one swap request against a hung
# session if that documented behavior doesn't hold for some real session:
# without this, a stuck interrupt would block this attempt (and the
# scheduler thread driving it) forever.
_INTERRUPT_RESULT_TIMEOUT_SECONDS = 30.0


async def _watch_effort_swap(
    client: ClaudeSDKClient,
    effort_swap_queue: queue.Queue[EffortSwapRequest],
    stop_event: asyncio.Event,
    triggered: list[EffortSwapRequest],
    current_model: list[str],
    receive_task: asyncio.Task[None],
) -> None:
    """
    Polls `effort_swap_queue` for a user-requested effort (and optionally
    bundled model) change. There is no SDK control request for changing
    effort mid-session — confirmed by reading every method in
    `claude_agent_sdk._internal.query.Query`'s control-request section
    (`interrupt`, `set_permission_mode`, `set_model`, `rewind_files`,
    `reconnect_mcp_server`, `get_mcp_status`, `get_context_usage` —
    nothing effort-related). A real change needs a *new* session built
    with `effort=` set at `ClaudeAgentOptions` construction time, which
    only `dispatch_execution_attempt`'s *next* dispatch for this task can
    do (via `resume_session_id`) — this function only does its own half:
    interrupt the current turn and let the receive loop end on its own.
    It never reconnects itself.

    `client.interrupt()` itself only sends the interrupt control request
    and waits for the CLI's acknowledgement of *that request* — confirmed
    from `ClaudeSDKClient.interrupt()`/`Query.interrupt()`'s source: it's
    a bare `_send_control_request({"subtype": "interrupt"})`, nothing
    more. It does not itself wait for the turn to actually end. The turn's
    own end still arrives later as an ordinary message on the same stream
    `_receive()` is already consuming — a terminal `ResultMessage` whose
    `terminal_reason` the SDK documents as `"aborted_streaming"`/
    `"aborted_tools"` for exactly this case — so `_receive()`'s existing
    `finally: stop_event.set()` is expected to fire the same way it does
    for a normal turn, with nothing special-cased here for it. The
    `asyncio.wait_for` below is this function's own acknowledgement that
    this is *confirmed* behavior (see `_INTERRUPT_RESULT_TIMEOUT_SECONDS`'s
    own comment), not something contractually guaranteed: if a real
    session's CLI process doesn't produce that terminal message promptly,
    merely giving up on *this function's own wait* would not be enough —
    `run_execution_attempt`'s top level is blocked on `await receive_task`
    (the task driving `_receive()`/`client.receive_response()`), which
    only a `ResultMessage` or an exception out of that generator would
    ever unblock. So the timeout here also directly cancels `receive_task`
    itself, which is what actually frees `run_execution_attempt` to reach
    its `finally: await client.disconnect()` — the one further step,
    without this, that a real hung interrupt would have blocked forever.
    `receive_task`'s own cancellation still runs `_receive()`'s `finally:
    stop_event.set()` on the way out (cancellation propagates through
    `finally` blocks), so anything else waiting on `stop_event` is
    released too, consistently.

    Only the first swap request this attempt sees is acted on — once one
    fires, the interrupt is already in flight and the turn is ending;
    anything else queued is left for the task's next dispatch to pick up
    fresh rather than trying to layer a second interrupt onto an
    already-ending turn.

    `current_model` is the same one-element mutable list `_watch_model_swap`
    updates after every `set_model()` call, seeded with this attempt's
    original dispatched model. When the fired request carries no `model`
    of its own (the common case — a person who only wants to change
    effort), the triggered record defaults to whatever `current_model`
    holds at that moment, rather than `None`: this is what makes a model
    swap that fired earlier in the same attempt survive into the new
    session an effort swap's own reconnect starts (see
    `run_execution_attempt`'s own docstring on the two queues racing), and
    it's also simply the right default when no model swap happened at all
    — "change effort, leave the model alone" should leave the model alone,
    not silently fall back to whatever the node type's/task's own default
    would otherwise have been for a *fresh* dispatch.
    """
    while not stop_event.is_set():
        try:
            request = effort_swap_queue.get_nowait()
        except queue.Empty:
            await asyncio.sleep(_CHAT_POLL_INTERVAL_SECONDS)
            continue
        triggered.append(
            request
            if request.model is not None
            else EffortSwapRequest(effort=request.effort, model=current_model[0])
        )
        await client.interrupt()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_INTERRUPT_RESULT_TIMEOUT_SECONDS)
        except TimeoutError:
            # The documented behavior this function otherwise relies on
            # didn't hold for this session — force the receive loop to
            # end rather than leave run_execution_attempt blocked on
            # `await receive_task` forever (see this function's own
            # docstring for why cancelling *this* task's own wait alone
            # would not have been enough).
            receive_task.cancel()
        return


async def run_execution_attempt(
    *,
    prompt: str,
    profile: PermissionProfile,
    worktree_path: str,
    model: str,
    max_turns: int | None = None,
    chat_queue: queue.Queue[str] | None = None,
    effort: EffortLevel | None = None,
    live_messages: queue.Queue[Any] | None = None,
    model_swap_queue: queue.Queue[str] | None = None,
    effort_swap_queue: queue.Queue[EffortSwapRequest] | None = None,
    resume_session_id: str | None = None,
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

    `effort`, when given, is forwarded to `build_claude_agent_options` and
    ends up on the real `ClaudeAgentOptions.effort` field — see that
    function's docstring for why this is `effort`, not `thinking`. `None`
    (the default) leaves `ClaudeAgentOptions.effort` unset, same as before
    this parameter existed.

    `live_messages`, when given, gets every message `receive_response()`
    yields pushed onto it (`put_nowait`, never blocking the receive loop)
    the moment it's appended to `messages` — the live-trace counterpart to
    `chat_queue`, same cross-thread-`queue.Queue` shape, opposite
    direction: `forge/tui/runner.py`'s `RunWorker` is what drains this one,
    from the main thread, while this coroutine keeps running on its own
    background thread's event loop. `None` (the default) means nothing is
    pushed anywhere — byte-for-byte what this function did before this
    parameter existed.

    `model_swap_queue`/`effort_swap_queue`, when given, run two more
    concurrent tasks (`_watch_model_swap`/`_watch_effort_swap`) alongside
    the receive loop and, when `chat_queue` is given, the chat-forwarding
    task too — all poll with the same non-blocking `get_nowait()` shape,
    so any combination of these can run together without one blocking
    another or the receive loop itself. `model_swap_queue` carries a plain
    model string, applied in place via `client.set_model()` — the fast
    path, no interruption. `effort_swap_queue` carries a validated
    `EffortSwapRequest`; there is no in-place way to change effort, so
    handling one means calling `client.interrupt()` and letting this
    receive loop end on its own (see `_watch_effort_swap`'s own docstring
    for exactly what's confirmed, not assumed, about that), at which point
    this function returns with `ExecutionResult.swap_requested` set — the
    signal `dispatch_execution_attempt` uses to record this attempt as
    `AttemptStatus.INTERRUPTED_FOR_SWAP` instead of PASSED/FAILED, and to
    arrange for this task's *next* dispatch to resume this exact session
    with the requested model/effort. If both an effort and a model swap
    request arrive in close succession, whichever the polling loops happen
    to observe first wins: a model swap already applied via `set_model()`
    survives into the new session started by the effort swap's own
    interrupt-and-reconnect (since that new session is instructed with
    whatever model `EffortSwapRequest.model` carries — the person's model
    choice is never silently lost by also changing effort); an effort swap
    that fires first ends the turn (and this function) before a
    model-swap request still sitting on `model_swap_queue` is ever drained
    — left for the task's next (resumed) dispatch to pick up a model_swap_
    queue request from, same as any other message that loses this race.
    `None` for both (the default, and what every caller outside the TUI's
    new modal uses) means neither task is spawned at all — byte-for-byte
    what this function did before either parameter existed.

    `resume_session_id`, when given, is forwarded to
    `build_claude_agent_options` as `resume`, landing on the real
    `ClaudeAgentOptions.resume` field — this is what actually continues a
    prior session rather than starting a fresh one; see that function's
    own docstring for the SDK-source confirmation that `resume` is
    independent of `model`/`effort`. `None` (the default) leaves it unset,
    same as every dispatch before this parameter existed.

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
        effort=effort,
        resume=resume_session_id,
    )
    messages: list[Any] = []
    triggered_swaps: list[EffortSwapRequest] = []
    # Seeded with this attempt's own dispatched model, updated by
    # `_watch_model_swap` after every successful `set_model()` call — see
    # both functions' own docstrings for why `_watch_effort_swap` reads it.
    current_model: list[str] = [model]
    client = ClaudeSDKClient(options=options)
    await client.connect(prompt)
    try:
        stop_event = asyncio.Event()

        async def _receive() -> None:
            try:
                async for message in client.receive_response():
                    messages.append(message)
                    if live_messages is not None:
                        # Unbounded by design (see `RunWorker.live_messages`'s
                        # own docstring for the full reasoning): one attempt's
                        # turn yields a small, human-paced number of discrete
                        # messages (not raw token deltas), so `put_nowait`
                        # here can never raise `queue.Full` and never blocks
                        # this loop on a slow/absent drainer.
                        live_messages.put_nowait(message)
            finally:
                stop_event.set()

        # One receive task plus zero or more auxiliary tasks (chat
        # forwarding, model swap, effort swap) — every combination of
        # queues given spawns exactly the auxiliary tasks it needs,
        # running concurrently on this one event loop with the receive
        # loop, never blocking it (every auxiliary task only ever does a
        # non-blocking queue poll between awaits). With none given at all
        # (every caller outside the TUI), this is a plain `ClaudeSDKClient`
        # turn with nothing auxiliary running — the observably-identical
        # baseline the original chat-only version of this swap was proven
        # against first.
        receive_task = asyncio.ensure_future(_receive())
        auxiliary_tasks: list[asyncio.Task[None]] = []
        if chat_queue is not None:
            auxiliary_tasks.append(
                asyncio.ensure_future(_forward_chat_messages(client, chat_queue, stop_event))
            )
        if model_swap_queue is not None:
            auxiliary_tasks.append(
                asyncio.ensure_future(
                    _watch_model_swap(client, model_swap_queue, stop_event, current_model)
                )
            )
        if effort_swap_queue is not None:
            auxiliary_tasks.append(
                asyncio.ensure_future(
                    _watch_effort_swap(
                        client,
                        effort_swap_queue,
                        stop_event,
                        triggered_swaps,
                        current_model,
                        receive_task,
                    )
                )
            )
        try:
            await receive_task
        except asyncio.CancelledError:
            # Only reachable via _watch_effort_swap's own timeout
            # fallback deliberately cancelling receive_task when an
            # interrupted turn never produced a terminal ResultMessage —
            # see that function's docstring. Swallowed here, not
            # re-raised: this is this function's own documented recovery
            # from an undocumented SDK behavior, not an external
            # cancellation of run_execution_attempt itself that a caller
            # would need to see.
            pass
        finally:
            for task in auxiliary_tasks:
                task.cancel()
            for task in auxiliary_tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001, S110 - an auxiliary-task failure
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
        swap_requested=triggered_swaps[0] if triggered_swaps else None,
    )
