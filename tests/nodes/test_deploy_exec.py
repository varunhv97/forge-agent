from __future__ import annotations

from pathlib import Path

from forge.adapters.deploy.vercel_adapter import (
    DeploymentResult,
    VercelAuthenticationError,
    VercelDeploymentError,
)
from forge.controller.mechanical import MechanicalContext
from forge.models.common import NodeKind
from forge.models.graph import TaskSpec
from forge.nodes.deploy_exec import (
    DEPLOY_EXEC,
    DeployExecOutput,
    _collect_worktree_files,
    make_deploy_runner,
)


def _task() -> TaskSpec:
    return TaskSpec(
        id="t1",
        description="deploy",
        node_kind=NodeKind.MECHANICAL,
        node_type="deploy_exec",
        permission_profile="deploy_default",
    )


class _FakeAdapter:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls = []

    def deploy(self, *, project_name, files, target):
        self.calls.append({"project_name": project_name, "files": files, "target": target})
        if self._error is not None:
            raise self._error
        return self._result


def test_node_type_fields():
    assert DEPLOY_EXEC.kind == NodeKind.MECHANICAL
    assert DEPLOY_EXEC.output_schema is DeployExecOutput
    assert DEPLOY_EXEC.required_upstream_node_types == frozenset({"unit_verify_exec"})
    assert DEPLOY_EXEC.produces_gate_evidence is True
    assert DEPLOY_EXEC.permission_profile == "deploy_default"


def test_collect_worktree_files_skips_git_and_forge_dirs(tmp_path: Path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("not part of the app\n")
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "state.json").write_text("{}\n")
    nested = tmp_path / "api"
    nested.mkdir()
    (nested / "index.py").write_text("print('nested')\n")

    files = _collect_worktree_files(str(tmp_path))

    paths = {f.path for f in files}
    assert paths == {"app.py", "api/index.py"}


def test_collect_worktree_files_skips_unreadable_binary_files(tmp_path: Path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / "image.bin").write_bytes(b"\xff\xfe\x00\x01binary-garbage")

    files = _collect_worktree_files(str(tmp_path))

    assert [f.path for f in files] == ["app.py"]


def test_runner_fails_with_no_upstream_worktree():
    runner = make_deploy_runner(_FakeAdapter())
    result = runner(_task(), MechanicalContext(worktree_path=None))
    assert result.passed is False
    assert "worktree" in result.summary


def test_runner_fails_with_no_files(tmp_path: Path):
    runner = make_deploy_runner(_FakeAdapter())
    result = runner(_task(), MechanicalContext(worktree_path=str(tmp_path)))
    assert result.passed is False
    assert "no files" in result.summary


def test_runner_succeeds_and_populates_outputs(tmp_path: Path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    adapter = _FakeAdapter(
        result=DeploymentResult(
            deployment_id="dep123", url="forge-abc.vercel.app", ready_state="READY"
        )
    )
    runner = make_deploy_runner(adapter)

    result = runner(_task(), MechanicalContext(worktree_path=str(tmp_path)))

    assert result.passed is True
    assert result.outputs["deployment_url"] == "forge-abc.vercel.app"
    assert result.outputs["deployment_id"] == "dep123"
    assert "forge-abc.vercel.app" in result.outputs["summary"]
    # DeployExecOutput must validate against exactly what the runner produced.
    DeployExecOutput.model_validate(result.outputs)

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["project_name"].startswith("forge-t1-")
    assert adapter.calls[0]["target"] == "production"


def test_runner_reports_failure_on_deployment_error(tmp_path: Path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    adapter = _FakeAdapter(error=VercelDeploymentError("boom"))
    runner = make_deploy_runner(adapter)

    result = runner(_task(), MechanicalContext(worktree_path=str(tmp_path)))

    assert result.passed is False
    assert "deployment failed" in result.summary
    assert "boom" in result.summary
    assert result.requires_user_input is False


def test_runner_flags_requires_user_input_on_credential_error(tmp_path: Path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    adapter = _FakeAdapter(error=VercelAuthenticationError(403, '{"error":{"invalidToken":true}}'))
    runner = make_deploy_runner(adapter)

    result = runner(_task(), MechanicalContext(worktree_path=str(tmp_path)))

    assert result.passed is False
    assert result.requires_user_input is True
    assert "deployment failed" in result.summary
