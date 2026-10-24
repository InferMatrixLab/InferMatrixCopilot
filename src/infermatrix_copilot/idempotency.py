"""The durable idempotency index: one run, and one *execution*, per key.

A bot that starts a review, loses the response, and retries must get the run it
already started — not a second full review of the same commit. Scanning for an
existing run and then reserving one is not enough: two concurrent callers both
scan, both miss, and both create.

Three separate problems, all of which have to be solved for the guarantee to
hold:

* **One run directory per key.** A blocking per-key lock, and a durable
  `<run_root>/.idem/<key>.json` mapping the key to a run id.

* **One execution per key.** Returning an existing id is not enough — the start
  path would still enqueue a second child. `reserve` therefore reports whether
  it *created* the run, and only a creation is enqueued.

* **Crash safety.** Reservation is several non-atomic steps, and a server death
  after the index entry is published but before the child starts leaves a key
  pointing at a run that will never execute — the retry sees a hit, enqueues
  nothing, and polls a corpse. A hit is therefore reusable only when its run is
  live or holds a real outcome; a reservation that never started is re-armed
  and relaunched under the same lock.

Scope is explicit caller-supplied business keys, shared by Direct, Strict and
quality reservations. Keying generic `start()` on a spec hash would be actively unsafe — `issue_answer` and
`issue_filter` carry `pr=None` and no head, so every issue task in a repo would
collapse onto one key and an answer for issue #1 could return the run for #500.
"""

from __future__ import annotations

from .persistence import atomic_write_bytes

import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from . import run_status as rs
from .engine.lifecycle import fcntl, require_file_locking

INDEX_DIR = ".idem"
# Long enough that no plausible retry window outlives it, short enough to bound
# the directory for resolved terminal runs. Unknown/interrupted bindings are
# retained for reconciliation regardless of age.
DEFAULT_RETENTION_DAYS = 30

# Opaque to us; the caller supplies its own stable attempt identifier.
KEY_RE = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


class IdempotencyError(ValueError):
    """A key was presented with a spec that does not match the recorded run."""


def validate_key(key: str) -> str:
    """Check a caller-supplied key, or "" for absent."""
    if not key:
        return ""
    if not KEY_RE.match(key):
        raise IdempotencyError(
            "idempotency_key must be 1-128 chars of [A-Za-z0-9_-], got "
            f"{key[:64]!r}")
    return key


def spec_fingerprint(spec_dump: dict) -> str:
    """A canonical hash of the whole post-policy spec.

    Whole-dict rather than a hand-picked tuple, so a field added later is
    covered automatically instead of silently collapsing two different requests
    onto one key."""
    canonical = json.dumps(spec_dump, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def index_dir(run_root: Path | str) -> Path:
    return Path(run_root) / INDEX_DIR


def _entry_path(run_root: Path | str, key: str) -> Path:
    # keys are validated to a filename-safe charset, but hash anyway so a long
    # key cannot hit a filesystem name limit
    name = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return index_dir(run_root) / f"{name}.json"


@contextmanager
def key_lock(run_root: Path | str, key: str, *, timeout: float = 30.0):
    """Hold the per-key lock for the whole check-and-reserve.

    Blocking, unlike `RunLock`: a concurrent duplicate start is the ordinary
    case this exists to absorb, so the loser must WAIT and then observe the
    winner's entry. Failing fast would turn a dedupe into an error."""
    require_file_locking()
    path = _entry_path(run_root, key).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise IdempotencyError(
                        f"idempotency lock busy for {timeout:.0f}s")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def read_entry(run_root: Path | str, key: str) -> Optional[dict]:
    """Return an existing binding; only an absent file permits a new run."""
    return _read_entry_path(_entry_path(run_root, key), expected_key=key)


def _read_entry_path(path: Path, *, expected_key: str | None = None) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # A dangling link is an unreadable binding, not proof of absence.
        if path.is_symlink():
            raise IdempotencyError("idempotency binding is unreadable; reconcile the original run")
        return None
    except (OSError, ValueError) as exc:
        raise IdempotencyError("idempotency binding is unreadable; reconcile the original run") from exc
    if (not isinstance(data, dict)
            or not isinstance(data.get("key"), str) or not KEY_RE.fullmatch(data["key"])
            or (expected_key is not None and data["key"] != expected_key)
            or not isinstance(data.get("run_id"), str) or not data["run_id"]
            or data["run_id"] in (".", "..") or "/" in data["run_id"] or "\\" in data["run_id"]):
        raise IdempotencyError("idempotency binding is invalid; reconcile the original run")
    return data


def write_entry(run_root: Path | str, key: str, run_id: str,
                fingerprint: str) -> None:
    """Publish `key -> run_id` atomically: tmp in the same directory, then
    `os.replace`, so a reader can never see a half-written entry."""
    path = _entry_path(run_root, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"key": key, "run_id": run_id, "spec_fingerprint": fingerprint,
               "created": time.time()}
    atomic_write_bytes(path, json.dumps(payload, indent=2).encode("utf-8"))


def relaunchable(run_dir: Path) -> bool:
    """Whether this run was reserved but never actually started.

    `init_queued` writes `child_pid: null` and the child's claim is what sets
    it, so a null pid on an `interrupted` run means no child ever ran — the
    owner died between reserving and launching. An `interrupted` run WITH a pid
    did partial work and is a real outcome."""
    status = rs.read_status(run_dir) or {}
    return (status.get("state") == rs.INTERRUPTED
            and "child_pid" in status
            and status.get("child_pid") is None)


def resolvable(run_dir: Path) -> bool:
    """Whether an index hit can be served as-is: a live run, or a terminal one
    holding a real outcome."""
    status = rs.read_status(run_dir) or {}
    state = status.get("state")
    if state is None:
        return False
    if state not in rs.TERMINAL:
        return True
    return state != rs.INTERRUPTED or status.get("child_pid") is not None


def reap_stale(run_root: Path | str, *, retention_days: int = DEFAULT_RETENTION_DAYS,
               worktree_root: Path | None = None,
               repo_paths: Any = None) -> dict[str, int]:
    """Drop what this feature makes accumulate. Returns per-category counts.

    Retention is a mechanism this has to build, not one it inherits: nothing in
    this repo prunes `run_root`, and because the key mapping is deliberately
    retained across terminal completion, unbounded growth is the default.

    Run *directories* are deliberately NOT reaped. They are the user-visible
    audit trail (`RUN_REPORT.md`, `run_trace.jsonl`, `DIAGNOSTICS.md`), nothing
    has ever pruned them, and deleting completed runs is a separate and much
    higher-consequence change than bounding an index this feature introduces."""
    require_file_locking()
    counts = {"entries": 0, "worktrees": 0, "refs": 0}
    cutoff = time.time() - retention_days * 86_400
    root = Path(run_root)

    for entry_file in sorted(index_dir(root).glob("*.json")):
        try:
            entry = _read_entry_path(entry_file)
            if entry is None or _entry_path(root, entry["key"]) != entry_file:
                continue
            with key_lock(root, entry["key"], timeout=1.0):
                # Re-read under the lock: a prior observation must not erase a
                # replacement binding. Unknown outcomes never expire here.
                entry = read_entry(root, entry["key"])
                if entry is None:
                    continue
                run_dir = (root / entry["run_id"]).resolve()
                if run_dir.parent != root.resolve() or not run_dir.is_dir():
                    continue
                status = rs.read_status(run_dir) or {}
                if status.get("state") not in rs.TERMINAL or status.get("state") == rs.INTERRUPTED:
                    continue
                created = entry.get("created")
                if type(created) in (int, float) and 0 <= created < cutoff:
                    entry_file.unlink(missing_ok=True)
                    counts["entries"] += 1
        except (OSError, IdempotencyError):
            continue

    counts["worktrees"] = _reap_worktrees(cutoff, worktree_root)
    counts["refs"] = _reap_refs(root, repo_paths)
    return counts


def _reap_worktrees(cutoff: float, worktree_root: Path | None) -> int:
    """Remove PR-time worktrees older than the window that no run holds.

    Liveness is the tree's own shared lock, not a run registry: `run_status.json`
    exists only for MCP-reserved runs, so a CLI review is invisible to it and a
    registry-based sweep would delete the tree it is reading. Removal goes
    through `git worktree remove` so git's metadata stays consistent — never a
    bare `rmtree`."""
    from .engine import worktrees as wt
    from .engine.steps._common import git as _git

    root = Path(worktree_root) if worktree_root is not None else wt.worktree_root()
    if not root.exists():
        return 0
    removed = 0
    for dest in sorted(p for p in root.iterdir() if p.is_dir()):
        # ONLY trees this tool keys. The worktrees root is shared scratch that
        # has long held other tooling's checkouts under other naming schemes,
        # and a sweep that deleted whatever it found would destroy work it knows
        # nothing about.
        if not wt.is_managed_dest(dest):
            continue
        try:
            if dest.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        fd = os.open(str(wt.lock_path(dest)), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue  # a run holds it
            try:
                # Re-read under the lock: a run may have reused (and touched)
                # this tree after the unlocked check above, then released its
                # materialize lock before taking its hold.
                if dest.stat().st_mtime >= cutoff:
                    continue
                code, out = _git(dest, "rev-parse", "--git-common-dir",
                                 timeout=30)
                owner = Path(out.strip()) if code == 0 else None
                if owner is not None and not owner.is_absolute():
                    owner = dest / owner
                if owner is not None and owner.exists():
                    # bounded: one slow removal must not stall the server that
                    # called this, and a partial removal must not abort the
                    # sweep — the next one retries it
                    _git(owner.parent, "worktree", "remove", "--force",
                         str(dest), timeout=120)
                if not dest.exists():
                    removed += 1
                    wt.lock_path(dest).unlink(missing_ok=True)
            except (OSError, subprocess.SubprocessError):
                continue
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return removed


def _reap_refs(run_root: Path, repo_paths: Any) -> int:
    """Delete `refs/imx/<run_id>/*` for runs whose directory is gone.

    Scoped to the run id so a sweep can never unpin a live run's base or head
    commit — those refs are what hold both against a concurrent `git gc`."""
    from .engine.steps._common import git as _git

    removed = 0
    for repo in [Path(p) for p in (repo_paths or [])]:
        code, out = _git(repo, "for-each-ref", "--format=%(refname)", "refs/imx/")
        if code != 0:
            continue
        for ref in out.splitlines():
            parts = ref.strip().split("/")
            if len(parts) < 4:
                continue
            if (run_root / parts[2]).exists():
                continue
            if _git(repo, "update-ref", "-d", ref.strip())[0] == 0:
                removed += 1
    return removed
