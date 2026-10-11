from __future__ import annotations

from pydantic import BaseModel

from forge.controller.mechanical import MechanicalRunner, make_shell_runner
from forge.controller.node_registry import NodeType
from forge.models.common import NodeKind


class MechanicalIO(BaseModel):
    """Empty placeholder input/output schema — see unit_verify_exec.py for why."""


# Runs the full test suite (unit tests plus whatever integration_test_write_exec
# added) inside the merged integration worktree. Mechanical and deterministic,
# same as unit_verify_exec — it's the pytest invocation that differs by
# intent, not by mechanism.
INTEGRATION_VERIFY_EXEC_RUNNER: MechanicalRunner = make_shell_runner(["pytest", "-q"])

INTEGRATION_VERIFY_EXEC = NodeType(
    name="integration_verify_exec",
    kind=NodeKind.MECHANICAL,
    input_schema=MechanicalIO,
    output_schema=MechanicalIO,
    permission_profile="mechanical_default",
    required_upstream_node_types=frozenset({"integration_test_write_exec"}),
    produces_gate_evidence=True,
)
