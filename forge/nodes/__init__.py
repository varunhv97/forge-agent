from __future__ import annotations

from typing import Optional

from forge.controller.execution_registry import ExecutionHandlerRegistry, ExecutionNodeHandlers
from forge.controller.mechanical import MechanicalRunnerRegistry
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.models.permissions import PermissionProfile
from forge.nodes import implement_exec as implement_exec_module
from forge.nodes import integration_test_write_exec as integration_test_write_exec_module
from forge.nodes.implement_exec import IMPLEMENT_EXEC
from forge.nodes.integration_test_write_exec import INTEGRATION_TEST_WRITE_EXEC
from forge.nodes.integration_verify_exec import (
    INTEGRATION_VERIFY_EXEC,
    INTEGRATION_VERIFY_EXEC_RUNNER,
)
from forge.nodes.manual_test_exec import MANUAL_TEST_EXEC
from forge.nodes.unit_verify_exec import UNIT_VERIFY_EXEC, UNIT_VERIFY_EXEC_RUNNER

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

# unit_verify_exec/integration_verify_exec are mechanical — no LLM, no
# tool-call boundary to enforce via a hook — but every task still needs a
# resolvable permission_profile (compile_and_validate checks it regardless
# of node kind), so this exists even though mechanical dispatch doesn't
# consult most of its fields.
MECHANICAL_DEFAULT = PermissionProfile(name="mechanical_default", allowed_tools=[])


def register_all(
    node_registry: NodeTypeRegistry,
    permission_registry: PermissionProfileRegistry,
    mechanical_registry: Optional[MechanicalRunnerRegistry] = None,
    execution_handler_registry: Optional[ExecutionHandlerRegistry] = None,
) -> None:
    """
    Register every node type and permission profile defined in this package
    into the registries passed in. This project owns one process-wide
    registry of each kind, populated at startup by a caller — this function
    does not construct or hold its own.

    `mechanical_registry`/`execution_handler_registry` are optional: a
    caller that only needs graph validation (compile_and_validate) can pass
    just the first two registries. Dispatching anything for real needs all
    four, since that's where the actual runner callables and
    input/prompt-builder functions get wired in.
    """
    permission_registry.register(CODE_EDIT_DEFAULT)
    permission_registry.register(MANUAL_TEST_DEFAULT)
    permission_registry.register(MECHANICAL_DEFAULT)

    node_registry.register(IMPLEMENT_EXEC)
    node_registry.register(INTEGRATION_TEST_WRITE_EXEC)
    node_registry.register(MANUAL_TEST_EXEC)
    node_registry.register(UNIT_VERIFY_EXEC)
    node_registry.register(INTEGRATION_VERIFY_EXEC)

    if mechanical_registry is not None:
        mechanical_registry.register("unit_verify_exec", UNIT_VERIFY_EXEC_RUNNER)
        mechanical_registry.register("integration_verify_exec", INTEGRATION_VERIFY_EXEC_RUNNER)

    if execution_handler_registry is not None:
        execution_handler_registry.register(
            "implement_exec",
            ExecutionNodeHandlers(
                build_input=implement_exec_module.build_input,
                build_prompt=implement_exec_module.build_prompt,
            ),
        )
        execution_handler_registry.register(
            "integration_test_write_exec",
            ExecutionNodeHandlers(
                build_input=integration_test_write_exec_module.build_input,
                build_prompt=integration_test_write_exec_module.build_prompt,
            ),
        )
        # manual_test_exec is intentionally not registered here yet: its
        # build_input needs a deployed/preview URL supplied by whatever
        # deploy step precedes it, which doesn't exist until that mode's
        # graph (Feature Blast) is actually built. Its NodeType is real and
        # registered above; only its live dispatch wiring is deferred.
