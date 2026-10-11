# Architecture

Full detail: the plan doc (linked from `CLAUDE.md`). This is the quick map.

## Three artifacts, one authority

- **Logical Task DAG** (`GraphVersion`) — the versioned plan. Append-only
  versions, immutable once active.
- **Execution Attempt DAG** (`Attempt` rows) — immutable history of what
  actually ran. Retries/escalations are new forward-pointing rows, never
  back-edges.
- **Controller state machine** — the only thing that reads both and writes
  transitions. Lives in SQLite (`forge/controller/db.py`, WAL mode, explicit
  transactions).

Nodes produce typed proposals (`DecisionProposal`, `NodeResult`). Only
`forge/controller/*` applies them.

## Modules

```
forge/
  models/        typed contracts (Pydantic): common, request, graph,
                  attempt, evidence, decision, permissions, model_registry,
                  run, trace
  controller/
    validator.py        compile_and_validate() — acyclicity + domain
                          invariants, same path for templates and revisions
    node_registry.py     global NodeTypeRegistry, shared across modes
    db.py                SQLite connection + schema + explicit transactions
    store.py             typed CRUD over the schema above
    mechanical.py        MechanicalRunnerRegistry — deterministic checks,
                          no LLM, no adapter
    model_registry.py    ModelRegistry (escalation eligibility) + select_model
    decisions.py          apply_decision — the only place a DecisionProposal
                          becomes a real state change (see below)
    decision_dispatch.py  invoke_decision_node + deterministic fallbacks +
                          the fixed decision_node_type->model_id table
    scheduler.py          controller_tick / run_to_completion / reconcile;
                          failed tasks get driven through a decision each
                          tick instead of staying failed forever (mechanical
                          nodes only — EXECUTION-node dispatch is M6/M7)
  nodes/          one module per node type, each owning its own prompt,
                  context-building, schemas, permission profile  (not yet)
  adapters/
    execution/    sdk_execution_adapter.py — wires forge/isolation/hooks.py
                  into real claude_agent_sdk types (ClaudeAgentOptions,
                  HookMatcher); codex_cli_adapter.py (container-enforced,
                  not yet — M5)
    decision/     anthropic_api_decision_adapter.py — direct Anthropic
                  Messages API calls with forced tool-use structured output
    deploy/       deploy_adapter_base.py, vercel_adapter.py       (not yet — M7)
  isolation/
    worktree_manager.py  create_worktree/remove_worktree over real git
                          worktrees, idempotent cleanup
    hooks.py              the PreToolUse policy decision engine itself
                          (ToolCallEvent -> HookDecision) — zero dependency
                          on any specific agent-runtime SDK by design
    docker_sandbox.py     container isolation for non-hook-capable
                          adapters (not yet — M5)
```

## Resumability

Mechanical dispatch is synchronous: an attempt is written as `RUNNING`
*before* it executes, then updated to `PASSED`/`FAILED` with its evidence
in one transaction. Since this is a single-process execution model, any
attempt still `RUNNING` when a run resumes was abandoned by a prior process
— `reconcile_orphaned_attempts` cancels it so the scheduler dispatches a
fresh attempt rather than waiting on one that will never report back.

## Decisions, concretely

A decision node's output (`DecisionProposal`) is bounded and typed — never
free text, never trusted directly. `apply_decision` is the only place one
becomes a state change, validated against the controller's own authority
first: `STOP_SUCCESS` re-derives `all_required_gates_met()` itself; a retry
must cite evidence, stay within `max_retries_per_task`, and not just repeat
the prior failure verbatim; an escalation's model is checked against
`ModelRegistry` before dispatch. Any decision-adapter failure falls back
to a hand-written deterministic policy rather than hanging the run.

## Enforcement, concretely

- Claude Agent SDK attempts: a PreToolUse hook, generated per attempt from
  the task's `PermissionProfile` and worktree path, is the actual boundary
  — not the node's prompt. Built and real: `forge/isolation/hooks.py`
  (the policy decision) wired into the real SDK's types by
  `forge/adapters/execution/sdk_execution_adapter.py`. PreToolUse is the one
  point in the SDK's permission pipeline that runs unconditionally, before
  `permission_mode`/`allowed_tools` are even consulted — confirmed from the
  SDK's own source and docstrings, not assumed.
- Codex CLI attempts: no hook equivalent exists, so these always run inside
  a Docker container mounting only that attempt's worktree. The dispatcher
  refuses to run a non-hook-capable adapter outside a container. Not built
  yet (M5).

**Known gap:** the live scenario (a model actually calling a disallowed
tool, the real `claude` CLI invoking our hook) hasn't run — no API key in
this sandbox. Everything else is tested offline. See `tasks.md`'s M2 entry.

## Status

See `tasks.md` for the milestone-by-milestone build order (M0–M8) and what's
actually done vs. planned.
