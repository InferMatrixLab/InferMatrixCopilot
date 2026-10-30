"""Fault-injection regressions for task-learning restart boundaries."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("accepted_learning_feature")
from test_chat_knowledge_learning import (
    git,
    lessons,
    prepared,
    proposal,
)
from test_chat_knowledge_learning import (
    source as _source_fixture,
)

from infermatrix_copilot.knowledge_service.learning import (
    TaskLearningError,
    TaskLearningService,
)

source = _source_fixture


class InterruptedLearning(RuntimeError):
    pass


def test_prepare_recovers_after_worktree_creation_before_database_completion(source, tmp_path, monkeypatch):
    state = tmp_path / "state"
    service = TaskLearningService(state, source)
    original = service._git

    def interrupt(root, *args):
        result = original(root, *args)
        if args[:2] == ("worktree", "add"):
            raise InterruptedLearning("transport lost after worktree creation")
        return result

    monkeypatch.setattr(service, "_git", interrupt)
    with pytest.raises(InterruptedLearning):
        service.prepare("interrupted-prepare", "x", "a" * 40, lessons())
    restarted = TaskLearningService(state, source)
    packet = restarted.prepare("interrupted-prepare", "x", "a" * 40, lessons())
    assert packet["status"] == "prepared"
    checkout = restarted._checkout(packet["task_key"])
    assert checkout.is_dir()
    assert git(checkout, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
    assert git(checkout, "status", "--porcelain") == ""


@pytest.mark.parametrize("boundary", ["after_add", "after_commit"])
def test_submit_recovers_exact_application_after_git_side_effect(source, tmp_path, monkeypatch, boundary):
    service, packet = prepared(source, tmp_path)
    rules = proposal(packet)
    original = service._git

    def interrupt(root, *args):
        result = original(root, *args)
        matched = args[0] == "add" if boundary == "after_add" else "commit" in args and "-m" in args
        if matched:
            raise InterruptedLearning("transport lost after " + boundary)
        return result

    monkeypatch.setattr(service, "_git", interrupt)
    with pytest.raises(InterruptedLearning):
        service.submit(packet["task_id"], packet["packet_digest"], rules)
    checkout = service._checkout(packet["task_key"])
    committed = git(checkout, "rev-parse", "HEAD") if boundary == "after_commit" else None
    restarted = TaskLearningService(tmp_path / "state", source)
    result = restarted.submit(packet["task_id"], packet["packet_digest"], rules)
    assert result["status"] == "local"
    assert git(Path(result["workspace"]), "rev-list", "--count", "HEAD") == "2"
    assert git(checkout, "status", "--porcelain") == ""
    if committed:
        assert result["head_sha"] == committed


def test_modified_validator_and_staged_source_work_cannot_run_or_enter_learning_commit(source, tmp_path):
    service, packet = prepared(source, tmp_path)
    checkout = service._checkout(packet["task_key"])
    executed = tmp_path / "modified-validator-executed"
    validator = checkout / "knowledge/tools/check_wiki_lint.py"
    validator.write_text(f"from pathlib import Path\nPath({str(executed)!r}).write_text('executed')\n")
    git(checkout, "add", "knowledge/tools/check_wiki_lint.py")
    try:
        result = service.submit(packet["task_id"], packet["packet_digest"], proposal(packet))
    except TaskLearningError:
        result = None
    assert not executed.exists(), "changed validator code must be refused before execution"
    if result is not None:
        assert result["status"] in {"stale", "rejected"}


def test_interrupted_recovery_preserves_unknown_staged_owner_page(source, tmp_path, monkeypatch):
    service, packet = prepared(source, tmp_path)
    rules = proposal(packet)
    original = service._git

    def interrupt(root, *args):
        result = original(root, *args)
        if args[0] == "add":
            raise InterruptedLearning("transport lost after staging generated rules")
        return result

    monkeypatch.setattr(service, "_git", interrupt)
    with pytest.raises(InterruptedLearning):
        service.submit(packet["task_id"], packet["packet_digest"], rules)
    checkout = service._checkout(packet["task_key"])
    page = checkout / "knowledge/repos/x/rules.md"
    generated = page.read_text()
    page.write_text(generated + "\nUnknown user version retained only in the index.\n")
    git(checkout, "add", "--", "knowledge/repos/x/rules.md")
    staged_before = git(checkout, "rev-parse", ":knowledge/repos/x/rules.md")
    # The working tree alone still matches the exact attempted generation.
    page.write_text(generated)
    restarted = TaskLearningService(tmp_path / "state", source)
    result = restarted.submit(packet["task_id"], packet["packet_digest"], rules)
    assert result["status"] == "stale"
    assert git(checkout, "rev-parse", ":knowledge/repos/x/rules.md") == staged_before
    assert "Unknown user version retained only in the index" in git(checkout, "show", ":knowledge/repos/x/rules.md")
    assert page.read_text() == generated
