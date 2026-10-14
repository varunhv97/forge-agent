from __future__ import annotations

import pytest
from pydantic import BaseModel

from forge.controller.execution_registry import (
    ExecutionHandlerRegistry,
    ExecutionNodeHandlers,
    ExecutionRunOutcome,
    UnknownExecutionHandlersError,
)


class _FakeInput(BaseModel):
    text: str = ""


def _build_input(task, request, upstream_outputs):
    return _FakeInput(text="hello")


def _build_prompt(input_model):
    return f"prompt: {input_model.text}"


def test_register_and_get_roundtrip(task_factory, request_factory):
    registry = ExecutionHandlerRegistry()
    handlers = ExecutionNodeHandlers(build_input=_build_input, build_prompt=_build_prompt)
    registry.register("implement_exec", handlers)

    fetched = registry.get("implement_exec")
    assert fetched is handlers
    assert (
        fetched.build_prompt(fetched.build_input(task_factory("t1"), request_factory(), {}))
        == "prompt: hello"
    )


def test_get_unknown_raises():
    registry = ExecutionHandlerRegistry()
    with pytest.raises(UnknownExecutionHandlersError):
        registry.get("does_not_exist")


def test_double_registration_raises():
    registry = ExecutionHandlerRegistry()
    handlers = ExecutionNodeHandlers(build_input=_build_input, build_prompt=_build_prompt)
    registry.register("implement_exec", handlers)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("implement_exec", handlers)


def test_execution_run_outcome_defaults():
    outcome = ExecutionRunOutcome(completed=True)
    assert outcome.summary == ""
