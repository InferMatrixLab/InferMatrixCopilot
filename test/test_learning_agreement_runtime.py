"""Runtime consent gates precede data collection, grading, and publication."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from urllib.error import HTTPError

import pytest

from infermatrix_copilot.knowledge_service.agreement import (
    AgreementRequiredError,
    AgreementStore,
)
from infermatrix_copilot.knowledge_service.feedback import KnowledgeFeedbackStore
from infermatrix_copilot.knowledge_service.feedback_sync import (
    FeedbackSyncClient,
    FeedbackSyncConfig,
    FeedbackSyncError,
)
from infermatrix_copilot.knowledge_service.learning import TaskLearningService
from infermatrix_copilot.knowledge_service.learning_publication import LearningPublisher
from infermatrix_copilot.knowledge_view import KnowledgeView

PAGE = "repos/r/rules.md"
CONTENT = "# Rules\n\n## SOURCE-1a — Preserve the source\n\nKeep the executable source contract.\n"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    from infermatrix_copilot.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", ())
    for name in ("KNOWLEDGE_FEEDBACK_URL", "KNOWLEDGE_FEEDBACK_TOKEN",
                 "KNOWLEDGE_LEARNING_STATE_DIR", "KNOWLEDGE_LEARNING_PUBLICATION",
                 "KNOWLEDGE_LEARNING_REPOSITORY", "KNOWLEDGE_LEARNING_BASE"):
        monkeypatch.delenv(name, raising=False)


def accept(state_dir, *, feedback_url=None, environ=None):
    """Create a synthetic acknowledgement exclusively in the test directory."""
    agreement = AgreementStore.from_env(state_dir=state_dir, feedback_url=feedback_url, environ=environ)
    document = agreement.document()
    agreement._accept("Synthetic Test User", version=document["version"],
                      agreement_sha256=document["sha256"],
                      context_sha256=document["context_sha256"], confirmation="I AGREE")
    return agreement


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "knowledge"
    (root / PAGE).parent.mkdir(parents=True)
    (root / PAGE).write_text(CONTENT)
    return KnowledgeFeedbackStore(tmp_path / "state"), KnowledgeView(root, "snapshot-1")


def issue(world, task="task-1"):
    store, view = world
    return store.issue_delivery(task_id=task, repository="r", view=view,
                                documents=[{"path": PAGE, "content": CONTENT}])


def vote(receipt):
    return {"target_id": receipt["targets"][0]["target_id"], "score": 1, "used": True,
            "reason": "The rule guided a verified result.", "evidence": ["Focused contract test passed."]}


def record(store, receipt):
    return store.record(task_id=receipt["task_id"], delivery_id=receipt["delivery_id"],
                        ratings=[vote(receipt)])


def test_unsigned_learning_refuses_before_lesson_processing_or_runtime_creation(tmp_path, monkeypatch):
    service = TaskLearningService(tmp_path / "state", tmp_path / "no-checkout")

    def never_process(_lessons):
        raise AssertionError("unsigned lessons must not be processed")

    monkeypatch.setattr(service, "_lessons", never_process)
    with pytest.raises(AgreementRequiredError):
        service.prepare("task", "r", "a" * 40, [])
    with pytest.raises(AgreementRequiredError):
        service.submit("task", "invalid", {"rules": []})
    assert service.status("task") == {"status": "unknown"}
    assert not service.state_dir.exists()


def test_unsigned_feedback_never_touches_content_or_creates_ledger(tmp_path):
    class UnreadableView:
        @property
        def root(self):
            raise AssertionError("unsigned delivery must not inspect knowledge")

    store = KnowledgeFeedbackStore(tmp_path / "state")
    result = store.issue_delivery(task_id="task", repository="r", view=UnreadableView(), documents=None)
    assert result == {"status": "agreement_required", "protocol": "knowledge-feedback/1", "targets": []}
    with pytest.raises(AgreementRequiredError):
        store.record(task_id="task", delivery_id="invalid", ratings=[])
    with pytest.raises(AgreementRequiredError):
        store.central_report()
    assert store.sync_pending()["status"] == "agreement_required"
    assert store.sync_status()["pending_count"] == 0
    assert store.repositories_for_task("task") == ()
    assert store.report()["total_ratings"] == 0
    assert not store.state_dir.exists()


def test_acknowledgement_enables_grading_and_revocation_preserves_identity(world):
    store, _view = world
    agreement = accept(store.state_dir)
    receipt = issue(world)
    first = record(store, receipt)
    assert first["sync"]["status"] == "disabled"
    agreement.revoke()
    assert issue(world)["status"] == "agreement_required"
    with pytest.raises(AgreementRequiredError):
        record(store, receipt)
    assert store.sync_pending()["pending_count"] == 1
    assert store.report()["total_ratings"] == 1
    assert store.repositories_for_task("task-1") == ("r",)
    accept(store.state_dir)
    assert issue(world) == receipt
    assert record(store, receipt)["ratings"] == first["ratings"]
    assert store.report()["total_ratings"] == 1


def test_changed_agreement_version_refuses_new_events(world, monkeypatch):
    from infermatrix_copilot.knowledge_service import agreement as module

    store, _view = world
    accept(store.state_dir)
    receipt = issue(world)
    monkeypatch.setattr(module, "AGREEMENT_VERSION", "future-version")
    assert issue(world)["status"] == "agreement_required"
    with pytest.raises(AgreementRequiredError):
        record(store, receipt)


def test_real_constructor_destination_must_match_acknowledgement(world, monkeypatch):
    original, view = world
    first_url, second_url = "https://first.example", "https://second.example"
    monkeypatch.setenv("KNOWLEDGE_FEEDBACK_URL", second_url)
    # Signing a different configured URL cannot authorize the actual constructor URL.
    accept(original.state_dir)
    configured = KnowledgeFeedbackStore(original.state_dir,
                                        sync_config=FeedbackSyncConfig(first_url, "x" * 32))
    assert issue((configured, view))["status"] == "agreement_required"
    accept(original.state_dir, feedback_url=first_url)
    assert "delivery_id" in issue((configured, view))
    configured.sync_config = FeedbackSyncConfig(second_url, "x" * 32)
    assert issue((configured, view))["status"] == "agreement_required"


def test_revocation_blocks_legacy_backfill_and_preserves_queue(world):
    store, _view = world
    agreement = accept(store.state_dir)
    receipt = issue(world)
    record(store, receipt)
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM feedback_outbox")
        db.execute("DELETE FROM feedback_sync_meta")
    agreement.revoke()
    assert store.sync_pending()["pending_count"] == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_outbox").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM feedback_sync_meta").fetchone()[0] == 0
    accept(store.state_dir)
    assert store.sync_pending()["pending_count"] == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_outbox").fetchone()[0] == 1


def test_transport_is_gated_even_when_called_directly(tmp_path, monkeypatch):
    config = FeedbackSyncConfig("https://collector.example", "x" * 32)
    client = FeedbackSyncClient(config, state_dir=tmp_path / "state")

    def never_network(*_args, **_kwargs):
        raise AssertionError("unsigned transport must not open a network connection")

    monkeypatch.setattr("infermatrix_copilot.knowledge_service.feedback_sync.build_opener", never_network)
    with pytest.raises(AgreementRequiredError):
        client.send(receipt={}, rating={}, target={}, feedback_id="invalid")
    with pytest.raises(AgreementRequiredError):
        client._request("/v1/feedback/events", {})
    with pytest.raises(AgreementRequiredError):
        client.report(repository="r", limit=100)
    assert not client.state_dir.exists()


def test_transport_adds_minimal_attestation_and_rechecks_before_request(world, monkeypatch):
    original, view = world
    config = FeedbackSyncConfig("https://collector.example", "x" * 32)
    store = KnowledgeFeedbackStore(original.state_dir, sync_config=config)
    agreement = accept(store.state_dir, feedback_url=config.url)
    receipt = issue((store, view))
    events = []

    def inspect_event(client, path, payload):
        events.append(payload)
        agreement.revoke()
        # Exercise the real transport gate after send's first check.
        return original_request(client, path, payload)

    original_request = FeedbackSyncClient._request
    monkeypatch.setattr(FeedbackSyncClient, "_request", inspect_event)
    result = record(store, receipt)
    assert result["sync"]["status"] == "agreement_required"
    assert result["sync"]["pending_count"] == 1
    assert len(events) == 1
    attestation = events[0]["agreement"]
    assert attestation["purpose"] == "feedback_upload"
    assert "Synthetic Test User" not in json.dumps(attestation)
    assert "signer" not in attestation


def test_flush_rechecks_between_events_without_dropping_pending_rows(world, monkeypatch):
    original, view = world
    config = FeedbackSyncConfig("https://collector.example", "x" * 32)
    store = KnowledgeFeedbackStore(original.state_dir, sync_config=config)
    agreement = accept(store.state_dir, feedback_url=config.url)

    def offline(*_args, **_kwargs):
        raise FeedbackSyncError("offline")

    monkeypatch.setattr(FeedbackSyncClient, "send", offline)
    for task in ("task-1", "task-2"):
        receipt = issue((store, view), task=task)
        record(store, receipt)
    uploaded = []

    def send_then_revoke(*_args, **kwargs):
        uploaded.append(kwargs["feedback_id"])
        agreement.revoke()

    monkeypatch.setattr(FeedbackSyncClient, "send", send_then_revoke)
    result = store.sync_pending()
    assert result["status"] == "agreement_required"
    assert result["pending_count"] == 1
    assert result["acknowledged_count"] == 1
    assert len(uploaded) == 1
    assert store.report()["total_ratings"] == 2


def test_publisher_requires_explicit_agreement_before_any_command(tmp_path, monkeypatch):
    def never_run(*_args, **_kwargs):
        raise AssertionError("unsigned publication must not run commands")

    monkeypatch.setattr("infermatrix_copilot.knowledge_service.learning_publication._run", never_run)
    publisher = LearningPublisher({"KNOWLEDGE_LEARNING_PUBLICATION": "draft_pr",
                                   "KNOWLEDGE_LEARNING_REPOSITORY": "owner/knowledge",
                                   "ALLOW_PUSH": "1", "ALLOW_POST": "1"}, state_dir=tmp_path / "state")
    with pytest.raises(AgreementRequiredError):
        publisher.publish(tmp_path, task_digest="a" * 64, head="b" * 40,
                          packet_digest="c" * 64, rules=1, outcomes={})
    assert not (tmp_path / "state").exists()


def test_learning_pending_submission_requires_current_agreement(tmp_path):
    state = tmp_path / "state"
    agreement = accept(state)
    service = TaskLearningService(state, tmp_path / "no-checkout")
    packet = service.prepare("empty", "r", "a" * 40, [])
    agreement.revoke()
    before = service.database.read_bytes()
    with pytest.raises(AgreementRequiredError):
        service.submit("empty", packet["packet_digest"], {"rules": []})
    assert service.database.read_bytes() == before
    assert service.status("empty")["status"] == "no_op"


def test_publisher_rechecks_after_validation_before_remote_fetch(tmp_path, monkeypatch):
    environ = {"KNOWLEDGE_LEARNING_PUBLICATION": "draft_pr",
               "KNOWLEDGE_LEARNING_REPOSITORY": "owner/knowledge",
               "ALLOW_PUSH": "1", "ALLOW_POST": "1"}
    agreement = accept(tmp_path / "state", environ=environ)
    workspace = tmp_path / "checkout"
    document = "knowledge/repos/r/rules.md"
    page = workspace / document
    page.parent.mkdir(parents=True)
    page.write_text(CONTENT)
    head, base = "a" * 40, "b" * 40
    calls = []

    def command(command, _workspace):
        calls.append(command)
        if command[1:3] == ["check-ref-format", "--branch"]:
            return "main"
        if command[1:] == ["rev-parse", "HEAD"]:
            return head
        if command[1:] == ["rev-parse", "HEAD^"]:
            return base
        if command[1] == "status":
            return ""
        if command[1] == "diff":
            agreement.revoke()
            return document
        raise AssertionError("revoked publication must not perform remote commands")

    monkeypatch.setattr("infermatrix_copilot.knowledge_service.learning_publication._run", command)
    with pytest.raises(AgreementRequiredError):
        LearningPublisher(environ, state_dir=tmp_path / "state").publish(
            workspace, task_digest="c" * 64, head=head, base_sha=base,
            packet_digest="d" * 64, rules=1, outcomes={},
            documents={document: hashlib.sha256(CONTENT.encode()).hexdigest()})
    assert not any(command[1] == "fetch" for command in calls)


def test_changed_publication_destination_blocks_prepared_task(tmp_path, monkeypatch):
    state = tmp_path / "state"
    accept(state)
    service = TaskLearningService(state, tmp_path / "no-checkout")
    packet = service.prepare("empty", "r", "a" * 40, [])
    monkeypatch.setenv("KNOWLEDGE_LEARNING_PUBLICATION", "draft_pr")
    monkeypatch.setenv("KNOWLEDGE_LEARNING_REPOSITORY", "new-owner/knowledge")
    with pytest.raises(AgreementRequiredError):
        service.submit("empty", packet["packet_digest"], {"rules": []})
    assert service.status("empty")["status"] == "no_op"


def test_direct_transport_helper_still_blocks_secret_egress(tmp_path, monkeypatch):
    state = tmp_path / "state"
    url = "https://collector.example"
    accept(state, feedback_url=url)

    def never_network(*_args, **_kwargs):
        raise AssertionError("private outgoing material must not reach a network connection")

    monkeypatch.setattr("infermatrix_copilot.knowledge_service.feedback_sync.build_opener", never_network)
    client = FeedbackSyncClient(FeedbackSyncConfig(url, "x" * 32), state_dir=state)
    with pytest.raises(FeedbackSyncError, match="privacy_blocked"):
        client._request("/v1/feedback/events", {"evidence": "ghp_" + "s" * 30})


def test_collector_428_preserves_pending_event_and_reports_agreement_required(world, monkeypatch):
    original, view = world
    config = FeedbackSyncConfig("https://collector.example", "x" * 32)
    accept(original.state_dir, feedback_url=config.url)
    store = KnowledgeFeedbackStore(original.state_dir, sync_config=config)
    receipt = issue((store, view))
    private_error = "private-collector-response"

    class RejectedByCollector:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 428, private_error, {}, io.BytesIO(private_error.encode()))

    monkeypatch.setattr("infermatrix_copilot.knowledge_service.feedback_sync.build_opener",
                        lambda *_args: RejectedByCollector())
    result = record(store, receipt)
    assert result["sync"]["status"] == "agreement_required"
    assert result["sync"]["pending_count"] == 1
    assert store.report()["total_ratings"] == 1
    assert private_error not in json.dumps(result)
