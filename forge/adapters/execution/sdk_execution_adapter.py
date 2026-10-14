from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookContext, HookInput, HookMatcher
from claude_agent_sdk import query as sdk_query
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

    @property
    def denied_calls(self) -> list[DecisionLogEntry]:
        return [entry for entry in self.decisions if not entry.decision.allowed]


async def run_execution_attempt(
    *,
    prompt: str,
    profile: PermissionProfile,
    worktree_path: str,
    model: str,
    max_turns: int | None = None,
) -> ExecutionResult:
    """
    Runs one execution node's prompt through a live Claude Agent SDK
    session, scoped to `worktree_path` and enforced by `profile`.

    This makes a real call to the Anthropic API via the bundled Claude Code
    CLI subprocess and therefore requires network access and either
    `ANTHROPIC_API_KEY` or an authenticated `claude` CLI session, which is
    why this function has no accompanying live-call test in the offline
    suite (`scripts/live_smoke_test.py` exercises it live). Every
    other piece of this module (the translation functions and
    `build_claude_agent_options`) is tested without the network, since
    they're pure logic over plain data. This function's job is only to wire
    those pieces to `claude_agent_sdk.query`; see
    `tests/adapters/test_sdk_execution_adapter.py` for what is and isn't
    covered, and why.
    """
    decision_log: list[DecisionLogEntry] = []
    options = build_claude_agent_options(
        profile=profile,
        worktree_path=worktree_path,
        model=model,
        max_turns=max_turns,
        decision_log=decision_log,
    )
    messages = [message async for message in sdk_query(prompt=prompt, options=options)]
    return ExecutionResult(messages=messages, decisions=decision_log)
