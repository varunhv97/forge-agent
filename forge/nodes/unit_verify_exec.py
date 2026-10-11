from __future__ import annotations

from pydantic import BaseModel

from forge.controller.mechanical import MechanicalRunner, make_shell_runner
from forge.controller.node_registry import NodeType
from forge.models.common import NodeKind


class MechanicalIO(BaseModel):
    """Mechanical nodes have no model-facing input/output schema — there's
    no prompt and no structured response to validate — but NodeType
    requires one for every node type regardless of kind, so this stands in
    as an explicit, empty placeholder rather than a null the registry
    would have to special-case."""


# Mechanical, deterministic, no LLM: runs the project's own test suite
# inside whatever worktree an upstream implement_exec attempt produced
# (the scheduler resolves that worktree automatically for any mechanical
# task that depends on one — see MechanicalContext). This is what actually
# verifies the unit tests implement_exec wrote alongside its code; nothing
# about this node writes code itself.
UNIT_VERIFY_EXEC_RUNNER: MechanicalRunner = make_shell_runner(["pytest", "-q"])

UNIT_VERIFY_EXEC = NodeType(
    name="unit_verify_exec",
    kind=NodeKind.MECHANICAL,
    input_schema=MechanicalIO,
    output_schema=MechanicalIO,
    permission_profile="mechanical_default",
    required_upstream_node_types=frozenset({"implement_exec"}),
    produces_gate_evidence=True,
)
