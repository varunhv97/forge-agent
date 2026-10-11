# Requirements

## What this is

A personal engineering tool that takes a substantial objective and carries
it through to a *verified* outcome, not an LLM's self-report of completion.
Scheduling, permissions, completion gates, and graph mutation are enforced
by controller code; LLMs only produce proposals the controller validates.

## v1 scope

- The core governed-DAG engine (typed models, graph validator, scheduler,
  decision/apply-decision loop, node registry, permission enforcement).
- Exactly one mode: **Feature Blast** — build an additive, bounded feature
  end to end (implement, test at three tiers, review, deploy, verify the
  deployed journey). Not "build me a whole app" — that's Project
  Bootstrapper, backlogged.
- Two execution runtimes: Claude Agent SDK (hook-enforced tool boundary) and
  Codex CLI (container-enforced, since it has no hook equivalent).
- 16 other modes are a named backlog only — no template/graph detail until
  we pick one up and discuss it first.

## Key decisions already made (see the plan doc for full rationale)

1. Execution nodes run via Claude Agent SDK or Codex CLI; decision nodes
   always run via direct Anthropic API calls with forced JSON-schema output.
2. Every attempt gets a git worktree + branch. Codex attempts additionally
   run inside a Docker container (its enforcement substitute for hooks).
3. Node types are global and mode-agnostic — one registry, reused by every
   mode, not redefined per mode.
4. Feature Blast stays additive/bounded by design.
5. Testing is three tiers, mapped from `AcceptanceCriterion.verification_method`:
   unit (written during implementation), functional/integration (a
   dedicated test-writing node), manual (LLM + browser-automation judgment,
   reused pre- and post-deploy).
6. Feature Blast is fully autonomous once started, including the actual
   deploy — `deploy_authorized` is a one-time flag, not a live per-deploy
   confirmation. The only stops are structural: ambiguous criteria, a
   missing credential, or budget exhaustion.

## Explicitly deferred (not missing — intentionally not built yet)

Parent/child subgraph composition, every mode but Feature Blast, a third+
runtime adapter, learned routing, a dashboard UI.
