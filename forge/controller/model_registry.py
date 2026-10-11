from __future__ import annotations

from forge.controller.node_registry import NodeType
from forge.models.attempt import Attempt, AttemptStatus
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

    prior_failures = sum(
        1
        for attempt in attempts
        if attempt.task_id == task_id and attempt.status == AttemptStatus.FAILED
    )

    if prior_failures < ESCALATION_FAILURE_THRESHOLD:
        return node_type.default_model

    return node_type.escalation_model or node_type.default_model
