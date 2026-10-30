"""Release identity and execution exclusion for durable Copilot runs.

This module never checks for updates. SDK users keep their installed runtime,
while the managed launcher supplies an immutable release identity to pin.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

RELEASE_PIN = "release.json"


def current_release() -> dict:
    """Describe this process without following a venv's Python symlink."""
    return {
        "schema_version": 1,
        "release_id": os.environ.get("COPILOT_RELEASE_ID", "unmanaged"),
        "release_root": os.environ.get("COPILOT_RELEASE_ROOT", ""),
        "python": os.path.abspath(os.environ.get("COPILOT_RUNTIME_PYTHON")
                                  or sys.executable),
    }


def read_release_pin(run_dir: str | Path) -> dict:
    """Read a pin; older runs have none, but corrupt pins must fail closed."""
    path = Path(run_dir) / RELEASE_PIN
    try:
        pin = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read pinned runtime: {path}: {exc}") from exc
    if (not isinstance(pin, dict) or pin.get("schema_version") != 1
            or not all(isinstance(pin.get(key), str)
                       for key in ("release_id", "release_root", "python"))
            or not pin["python"] or not Path(pin["python"]).is_absolute()):
        raise RuntimeError(f"invalid pinned runtime: {path}")
    return pin


def pin_run_release(run_dir: str | Path) -> dict:
    """Persist the reservation's runtime once, retaining reclaimed run pins."""
    root = Path(run_dir)
    existing = read_release_pin(root)
    if existing:
        return existing
    from .storage import atomic_json

    pin = current_release()
    atomic_json(root / RELEASE_PIN, pin)
    return pin


def release_command(run_dir: str | Path, args: list[str], env: dict) -> list[str]:
    """Select the recorded physical interpreter; never fall back on removal."""
    pin = read_release_pin(run_dir)
    if not pin:
        return [sys.executable, "-m", "infermatrix_copilot", *args]
    python = Path(pin["python"])
    if not python.is_file():
        raise RuntimeError(
            f"pinned release {pin['release_id']} is unavailable: {python}; "
            "restore that release before resuming this run")
    if pin["release_root"] and not Path(pin["release_root"]).is_dir():
        raise RuntimeError(
            f"pinned release {pin['release_id']} is unavailable: "
            f"{pin['release_root']}; restore it before resuming this run")
    env.update(COPILOT_RELEASE_ID=pin["release_id"],
               COPILOT_RELEASE_ROOT=pin["release_root"],
               COPILOT_RUNTIME_PYTHON=pin["python"])
    if pin["release_root"]:
        # An inherited source checkout would override the pinned wheel.
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
    return [str(python), "-m", "infermatrix_copilot", *args]


def delegate_to_release(run_dir: Path, args: list[str], *,
                        env: dict | None = None) -> int | None:
    """Execute an explicitly requested re-entry using its original runtime.

    A worker or resume request is forwarded once before any run is claimed or
    mutated. This never starts a replacement task or retries a failed task.
    """
    pin = read_release_pin(run_dir)
    if not pin:
        return None
    if (pin["release_id"] == current_release()["release_id"]
            and os.path.abspath(sys.executable) == pin["python"]):
        return None
    child_env = dict(os.environ) if env is None else dict(env)
    child_env["RUN_ROOT"] = str(run_dir.parent)
    command = release_command(run_dir, args, child_env)
    try:
        return subprocess.call(command, env=child_env)
    except OSError as exc:
        raise RuntimeError(
            f"pinned release {pin['release_id']} cannot start; restore its "
            "interpreter before resuming this run") from exc


@contextmanager
def execution_lock(checkout: str, pr: int | str | None) -> Iterator[None]:
    """Exclude shared-checkout work across MCP sessions and release versions.

    The worker itself holds this lock, so restarting its MCP parent cannot
    release it while the worker is alive. Lock files are never unlinked.
    """
    from .storage import file_lock

    where = (str(Path(checkout).expanduser().resolve()) if checkout
             and not checkout.startswith("repo:") else checkout)
    where = os.path.normcase(where)
    key = json.dumps([where, None if pr is None else str(pr)],
                     separators=(",", ":"), ensure_ascii=True)
    root = Path(os.environ.get("INFERMATRIX_COPILOT_HOME")
                or Path.home() / ".infermatrix-copilot").expanduser()
    path = root / "locks" / "execution" / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
    # A live task may take hours. A queued run must wait, not fail after the
    # updater's short lock timeout. Each wait still uses an OS advisory lock.
    with file_lock(path, blocking=True, timeout=None) as acquired:
        if not acquired:
            raise RuntimeError(f"cannot acquire shared execution lock: {path}")
        yield
