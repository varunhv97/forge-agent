from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from pydantic import BaseModel

from forge.models.graph import TaskSpec


class MechanicalResult(BaseModel):
    passed: bool
    summary: str = ""


@dataclass
class MechanicalContext:
    """
    What a mechanical runner needs from the scheduler beyond the task spec
    itself. Currently just the directory it should operate in: a
    dependency-driven mechanical task (e.g. `unit_verify_exec`) runs inside
    the worktree an upstream EXECUTION attempt (e.g. `implement_exec`)
    already produced, rather than getting a worktree of its own — it's
    verifying that output, not creating new work. `worktree_path=None`
    means "no upstream attempt produced a worktree," in which case a
    runner falls back to its own process's current directory, preserving
    the behavior every mechanical node had before this existed.
    """

    worktree_path: Optional[str] = None


MechanicalRunner = Callable[[TaskSpec, MechanicalContext], MechanicalResult]


class UnknownMechanicalRunnerError(KeyError):
    pass


class MechanicalRunnerRegistry:
    """
    Maps a mechanical NodeType's name to the plain Python callable that
    actually performs it. No LLM, no adapter — mechanical nodes are
    deterministic code, and this registry is how the scheduler finds that
    code without branching on node type by name itself.
    """

    def __init__(self) -> None:
        self._runners: Dict[str, MechanicalRunner] = {}

    def register(self, node_type_name: str, runner: MechanicalRunner) -> None:
        if node_type_name in self._runners:
            raise ValueError(f"mechanical runner for '{node_type_name}' already registered")
        self._runners[node_type_name] = runner

    def get(self, node_type_name: str) -> MechanicalRunner:
        try:
            return self._runners[node_type_name]
        except KeyError as exc:
            raise UnknownMechanicalRunnerError(node_type_name) from exc


def make_shell_runner(command: list[str], timeout_seconds: float = 60.0) -> MechanicalRunner:
    """
    Builds a mechanical runner that executes a fixed argument list (never
    a shell string, so there's no shell-injection surface) and passes iff
    it exits zero. A real deployment would have one of these per actual
    check (running pytest, a linter, a deploy-status probe); this is the
    minimal, genuinely mechanical building block they'd all share.

    Runs in `context.worktree_path` when one is given (e.g. verifying code
    an upstream `implement_exec` attempt just wrote), and in the current
    process's own directory otherwise — the same behavior this had before
    `MechanicalContext` existed.
    """

    def runner(_task: TaskSpec, context: MechanicalContext) -> MechanicalResult:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=context.worktree_path,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return MechanicalResult(passed=False, summary=f"failed to execute: {exc}")
        if result.returncode == 0:
            return MechanicalResult(passed=True, summary=result.stdout.strip())
        return MechanicalResult(
            passed=False,
            summary=f"exit code {result.returncode}: {(result.stderr or result.stdout).strip()}",
        )

    return runner
