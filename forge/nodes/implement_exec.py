from __future__ import annotations

from pydantic import BaseModel

from forge.controller.node_registry import NodeType
from forge.models.common import NodeKind
from forge.models.graph import TaskSpec
from forge.models.request import AcceptanceCriterion, Request


class ImplementExecInput(BaseModel):
    task_description: str
    acceptance_criteria: list[AcceptanceCriterion]
    # Brief prior-work summaries, not full upstream objects — keeps context
    # bounded regardless of how large an upstream task's own output grew.
    upstream_summaries: list[str] = []
    prior_failure_feedback: str | None = None


class ImplementExecOutput(BaseModel):
    result_commit: str
    changed_paths: list[str]
    summary: str


def build_input(
    task: TaskSpec,
    request: Request,
    upstream_outputs: dict[str, BaseModel],
) -> ImplementExecInput:
    """
    Pure assembly of this node's input from the surrounding graph state.

    `upstream_outputs` is keyed by upstream task id; each value is that
    task's already-parsed output model. Rather than special-casing every
    node type that might feed into an implement_exec task, this just pulls
    a `summary` attribute off each upstream output when present, skipping
    any that don't have one.
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
    return ImplementExecInput(
        task_description=task.description,
        acceptance_criteria=acceptance_criteria,
        upstream_summaries=upstream_summaries,
        prior_failure_feedback=None,
    )


def build_prompt(input: ImplementExecInput) -> str:
    """Pure assembly of the prompt text sent to the model for this node."""
    lines = [
        "You are implementing a task in a coding agent pipeline.",
        "",
        f"Task: {input.task_description}",
        "",
        "Acceptance criteria this task must satisfy:",
    ]
    for criterion in input.acceptance_criteria:
        requirement = "required" if criterion.required else "optional"
        lines.append(f"- [{requirement}] ({criterion.id}) {criterion.description}")

    if input.upstream_summaries:
        lines.append("")
        lines.append("Summaries of prior work this task builds on:")
        for summary in input.upstream_summaries:
            lines.append(f"- {summary}")

    if input.prior_failure_feedback:
        lines.append("")
        lines.append("Feedback from a previous failed attempt at this task:")
        lines.append(input.prior_failure_feedback)

    lines.append("")
    lines.append(
        "Implement the task described above. Write unit tests covering your "
        "changes in the same commit — unit-level coverage is this node's own "
        "responsibility, not a separate step. When you are done, commit your "
        "changes with a clear commit message."
    )
    return "\n".join(lines)


IMPLEMENT_EXEC = NodeType(
    name="implement_exec",
    kind=NodeKind.EXECUTION,
    input_schema=ImplementExecInput,
    output_schema=ImplementExecOutput,
    permission_profile="code_edit_default",
    produces_gate_evidence=False,
    default_model="claude-sonnet-5",
    escalation_model="claude-opus-5",
)
