from __future__ import annotations

from forge.controller.node_registry import NodeType
from forge.models.attempt import Attempt, AttemptStatus
from forge.models.common import EffortLevel
from forge.models.model_registry import ModelCapability

ESCALATION_FAILURE_THRESHOLD = 2


class UnknownModelError(KeyError):
    """Raised when a model_id isn't registered in the ModelRegistry at all."""


class ModelNotEligibleError(ValueError):
    """
    Raised when a model_id is registered but its eligible_roles doesn't
    include the requested node type. This is deliberately a ValueError, not
    a KeyError like UnknownModelError: the lookup itself succeeds (the model
    exists), so there's no missing key here, just a value that fails a
    validation rule for the context it's being used in.
    """


class NoDefaultModelConfiguredError(Exception):
    """
    Raised when a NodeType has no default_model set at all. A node type
    must have something to run on; returning None or "" would push this
    failure downstream into whatever tries to actually invoke the model,
    where it would be far less clear what went wrong.
    """


class ModelRegistry:
    """
    Wraps the set of Anthropic models permitted for use anywhere in the
    system. A decision node's proposed model (for a retry or escalation) is
    never trusted directly — it is checked here, against the node type it's
    proposed for, before the controller acts on it.
    """

    def __init__(self, capabilities: list[ModelCapability]) -> None:
        self._by_model_id: dict[str, ModelCapability] = {}
        for capability in capabilities:
            self._by_model_id[capability.model_id] = capability

    def get(self, model_id: str) -> ModelCapability:
        try:
            return self._by_model_id[model_id]
        except KeyError as exc:
            raise UnknownModelError(model_id) from exc

    def all_capabilities(self) -> list[ModelCapability]:
        """
        Every registered model, in registration order — for a UI picker
        (the live-run model/effort swap modal, `forge/tui/screens.py`)
        that needs to enumerate real options rather than look one up by
        id. Nothing needed full enumeration before this, mirroring why
        `NodeTypeRegistry.all_types()` didn't exist before the DAG
        builder's node-type palette needed one.
        """
        return list(self._by_model_id.values())

    def validate_eligible(self, model_id: str, node_type_name: str) -> ModelCapability:
        """
        Returns the ModelCapability for model_id if, and only if, it is
        registered AND node_type_name appears in its eligible_roles. Raises
        UnknownModelError for the former failure and ModelNotEligibleError
        for the latter, so callers can distinguish "this model doesn't
        exist" from "this model exists but isn't allowed for this role."
        """
        capability = self.get(model_id)
        if node_type_name not in capability.eligible_roles:
            raise ModelNotEligibleError(
                f"model '{model_id}' is not eligible for node type '{node_type_name}' "
                f"(eligible_roles={capability.eligible_roles})"
            )
        return capability


def build_default_model_registry() -> ModelRegistry:
    """
    The real `ModelRegistry` every live entry point (the TUI, `forge_cli.py`)
    should construct — every call site previously passed `ModelRegistry([])`,
    which meant a `model` override in a custom-DAG task (see
    `custom_dag.py::resolve_model_override`) could never actually pass
    eligibility in live use, only in tests that built their own populated
    registry. Pricing/context-window figures are current as of this file's
    last edit (checked against Anthropic's own published rates, not
    guessed) — update them if those change.

    `adapter_kind` is currently pure metadata: nothing in this codebase
    reads it to gate a decision (confirmed — only this file's docstrings
    and the `ModelCapability` definition itself reference it). Since
    `ModelRegistry` keys purely by `model_id` (one entry per id, not per
    `(model_id, adapter_kind)` pair), a model used both ways — claude-sonnet-5
    is Forge's default for three EXECUTION node types *and* the one
    registered decision node type (`retry_or_escalate_policy`, see
    `decision_dispatch.py::DECISION_MODEL_BY_NODE_TYPE`) — gets ONE entry
    whose `eligible_roles` lists every role it's valid for, tagged with
    whichever `adapter_kind` best describes its primary use here
    (`sdk_execution`, since three execution node types default to it versus
    one decision type).
    """
    execution_roles = ["implement_exec", "manual_test_exec", "integration_test_write_exec"]
    return ModelRegistry(
        [
            ModelCapability(
                provider="anthropic",
                model_id="claude-sonnet-5",
                adapter_kind="sdk_execution",
                supports_tools=True,
                context_window=1_000_000,
                cost_per_mtok_in=2.00,
                cost_per_mtok_out=10.00,
                eligible_roles=[*execution_roles, "retry_or_escalate_policy"],
            ),
            ModelCapability(
                provider="anthropic",
                model_id="claude-opus-5",
                adapter_kind="sdk_execution",
                supports_tools=True,
                context_window=1_000_000,
                cost_per_mtok_in=5.00,
                cost_per_mtok_out=25.00,
                eligible_roles=execution_roles,
            ),
            ModelCapability(
                provider="anthropic",
                model_id="claude-haiku-4-5",
                adapter_kind="sdk_execution",
                supports_tools=True,
                context_window=200_000,
                cost_per_mtok_in=1.00,
                cost_per_mtok_out=5.00,
                eligible_roles=execution_roles,
            ),
        ]
    )


def _prior_failures(task_id: str, attempts: list[Attempt]) -> int:
    """
    How many of a task's recorded attempts are FAILED — the one piece of
    history both select_model and select_effort escalate on. Shared so the
    two policies can never silently drift apart on what "prior failures"
    means.
    """
    return sum(
        1
        for attempt in attempts
        if attempt.task_id == task_id and attempt.status == AttemptStatus.FAILED
    )


def select_model(task_id: str, node_type: NodeType, attempts: list[Attempt]) -> str:
    """
    The v1 model-selection heuristic: a deliberately simple, hand-written
    rule rather than anything learned. It only ever returns one of
    node_type.default_model or node_type.escalation_model — both of which
    are themselves validated against the ModelRegistry elsewhere before
    being trusted; this function never invents a model string.

    Policy:
      - 0 or 1 prior FAILED attempts for this task: retry on default_model.
        The first failure alone doesn't justify escalating — it gets one
        same-model retry with feedback first.
      - 2 or more prior FAILED attempts: escalate to escalation_model. If
        no escalation_model is configured, fall back to default_model
        rather than raising, since there's nothing stronger to escalate to
        and the task should still be able to keep retrying on what it has.
    """
    if node_type.default_model is None:
        raise NoDefaultModelConfiguredError(
            f"node type '{node_type.name}' has no default_model configured"
        )

    if _prior_failures(task_id, attempts) < ESCALATION_FAILURE_THRESHOLD:
        return node_type.default_model

    return node_type.escalation_model or node_type.default_model


def select_effort(task_id: str, node_type: NodeType, attempts: list[Attempt]) -> EffortLevel | None:
    """
    The effort-level counterpart to select_model, kept as its own function
    rather than folded into select_model's return value: the two policies
    share the exact same failure-count shape (see _prior_failures), but
    their "nothing configured" behavior differs in a way that's awkward to
    express as one return type. A node type MUST have a default_model (no
    default means NoDefaultModelConfiguredError), but effort is allowed to
    be entirely unconfigured — that's not an error, it just means "don't
    override effort for this node type at all," so this returns None
    rather than raising. Keeping them separate lets each function's error
    semantics stay simple instead of one combined function having to
    express "required" and "optional" at once.

    Policy, once default_effort IS configured (identical shape to
    select_model):
      - 0 or 1 prior FAILED attempts for this task: default_effort.
      - 2 or more prior FAILED attempts: escalation_effort, falling back to
        default_effort if no escalation_effort is configured.
    """
    if node_type.default_effort is None:
        return None

    if _prior_failures(task_id, attempts) < ESCALATION_FAILURE_THRESHOLD:
        return node_type.default_effort

    return node_type.escalation_effort or node_type.default_effort
