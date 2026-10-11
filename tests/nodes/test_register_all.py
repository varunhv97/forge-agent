from __future__ import annotations

import pytest

from forge.controller.execution_registry import ExecutionHandlerRegistry
from forge.controller.mechanical import MechanicalRunnerRegistry
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.nodes import register_all
from forge.nodes.deploy_exec import DeployExecOutput


def test_register_all_populates_node_registry():
    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()

    register_all(node_registry, permission_registry)

    assert "implement_exec" in node_registry
    assert "integration_test_write_exec" in node_registry
    assert "manual_test_exec" in node_registry
    assert "deploy_exec" in node_registry


def test_register_all_populates_permission_registry():
    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()

    register_all(node_registry, permission_registry)

    code_edit = permission_registry.get("code_edit_default")
    manual_test = permission_registry.get("manual_test_default")
    deploy = permission_registry.get("deploy_default")

    assert code_edit.network_allowed is False
    assert manual_test.network_allowed is True
    assert "Bash(npx*)" in manual_test.allowed_tools
    assert "Bash" not in manual_test.allowed_tools
    assert deploy.network_allowed is True


def test_registered_node_types_reference_registered_permission_profiles():
    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()

    register_all(node_registry, permission_registry)

    for name in ("implement_exec", "integration_test_write_exec", "manual_test_exec"):
        node_type = node_registry.get(name)
        # Resolves without raising — confirms every NodeType's declared
        # permission_profile was actually registered, not just spelled
        # consistently.
        permission_registry.get(node_type.permission_profile)


def test_register_all_with_mechanical_and_execution_registries():
    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()
    mechanical_registry = MechanicalRunnerRegistry()
    execution_handler_registry = ExecutionHandlerRegistry()

    register_all(
        node_registry, permission_registry, mechanical_registry, execution_handler_registry
    )

    assert "unit_verify_exec" in node_registry
    assert "integration_verify_exec" in node_registry
    mechanical_registry.get("unit_verify_exec")
    mechanical_registry.get("integration_verify_exec")
    mechanical_registry.get("deploy_exec")
    execution_handler_registry.get("implement_exec")
    execution_handler_registry.get("integration_test_write_exec")
    execution_handler_registry.get("manual_test_exec")


def test_manual_test_exec_build_input_resolves_target_base_url_from_upstream_deploy(
    request_factory, task_factory
):
    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()
    execution_handler_registry = ExecutionHandlerRegistry()
    register_all(
        node_registry,
        permission_registry,
        execution_handler_registry=execution_handler_registry,
    )

    handlers = execution_handler_registry.get("manual_test_exec")
    request = request_factory()
    task = task_factory("t2", node_type="manual_test_exec", depends_on=["t1"])
    upstream_outputs = {
        "t1": DeployExecOutput(
            deployment_url="forge-abc.vercel.app",
            deployment_id="dep123",
            summary="deployed to https://forge-abc.vercel.app",
        )
    }

    result = handlers.build_input(task, request, upstream_outputs)

    assert result.target_base_url == "https://forge-abc.vercel.app"


def test_manual_test_exec_build_input_raises_without_exactly_one_upstream_deploy(
    request_factory, task_factory
):
    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()
    execution_handler_registry = ExecutionHandlerRegistry()
    register_all(
        node_registry,
        permission_registry,
        execution_handler_registry=execution_handler_registry,
    )

    handlers = execution_handler_registry.get("manual_test_exec")
    request = request_factory()
    task = task_factory("t2", node_type="manual_test_exec")

    with pytest.raises(ValueError):
        handlers.build_input(task, request, {})


def test_register_all_without_mechanical_or_execution_registries_still_works():
    """compile_and_validate-only callers shouldn't need to construct
    registries they don't use."""
    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()

    register_all(node_registry, permission_registry)

    assert "unit_verify_exec" in node_registry
    assert "integration_verify_exec" in node_registry
