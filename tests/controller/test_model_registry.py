from __future__ import annotations

import pytest
from pydantic import BaseModel

from forge.controller.model_registry import (
    ModelNotEligibleError,
    ModelRegistry,
    NoDefaultModelConfiguredError,
    UnknownModelError,
    select_model,
)
from forge.controller.node_registry import NodeType
from forge.models.attempt import Attempt, AttemptStatus
from forge.models.common import NodeKind
from forge.models.model_registry import ModelCapability


class _DummySchema(BaseModel):
    pass


def _node_type(
    name: str = "coder",
    default_model: str | None = "claude-sonnet-5",
    escalation_model: str | None = "claude-opus-5",
) -> NodeType:
    return NodeType(
        name=name,
        kind=NodeKind.EXECUTION,
        input_schema=_DummySchema,
        output_schema=_DummySchema,
        permission_profile="default",
        default_model=default_model,
        escalation_model=escalation_model,
    )


def _capability(
    model_id: str = "claude-sonnet-5", eligible_roles: list[str] | None = None
) -> ModelCapability:
    return ModelCapability(
        provider="anthropic",
        model_id=model_id,
        adapter_kind="sdk_execution",
        supports_tools=True,
        context_window=200_000,
        cost_per_mtok_in=3.0,
        cost_per_mtok_out=15.0,
        eligible_roles=eligible_roles if eligible_roles is not None else ["coder"],
    )


def _attempt(task_id: str, status: AttemptStatus, attempt_number: int = 1) -> Attempt:
    return Attempt(
        id=f"{task_id}-attempt-{attempt_number}",
        run_id="run-1",
        task_id=task_id,
        graph_version_id="graph-1",
        attempt_number=attempt_number,
        model_used="claude-sonnet-5",
        permission_profile="default",
        worktree_path="/tmp/worktree",
        branch_name="branch",
        base_commit="abc123",
        status=status,
    )


class TestModelRegistryGet:
    def test_get_unknown_model_raises(self) -> None:
        registry = ModelRegistry([_capability("claude-sonnet-5")])
        with pytest.raises(UnknownModelError):
            registry.get("claude-made-up")

    def test_get_known_model_returns_capability(self) -> None:
        capability = _capability("claude-sonnet-5")
        registry = ModelRegistry([capability])
        assert registry.get("claude-sonnet-5") == capability


class TestValidateEligible:
    def test_unregistered_model_raises_unknown_model_error(self) -> None:
        registry = ModelRegistry([_capability("claude-sonnet-5", eligible_roles=["coder"])])
        with pytest.raises(UnknownModelError):
            registry.validate_eligible("claude-made-up", "coder")

    def test_registered_but_ineligible_role_raises_model_not_eligible_error(self) -> None:
        registry = ModelRegistry([_capability("claude-sonnet-5", eligible_roles=["reviewer"])])
        with pytest.raises(ModelNotEligibleError):
            registry.validate_eligible("claude-sonnet-5", "coder")

    def test_eligible_model_returns_capability(self) -> None:
        capability = _capability("claude-sonnet-5", eligible_roles=["coder", "reviewer"])
        registry = ModelRegistry([capability])
        assert registry.validate_eligible("claude-sonnet-5", "coder") == capability


class TestSelectModel:
    def test_zero_failures_returns_default_model(self) -> None:
        node_type = _node_type()
        assert select_model("task-1", node_type, attempts=[]) == "claude-sonnet-5"

    def test_exactly_one_failure_still_returns_default_model(self) -> None:
        node_type = _node_type()
        attempts = [_attempt("task-1", AttemptStatus.FAILED)]
        assert select_model("task-1", node_type, attempts) == "claude-sonnet-5"

    def test_two_failures_escalates(self) -> None:
        node_type = _node_type()
        attempts = [
            _attempt("task-1", AttemptStatus.FAILED, attempt_number=1),
            _attempt("task-1", AttemptStatus.FAILED, attempt_number=2),
        ]
        assert select_model("task-1", node_type, attempts) == "claude-opus-5"

    def test_three_failures_still_escalates(self) -> None:
        node_type = _node_type()
        attempts = [_attempt("task-1", AttemptStatus.FAILED, attempt_number=n) for n in range(1, 4)]
        assert select_model("task-1", node_type, attempts) == "claude-opus-5"

    def test_failures_for_other_tasks_are_not_counted(self) -> None:
        node_type = _node_type()
        attempts = [
            _attempt("other-task", AttemptStatus.FAILED, attempt_number=1),
            _attempt("other-task", AttemptStatus.FAILED, attempt_number=2),
        ]
        assert select_model("task-1", node_type, attempts) == "claude-sonnet-5"

    def test_non_failed_attempts_are_not_counted(self) -> None:
        node_type = _node_type()
        attempts = [
            _attempt("task-1", AttemptStatus.PASSED, attempt_number=1),
            _attempt("task-1", AttemptStatus.RUNNING, attempt_number=2),
        ]
        assert select_model("task-1", node_type, attempts) == "claude-sonnet-5"

    def test_two_failures_with_no_escalation_model_falls_back_to_default(self) -> None:
        node_type = _node_type(escalation_model=None)
        attempts = [
            _attempt("task-1", AttemptStatus.FAILED, attempt_number=1),
            _attempt("task-1", AttemptStatus.FAILED, attempt_number=2),
        ]
        assert select_model("task-1", node_type, attempts) == "claude-sonnet-5"

    def test_no_default_model_configured_raises(self) -> None:
        node_type = _node_type(default_model=None, escalation_model=None)
        with pytest.raises(NoDefaultModelConfiguredError):
            select_model("task-1", node_type, attempts=[])
