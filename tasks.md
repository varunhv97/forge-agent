# Tasks

Milestones from the plan doc's roadmap. Build order matters — don't skip
ahead. Full detail for completed milestones lives in git history/commit
messages, not here — this stays a living index, not an archive.

- [x] **M0 — Repo setup + core models + validator.** Pydantic models,
      `NodeTypeRegistry`/`PermissionProfileRegistry`, `compile_and_validate`
      (acyclicity, type/dependency/gate/invalidation/permission checks).
      24 tests. Pushed to `https://github.com/varunhv97/forge-agent`.
- [x] **M1 — Controller skeleton + SQLite + mechanical nodes, resumability.**
      `Run`/`TraceEvent` models, WAL-mode SQLite `Store`,
      `MechanicalRunnerRegistry`, the scheduler (readiness, dispatch,
      `all_required_gates_met`, orphaned-attempt reconciliation on
      restart). 15 tests, including a simulated crash-and-resume.
- [x] **M2 — Worktree isolation + Claude Agent SDK adapter + PreToolUse hooks.**
      Python migrated 3.9.6 → 3.11.16 (`claude-agent-sdk` needs >=3.10; see
      `CLAUDE.md`'s Environment section for the standalone-build workaround).
      `forge/isolation/worktree_manager.py` and `hooks.py` (the policy
      engine, adapter-agnostic), wired into real `claude_agent_sdk` types by
      `forge/adapters/execution/sdk_execution_adapter.py`. 26 tests.
      **Known gap:** the live "model tries `git push`, hook blocks it"
      scenario needs network + `ANTHROPIC_API_KEY`/an authenticated `claude`
      CLI, unavailable in this sandbox — everything else is tested offline,
      including invoking the exact callback the SDK would be handed.
- [x] **M3 — Decision adapter + bounded `DecisionAction` + `apply_decision`.**
      `forge/adapters/decision/anthropic_api_decision_adapter.py` (forced
      tool-use structured output, fully offline-tested via
      `httpx.MockTransport` — no live-call gap here, unlike M2).
      `forge/controller/model_registry.py` (`select_model` heuristic +
      eligibility validation). `forge/controller/decisions.py`
      (`apply_decision` — the only place a proposal becomes a state change
      — plus `assert_new_information`, `materialize_revision`,
      `check_revision_budget`). `forge/controller/decision_dispatch.py`
      (`invoke_decision_node` + deterministic fallback policies + the fixed
      `decision_node_type -> model_id` table). Scheduler now drives failed
      tasks through a decision each tick instead of leaving them failed
      forever. 34 tests. Found and fixed a real bug:
      `_has_unrecoverable_failure` checked "ever failed" instead of
      "latest attempt failed," so a task that recovered via retry was still
      reported as unrecoverable.
      `PROPOSE_GRAPH_REVISION` handling here is a head start on M4, not M4
      itself — `propagate_invalidation` and the commit/touched-paths-based
      automatic evidence-invalidation rule aren't wired into real scheduler
      behavior yet.
- [ ] **M4** — graph revision + invalidation propagation
- [ ] **M5** — Codex CLI adapter + Docker-sandbox isolation
- [ ] **M6** — three-tier testing nodes (`integration_test_write_exec`, `manual_test_exec`)
- [ ] **M7** — Feature Blast end-to-end with a real Vercel deployment (flagship milestone)
- [ ] **M8** — CLI polish + `forge trace`/`forge inspect`

Everything past M8 (other modes, a third adapter, dashboard) is backlog —
see `requirements.md` — not scheduled.

## Notes

- This machine's home directory is locked down for writes/reads from this
  tool's shell (even with the sandbox override) — `gh` lives at `.gh-bin`
  with `GH_CONFIG_DIR=.gh-config` (both gitignored). Pushes must be run by
  the user directly; this tool's shell cannot reach git credentials.
