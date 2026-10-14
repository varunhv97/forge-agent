from __future__ import annotations

from typing import Any

import pytest

from forge.adapters.execution.codex_cli_adapter import (
    CodexExecutionResult,
    ContainerRequiredError,
    build_codex_command,
    parse_codex_jsonl,
    run_codex_attempt,
)


class _FakeExecResult:
    def __init__(self, returncode: int, stdout: str, stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _RecordingExecutor:
    """A fake `exec_in_container` that records its call and returns a canned result."""

    def __init__(self, result: _FakeExecResult):
        self._result = result
        self.calls: list[tuple[Any, list[str], str]] = []

    def __call__(self, container, command, cwd):
        self.calls.append((container, command, cwd))
        return self._result


class _FakeContainer:
    container_id = "container-abc123"


# --- ContainerRequiredError enforcement ------------------------------------


def test_run_codex_attempt_without_container_raises_and_never_calls_executor():
    executor = _RecordingExecutor(_FakeExecResult(0, ""))

    with pytest.raises(ContainerRequiredError):
        run_codex_attempt(
            prompt="do something",
            model="gpt-5-codex",
            container=None,
            exec_in_container=executor,
        )

    assert executor.calls == []


# --- build_codex_command ----------------------------------------------------


def test_build_codex_command_exact_argument_list_for_ordinary_prompt():
    command = build_codex_command(prompt="fix the bug", model="gpt-5-codex")
    assert command == [
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
        "gpt-5-codex",
        "--",
        "fix the bug",
    ]


def test_build_codex_command_uses_dash_dash_so_leading_hyphen_prompt_is_not_a_flag():
    # Without the `--` separator, a prompt starting with `-` would be
    # misparsed by the real `codex` CLI as another flag. Proving the
    # separator is present and immediately precedes the prompt is the point
    # of this test, not just checking an ordinary prompt.
    prompt = "--delete-everything, actually just summarize the README"
    command = build_codex_command(prompt=prompt, model="gpt-5-codex")

    assert command[-2:] == ["--", prompt]
    assert command.count("--") == 1
    # the prompt's own leading '-' must not be interpretable as a flag
    # occurring anywhere before the separator
    assert prompt not in command[:-1]


# --- run_codex_attempt wiring ------------------------------------------------


def test_run_codex_attempt_passes_container_and_command_through_and_wraps_result():
    fabricated_stdout = "\n".join(
        [
            '{"type": "agent_message", "message": "starting work"}',
            '{"type": "agent_message", "message": "done: added the fix"}',
        ]
    )
    executor = _RecordingExecutor(_FakeExecResult(0, fabricated_stdout, ""))
    container = _FakeContainer()

    result = run_codex_attempt(
        prompt="fix the bug",
        model="gpt-5-codex",
        container=container,
        exec_in_container=executor,
    )

    assert len(executor.calls) == 1
    called_container, called_command, called_cwd = executor.calls[0]
    assert called_container is container
    assert called_command == build_codex_command(prompt="fix the bug", model="gpt-5-codex")
    assert called_cwd == "/workspace"

    assert isinstance(result, CodexExecutionResult)
    assert result.returncode == 0
    assert result.raw_stdout == fabricated_stdout
    assert result.raw_stderr == ""
    assert len(result.events) == 2
    assert result.final_message == "done: added the fix"


def test_run_codex_attempt_honors_custom_cwd():
    executor = _RecordingExecutor(_FakeExecResult(0, ""))
    container = _FakeContainer()

    run_codex_attempt(
        prompt="p",
        model="m",
        container=container,
        exec_in_container=executor,
        cwd="/custom/workdir",
    )

    _, _, called_cwd = executor.calls[0]
    assert called_cwd == "/custom/workdir"


# --- parse_codex_jsonl -------------------------------------------------------


def test_parse_codex_jsonl_extracts_final_message_from_multi_event_stream():
    stdout = "\n".join(
        [
            '{"type": "session_start", "id": "abc"}',
            '{"type": "agent_message", "message": "thinking about the task"}',
            '{"type": "agent_message", "message": "final answer: the fix is in place"}',
            '{"type": "session_end"}',
        ]
    )

    events, final_message = parse_codex_jsonl(stdout)

    assert len(events) == 4
    assert final_message == "final answer: the fix is in place"


def test_parse_codex_jsonl_skips_one_malformed_line_without_raising():
    stdout = "\n".join(
        [
            '{"type": "session_start"}',
            "not valid json at all {{{",
            '{"type": "agent_message", "message": "ok"}',
        ]
    )

    events, final_message = parse_codex_jsonl(stdout)

    assert len(events) == 2
    assert events[0] == {"type": "session_start"}
    assert events[1] == {"type": "agent_message", "message": "ok"}
    assert final_message == "ok"


def test_parse_codex_jsonl_on_empty_input_returns_no_events_and_none_message():
    events, final_message = parse_codex_jsonl("")
    assert events == []
    assert final_message is None


def test_parse_codex_jsonl_on_blank_lines_only_returns_no_events():
    events, final_message = parse_codex_jsonl("\n\n   \n\n")
    assert events == []
    assert final_message is None
