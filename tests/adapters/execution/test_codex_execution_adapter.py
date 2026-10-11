from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from forge.adapters.execution.codex_execution_adapter import run_codex_in_sandbox
from forge.isolation.docker_sandbox import ContainerError


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=args, returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_full_lifecycle_launches_execs_and_stops():
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1] == "run":
            return _completed(args, stdout="container-abc123\n")
        if args[1] == "exec":
            jsonl = '{"message": "hello"}\n{"message": "final answer here"}\n'
            return _completed(args, stdout=jsonl)
        if args[1] == "stop":
            return _completed(args)
        raise AssertionError(f"unexpected docker subcommand: {args}")

    with patch("forge.isolation.docker_sandbox.subprocess.run", side_effect=fake_run):
        result = run_codex_in_sandbox(
            prompt="do the thing",
            model="gpt-5.1-codex",
            worktree_path=Path("/tmp/wt-1"),
        )

    assert result.final_message == "final answer here"
    assert len(result.events) == 2
    assert len(calls) == 3

    run_call, exec_call, stop_call = calls
    assert run_call[:2] == ["docker", "run"]
    assert "/tmp/wt-1:/workspace:rw" in run_call
    assert "none" in run_call  # network_allowed defaults to False

    assert exec_call[:2] == ["docker", "exec"]
    assert "container-abc123" in exec_call
    assert "codex" in exec_call and "exec" in exec_call  # the inner codex command
    assert "--" in exec_call  # the prompt separator survived the full round trip

    assert stop_call == ["docker", "stop", "container-abc123"]


def test_network_allowed_attaches_named_network():
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1] == "run":
            return _completed(args, stdout="container-xyz\n")
        if args[1] == "exec":
            return _completed(args, stdout="")
        if args[1] == "stop":
            return _completed(args)
        raise AssertionError(f"unexpected docker subcommand: {args}")

    with patch("forge.isolation.docker_sandbox.subprocess.run", side_effect=fake_run):
        run_codex_in_sandbox(
            prompt="fetch something",
            model="gpt-5.1-codex",
            worktree_path=Path("/tmp/wt-2"),
            network_allowed=True,
        )

    run_call = calls[0]
    assert "forge-sandbox-net" in run_call
    assert "none" not in run_call


def test_container_is_stopped_even_when_exec_fails():
    """
    If the `docker exec` step itself fails (not just the inner command
    exiting non-zero, but docker reporting the container is gone), the
    container must still be torn down -- a failed attempt can never leave
    an orphaned container behind.
    """
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1] == "run":
            return _completed(args, stdout="container-abc123\n")
        if args[1] == "exec":
            return _completed(
                args, returncode=1, stderr="Error: No such container: container-abc123"
            )
        if args[1] == "stop":
            return _completed(args)
        raise AssertionError(f"unexpected docker subcommand: {args}")

    with patch("forge.isolation.docker_sandbox.subprocess.run", side_effect=fake_run):
        try:
            run_codex_in_sandbox(
                prompt="do the thing",
                model="gpt-5.1-codex",
                worktree_path=Path("/tmp/wt-1"),
            )
            raised = False
        except ContainerError:
            raised = True

    assert raised
    subcommands = [c[1] for c in calls]
    assert subcommands == ["run", "exec", "stop"]  # stop still happened, in the finally
