"""Durable byte publication without choosing a caller's serialization or policy."""
import os
from pathlib import Path
import stat
import uuid


def fsync_directory(directory: Path) -> None:
    if hasattr(os, "O_DIRECTORY"):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _publish_bytes(path: Path, data: bytes, mode, publish, directory_fsync=fsync_directory) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else mode)
        with os.fdopen(fd, "wb") as stream:
            if mode is not None:
                os.chmod(temporary, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        publish(temporary, path)
        directory_fsync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_bytes(path, data: bytes, *, mode: int | None = None, directory_fsync=None) -> None:
    path = Path(path)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if mode is None and existing is not None and stat.S_ISREG(existing.st_mode):
        mode = stat.S_IMODE(existing.st_mode)
    _publish_bytes(path, data, mode, os.replace,
                   fsync_directory if directory_fsync is None else directory_fsync)


def immutable_write_bytes(path, data: bytes, *, mode: int = 0o600, exist_ok: bool = False) -> None:
    """Publish by exclusive hardlink; retries may verify identical regular bytes."""
    def publish(temporary, target):
        try:
            os.link(temporary, target)
        except FileExistsError:
            if not exist_ok or target.is_symlink() or not target.is_file() or target.read_bytes() != data:
                raise

    _publish_bytes(Path(path), data, mode, publish)
