from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from typing import Any, TypeVar, cast

from claude_agent_sdk import HookMatcher
from claude_agent_sdk.types import HookJSONOutput, PreToolUseHookInput, PreToolUseHookSpecificOutput

from forge.adapters.execution.sdk_execution_adapter import (
    DecisionLogEntry,
    build_claude_agent_options,
    hook_json_output_from_decision,
    make_pretooluse_hook_callback,
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
