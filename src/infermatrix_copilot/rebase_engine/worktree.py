"""Working-tree hygiene for rebase runs — port of `01_guard_branch_clean.sh`
and `lib/apply_dirty_worktree_decision.py`.

Three neutral pieces:
- `abort_stale_inflight_state`: a halted prior run can leave a merge /
  cherry-pick / revert / rebase in flight; its unmerged entries then surface as
  regular dirty entries that `git restore` refuses ("path is unmerged"),
  failing cleanup in a confusing way. Abort them first.
- `discard_untracked_matching`: quick-discard untracked artifacts matching
  adapter-supplied patterns (e.g. pytest-copied config files) before deciding
  the tree is dirty.
- `apply_dirty_worktree_decision`: apply an L2 agent's discard/commit decision
  (same JSON schema as the parent: per-repo ``{"discard": [...], "commit":
  {"message", "paths"} | null}``). The caller supplies the decision-key →
  repo-root mapping; every mapped key must be present in the decision.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Mapping


def _log(msg: str) -> None:
    print(f"[worktree] {msg}", flush=True)


def _git(repo: Path, *args: str, check: bool = True,
         env: Mapping[str, str] | None = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["git", *args], cwd=str(repo), check=check,
                          text=True, capture_output=True, errors="replace",
                          env=dict(env) if env is not None else None)


def porcelain(repo: Path) -> str:
    r = _git(repo, "status", "--porcelain", check=False)
    return (r.stdout or "").strip()


def abort_stale_inflight_state(repo: Path, *,
                               log: Callable[[str], None] = _log) -> list[str]:
    """Abort any leftover merge/cherry-pick/revert/rebase from a halted prior
    run. Best-effort: each abort's failure is ignored (the guard's dirty-tree
    check after us still fails closed). Returns the kinds aborted."""
    repo = Path(repo)
    if not (repo / ".git").exists():
        return []
    r = _git(repo, "rev-parse", "--git-dir", check=False)
    if r.returncode != 0:
        return []
    git_dir = Path(r.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = repo / git_dir

    aborted: list[str] = []
    checks = (
        ("merge", git_dir / "MERGE_HEAD", ["merge", "--abort"]),
        ("cherry-pick", git_dir / "CHERRY_PICK_HEAD", ["cherry-pick", "--abort"]),
        ("revert", git_dir / "REVERT_HEAD", ["revert", "--abort"]),
    )
    for kind, marker, cmd in checks:
        if marker.is_file():
            log(f"Detected leftover {marker.name} in {repo.name}; "
                f"aborting stale {kind}...")
            _git(repo, *cmd, check=False)
            aborted.append(kind)
    if (git_dir / "rebase-merge").is_dir() or (git_dir / "rebase-apply").is_dir():
        log(f"Detected in-progress rebase in {repo.name}; aborting...")
        _git(repo, "rebase", "--abort", check=False)
        aborted.append("rebase")
    return aborted


def discard_untracked_matching(repo: Path, patterns: Iterable[str], *,
                               log: Callable[[str], None] = _log) -> list[str]:
    """Remove untracked files whose repo-relative path matches any of the
    given regexes (adapter data). Only untracked files are ever touched."""
    repo = Path(repo)
    compiled = [re.compile(p) for p in patterns]
    if not compiled:
        return []
    r = _git(repo, "ls-files", "--others", "--exclude-standard", check=False)
    removed: list[str] = []
    for rel in (r.stdout or "").splitlines():
        rel = rel.strip()
        if rel and any(c.search(rel) for c in compiled):
            log(f"Quick-discard untracked artifact: {rel}")
            # ls-files output is literal file names — glob chars in a name
            # must not expand into neighbours
            _git(repo, "clean", "-fd", "--", f":(literal){rel}", check=False)
            removed.append(rel)
    return removed


# -- L2 dirty-worktree decision (same JSON schema as the parent) --------------

_UNMERGED_PORCELAIN_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


class DecisionError(RuntimeError):
    """The decision JSON is malformed or names an illegal path."""


def _norm_rel(rel: str) -> str:
    """Canonical repo-relative spelling: strips trailing slashes, './', and
    doubled separators (PurePosixPath semantics) so equivalent spellings like
    'dir' and 'dir/' compare — and reach git — identically."""
    from pathlib import PurePosixPath
    return str(PurePosixPath(rel))


def _lit(rel: str) -> str:
    """Literal-pathspec form: agent-named paths are file names, never globs —
    without this, a decision naming `*.py` would expand and discard/commit far
    more than the agent said."""
    return f":(literal){rel}"


def _safe_rel(repo: Path, path: str) -> None:
    rel = Path(path)
    if rel.is_absolute():
        raise DecisionError(f"absolute path not allowed: {path}")
    if ".." in rel.parts:
        raise DecisionError(f"path must not contain '..': {path}")
    out = (repo / rel).resolve()
    try:
        out.relative_to(Path(repo).resolve())
    except ValueError as e:
        raise DecisionError(f"path escapes repo: {path}") from e


def _porcelain_for(repo: Path, rel: str) -> str:
    r = _git(repo, "status", "--porcelain", "--", _lit(rel), check=False)
    return (r.stdout or "").strip()


def _head_has_path(repo: Path, rel: str) -> bool:
    return _git(repo, "cat-file", "-e", f"HEAD:{rel}", check=False).returncode == 0


def _discard_unmerged(repo: Path, rel: str) -> None:
    """Discard an unmerged path while a merge, rebase, or cherry-pick is live:
    `git restore` refuses unmerged entries, so snap the path back to HEAD —
    keep the file if HEAD has it, otherwise remove it. Safe because the
    decision said "discard": none of the in-flight side's changes are wanted."""
    if _head_has_path(repo, rel):
        _git(repo, "checkout", "HEAD", "--", _lit(rel))
        _git(repo, "add", "--", _lit(rel))
    else:
        _git(repo, "rm", "-f", "--", _lit(rel))


def _apply_discard(repo: Path, rel: str) -> None:
    _safe_rel(repo, rel)
    line = _porcelain_for(repo, rel)
    if not line:
        return
    if line.startswith("??"):
        _git(repo, "clean", "-fd", "--", _lit(rel))
        return
    if line[:2] in _UNMERGED_PORCELAIN_CODES:
        _discard_unmerged(repo, rel)
        return
    _git(repo, "restore", "--staged", "--worktree", "--", _lit(rel))


def _apply_commit(repo: Path, message: str, paths: list[str],
                  author_name: str, author_email: str) -> None:
    for rel in paths:
        _safe_rel(repo, rel)
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
    })
    _git(repo, "add", "--", *(_lit(p) for p in paths), env=env)
    _git(repo, "commit", "--signoff", "-m", message, env=env)


def load_decision_json(path: Path) -> dict:
    """Parse the decision file; tolerate agent chatter around the JSON by
    falling back to the trailing {...} block (parent parity)."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    from ..structured_output import decode_json_object

    try:
        return decode_json_object(raw, policy="trailing_object")
    except ValueError:
        pass
    raise DecisionError(f"invalid JSON in {path}")


def _validate_block(key: str, block, root: Path) -> tuple[list[str],
                                                          tuple[str, list[str]] | None]:
    """Normalize + fully validate one repo's decision block WITHOUT touching
    the tree. Returns (discard_paths, commit) where commit is
    (message, paths) or None."""
    if not isinstance(block, Mapping):
        raise DecisionError(f"missing or invalid '{key}' object in decision")
    disc = block.get("discard")
    if disc is None:
        disc = []
    # a falsey non-list ("" / 0 / {}) must fail, not coerce to "nothing to
    # discard" — plan-then-apply only holds if malformed shapes reject here
    if not isinstance(disc, list):
        raise DecisionError(f"'{key}.discard' must be a list")
    discards: list[str] = []
    for rel in disc:
        if not isinstance(rel, str) or not rel.strip():
            raise DecisionError(f"invalid discard path in {key}: {rel!r}")
        rel = _norm_rel(rel.strip())
        _safe_rel(root, rel)
        discards.append(rel)

    commit = block.get("commit")
    if commit is None:
        return discards, None
    if not isinstance(commit, Mapping):
        raise DecisionError(f"'{key}.commit' must be null or object")
    msg = commit.get("message")
    cpaths = commit.get("paths")
    if not isinstance(msg, str) or not msg.strip():
        raise DecisionError(f"'{key}.commit.message' required")
    if not isinstance(cpaths, list) or not cpaths:
        raise DecisionError(f"'{key}.commit.paths' must be non-empty list")
    paths: list[str] = []
    for p in cpaths:
        if not isinstance(p, str) or not p.strip():
            raise DecisionError(f"invalid commit path in {key}: {p!r}")
        p = _norm_rel(p.strip())
        _safe_rel(root, p)
        paths.append(p)
    # discarding a path restores it to HEAD, so committing the same path —
    # or anything containing/contained by it — is self-contradictory: the
    # commit would fail (or partially apply) only AFTER the discard already
    # destroyed changes irreversibly. Compare NORMALIZED paths and reject
    # directory containment in either direction, not just equal spellings.
    overlap = sorted(d for d in discards
                     if any(d == p or d.startswith(p + "/")
                            or p.startswith(d + "/") for p in paths))
    if overlap:
        raise DecisionError(
            f"'{key}' discard and commit paths overlap: {overlap}")
    return discards, (msg.strip(), paths)


def apply_dirty_worktree_decision(decision: Mapping, repos: Mapping[str, Path],
                                  *, author_name: str, author_email: str) -> None:
    """Apply discard/commit blocks per repo. `repos` maps the decision's keys
    (schema-stable, e.g. the parent's "vllm"/"omni") to repo roots; every
    mapped key must be present. Discards run before the optional commit.

    The ENTIRE decision is validated before anything is applied: discards
    are irreversible, so a malformed later block must fail the run while
    every tree is still untouched — never after the first repo was already
    mutated."""
    if not author_name or not author_email:
        raise DecisionError("author_name and author_email are required")
    plan: list[tuple[Path, list[str], tuple[str, list[str]] | None]] = []
    for key, root in repos.items():
        discards, commit = _validate_block(key, decision.get(key), Path(root))
        plan.append((Path(root), discards, commit))
    for root, discards, commit in plan:
        for rel in discards:
            _apply_discard(root, rel)
        if commit is not None:
            _apply_commit(root, commit[0], commit[1], author_name, author_email)
