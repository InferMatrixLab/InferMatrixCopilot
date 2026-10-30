"""Durable work keeps its interpreter and excludes other release processes."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from infermatrix_copilot.upgrade import runtime


def test_reservation_keeps_original_release_and_interpreter(tmp_path, monkeypatch):
    release = tmp_path / "release-one"
    release.mkdir()
    python = release / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    monkeypatch.setenv("COPILOT_RELEASE_ID", "one")
    monkeypatch.setenv("COPILOT_RELEASE_ROOT", str(release))
    monkeypatch.setenv("COPILOT_RUNTIME_PYTHON", str(python))
    run = tmp_path / "run"
    run.mkdir()
    assert runtime.pin_run_release(run)["python"] == str(python)

    monkeypatch.setenv("COPILOT_RELEASE_ID", "two")
    monkeypatch.setenv("COPILOT_RUNTIME_PYTHON", "/different/python")
    assert runtime.pin_run_release(run)["release_id"] == "one"
    env = {"PYTHONPATH": "/new/source", "PYTHONHOME": "/new/python"}
    command = runtime.release_command(run, ["--execute-reserved", "run-id"], env)
    assert command == [str(python), "-m", "infermatrix_copilot", "--execute-reserved", "run-id"]
    assert env["COPILOT_RELEASE_ID"] == "one"
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env


def test_missing_or_corrupt_pin_never_uses_current_release(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_RUNTIME_PYTHON", str(tmp_path / "missing-python"))
    runtime.pin_run_release(tmp_path)
    with pytest.raises(RuntimeError, match="restore that release"):
        runtime.release_command(tmp_path, [], {})
    (tmp_path / runtime.RELEASE_PIN).write_text("{broken")
    with pytest.raises(RuntimeError, match="cannot read pinned runtime"):
        runtime.release_command(tmp_path, [], {})


def test_explicit_resume_forwards_exact_run_once(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_RELEASE_ID", "original")
    monkeypatch.setenv("COPILOT_RUNTIME_PYTHON", sys.executable)
    monkeypatch.delenv("COPILOT_RELEASE_ROOT", raising=False)
    runtime.pin_run_release(tmp_path)
    monkeypatch.setenv("COPILOT_RELEASE_ID", "new")
    calls = []
    monkeypatch.setattr(runtime.subprocess, "call", lambda command, **kwargs:
                        calls.append((command, kwargs)) or 3)
    assert runtime.delegate_to_release(tmp_path, ["--resume", "run-exact"]) == 3
    assert len(calls) == 1
    assert calls[0][0][-2:] == ["--resume", "run-exact"]
    monkeypatch.setenv("COPILOT_RELEASE_ID", "original")
    assert runtime.delegate_to_release(tmp_path, ["--resume", "run-exact"]) is None
    assert len(calls) == 1


def test_unstartable_pinned_runtime_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_RELEASE_ID", "original")
    monkeypatch.setenv("COPILOT_RUNTIME_PYTHON", sys.executable)
    monkeypatch.delenv("COPILOT_RELEASE_ROOT", raising=False)
    runtime.pin_run_release(tmp_path)
    monkeypatch.setenv("COPILOT_RELEASE_ID", "new")

    def cannot_execute(*args, **kwargs):
        raise PermissionError("not executable")

    monkeypatch.setattr(runtime.subprocess, "call", cannot_execute)
    with pytest.raises(RuntimeError, match="restore its interpreter"):
        runtime.delegate_to_release(tmp_path, ["--resume", "exact-run"])


def test_execution_lock_serializes_different_release_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("INFERMATRIX_COPILOT_HOME", str(tmp_path / "state"))
    marker = tmp_path / "acquired"
    ready = tmp_path / "ready"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    code = (
        "from pathlib import Path\n"
        "from infermatrix_copilot.upgrade.runtime import execution_lock\n"
        f"Path({str(ready)!r}).touch()\n"
        f"with execution_lock({str(checkout)!r}, '12'):\n"
        f"    Path({str(marker)!r}).touch()\n"
    )
    env = dict(os.environ, COPILOT_RELEASE_ID="another-release")
    proc = None
    try:
        with runtime.execution_lock(str(checkout), 12):
            proc = subprocess.Popen([sys.executable, "-c", code], env=env)
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists()
            time.sleep(0.1)
            assert not marker.exists()
        assert proc.wait(timeout=10) == 0
        assert marker.exists()
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()


def test_new_reservation_records_release_before_queued_status(tmp_path, monkeypatch):
    from infermatrix_copilot.app.reservation import RunReservation
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.task_spec import TaskSpec

    monkeypatch.setenv("COPILOT_RELEASE_ID", "reservation-release")
    monkeypatch.setenv("COPILOT_RUNTIME_PYTHON", sys.executable)
    monkeypatch.delenv("COPILOT_RELEASE_ROOT", raising=False)
    settings = Settings(_env_file=None, run_root=tmp_path)
    run_id, created = RunReservation(settings).reserve(
        TaskSpec(kind="issue_answer", repo="example", issue=1),
        owner_server_id="server", owner_server_pid=os.getpid())
    assert created
    assert runtime.read_release_pin(tmp_path / run_id)["release_id"] == "reservation-release"
    assert json.loads((tmp_path / run_id / "run_status.json").read_text())["state"] == "queued"


def test_server_launch_uses_reserved_interpreter_after_activation(tmp_path, monkeypatch):
    from infermatrix_copilot.app.run_service import RunService
    from infermatrix_copilot.config import Settings

    run = tmp_path / "run-20261030-120000-123abc"
    run.mkdir()
    original_python = tmp_path / "old-release-python"
    original_python.touch()
    monkeypatch.setenv("COPILOT_RELEASE_ID", "old-release")
    monkeypatch.setenv("COPILOT_RUNTIME_PYTHON", str(original_python))
    monkeypatch.delenv("COPILOT_RELEASE_ROOT", raising=False)
    runtime.pin_run_release(run)
    monkeypatch.setenv("COPILOT_RELEASE_ID", "new-release")
    monkeypatch.setenv("COPILOT_RUNTIME_PYTHON", "/new-release/python")
    service = RunService.__new__(RunService)
    service.settings = Settings(_env_file=None, run_root=tmp_path)
    service.run_root = tmp_path
    service._reap = lambda: None
    launches = []

    class Process:
        pid = os.getpid()
        returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr("infermatrix_copilot.app.run_service.subprocess.Popen",
                        lambda command, **kwargs: launches.append((command, kwargs)) or Process())
    monkeypatch.setattr("infermatrix_copilot.app.run_service.rs.reconcile_after_wait", lambda *a, **kw: None)
    service._launch(run.name)
    assert launches[0][0][0] == str(original_python)
    assert launches[0][1]["env"]["COPILOT_RELEASE_ID"] == "old-release"


def test_selected_resume_preserves_assertions_and_rerun_across_releases(
        tmp_path, monkeypatch):
    from types import SimpleNamespace

    from infermatrix_copilot.app.core import Copilot
    from infermatrix_copilot.app.reservation import RunReservation
    from infermatrix_copilot.cli import entry

    run_id = "run-20261030-120000-123abc"
    run = tmp_path / run_id
    run.mkdir()
    # The pinned release parses its own task format, before the new process
    # attempts to interpret or execute that workflow.
    (run / "task.json").write_text('{"future_task_format": true}')
    copilot = Copilot.__new__(Copilot)
    copilot.settings = SimpleNamespace(run_root=tmp_path)
    copilot.reservations = RunReservation(copilot.settings)
    forwarded = []
    monkeypatch.setattr("infermatrix_copilot.app.core.delegate_to_release",
                        lambda selected, args: forwarded.append((selected, args)) or 17)
    params = {"retry_limit": 1, "enabled": True, "literal": "true",
              "items": ["one", {"two": False}]}
    assert copilot.resume_last(run_id, repo="demo", playbook="release",
                               params=params, rerun_from="wave1") == 17
    assert forwarded[0][0] == run
    args = forwarded[0][1]
    assert args[:6] == ["--resume", run_id, "--repo", "demo", "--playbook", "release"]
    assert args[-2:] == ["--rerun-from", "wave1"]

    received = []
    fake = SimpleNamespace(resume_last=lambda *a, **kw: received.append((a, kw)) or 17)
    monkeypatch.setattr("infermatrix_copilot.cli.copilot.Copilot", lambda: fake)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("IMX_INVOCATION_ID", raising=False)
    assert entry.main(args) == 17
    assert received == [((run_id,), {"repo": "demo", "playbook": "release",
                                    "params": params, "rerun_from": "wave1"})]


def test_execution_keeps_rerun_checkpoint_and_releases_both_locks(
        tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from infermatrix_copilot.app.core import Copilot
    from infermatrix_copilot.engine.lifecycle import RunLock, RunLockHeld, fcntl
    from infermatrix_copilot.task_spec import TaskSpec

    run = tmp_path / "run"
    run.mkdir()
    held = []
    calls = []

    @contextmanager
    def shared_lock(checkout, pr):
        # Per-run lock is held before entering the cross-release lock.
        if fcntl is not None:
            with pytest.raises(RunLockHeld):
                RunLock(run).acquire()
        held.append((checkout, pr))
        try:
            yield
        finally:
            held.pop()

    def execute(*args, **kwargs):
        assert held == [(str(tmp_path / "checkout"), 12)]
        calls.append(kwargs)
        return SimpleNamespace(exit_code=0, blocked_reason="")

    monkeypatch.setattr("infermatrix_copilot.app.core.execution_lock", shared_lock)
    copilot = Copilot.__new__(Copilot)
    copilot.llm = None
    copilot.repository_context = SimpleNamespace(for_spec=lambda spec:
        SimpleNamespace(repo_path=str(tmp_path / "checkout")))
    copilot.workflow_execution = SimpleNamespace(run=execute)
    spec = TaskSpec(kind="pr_review", repo="demo", pr=12)
    assert copilot._execute(None, spec, run, resuming=True, rerun_from="wave1") == 0
    assert calls[0]["rerun_from"] == "wave1"
    assert calls[0]["held_lock"] is not None
    assert held == []
    with RunLock(run):
        pass


@pytest.mark.parametrize("args", [
    ["--resume", "--resume-params-json", "[]"],
    ["--resume", "--resume-params-json", "null"],
    ["--resume", "--resume-params-json", "{invalid"],
    ["--resume-params-json", "{}"],
    ["--resume", "--resume-params-json", "{}", "--task-param", "limit=1"],
])
def test_delegated_assertions_reject_invalid_or_ambiguous_shape(args, monkeypatch):
    from infermatrix_copilot.cli import entry

    def cannot_construct():
        pytest.fail("invalid delegated assertions must fail before creating Copilot")

    monkeypatch.setattr("infermatrix_copilot.cli.copilot.Copilot", cannot_construct)
    with pytest.raises(SystemExit) as exc:
        entry.main(args)
    assert exc.value.code == 2
