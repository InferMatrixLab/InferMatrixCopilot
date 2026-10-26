"""PR0 guardrails: durable progress checkpoints, the same-run lock, and the
finalizer hook point — all behavior-preserving for existing playbooks."""

import asyncio
import json
from pathlib import Path

import pytest

from infermatrix_copilot.engine import lifecycle
from infermatrix_copilot.engine.executor import Executor
from infermatrix_copilot.engine.lifecycle import RunLock, RunLockHeld, run_guarded
from infermatrix_copilot.engine.registry import StepRegistry


@pytest.fixture()
def executor(settings, trace, tmp_path: Path) -> Executor:
    return Executor(StepRegistry(), settings, run_dir=tmp_path / "run", trace=trace)


# -- durable progress.json -----------------------------------------------------

def test_save_progress_atomic_and_clean(executor):
    executor._save_progress({"completed": {"a": {"summary": "ok"}}})
    saved = json.loads(executor.progress_file.read_text())
    assert saved["completed"]["a"]["summary"] == "ok"
    # no tmp residue: the write went through rename, not in-place truncation
    assert list(executor.run_dir.glob("*.tmp")) == []


def test_save_progress_crash_leaves_old_checkpoint(executor, monkeypatch):
    """A crash mid-write (before the rename) must leave the previous checkpoint
    intact — a torn progress.json would strand every resume path."""
    executor._save_progress({"completed": {"a": {}}})
    import infermatrix_copilot.persistence as ex

    def boom(fd):
        raise OSError("simulated crash during write")

    monkeypatch.setattr(ex.os, "fsync", boom)
    with pytest.raises(OSError):
        executor._save_progress({"completed": {"a": {}, "b": {}}})
    saved = json.loads(executor.progress_file.read_text())
    assert set(saved["completed"]) == {"a"}  # old checkpoint survived


# -- same-run lock -------------------------------------------------------------

def test_run_lock_excludes_second_holder(tmp_path):
    pytest.importorskip("fcntl")  # exclusion exists only where flock does
    run_dir = tmp_path / "run"
    with RunLock(run_dir):
        with pytest.raises(RunLockHeld):
            RunLock(run_dir).acquire()


def test_run_lock_reacquirable_after_release(tmp_path):
    run_dir = tmp_path / "run"
    RunLock(run_dir).acquire().release()
    with RunLock(run_dir):
        pass  # no raise


def test_run_lock_excludes_across_processes(tmp_path):
    """The lock's whole point is cross-process exclusion (two --resume
    invocations), so contend against a real second process holding the flock."""
    pytest.importorskip("fcntl")  # POSIX-only; the lock degrades elsewhere
    import subprocess
    import sys
    import time

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl,sys,time; f=open(sys.argv[1],'w'); "
         "fcntl.flock(f, fcntl.LOCK_EX); print('held',flush=True); time.sleep(30)",
         str(run_dir / ".lock")],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(RunLockHeld):
            RunLock(run_dir).acquire()
    finally:
        holder.kill()
        holder.wait()
    time.sleep(0.05)  # kernel releases the flock with the process
    RunLock(run_dir).acquire().release()


def test_run_lock_degrades_without_fcntl(tmp_path, monkeypatch):
    """Non-POSIX platforms have no fcntl (run_status.py precedent): the lock
    becomes a no-op rather than an import error or a crash at acquire."""
    monkeypatch.setattr(lifecycle, "fcntl", None)
    lock = RunLock(tmp_path / "run").acquire()  # no raise, no lock file needed
    lock.release()


# -- finalizer hook point ------------------------------------------------------

def test_finalize_is_noop_when_nothing_registered(tmp_path):
    asyncio.run(lifecycle.finalize(tmp_path / "run", None))  # no raise


def test_finalizers_run_on_success_and_receive_outcome(tmp_path):
    run_dir = tmp_path / "run"
    seen = []

    async def fin(outcome):
        seen.append(outcome)

    lifecycle.register_finalizer(run_dir, fin)

    async def work():
        return "the-outcome"

    result = asyncio.run(run_guarded(work(), run_dir))
    assert result == "the-outcome"
    assert seen == ["the-outcome"]


def test_finalizers_run_when_the_run_raises(tmp_path):
    run_dir = tmp_path / "run"
    seen = []

    async def fin(outcome):
        seen.append(outcome)

    lifecycle.register_finalizer(run_dir, fin)

    async def work():
        raise RuntimeError("executor died")

    with pytest.raises(RuntimeError):
        asyncio.run(run_guarded(work(), run_dir))
    assert seen == [None]  # no outcome existed; finalizer still ran


def test_finalizers_run_exactly_once(tmp_path):
    run_dir = tmp_path / "run"
    seen = []

    async def fin(outcome):
        seen.append(outcome)

    lifecycle.register_finalizer(run_dir, fin)
    asyncio.run(lifecycle.finalize(run_dir, "x"))
    asyncio.run(lifecycle.finalize(run_dir, "y"))  # consumed: second is a no-op
    assert seen == ["x"]


def test_cancelled_finalizer_never_masks_the_outcome(tmp_path):
    """CancelledError is a BaseException: a finalizer that trips on an
    already-cancelled task must not mask the run's outcome or skip siblings."""
    run_dir = tmp_path / "run"
    ran = []

    async def cancelled(outcome):
        raise asyncio.CancelledError()

    async def good(outcome):
        ran.append(True)

    lifecycle.register_finalizer(run_dir, cancelled)
    lifecycle.register_finalizer(run_dir, good)

    async def work():
        return "ok"

    assert asyncio.run(run_guarded(work(), run_dir)) == "ok"
    assert ran == [True]


def test_finalizer_exception_never_masks_the_outcome(tmp_path):
    run_dir = tmp_path / "run"
    ran = []

    async def bad(outcome):
        raise RuntimeError("finalizer bug")

    async def good(outcome):
        ran.append(True)

    lifecycle.register_finalizer(run_dir, bad)
    lifecycle.register_finalizer(run_dir, good)

    async def work():
        return 42

    assert asyncio.run(run_guarded(work(), run_dir)) == 42
    assert ran == [True]  # sibling still ran after the buggy one


def test_cancellation_during_active_finalizer_completes_teardown(tmp_path):
    """Cancelling run_guarded while a finalizer is mid-flight must let the
    teardown finish (shield alone would orphan it to loop shutdown), then
    propagate the cancellation."""
    run_dir = tmp_path / "run"
    completed = []

    async def scenario():
        started = asyncio.Event()

        async def slow_fin(outcome):
            started.set()
            await asyncio.sleep(0.05)
            completed.append(outcome)

        lifecycle.register_finalizer(run_dir, slow_fin)

        async def work():
            return "ok"

        task = asyncio.ensure_future(run_guarded(work(), run_dir))
        await started.wait()  # finalizer is now mid-flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed == ["ok"]  # teardown finished before the cancel won

    asyncio.run(scenario())


def test_duplicate_execute_reserved_leaves_status_untouched(settings, tmp_path):
    """The reserved-run race: a duplicate child losing the lock must return
    BLOCKED_EXIT without rewriting child_pid or marking run_status.json — a
    poller must never observe a terminal state while the winner is running."""
    pytest.importorskip("fcntl")  # exclusion exists only where flock does
    import json as _json

    from infermatrix_copilot import run_status as rs
    from infermatrix_copilot.cli.copilot import Copilot
    from infermatrix_copilot.notify import BLOCKED_EXIT

    copilot = Copilot(settings)
    run_id = "run-20260728-000000-abc123"
    run_dir = settings.run_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "request.json").write_text("{}")
    rs.init_queued(run_dir, run_id=run_id, owner_server_id="s", owner_server_pid=1)
    rs.mark_child_started(run_dir, child_pid=4242, state=rs.RUNNING)
    before = _json.loads((run_dir / "run_status.json").read_text())

    with RunLock(run_dir):  # the winning process
        assert copilot.execute_reserved(run_id) == BLOCKED_EXIT

    after = _json.loads((run_dir / "run_status.json").read_text())
    assert after == before  # pid still 4242, state still running — untouched
