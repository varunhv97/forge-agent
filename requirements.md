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

## v2 scope (M9, discussed and settled, not yet built)

- An interactive TUI as the default `forge` experience (light theme by
  default, dark optional), replacing one-shot CLI commands as the primary
  way to use Forge — `forge trace`/`forge inspect` stay as the plain-text
  layer underneath. Per-project state in `.forge/forge.db` +
  `.forge/worktrees`, discovered from the current working directory.
- A DAG view of a live run (not just a flat task list), including zoom
  (less detail, whole graph) and zoom-in-with-pan (full detail, cropped,
  with an overview minimap) for graphs too wide to show at once.
- User-authored custom DAGs: arrangements of *existing* node types into
  new graph shapes, saved as `.forge/dags/*.json` and run through the
  exact same `compile_and_validate` gate as Feature Blast — not new node
  behavior, not a new mode in the `Mode` enum sense. Authorable by hand or
  via a visual, keyboard-driven DAG builder that writes the same file.
- Mid-run chat: a free-form message into a *live* EXECUTION session,
  treated the same as the model's own reasoning (no controller bypass) —
  sized separately since it depends on what the Claude Agent SDK actually
  exposes for multi-turn sessions (see `tasks.md` M9 and Known gaps).

## Key decisions

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

Parent/child subgraph composition, every built-in mode but Feature Blast,
a third+ runtime adapter, learned routing, a hosted/web dashboard (the M9
TUI is a local terminal app, not this — Forge stays a personal tool run
by one person on their own machine, never a multi-user hosted service).
