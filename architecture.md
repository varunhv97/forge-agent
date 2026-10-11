# Architecture

Full detail: the plan doc (linked from `CLAUDE.md`). This is the quick map.

## Three artifacts, one authority

- **Logical Task DAG** (`GraphVersion`) — the versioned plan. Append-only
  versions, immutable once active.
- **Execution Attempt DAG** (`Attempt` rows) — immutable history of what
  actually ran. Retries/escalations are new forward-pointing rows, never
  back-edges.
- **Controller state machine** — the only thing that reads both and writes
  transitions. Lives in SQLite (not yet built — M1).

Nodes produce typed proposals (`DecisionProposal`, `NodeResult`). Only
`forge/controller/*` applies them.

## Modules

```
forge/
  models/        typed contracts (Pydantic): common, request, graph,
                  attempt, evidence, decision, permissions, model_registry
  controller/
    validator.py        compile_and_validate() — acyclicity + domain
                          invariants, same path for templates and revisions
    node_registry.py     global NodeTypeRegistry, shared across modes
    scheduler.py         controller_tick / apply_decision  (M1+)
  nodes/          one module per node type, each owning its own prompt,
                  context-building, schemas, permission profile
  adapters/
    execution/    sdk_execution_adapter.py (hook-enforced),
                  codex_cli_adapter.py (container-enforced)
    decision/     anthropic_api_decision_adapter.py
    deploy/       deploy_adapter_base.py, vercel_adapter.py
  isolation/      worktree_manager.py, hooks.py, docker_sandbox.py
```

## Enforcement, concretely

- Claude Agent SDK attempts: a PreToolUse hook, generated per attempt from
  the task's `PermissionProfile` and worktree path, is the actual boundary
  — not the node's prompt.
- Codex CLI attempts: no hook equivalent exists, so these always run inside
  a Docker container mounting only that attempt's worktree. The dispatcher
  refuses to run a non-hook-capable adapter outside a container.
- Either way: `STOP_SUCCESS` from a decision node is a recommendation only
  — the controller independently recomputes `all_required_gates_met()` from
  persisted `Evidence` rows before ever marking a run complete.

## Status

See `tasks.md` for the milestone-by-milestone build order (M0–M8) and what's
actually done vs. planned.
