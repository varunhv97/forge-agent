from __future__ import annotations

import os

from forge.isolation.hooks import ToolCallEvent, build_pretooluse_hook
from forge.models.permissions import PermissionProfile


def _profile(**overrides) -> PermissionProfile:
    defaults = dict(
        name="test-profile",
        allowed_tools=["Read", "Edit", "Write", "Bash(pytest*)"],
        denied_bash_patterns=["git push*", "*vercel deploy*", "rm -rf *"],
        network_allowed=False,
    )
    defaults.update(overrides)
    return PermissionProfile(**defaults)


def test_allowed_tool_passes(tmp_path):
    hook = build_pretooluse_hook(_profile(), str(tmp_path))
    decision = hook(ToolCallEvent(tool_name="Read", path=str(tmp_path / "file.txt")))
    assert decision.allowed is True
    assert decision.reason is None


def test_bash_command_matching_allowed_pattern_passes(tmp_path):
    hook = build_pretooluse_hook(_profile(), str(tmp_path))
    decision = hook(ToolCallEvent(tool_name="Bash", command="pytest -q"))
    assert decision.allowed is True


def test_tool_not_in_allowlist_is_denied(tmp_path):
    hook = build_pretooluse_hook(_profile(), str(tmp_path))
    decision = hook(ToolCallEvent(tool_name="WebFetch"))
    assert decision.allowed is False
    assert "WebFetch" in decision.reason


def test_bash_not_matching_any_allowed_pattern_is_denied(tmp_path):
    hook = build_pretooluse_hook(_profile(), str(tmp_path))
    decision = hook(ToolCallEvent(tool_name="Bash", command="ls -la"))
    assert decision.allowed is False


def test_explicit_deny_pattern_wins_over_wildcard_allow(tmp_path):
    profile = _profile(allowed_tools=["Bash(*)"], denied_bash_patterns=["git push*"])
    hook = build_pretooluse_hook(profile, str(tmp_path))
    decision = hook(ToolCallEvent(tool_name="Bash", command="git push origin main"))
    assert decision.allowed is False
    assert "denied pattern" in decision.reason


def test_network_looking_bash_denied_when_network_not_allowed(tmp_path):
    profile = _profile(allowed_tools=["Bash(*)"], network_allowed=False)
    hook = build_pretooluse_hook(profile, str(tmp_path))
    decision = hook(ToolCallEvent(tool_name="Bash", command="curl https://example.com"))
    assert decision.allowed is False
    assert "network" in decision.reason


def test_network_looking_bash_allowed_when_network_allowed(tmp_path):
    profile = _profile(allowed_tools=["Bash(*)"], network_allowed=True)
    hook = build_pretooluse_hook(profile, str(tmp_path))
    decision = hook(ToolCallEvent(tool_name="Bash", command="curl https://example.com"))
    assert decision.allowed is True


def test_path_inside_worktree_allowed(tmp_path):
    hook = build_pretooluse_hook(_profile(), str(tmp_path))
    target = tmp_path / "subdir" / "file.txt"
    target.parent.mkdir()
    target.write_text("hello")
    decision = hook(ToolCallEvent(tool_name="Edit", path=str(target)))
    assert decision.allowed is True


def test_path_outside_worktree_denied(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    hook = build_pretooluse_hook(_profile(), str(worktree))
    decision = hook(ToolCallEvent(tool_name="Read", path=str(outside)))
    assert decision.allowed is False
    assert "outside" in decision.reason


def test_relative_traversal_escape_denied(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    hook = build_pretooluse_hook(_profile(), str(worktree))
    traversal_path = os.path.join(str(worktree), "..", "secret.txt")
    decision = hook(ToolCallEvent(tool_name="Read", path=traversal_path))
    assert decision.allowed is False


def test_symlink_escape_denied(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside_target = tmp_path / "outside_secret.txt"
    outside_target.write_text("secret")
    symlink_path = worktree / "innocuous_link.txt"
    os.symlink(str(outside_target), str(symlink_path))

    hook = build_pretooluse_hook(_profile(), str(worktree))
    decision = hook(ToolCallEvent(tool_name="Read", path=str(symlink_path)))
    assert decision.allowed is False
    assert "outside" in decision.reason


def test_symlink_pointing_inside_worktree_allowed(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    inside_target = worktree / "real.txt"
    inside_target.write_text("fine")
    symlink_path = worktree / "link.txt"
    os.symlink(str(inside_target), str(symlink_path))

    hook = build_pretooluse_hook(_profile(), str(worktree))
    decision = hook(ToolCallEvent(tool_name="Read", path=str(symlink_path)))
    assert decision.allowed is True


def test_path_scoped_tool_with_no_path_denied(tmp_path):
    hook = build_pretooluse_hook(_profile(), str(tmp_path))
    decision = hook(ToolCallEvent(tool_name="Read", path=None))
    assert decision.allowed is False
