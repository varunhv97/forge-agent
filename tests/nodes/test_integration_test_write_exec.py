from __future__ import annotations

from pydantic import BaseModel

from forge.models.common import NodeKind
from forge.models.request import AcceptanceCriterion
from forge.nodes.integration_test_write_exec import (
    INTEGRATION_TEST_WRITE_EXEC,
    IntegrationTestWriteInput,
    build_input,
    build_prompt,
)


class _OutputWithSummaryAndPaths(BaseModel):
    summary: str
    changed_paths: list[str]


class _OutputWithNeither(BaseModel):
    passed: bool


def _criterion(criterion_id: str) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=criterion_id,
        description=f"criterion {criterion_id}",
        verification_method="automated_test",
    )


def test_build_input_filters_acceptance_criteria_by_task(request_factory, task_factory):
    request = request_factory(acceptance_criteria=[_criterion("ac1"), _criterion("ac2")])
    task = task_factory("t1", acceptance_criterion_ids=["ac2"])

    result = build_input(task, request, upstream_outputs={})

    assert [c.id for c in result.acceptance_criteria] == ["ac2"]


def test_build_input_collects_summaries_and_changed_paths_skipping_missing(
    request_factory, task_factory
):
    request = request_factory()
    task = task_factory("t1")
    upstream_outputs = {
        "u1": _OutputWithSummaryAndPaths(
            summary="implemented feature A", changed_paths=["a.py", "b.py"]
        ),
        "u2": _OutputWithNeither(passed=True),
    }

    result = build_input(task, request, upstream_outputs=upstream_outputs)

    assert result.upstream_summaries == ["implemented feature A"]
    assert result.changed_paths == ["a.py", "b.py"]


def test_build_input_deduplicates_changed_paths(request_factory, task_factory):
    request = request_factory()
    task = task_factory("t1")
    upstream_outputs = {
        "u1": _OutputWithSummaryAndPaths(summary="s1", changed_paths=["a.py"]),
        "u2": _OutputWithSummaryAndPaths(summary="s2", changed_paths=["a.py", "c.py"]),
    }

    result = build_input(task, request, upstream_outputs=upstream_outputs)

    assert result.changed_paths == ["a.py", "c.py"]


def test_build_prompt_includes_merged_branch_framing_and_changed_paths():
    input_model = IntegrationTestWriteInput(
        acceptance_criteria=[_criterion("ac1")],
        upstream_summaries=["feature A landed"],
        changed_paths=["src/feature_a.py"],
    )

    prompt = build_prompt(input_model)

    assert "merged" in prompt.lower()
    assert "interact" in prompt.lower()
    assert "feature A landed" in prompt
    assert "src/feature_a.py" in prompt
    assert "commit" in prompt.lower()
    assert "duplicate" in prompt.lower()


def test_node_type_fields():
    assert INTEGRATION_TEST_WRITE_EXEC.name == "integration_test_write_exec"
    assert INTEGRATION_TEST_WRITE_EXEC.kind == NodeKind.EXECUTION
    assert INTEGRATION_TEST_WRITE_EXEC.input_schema is IntegrationTestWriteInput
    assert INTEGRATION_TEST_WRITE_EXEC.permission_profile == "code_edit_default"
    assert INTEGRATION_TEST_WRITE_EXEC.produces_gate_evidence is False
    assert INTEGRATION_TEST_WRITE_EXEC.default_model == "claude-sonnet-5"
    assert INTEGRATION_TEST_WRITE_EXEC.escalation_model == "claude-opus-5"
