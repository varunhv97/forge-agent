from __future__ import annotations

from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.models.permissions import PermissionProfile
from forge.nodes.implement_exec import IMPLEMENT_EXEC
from forge.nodes.integration_test_write_exec import INTEGRATION_TEST_WRITE_EXEC
from forge.nodes.manual_test_exec import MANUAL_TEST_EXEC

# Shared by implement_exec and integration_test_write_exec: both edit code
# and run the project's own test suite, nothing more. `denied_bash_patterns`
# and `network_allowed` are left at PermissionProfile's own defaults, which
# already cover "git push*" and "rm -rf *" and already default network
# access off.
CODE_EDIT_DEFAULT = PermissionProfile(
    name="code_edit_default",
    allowed_tools=["Read", "Edit", "Write", "Grep", "Bash(pytest*)", "Bash(python*)"],
)

# manual_test_exec is the first node type in this project with network
# access. It is scoped to only what driving a Playwright script needs —
# reading files and invoking Playwright through npx — not a blanket Bash
# allowance. `denied_bash_patterns` is left at PermissionProfile's own
# default, which still blocks `curl *` even though `network_allowed=True`
# permits the Playwright browser itself to reach the target URL.
MANUAL_TEST_DEFAULT = PermissionProfile(
    name="manual_test_default",
    allowed_tools=["Read", "Bash(playwright*)", "Bash(npx playwright*)", "Bash(npx*)"],
    network_allowed=True,
)


def register_all(
    node_registry: NodeTypeRegistry, permission_registry: PermissionProfileRegistry
) -> None:
    """
    Register every node type and permission profile defined in this package
    into the registries passed in. This project owns one process-wide
    registry of each kind, populated at startup by a caller — this function
    does not construct or hold its own.
    """
    permission_registry.register(CODE_EDIT_DEFAULT)
    permission_registry.register(MANUAL_TEST_DEFAULT)

    node_registry.register(IMPLEMENT_EXEC)
    node_registry.register(INTEGRATION_TEST_WRITE_EXEC)
    node_registry.register(MANUAL_TEST_EXEC)
