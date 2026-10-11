from __future__ import annotations

from pydantic import BaseModel

from forge.controller.node_registry import NodeType
from forge.models.common import NodeKind
from forge.models.graph import TaskSpec
from forge.models.request import AcceptanceCriterion, Request


class ManualTestInput(BaseModel):
    journey_description: str
    target_base_url: str
    acceptance_criteria: list[AcceptanceCriterion]


class ManualTestOutput(BaseModel):
    """
    Structurally different from the other node outputs in this package: there
    is no separate mechanical verify step for a manual/visual journey, since
    nothing mechanical can check "does this look right in a browser". This
    node's own structured output IS the gate evidence.

    That is not this project's usual "self-report trusted uncritically"
    anti-pattern — it is the one place in the pipeline where no mechanical
    alternative exists at all. What keeps it auditable rather than a bare,
    unverifiable claim is that `reasoning` is always required alongside
    `passed`, and `screenshot_paths` persists what the model actually saw
    so a human can review the judgment call after the fact.
    """

    passed: bool
    reasoning: str
    screenshot_paths: list[str] = []


def build_input(
    task: TaskSpec,
    request: Request,
    target_base_url: str,
) -> ManualTestInput:
    """
    Pure assembly of this node's input from the surrounding graph state.

    Unlike implement_exec and integration_test_write_exec, this node does
    not consume upstream task summaries — its job is a fresh, first-person
    journey through a running application, not a continuation of prior
    written work. `target_base_url` is supplied by the caller (the address
    of the environment to exercise, e.g. a deploy node's result) rather than
    scraped from an upstream output's attributes, since this project does
    not yet fix the shape of that deploy output.
    """
    acceptance_criteria = [
        criterion
        for criterion in request.acceptance_criteria
        if criterion.id in task.acceptance_criterion_ids
    ]
    return ManualTestInput(
        journey_description=task.description,
        target_base_url=target_base_url,
        acceptance_criteria=acceptance_criteria,
    )


def build_prompt(input: ManualTestInput) -> str:
    """Pure assembly of the prompt text sent to the model for this node."""
    lines = [
        "You are manually verifying a user journey in a running application "
        "by driving a real browser.",
        "",
        f"Journey to verify: {input.journey_description}",
        f"Target base URL: {input.target_base_url}",
        "",
        "Acceptance criteria this journey must satisfy:",
    ]
    for criterion in input.acceptance_criteria:
        requirement = "required" if criterion.required else "optional"
        lines.append(f"- [{requirement}] ({criterion.id}) {criterion.description}")

    lines.append("")
    lines.append(
        "Write and run a small Playwright script (via Bash) against the "
        f"target base URL ({input.target_base_url}) to carry out this "
        "journey step by step. Take screenshots as you go, at each "
        "meaningful step, and save their paths. Use your own judgment to "
        "decide whether the journey actually works as described — do not "
        "assume success just because no error was thrown."
    )
    lines.append("")
    lines.append(
        "Report `passed` and `reasoning` honestly. Your `reasoning` must "
        "describe what you actually observed in the browser (what rendered, "
        "what happened after each action, anything unexpected) and not just "
        "state a verdict. If the journey fails or looks wrong, say so and "
        "explain what you saw instead of what you expected."
    )
    lines.append("")
    lines.append(
        "When you are done, write your verdict to a file named "
        "`.forge_result.json` in the current directory, containing exactly "
        'this shape: {"passed": true or false, "reasoning": "...", '
        '"screenshot_paths": ["..."]}. This file, not anything you say in '
        "conversation, is what actually gets read as your result."
    )
    return "\n".join(lines)


MANUAL_TEST_EXEC = NodeType(
    name="manual_test_exec",
    kind=NodeKind.EXECUTION,
    input_schema=ManualTestInput,
    output_schema=ManualTestOutput,
    permission_profile="manual_test_default",
    produces_gate_evidence=True,
    default_model="claude-sonnet-5",
    escalation_model="claude-opus-5",
    output_strategy="result_file",
)
