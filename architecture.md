# Architecture

A quick map of the system. See `requirements.md` for scope and `tasks.md`
for progress.

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
    invalidation.py       evidence_is_stale — the commit/touched-paths
                          staleness rule, real-git-tested; not yet wired
                          into live dispatch (see Known gap below)
    execution_registry.py ExecutionHandlerRegistry — the EXECUTION-node
                          counterpart to MechanicalRunnerRegistry: maps a
                          node type to its build_input/build_prompt pair
    attempt_commit.py     current_head/commit_dirty_changes/changed_paths_
                          between — mechanically derives what an EXECUTION
                          attempt actually did, from git, never from what
                          the node claims
    scheduler.py          controller_tick / run_to_completion / reconcile;
                          dispatch_mechanical_attempt and
                          dispatch_execution_attempt (real worktree, real
                          prompt, real adapter call); failed tasks of
                          either kind get driven through a decision each
                          tick instead of staying failed forever
  nodes/          implement_exec, integration_test_write_exec,
                  manual_test_exec (EXECUTION, output_strategy=
                  "result_file" — writes .forge_result.json, no git diff to
                  judge pass/fail from); unit_verify_exec,
                  integration_verify_exec, deploy_exec (MECHANICAL,
                  worktree-aware; deploy_exec reaches the real network);
                  register_all() populates every registry at once,
                  including deploy_exec's runner (make_deploy_runner) and
                  manual_test_exec's build_input adapter
                  (_manual_test_build_input, resolves target_base_url from
                  the upstream deploy_exec output)
  modes/          feature_blast.py — build_feature_blast_request_and_graph,
                  the descoped M7 slice's Request+draft GraphVersion
                  builder; the caller still runs the draft through
                  compile_and_validate itself, same as any other graph
  adapters/
    execution/    sdk_execution_adapter.py — wires forge/isolation/hooks.py
                  into real claude_agent_sdk types (ClaudeAgentOptions,
                  HookMatcher); sdk_node_runner.py bridges it to the
                  scheduler's sync ExecutionRunner contract (asyncio.run,
                  same pattern as the decision adapter's bridge);
                  codex_cli_adapter.py (command construction + JSONL
                  parsing, decoupled from the sandbox via a duck-typed
                  executor); codex_execution_adapter.py (wires the Codex
                  adapter to docker_sandbox, owns the launch/exec/stop
                  lifecycle — stop always runs, even on failure)
    decision/     anthropic_api_decision_adapter.py — direct Anthropic
                  Messages API calls with forced tool-use structured output
    deploy/       vercel_adapter.py — VercelDeployAdapter: creates a
                  deployment via the Vercel REST API and polls it to a
                  terminal readyState (READY/ERROR), real httpx.Client,
                  fully offline-tested via httpx.MockTransport. Every
                  create-deployment call passes
                  skipAutoDetectionConfirmation=1, since deploy_exec always
                  deploys a fresh, disposable project name — every
                  deployment is that project's first, and Vercel requires
                  either that flag or an explicit projectSettings object
                  for a project with no prior framework-detection answer
                  on file. The Vercel token itself must be Full-Account
                  scoped, not Team/Project scoped — a narrower scope can't
                  create new projects and Vercel rejects it as
                  invalidToken, which VercelAuthenticationError surfaces.
                  deploy() also PATCHes the new project right after
                  creating it (ssoProtection: null) — new Vercel projects
                  default to Vercel Authentication on every deployment,
                  which would otherwise return a login-page redirect to
                  any unauthenticated request instead of the app's actual
                  response, defeating the point of deploying something
                  Forge itself (or a human without a Vercel login) can
                  reach
  isolation/
    worktree_manager.py  create_worktree/remove_worktree over real git
                          worktrees, idempotent cleanup
    hooks.py              the PreToolUse policy decision engine itself
                          (ToolCallEvent -> HookDecision) — zero dependency
                          on any specific agent-runtime SDK by design
    docker_sandbox.py     launch_container/exec_in_container/stop_container
                          — the enforcement substitute for adapters with no
                          hook mechanism (Codex CLI today)
```

## Resumability

Mechanical dispatch is synchronous: an attempt is written `RUNNING` before
it executes, then updated to `PASSED`/`FAILED` with its evidence in one
transaction. Single-process execution model, so any attempt still
`RUNNING` when a run resumes was abandoned by a prior process —
`reconcile_orphaned_attempts` cancels it so the scheduler dispatches fresh
rather than waiting on one that will never report back.

## Decisions and invalidation, concretely

`DecisionProposal` is bounded/typed, never trusted directly. `apply_decision`
is the only place one becomes a state change: `STOP_SUCCESS` re-derives
`all_required_gates_met()` itself; a retry must cite evidence, stay within
`max_retries_per_task`, and not just repeat the prior failure verbatim; an
escalation's model is checked against `ModelRegistry`. Any decision-adapter
failure falls back to a hand-written policy rather than hanging the run.

A `PROPOSE_GRAPH_REVISION` that invalidates evidence propagates: every
downstream task whose dependency chain consumed it gets *its* evidence
invalidated too (not just the one named) and its latest attempt superseded
— which doesn't block `_ready_tasks` the way PASSED does, so the scheduler
re-verifies instead of a completion check trusting stale work. The
*automatic* version (a later commit's diff touching the same paths an
older evidence row covered) is real-git-tested in `invalidation.py` but
not wired into live dispatch yet (see Known gaps).

## EXECUTION dispatch, concretely

`dispatch_execution_attempt` creates a real worktree, builds a prompt from
the node type's own `build_input`/`build_prompt`, and calls whatever
`ExecutionRunner` is configured — but never trusts that runner's own
report of what it did. Afterward it commits any dirty changes left in the
worktree as a safety net, reads the actual resulting commit, and diffs it
against the base commit for `changed_paths` — `result_commit` and
`changed_paths` are facts derived from git, not claims a node makes about
itself. A `MECHANICAL` task that depends on an EXECUTION one (e.g.
`unit_verify_exec` on `implement_exec`) runs inside that same worktree
rather than getting a fresh one — it's verifying that output, not
starting over.

## Enforcement, concretely

- Claude Agent SDK attempts: a PreToolUse hook, generated per attempt from
  the task's `PermissionProfile` and worktree path, is the actual boundary
  — not the node's prompt. Built and real: `forge/isolation/hooks.py`
  (the policy decision) wired into the real SDK's types by
  `forge/adapters/execution/sdk_execution_adapter.py`. PreToolUse is the one
  point in the SDK's permission pipeline that runs unconditionally, before
  `permission_mode`/`allowed_tools` are even consulted — confirmed from the
  SDK's own source and docstrings, not assumed.
- Codex CLI attempts: no hook equivalent exists, so these always run
  inside a Docker container mounting only that attempt's worktree —
  `run_codex_attempt` raises `ContainerRequiredError` immediately if no
  container is supplied; there is no configuration path around this.
  `run_codex_in_sandbox` owns the full lifecycle and guarantees
  `stop_container` runs even when the attempt itself fails.
- `Bash(<glob>)` allowlist entries in a `PermissionProfile` match against
  the *entire* command string (`hooks.py::_tool_covered_by_allowlist`,
  `fnmatch` with no implicit wildcard), not just a leading token — a
  narrowly-scoped pattern like `Bash(npx*)` denies any compound command
  (`cd ... && npx ...`, `mkdir -p ... && npx ...`) regardless of what
  follows the `&&`. A live M7 run surfaced this concretely: discovered
  when `manual_test_default`'s original narrow allowlist denied nearly
  every tool call `manual_test_exec` attempted. `denied_bash_patterns`
  (`git push*`/`*vercel deploy*`/`curl *`/`rm -rf *`, every profile's
  default) is the actual intended backstop for a profile that needs real
  shell flexibility — a narrow `allowed_tools` glob is not a substitute
  for it and can silently block legitimate work instead.

## M7 descoped slice, concretely

`forge/modes/feature_blast.py` builds one deterministic chain —
`implement_exec → unit_verify_exec → deploy_exec → manual_test_exec` — with
no LLM-driven `plan_decision` step yet; the shape itself is fixed by that
function, not proposed. `deploy_exec` is `MECHANICAL`: it reads every text
file out of its upstream worktree (inherited through the same
worktree-reuse chain `unit_verify_exec` already uses) and calls
`VercelDeployAdapter.deploy()` for real. `manual_test_exec` then needs a
live URL to test against that it can't derive itself —
`_manual_test_build_input` (in `forge/nodes/__init__.py`) bridges this by
pulling `deployment_url` off the upstream `deploy_exec` output before
calling `manual_test_exec.build_input`.

`manual_test_exec`'s pass/fail is a judgment call, not something a git diff
can express — this is why `NodeType` gained `output_strategy`
(`"commit_diff"` default vs. `"result_file"`). A `"result_file"` node
writes `.forge_result.json` into its own worktree before finishing;
`dispatch_execution_attempt` reads and validates that file against the
node's `output_schema` instead of deriving output from the commit diff,
still never trusting anything said in the session's own transcript.

`scripts/live_feature_blast_m7.py` is the live verification entry point
(same pattern as `live_smoke_test.py`): scaffolds a throwaway Vercel-Python
toy app, runs the full chain against a real `claude` CLI session and a
real Vercel deployment, then independently curls the live URL itself
rather than trusting the run's own `COMPLETED_VERIFIED`.

A first live run surfaced a real gap: `deploy_exec` retried a rejected
Vercel token 3 times, identically, before giving up — the generic
`retry_or_escalate_policy` has no way to know a failure is unrecoverable
rather than just unlucky. Fixed via `Attempt.requires_user_input`/
`MechanicalResult.requires_user_input`: a mechanical runner that can tell
its own failure can't be fixed by retrying (deploy_exec, via
`vercel_adapter.py`'s new `VercelCredentialError` — raised for a 401/403
auth rejection, distinct from an ordinary `VercelAPIError`) sets this flag,
and `scheduler._resolve_failed_tasks` routes a FAILED attempt with it
straight to a synthesized `REQUEST_USER_INPUT` decision
(`_unrecoverable_failure_proposal`) — no decision adapter call, no retry
budget spent, one attempt then `BLOCKED_AWAITING_INPUT`. This generalizes
beyond deploy_exec: any mechanical node can opt into the same fast path by
setting the flag on its own `MechanicalResult`.

**Known gaps:**

- `scripts/live_smoke_test.py` has run successfully with real credentials
  (`implement_exec` → `unit_verify_exec`, `completed_verified`), so
  EXECUTION dispatch and the PreToolUse hook path work live end to end. A
  hook actually *denying* a disallowed tool call has not been exercised
  live yet (the smoke test's task never attempted one).
- The commit-based automatic invalidation rule (`invalidation.py`) has real
  commits to work with as of M6 but is not wired into the scheduler yet.
- The Docker/Codex CLI path is tested only by mocking `subprocess.run`, and
  its JSONL event-schema assumption has not been checked against a real
  `codex exec --json` run.

## Status

See `tasks.md` for the milestone-by-milestone build order (M0–M8) and what's
actually done vs. planned.
