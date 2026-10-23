"""
Bridges the controller's synchronous scheduler loop (`run_to_completion`,
`forge/controller/scheduler.py`) into Textual's asyncio event loop without
blocking it: each `RunWorker` drives one run's loop on a plain
`threading.Thread`, with its own SQLite connection opened fresh inside that
thread — never a `sqlite3.Connection` shared across threads. WAL mode
(`forge/controller/db.py::connect`) is specifically what makes this safe:
one writer thread per `RunWorker`, independent reader connections (the
Textual app's own `Store`) polling concurrently.

The UI layer (`forge.tui.screens`) never imports anything here except to
start/inspect a `RunWorker` — it never reaches into the scheduler, the
registries, or a write-capable `Store` itself. That stays this module's
job alone, per the project's core invariant: nodes and UI code produce
intent, only `forge/controller/*` applies it.
"""

from __future__ import annotations

import threading
from pathlib import Path

from forge.adapters.deploy.vercel_adapter import VercelDeployAdapter
from forge.adapters.execution.sdk_node_runner import run_sdk_node
from forge.controller import db
from forge.controller.execution_registry import ExecutionHandlerRegistry
from forge.controller.mechanical import MechanicalRunnerRegistry
from forge.controller.model_registry import ModelRegistry
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.controller.scheduler import ControllerDependencies, run_to_completion
from forge.controller.store import Store
from forge.nodes import register_all


class RunWorker:
    """
    Owns one background thread driving `run_to_completion` for a single
    run. `start()` is idempotent-by-construction — callers are expected to
    create a fresh `RunWorker` per dispatch and track it (e.g. in
    `ForgeApp._live_workers`) so the same run is never driven by two
    threads at once; this class itself only guards against calling
    `start()` twice on the same instance.

    Mirrors exactly what `scripts/forge_cli.py`'s `_cmd_run`/`_cmd_resume`
    already do (build the real registries via `register_all`, wire
    `run_sdk_node` as the execution runner, call `run_to_completion`) —
    this is that same call, just moved onto a thread the Textual app
    doesn't have to block on.
    """

    def __init__(self, run_id: str, db_path: Path, repo_root: Path) -> None:
        self.run_id = run_id
        self.db_path = db_path
        self.repo_root = repo_root
        self.error: BaseException | None = None
        self.done = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError(f"RunWorker for '{self.run_id}' already started")
        thread = threading.Thread(target=self._run, name=f"forge-run-{self.run_id}", daemon=True)
        self._thread = thread
        thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        conn = db.connect(self.db_path)
        store = Store(conn)
        try:
            node_registry = NodeTypeRegistry()
            permission_registry = PermissionProfileRegistry()
            mechanical_registry = MechanicalRunnerRegistry()
            execution_handler_registry = ExecutionHandlerRegistry()
            register_all(
                node_registry,
                permission_registry,
                mechanical_registry,
                execution_handler_registry,
                deploy_adapter=VercelDeployAdapter(),
            )
            deps = ControllerDependencies(
                mechanical_registry=mechanical_registry,
                node_registry=node_registry,
                permission_registry=permission_registry,
                model_registry=ModelRegistry([]),
                execution_handler_registry=execution_handler_registry,
                execution_runner=run_sdk_node,
                repo_root=self.repo_root,
            )
            run_to_completion(self.run_id, store, deps)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the UI, never swallowed
            self.error = exc
        finally:
            store.close()
            self.done.set()
