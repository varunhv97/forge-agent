from __future__ import annotations

from forge.controller.mechanical import MechanicalContext
from forge.models.common import NodeKind
from forge.models.graph import TaskSpec
from forge.nodes.integration_verify_exec import (
    INTEGRATION_VERIFY_EXEC,
    INTEGRATION_VERIFY_EXEC_RUNNER,
)


def _task() -> TaskSpec:
    return TaskSpec(
        id="t1",
        description="verify",
        node_kind=NodeKind.MECHANICAL,
        node_type="integration_verify_exec",
        permission_profile="mechanical_default",
    )


def test_node_type_fields():
    assert INTEGRATION_VERIFY_EXEC.kind == NodeKind.MECHANICAL
    assert INTEGRATION_VERIFY_EXEC.produces_gate_evidence is True
    assert INTEGRATION_VERIFY_EXEC.required_upstream_node_types == frozenset(
        {"integration_test_write_exec"}
    )


def test_runner_passes_in_a_repo_with_passing_tests(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    result = INTEGRATION_VERIFY_EXEC_RUNNER(_task(), MechanicalContext(worktree_path=str(tmp_path)))
    assert result.passed is True
