from __future__ import annotations

from pydantic import BaseModel

from forge.models.common import NodeKind
from forge.models.request import AcceptanceCriterion
from forge.nodes.implement_exec import (
    IMPLEMENT_EXEC,
    ImplementExecInput,
    build_input,
    build_prompt,
)


class _OutputWithSummary(BaseModel):
    summary: str


class _OutputWithoutSummary(BaseModel):
    passed: bool


def _criterion(criterion_id: str) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=criterion_id,
        description=f"criterion {criterion_id}",
        verification_method="automated_test",
    )


def test_build_input_filters_acceptance_criteria_by_task(request_factory, task_factory):
    request = request_factory(acceptance_criteria=[_criterion("ac1"), _criterion("ac2")])
    task = task_factory("t1", acceptance_criterion_ids=["ac1"])

    result = build_input(task, request, upstream_outputs={})

    assert [c.id for c in result.acceptance_criteria] == ["ac1"]
    assert result.task_description == task.description


def test_build_input_collects_upstream_summaries_skipping_missing(request_factory, task_factory):
    request = request_factory()
    task = task_factory("t1")
    upstream_outputs = {
        "u1": _OutputWithSummary(summary="did the thing"),
        "u2": _OutputWithoutSummary(passed=True),
    }

    result = build_input(task, request, upstream_outputs=upstream_outputs)

    assert result.upstream_summaries == ["did the thing"]


def test_build_input_defaults_prior_failure_feedback_to_none(request_factory, task_factory):
    request = request_factory()
    task = task_factory("t1")

    result = build_input(task, request, upstream_outputs={})

    assert result.prior_failure_feedback is None


def test_build_prompt_includes_task_description_and_commit_instruction():
    input_model = ImplementExecInput(
        task_description="add a login button",
        acceptance_criteria=[_criterion("ac1")],
        upstream_summaries=["earlier work summary"],
        prior_failure_feedback="previous attempt failed the ac1 check",
    )

    prompt = build_prompt(input_model)

    assert "add a login button" in prompt
    assert "ac1" in prompt
    assert "earlier work summary" in prompt
    assert "previous attempt failed the ac1 check" in prompt
    assert "commit" in prompt.lower()
    assert "unit test" in prompt.lower()


def test_build_prompt_omits_optional_sections_when_absent():
    input_model = ImplementExecInput(
        task_description="add a login button",
        acceptance_criteria=[],
    )

    prompt = build_prompt(input_model)

    assert "add a login button" in prompt
    assert "commit" in prompt.lower()


def test_node_type_fields():
    assert IMPLEMENT_EXEC.name == "implement_exec"
    assert IMPLEMENT_EXEC.kind == NodeKind.EXECUTION
    assert IMPLEMENT_EXEC.input_schema is ImplementExecInput
    assert IMPLEMENT_EXEC.permission_profile == "code_edit_default"
    assert IMPLEMENT_EXEC.produces_gate_evidence is False
    assert IMPLEMENT_EXEC.default_model == "claude-sonnet-5"
    assert IMPLEMENT_EXEC.escalation_model == "claude-opus-5"
