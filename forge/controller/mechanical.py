from __future__ import annotations

import subprocess
from typing import Callable, Dict

from pydantic import BaseModel

from forge.models.graph import TaskSpec


class MechanicalResult(BaseModel):
    passed: bool
    summary: str = ""


MechanicalRunner = Callable[[TaskSpec], MechanicalResult]


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
    """

    def runner(_task: TaskSpec) -> MechanicalResult:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout_seconds
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
