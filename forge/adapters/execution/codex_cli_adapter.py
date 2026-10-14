from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class ContainerRequiredError(RuntimeError):
    """
    Raised when a Codex CLI attempt is requested without a container to run
    inside. Codex CLI has no PreToolUse-style hook mechanism of its own, so
    unlike the Claude Agent SDK adapter, this project's enforcement for
    Codex attempts is entirely structural: the container's filesystem and
    network boundary. There is no configuration path that lets a Codex
    attempt run without one — this error is raised before any command is
    built or executed, so reaching that point without a container is not
    possible.
    """


class ContainerHandle(Protocol):
    """
    The minimum this adapter needs from "a container to run inside." Kept
    as a narrow Protocol so this module has no import-time dependency on
    any concrete sandbox implementation — a caller supplies whatever object
    satisfies this shape.
    """

    container_id: str


class ContainerExecResult(Protocol):
    """
    The minimum this adapter needs from "the result of running a command
    inside a container." Duck-typed on purpose: a caller's real executor
    can return any object with these three attributes.
    """

    returncode: int
    stdout: str
    stderr: str


# Callable[[container_handle, command_argv, cwd], exec_result]. The caller
# wires in the real container-exec implementation; this module never
# imports it.
ContainerExecutor = Callable[[Any, list[str], str], Any]


@dataclass
class CodexExecutionResult:
    """What one Codex CLI attempt produced, translated out of its raw JSONL stream."""

    events: list[dict] = field(default_factory=list)
    final_message: str | None = None
    returncode: int = 0
    raw_stdout: str = ""
    raw_stderr: str = ""


def build_codex_command(prompt: str, model: str) -> list[str]:
    """
    Builds the exact `codex exec` argument list for one non-interactive,
    no-human-in-the-loop attempt:

      - `exec` is the non-interactive entry point.
      - `--cd /workspace` sets Codex's working directory to the path the
        container mounts the attempt's worktree at.
      - `--sandbox workspace-write` is Codex's own internal filesystem
        sandboxing, layered under the container boundary, not a substitute
        for it — the same "defense in depth under the real boundary, not
        the boundary itself" idea as this project's `denied_bash_patterns`.
      - `--ask-for-approval never` removes any interactive approval prompt,
        since nothing here can answer one.
      - `--json` streams JSONL events to stdout for `parse_codex_jsonl` to
        consume.
      - `--model <model>` pins the model explicitly rather than relying on
        whatever is configured as Codex's default.
      - `--` separates flags from the prompt so a prompt that itself starts
        with `-` can never be misparsed as a flag.

    This is kept as its own pure function so the exact command can be
    tested independently of any container or subprocess machinery.
    """
    return [
        "codex",
        "exec",
        "--cd",
        "/workspace",
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "--json",
        "--model",
        model,
        "--",
        prompt,
    ]


def _extract_final_message(events: list[dict]) -> str | None:
    """
    Best-effort extraction of "the agent's final response text" out of a
    list of parsed `codex exec --json` events.

    NEEDS VERIFICATION against a real `codex exec --json` run before being
    trusted in production: the exact event schema is not documented in a
    way this project can check without a live Codex install. The rule used
    here is deliberately simple rather than a guess at a precise schema:
    scan the events in order, and for each one look for a message/content
    string under a short list of plausible key names (checking top-level
    keys first, then one level inside a nested "message" or "msg" dict, since
    that's a common shape for CLI JSONL event streams). The last event with
    a matching, non-empty string wins, on the assumption that a streamed
    event log's final textual payload is its final response. If nothing
    plausible is found, this returns None rather than guessing further.
    """
    candidate_keys = ("final_message", "message", "text", "content")
    last_found: str | None = None

    for event in events:
        if not isinstance(event, dict):
            continue

        for key in candidate_keys:
            value = event.get(key)
            if isinstance(value, str) and value:
                last_found = value
                break
        else:
            for nested_key in ("message", "msg"):
                nested = event.get(nested_key)
                if isinstance(nested, dict):
                    for key in candidate_keys:
                        value = nested.get(key)
                        if isinstance(value, str) and value:
                            last_found = value
                            break

    return last_found


def parse_codex_jsonl(stdout: str) -> tuple[list[dict], str | None]:
    """
    Parses `codex exec --json` stdout defensively: each non-empty line is
    parsed as JSON independently, every successfully-parsed object is kept
    in order, and any line that fails to parse (or does not parse to a JSON
    object) is silently skipped rather than raising. This is deliberately
    tolerant because the exact event schema has not yet been verified
    against a live Codex install.

    Returns `(events, final_message)`, where `final_message` is the
    best-effort extraction documented on `_extract_final_message`.
    """
    events: list[dict] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)

    return events, _extract_final_message(events)


def run_codex_attempt(
    *,
    prompt: str,
    model: str,
    container: Any,
    exec_in_container: ContainerExecutor,
    cwd: str = "/workspace",
) -> CodexExecutionResult:
    """
    Runs one Codex CLI attempt. This is the only entry point this module
    offers for actually executing a Codex attempt, and it refuses outright
    without a container: there is no configuration flag or fallback path
    that runs Codex outside one, because the container is this project's
    actual enforcement mechanism for Codex attempts (Codex CLI has no
    PreToolUse-hook equivalent).

    `container` is opaque to this function beyond being non-None — it is
    passed straight through to `exec_in_container`, which is the caller's
    concrete bridge to whatever container runtime is actually wired in.
    """
    if container is None:
        raise ContainerRequiredError(
            "a Codex CLI attempt requires a running container; refusing to "
            "run codex exec without one"
        )

    command = build_codex_command(prompt=prompt, model=model)
    exec_result = exec_in_container(container, command, cwd)

    raw_stdout = exec_result.stdout
    raw_stderr = exec_result.stderr
    events, final_message = parse_codex_jsonl(raw_stdout)

    return CodexExecutionResult(
        events=events,
        final_message=final_message,
        returncode=exec_result.returncode,
        raw_stdout=raw_stdout,
        raw_stderr=raw_stderr,
    )
