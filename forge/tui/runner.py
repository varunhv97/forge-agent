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

`RunWorker.live_messages` (post-M9 phase 3, the live trace window) is the
same shape, opposite direction: a `queue.Queue[Any] | None`, also set up
and torn down by `_run_sdk_node_with_chat` alongside `active_chat_queue`
(same `finally`, literally copied, per this phase's own explicit
instruction to reuse that pattern rather than reinvent it) — non-`None`
for exactly the duration of one EXECUTION attempt, `None` the rest of the
time (between attempts, during a MECHANICAL one, before the first attempt,
after the run finishes). Unbounded (`queue.Queue()`, no `maxsize`) is a
deliberate choice, not an oversight: a single attempt's `receive_response()`
turn yields a small, human-paced number of discrete messages (complete
`AssistantMessage`/`UserMessage`/`ResultMessage` objects, not raw streaming
token deltas — confirmed from `claude_agent_sdk.types`), so even an
unread backlog across a whole attempt is at most dozens of small objects,
never an unbounded-growth risk the way a token-level stream would be; a
bounded queue would only add a drop policy (oldest? newest?) this volume
never needs. `LiveRunScreen` drains it on every 0.75s poll tick — see
that module's own docstring for how it still drains whatever was already
enqueued even after this attribute has gone back to `None` (the attempt
finished before the last drain), so a message that arrives right at the
end of an attempt is never silently lost, only ever delivered late by at
most one poll tick.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any

from forge.adapters.deploy.vercel_adapter import VercelDeployAdapter
from forge.adapters.execution.sdk_node_runner import run_sdk_node
from forge.controller import db
from forge.controller.execution_registry import (
    EffortSwapRequest,
    ExecutionHandlerRegistry,
    ExecutionRunOutcome,
)
from forge.controller.mechanical import MechanicalRunnerRegistry
from forge.controller.model_registry import build_default_model_registry
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
        # Same lifecycle, opposite direction — see this module's own
        # docstring for the unbounded-queue/backpressure reasoning.
        self.live_messages: queue.Queue[Any] | None = None
        # Same lifecycle again (post-M9 phase 4, in-place model/effort
        # swap): non-`None` for exactly the duration of one EXECUTION
        # attempt, set/cleared by `_run_sdk_node_with_chat` in the same
        # `finally` as the two above. `model_swap_queue` carries a plain
        # model string for the fast, no-interruption path
        # (`ClaudeSDKClient.set_model`); `effort_swap_queue` carries a
        # validated `EffortSwapRequest` for the interrupt-and-reconnect
        # path (there is no in-place way to change effort). Neither is
        # ever written to directly by the TUI — only
        # `forge.controller.decisions.request_execution_swap`, via
        # `ForgeApp.request_execution_swap`, pushes onto either, after
        # validating the request against the real `ModelRegistry`.
        self.model_swap_queue: queue.Queue[str] | None = None
        self.effort_swap_queue: queue.Queue[EffortSwapRequest] | None = None

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
        resume_session_id: str | None = None,
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
        for it, just forwards it straight to `run_sdk_node`. `resume_session_id`
        is the same shape again (post-M9 phase 4): the scheduler passes it
        when this dispatch is the one honoring a prior INTERRUPTED_FOR_SWAP
        attempt's resume request; this method just forwards it too.

        `live_messages`/`model_swap_queue`/`effort_swap_queue` each get the
        exact same fresh-queue/set/finally-clear treatment as `chat_queue`,
        literally copied — this module's own docstring already worked out
        that pattern for one cross-thread channel, and nothing about
        adding three more (two more directions of the same "a queue the
        TUI's main thread pushes a validated request onto" shape
        `chat_queue` already is, one more of `live_messages`'s "the
        background thread pushes data out" shape) changes it. A request
        still sitting on `model_swap_queue`/`effort_swap_queue` the instant
        this attempt ends normally (not via a swap at all) is handled the
        same way a stale `chat_queue` message already is: the queue object
        these variables pointed at is simply never read again after
        they're cleared back to `None` below, so a `.put()` racing the end
        of this method either lands on a queue nothing drains any more or
        is rejected outright by `ForgeApp.request_execution_swap` once it
        observes the attribute back at `None` — never delivered into a
        stale or future attempt's session.
        """
        chat_queue: queue.Queue[str] = queue.Queue()
        live_messages: queue.Queue[Any] = queue.Queue()
        model_swap_queue: queue.Queue[str] = queue.Queue()
        effort_swap_queue: queue.Queue[EffortSwapRequest] = queue.Queue()
        self.active_chat_queue = chat_queue
        self.live_messages = live_messages
        self.model_swap_queue = model_swap_queue
        self.effort_swap_queue = effort_swap_queue
        try:
            return run_sdk_node(
                model,
                prompt,
                profile,
                worktree_path,
                chat_queue=chat_queue,
                effort=effort,
                live_messages=live_messages,
                model_swap_queue=model_swap_queue,
                effort_swap_queue=effort_swap_queue,
                resume_session_id=resume_session_id,
            )
        finally:
            self.active_chat_queue = None
            self.live_messages = None
            self.model_swap_queue = None
            self.effort_swap_queue = None

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
                model_registry=build_default_model_registry(),
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
