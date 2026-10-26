"""Atomic replacements retain bytes and modes, including fault boundaries."""
from concurrent.futures import ThreadPoolExecutor
import errno
import importlib
import os
import stat

import pytest

from infermatrix_copilot.persistence import atomic_write_bytes, immutable_write_bytes


def test_atomic_bytes_preserve_mode_and_replace_symlink_itself(tmp_path):
    target, link = tmp_path / "state", tmp_path / "link"
    target.write_bytes(b"original")
    target.chmod(0o600)
    atomic_write_bytes(target, "完整\n".encode())
    assert target.read_bytes() == "完整\n".encode()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    link.symlink_to(target)
    atomic_write_bytes(link, b"replacement", mode=0o640)
    assert not link.is_symlink() and link.read_bytes() == b"replacement"
    assert target.read_bytes() == "完整\n".encode()
    assert stat.S_IMODE(link.stat().st_mode) == 0o640


def test_new_file_respects_original_umask_contract(tmp_path):
    previous = os.umask(0o027)
    try:
        atomic_write_bytes(tmp_path / "state", b"bytes")
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o640


@pytest.mark.parametrize("failed_fsync", [1, 2])
def test_fsync_failure_propagates_without_partial_bytes_or_orphan_temp(tmp_path, monkeypatch, failed_fsync):
    target = tmp_path / "state"
    target.write_bytes(b"old")
    original, count = os.fsync, 0

    def fsync(fd):
        nonlocal count
        count += 1
        if count == failed_fsync:
            raise OSError("storage unavailable")
        original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError, match="storage unavailable"):
        atomic_write_bytes(target, b"new")
    assert target.read_bytes() == (b"old" if failed_fsync == 1 else b"new")
    assert list(tmp_path.iterdir()) == [target]


def test_concurrent_writers_publish_one_complete_value(tmp_path):
    target = tmp_path / "state"
    payloads = [bytes([n]) * 10000 for n in range(16)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda data: atomic_write_bytes(target, data), payloads))
    assert target.read_bytes() in payloads
    assert list(tmp_path.iterdir()) == [target]


def test_platform_without_directory_descriptors_retains_file_fsync(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    calls = []
    original = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd) or original(fd))
    target = tmp_path / "state"
    atomic_write_bytes(target, b"portable")
    assert target.read_bytes() == b"portable" and len(calls) == 1


def test_immutable_publish_is_private_and_never_replaces_an_existing_inode(tmp_path):
    target = tmp_path / "receipt"
    previous = os.umask(0o002)
    try:
        immutable_write_bytes(target, b"signed bytes")
    finally:
        os.umask(previous)
    inode = target.stat().st_ino
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        immutable_write_bytes(target, b"signed bytes")
    immutable_write_bytes(target, b"signed bytes", exist_ok=True)
    with pytest.raises(FileExistsError):
        immutable_write_bytes(target, b"changed", exist_ok=True)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        immutable_write_bytes(link, b"signed bytes", exist_ok=True)
    assert target.stat().st_ino == inode and target.read_bytes() == b"signed bytes"
    assert list(sorted(p.name for p in tmp_path.iterdir())) == ["link", "receipt"]


def test_immutable_concurrent_same_bytes_retries_and_conflicts(tmp_path):
    target = tmp_path / "receipt"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: immutable_write_bytes(target, b"signed bytes", exist_ok=True), range(16)))
    inode = target.stat().st_ino
    with pytest.raises(FileExistsError):
        immutable_write_bytes(target, b"other bytes", exist_ok=True)
    assert target.stat().st_ino == inode and list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("failed_fsync", [1, 2])
def test_immutable_fsync_failure_retains_exclusive_publication_boundary(tmp_path, monkeypatch, failed_fsync):
    target = tmp_path / "receipt"
    original, count = os.fsync, 0

    def fsync(fd):
        nonlocal count
        count += 1
        if count == failed_fsync:
            raise OSError("storage unavailable")
        original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError, match="storage unavailable"):
        immutable_write_bytes(target, b"signed bytes")
    assert target.exists() == (failed_fsync == 2)
    monkeypatch.setattr(os, "fsync", original)
    immutable_write_bytes(target, b"signed bytes", exist_ok=True)
    assert target.read_bytes() == b"signed bytes" and list(tmp_path.iterdir()) == [target]


def test_directory_callback_runs_after_complete_replacement_and_keeps_file_fsync(tmp_path, monkeypatch):
    target = tmp_path / "state"
    calls = []
    original = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append("file synced") or original(fd))

    def sync(directory):
        assert directory == tmp_path and target.read_bytes() == b"complete"
        assert calls == ["file synced"]
        raise RuntimeError("domain durability refusal")

    with pytest.raises(RuntimeError, match="domain durability refusal"):
        atomic_write_bytes(target, b"complete", directory_fsync=sync)
    assert target.read_bytes() == b"complete" and list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("module_name,error", [("push_wal", "PushWalError"), ("ci_loop", "CIOpError"),
                                             ("substate", "SubstateError")])
@pytest.mark.parametrize("error_number", [errno.EINVAL, errno.EIO])
def test_rebase_directory_fsync_keeps_platform_tolerance_and_domain_refusal(tmp_path, monkeypatch, module_name, error, error_number):
    module = importlib.import_module("infermatrix_copilot.rebase_engine." + module_name)
    monkeypatch.setattr(module, "fsync_directory", lambda _: (_ for _ in ()).throw(OSError(error_number, "storage")))
    target = tmp_path / "state.json"
    if module_name == "substate":
        state = module.Substate(tmp_path, "run")
        target, write = state.path, state._write_durable
    else:
        write = lambda data: module._durable_write(target, data)
    if error_number == errno.EIO:
        with pytest.raises(getattr(module, error), match="durability"):
            write({"run": "original"})
    else:
        write({"run": "original"})
    assert target.read_bytes() == b'{\n "run": "original"\n}'


def test_skill_writer_retains_private_mode_and_best_effort_directory_sync(tmp_path, monkeypatch):
    from infermatrix_copilot.memory.skills import _write_durable
    original, calls = os.fsync, 0

    def fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "directory unavailable")
        return original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    target = tmp_path / "SKILL.md"
    _write_durable(target, "完整\n")
    assert target.read_bytes() == "完整\n".encode() and stat.S_IMODE(target.stat().st_mode) == 0o600
