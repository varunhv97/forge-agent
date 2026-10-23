"""
Per-project `.forge/` discovery: given a starting directory (the cwd, in
practice), finds the enclosing git repo and ensures that repo has a
`.forge/forge.db` and a `.forge/worktrees/` directory, creating either if
missing. This is what backs `forge run`/`resume`/`list` resolving their
state from wherever the user happens to be, the same way `git` itself
resolves `.git` from any subdirectory.

Talks to git the same way `forge/isolation/worktree_manager.py` already
does: `subprocess.run` with an argv list (never a shell string) and
`capture_output=True, text=True, check=False`, inspecting the return code
and stderr ourselves rather than relying on exceptions for an expected
failure mode.

This module never prints or reads from stdin — `NotAGitRepoError` is the
typed signal the CLI layer (`scripts/forge_cli.py`) catches to show the
interactive "run `git init` here?" prompt and either call `init_repo` or
exit cleanly. Keeping that split means this module stays trivially
testable without faking a terminal.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from forge.controller import db

_FORGE_GITIGNORE_ENTRY = ".forge/"


class NotAGitRepoError(Exception):
    """
    Raised by `find_repo_root`/`discover_project` when `start` (or its
    parents) isn't inside a git repository. Carries the directory that was
    checked so the CLI layer can show it verbatim in the prompt it asks the
    user before running `git init` on their behalf.
    """

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        super().__init__(f"not a git repo: {cwd}")


@dataclass(frozen=True)
class ProjectPaths:
    """Everything `forge run`/`resume`/`list` need to find a project's state."""

    repo_root: Path
    db_path: Path
    worktrees_dir: Path


def find_repo_root(start: Path | None = None) -> Path:
    """
    Resolves the git repo root enclosing `start` (defaulting to the
    current working directory), the same way `git` itself would from any
    subdirectory. Raises `NotAGitRepoError` if `start` isn't inside a git
    repository at all — this is an expected, ordinary outcome (a brand new
    project directory), not a bug, so it's a typed exception rather than
    letting git's own stderr leak through.
    """
    cwd = start if start is not None else Path.cwd()
    result = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise NotAGitRepoError(cwd)
    return Path(result.stdout.strip())


def init_repo(start: Path) -> Path:
    """
    Runs `git init` in `start` on the user's explicit, interactive
    confirmation (that confirmation itself happens in the CLI layer, never
    here) and returns the resulting repo root. Raises `NotAGitRepoError`
    if `git init` itself fails to produce something `find_repo_root` can
    then resolve — git's own stderr isn't surfaced beyond that, since a
    `git init` failure in a directory the process can already read/write
    is not an expected outcome worth a dedicated error type.
    """
    start.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", str(start)],
        capture_output=True,
        check=True,
        text=True,
    )
    return find_repo_root(start)


def _ensure_gitignore_entry(repo_root: Path) -> None:
    """
    Adds a `.forge/` line to the repo's root `.gitignore` if nothing
    there already covers it. Checks for either the exact entry or a bare
    `forge.db` line (some repos — this one included — ignore the db file
    by name directly rather than via the directory) before appending,
    so a project that already ignores `.forge/` some other way never gets
    a duplicate line.
    """
    gitignore_path = repo_root / ".gitignore"
    existing_lines = gitignore_path.read_text().splitlines() if gitignore_path.exists() else []
    already_covered = any(
        line.strip() in (_FORGE_GITIGNORE_ENTRY, ".forge", "forge.db") for line in existing_lines
    )
    if already_covered:
        return

    prefix = "\n" if existing_lines and existing_lines[-1].strip() else ""
    with gitignore_path.open("a") as f:
        f.write(f"{prefix}{_FORGE_GITIGNORE_ENTRY}\n")


def ensure_forge_dir(repo_root: Path) -> ProjectPaths:
    """
    Ensures `<repo_root>/.forge/forge.db` and `<repo_root>/.forge/worktrees/`
    exist (creating whichever is missing) and that the repo's `.gitignore`
    covers `.forge/`. Idempotent: calling this on an already-set-up repo
    just returns the same paths without altering anything.
    """
    forge_dir = repo_root / ".forge"
    worktrees_dir = forge_dir / "worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    db_path = forge_dir / "forge.db"
    if not db_path.exists():
        # db.connect() both creates the file and applies the schema, so a
        # freshly discovered project's db is immediately usable rather than
        # an empty file some later connect() call has to notice and migrate.
        db.connect(db_path).close()

    _ensure_gitignore_entry(repo_root)

    return ProjectPaths(repo_root=repo_root, db_path=db_path, worktrees_dir=worktrees_dir)


def discover_project(start: Path | None = None) -> ProjectPaths:
    """
    The one entry point `forge run`/`resume`/`list` need: finds the
    enclosing git repo from `start` (defaulting to cwd) and ensures its
    `.forge/` state exists. Raises `NotAGitRepoError` for the CLI layer to
    handle interactively — this function itself never prompts.
    """
    repo_root = find_repo_root(start)
    return ensure_forge_dir(repo_root)
