from __future__ import annotations

import uuid
from pathlib import Path
from typing import List

from pydantic import BaseModel

from forge.adapters.deploy.vercel_adapter import (
    DeploymentFile,
    VercelCredentialError,
    VercelDeployAdapter,
    VercelDeploymentError,
)
from forge.controller.mechanical import MechanicalContext, MechanicalResult, MechanicalRunner
from forge.controller.node_registry import NodeType
from forge.models.common import NodeKind
from forge.models.graph import TaskSpec


# Mirrors unit_verify_exec.py's MechanicalIO — deploy_exec's input has no
# model-facing schema (it's mechanical), but its *output* is real: a
# downstream node (manual_test_exec) needs the deployed URL, so this is
# not the same empty placeholder on both sides.
class DeployExecInput(BaseModel):
    pass


class DeployExecOutput(BaseModel):
    deployment_url: str
    deployment_id: str
    summary: str


# Paths never uploaded to Vercel regardless of what's sitting in the
# worktree — git's own metadata and this project's own attempt-isolation
# bookkeeping, neither of which is part of the application being deployed.
_EXCLUDED_DIR_NAMES = {".git", ".forge"}


def _collect_worktree_files(worktree_path: str) -> List[DeploymentFile]:
    """
    Reads every text file in `worktree_path` (recursively, skipping git/
    forge bookkeeping directories) into a `DeploymentFile` list. Binary
    files are skipped rather than crashing the deploy — a toy app built by
    implement_exec is expected to be source text, and silently dropping
    something genuinely binary is a real limitation worth being honest
    about rather than guessing at base64 handling for a case this
    milestone doesn't need yet.
    """
    root = Path(worktree_path)
    files: List[DeploymentFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _EXCLUDED_DIR_NAMES for part in path.relative_to(root).parts):
            continue
        try:
            content = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        files.append(DeploymentFile(path=str(path.relative_to(root)), content=content))
    return files


def make_deploy_runner(adapter: VercelDeployAdapter) -> MechanicalRunner:
    """
    Builds a mechanical runner that deploys whatever is in the upstream
    worktree to Vercel via `adapter`. The project name is generated fresh
    per attempt (`forge-<task id>-<random suffix>`) rather than being
    fixed, since this milestone's target is a disposable demo app, not a
    stable named project a real deploy would reuse across runs.
    """

    def runner(task: TaskSpec, context: MechanicalContext) -> MechanicalResult:
        if not context.worktree_path:
            return MechanicalResult(
                passed=False, summary="deploy_exec has no upstream worktree to deploy from"
            )
        files = _collect_worktree_files(context.worktree_path)
        if not files:
            return MechanicalResult(passed=False, summary="no files found to deploy")

        project_name = f"forge-{task.id}-{uuid.uuid4().hex[:8]}"
        try:
            result = adapter.deploy(project_name=project_name, files=files, target="production")
        except VercelCredentialError as exc:
            # No further attempt with the same token could succeed — flagged
            # so the scheduler skips straight to REQUEST_USER_INPUT instead
            # of spending this task's retry budget on guaranteed repeats.
            return MechanicalResult(
                passed=False, summary=f"deployment failed: {exc}", requires_user_input=True
            )
        except VercelDeploymentError as exc:
            return MechanicalResult(passed=False, summary=f"deployment failed: {exc}")

        return MechanicalResult(
            passed=True,
            summary=f"deployed to https://{result.url}",
            outputs={
                "deployment_url": result.url,
                "deployment_id": result.deployment_id,
                "summary": f"deployed to https://{result.url}",
            },
        )

    return runner


DEPLOY_EXEC = NodeType(
    name="deploy_exec",
    kind=NodeKind.MECHANICAL,
    input_schema=DeployExecInput,
    output_schema=DeployExecOutput,
    permission_profile="deploy_default",
    required_upstream_node_types=frozenset({"unit_verify_exec"}),
    produces_gate_evidence=True,
)
