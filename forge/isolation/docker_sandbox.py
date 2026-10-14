from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel

# Docker's own message substring for "you asked me to operate on a
# container that doesn't exist (any more)". Matching on this - rather than
# on the stop command's return code alone - is what lets stop_container be
# idempotent: "already gone" is treated as success, every other failure is
# not. Mirrors how worktree_manager.py matches git's "is not a working
# tree" message.
_CONTAINER_ALREADY_GONE = "No such container"

# Name of the Docker network containers are attached to when a task's
# permission profile allows network access. Provisioning this network on
# the host (docker network create ...) is out of scope for this module -
# launch_container only ever references it by name and assumes it already
# exists wherever containers are actually run.
NETWORK_NAME = "forge-sandbox-net"


class ContainerHandle(BaseModel):
    """
    Everything the controller needs to remember about one running sandbox
    container: its id (for exec/stop) and the worktree it has mounted.
    Produced by `launch_container` and passed back into `exec_in_container`
    and `stop_container` unchanged.
    """

    container_id: str
    worktree_path: str


class ExecResult(BaseModel):
    """
    The outcome of one `docker exec` call. `returncode`/`stdout`/`stderr`
    belong to the command that ran *inside* the container, not to the
    `docker exec` invocation itself - a non-zero `returncode` here is a
    normal result, not a failure of this module.
    """

    returncode: int
    stdout: str
    stderr: str


class DockerUnavailableError(RuntimeError):
    """
    Raised when the `docker` binary itself can't be invoked at all - it's
    not on PATH, or the daemon it talks to isn't reachable. Distinguished
    from `ContainerError` so a personal tool running on a machine without
    Docker installed or running can report that clearly, rather than
    surfacing a confusing container-operation failure.
    """


class ContainerError(RuntimeError):
    """
    Raised for any docker command that ran but failed for a reason other
    than "docker itself isn't usable" or "the container was already gone".
    The message always carries docker's own stderr (or stdout, if that's
    where the failure was written) verbatim, same philosophy as
    `WorktreeError` in `worktree_manager.py`.
    """


def _run_docker(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        # docker itself isn't on PATH, or (depending on platform) the
        # daemon/socket it needs isn't reachable before docker even gets a
        # chance to print its own error.
        raise DockerUnavailableError(f"failed to invoke 'docker {' '.join(args)}': {exc}") from exc


def _docker_failure_message(result: subprocess.CompletedProcess) -> str:
    message = (result.stderr or result.stdout).strip()
    return message or f"docker exited with status {result.returncode} and produced no output"


def launch_container(
    worktree_path: Path,
    *,
    network_allowed: bool = False,
    memory: str = "2g",
    cpus: str = "2",
    image: str = "forge-sandbox:latest",
) -> ContainerHandle:
    """
    Starts a detached, self-removing container that mounts exactly one
    attempt's worktree read-write at `/workspace` and nothing else from the
    host. No network unless `network_allowed=True`, in which case the
    container is attached to `NETWORK_NAME` - a named Docker network this
    function only ever references, never creates.

    Raises `DockerUnavailableError` if `docker` itself can't be invoked,
    `ContainerError` (wrapping docker's stderr/stdout verbatim) if the
    command runs but exits non-zero.
    """
    network = NETWORK_NAME if network_allowed else "none"

    result = _run_docker(
        [
            "run",
            "-d",
            "--rm",
            "-v",
            f"{worktree_path}:/workspace:rw",
            "--network",
            network,
            "--memory",
            memory,
            "--cpus",
            cpus,
            image,
            "tail",
            "-f",
            "/dev/null",
        ]
    )
    if result.returncode != 0:
        raise ContainerError(
            f"docker run failed for worktree '{worktree_path}' (image '{image}'): "
            f"{_docker_failure_message(result)}"
        )

    container_id = result.stdout.strip()
    if not container_id:
        raise ContainerError(
            f"docker run for worktree '{worktree_path}' (image '{image}') exited "
            "successfully but printed no container id"
        )

    return ContainerHandle(container_id=container_id, worktree_path=str(worktree_path))


def stop_container(handle: ContainerHandle) -> None:
    """
    Stops (and, since the container was started with `--rm`, thereby
    removes) one sandbox container. Safe to call more than once, or on a
    container that was already stopped/removed out-of-band: docker's own
    "No such container" failure is treated as success rather than an
    error, matching the postcondition this function promises - the
    container is gone - which is already true in that case.

    Any other stop failure raises `ContainerError`.
    """
    result = _run_docker(["stop", handle.container_id])
    if result.returncode != 0:
        message = _docker_failure_message(result)
        if _CONTAINER_ALREADY_GONE not in message:
            raise ContainerError(
                f"docker stop failed for container '{handle.container_id}': {message}"
            )
        # Already gone out-of-band (or by a prior call) - nothing left to do.


def exec_in_container(
    handle: ContainerHandle, command: list[str], *, cwd: str = "/workspace"
) -> ExecResult:
    """
    Runs `command` inside an already-running sandbox container via
    `docker exec`, working directory `cwd`. This function has no opinion
    about what `command` actually is - that's a different piece of work's
    job - and always returns an `ExecResult` carrying the inner command's
    own exit status, even when that status is non-zero.

    Raises `DockerUnavailableError`/`ContainerError` only for a failure of
    `docker exec` itself (the container doesn't exist, or `docker` can't be
    invoked at all) - never for the inner command's own exit code.
    """
    result = _run_docker(["exec", "-w", cwd, handle.container_id, *command])
    if result.returncode != 0:
        message = _docker_failure_message(result)
        if _CONTAINER_ALREADY_GONE in message:
            raise ContainerError(
                f"docker exec failed for container '{handle.container_id}': {message}"
            )
        # A non-zero exit here could be `docker exec`'s own failure (e.g. a
        # malformed invocation) or the inner command's exit code - docker
        # exec always returns the inner command's exit code on success, so
        # the only way to tell "docker exec itself failed" apart from "the
        # inner command exited non-zero" is docker's own stderr wording. A
        # container that doesn't exist is the one failure this module is
        # responsible for distinguishing; anything else is treated as the
        # inner command's own result and returned, not raised.

    return ExecResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
