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
- [x] **M4 — Graph revision + invalidation propagation.**
      `forge/controller/invalidation.py` — `evidence_is_stale`/
      `compute_stale_evidence_ids`, the commit/touched-paths-based
      automatic staleness rule, tested against real git repos (built via
      parallel subagent). **Known gap, same pattern as M2:** not wired into
      the live scheduler — mechanical nodes don't produce real commits yet
      (that needs EXECUTION-node dispatch, M6/M7), so this stays a
      correct, tested, standalone unit until there's real commit data to
      feed it.
      What *is* wired live (mine): `apply_decision`'s `PROPOSE_GRAPH_REVISION`
      path now calls `propagate_invalidation` for real — explicitly
      invalidated evidence propagates to every downstream task that
      depended on it, invalidating *their* evidence too (not just the
      named evidence) and superseding their latest attempt, so the
      scheduler re-dispatches them instead of silently trusting stale
      work. Added `Store.update_evidence`. Fixed a related bug:
      `_ready_tasks` had the same "ever vs. latest" mistake M3 fixed in
      `_has_unrecoverable_failure` — a task whose only attempt was
      superseded could still get stuck behind an old FAILED attempt
      earlier in its history. 12 new tests, including a full
      run-completes → evidence-invalidated → re-dispatched →
      re-completes-on-fresh-evidence scheduler test.
- [x] **M5 — Codex CLI adapter + Docker-sandbox isolation.**
      `forge/isolation/docker_sandbox.py` (`launch_container`/
      `exec_in_container`/`stop_container`, idempotent cleanup — built via
      parallel subagent) and `forge/adapters/execution/codex_cli_adapter.py`
      (`build_codex_command`/`parse_codex_jsonl`/`run_codex_attempt`,
      decoupled from the sandbox via a duck-typed executor — built via a
      second parallel subagent). `forge/adapters/execution/
      codex_execution_adapter.py` (mine) wires the two together
      (`run_codex_in_sandbox`), including the small interface mismatch
      between them (keyword-only vs. positional `cwd`), and guarantees
      container cleanup in a `finally` even when the attempt itself fails.
      Codex attempts refuse to run at all without a container — structural,
      not configurable, mirroring the Claude Agent SDK's hook requirement.
      25 new tests. **Known gap, same pattern as M2/M4:** neither Docker nor
      the Codex CLI is installed in this sandbox, so everything is tested
      by mocking `subprocess.run` to simulate exactly what each would do —
      real, correct code and command construction, not yet verified
      against the real binaries. The Codex JSONL event schema used by
      `parse_codex_jsonl`'s `final_message` extraction is a documented
      best-effort guess for the same reason and needs checking against a
      real `codex exec --json` run.
- [x] **M6 — Three-tier testing nodes + EXECUTION dispatch wired live.**
      `forge/nodes/` populated for real — `implement_exec`,
      `integration_test_write_exec`, `manual_test_exec` (parallel subagent),
      `unit_verify_exec`/`integration_verify_exec` (mine, worktree-aware
      MECHANICAL nodes). The bigger piece (mine): EXECUTION-kind tasks are
      now actually dispatchable — `dispatch_execution_attempt` creates a
      real worktree, builds a prompt from the node's own context-building
      logic, calls an injected `ExecutionRunner`, then derives
      `result_commit`/`changed_paths` *mechanically from git* (never
      trusting the model's own report) via `forge/controller/
      attempt_commit.py`. `forge/adapters/execution/sdk_node_runner.py`
      bridges the real SDK adapter in. 39 new tests, including a genuine
      end-to-end test (real git repo, real worktree, real `pytest -q`,
      only the LLM call faked) proving `implement_exec → unit_verify_exec`
      completes a run.
      **Live-verified for real, not just "known gap" this time:**
      `scripts/live_smoke_test.py`, run by the user from their own terminal
      with an authenticated `claude` CLI (can't run from this sandbox —
      same restriction as `git push`). Result: `completed_verified`, a
      real `result_commit`, and `unit_verify_exec` running in the exact
      same worktree `implement_exec` produced — confirming the worktree-
      reuse design works against the real SDK, not just in offline tests.
      The first genuinely end-to-end live run in this project.
      Deferred to M7 (needs merge/deploy orchestration that doesn't exist
      yet): `manual_test_exec` isn't wired into `ExecutionHandlerRegistry`
      (needs a deploy URL source); `integration_test_write_exec`/
      `integration_verify_exec` are real and registered but not yet
      exercised end-to-end. Codex adapter choice deferred too (SDK-only
      for now, per discussion).
- [ ] **M7 — Feature Blast end-to-end with a real Vercel deployment (in progress).**
      Descoped first slice, user-approved: a single deterministic chain
      (`implement_exec → unit_verify_exec → deploy_exec → manual_test_exec`),
      no LLM-driven `plan_decision` yet. Built so far:
      `forge/adapters/deploy/vercel_adapter.py` (`VercelDeployAdapter` —
      create + poll the Vercel REST API, built via parallel subagent, fully
      offline-tested via `httpx.MockTransport`). `forge/nodes/deploy_exec.py`
      (mine — MECHANICAL, collects every text file out of the upstream
      worktree and deploys it). `NodeType.output_strategy` (mine — new
      field, `"commit_diff"` default vs. `"result_file"`) plus
      `scheduler.py`'s `_read_result_file`, needed because
      `manual_test_exec`'s pass/fail is a judgment call with no git diff to
      derive it from — it now writes `.forge_result.json` in its worktree
      and the dispatcher reads/validates that instead of trusting the
      session's transcript. `manual_test_exec` and `deploy_exec` now both
      registered for real in `forge/nodes/__init__.py::register_all`
      (`deploy_default` permission profile, network-allowed;
      `_manual_test_build_input` bridges the dispatcher's generic
      `upstream_outputs` shape to `manual_test_exec`'s `target_base_url`
      param by reading the upstream `deploy_exec` output).
      `forge/modes/feature_blast.py::build_feature_blast_request_and_graph`
      — builds the `Request`+draft `GraphVersion` for the slice above,
      run through the same `compile_and_validate` as any other graph.
      `scripts/live_feature_blast_m7.py` — the live verification script
      (same pattern as `scripts/live_smoke_test.py`), scaffolds a throwaway
      Vercel-Python toy app, asks `implement_exec` to add a new
      `api/greet.py` endpoint, deploys it for real, and independently
      curls the live deployed URL rather than trusting the run's own
      `COMPLETED_VERIFIED`.

      **First live run (user, from their own terminal):** `implement_exec`
      and `unit_verify_exec` completed for real against the authenticated
      `claude` CLI — the first genuinely live EXECUTION pipeline on a
      non-trivial task. `deploy_exec` then failed 3x identically against a
      bad/expired Vercel token (`403 ... "invalidToken":true`), burning the
      task's full retry budget on an error no retry could ever fix before
      the run gave up (`failed_exhausted`).

      Fixed in response (mine): `Attempt.requires_user_input` and
      `MechanicalResult.requires_user_input` — a mechanical runner that can
      tell its own failure is unrecoverable (not just "failed this time")
      now flags it, and `scheduler._resolve_failed_tasks` routes a FAILED
      attempt with this flag straight to a synthesized `REQUEST_USER_INPUT`
      decision (`_unrecoverable_failure_proposal`), skipping
      `retry_or_escalate_policy` entirely — one attempt, then
      `BLOCKED_AWAITING_INPUT`, not three wasted ones. `vercel_adapter.py`
      gained `VercelCredentialError` (base for `MissingVercelTokenError`
      and the new `VercelAuthenticationError`, raised for a 401/403 from
      either the create or poll call) so `deploy_exec` can distinguish "the
      token itself is rejected" from an ordinary `VercelAPIError`.

      **Second live run attempt:** a Team/Project-scoped Vercel token
      caused the 403 above — root-caused via the exact Vercel error shape
      (`invalidToken: true`), fixed by the user regenerating a
      **Full Account**-scoped token. Past that, `deploy_exec` then failed
      with a real, different Vercel API error: `400 missing_project_settings`
      — Vercel requires either a `projectSettings` object or the
      `skipAutoDetectionConfirmation=1` query parameter on a brand-new
      project's first deployment (every deploy_exec deployment is a fresh,
      disposable project, so it's always a first deployment). Fixed:
      `vercel_adapter.py`'s create-deployment POST now always passes
      `skipAutoDetectionConfirmation=1`, accepting Vercel's own automatic
      framework detection rather than requiring explicit settings for a
      project that was only just created. 227 tests. **Still not yet run
      live end-to-end** — needs the user to re-run
      `scripts/live_feature_blast_m7.py` from their own terminal with the
      Full-Account-scoped token now in `.env.local`.
- [ ] **M8** — CLI polish + `forge trace`/`forge inspect`

Everything past M8 (other modes, a third adapter, dashboard) is backlog —
see `requirements.md` — not scheduled.

## Notes

- This machine's home directory is locked down for writes/reads from this
  tool's shell (even with the sandbox override) — `gh` lives at `.gh-bin`
  with `GH_CONFIG_DIR=.gh-config` (both gitignored). Pushes must be run by
  the user directly; this tool's shell cannot reach git credentials.
