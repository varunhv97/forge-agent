#!/usr/bin/env python3
"""
Live smoke test for Forge's EXECUTION dispatch path.

Runs a real implement_exec -> unit_verify_exec chain through the actual
Claude Agent SDK, in a disposable toy git repo: a real worktree gets
created, a real `claude` CLI session edits files in it, the resulting
commit/changed paths are derived from real git, and a real `pytest -q`
subprocess verifies the result.

This needs an authenticated `claude` CLI session (the same one your
interactive terminal already uses — if `claude` works for you normally,
this should too), so it is not part of the offline test suite. Run it
from your own terminal.

Usage:
    source .venv/bin/activate
    python scripts/live_smoke_test.py
    python scripts/live_smoke_test.py --objective "..." --keep
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.adapters.execution.sdk_node_runner import run_sdk_node  # noqa: E402
from forge.controller import db  # noqa: E402
from forge.controller.execution_registry import ExecutionHandlerRegistry  # noqa: E402
from forge.controller.mechanical import MechanicalRunnerRegistry  # noqa: E402
from forge.controller.model_registry import ModelRegistry  # noqa: E402
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry  # noqa: E402
from forge.controller.scheduler import (
    ControllerDependencies,
    run_to_completion,
    start_run,
)  # noqa: E402
from forge.controller.store import Store  # noqa: E402
from forge.models.common import NodeKind  # noqa: E402
from forge.models.graph import EdgeSpec, GraphVersion, TaskSpec  # noqa: E402
from forge.models.permissions import Budgets  # noqa: E402
from forge.models.request import AcceptanceCriterion, Request  # noqa: E402
from forge.models.run import RunStatus  # noqa: E402
from forge.nodes import register_all  # noqa: E402

DEFAULT_OBJECTIVE = (
    "Create math_utils.py with a function add(a, b) that returns a + b, " "and a unit test for it."
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_toy_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.email", "forge-smoke-test@example.com")
    _git(path, "config", "user.name", "Forge Smoke Test")
    (path / "README.md").write_text("Forge live smoke test scratch repo.\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "initial")


def build_graph(objective: str) -> tuple[Request, GraphVersion]:
    criterion = AcceptanceCriterion(
        id="c1", description="unit tests pass", verification_method="automated_test"
    )
    request = Request(
        id=f"req-{uuid.uuid4().hex[:8]}",
        mode="feature_blast",
        objective_text=objective,
        acceptance_criteria=[criterion],
        budgets=Budgets(max_cost_usd=5.0, max_wall_seconds=600),
        created_at=datetime.now(timezone.utc),
    )
    t1 = TaskSpec(
        id="t1",
        description=objective,
        node_kind=NodeKind.EXECUTION,
        node_type="implement_exec",
        permission_profile="code_edit_default",
    )
    t2 = TaskSpec(
        id="t2",
        description="verify the unit tests implement_exec wrote",
        node_kind=NodeKind.MECHANICAL,
        node_type="unit_verify_exec",
        depends_on=["t1"],
        acceptance_criterion_ids=["c1"],
        permission_profile="mechanical_default",
    )
    graph = GraphVersion(
        id="gv-1",
        request_id=request.id,
        version=1,
        parent_version=None,
        tasks=[t1, t2],
        edges=[EdgeSpec(from_task="t1", to_task="t2", relation="dependency")],
        status="active",
        created_by="scripts/live_smoke_test.py",
        created_at=datetime.now(timezone.utc),
    )
    return request, graph


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objective", default=DEFAULT_OBJECTIVE)
    parser.add_argument(
        "--keep", action="store_true", help="don't delete the scratch repo afterward"
    )
    args = parser.parse_args()

    scratch_dir = Path(tempfile.mkdtemp(prefix="forge-smoke-"))
    repo_root = scratch_dir / "repo"
    print(f"Scratch repo: {repo_root}")
    make_toy_repo(repo_root)

    conn = db.connect(scratch_dir / "forge.db")
    store = Store(conn)

    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()
    mechanical_registry = MechanicalRunnerRegistry()
    execution_handler_registry = ExecutionHandlerRegistry()
    register_all(
        node_registry, permission_registry, mechanical_registry, execution_handler_registry
    )

    deps = ControllerDependencies(
        mechanical_registry=mechanical_registry,
        node_registry=node_registry,
        permission_registry=permission_registry,
        model_registry=ModelRegistry([]),
        execution_handler_registry=execution_handler_registry,
        execution_runner=run_sdk_node,
        repo_root=repo_root,
    )

    request, graph = build_graph(args.objective)

    print(f"Objective: {args.objective}")
    print("Starting run — this makes a real call to your authenticated `claude` CLI...\n")
    run = start_run(request, graph, store)
    result = run_to_completion(run.id, store, deps, max_ticks=20)

    print(f"Final run status: {result.status.value}")
    for attempt in sorted(store.load_attempts(run.id), key=lambda a: (a.task_id, a.attempt_number)):
        print(
            f"  {attempt.task_id} attempt#{attempt.attempt_number}: {attempt.status.value} "
            f"(worktree={attempt.worktree_path or '-'})"
        )
        if attempt.failure_summary:
            print(f"    failure: {attempt.failure_summary}")
        if attempt.result_commit:
            print(f"    result_commit: {attempt.result_commit}")

    conn.close()
    if args.keep:
        print(f"\nScratch repo kept at: {repo_root}")
    else:
        shutil.rmtree(scratch_dir, ignore_errors=True)

    return 0 if result.status == RunStatus.COMPLETED_VERIFIED else 1


if __name__ == "__main__":
    raise SystemExit(main())
