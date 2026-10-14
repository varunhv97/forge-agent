from __future__ import annotations

import subprocess
from pathlib import Path


class GitStateError(RuntimeError):
    """
    Raised when a git command needed to determine an attempt's resulting
    commit/changed paths fails unexpectedly. Never raised for "nothing to
    commit" — that's an ordinary, expected outcome handled by the callers
    in this module, not a failure.
    """


def _run_git(repo_path: str | Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as exc:
        raise GitStateError(
            f"failed to invoke 'git {' '.join(args)}' in '{repo_path}': {exc}"
        ) from exc


def current_head(repo_path: str | Path) -> str:
    """Returns the current commit SHA at `repo_path`'s HEAD."""
    result = _run_git(repo_path, ["rev-parse", "HEAD"])
    if result.returncode != 0:
        raise GitStateError(f"git rev-parse HEAD failed in '{repo_path}': {result.stderr.strip()}")
    return result.stdout.strip()


def commit_dirty_changes(repo_path: str | Path, message: str) -> bool:
    """
    Commits whatever uncommitted changes exist at `repo_path`, if any. This
    is a safety net, not the primary expectation — a node's prompt always
    asks it to commit its own work — but the controller never trusts that
    instruction was followed; this guarantees a result commit exists
    either way. Returns whether anything was actually committed.
    """
    status = _run_git(repo_path, ["status", "--porcelain"])
    if status.returncode != 0:
        raise GitStateError(f"git status failed in '{repo_path}': {status.stderr.strip()}")
    if not status.stdout.strip():
        return False

    add_result = _run_git(repo_path, ["add", "-A"])
    if add_result.returncode != 0:
        raise GitStateError(f"git add -A failed in '{repo_path}': {add_result.stderr.strip()}")

    commit_result = _run_git(repo_path, ["commit", "-m", message])
    if commit_result.returncode != 0:
        raise GitStateError(
            f"git commit failed in '{repo_path}': "
            f"{(commit_result.stderr or commit_result.stdout).strip()}"
        )
    return True


def changed_paths_between(repo_path: str | Path, old_commit: str, new_commit: str) -> list[str]:
    """Returns the paths that differ between `old_commit` and `new_commit`."""
    if old_commit == new_commit:
        return []
    result = _run_git(repo_path, ["diff", "--name-only", old_commit, new_commit])
    if result.returncode != 0:
        raise GitStateError(
            f"git diff --name-only {old_commit} {new_commit} failed in '{repo_path}': "
            f"{result.stderr.strip()}"
        )
    return [line for line in result.stdout.splitlines() if line]
