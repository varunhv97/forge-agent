# Forge — project instructions

Forge is a personal coding agent whose execution is governed by a strict,
code-enforced DAG (not a prompt asking an LLM to follow a workflow). Full
design: `/Users/varvenka/.claude/plans/pasted-content-id-aee6-plan-a-wondrous-stream.md`.
Read `requirements.md`, `architecture.md`, `tasks.md` at session start.

## Core invariant

Nodes (execution and decision) never write to the graph, their own
permissions, or run state — they produce typed proposals. Only
`forge/controller/*` validates and applies them. If you're tempted to let a
node "just also" update status/schedule/permissions directly, stop — that's
exactly the pattern this project exists to prevent.

## Environment

- This machine has no `pyinit`, `direnv`, `brew`, or `pyenv`, and system
  `python3` is 3.9.6 — too old for `claude-agent-sdk` (requires >=3.10).
  `.venv/` therefore runs on a standalone CPython 3.11 build downloaded
  into `.runtimes/python` (from `astral-sh/python-build-standalone`; both
  `.runtimes/` and `.venv/` are gitignored). If either goes missing, rebuild
  with:
  ```
  mkdir -p .runtimes && cd .runtimes && curl -sL -o py.tar.gz \
    "https://github.com/astral-sh/python-build-standalone/releases/download/20260924/cpython-3.11.16%2B20260924-aarch64-apple-darwin-install_only.tar.gz" \
    && tar -xzf py.tar.gz && rm py.tar.gz && cd ..
  .runtimes/python/bin/python3 -m venv .venv
  source .venv/bin/activate && pip install -r requirements-dev.txt
  ```
- Every module using `X | Y` union syntax still needs
  `from __future__ import annotations` at the top (harmless on 3.11, and
  keeps the codebase consistent).
- Install deps with `pip install -r requirements-dev.txt` inside the venv
  (not bare `pip3 install` against system Python).
- Format with `black .`; run tests with `pytest`.
- `gh` lives at `.gh-bin` with `GH_CONFIG_DIR=.gh-config` (both gitignored)
  instead of the usual `~/.local/bin`/`~/.config/gh` — this tool's shell
  cannot read or write the home directory at all, even with sandbox
  overrides. The same restriction means `git push` must be run by the user
  directly; never assume it can be done from here.

## Working agreement

- Build iteratively: v1 = core engine + Feature Blast mode only. Every other
  mode is a backlog entry — do not build one without discussing
  it with the user first.
- Node types live in one global registry (`forge/controller/node_registry.py`)
  shared across modes — never give a mode its own private node type when an
  existing one's input/output contract already fits.
- Follow the milestone order in `tasks.md`. Each milestone's acceptance
  criterion must be independently checked (a passing test, an actual curl
  against a deployed URL, etc.) before marking it done — not just "the code
  is written."
