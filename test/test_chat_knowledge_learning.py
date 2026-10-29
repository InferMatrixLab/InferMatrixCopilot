"""Host-distilled task learning uses private state and an isolated checkout."""

from __future__ import annotations

import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("accepted_learning_feature")

from infermatrix_copilot.knowledge_service.learning import (
    TaskLearningError,
    TaskLearningService,
)
from infermatrix_copilot.knowledge_service.learning_publication import (
    LearningPublicationError,
    LearningPublisher,
)

PAGE = "knowledge/repos/x/rules.md"
PAGE_TEXT = """---
title: x rules
created: 2026-08-01
updated: 2026-08-01
type: rule
tags: [review]
sources: []
---

# x rules

## X-1 — existing rule

- Trigger: existing behavior changes.
- Required: preserve the current behavior.
"""


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    (root / "knowledge/repos/x").mkdir(parents=True)
    (root / "knowledge/tools").mkdir()
    (root / "knowledge/AGENTS.md").write_text("Governed knowledge\n")
    (root / PAGE).write_text(PAGE_TEXT)
    for name in ("check_knowledge_tree.py", "check_wiki_lint.py"):
        (root / "knowledge/tools" / name).write_text("raise SystemExit(0)\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root


def lessons():
    return [{"outcome": "positive", "summary": "A precise source comparison preserved the API behavior.",
             "evidence": ["source comparison passed"], "changed_paths": ["src/api.py"]},
            {"outcome": "negative", "summary": "An empty request exposed a missing boundary check.",
             "evidence": ["empty-input regression failed before the fix"], "changed_paths": ["src/api.py"]}]


def proposal(packet, rule="X-2"):
    citation = packet["source_references"][0]
    return {"rules": [{"page": PAGE, "rule_id": rule,
        "section_markdown": f"## {rule} — preserve API boundary behavior\n\n"
            "- Trigger: a patch changes API request boundary handling.\n"
            f"- Required: compare boundary results and preserve the verified public contract. ^[{citation}]",
        "sources": [citation]}]}


def prepared(source, tmp_path):
    service = TaskLearningService(tmp_path / "state", source)
    return service, service.prepare("private-chat-123", "x", "a" * 40, lessons())


def test_positive_negative_host_prompt_and_private_isolated_apply(source, tmp_path):
    original_head = git(source, "rev-parse", "HEAD")
    (source / PAGE).write_text(PAGE_TEXT + "\nUser's unfinished edit\n")
    (source / "private-untracked.txt").write_text("user data")
    service, packet = prepared(source, tmp_path)
    assert packet["outcomes"] == {"positive": 1, "negative": 1}
    events = json.loads(packet["prompt"].split('<untrusted_data encoding="json">\n')[1].split("</untrusted_data>")[0])
    assert [json.loads(event["attributes_json"])["outcome"] for event in events] == ["positive", "negative"]
    assert "private-chat-123" not in packet["prompt"]
    assert packet["source_pin"] == "a" * 40
    assert all(len(ref) <= 60 for ref in packet["source_references"])
    result = service.submit("private-chat-123", packet["packet_digest"], proposal(packet))
    assert result["status"] == "local" and result["applied"] == 1
    checkout = Path(result["workspace"])
    assert checkout != source and "## X-2" in (checkout / PAGE).read_text()
    assert "User's unfinished edit" not in (checkout / PAGE).read_text()
    assert "User's unfinished edit" in (source / PAGE).read_text()
    assert (source / "private-untracked.txt").read_text() == "user data"
    assert git(source, "rev-parse", "HEAD") == original_head
    assert git(checkout, "show", "--pretty=", "--name-only", "HEAD") == PAGE


@pytest.mark.parametrize("bad", [
    [{"outcome": "positive", "summary": "x", "transcript": "raw local chat"}],
    [{"outcome": "positive", "summary": "x", "messages": []}],
    [{"outcome": "neutral", "summary": "x"}],
    [{"outcome": "negative", "summary": "x", "changed_paths": ["../outside"]}],
    [{"outcome": "negative", "summary": "x", "evidence": ["x" * 1001]}],
])
def test_no_transcript_or_unbounded_lesson_inputs(source, tmp_path, bad):
    service = TaskLearningService(tmp_path / "state", source)
    with pytest.raises(TaskLearningError):
        service.prepare("task", "x", "a" * 40, bad)


def test_empty_lessons_and_empty_host_proposals_are_noop_without_workspace(tmp_path):
    service = TaskLearningService(tmp_path / "state", tmp_path / "not-a-checkout")
    packet = service.prepare("empty", "x", "a" * 40, [])
    assert packet["status"] == "no_op" and packet["prompt"] == ""
    assert service.submit("empty", packet["packet_digest"], {"rules": []})["applied"] == 0
    assert not (tmp_path / "state/tasks").exists()


def test_interrupted_prepare_cannot_submit_empty_digest_or_change_phase(source, tmp_path, monkeypatch):
    service = TaskLearningService(tmp_path / "state", source)
    original = service._git
    def interrupt(root, *args):
        result = original(root, *args)
        if args[:2] == ("worktree", "add"):
            raise RuntimeError("interrupted after workspace creation")
        return result
    monkeypatch.setattr(service, "_git", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        service.prepare("interrupted", "x", "a" * 40, lessons())
    restarted = TaskLearningService(tmp_path / "state", source)
    assert restarted.status("interrupted")["status"] == "preparing"
    with pytest.raises(TaskLearningError, match="finished preparation"):
        restarted.submit("interrupted", "", {"rules": []})
    with pytest.raises(TaskLearningError):
        restarted.submit("interrupted", "b" * 64, {"rules": []})
    assert restarted.status("interrupted")["status"] == "preparing"
    assert restarted.prepare("interrupted", "x", "a" * 40, lessons())["status"] == "prepared"


def test_exact_retry_restart_and_concurrent_submit_create_one_commit(source, tmp_path):
    service, packet = prepared(source, tmp_path)
    document = proposal(packet)
    def submit(_):
        return TaskLearningService(tmp_path / "state", source).submit("private-chat-123", packet["packet_digest"], document)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert len({r["head_sha"] for r in results}) == 1
    assert all(r["status"] == "local" for r in results)
    checkout = Path(results[0]["workspace"])
    assert git(checkout, "rev-list", "--count", "HEAD") == "2"
    assert service.prepare("private-chat-123", "x", "a" * 40, lessons())["packet_digest"] == packet["packet_digest"]
    assert TaskLearningService(tmp_path / "state", source).status("private-chat-123")["head_sha"] == results[0]["head_sha"]
    with pytest.raises(TaskLearningError, match="different host proposals"):
        service.submit("private-chat-123", packet["packet_digest"], proposal(packet, "X-3"))
    with pytest.raises(TaskLearningError, match="different learning packet"):
        service.prepare("private-chat-123", "x", "b" * 40, lessons())


def test_rejection_preserves_packet_and_never_applies_partial_rules(source, tmp_path):
    service, packet = prepared(source, tmp_path)
    document = proposal(packet)
    document["rules"].append({**document["rules"][0], "page": "knowledge/repos/y/rules.md"})
    result = service.submit("private-chat-123", packet["packet_digest"], document)
    assert result["status"] == "rejected" and result["applied"] == 0
    assert service.status("private-chat-123")["prompt"] == packet["prompt"]
    assert git(service._checkout(service._task("private-chat-123")), "rev-list", "--count", "HEAD") == "1"


def test_knowledge_source_advance_and_owner_page_drift_are_stale(source, tmp_path):
    service, packet = prepared(source, tmp_path)
    git(source, "commit", "--allow-empty", "-qm", "another knowledge baseline")
    assert service.submit("private-chat-123", packet["packet_digest"], proposal(packet))["status"] == "stale"


def test_changed_frozen_page_is_not_overwritten(source, tmp_path):
    service, packet = prepared(source, tmp_path)
    page = service._checkout(packet["task_key"]) / PAGE
    page.write_text(page.read_text() + "\nUnexpected page edit\n")
    assert service.submit(packet["task_id"], packet["packet_digest"], proposal(packet))["status"] == "stale"
    assert "Unexpected page edit" in page.read_text()


def test_sha256_source_pin_and_unknown_repository(source, tmp_path):
    service = TaskLearningService(tmp_path / "state", source)
    packet = service.prepare("sha256-task", "x", "d" * 64, lessons())
    assert packet["source_pin"] == "d" * 64 and all(len(ref) <= 60 for ref in packet["source_references"])
    assert service.submit(packet["task_id"], packet["packet_digest"], proposal(packet))["status"] == "local"
    with pytest.raises(TaskLearningError, match="verified knowledge slice"):
        service.prepare("other-task", "unknown", "a" * 40, lessons())


def test_validator_failure_rolls_back_and_retains_packet(source, tmp_path):
    (source / "knowledge/tools/check_wiki_lint.py").write_text("raise SystemExit(1)\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "failing validator")
    service, packet = prepared(source, tmp_path)
    result = service.submit("private-chat-123", packet["packet_digest"], proposal(packet))
    assert result["status"] == "validator_failed" and result["validation"]["rolled_back"]
    assert service.status("private-chat-123")["packet_digest"] == packet["packet_digest"]
    checkout = service._checkout(service._task("private-chat-123"))
    assert (checkout / PAGE).read_text() == PAGE_TEXT


def test_publication_requires_optin_and_both_flags(source, monkeypatch):
    def unavailable(*_):
        raise AssertionError("publication must not call the network")
    monkeypatch.setattr("infermatrix_copilot.knowledge_service.learning_publication._run", unavailable)
    local = LearningPublisher({}).publish(source, task_digest="a" * 64, head="b" * 40,
        packet_digest="c" * 64, rules=1, outcomes={})
    assert local["status"] == "local"
    monkeypatch.setattr("infermatrix_copilot.knowledge_service.learning_publication._run", lambda command, workspace: "main")
    for flags in ({}, {"ALLOW_PUSH": "1"}, {"ALLOW_POST": "1"}):
        result = LearningPublisher({"KNOWLEDGE_LEARNING_PUBLICATION": "draft_pr",
            "KNOWLEDGE_LEARNING_REPOSITORY": "owner/knowledge", **flags}).publish(source,
                task_digest="a" * 64, head="b" * 40, packet_digest="c" * 64, rules=1, outcomes={})
        assert result["status"] == "publication_blocked"


def test_failed_upload_retained_and_retry_reuses_applied_commit(source, tmp_path, monkeypatch):
    service, packet = prepared(source, tmp_path)
    def fail(*_args, **_kwargs):
        raise LearningPublicationError("upload failed")
    monkeypatch.setattr(LearningPublisher, "publish", fail)
    result = service.submit("private-chat-123", packet["packet_digest"], proposal(packet))
    assert result["status"] == "publication_failed" and result["applied"] == 1
    head = result["head_sha"]
    monkeypatch.setattr(LearningPublisher, "publish", lambda *_args, **_kwargs:
        {"status": "draft_pr", "url": "https://github.com/owner/knowledge/pull/3"})
    retried = TaskLearningService(tmp_path / "state", source).submit("private-chat-123", packet["packet_digest"], proposal(packet))
    assert retried["status"] == "draft_pr" and retried["head_sha"] == head
    assert git(Path(result["workspace"]), "rev-list", "--count", "HEAD") == "2"


def test_publisher_create_only_push_and_pr_recovery_do_not_publish_private_context(source, monkeypatch):
    calls, reviews = [], []
    head, digest = "b" * 40, "c" * 64
    def run(command, workspace):
        calls.append(command)
        if command[:3] == ["git", "check-ref-format", "--branch"]:
            return "main"
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps(reviews)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return head
        if command[:3] == ["git", "rev-parse", "HEAD^"]:
            return "d" * 40
        if command[:2] == ["git", "diff"]:
            return PAGE
        if command[:2] == ["git", "merge-base"]:
            return "d" * 40
        if command[:3] == ["gh", "pr", "create"]:
            body = Path(command[-1]).read_text()
            assert "positive" in body and "negative" in body
            assert "source pin: `" + "e" * 40 + "`" in body
            assert "private-chat-123" not in body and str(source) not in body
            reviews.append({"url": "https://github.com/owner/knowledge/pull/4", "headRefOid": head,
                "baseRefName": "main", "body": body, "isDraft": True, "state": "OPEN"})
            raise LearningPublicationError("response lost after PR creation")
        return ""
    monkeypatch.setattr("infermatrix_copilot.knowledge_service.learning_publication._run", run)
    publisher = LearningPublisher({"KNOWLEDGE_LEARNING_PUBLICATION": "draft_pr",
        "KNOWLEDGE_LEARNING_REPOSITORY": "owner/knowledge", "ALLOW_PUSH": "1", "ALLOW_POST": "1"})
    options = {"task_digest": "a" * 64, "head": head, "packet_digest": digest, "rules": 1,
               "source_pin": "e" * 40, "source_repository": "x",
               "outcomes": {"positive": 1, "negative": 1}, "base_sha": "d" * 40,
               "documents": {PAGE: hashlib.sha256((source / PAGE).read_bytes()).hexdigest()}}
    first = publisher.publish(source, **options)
    assert first["status"] == "draft_pr"
    assert publisher.publish(source, **options) == first
    pushes = [c for c in calls if c[:2] == ["git", "push"]]
    assert len(pushes) == 1
    assert pushes[0][-1] == "--force-with-lease=knowledge/chat-" + "a" * 24 + ":"
    assert len([c for c in calls if c[:3] == ["gh", "pr", "create"]]) == 1


def test_existing_remote_branch_cannot_be_overwritten(source, monkeypatch):
    calls = []
    def run(command, workspace):
        calls.append(command)
        if command[:3] == ["git", "check-ref-format", "--branch"]:
            return "main"
        if command[:3] == ["gh", "pr", "list"]:
            return "[]"
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "b" * 40
        if command[:3] == ["git", "rev-parse", "HEAD^"]:
            return "d" * 40
        if command[:2] == ["git", "status"] or command[:2] == ["git", "fetch"]:
            return ""
        if command[:2] == ["git", "diff"]:
            return PAGE
        if command[:2] == ["git", "merge-base"]:
            return "d" * 40
        if command[:2] == ["git", "ls-remote"]:
            return "e" * 40 + "\trefs/heads/knowledge/chat-" + "a" * 24
        raise AssertionError("conflicting branch must never be pushed or reviewed")
    monkeypatch.setattr("infermatrix_copilot.knowledge_service.learning_publication._run", run)
    publisher = LearningPublisher({"KNOWLEDGE_LEARNING_PUBLICATION": "draft_pr",
        "KNOWLEDGE_LEARNING_REPOSITORY": "owner/knowledge", "ALLOW_PUSH": "1", "ALLOW_POST": "1"})
    with pytest.raises(LearningPublicationError, match="different work"):
        publisher.publish(source, task_digest="a" * 64, head="b" * 40, packet_digest="c" * 64, rules=1, outcomes={},
                          base_sha="d" * 40, documents={PAGE: hashlib.sha256((source / PAGE).read_bytes()).hexdigest()})
    assert not any(command[:2] == ["git", "push"] for command in calls)


def test_feature_branch_ancestry_never_enters_a_knowledge_pr(source, tmp_path, monkeypatch):
    destination_base = git(source, "rev-parse", "HEAD")
    (source / "unrelated-feature.py").write_text("unrelated = True\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "unfinished user feature")
    feature_base = git(source, "rev-parse", "HEAD")
    service, packet = prepared(source, tmp_path)
    result = service.submit(packet["task_id"], packet["packet_digest"], proposal(packet))
    checkout = Path(result["workspace"])
    from infermatrix_copilot.knowledge_service import learning_publication
    original = learning_publication._run
    calls = []
    def run(command, workspace):
        calls.append(command)
        if command[:2] == ["git", "fetch"]:
            git(workspace, "update-ref", command[-1].split(":", 1)[1], destination_base)
            return ""
        if command[0] == "gh" or command[:2] == ["git", "push"]:
            raise AssertionError("unrelated ancestry must fail before any review or push")
        return original(command, workspace)
    monkeypatch.setattr(learning_publication, "_run", run)
    publisher = LearningPublisher({"KNOWLEDGE_LEARNING_PUBLICATION": "draft_pr",
        "KNOWLEDGE_LEARNING_REPOSITORY": "owner/knowledge", "ALLOW_PUSH": "1", "ALLOW_POST": "1"})
    with pytest.raises(LearningPublicationError, match="outside the destination"):
        publisher.publish(checkout, task_digest=packet["task_key"], head=result["head_sha"],
            packet_digest=packet["packet_digest"], rules=1, outcomes={}, base_sha=feature_base,
            documents={PAGE: hashlib.sha256((checkout / PAGE).read_bytes()).hexdigest()})
    assert not any(command[:2] == ["git", "push"] for command in calls)
