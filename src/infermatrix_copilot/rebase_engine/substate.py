"""Run-scoped rebase substate — the parent's `state.json` semantics, made
durable and single-writer (plan §3 / Rev 8 §3).

The parent merged partial updates into `rebase_logs/state.json` so concurrent
writers (per-phase progress, the main CI gate) never clobbered each other, and
resume read it back. This port keeps the merge-not-overwrite contract and
adds what the parent lacked: durable writes (tmp + fsync + replace, real
storage failures propagate), an flock serializing writers, deep-merging for
nested sections (a phase updating `modules.x.status` must not erase
`modules.y`), and a run_id stamp — a resumed run refuses substate belonging
to a different run rather than silently blending two runs' truths.

Repo-neutral: module names and section shapes are data supplied by callers.
"""

from __future__ import annotations

from ..persistence import atomic_write_bytes, fsync_directory

import errno
import json
from pathlib import Path
from typing import Any, Mapping

_DIR_FSYNC_TOLERATED = {errno.EINVAL, errno.EOPNOTSUPP,
                        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
                        errno.EACCES, errno.EPERM, errno.EISDIR, errno.EBADF}


class SubstateError(RuntimeError):
    """The substate file is unusable or belongs to a different run."""


def _sync_directory(directory):
    try:
        fsync_directory(directory)
    except OSError as e:
        if e.errno not in _DIR_FSYNC_TOLERATED:
            raise SubstateError(
                "substate directory fsync failed — durability cannot be "
                "guaranteed") from e


def _deep_merge(base: dict, extra: Mapping) -> dict:
    """Recursive dict merge: nested dicts merge key-by-key, everything else
    (lists included) replaces — matching the parent's whole-value updates
    while protecting sibling keys in nested sections."""
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Substate:
    """One run's substate file at `<run_dir>/substate.json`.

    Every mutation goes through `update()` (merge semantics under the writer
    lock); reads parse the current file. The optional flock degrades to
    best-effort where fcntl is unavailable — mirroring the run-status
    protocol's documented degradation."""

    def __init__(self, run_dir: Path, run_id: str):
        if not run_id:
            raise SubstateError("run_id is required — substate is run-stamped")
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "substate.json"
        self._lock_path = self.run_dir / ".substate.lock"
        self.run_id = run_id

    # -- io -------------------------------------------------------------------

    def _read_raw(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            raise SubstateError(f"unreadable substate {self.path}: {e}") from e
        if not isinstance(data, dict):
            raise SubstateError(f"substate {self.path} is not an object")
        return data

    def _check_owner(self, data: dict) -> None:
        owner = data.get("run_id")
        if owner and owner != self.run_id:
            raise SubstateError(
                f"substate belongs to run {owner!r}, not {self.run_id!r} — "
                "refusing to blend two runs' state")

    def _write_durable(self, data: dict) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(self.path, json.dumps(data, indent=1, sort_keys=True).encode("utf-8"),
                           directory_fsync=_sync_directory)

    # -- api ------------------------------------------------------------------

    def read(self) -> dict:
        data = self._read_raw()
        self._check_owner(data)
        return data

    def update(self, extra: Mapping[str, Any]) -> dict:
        """Merge `extra` in (deep for nested dicts) under the writer lock and
        durably persist. Returns the post-merge state."""
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows
            fcntl = None
        self.run_dir.mkdir(parents=True, exist_ok=True)
        lock_file = open(self._lock_path, "w")
        try:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            data = self._read_raw()
            self._check_owner(data)
            data = _deep_merge(data, extra)
            data["run_id"] = self.run_id
            self._write_durable(data)
            return data
        finally:
            if fcntl is not None:
                try:
                    import fcntl as _f
                    _f.flock(lock_file, _f.LOCK_UN)
                except OSError:
                    pass
            lock_file.close()

    def get(self, dotted: str, default: Any = None) -> Any:
        """Read `a.b.c`-style nested keys (the parent's get_state_field)."""
        node: Any = self.read()
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set_field(self, dotted: str, value: Any) -> dict:
        """Write one `a.b.c`-style field (the parent's update_state_field),
        via the same merge machinery."""
        extra: dict = {}
        node = extra
        parts = dotted.split(".")
        for part in parts[:-1]:
            node[part] = {}
            node = node[part]
        node[parts[-1]] = value
        return self.update(extra)
