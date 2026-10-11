from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict

from pydantic import BaseModel

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
    """

    completed: bool
    summary: str = ""


# (model, prompt, permission_profile, worktree_path) -> ExecutionRunOutcome
ExecutionRunner = Callable[[str, str, PermissionProfile, str], ExecutionRunOutcome]


@dataclass
class ExecutionNodeHandlers:
    """
    The two pure functions an EXECUTION node type owns: how to build its
    typed input from task/request/upstream context, and how to turn that
    input into the actual prompt text. Neither function makes a model call
    or touches a worktree — the dispatcher does that, using whatever these
    produce.
    """

    build_input: Callable[[TaskSpec, Request, Dict[str, BaseModel]], BaseModel]
    build_prompt: Callable[[BaseModel], str]


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
        self._handlers: Dict[str, ExecutionNodeHandlers] = {}

    def register(self, node_type_name: str, handlers: ExecutionNodeHandlers) -> None:
        if node_type_name in self._handlers:
            raise ValueError(f"execution handlers for '{node_type_name}' already registered")
        self._handlers[node_type_name] = handlers

    def get(self, node_type_name: str) -> ExecutionNodeHandlers:
        try:
            return self._handlers[node_type_name]
        except KeyError as exc:
            raise UnknownExecutionHandlersError(node_type_name) from exc
