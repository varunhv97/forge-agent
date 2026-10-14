from __future__ import annotations

import subprocess
from pathlib import Path

from forge.models.evidence import Evidence


class InvalidationCheckError(RuntimeError):
    """
    Raised when a git subprocess invoked while checking staleness fails for
    a reason other than the specific, expected "nothing to compare" or
    "not a descendant" outcomes that `evidence_is_stale` already treats as
    ordinary `False` results (missing/placeholder commit, unrelated
    lineage). A genuinely broken `repo_root` (not a git repository) or an
    unexpected `git diff` failure surfaces here instead of leaking a raw
    `CalledProcessError`/`OSError`.
    """


def _run_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise InvalidationCheckError(
            f"failed to invoke 'git {' '.join(args)}' in '{repo_root}': {exc}"
        ) from exc


def _require_git_repo(repo_root: Path) -> None:
    # Checked once, up front, so that a bad `repo_root` (not a git
    # repository at all) always raises `InvalidationCheckError` rather
    # than being silently absorbed into "commit doesn't resolve" below —
    # `git cat-file`/`merge-base`/`diff` all fail identically (exit 128,
    # "not a git repository") whether the repo itself is missing or the
    # commit is merely unknown to it, so this is the one place that
    # distinguishes the two.
    result = _run_git(repo_root, ["rev-parse", "--git-dir"])
    if result.returncode != 0:
        raise InvalidationCheckError(
            f"'{repo_root}' is not a git repository: {(result.stderr or result.stdout).strip()}"
        )


def _commit_exists(repo_root: Path, commit: str) -> bool:
    # `git cat-file -e <commit>^{commit}` resolves `commit` and confirms it
    # is specifically a commit object (not e.g. a blob or tree sha that
    # happens to match), succeeding iff both hold. Exit code only, no
    # stdout/stderr is used. Called only after `_require_git_repo` has
    # already confirmed `repo_root` itself is valid, so any failure here
    # means specifically "this commit doesn't resolve", not "no repo".
    result = _run_git(repo_root, ["cat-file", "-e", f"{commit}^{{commit}}"])
    return result.returncode == 0


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    result = _run_git(repo_root, ["merge-base", "--is-ancestor", ancestor, descendant])
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        # Specifically means "not an ancestor" per git's own documented
        # contract for this flag — the ordinary, expected negative result.
        return False
    raise InvalidationCheckError(
        f"'git merge-base --is-ancestor {ancestor} {descendant}' in '{repo_root}' "
        f"failed unexpectedly (exit {result.returncode}): "
        f"{(result.stderr or result.stdout).strip()}"
    )


def _changed_paths(repo_root: Path, old_commit: str, new_commit: str) -> set[str]:
    result = _run_git(repo_root, ["diff", "--name-only", old_commit, new_commit])
    if result.returncode != 0:
        raise InvalidationCheckError(
            f"'git diff --name-only {old_commit} {new_commit}' in '{repo_root}' "
            f"failed (exit {result.returncode}): {(result.stderr or result.stdout).strip()}"
        )
    return {line for line in result.stdout.splitlines() if line}


def evidence_is_stale(evidence: Evidence, new_commit: str, repo_root: Path) -> bool:
    """
    Whether `evidence` should be treated as stale now that `new_commit`
    exists on the same lineage.

    Design choice for step 3 (empty `touched_paths`): evidence with no
    `touched_paths` is making a claim that isn't scoped to particular
    files (e.g. a whole-run `manual_note` or `finding_set` with no specific
    `affected_paths` recorded), so a file-level diff has no basis to say
    anything about whether it's stale — there is nothing to intersect
    against. This function therefore returns `False` for that case rather
    than treating "no paths recorded" as "every path", which would make
    any later commit on the lineage invalidate it. Only an explicit,
    decision-driven invalidation (a human/LLM decision proposal, not this
    mechanical diff check) should ever retire that kind of evidence.

    Returns `False`, without raising, for every "nothing meaningful to
    compare" case:
    - `evidence.commit_sha` is empty/falsy (the current placeholder value
      for mechanical nodes, since real commits aren't wired in yet).
    - `evidence.commit_sha == new_commit` (nothing has moved).
    - `evidence.commit_sha` does not resolve to a real commit in
      `repo_root` (also covers the placeholder-data case generally).
    - `new_commit` is not a descendant of `evidence.commit_sha` on this
      lineage (unrelated branches, or `new_commit` actually precedes
      `evidence.commit_sha`) — this check only ever looks forward in
      history.

    Raises `InvalidationCheckError` for genuinely unexpected git failures
    (e.g. `repo_root` is not a git repository at all, or `git diff` fails
    for a reason other than the ordinary cases enumerated above).
    """
    if not evidence.commit_sha or evidence.commit_sha == new_commit:
        return False

    _require_git_repo(repo_root)

    if not _commit_exists(repo_root, evidence.commit_sha):
        return False

    if not _is_ancestor(repo_root, evidence.commit_sha, new_commit):
        return False

    if not evidence.touched_paths:
        return False

    changed = _changed_paths(repo_root, evidence.commit_sha, new_commit)
    return not changed.isdisjoint(evidence.touched_paths)


def compute_stale_evidence_ids(
    evidence_list: list[Evidence], new_commit: str, repo_root: Path
) -> list[str]:
    """
    Applies `evidence_is_stale` across `evidence_list`, returning the ids
    of the entries that come back stale. A single call site for callers
    instead of a list comprehension repeated at every use.
    """
    return [
        evidence.id
        for evidence in evidence_list
        if evidence_is_stale(evidence, new_commit, repo_root)
    ]
