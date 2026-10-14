# Roadmap

Milestones are built in order, since each one depends on the previous. A
milestone counts as done only when its acceptance criterion has been checked
independently (passing tests, or a live run for the live-verified ones).
Commit messages hold the full history; this file is a short index.

- [x] **M0: Core models and graph validator.** Pydantic models,
      `NodeTypeRegistry`/`PermissionProfileRegistry`, `compile_and_validate`
      (acyclicity plus type, dependency, gate, invalidation and permission
      checks).
- [x] **M1: Controller, SQLite store, mechanical nodes, resumability.**
      `Run`/`TraceEvent` models, a WAL-mode SQLite `Store`,
      `MechanicalRunnerRegistry`, the scheduler (readiness, dispatch,
      `all_required_gates_met`) and orphaned-attempt reconciliation on
      restart. Includes a simulated crash-and-resume test.
- [x] **M2: Worktree isolation, Claude Agent SDK adapter, PreToolUse hooks.**
      `forge/isolation/worktree_manager.py`, the adapter-agnostic policy
      engine in `hooks.py`, and its wiring into real `claude_agent_sdk` types
      in `sdk_execution_adapter.py`. Requires Python 3.11.
- [x] **M3: Decision adapter, bounded `DecisionAction`, `apply_decision`.**
      An Anthropic API decision adapter with forced tool-use structured output,
      tested offline via `httpx.MockTransport`. Also adds `select_model` plus
      eligibility checks, `apply_decision` (the only place a proposal becomes
      a state change), deterministic fallback policies, and scheduler support
      for driving failed tasks through a decision every tick. Bug fixed:
      `_has_unrecoverable_failure` checked whether a task had *ever* failed
      instead of whether its *latest* attempt failed.
- [x] **M4: Graph revision and invalidation propagation.**
      `PROPOSE_GRAPH_REVISION` now runs `propagate_invalidation`. It
      invalidates the evidence of every downstream task that consumed the
      named evidence and supersedes those tasks' latest attempts, so they get
      re-verified. The commit and touched-paths staleness rule
      (`invalidation.py`) is built and tested against real git. Bug fixed:
      `_ready_tasks` had the same "ever vs. latest" mistake.
- [x] **M5: Codex CLI adapter and Docker sandbox.**
      `docker_sandbox.py` (launch, exec and idempotent stop),
      `codex_cli_adapter.py` (command construction and JSONL parsing) and
      `codex_execution_adapter.py`, which joins the two and always stops the
      container, even when the attempt fails. A Codex attempt refuses to run
      without a container.
- [x] **M6: Three-tier testing nodes, with EXECUTION dispatch wired live.**
      Adds `implement_exec`, `integration_test_write_exec`, `manual_test_exec`,
      `unit_verify_exec` and `integration_verify_exec`.
      `dispatch_execution_attempt` creates a real worktree, calls the runtime
      adapter, and derives `result_commit`/`changed_paths` from git.
      **Live-verified:** `scripts/live_smoke_test.py` reached
      `completed_verified` against a real `claude` session, with
      `unit_verify_exec` running in the worktree that `implement_exec`
      produced.
- [ ] **M7: Feature Blast end to end with a real Vercel deployment (in progress).**
      The first slice is a fixed chain, with no LLM-driven planning step yet:
      `implement_exec → unit_verify_exec → deploy_exec → manual_test_exec`.
      This milestone added `VercelDeployAdapter`, `deploy_exec`,
      `NodeType.output_strategy` (`manual_test_exec` reports pass/fail in
      `.forge_result.json` because no git diff can express it),
      `build_feature_blast_request_and_graph` and
      `scripts/live_feature_blast_m7.py`. Issues found in live runs and fixed:
      - A rejected Vercel token was retried 3 times. Runners can now flag a
        failure as unrecoverable (`requires_user_input`), which sends it
        straight to `REQUEST_USER_INPUT`.
      - Vercel returned `missing_project_settings` on a new project's first
        deploy. Every deploy now passes `skipAutoDetectionConfirmation=1`.
      - New projects default to Vercel Authentication, so the deployment
        served a login page. SSO protection is now disabled on the project
        after it is created.
      - `manual_test_exec`'s permission profile had no `Write` tool, and its
        narrow `Bash(<glob>)` entries denied compound commands. It now gets
        `Read`/`Write`/`Bash`, with `denied_bash_patterns` as the backstop.

      In the latest live run, `implement_exec`, `unit_verify_exec` and
      `deploy_exec` passed, and an independent probe of the deployed URL
      returned the expected response. **Remaining:** confirm a fully green
      run that includes `manual_test_exec`.
- [ ] **M8: CLI polish, `forge trace` / `forge inspect`.**

Everything after M8 (other modes, a third runtime adapter, a dashboard) is
backlog. See `requirements.md`.

## Known gaps

- A PreToolUse hook denying a tool call is covered by offline tests but
  hasn't been exercised in a live session yet.
- Automatic commit-based invalidation (`invalidation.py`) is not wired into
  the scheduler yet.
- The Docker/Codex CLI path is tested only by mocking `subprocess.run`. The
  JSONL event schema that `parse_codex_jsonl` relies on still needs checking
  against a real `codex exec --json` run. Live dispatch uses the Claude
  Agent SDK only for now.
- `integration_test_write_exec` and `integration_verify_exec` are registered
  but haven't been exercised end to end yet.
