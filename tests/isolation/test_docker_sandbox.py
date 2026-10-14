from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from forge.isolation.docker_sandbox import (
    NETWORK_NAME,
    ContainerError,
    ContainerHandle,
    DockerUnavailableError,
    ExecResult,
    exec_in_container,
    launch_container,
    stop_container,
)


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_launch_container_builds_expected_args_with_network_disallowed(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    captured: list[list[str]] = []

    def fake_run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return _completed(0, stdout="abc123\n")

    with patch("subprocess.run", side_effect=fake_run):
        handle = launch_container(worktree_path, network_allowed=False)

    assert handle == ContainerHandle(container_id="abc123", worktree_path=str(worktree_path))
    assert captured == [
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-v",
            f"{worktree_path}:/workspace:rw",
            "--network",
            "none",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "forge-sandbox:latest",
            "tail",
            "-f",
            "/dev/null",
        ]
    ]


def test_launch_container_builds_expected_args_with_network_allowed(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    captured: list[list[str]] = []

    def fake_run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return _completed(0, stdout="def456\n")

    with patch("subprocess.run", side_effect=fake_run):
        handle = launch_container(
            worktree_path,
            network_allowed=True,
            memory="4g",
            cpus="4",
            image="forge-sandbox:custom",
        )

    assert handle.container_id == "def456"
    assert captured == [
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-v",
            f"{worktree_path}:/workspace:rw",
            "--network",
            NETWORK_NAME,
            "--memory",
            "4g",
            "--cpus",
            "4",
            "forge-sandbox:custom",
            "tail",
            "-f",
            "/dev/null",
        ]
    ]


def test_launch_container_raises_docker_unavailable_when_binary_missing(tmp_path: Path) -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError("docker not found")):
        with pytest.raises(DockerUnavailableError):
            launch_container(tmp_path / "worktree")


def test_launch_container_raises_container_error_on_nonzero_exit(tmp_path: Path) -> None:
    with (
        patch(
            "subprocess.run",
            return_value=_completed(
                1, stderr="Unable to find image 'forge-sandbox:latest' locally"
            ),
        ),
        pytest.raises(ContainerError, match="Unable to find image"),
    ):
        launch_container(tmp_path / "worktree")


def test_launch_container_raises_container_error_when_no_id_printed(tmp_path: Path) -> None:
    with patch("subprocess.run", return_value=_completed(0, stdout="\n")):
        with pytest.raises(ContainerError):
            launch_container(tmp_path / "worktree")


def test_stop_container_succeeds_silently_when_already_gone() -> None:
    handle = ContainerHandle(container_id="abc123", worktree_path="/tmp/worktree")

    with patch(
        "subprocess.run",
        return_value=_completed(1, stderr="Error response from daemon: No such container: abc123"),
    ) as mock_run:
        stop_container(handle)

    mock_run.assert_called_once()
    assert mock_run.call_args[0][0] == ["docker", "stop", "abc123"]


def test_stop_container_raises_container_error_for_other_failures() -> None:
    handle = ContainerHandle(container_id="abc123", worktree_path="/tmp/worktree")

    with (
        patch(
            "subprocess.run",
            return_value=_completed(1, stderr="Cannot connect to the Docker daemon"),
        ),
        pytest.raises(ContainerError, match="Cannot connect to the Docker daemon"),
    ):
        stop_container(handle)


def test_stop_container_raises_docker_unavailable_when_binary_missing() -> None:
    handle = ContainerHandle(container_id="abc123", worktree_path="/tmp/worktree")

    with patch("subprocess.run", side_effect=OSError("no such file")):
        with pytest.raises(DockerUnavailableError):
            stop_container(handle)


def test_exec_in_container_builds_expected_args_and_returns_result() -> None:
    handle = ContainerHandle(container_id="abc123", worktree_path="/tmp/worktree")
    captured: list[list[str]] = []

    def fake_run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return _completed(0, stdout="hello\n", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = exec_in_container(handle, ["echo", "hello"])

    assert captured == [["docker", "exec", "-w", "/workspace", "abc123", "echo", "hello"]]
    assert result == ExecResult(returncode=0, stdout="hello\n", stderr="")


def test_exec_in_container_uses_custom_cwd() -> None:
    handle = ContainerHandle(container_id="abc123", worktree_path="/tmp/worktree")
    captured: list[list[str]] = []

    def fake_run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return _completed(0)

    with patch("subprocess.run", side_effect=fake_run):
        exec_in_container(handle, ["ls"], cwd="/workspace/subdir")

    assert captured == [["docker", "exec", "-w", "/workspace/subdir", "abc123", "ls"]]


def test_exec_in_container_returns_result_reflecting_nonzero_inner_exit() -> None:
    handle = ContainerHandle(container_id="abc123", worktree_path="/tmp/worktree")

    with patch(
        "subprocess.run",
        return_value=_completed(127, stdout="", stderr="bash: nonexistent: command not found"),
    ):
        result = exec_in_container(handle, ["nonexistent"])

    assert result == ExecResult(
        returncode=127, stdout="", stderr="bash: nonexistent: command not found"
    )


def test_exec_in_container_raises_container_error_when_container_gone() -> None:
    handle = ContainerHandle(container_id="abc123", worktree_path="/tmp/worktree")

    with (
        patch(
            "subprocess.run",
            return_value=_completed(
                1, stderr="Error response from daemon: No such container: abc123"
            ),
        ),
        pytest.raises(ContainerError, match="No such container"),
    ):
        exec_in_container(handle, ["echo", "hi"])


def test_exec_in_container_raises_docker_unavailable_when_binary_missing() -> None:
    handle = ContainerHandle(container_id="abc123", worktree_path="/tmp/worktree")

    with patch("subprocess.run", side_effect=FileNotFoundError("docker not found")):
        with pytest.raises(DockerUnavailableError):
            exec_in_container(handle, ["echo", "hi"])
