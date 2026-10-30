"""Task isolation and rejected-preparation recovery through real services."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("accepted_learning_feature")
from test_chat_knowledge_learning import git, lessons, proposal
from test_chat_knowledge_learning import source as _source_fixture

from infermatrix_copilot.knowledge_service.learning import (
    TaskLearningError,
    TaskLearningService,
)
from infermatrix_copilot.knowledge_service.learning_publication import LearningPublisher
from infermatrix_copilot.trace_store import file_lock

source = _source_fixture


def _legacy_invalid_preparation(service, monkeypatch):
    # Old versions had no committed-slice preflight: persist preparing intent,
    # create the detached checkout, and only then discover the missing slice.
    with monkeypatch.context() as old_version:
        old_version.setattr(service, "_verify_repository", lambda *_args: None)
        with pytest.raises(TaskLearningError, match="no verified knowledge slice"):
            service.prepare("legacy-typo", "unknown", "a" * 40, lessons())
    packet = service.status("legacy-typo")
    assert packet["status"] == "preparing"
    checkout = service._checkout(packet["task_key"])
    assert checkout.is_dir()
    return checkout


def test_corrected_repository_recovers_pristine_previously_poisoned_task(source, tmp_path, monkeypatch):
    service = TaskLearningService(tmp_path / "state", source)
    checkout = _legacy_invalid_preparation(service, monkeypatch)
    original_inode = checkout.stat().st_ino
    packet = service.prepare("legacy-typo", "x", "a" * 40, lessons())
    assert packet["status"] == "prepared" and packet["repository"] == "x"
    assert checkout.stat().st_ino == original_inode, "reuse the proven pristine worktree"
    assert git(source, "worktree", "list", "--porcelain").count("worktree ") == 2
    assert service.submit(packet["task_id"], packet["packet_digest"], proposal(packet))["status"] == "local"


def test_corrected_repository_recovers_legacy_intent_without_worktree(source, tmp_path, monkeypatch):
    service = TaskLearningService(tmp_path / "state", source)
    checkout = _legacy_invalid_preparation(service, monkeypatch)
    # A prior interrupted/manual cleanup may leave just the durable intent.
    git(source, "worktree", "remove", str(checkout))
    packet = service.prepare("legacy-typo", "x", "a" * 40, lessons())
    assert packet["status"] == "prepared"
    assert checkout.is_dir()


def test_corrected_repository_preserves_dirty_legacy_worktree(source, tmp_path, monkeypatch):
    service = TaskLearningService(tmp_path / "state", source)
    checkout = _legacy_invalid_preparation(service, monkeypatch)
    private = checkout / "unfinished-user-work.txt"
    private.write_text("Preserve these unknown bytes.\n")
    with pytest.raises(TaskLearningError, match="preserve it for inspection"):
        service.prepare("legacy-typo", "x", "a" * 40, lessons())
    assert private.read_text() == "Preserve these unknown bytes.\n"
    assert service.status("legacy-typo")["repository"] == "unknown"
    assert service.status("legacy-typo")["status"] == "preparing"


@pytest.mark.parametrize("artifact", ["application-intent.json", "applied.json", "unknown-user-note.txt"])
def test_corrected_repository_refuses_unknown_or_applied_legacy_artifacts(source, tmp_path, monkeypatch, artifact):
    service = TaskLearningService(tmp_path / "state", source)
    checkout = _legacy_invalid_preparation(service, monkeypatch)
    retained = checkout.parent / artifact
    retained.write_text("private state must survive\n")
    with pytest.raises(TaskLearningError, match="preserve it for inspection"):
        service.prepare("legacy-typo", "x", "a" * 40, lessons())
    assert retained.read_text() == "private state must survive\n"
    assert service.status("legacy-typo")["repository"] == "unknown"


def test_legacy_repository_correction_cannot_change_source_or_lessons(source, tmp_path, monkeypatch):
    service = TaskLearningService(tmp_path / "state", source)
    _legacy_invalid_preparation(service, monkeypatch)
    for source_pin, distilled in (("b" * 40, lessons()), ("a" * 40, lessons()[:1])):
        with pytest.raises(TaskLearningError, match="different learning packet"):
            service.prepare("legacy-typo", "x", source_pin, distilled)
    assert service.status("legacy-typo")["repository"] == "unknown"


@pytest.mark.parametrize("slice_kind", ["missing", "untracked", "file"])
def test_invalid_repository_does_not_reserve_task_or_create_worktree(source, tmp_path, slice_kind):
    invalid_slice = source / "knowledge/repos/unknown"
    if slice_kind == "untracked":
        invalid_slice.mkdir()
    elif slice_kind == "file":
        invalid_slice.write_text("A file is not a repository slice.\n")
        git(source, "add", ".")
        git(source, "commit", "-qm", "non-directory slice")
    service = TaskLearningService(tmp_path / "state", source)
    task_id = "repository-typo"

    with pytest.raises(TaskLearningError, match="no verified knowledge slice"):
        service.prepare(task_id, "unknown", "a" * 40, lessons())

    assert service.status(task_id) == {"status": "unknown"}
    assert not service._checkout(service._task(task_id)).exists()
    assert git(source, "worktree", "list", "--porcelain").count("worktree ") == 1
    corrected = service.prepare(task_id, "x", "a" * 40, lessons())
    assert corrected["status"] == "prepared"
    assert service.submit(task_id, corrected["packet_digest"], proposal(corrected))["status"] == "local"


def test_concurrent_same_task_prepare_reuses_one_frozen_worktree(source, tmp_path):
    def prepare(_):
        return TaskLearningService(tmp_path / "state", source).prepare("same-preparation", "x", "a" * 40, lessons())

    with ThreadPoolExecutor(max_workers=4) as pool:
        packets = list(pool.map(prepare, range(4)))
    assert all(packet["status"] == "prepared" for packet in packets)
    assert len({packet["packet_digest"] for packet in packets}) == 1
    assert git(source, "worktree", "list", "--porcelain").count("worktree ") == 2


@pytest.mark.parametrize("boundary", ["prepare", "apply", "publish"])
def test_slow_task_does_not_hold_sqlite_writer_lock_for_other_tasks(source, tmp_path, monkeypatch, boundary):
    first = TaskLearningService(tmp_path / "state", source)
    second = TaskLearningService(tmp_path / "state", source)
    packet = first.prepare("slow-task", "x", "a" * 40, lessons()) if boundary != "prepare" else None
    entered, release = threading.Event(), threading.Event()

    def wait():
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test task did not release its gate")

    if boundary == "prepare":
        original = first._finish_prepare

        def finish_prepare(*args):
            wait()
            return original(*args)

        monkeypatch.setattr(first, "_finish_prepare", finish_prepare)
        operation = lambda: first.prepare("slow-task", "x", "a" * 40, lessons())
    elif boundary == "apply":
        original = first._apply_task

        def apply(*args):
            wait()
            return original(*args)

        monkeypatch.setattr(first, "_apply_task", apply)
        operation = lambda: first.submit(packet["task_id"], packet["packet_digest"], proposal(packet))
    else:
        original = LearningPublisher.publish

        def publish(publisher, workspace, **kwargs):
            if kwargs["task_digest"] == packet["task_key"]:
                wait()
            return original(publisher, workspace, **kwargs)

        monkeypatch.setattr(LearningPublisher, "publish", publish)
        operation = lambda: first.submit(packet["task_id"], packet["packet_digest"], proposal(packet))

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(operation)
        try:
            assert entered.wait(5)
            # Fail fast if the slow operation owns SQLite's global write lock,
            # rather than waiting through the production 60-second timeout.
            with sqlite3.connect(first.database, timeout=0.1) as writer:
                writer.execute("BEGIN IMMEDIATE")
            assert second.status("slow-task")["status"] == ("preparing" if boundary == "prepare" else "prepared")
            other = second.prepare("independent-task", "x", "a" * 40, lessons())
            assert second.submit(other["task_id"], other["packet_digest"], proposal(other))["status"] == "local"
            assert not pending.done(), "the unrelated task must finish while the slow task is blocked"
        finally:
            release.set()
        assert pending.result(timeout=10)["status"] == ("prepared" if boundary == "prepare" else "local")


def _wait_for_file(path, process):
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"child exited before its checkpoint: {stdout}\n{stderr}")
        time.sleep(0.02)
    assert path.exists(), "child did not reach its checkpoint"


def test_same_task_submit_serializes_across_processes_and_publishes_once(source, tmp_path):
    service = TaskLearningService(tmp_path / "state", source)
    packet = service.prepare("shared-task", "x", "a" * 40, lessons())
    worker = tmp_path / "submit_worker.py"
    worker.write_text('''import json, sys, time
from pathlib import Path
from infermatrix_copilot.knowledge_service.learning import TaskLearningService
from infermatrix_copilot.knowledge_service.learning_publication import LearningPublisher

state, source, payload, signal_dir, name = sys.argv[1:]
signals = Path(signal_dir)
packet, proposals = json.loads(Path(payload).read_text())
service = TaskLearningService(state, source)

def publish(self, workspace, **kwargs):
    with (signals / "publications").open("a") as output:
        output.write(name + "\\n")
    (signals / "publishing").touch()
    deadline = time.monotonic() + 15
    while not (signals / "release").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("publication release timed out")
        time.sleep(0.02)
    return {"status": "draft_pr", "url": "https://github.com/example/knowledge/pull/1"}

LearningPublisher.publish = publish
(signals / (name + "-started")).touch()
result = service.submit(packet["task_id"], packet["packet_digest"], proposals)
print(json.dumps(result))
''')
    payload = tmp_path / "submit.json"
    payload.write_text(json.dumps([packet, proposal(packet)]))
    signals = tmp_path / "signals"
    signals.mkdir()
    environment = dict(os.environ)
    from infermatrix_copilot.knowledge_service import learning
    environment["PYTHONPATH"] = str(Path(learning.__file__).parents[2])
    processes = []
    try:
        for name in ("first", "second"):
            process = subprocess.Popen(
                [sys.executable, str(worker), str(service.state_dir), str(source), str(payload), str(signals), name],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            processes.append(process)
            _wait_for_file(signals / ("publishing" if name == "first" else "second-started"), process)
        with file_lock(service.state_dir / "locks" / f"task-{packet['task_key']}.lock", blocking=False) as held:
            assert not held, "the publishing child must hold an OS lock visible to other processes"
        (signals / "release").touch()
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
    finally:
        (signals / "release").touch()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()

    assert all(result["status"] == "draft_pr" for result in results)
    assert len({result["head_sha"] for result in results}) == 1
    assert (signals / "publications").read_text().splitlines() == ["first"]
    assert git(service._checkout(packet["task_key"]), "rev-list", "--count", "HEAD") == "2"
