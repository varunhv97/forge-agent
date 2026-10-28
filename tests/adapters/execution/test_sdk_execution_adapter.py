from __future__ import annotations

import asyncio
import queue
from collections.abc import Awaitable, Mapping
from typing import Any, ClassVar, TypeVar, cast

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import HookJSONOutput, PreToolUseHookInput, PreToolUseHookSpecificOutput

from forge.adapters.execution import sdk_execution_adapter
from forge.adapters.execution.sdk_execution_adapter import (
    DecisionLogEntry,
    build_claude_agent_options,
    hook_json_output_from_decision,
    make_pretooluse_hook_callback,
    render_live_message,
    run_execution_attempt,
    tool_call_event_from_hook_input,
)
from forge.isolation.hooks import HookDecision, build_pretooluse_hook
from forge.models.permissions import PermissionProfile

T = TypeVar("T")


def _profile(**overrides) -> PermissionProfile:
    defaults = {
        "name": "test-profile",
        "allowed_tools": ["Read", "Edit", "Bash(pytest*)"],
        "denied_bash_patterns": ["git push*"],
        "network_allowed": False,
    }
    defaults.update(overrides)
    return PermissionProfile.model_validate(defaults)


def _bash_hook_input(command: str, tool_use_id: str, cwd: str = "/tmp/wt") -> PreToolUseHookInput:
    """A complete PreToolUse payload, shaped exactly as the SDK delivers it."""
    return {
        "session_id": "s1",
        "transcript_path": "/tmp/t",
        "cwd": cwd,
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": tool_use_id,
    }


def _pretooluse_output(output: HookJSONOutput) -> PreToolUseHookSpecificOutput:
    """Asserts the output is a PreToolUse response and narrows it to that shape."""
    specific = cast(Mapping[str, Any], output).get("hookSpecificOutput")
    assert isinstance(specific, dict)
    assert specific.get("hookEventName") == "PreToolUse"
    return cast(PreToolUseHookSpecificOutput, specific)


def _await(awaitable: Awaitable[T]) -> T:
    async def _inner() -> T:
        return await awaitable

    return asyncio.run(_inner())


# --- translation: SDK hook input -> ToolCallEvent -----------------------


def test_bash_command_extracted_from_tool_input():
    event = tool_call_event_from_hook_input(
        {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}}
    )
    assert event.tool_name == "Bash"
    assert event.command == "git push origin main"
    assert event.path is None


def test_read_edit_write_use_file_path_key():
    for tool_name in ("Read", "Edit", "Write"):
        event = tool_call_event_from_hook_input(
            {"tool_name": tool_name, "tool_input": {"file_path": "/a/b.py"}}
        )
        assert event.path == "/a/b.py"
        assert event.command is None


def test_notebook_edit_uses_notebook_path_key_not_file_path():
    event = tool_call_event_from_hook_input(
        {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "/a/nb.ipynb"}}
    )
    assert event.path == "/a/nb.ipynb"


def test_unrecognized_tool_has_no_command_or_path():
    event = tool_call_event_from_hook_input({"tool_name": "Glob", "tool_input": {"pattern": "*"}})
    assert event.tool_name == "Glob"
    assert event.command is None
    assert event.path is None


def test_missing_tool_input_key_handled_gracefully():
    event = tool_call_event_from_hook_input({"tool_name": "Bash"})
    assert event.command is None


# --- translation: HookDecision -> SDK hook output ------------------------


def test_allow_decision_translates_to_allow_output():
    output = _pretooluse_output(hook_json_output_from_decision(HookDecision.allow()))
    assert output["permissionDecision"] == "allow"
    assert "permissionDecisionReason" not in output


def test_deny_decision_translates_to_deny_output_with_reason():
    output = _pretooluse_output(hook_json_output_from_decision(HookDecision.deny("nope")))
    assert output["permissionDecision"] == "deny"
    assert output.get("permissionDecisionReason") == "nope"


# --- the wrapped hook callback, invoked the way the SDK would -------------


def test_hook_callback_denies_and_logs_a_disallowed_bash_command():
    profile = _profile()
    policy = build_pretooluse_hook(profile, worktree_path="/tmp/wt")
    decision_log: list[DecisionLogEntry] = []
    callback = make_pretooluse_hook_callback(policy, decision_log=decision_log)

    output = _pretooluse_output(
        asyncio.run(
            callback(_bash_hook_input("git push origin main", "tu_1"), "tu_1", {"signal": None})
        )
    )

    assert output["permissionDecision"] == "deny"
    assert len(decision_log) == 1
    assert decision_log[0].decision.allowed is False
    assert decision_log[0].event.command == "git push origin main"


def test_hook_callback_allows_a_permitted_bash_command():
    profile = _profile()
    policy = build_pretooluse_hook(profile, worktree_path="/tmp/wt")
    decision_log: list[DecisionLogEntry] = []
    callback = make_pretooluse_hook_callback(policy, decision_log=decision_log)

    output = _pretooluse_output(
        asyncio.run(callback(_bash_hook_input("pytest -q", "tu_2"), "tu_2", {"signal": None}))
    )

    assert output["permissionDecision"] == "allow"
    assert decision_log[0].decision.allowed is True


# --- build_claude_agent_options --------------------------------------------


def test_build_claude_agent_options_scopes_cwd_and_model():
    options = build_claude_agent_options(
        profile=_profile(), worktree_path="/tmp/wt-1", model="claude-sonnet-5", max_turns=10
    )
    assert options.cwd == "/tmp/wt-1"
    assert options.model == "claude-sonnet-5"
    assert options.max_turns == 10
    assert options.permission_mode == "default"


def test_build_claude_agent_options_strips_bash_patterns_to_plain_tool_names():
    options = build_claude_agent_options(
        profile=_profile(allowed_tools=["Read", "Bash(pytest*)", "Bash(ls*)"]),
        worktree_path="/tmp/wt-1",
        model="claude-sonnet-5",
    )
    # the SDK's own allowlist only understands tool names, not the finer
    # Bash(<glob>) command patterns — those are enforced by our hook instead
    assert options.allowed_tools == ["Bash", "Read"]


def test_build_claude_agent_options_sets_effort_when_given():
    """
    The real correction this feature's own investigation surfaced:
    `ClaudeAgentOptions.effort` is its own top-level field (confirmed from
    `claude_agent_sdk.types` directly), entirely separate from `thinking`
    — there is no `effort` key nested inside `ThinkingConfig`. This is the
    one test proving Forge's own `EffortLevel` value actually lands on
    that field.
    """
    options = build_claude_agent_options(
        profile=_profile(), worktree_path="/tmp/wt-1", model="claude-sonnet-5", effort="high"
    )
    assert options.effort == "high"
    # Setting effort must never implicitly touch thinking — they're
    # independent fields on the real SDK type.
    assert options.thinking is None


def test_build_claude_agent_options_leaves_effort_unset_by_default():
    """
    No `effort` argument at all (the default, and what every call site
    outside this feature's own new code path uses) must leave
    `ClaudeAgentOptions.effort` exactly as it was before this feature
    existed: unset, so the model's own default behavior applies.
    """
    options = build_claude_agent_options(
        profile=_profile(), worktree_path="/tmp/wt-1", model="claude-sonnet-5"
    )
    assert options.effort is None


def test_build_claude_agent_options_registers_one_pretooluse_matcher():
    options = build_claude_agent_options(
        profile=_profile(), worktree_path="/tmp/wt-1", model="claude-sonnet-5"
    )
    assert options.hooks is not None
    matchers = options.hooks["PreToolUse"]
    assert len(matchers) == 1
    matcher = matchers[0]
    assert isinstance(matcher, HookMatcher)
    assert matcher.matcher is None  # None matches every tool, per the SDK's own contract
    assert len(matcher.hooks) == 1


def test_build_claude_agent_options_wired_hook_actually_enforces_the_profile():
    """
    End-to-end for everything that doesn't require a live model call: build
    real ClaudeAgentOptions, pull out the exact callback the SDK would
    invoke, and confirm it denies a disallowed command when invoked the way
    the SDK's documented HookCallback contract says it will be. This is the
    offline substitute for the live "model tries git push, hook blocks it"
    acceptance scenario — the one piece this can't cover is whether the
    real `claude` CLI subprocess actually calls this callback as advertised,
    which has no network-free way to verify.
    """
    decision_log: list[DecisionLogEntry] = []
    options = build_claude_agent_options(
        profile=_profile(),
        worktree_path="/tmp/wt-1",
        model="claude-sonnet-5",
        decision_log=decision_log,
    )
    assert options.hooks is not None
    callback = options.hooks["PreToolUse"][0].hooks[0]

    hook_input = _bash_hook_input("git push origin main", "tu_3", cwd="/tmp/wt-1")
    output = _pretooluse_output(_await(callback(hook_input, "tu_3", {"signal": None})))

    assert output["permissionDecision"] == "deny"
    assert decision_log
    assert decision_log[-1].decision.allowed is False


# --- run_execution_attempt: the ClaudeSDKClient swap + mid-session chat ---
#
# These exercise `run_execution_attempt` against a fake standing in for
# `claude_agent_sdk.ClaudeSDKClient` itself (connect/query/receive_response/
# disconnect) rather than a higher-level stand-in for the whole adapter —
# this is what actually proves the one-shot `query()` -> `ClaudeSDKClient`
# swap and the mid-session chat injection work, not just that the
# scheduler eventually reports some `ExecutionRunOutcome`.


def _make_fake_client_class(response_chunks: list[list[Any]], *, turn_delay_seconds: float = 0.1):
    """
    Builds a fresh fake `ClaudeSDKClient` class (its own `instances` list,
    so tests never share state) whose `receive_response()` yields
    `response_chunks` with a deliberate, generous delay before each chunk —
    giving a concurrently-running chat-forwarding loop a real window to
    call `.query()` before the "turn" ends. Generous per this phase's own
    lesson about tight timing racing real concurrency: a flaky failure
    here should mean a real bug, not an unlucky scheduler.
    """

    class _FakeClaudeSDKClient:
        instances: ClassVar[list[Any]] = []

        def __init__(self, options=None):
            self.options = options
            self.calls: list[tuple[str, Any]] = []
            type(self).instances.append(self)

        async def connect(self, prompt):
            self.calls.append(("connect", prompt))

        async def query(self, message):
            self.calls.append(("query", message))

        async def receive_response(self):
            for chunk in response_chunks:
                await asyncio.sleep(turn_delay_seconds)
                for message in chunk:
                    yield message

        async def disconnect(self):
            self.calls.append(("disconnect", None))

    return _FakeClaudeSDKClient


def test_run_execution_attempt_uses_claude_sdk_client_connect_and_disconnect(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    The baseline this swap was built to preserve: with no chat message
    ever sent, `run_execution_attempt` just connects with the prompt,
    drains the turn's response, and disconnects — nothing observably
    different from the one-shot `query()` it replaced.
    """
    fake_cls = _make_fake_client_class([["assistant: hello"]])
    monkeypatch.setattr(sdk_execution_adapter, "ClaudeSDKClient", fake_cls)

    result = asyncio.run(
        run_execution_attempt(
            prompt="do the thing",
            profile=_profile(),
            worktree_path="/tmp/wt",
            model="claude-sonnet-5",
        )
    )

    assert result.messages == ["assistant: hello"]
    client = fake_cls.instances[-1]
    assert client.calls[0] == ("connect", "do the thing")
    assert client.calls[-1] == ("disconnect", None)
    assert not any(call[0] == "query" for call in client.calls)


def test_chat_message_forwarded_via_query_on_the_same_client_before_attempt_completes(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    A message put on the queue while the attempt is in flight results in a
    real `.query()` call on the *same* client instance, before the attempt
    completes — the core proof this phase's cross-thread channel actually
    reaches a live session rather than, say, starting a second one.
    """
    fake_cls = _make_fake_client_class(
        [["assistant: part one"], ["assistant: part two"]], turn_delay_seconds=0.2
    )
    monkeypatch.setattr(sdk_execution_adapter, "ClaudeSDKClient", fake_cls)
    chat_queue: queue.Queue[str] = queue.Queue()

    async def _drive():
        attempt = asyncio.ensure_future(
            run_execution_attempt(
                prompt="do the thing",
                profile=_profile(),
                worktree_path="/tmp/wt",
                model="claude-sonnet-5",
                chat_queue=chat_queue,
            )
        )
        # Generous but well inside the fake's 0.2s-per-chunk turn: long
        # enough for connect() and the first receive_response() sleep to
        # have actually started, short enough to land well before the
        # turn finishes.
        await asyncio.sleep(0.05)
        chat_queue.put("please also check the README")
        return await attempt

    result = asyncio.run(_drive())

    client = fake_cls.instances[-1]
    query_calls = [call for call in client.calls if call[0] == "query"]
    assert query_calls == [("query", "please also check the README")]
    disconnect_index = client.calls.index(("disconnect", None))
    query_index = client.calls.index(query_calls[0])
    assert query_index < disconnect_index  # sent while still "in flight"
    assert result.messages == ["assistant: part one", "assistant: part two"]


def test_chat_message_arriving_after_the_turn_finishes_is_never_forwarded(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    Once the receive loop's own turn has finished, the chat-forwarding
    loop has already stopped — a message put on the queue after that point
    is never sent into the (by-then-disconnected) client. This is the
    concurrency-layer half of "the queue is cleared once the attempt
    finishes"; `tests/tui/test_runner.py` covers the other half
    (`RunWorker.active_chat_queue` itself going back to `None`).
    """
    fake_cls = _make_fake_client_class([["assistant: done"]], turn_delay_seconds=0.01)
    monkeypatch.setattr(sdk_execution_adapter, "ClaudeSDKClient", fake_cls)
    chat_queue: queue.Queue[str] = queue.Queue()

    result = asyncio.run(
        run_execution_attempt(
            prompt="do the thing",
            profile=_profile(),
            worktree_path="/tmp/wt",
            model="claude-sonnet-5",
            chat_queue=chat_queue,
        )
    )
    chat_queue.put("too late")  # the attempt (and its forwarding loop) is already done

    client = fake_cls.instances[-1]
    assert not any(call[0] == "query" for call in client.calls)
    assert result.messages == ["assistant: done"]


def _result_message(session_id: str) -> ResultMessage:
    """A minimal real `ResultMessage` — only the fields that dataclass
    requires with no default, plus the one this feature actually reads."""
    return ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=90,
        is_error=False,
        num_turns=1,
        session_id=session_id,
    )


def test_run_execution_attempt_returns_session_id_from_terminal_result_message(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    The real session id a live Claude Code session gets (carried on the
    turn's terminal `ResultMessage`) must make it all the way through
    `run_execution_attempt`'s return value — this is what later lets a
    person run `claude --resume <that id>` themselves.
    """
    result_message = _result_message("sess-abc-123")
    fake_cls = _make_fake_client_class([["assistant: hello", result_message]])
    monkeypatch.setattr(sdk_execution_adapter, "ClaudeSDKClient", fake_cls)

    result = asyncio.run(
        run_execution_attempt(
            prompt="do the thing",
            profile=_profile(),
            worktree_path="/tmp/wt",
            model="claude-sonnet-5",
        )
    )

    assert result.session_id == "sess-abc-123"
    assert result.messages == ["assistant: hello", result_message]


def test_run_execution_attempt_session_id_is_none_without_a_result_message(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    No `ResultMessage` in the drained messages (shouldn't normally happen)
    must never raise — `session_id` is simply `None`.
    """
    fake_cls = _make_fake_client_class([["assistant: hello"]])
    monkeypatch.setattr(sdk_execution_adapter, "ClaudeSDKClient", fake_cls)

    result = asyncio.run(
        run_execution_attempt(
            prompt="do the thing",
            profile=_profile(),
            worktree_path="/tmp/wt",
            model="claude-sonnet-5",
        )
    )

    assert result.session_id is None


def test_chat_forwarding_failure_does_not_replace_the_receive_loops_own_result(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    If forwarding a chat message itself blows up (e.g. the transport
    already gone), that must not be allowed to mask or replace whatever
    the receive loop produced for this attempt — chat is additive, never a
    new way for the attempt itself to fail.
    """
    fake_cls = _make_fake_client_class([["assistant: fine"]], turn_delay_seconds=0.2)

    async def _raising_query(self, message):
        raise RuntimeError("transport already gone")

    fake_cls.query = _raising_query
    monkeypatch.setattr(sdk_execution_adapter, "ClaudeSDKClient", fake_cls)
    chat_queue: queue.Queue[str] = queue.Queue()

    async def _drive():
        attempt = asyncio.ensure_future(
            run_execution_attempt(
                prompt="do the thing",
                profile=_profile(),
                worktree_path="/tmp/wt",
                model="claude-sonnet-5",
                chat_queue=chat_queue,
            )
        )
        await asyncio.sleep(0.05)
        chat_queue.put("hello")
        return await attempt

    result = asyncio.run(_drive())
    assert result.messages == ["assistant: fine"]


# -- live_messages: the live-trace cross-thread channel (post-M9 phase 3) --


def test_live_messages_receives_each_message_as_it_arrives_not_just_at_the_end(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    The core proof this cross-thread channel actually streams: a message
    from the *first* response chunk is sitting on `live_messages` while
    the attempt is still in flight (waiting on the second, deliberately
    delayed chunk), not just visible once `run_execution_attempt` finally
    returns. Mirrors `test_chat_message_forwarded_via_query_on_the_same_
    client_before_attempt_completes`'s shape for the opposite direction.
    """
    fake_cls = _make_fake_client_class(
        [["assistant: part one"], ["assistant: part two"]], turn_delay_seconds=0.2
    )
    monkeypatch.setattr(sdk_execution_adapter, "ClaudeSDKClient", fake_cls)
    live_messages: queue.Queue[Any] = queue.Queue()
    seen_mid_flight: list[Any] = []

    async def _drive():
        attempt = asyncio.ensure_future(
            run_execution_attempt(
                prompt="do the thing",
                profile=_profile(),
                worktree_path="/tmp/wt",
                model="claude-sonnet-5",
                live_messages=live_messages,
            )
        )
        # Well inside the first 0.2s chunk delay but after it should have
        # landed; well before the second chunk's own delay elapses.
        await asyncio.sleep(0.25)
        while True:
            try:
                seen_mid_flight.append(live_messages.get_nowait())
            except queue.Empty:
                break
        return await attempt

    result = asyncio.run(_drive())

    assert seen_mid_flight == ["assistant: part one"]
    # Drained mid-flight, so nothing's left for a second drain after the
    # attempt finishes — but the full message list is still intact.
    assert result.messages == ["assistant: part one", "assistant: part two"]


def test_live_messages_gets_every_message_the_attempt_produces(monkeypatch: pytest.MonkeyPatch):
    """Simpler end-state check: every message that ends up in
    `result.messages` also ends up on `live_messages`, in the same order,
    once the attempt is done and nothing has drained it yet."""
    fake_cls = _make_fake_client_class([["assistant: hello", "assistant: world"]])
    monkeypatch.setattr(sdk_execution_adapter, "ClaudeSDKClient", fake_cls)
    live_messages: queue.Queue[Any] = queue.Queue()

    result = asyncio.run(
        run_execution_attempt(
            prompt="do the thing",
            profile=_profile(),
            worktree_path="/tmp/wt",
            model="claude-sonnet-5",
            live_messages=live_messages,
        )
    )

    drained = []
    while True:
        try:
            drained.append(live_messages.get_nowait())
        except queue.Empty:
            break
    assert drained == result.messages == ["assistant: hello", "assistant: world"]


def test_no_live_messages_queue_is_the_byte_for_byte_default_baseline(
    monkeypatch: pytest.MonkeyPatch,
):
    """With no `live_messages` argument at all (every caller outside the
    TUI), nothing changes — this is the same baseline-preservation
    guarantee `chat_queue` already got when it was added."""
    fake_cls = _make_fake_client_class([["assistant: hello"]])
    monkeypatch.setattr(sdk_execution_adapter, "ClaudeSDKClient", fake_cls)

    result = asyncio.run(
        run_execution_attempt(
            prompt="do the thing",
            profile=_profile(),
            worktree_path="/tmp/wt",
            model="claude-sonnet-5",
        )
    )
    assert result.messages == ["assistant: hello"]


# -- render_live_message: abbreviated rendering for the live trace window --


def test_render_assistant_text_block():
    message = AssistantMessage(content=[TextBlock(text="hello there")], model="claude-sonnet-5")
    (line,) = render_live_message(message)
    assert line.startswith("text")
    assert line.endswith("hello there")


def test_render_assistant_text_collapses_newlines_and_truncates():
    long_text = "line one\nline two\n" + "x" * 300
    message = AssistantMessage(content=[TextBlock(text=long_text)], model="claude-sonnet-5")
    (line,) = render_live_message(message)
    assert "\n" not in line
    assert line.endswith("…")
    assert len(line) < len(long_text)


def test_render_thinking_block_is_a_short_marker_not_the_raw_content():
    message = AssistantMessage(
        content=[ThinkingBlock(thinking="a very long chain of reasoning" * 20, signature="sig")],
        model="claude-sonnet-5",
    )
    lines = render_live_message(message)
    assert len(lines) == 1
    assert lines[0].startswith("thinking")
    assert "a very long chain" not in lines[0]


def test_render_tool_call_bash_shows_the_command_not_raw_json():
    message = AssistantMessage(
        content=[ToolUseBlock(id="tu_1", name="Bash", input={"command": "pytest -q"})],
        model="claude-sonnet-5",
    )
    (line,) = render_live_message(message)
    assert "Bash" in line
    assert "pytest -q" in line
    assert "{" not in line  # not a raw dict dump


def test_render_tool_call_file_tool_shows_the_path():
    message = AssistantMessage(
        content=[ToolUseBlock(id="tu_2", name="Edit", input={"file_path": "/a/b.py", "old": "x"})],
        model="claude-sonnet-5",
    )
    (line,) = render_live_message(message)
    assert "Edit" in line
    assert "/a/b.py" in line


def test_render_tool_call_with_no_input_shows_just_the_name():
    message = AssistantMessage(
        content=[ToolUseBlock(id="tu_3", name="Glob", input={})], model="claude-sonnet-5"
    )
    (line,) = render_live_message(message)
    assert "Glob" in line


def test_render_tool_result_ok_and_failed():
    ok_message = UserMessage(
        content=[ToolResultBlock(tool_use_id="tu_1", content="all good", is_error=False)]
    )
    failed_message = UserMessage(
        content=[ToolResultBlock(tool_use_id="tu_1", content="boom", is_error=True)]
    )
    (ok_line,) = render_live_message(ok_message)
    (failed_line,) = render_live_message(failed_message)
    assert "ok" in ok_line
    assert "all good" in ok_line
    assert "failed" in failed_line
    assert "boom" in failed_line


def test_render_tool_result_with_list_content_blocks_extracts_text():
    message = UserMessage(
        content=[
            ToolResultBlock(
                tool_use_id="tu_1",
                content=[{"type": "text", "text": "stdout here"}, {"type": "image"}],
                is_error=False,
            )
        ]
    )
    (line,) = render_live_message(message)
    assert "stdout here" in line


def test_render_result_message_is_a_short_turn_finished_marker():
    message = ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=9,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
    )
    (line,) = render_live_message(message)
    assert "success" in line


def test_render_system_message_produces_no_lines():
    """Internal CLI plumbing a human watching this window doesn't need —
    renders nothing, not an empty/placeholder line."""
    message = SystemMessage(subtype="init", data={})
    assert render_live_message(message) == []
