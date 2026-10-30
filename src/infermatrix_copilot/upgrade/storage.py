"""Small, dependency-free durable storage shared with the recovery launcher."""

from __future__ import annotations

import json
import errno
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class UpgradeError(RuntimeError):
    pass


def home_path(value: str | Path | None = None) -> Path:
    return Path(value or os.environ.get("INFERMATRIX_COPILOT_HOME")
                or Path.home() / ".infermatrix-copilot").expanduser().absolute()


def release_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", value):
        raise UpgradeError("invalid release identity")
    return value


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise UpgradeError(f"cannot read {path.name}") from exc


def atomic_json(path: Path, value) -> None:
    atomic_write(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def file_lock(path: Path, *, blocking: bool = True, timeout: float | None = 30.0):
    """OS-released lock, including on Windows; never unlink the lock file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    held = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}:
                    raise UpgradeError("filesystem does not support the required advisory lock") from exc
                if not blocking or (deadline is not None and time.monotonic() >= deadline):
                    break
                time.sleep(0.05)
        yield held
    finally:
        if held:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
