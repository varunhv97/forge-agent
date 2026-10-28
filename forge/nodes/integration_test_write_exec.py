from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from forge.controller.node_registry import NodeType
from forge.models.common import NodeKind
from forge.models.graph import TaskSpec
from forge.models.request import AcceptanceCriterion, Request


class IntegrationTestWriteInput(BaseModel):
    acceptance_criteria: list[AcceptanceCriterion]
    upstream_summaries: list[str] = []
    # Files changed across the merged branch, for context on what to test —
    # not just what one upstream task touched.
    changed_paths: list[str] = []


class IntegrationTestWriteOutput(BaseModel):
    result_commit: str
    changed_paths: list[str]
    summary: str


def build_input(
    task: TaskSpec,
    request: Request,
    upstream_outputs: Mapping[str, BaseModel],
) -> IntegrationTestWriteInput:
    """
    Pure assembly of this node's input from the surrounding graph state.

    Like implement_exec, this pulls a `summary` attribute off each upstream
    output when present, skipping any that don't have one, and additionally
    collects `changed_paths` across every upstream output that has that
    attribute — since this node runs against a merged branch where several
    upstream tasks' changes now coexist.
    """
    acceptance_criteria = [
        criterion
        for criterion in request.acceptance_criteria
        if criterion.id in task.acceptance_criterion_ids
    ]
    upstream_summaries = [
        summary
        for summary in (getattr(output, "summary", None) for output in upstream_outputs.values())
        if summary is not None
    ]
    changed_paths: list[str] = []
    for output in upstream_outputs.values():
        for path in getattr(output, "changed_paths", None) or []:
            if path not in changed_paths:
                changed_paths.append(path)
    return IntegrationTestWriteInput(
        acceptance_criteria=acceptance_criteria,
        upstream_summaries=upstream_summaries,
        changed_paths=changed_paths,
    )


def build_prompt(input: IntegrationTestWriteInput) -> str:
    """Pure assembly of the prompt text sent to the model for this node."""
    lines = [
        "You are writing functional/integration tests in a coding agent pipeline.",
        "",
        "You are working on a MERGED branch: several separately implemented "
        "pieces of work now coexist here. Your job is to write tests that "
        "exercise how those pieces interact with each other — not to "
        "duplicate the unit tests that were already written alongside each "
        "individual piece of work.",
        "",
        "Acceptance criteria this test coverage must support:",
    ]
    for criterion in input.acceptance_criteria:
        requirement = "required" if criterion.required else "optional"
        lines.append(f"- [{requirement}] ({criterion.id}) {criterion.description}")

    if input.upstream_summaries:
        lines.append("")
        lines.append("Summaries of the merged pieces of work:")
        for summary in input.upstream_summaries:
            lines.append(f"- {summary}")

    if input.changed_paths:
        lines.append("")
        lines.append("Files changed across the merged branch:")
        for path in input.changed_paths:
            lines.append(f"- {path}")

    lines.append("")
    lines.append(
        "Write tests that exercise the integration points between these "
        "changed pieces. When you are done, commit your changes with a "
        "clear commit message."
    )
    return "\n".join(lines)


# Writing integration tests needs real reasoning about how several merged
# pieces of work interact — more than a mechanical check, but a narrower,
# better-scoped task than implement_exec's open-ended feature work (it's
# reading and testing against code that already exists, not designing new
# behavior from a description). "medium" by default, "high" (not "max")
# on escalation: a tougher look at the same interactions, not a leap to
# the most expensive setting for what's still fundamentally a bounded
# writing task.
INTEGRATION_TEST_WRITE_EXEC = NodeType(
    name="integration_test_write_exec",
    kind=NodeKind.EXECUTION,
    input_schema=IntegrationTestWriteInput,
    output_schema=IntegrationTestWriteOutput,
    permission_profile="code_edit_default",
    produces_gate_evidence=False,
    default_model="claude-sonnet-5",
    escalation_model="claude-opus-5",
    default_effort="medium",
    escalation_effort="high",
)
