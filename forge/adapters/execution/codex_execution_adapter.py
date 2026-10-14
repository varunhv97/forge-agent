from __future__ import annotations

from pathlib import Path

from forge.adapters.execution.codex_cli_adapter import CodexExecutionResult, run_codex_attempt
from forge.isolation.docker_sandbox import (
    ContainerHandle,
    ExecResult,
    exec_in_container,
    launch_container,
    stop_container,
)


def _exec_adapter(container: ContainerHandle, command: list[str], cwd: str) -> ExecResult:
    """
    Bridges `codex_cli_adapter`'s `ContainerExecutor` shape (positional
    `cwd`) to `docker_sandbox.exec_in_container`'s real signature (`cwd` is
    keyword-only there). The two modules were built independently and
    deliberately don't import each other — this is the one place that
    knows about both.
    """
    return exec_in_container(container, command, cwd=cwd)


def run_codex_in_sandbox(
    *,
    prompt: str,
    model: str,
    worktree_path: Path,
    network_allowed: bool = False,
) -> CodexExecutionResult:
    """
    The full lifecycle for one Codex CLI attempt: launches a fresh sandbox
    container mounting `worktree_path`, runs the attempt inside it via
    `run_codex_attempt`, and always tears the container down afterward —
    even if the attempt itself raises — so a failed attempt never leaves
    an orphaned container running. This is the only way Codex attempts get
    executed in this project; there is no path that skips the container.
    """
    container = launch_container(worktree_path, network_allowed=network_allowed)
    try:
        return run_codex_attempt(
            prompt=prompt,
            model=model,
            container=container,
            exec_in_container=_exec_adapter,
        )
    finally:
        stop_container(container)
