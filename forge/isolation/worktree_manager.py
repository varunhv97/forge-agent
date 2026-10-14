from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel

# Substrings git itself uses when an operation's target is already gone.
# Matching on these (rather than a return code alone) is what lets cleanup
# be idempotent: "already removed" is treated as success, every other
# failure is not.
_WORKTREE_ALREADY_GONE = "is not a working tree"
_BRANCH_ALREADY_GONE = "not found"


class WorktreeHandle(BaseModel):
    """
    Everything the controller needs to remember about one attempt's
    isolated git worktree: where it lives on disk, which throwaway branch
    it's checked out on, and the commit it was branched from. Produced by
    `create_worktree` and passed back into `remove_worktree` unchanged.
    """

    worktree_path: str
    branch_name: str
    base_commit: str
    task_id: str
    attempt_id: str


class WorktreeError(RuntimeError):
    """
    Raised for any git worktree/branch operation that fails for a reason
    other than "this was already cleaned up". The message always carries
    git's own stderr (or stdout, if that's where git wrote the failure)
    verbatim, so whoever reads the controller's logs sees exactly what git
    said rather than a generic wrapper message.
    """


def _run_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as exc:
        # git itself isn't on PATH, or repo_root can't even be stat'd as a
        # directory by the OS before git gets a chance to say anything.
        raise WorktreeError(f"failed to invoke 'git {' '.join(args)}': {exc}") from exc


def _git_failure_message(result: subprocess.CompletedProcess[str]) -> str:
    message = (result.stderr or result.stdout).strip()
    return message or f"git exited with status {result.returncode} and produced no output"


def create_worktree(
    repo_root: Path, task_id: str, attempt_id: str, base_commit: str
) -> WorktreeHandle:
    """
    Creates a fresh git worktree, checked out on a fresh branch
    (`forge/<task_id>/<attempt_id>`), rooted at `base_commit`, located at
    `<repo_root>/.forge/worktrees/<attempt_id>`.

    `git worktree add` creates every missing directory on the way to the
    worktree path itself — including `.forge/worktrees/` the first time
    it's ever used — so this function does not pre-create anything; it
    only runs the one command and reports what git did.

    Raises `WorktreeError`, wrapping git's own stderr verbatim, for every
    failure mode: `repo_root` is not a git repository, `base_commit` does
    not resolve to a commit, the branch name is already taken, or a
    worktree (or an unrelated, occupied directory) already exists at the
    target path.
    """
    branch_name = f"forge/{task_id}/{attempt_id}"
    worktree_path = repo_root / ".forge" / "worktrees" / attempt_id

    result = _run_git(
        repo_root,
        ["worktree", "add", "-b", branch_name, str(worktree_path), base_commit],
    )
    if result.returncode != 0:
        raise WorktreeError(
            f"git worktree add failed for task '{task_id}' attempt '{attempt_id}' "
            f"(branch '{branch_name}', path '{worktree_path}', base commit "
            f"'{base_commit}'): {_git_failure_message(result)}"
        )

    return WorktreeHandle(
        worktree_path=str(worktree_path),
        branch_name=branch_name,
        base_commit=base_commit,
        task_id=task_id,
        attempt_id=attempt_id,
    )


def remove_worktree(repo_root: Path, handle: WorktreeHandle, force: bool = False) -> None:
    """
    Tears down one attempt's worktree and its now-unused branch.

    The two halves of cleanup are deliberately sequential and fail
    independently, rather than being folded into one catch-all:

    - `git worktree remove` runs first (with `--force` when `force=True`,
      since git otherwise refuses to remove a worktree with uncommitted
      changes). If it fails for a real reason — most commonly uncommitted
      changes without `force=True` — this raises immediately and never
      touches the branch: the branch is still checked out by a worktree
      that still exists, so deleting it would be wrong, not just redundant,
      and `git branch -D` would fail anyway.
    - If instead git reports the path is already not a working tree (it
      was deleted out-of-band, e.g. `rm -rf` by hand, or a previous
      `remove_worktree` call already removed it), that is treated as
      success rather than an error: "the worktree is gone" is exactly the
      postcondition this function promises, and it's already true.
      Cleanup proceeds to the branch.
    - `git branch -D` runs next. A failure because the branch is already
      gone is likewise treated as success. Any other branch-deletion
      failure raises `WorktreeError` with a message that distinguishes
      this case from the first: the worktree *was* successfully removed,
      and only the branch cleanup is stuck — important for a caller
      deciding whether a retry is safe.

    Because both halves treat "already gone" as success, calling this
    twice on the same handle (or once on a handle whose worktree was
    already deleted out-of-band) is safe and will not raise.
    """
    worktree_result = _run_git(
        repo_root,
        ["worktree", "remove", *(["--force"] if force else []), handle.worktree_path],
    )
    if worktree_result.returncode != 0:
        message = _git_failure_message(worktree_result)
        if _WORKTREE_ALREADY_GONE not in message:
            raise WorktreeError(
                f"git worktree remove failed for '{handle.worktree_path}' "
                f"(branch '{handle.branch_name}'): {message}"
            )
        # Already gone out-of-band (or by a prior call) — fall through to
        # branch cleanup rather than raising.

    branch_result = _run_git(repo_root, ["branch", "-D", handle.branch_name])
    if branch_result.returncode != 0:
        message = _git_failure_message(branch_result)
        if _BRANCH_ALREADY_GONE not in message:
            raise WorktreeError(
                f"worktree '{handle.worktree_path}' was removed, but deleting "
                f"branch '{handle.branch_name}' failed: {message}"
            )
        # Already gone — nothing left to do.
