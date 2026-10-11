from __future__ import annotations

from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.nodes import register_all


def test_register_all_populates_node_registry():
    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()

    register_all(node_registry, permission_registry)

    assert "implement_exec" in node_registry
    assert "integration_test_write_exec" in node_registry
    assert "manual_test_exec" in node_registry


def test_register_all_populates_permission_registry():
    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()

    register_all(node_registry, permission_registry)

    code_edit = permission_registry.get("code_edit_default")
    manual_test = permission_registry.get("manual_test_default")

    assert code_edit.network_allowed is False
    assert manual_test.network_allowed is True
    assert "Bash(npx*)" in manual_test.allowed_tools
    assert "Bash" not in manual_test.allowed_tools


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
