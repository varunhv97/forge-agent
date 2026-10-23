"""
Tests for `forge/tui/runner.py`'s `RunWorker` — the background-thread
bridge between Textual's asyncio event loop and the synchronous
`run_to_completion` scheduler loop. These don't touch Textual at all: they
exercise the thread/own-connection contract directly, the same way
`tests/cli/test_run_resume_list.py` exercises `run_to_completion` through
`forge_cli.main` but via `RunWorker` instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.controller import db
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.controller.scheduler import start_run
from forge.controller.store import Store
from forge.controller.validator import compile_and_validate
from forge.models.permissions import Budgets
from forge.models.run import RunStatus
from forge.modes.custom_dag import build_custom_dag_request_and_graph
from forge.nodes import register_all
from forge.tui import runner as runner_module
from forge.tui.runner import RunWorker
from tests.tui.conftest import fake_execution_runner, write_dag


def _start_stalled_run(git_repo: Path) -> tuple[Path, str]:
    """
    Builds and persists a run exactly like `forge run`/`ForgeApp.
    start_new_run` do, but stops short of dispatching anything — the
    scheduler loop itself is `RunWorker`'s job, not this helper's.
    """
    write_dag(git_repo)
    db_path = git_repo / "forge.db"
    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()
    register_all(node_registry, permission_registry)
    request, draft = build_custom_dag_request_and_graph(
        git_repo / ".forge" / "dags" / "implement-and-verify.json",
        {"objective": "build a tiny thing"},
        node_registry,
        permission_registry,
        Budgets(max_cost_usd=5.0, max_wall_seconds=900),
        deploy_authorized=True,
    )
    graph = compile_and_validate(draft, request, node_registry, permission_registry)

    conn = db.connect(db_path)
    store = Store(conn)
    try:
        run = start_run(request, graph, store)
    finally:
        store.close()
    return db_path, run.id


def test_run_worker_drives_a_run_to_completion_on_its_own_thread(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.setattr(runner_module, "run_sdk_node", fake_execution_runner)
    db_path, run_id = _start_stalled_run(git_repo)

    worker = RunWorker(run_id, db_path, git_repo)
    assert not worker.is_alive()
    worker.start()

    assert worker.done.wait(timeout=15), "RunWorker did not finish within 15s"
    assert worker.error is None
    assert not worker.is_alive()

    conn = db.connect(db_path)
    store = Store(conn)
    try:
        run = store.load_run(run_id)
    finally:
        store.close()
    assert run.status == RunStatus.COMPLETED_VERIFIED


def test_run_worker_refuses_to_start_twice(monkeypatch: pytest.MonkeyPatch, git_repo: Path) -> None:
    monkeypatch.setattr(runner_module, "run_sdk_node", fake_execution_runner)
    db_path, run_id = _start_stalled_run(git_repo)

    worker = RunWorker(run_id, db_path, git_repo)
    worker.start()
    worker.done.wait(timeout=15)

    with pytest.raises(RuntimeError):
        worker.start()
