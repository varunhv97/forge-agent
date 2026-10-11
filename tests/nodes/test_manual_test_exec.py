from __future__ import annotations

from forge.models.common import NodeKind
from forge.models.request import AcceptanceCriterion
from forge.nodes.manual_test_exec import (
    MANUAL_TEST_EXEC,
    ManualTestInput,
    build_input,
    build_prompt,
)


def _criterion(criterion_id: str) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=criterion_id,
        description=f"criterion {criterion_id}",
        verification_method="manual_journey",
    )


def test_build_input_filters_acceptance_criteria_and_sets_journey_fields(
    request_factory, task_factory
):
    request = request_factory(acceptance_criteria=[_criterion("ac1"), _criterion("ac2")])
    task = task_factory("t1", acceptance_criterion_ids=["ac1"])

    result = build_input(task, request, target_base_url="https://staging.example.com")

    assert result.journey_description == task.description
    assert result.target_base_url == "https://staging.example.com"
    assert [c.id for c in result.acceptance_criteria] == ["ac1"]


def test_build_prompt_includes_target_base_url_and_instructions():
    input_model = ManualTestInput(
        journey_description="sign up and reach the dashboard",
        target_base_url="https://staging.example.com",
        acceptance_criteria=[_criterion("ac1")],
    )

    prompt = build_prompt(input_model)

    assert "sign up and reach the dashboard" in prompt
    assert "https://staging.example.com" in prompt
    assert "playwright" in prompt.lower()
    assert "screenshot" in prompt.lower()
    assert "passed" in prompt.lower()
    assert "reasoning" in prompt.lower()
    assert "honestly" in prompt.lower()


def test_node_type_fields():
    assert MANUAL_TEST_EXEC.name == "manual_test_exec"
    assert MANUAL_TEST_EXEC.kind == NodeKind.EXECUTION
    assert MANUAL_TEST_EXEC.input_schema is ManualTestInput
    assert MANUAL_TEST_EXEC.permission_profile == "manual_test_default"
    assert MANUAL_TEST_EXEC.produces_gate_evidence is True
    assert MANUAL_TEST_EXEC.default_model == "claude-sonnet-5"
    assert MANUAL_TEST_EXEC.escalation_model == "claude-opus-5"
