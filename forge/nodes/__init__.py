from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from forge.adapters.deploy.vercel_adapter import VercelDeployAdapter
from forge.controller.execution_registry import ExecutionHandlerRegistry, ExecutionNodeHandlers
from forge.controller.mechanical import MechanicalRunnerRegistry
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.models.graph import TaskSpec
from forge.models.permissions import PermissionProfile
from forge.models.request import Request
from forge.nodes import implement_exec as implement_exec_module
from forge.nodes import integration_test_write_exec as integration_test_write_exec_module
from forge.nodes import manual_test_exec as manual_test_exec_module
from forge.nodes.deploy_exec import (
    DEPLOY_EXEC,
    DeployAdapter,
    DeployExecOutput,
    make_deploy_runner,
)
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
# access. A live M7 run showed the original narrow allowlist
# (["Read", "Bash(playwright*)", "Bash(npx playwright*)", "Bash(npx*)"], no
# "Write" at all) was unworkable in practice: the node's own prompt
# requires writing `.forge_result.json` — a tool call the old list could
# never permit — and `Bash(<glob>)` is matched against the *entire* command
# string (see hooks.py's _tool_covered_by_allowlist), so any compound
# command (`cd ... && pip install playwright`, `mkdir -p screenshots`, a
# plain `python3 script.py`) was denied outright regardless of prefix,
# leaving no real way to install or drive a test runner at all. "Bash"
# unscoped is the fix; `denied_bash_patterns`'s own default
# ("git push*"/"*vercel deploy*"/"curl *"/"rm -rf *") is still the backstop
# — this node still can't push, deploy, curl, or rm -rf, and still can't
# reach the network at all if network_allowed were False.
MANUAL_TEST_DEFAULT = PermissionProfile(
    name="manual_test_default",
    allowed_tools=["Read", "Write", "Bash"],
    network_allowed=True,
)

# unit_verify_exec/integration_verify_exec are mechanical — no LLM, no
# tool-call boundary to enforce via a hook — but every task still needs a
# resolvable permission_profile (compile_and_validate checks it regardless
# of node kind), so this exists even though mechanical dispatch doesn't
# consult most of its fields.
MECHANICAL_DEFAULT = PermissionProfile(name="mechanical_default", allowed_tools=[])

# deploy_exec is mechanical like unit_verify_exec/integration_verify_exec —
# no LLM, no hook-enforced tool boundary — but it is the first mechanical
# node that actually reaches the network (the Vercel API), so it needs its
# own profile with network_allowed=True rather than reusing
# MECHANICAL_DEFAULT.
DEPLOY_DEFAULT = PermissionProfile(name="deploy_default", allowed_tools=[], network_allowed=True)


def _manual_test_build_input(
    task: TaskSpec, request: Request, upstream_outputs: Mapping[str, BaseModel]
) -> manual_test_exec_module.ManualTestInput:
    """
    Bridges the dispatcher's generic `(task, request, upstream_outputs)`
    shape to `manual_test_exec.build_input`'s `target_base_url` parameter.

    `target_base_url` is not something manual_test_exec can derive for
    itself — it comes from whichever upstream task actually deployed the
    thing being tested. This looks for exactly one upstream `deploy_exec`
    output and takes its `deployment_url`; anything else (no deploy
    upstream, or an output that doesn't parse as `DeployExecOutput`) is a
    graph-construction bug, not a runtime condition to recover from, so it
    fails loudly rather than silently testing against an empty URL.
    """
    deploy_outputs = [
        output for output in upstream_outputs.values() if isinstance(output, DeployExecOutput)
    ]
    if len(deploy_outputs) != 1:
        raise ValueError(
            f"manual_test_exec task '{task.id}' needs exactly one upstream "
            f"deploy_exec output, found {len(deploy_outputs)}"
        )
    target_base_url = f"https://{deploy_outputs[0].deployment_url}"
    return manual_test_exec_module.build_input(task, request, target_base_url)


def register_all(
    node_registry: NodeTypeRegistry,
    permission_registry: PermissionProfileRegistry,
    mechanical_registry: MechanicalRunnerRegistry | None = None,
    execution_handler_registry: ExecutionHandlerRegistry | None = None,
    deploy_adapter: DeployAdapter | None = None,
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

    `deploy_adapter` lets a caller inject a configured `VercelDeployAdapter`
    (e.g. one built from a real token for a live run); when omitted and
    `mechanical_registry` is given, a default adapter is constructed here.
    Constructing one is side-effect-free — it only resolves its token and
    touches the network inside `.deploy()`, never in `__init__` — so this
    default is safe even for callers (like the test suite) that never
    actually dispatch a deploy_exec attempt.
    """
    permission_registry.register(CODE_EDIT_DEFAULT)
    permission_registry.register(MANUAL_TEST_DEFAULT)
    permission_registry.register(MECHANICAL_DEFAULT)
    permission_registry.register(DEPLOY_DEFAULT)

    node_registry.register(IMPLEMENT_EXEC)
    node_registry.register(INTEGRATION_TEST_WRITE_EXEC)
    node_registry.register(MANUAL_TEST_EXEC)
    node_registry.register(UNIT_VERIFY_EXEC)
    node_registry.register(INTEGRATION_VERIFY_EXEC)
    node_registry.register(DEPLOY_EXEC)

    if mechanical_registry is not None:
        mechanical_registry.register("unit_verify_exec", UNIT_VERIFY_EXEC_RUNNER)
        mechanical_registry.register("integration_verify_exec", INTEGRATION_VERIFY_EXEC_RUNNER)
        mechanical_registry.register(
            "deploy_exec", make_deploy_runner(deploy_adapter or VercelDeployAdapter())
        )

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
        execution_handler_registry.register(
            "manual_test_exec",
            ExecutionNodeHandlers(
                build_input=_manual_test_build_input,
                build_prompt=manual_test_exec_module.build_prompt,
            ),
        )
