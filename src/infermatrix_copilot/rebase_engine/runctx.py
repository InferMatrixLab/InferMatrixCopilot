"""Run-resource lifecycle — `RebaseRuntime` + loop-scoped registry
(plan §4 / Rev 8 §3.4).

A runtime owns one run's process-lifetime resources: the substate handle,
named checkout flocks (the shared per-checkout lock every user of a checkout
must hold), registered finalizers, and the abort flag. The registry hands out
runtimes keyed by ``(run_dir, event-loop id)`` — asyncio primitives created
inside a runtime must never cross ``asyncio.run`` boundaries, so a second
`asyncio.run` over the same run_dir gets a FRESH runtime rather than one
holding another loop's dead primitives. Repeated sequential runs are the
normal chat/CLI pattern; the pinning test drives exactly that.

Teardown runs finalizers newest-first inside a bounded window and never
raises across the boundary — the caller's shielded `finally` (PR0's
`run_guarded`) depends on that.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Callable

from .substate import Substate


class RuntimeError_(RuntimeError):
    """Runtime acquisition/teardown failed."""


_EXCLUDE_COMMENT = "# rebase checkout flock — never dirt, never cleaned"
# ROOT-anchored: git's bare `locks/` matches a `locks` dir at ANY depth,
# which would hide legitimate nested source dirs from status/`add -A`;
# the runtime dir is exactly `<checkout>/locks`
_EXCLUDE_LINE = "/locks/"


class _GitMetaError(Exception):
    """Git metadata exists but could not be read/decoded — fail closed,
    never treat as a non-git directory."""


def _atomic_write(path: Path, content: str) -> None:
    """Same-directory tmp + fsync + rename — a failed write (ENOSPC, EIO,
    interruption) must never leave `info/exclude` truncated with the
    user's foreign ignore rules destroyed."""
    import os
    tmp = path.with_name(path.name + ".imx-tmp")
    try:
        with open(tmp, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _ensure_lock_dir_excluded(checkout: Path) -> bool:
    """Mark `locks/` git-ignored via the LOCAL `.git/info/exclude` (never a
    committed file, never dirt itself). Without this the live lock file is
    an untracked entry, and a workspace-hygiene pass using `git clean`, or
    a bulk `git add -A` staging, would delete or commit it. A `git clean`ed lock
    file leaves its holder flocking a DELETED inode while a new run locks
    a fresh one at the same path: mutual exclusion defeated. Ignored files
    are exempt from status, non-`-x` clean, and `add -A`.

    Returns True when the exclusion is verifiably in place — or the
    directory has NO git metadata (no git, no hygiene hazard). False for
    every failure on a git checkout, INCLUDING unreadable/undecodable
    metadata (that is "unknown git", never "no git"): the caller FAILS
    CLOSED. The pattern is ROOT-anchored (`/locks/`, migrating an earlier
    unanchored line) so nested `locks` source dirs stay visible.
    Idempotent; worktree-aware (a RELATIVE `gitdir:` pointer resolves
    against the `.git` file's directory, as git does). Call only while
    HOLDING the flock — the flock serializes writers, so a losing
    contender never touches the file."""
    checkout = Path(checkout)
    git = checkout / ".git"
    if git.is_dir():
        info = git / "info"
    elif git.is_file():  # linked worktree: `gitdir: …/.git/worktrees/<n>`
        # existing-but-INVALID metadata is as untrustworthy as unreadable
        # metadata — an empty/garbled pointer must never resolve to an
        # invented path we then "shield"
        try:
            raw = git.read_text().strip()
        except (OSError, UnicodeDecodeError):
            return False
        if not raw.startswith("gitdir:"):
            return False
        target_s = raw[len("gitdir:"):].strip()
        if not target_s or "\n" in target_s:
            return False
        target = Path(target_s)
        if not target.is_absolute():
            target = (checkout / target).resolve()
        if not (target / "HEAD").exists():  # every real git dir has HEAD
            return False
        info = (target.parent.parent / "info"
                if target.parent.name == "worktrees" else target / "info")
    else:
        import os
        if os.path.lexists(git):
            # dangling symlink, FIFO, socket, … — metadata EXISTS in some
            # unsupported form; "unknown git", never "no git": fail closed
            return False
        return True
    try:
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        text = exclude.read_text() if exclude.exists() else ""
        lines = text.split("\n")
        # OWNED entries are ONLY comment-paired lines: a standalone user
        # rule that happens to read `locks/` is FOREIGN and preserved
        # verbatim (deleting it could expose/stage deliberately-ignored
        # files)
        owned_stale = any(
            lines[i] == _EXCLUDE_COMMENT and i + 1 < len(lines)
            and lines[i + 1] == "locks/" for i in range(len(lines)))
        if _EXCLUDE_LINE not in lines or owned_stale:
            kept: list[str] = []
            i = 0
            while i < len(lines):
                if lines[i] == _EXCLUDE_COMMENT:
                    if i + 1 < len(lines) and lines[i + 1] in (
                            "locks/", _EXCLUDE_LINE):
                        i += 2      # drop OUR comment+entry pair
                    else:
                        i += 1      # drop an orphaned comment of ours
                    continue
                kept.append(lines[i])
                i += 1
            while kept and kept[-1] == "":
                kept.pop()
            _atomic_write(
                exclude,
                ("\n".join(kept) + "\n" if kept else "")
                + f"{_EXCLUDE_COMMENT}\n{_EXCLUDE_LINE}\n")
        return _EXCLUDE_LINE in exclude.read_text().split("\n")
    except (OSError, UnicodeDecodeError):
        return False


class CheckoutLock:
    """A named exclusive flock under `<checkout>/locks/<name>.lock` — the
    shared by internal rebase execution and archival."""

    def __init__(self, checkout: Path, name: str):
        self.checkout = Path(checkout)
        self.path = Path(checkout) / "locks" / f"{name}.lock"
        self._fh = None
        # why the last acquire() returned False — callers must distinguish
        # CONTENTION (retry later) from SETUP failures (permissions/
        # metadata: retrying cannot help)
        self.last_failure = ""

    def acquire(self, *, blocking: bool = True) -> bool:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows
            return True  # documented degradation: no fcntl, no exclusion
        import errno
        import os
        if not self.checkout.is_dir():
            # a typo'd path or missing mount must SURFACE, not be silently
            # fabricated as an empty "checkout" whose lock then succeeds
            self.last_failure = (f"lock setup failed (checkout does not "
                                 f"exist: {self.checkout}) — not contention")
            return False
        try:
            self.path.parent.mkdir(exist_ok=True)  # only locks/, NEVER
            #                                        the checkout itself
            # symlink-hostile: a symlinked `locks` dir or lock file would
            # (a) let open() truncate an arbitrary target and (b) NOT match
            # git's `/locks/` directory pattern — `git clean -fd` could
            # remove the symlink and a later run would lock a DIFFERENT
            # inode at the same path
            if self.path.parent.is_symlink() \
                    or not self.path.parent.is_dir():
                raise OSError(errno.EINVAL,
                              "locks dir is not a real directory: "
                              f"{self.path.parent}")
            fd = os.open(self.path,
                         os.O_CREAT | os.O_RDWR
                         | getattr(os, "O_NOFOLLOW", 0), 0o644)
            fh = os.fdopen(fd, "r+")
        except OSError as exc:
            self.last_failure = f"lock setup failed ({exc}) — not contention"
            return False
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fh, flags)
        except OSError as exc:
            fh.close()
            # only a lock CONFLICT is contention; ENOLCK/EIO/unsupported-fs
            # failures mean broken setup and waiting cannot help
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK,
                             errno.EACCES):
                self.last_failure = "contention: another run holds the lock"
            else:
                self.last_failure = (f"lock setup failed (flock: {exc}) — "
                                     "not contention")
            return False
        # the hygiene shield is installed UNDER the flock (winner-only
        # writer) and is a HARD prerequisite on git checkouts: an unignored
        # lock file faces the guard/L2 `git clean` deletion path, so
        # failing to shield it means failing to lock
        if not _ensure_lock_dir_excluded(self.checkout):
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()
            self.last_failure = (
                "hygiene shield could not be installed/verified (git "
                "metadata unreadable or info/exclude unwritable) — not "
                "contention")
            return False
        self._fh = fh
        self.last_failure = ""
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                import fcntl
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            self._fh.close()
            self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None


class RebaseRuntime:
    """One run's resource owner. Created only via `RuntimeRegistry`."""

    def __init__(self, run_dir: Path, run_id: str):
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.substate = Substate(run_dir, run_id)
        self.abort_requested = False
        self._finalizers: list[tuple[str, Callable[[], None]]] = []
        self._checkout_locks: dict[str, CheckoutLock] = {}
        self._torn_down = False

    def acquire_checkout_lock(self, checkout: Path, name: str, *,
                              blocking: bool = True) -> bool:
        """Idempotent per (checkout, name); the lock is owned by the runtime
        and released in teardown (reverse order via finalizers)."""
        key = f"{Path(checkout)}::{name}"
        lock = self._checkout_locks.get(key)
        if lock is not None and lock.held:
            return True
        lock = CheckoutLock(checkout, name)
        if not lock.acquire(blocking=blocking):
            return False
        self._checkout_locks[key] = lock
        self.add_finalizer(f"release {name} lock", lock.release)
        return True

    def add_finalizer(self, label: str, fn: Callable[[], None]) -> None:
        if self._torn_down:
            raise RuntimeError_("runtime already torn down")
        self._finalizers.append((label, fn))

    def request_abort(self) -> None:
        """Signal handlers ONLY set this flag (and cancel the task); actual
        cleanup belongs to teardown — plan §4 signal contract."""
        self.abort_requested = True

    def teardown(self, *, timeout_sec: float = 60.0) -> list[str]:
        """Run finalizers newest-first inside a bounded window. Never raises;
        returns the labels of finalizers that failed, hung past their share
        of the window, or were skipped when it closed.

        Each finalizer runs in a daemon thread joined against the REMAINING
        window — a blocked finalizer (dead NFS, wedged subprocess) cannot
        hang teardown forever and defeat the lifecycle contract. Python
        cannot kill the hung thread; it is abandoned (daemon) and reported,
        which the caller surfaces as a lock-leak warning."""
        if self._torn_down:
            return []
        self._torn_down = True
        failures: list[str] = []
        deadline = time.monotonic() + timeout_sec
        for label, fn in reversed(self._finalizers):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failures.append(f"{label} (skipped: teardown window closed)")
                continue
            outcome: list[str] = []

            def runner(fn=fn, outcome=outcome):
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001 - never raises out
                    outcome.append(f"{type(exc).__name__}: {exc}")

            t = threading.Thread(target=runner, daemon=True)
            t.start()
            t.join(remaining)
            if t.is_alive():
                failures.append(f"{label} (hung past the teardown window; "
                                "thread abandoned)")
            elif outcome:
                failures.append(f"{label} ({outcome[0]})")
        self._finalizers.clear()
        return failures


def _current_loop() -> "asyncio.AbstractEventLoop | None":
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class RuntimeRegistry:
    """Process-wide registry keyed by (run_dir, event loop) under a
    threading.Lock with double-checked creation. The loop key is a WEAK
    reference to the loop OBJECT — `id(loop)` is unusable, since a dead
    loop's memory address is routinely reused by the next `asyncio.run` and
    the stale runtime (holding the dead loop's primitives) would be handed
    out again. When the loop is garbage-collected its runtimes vanish with
    it. `release()` tears the runtime down and forgets it; a later acquire
    builds a fresh one. Sync (no-loop) contexts get their own bucket."""

    def __init__(self):
        import weakref
        self._lock = threading.Lock()
        # loop -> {run_dir -> runtime}; entries die with their loop
        self._by_loop: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
        self._sync: dict[str, RebaseRuntime] = {}

    def _bucket(self) -> dict:
        loop = _current_loop()
        if loop is None:
            return self._sync
        bucket = self._by_loop.get(loop)
        if bucket is None:
            bucket = {}
            self._by_loop[loop] = bucket
        return bucket

    def get_or_create(self, run_dir: Path, run_id: str) -> RebaseRuntime:
        key = str(Path(run_dir))
        with self._lock:
            bucket = self._bucket()
            rt = bucket.get(key)
            if rt is None:
                rt = RebaseRuntime(run_dir, run_id)
                bucket[key] = rt
            elif rt.run_id != run_id:
                raise RuntimeError_(
                    f"run_dir {key} is registered to run {rt.run_id!r}, "
                    f"not {run_id!r}")
            return rt

    def release(self, run_dir: Path, *, timeout_sec: float = 60.0) -> list[str]:
        key = str(Path(run_dir))
        with self._lock:
            rt = self._bucket().pop(key, None)
        return rt.teardown(timeout_sec=timeout_sec) if rt else []


REGISTRY = RuntimeRegistry()
