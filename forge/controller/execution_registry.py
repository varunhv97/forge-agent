from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel

from forge.models.common import EffortLevel
from forge.models.graph import TaskSpec
from forge.models.permissions import PermissionProfile
from forge.models.request import Request


@dataclass
class ExecutionRunOutcome:
    """
    What an execution runner (the thing that actually drives an LLM
    session — Claude Agent SDK today) reports back. Deliberately minimal:
    `completed` is whether the session itself ran without the adapter call
    erroring out (network failure, auth failure, the process crashing —
    procedural failures, not a judgment about the work's quality), and
    `summary` is free text for the trace/evidence record. The dispatcher
    never trusts this for `result_commit`/`changed_paths` — those are
    derived mechanically from git afterward, regardless of what a runner
    reports here.

    `session_id` is the real Claude Agent SDK session id (a UUID, carried
    on the terminal `ResultMessage` of a live session) for a Claude Agent
    SDK-dispatched attempt, or `None` for anything else (a MECHANICAL
    runner, a Codex CLI attempt, or an SDK session that never produced a
    `ResultMessage`). Forge only captures and persists this value — it
    never acts on it (e.g. no automatic `resume=` wiring into a later
    attempt); that's a separate, deliberately deferred decision. The point
    is solely so a person can run `claude --resume <session_id>`
    themselves to inspect or continue that exact session.
    """

    completed: bool
    summary: str = ""
    session_id: str | None = None


class ExecutionRunner(Protocol):
    """
    (model, prompt, permission_profile, worktree_path) -> ExecutionRunOutcome,
    plus a keyword-only `effort` the dispatcher passes on every call (see
    `forge.controller.model_registry.select_effort`). Declared as a
    Protocol rather than a plain `Callable[[...], ...]` type alias
    specifically so this keyword-only parameter can be expressed at all —
    `Callable[[...], ...]` can only describe positional parameters.
    `effort` is `None` whenever a node type configures no
    default_effort/escalation_effort, which is the common case for most
    node types today; every concrete runner (`run_sdk_node`, `RunWorker.
    _run_sdk_node_with_chat`) accepts it and simply does nothing SDK-side
    when it's `None`, same as `chat_queue` already does for "no chat this
    attempt."
    """

    def __call__(
        self,
        model: str,
        prompt: str,
        profile: PermissionProfile,
        worktree_path: str,
        *,
        effort: EffortLevel | None = None,
    ) -> ExecutionRunOutcome: ...


InputT = TypeVar("InputT", bound=BaseModel)


@dataclass
class ExecutionNodeHandlers(Generic[InputT]):
    """
    The two pure functions an EXECUTION node type owns: how to build its
    typed input from task/request/upstream context, and how to turn that
    input into the actual prompt text. Neither function makes a model call
    or touches a worktree — the dispatcher does that, using whatever these
    produce.
    """

    build_input: Callable[[TaskSpec, Request, Mapping[str, BaseModel]], InputT]
    build_prompt: Callable[[InputT], str]


class UnknownExecutionHandlersError(KeyError):
    pass


class ExecutionHandlerRegistry:
    """
    Maps an EXECUTION NodeType's name to its `ExecutionNodeHandlers` — the
    EXECUTION-node counterpart to `MechanicalRunnerRegistry`. A node
    module (under `forge/nodes/`) registers its own handlers here; the
    dispatcher looks them up by name, never by importing a specific node
    module directly.
    """

    def __init__(self) -> None:
        # Heterogeneous across node types; each entry's build_input/build_prompt
        # pairing is type-checked where its ExecutionNodeHandlers is constructed.
        self._handlers: dict[str, ExecutionNodeHandlers[Any]] = {}

    def register(self, node_type_name: str, handlers: ExecutionNodeHandlers[Any]) -> None:
        if node_type_name in self._handlers:
            raise ValueError(f"execution handlers for '{node_type_name}' already registered")
        self._handlers[node_type_name] = handlers

    def get(self, node_type_name: str) -> ExecutionNodeHandlers[Any]:
        try:
            return self._handlers[node_type_name]
        except KeyError as exc:
            raise UnknownExecutionHandlersError(node_type_name) from exc
