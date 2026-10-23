"""
Tests for `forge/tui/runner.py`'s `RunWorker` — the background-thread
bridge between Textual's asyncio event loop and the synchronous
`run_to_completion` scheduler loop. These don't touch Textual at all: they
exercise the thread/own-connection contract directly, the same way
`tests/cli/test_run_resume_list.py` exercises `run_to_completion` through
`forge_cli.main` but via `RunWorker` instead.
"""

from __future__ import annotations

import contextlib
import queue
import time
from pathlib import Path

import pytest

from forge.controller import db
from forge.controller.execution_registry import ExecutionRunOutcome
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

    assert worker.done.wait(timeout=30), "RunWorker did not finish within 30s"
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
    worker.done.wait(timeout=30)

    with pytest.raises(RuntimeError):
        worker.start()


# -- mid-run chat: active_chat_queue (M9 phase 6) ---------------------------


def test_active_chat_queue_is_none_before_a_run_ever_starts(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.setattr(runner_module, "run_sdk_node", fake_execution_runner)
    db_path, run_id = _start_stalled_run(git_repo)

    worker = RunWorker(run_id, db_path, git_repo)
    assert worker.active_chat_queue is None


def test_active_chat_queue_is_set_only_for_the_execution_task_and_cleared_after(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    """
    `implement-and-verify` (the fixture DAG `_start_stalled_run` builds via
    `write_dag`) is `implement_exec` (EXECUTION) -> `unit_verify_exec`
    (MECHANICAL). `run_sdk_node` — the thing `RunWorker._run_sdk_node_with_
    chat` wraps — is only ever called for the EXECUTION task; a MECHANICAL
    one never reaches it at all. This is the regression test for "a
    MECHANICAL attempt never gets a chat queue": if `unit_verify_exec`
    somehow caused a second call, `seen_chat_queues` would have two
    entries instead of one.
    """
    seen_chat_queues: list[queue.Queue[str] | None] = []

    def _tracking_runner(
        model: str,
        prompt: str,
        profile: object,
        worktree_path: str,
        *,
        chat_queue: queue.Queue[str] | None = None,
    ) -> ExecutionRunOutcome:
        seen_chat_queues.append(chat_queue)
        return fake_execution_runner(model, prompt, profile, worktree_path, chat_queue=chat_queue)

    monkeypatch.setattr(runner_module, "run_sdk_node", _tracking_runner)
    db_path, run_id = _start_stalled_run(git_repo)

    worker = RunWorker(run_id, db_path, git_repo)
    assert worker.active_chat_queue is None
    worker.start()

    assert worker.done.wait(timeout=30), "RunWorker did not finish within 30s"
    assert worker.error is None

    assert len(seen_chat_queues) == 1
    assert seen_chat_queues[0] is not None
    # Cleared once that one attempt finished — not left dangling after the
    # whole run has also gone terminal.
    assert worker.active_chat_queue is None


def test_message_put_on_active_chat_queue_reaches_the_in_flight_attempt(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    """
    Proves the cross-thread hand-off end to end at this layer, with no
    Textual involved: a message put onto `worker.active_chat_queue` while
    the EXECUTION attempt is genuinely still in flight is the exact same
    object the (fake) runner is blocked reading from — not a copy, not a
    different queue for a different attempt. `tests/tui/test_chat.py`
    covers the same wiring again, but driven through the real chat
    `Input` widget instead of poking the queue directly.
    """
    received: list[str] = []

    def _blocking_runner(
        model: str,
        prompt: str,
        profile: object,
        worktree_path: str,
        *,
        chat_queue: queue.Queue[str] | None = None,
    ) -> ExecutionRunOutcome:
        if chat_queue is not None:
            with contextlib.suppress(queue.Empty):
                received.append(chat_queue.get(timeout=15))
        return fake_execution_runner(model, prompt, profile, worktree_path)

    monkeypatch.setattr(runner_module, "run_sdk_node", _blocking_runner)
    db_path, run_id = _start_stalled_run(git_repo)
    worker = RunWorker(run_id, db_path, git_repo)
    worker.start()

    # Generous poll for the attempt to actually reach the EXECUTION task
    # and set the queue up — thread startup/scheduling is not instant.
    deadline = time.monotonic() + 15.0
    chat_queue = worker.active_chat_queue
    while chat_queue is None and time.monotonic() < deadline:
        time.sleep(0.05)
        chat_queue = worker.active_chat_queue
    assert chat_queue is not None, "active_chat_queue was never set within 15s"

    chat_queue.put("hello from the main thread")

    assert worker.done.wait(timeout=30), "RunWorker did not finish within 30s"
    assert worker.error is None
    assert received == ["hello from the main thread"]
    assert worker.active_chat_queue is None
