from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from forge.models.permissions import PermissionProfile


class ToolCallEvent(BaseModel):
    """
    A plain, adapter-agnostic description of one attempted tool call.

    This is deliberately not shaped after any specific agent-runtime SDK's
    native hook payload. An adapter (e.g. a future Claude Agent SDK
    PreToolUse hook registration) is responsible for translating whatever
    that SDK's actual event object looks like into one of these before
    calling a hook built by `build_pretooluse_hook`, and for translating the
    resulting `HookDecision` back into that SDK's expected response shape.
    This module never imports or assumes anything about that SDK.
    """

    tool_name: str
    command: str | None = None  # the Bash command string, when tool_name == "Bash"
    path: str | None = None  # the file path being read/written, for Read/Edit/Write-style tools


@dataclass(frozen=True)
class HookDecision:
    """The outcome of a PreToolUse policy check: allow, or deny with a reason."""

    allowed: bool
    reason: str | None = None

    @classmethod
    def allow(cls) -> HookDecision:
        return cls(allowed=True, reason=None)

    @classmethod
    def deny(cls, reason: str) -> HookDecision:
        return cls(allowed=False, reason=reason)


PreToolUseHook = Callable[[ToolCallEvent], HookDecision]

# Tool names treated as filesystem-touching for the purposes of the path-scope
# check. Read/Edit/Write are the tools the task spec calls out explicitly;
# NotebookEdit is included on the same reasoning (it reads and writes a file
# at a given path, same as Edit/Write) since it plausibly touches the
# filesystem the same way. Any other tool name is left alone by this check —
# if a real adapter introduces another file-path-bearing tool, its name
# should be added here rather than silently falling outside path scoping.
_PATH_SCOPED_TOOLS = frozenset({"Read", "Edit", "Write", "NotebookEdit"})

# Bash binaries whose presence as a command token strongly suggests the
# command reaches the network.
_NETWORK_COMMAND_TOKENS = frozenset({"curl", "wget", "ssh", "scp", "nc"})
_NETWORK_URL_SUBSTRINGS = ("http://", "https://")


def _looks_like_network_bash(command: str) -> bool:
    """
    A deliberately narrow heuristic for "this Bash command probably reaches
    the network": a known network-capable binary invoked as a command token,
    or a literal http(s) URL substring. It does not parse shell syntax, so it
    will miss network access reached indirectly (a wrapper script, a language
    runtime's built-in HTTP client, a package manager install) and can in
    principle over-flag a token that merely happens to match inside an
    unrelated argument. This is a backstop layered under the explicit
    `denied_bash_patterns` and tool allowlist, not a sandbox by itself.
    """
    tokens = re.findall(r"[A-Za-z0-9_./-]+", command)
    basenames = {os.path.basename(token) for token in tokens}
    if basenames & _NETWORK_COMMAND_TOKENS:
        return True
    return any(substring in command for substring in _NETWORK_URL_SUBSTRINGS)


def _tool_covered_by_allowlist(event: ToolCallEvent, allowed_tools: list[str]) -> bool:
    for entry in allowed_tools:
        if entry == event.tool_name:
            return True
        if (
            event.tool_name == "Bash"
            and entry.startswith("Bash(")
            and entry.endswith(")")
            and event.command is not None
        ):
            pattern = entry[len("Bash(") : -1]
            if fnmatch.fnmatch(event.command, pattern):
                return True
    return False


def _is_within(path: str, root: str) -> bool:
    """
    True iff the fully-resolved `path` is `root` itself or lies strictly
    beneath it, checked by path component rather than by string prefix so
    that a sibling with a shared prefix (e.g. root=/a/worktree,
    path=/a/worktree-evil) is not mistaken for containment.
    """
    if path == root:
        return True
    try:
        common = os.path.commonpath([path, root])
    except ValueError:
        # Raised when the paths don't share a root at all (e.g. different
        # drives on Windows); not applicable to this project's POSIX
        # worktrees, but treated as "not contained" rather than propagating.
        return False
    return common == root


def build_pretooluse_hook(profile: PermissionProfile, worktree_path: str) -> PreToolUseHook:
    """
    Builds the actual tool-boundary enforcement closure for one attempt.

    Given a `ToolCallEvent`, the returned callable decides allow/deny by
    checking, in order:

      1. Tool allowlist — `profile.allowed_tools` entries are either an
         exact tool name, or (for Bash only) a `"Bash(<glob>)"` pattern
         matched against the command with `fnmatch`. A call not covered by
         any entry is denied.
      2. Explicit denial patterns — a Bash command matching any
         `profile.denied_bash_patterns` entry is denied even if step 1
         allowed it. An explicit deny always wins over an allow.
      3. Network scope — a Bash command that looks like it reaches the
         network (see `_looks_like_network_bash`) is denied when
         `profile.network_allowed` is False.
      4. Path scope — for Read/Edit/Write/NotebookEdit calls, the `path` is
         resolved with `os.path.realpath` (following symlinks) and must
         fall under the similarly resolved worktree root; anything that
         resolves outside it, including via `../` traversal or a symlink
         planted inside the worktree that points outside it, is denied.

    A call that survives all four checks is allowed. This function has no
    dependency on any specific agent-runtime SDK — it is a pure policy
    decision engine over the plain `ToolCallEvent` shape; translating a
    real SDK's native hook payload into that shape, and this decision back
    into whatever response format that SDK expects, is an adapter's job.
    """
    real_worktree = os.path.realpath(worktree_path)

    def decide(event: ToolCallEvent) -> HookDecision:
        if not _tool_covered_by_allowlist(event, profile.allowed_tools):
            return HookDecision.deny(
                f"tool '{event.tool_name}' is not permitted by this task's permission profile "
                f"'{profile.name}'"
            )

        if event.tool_name == "Bash" and event.command is not None:
            for pattern in profile.denied_bash_patterns:
                if fnmatch.fnmatch(event.command, pattern):
                    return HookDecision.deny(f"command matches denied pattern '{pattern}'")
            if not profile.network_allowed and _looks_like_network_bash(event.command):
                return HookDecision.deny(
                    "command appears to reach the network and this task's permission "
                    "profile has network_allowed=False"
                )

        if event.tool_name in _PATH_SCOPED_TOOLS:
            if event.path is None:
                return HookDecision.deny(
                    f"{event.tool_name} call provided no path to check against the "
                    f"worktree boundary"
                )
            real_path = os.path.realpath(event.path)
            if not _is_within(real_path, real_worktree):
                return HookDecision.deny(
                    f"path '{event.path}' resolves outside this task's worktree "
                    f"({worktree_path})"
                )

        return HookDecision.allow()

    return decide
