from __future__ import annotations

from forge.controller.mechanical import MechanicalContext
from forge.models.common import NodeKind
from forge.models.graph import TaskSpec
from forge.nodes.unit_verify_exec import UNIT_VERIFY_EXEC, UNIT_VERIFY_EXEC_RUNNER


def _task() -> TaskSpec:
    return TaskSpec(
        id="t1",
        description="verify",
        node_kind=NodeKind.MECHANICAL,
        node_type="unit_verify_exec",
        permission_profile="mechanical_default",
    )


def test_node_type_fields():
    assert UNIT_VERIFY_EXEC.kind == NodeKind.MECHANICAL
    assert UNIT_VERIFY_EXEC.produces_gate_evidence is True
    assert UNIT_VERIFY_EXEC.required_upstream_node_types == frozenset({"implement_exec"})
    assert UNIT_VERIFY_EXEC.permission_profile == "mechanical_default"


def test_runner_passes_in_a_repo_with_passing_tests(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    result = UNIT_VERIFY_EXEC_RUNNER(_task(), MechanicalContext(worktree_path=str(tmp_path)))
    assert result.passed is True


def test_runner_fails_in_a_repo_with_failing_tests(tmp_path):
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert False\n")
    result = UNIT_VERIFY_EXEC_RUNNER(_task(), MechanicalContext(worktree_path=str(tmp_path)))
    assert result.passed is False


def test_runner_uses_current_directory_when_no_worktree_given(tmp_path, monkeypatch):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    monkeypatch.chdir(tmp_path)
    result = UNIT_VERIFY_EXEC_RUNNER(_task(), MechanicalContext())
    assert result.passed is True
