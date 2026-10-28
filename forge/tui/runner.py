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

`RunWorker.active_chat_queue` is the one piece of chat infrastructure
this module adds (M9 phase 6): a `queue.Queue[str] | None` that's
non-`None` for exactly the duration of one EXECUTION attempt. It's set
and cleared by `_run_sdk_node_with_chat`, the method this module wires in
as the scheduler's `execution_runner` in place of `run_sdk_node` directly
— `run_to_completion` only ever calls that for an EXECUTION-kind task
(`dispatch_mechanical_attempt` never touches `execution_runner` at all),
so being called *is* the "this attempt is chattable" signal; no separate
node-kind check is needed here. `LiveRunScreen` reads this attribute from
the main thread via `ForgeApp.active_chat_queue_for` to decide whether its
chat input is enabled — a single thread-safe pointer (plain attribute
assignment is atomic under the GIL) is all that's needed, not a second
notification mechanism.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from forge.adapters.deploy.vercel_adapter import VercelDeployAdapter
from forge.adapters.execution.sdk_node_runner import run_sdk_node
from forge.controller import db
from forge.controller.execution_registry import ExecutionHandlerRegistry, ExecutionRunOutcome
from forge.controller.mechanical import MechanicalRunnerRegistry
from forge.controller.model_registry import ModelRegistry
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.controller.scheduler import ControllerDependencies, run_to_completion
from forge.controller.store import Store
from forge.models.common import EffortLevel
from forge.models.permissions import PermissionProfile
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
        # Non-None for exactly the duration of one EXECUTION attempt — see
        # `_run_sdk_node_with_chat` and this module's own docstring.
        self.active_chat_queue: queue.Queue[str] | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError(f"RunWorker for '{self.run_id}' already started")
        thread = threading.Thread(target=self._run, name=f"forge-run-{self.run_id}", daemon=True)
        self._thread = thread
        thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_sdk_node_with_chat(
        self,
        model: str,
        prompt: str,
        profile: PermissionProfile,
        worktree_path: str,
        *,
        effort: EffortLevel | None = None,
    ) -> ExecutionRunOutcome:
        """
        Wraps `run_sdk_node` so this `RunWorker` can expose, for exactly
        the duration of one EXECUTION attempt, a `queue.Queue` the TUI's
        main thread can put a chat message onto. A fresh queue per
        attempt (never reused across attempts) — `active_chat_queue` is
        set just before the call and always cleared in a `finally`, so a
        chat message sent after this attempt has already finished either
        lands on a queue nothing is reading from any more (a plain
        `queue.Queue.put` never blocks or raises for that) or, more
        likely, is rejected outright because `LiveRunScreen` already saw
        `active_chat_queue` go back to `None` — either way, never
        delivered into a stale or future attempt's session.

        `effort` is accepted keyword-only (same shape chat_queue already
        set) because this method is what's wired in as `ControllerDependencies.
        execution_runner`, which the scheduler now calls with `effort=`
        on every EXECUTION dispatch — this method has no logic of its own
        for it, just forwards it straight to `run_sdk_node`.
        """
        chat_queue: queue.Queue[str] = queue.Queue()
        self.active_chat_queue = chat_queue
        try:
            return run_sdk_node(
                model, prompt, profile, worktree_path, chat_queue=chat_queue, effort=effort
            )
        finally:
            self.active_chat_queue = None

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
                execution_runner=self._run_sdk_node_with_chat,
                repo_root=self.repo_root,
            )
            run_to_completion(self.run_id, store, deps)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the UI, never swallowed
            self.error = exc
        finally:
            store.close()
            self.done.set()
