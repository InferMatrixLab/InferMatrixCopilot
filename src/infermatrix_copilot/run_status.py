"""`run_status.json` — the durable, single-writer lifecycle record of one run.

The MCP server (mcp_server.py) launches each run as a subprocess and can only
observe it through the filesystem, so the run's state must be *durable* (survives
a server restart) and *unambiguous* (a crashed run is distinguishable from a
running one — file-presence heuristics can't do that). This module is that
record.

Two design invariants make it safe under the plan's multi-server model (Claude
Code and Codex each launch their own server):

- **Single writer.** `reserve_run` (the server) writes the initial `queued`
  record before the child exists. Once launched, the **child is the sole writer**
  during the run: it writes its own pid (`mark_child_started`) as its first act,
  then drives `planning -> running -> {done|blocked|failed}`. The parent never
  writes while the child is alive; it only reconciles *after* `.wait()` (child
  dead). Cross-process reconcilers write only after confirming the writer is
  dead. Every write takes an advisory `flock` and preserves the ownership fields,
  so there are never two concurrent writers and no lost updates.

- **Ownership-aware reconciliation.** Each run stores the `owner_server_id` /
  `owner_server_pid` that reserved it and (once launched) the `child_pid`. A
  server reconciles a non-terminal run to `interrupted` **only** when the owning
  server is confirmed dead *and* the child is dead/absent — so a second server
  never marks another live server's legitimately-`queued` run interrupted.
"""

from __future__ import annotations

from .persistence import atomic_write_bytes

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

try:  # POSIX advisory locking; the copilot targets Linux (H200 box)
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback (no cross-proc lock)
    fcntl = None  # type: ignore

# lifecycle states; TERMINAL states never transition again
QUEUED = "queued"
PLANNING = "planning"
RUNNING = "running"
DONE = "done"
BLOCKED = "blocked"
FAILED = "failed"
INTERRUPTED = "interrupted"
TERMINAL: frozenset[str] = frozenset({DONE, BLOCKED, FAILED, INTERRUPTED})

STATUS_NAME = "run_status.json"
_LOCK_NAME = "run_status.json.lock"


def mutable_status_paths() -> tuple[str, str]:
    """Lifecycle metadata touched by readers/writers, not immutable step input."""
    return STATUS_NAME, _LOCK_NAME


def status_path(run_dir: str | Path) -> Path:
    """Path to the run's status file."""
    return Path(run_dir) / STATUS_NAME


def read_status(run_dir: str | Path) -> Optional[dict]:
    """Read the status record, or None when absent/unreadable (a partially
    written file is never observed because writes are atomic `os.replace`)."""
    p = status_path(run_dir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (ValueError, OSError):
        return None


def _locked_update(run_dir: str | Path,
                   fn: Callable[[dict], Optional[dict]]) -> Optional[dict]:
    """Read-modify-write the status file under an exclusive advisory lock.

    `fn` receives the current record (``{}`` if none) and returns the fields to
    merge, or None to leave the file untouched. Holding the lock across the whole
    read+decide+write is what lets a reconciler safely check "still non-terminal?"
    and write in one critical section without racing another reconciler. The
    write itself is a tmp-file + `os.replace` so readers never see a torn file.
    Returns the new record, or the current one when `fn` opts out."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    lock = run_dir / _LOCK_NAME
    with open(lock, "w", encoding="utf-8") as lf:
        if fcntl is not None:
            fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            cur = read_status(run_dir) or {}
            updates = fn(cur)
            if updates is None:
                return cur or None
            now = time.time()
            new = {**cur, **updates, "updated": now}
            new.setdefault("created", now)
            atomic_write_bytes(status_path(run_dir), json.dumps(new, indent=2, default=str).encode("utf-8"))
            return new
        finally:
            if fcntl is not None:
                fcntl.flock(lf, fcntl.LOCK_UN)


# ── writers ───────────────────────────────────────────────────────────────────
def init_queued(run_dir: str | Path, *, run_id: str, owner_server_id: str,
                owner_server_pid: int, request_fingerprint: str = "",
                execution_mode: str = "", native_completion_sha256: str = "") -> dict:
    """Server-side initial write (before the child exists): `queued` + the
    ownership stamps used later for reconciliation. `child_pid` starts null."""
    return _locked_update(run_dir, lambda _cur: {
        "run_id": run_id, "state": QUEUED,
        "owner_server_id": owner_server_id, "owner_server_pid": int(owner_server_pid),
        "child_pid": None, "note": "",
        **({"request_fingerprint": request_fingerprint, "execution_mode": execution_mode}
           if request_fingerprint else {}),
        **({"native_completion_sha256": native_completion_sha256} if native_completion_sha256 else {}),
    })  # type: ignore[return-value]


def mark_child_started(run_dir: str | Path, *, child_pid: int,
                       state: str = PLANNING) -> dict:
    """The child's FIRST write: record its own pid and enter `planning`. Doing
    this first (rather than the parent recording the pid after Popen) keeps the
    single-writer invariant — the parent never writes while the child lives.

    Unconditional. Reserved runs use `claim_for_execution` instead, because a
    reserved run can legitimately be enqueued twice."""
    return _locked_update(run_dir, lambda _cur: {
        "child_pid": int(child_pid), "state": state,
    })  # type: ignore[return-value]


def claim_for_execution(run_dir: str | Path, *, child_pid: int) -> bool:
    """Atomically claim a reserved run for execution: `queued -> planning`.

    True when this caller won and must execute; False when someone already has,
    and the caller must exit without touching the run.

    A compare-and-set, not a write, because `RunLock` excludes *concurrent*
    children but not *serial* ones. The window it closes: the server spawns
    child A and dies before A records its pid, so the status is still `queued`
    with a null `child_pid`; a retry legitimately reclaims the reservation and
    enqueues child B; A then wakes, takes the lock, runs the whole review, marks
    it `done` and releases — and B acquires the now-free lock and walks `done`
    straight back to `planning`, reviewing the PR a second time. Nothing about
    who holds a lock can catch that; only what state the run is in when the
    holder arrives.

    Re-claiming by the same pid is idempotent so a retried entry point is not
    mistaken for a duplicate."""
    def _cas(cur: dict) -> Optional[dict]:
        recorded = cur.get("child_pid")
        if cur.get("state") == PLANNING and recorded == int(child_pid):
            return None  # already ours: idempotent, nothing to change
        if cur.get("state") != QUEUED or "child_pid" not in cur or recorded not in (None, int(child_pid)):
            return None  # someone else owns it, or it already ran
        return {"child_pid": int(child_pid), "state": PLANNING}

    after = _locked_update(run_dir, _cas) or {}
    return (after.get("state") == PLANNING
            and after.get("child_pid") == int(child_pid))


def reclaim_queued(run_dir: str | Path, *, owner_server_id: str,
                   owner_server_pid: int) -> bool:
    """Re-arm a reservation that never became a run: `interrupted -> queued`,
    re-stamped with the current server's ownership. True when re-armed.

    A deliberate, narrow exception to "TERMINAL states never transition again",
    admissible only because `interrupted` with a null `child_pid` is not an
    outcome: it is a reservation whose owner died before any child started, and
    `interrupted` is a label `reconcile_if_dead` applied on its behalf. An
    `interrupted` run that DID start a child did partial work and stays
    terminal — a genuine second attempt is a new idempotency key, not a rewind.

    Callers hold the per-key idempotency lock across this, so a second retry
    observing `queued` with a live owner is told the run already exists and does
    not enqueue it again."""
    rearmed = False

    def _rearm(cur: dict) -> Optional[dict]:
        nonlocal rearmed
        if cur.get("state") != INTERRUPTED or "child_pid" not in cur or cur.get("child_pid") is not None:
            return None
        rearmed = True
        return {"state": QUEUED, "note": "",
                "owner_server_id": owner_server_id,
                "owner_server_pid": int(owner_server_pid)}

    after = _locked_update(run_dir, _rearm) or {}
    return rearmed and after.get("state") == QUEUED and after.get("child_pid") is None


def mark(run_dir: str | Path, state: str, *, note: str = "") -> dict:
    """Child-side transition to `state` (running, or a terminal state). Ownership
    fields are preserved by the merge in `_locked_update`."""
    upd: dict[str, Any] = {"state": state}
    if note:
        upd["note"] = note
    return _locked_update(run_dir, lambda _cur: upd)  # type: ignore[return-value]


# ── liveness ──────────────────────────────────────────────────────────────────
def pid_alive(pid: Optional[int]) -> bool:
    """True if `pid` names a live process. `os.kill(pid, 0)` probes without
    signalling; PermissionError means it exists but is owned by another user."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError):
        return False
    except OSError:
        # Windows raises a plain OSError (for example WinError 87) when the
        # probed PID no longer identifies a process.  Treat that as dead so an
        # orphaned MCP run can be reconciled instead of crashing server startup.
        return False
    return True


def _servers_dir(run_root: str | Path) -> Path:
    """Directory of per-server liveness tokens under the run root."""
    return Path(run_root) / "servers"


def register_server(run_root: str | Path, server_id: str, pid: int) -> None:
    """Write this server's liveness token (`servers/<id>` -> pid). Best-effort:
    liveness failures should never crash a server."""
    d = _servers_dir(run_root)
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / server_id).write_text(str(int(pid)), encoding="utf-8")
    except OSError:
        pass


def unregister_server(run_root: str | Path, server_id: str) -> None:
    """Remove this server's liveness token on graceful shutdown (best-effort)."""
    try:
        (_servers_dir(run_root) / server_id).unlink()
    except OSError:
        pass


def server_alive(run_root: str | Path, server_id: Optional[str]) -> bool:
    """True when the server's token exists and its pid is live. (pid reuse can
    in theory give a false positive; graceful shutdown removes the token, and
    reconciliation only ever *delays*, never corrupts, in that rare case.)"""
    if not server_id:
        return False
    token = _servers_dir(run_root) / server_id
    try:
        pid = int(token.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return pid_alive(pid)


# ── reconciliation ────────────────────────────────────────────────────────────
def reconcile_after_wait(run_dir: str | Path, *, note: str = "",
                         child_pid: Optional[int] = None,
                         suspect_lock_loser: bool = False) -> Optional[dict]:
    """Called by the launching parent immediately after `Popen.wait()`. If the
    run is still non-terminal (the child died before writing a terminal state,
    e.g. SIGKILL), mark it `interrupted`. A run that terminated cleanly is
    left as-is.

    `child_pid` (the pid the caller just reaped) scopes the write: a
    duplicate child that lost the run lock exits without touching
    `run_status.json`, so the recorded `child_pid` still names the winning
    process — reconciling then would mark the live winner's run interrupted.
    Only the parent of the recorded child may reconcile. `suspect_lock_loser`
    (caller saw the loser's exit code) additionally covers the window where
    the winner holds the run lock but has not yet published its pid: an
    unclaimed status is then left alone rather than treated as a crash."""
    def _fn(cur: dict) -> Optional[dict]:
        if not cur or cur.get("state") in TERMINAL:
            return None
        recorded = cur.get("child_pid")
        if child_pid is not None and recorded not in (None, child_pid):
            return None  # a different (winning) child governs this status
        if recorded is None and suspect_lock_loser:
            return None  # winner pre-publish; a real crash never exits 3
        return {"state": INTERRUPTED,
                "note": note or "child exited without a terminal status"}
    return _locked_update(run_dir, _fn)


def reconcile_if_dead(run_dir: str | Path, run_root: str | Path) -> Optional[dict]:
    """Ownership-aware reconciliation for any server (lazy at read, at startup,
    or from a periodic sweep). Marks a non-terminal run `interrupted` **only**
    when its owning server is confirmed dead AND its child is dead/absent — so a
    live owner's `queued`/`running` run is never touched. The terminal check and
    the write share the lock, so two reconcilers can't both act."""
    def _fn(cur: dict) -> Optional[dict]:
        if not cur or cur.get("state") in TERMINAL:
            return None
        if server_alive(run_root, cur.get("owner_server_id")):
            return None  # the owner is alive; its worker/child still governs
        if pid_alive(cur.get("child_pid")):
            return None  # child still running; it will write its own terminal
        return {"state": INTERRUPTED,
                "note": "owner server and child both dead — reconciled"}
    return _locked_update(run_dir, _fn)


def startup_reconcile(run_root: str | Path) -> int:
    """Sweep every run dir once at server startup, reconciling dead-owner runs.
    Returns the count reconciled. A backstop for runs orphaned by a server death
    before any later poll would trigger the lazy path."""
    root = Path(run_root)
    if not root.exists():
        return 0
    n = 0
    for run_dir in root.glob("run-*"):
        if not run_dir.is_dir():
            continue
        try:
            res = reconcile_if_dead(run_dir, run_root)
        except (OSError, ValueError):
            # Recovery is best-effort.  One stale/corrupt run must not prevent
            # the whole stdio MCP server from initializing in its host.
            continue
        if res and res.get("state") == INTERRUPTED:
            n += 1
    return n
