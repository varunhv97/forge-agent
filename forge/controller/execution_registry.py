from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel

from forge.models.common import EffortLevel
from forge.models.graph import TaskSpec
from forge.models.permissions import PermissionProfile
from forge.models.request import Request


@dataclass(frozen=True)
class EffortSwapRequest:
    """
    A validated, user-requested effort (and optionally model) change for a
    currently-live EXECUTION attempt. Built only by
    `forge.controller.decisions.request_execution_swap` (the one validated
    path — never constructed from unvalidated UI input directly) and then:

    - put onto `RunWorker.effort_swap_queue` (`forge/tui/runner.py`) by that
      function, to reach the running attempt's own interrupt-watching task
      (`sdk_execution_adapter._watch_effort_swap`);
    - carried back out on `ExecutionRunOutcome.swap_requested` once that
      attempt has been interrupted, for `dispatch_execution_attempt` to
      persist onto the interrupted `Attempt`'s own `pending_resume_model`/
      `pending_resume_effort` fields, which its *next* dispatch reads back.

    `effort` is required (this is the one thing that forces the
    interrupt-and-reconnect path at all — a model-only change never
    constructs one of these, it goes straight onto `model_swap_queue`
    instead, see `request_execution_swap`); `model`, when also given,
    rides along on the same reconnect rather than costing a second one.
    """

    effort: EffortLevel
    model: str | None = None


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
    # Set when this attempt was interrupted because a person asked to
    # change its effort (optionally bundled with a model change) in place
    # — see `EffortSwapRequest`'s own docstring for the full round trip.
    # `completed` is still `True` in this case (the SDK call itself didn't
    # error; the session was cut short on purpose, not by a procedural
    # failure) — `dispatch_execution_attempt` checks `swap_requested`
    # *before* `completed` for exactly this reason, so this outcome is
    # never scored as a normal pass/fail attempt. `session_id` (above) is
    # still populated as usual in this case too — it's the session to
    # resume from, read off the same terminal `ResultMessage` an ordinary
    # turn would have produced (confirmed: an interrupted turn still ends
    # with one, see `sdk_execution_adapter._watch_effort_swap`'s
    # docstring). `None` for every ordinary pass/fail outcome, and always
    # `None` for a MECHANICAL runner or a Codex CLI attempt.
    swap_requested: EffortSwapRequest | None = None


class ExecutionRunner(Protocol):
    """
    (model, prompt, permission_profile, worktree_path) -> ExecutionRunOutcome,
    plus two keyword-only parameters the dispatcher passes on every call:
    `effort` (see `forge.controller.model_registry.select_effort`) and
    `resume_session_id` (the real Claude Agent SDK session id to resume
    from, when this dispatch is the one honoring a prior
    INTERRUPTED_FOR_SWAP attempt's pending_resume_model/
    pending_resume_effort — see `forge.controller.scheduler.
    dispatch_execution_attempt`). Declared as a Protocol rather than a
    plain `Callable[[...], ...]` type alias specifically so these
    keyword-only parameters can be expressed at all — `Callable[[...],
    ...]` can only describe positional parameters. Both are `None` in the
    overwhelmingly common case (no effort configured; a fresh, non-resumed
    dispatch); every concrete runner (`run_sdk_node`, `RunWorker.
    _run_sdk_node_with_chat`) accepts both and simply does nothing SDK-side
    when they're `None`, same as `chat_queue` already does for "no chat
    this attempt."
    """

    def __call__(
        self,
        model: str,
        prompt: str,
        profile: PermissionProfile,
        worktree_path: str,
        *,
        effort: EffortLevel | None = None,
        resume_session_id: str | None = None,
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
