"""Storage recovery repairs feedback for the exact original adaptive delivery."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

pytestmark = pytest.mark.usefixtures("accepted_learning_feature")
import test_kb_adaptive_containment as containment_helpers
from test_thin_mcp_server import _fake_mcp

from infermatrix_copilot.knowledge_context import (
    ContextBudget,
    ContextError,
    KnowledgeContextService,
)
from infermatrix_copilot.knowledge_service.containment import (
    knowledge_availability_check,
    knowledge_usage_record,
)
from infermatrix_copilot.knowledge_service.feedback import (
    KnowledgeFeedbackStore,
    model_content_documents,
)
from infermatrix_copilot.knowledge_view import KnowledgeView, build_manifest
from infermatrix_copilot.sdk.v1 import DirectClient

PIN = "a" * 40
DOCUMENT = "repos/r/rules.md"
FIRST = (
    "## FIRST-1a — Preserve the input ownership contract\n\n"
    + "Compare the verified input ownership and preserve the successful source contract before publishing the result. " * 6
    + "\n\n"
)
SECOND = (
    "## SECOND-1a — Preserve the undisclosed sibling contract\n\n"
    "UNDISCLOSED_SIBLING requires a different independently verified downstream boundary.\n"
)
PAGE = "# Rules\n\n" + FIRST + SECOND
UNAVAILABLE = {"status": "unavailable", "error": "feedback_storage_unavailable"}


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    root = tmp_path / "snapshot" / "knowledge"
    (root / "repos/r").mkdir(parents=True)
    (root / DOCUMENT).write_text(PAGE)
    manifest = build_manifest(root, "recovery-snapshot")
    view = KnowledgeView(root, "recovery-snapshot", manifest["files"])
    binding = {"repo_id": "r", "knowledge_slice": "repos/r", "source_pin": PIN,
               "catalog_hash": "b" * 64, "policy_hash": "c" * 64}
    monkeypatch.setenv("KNOWLEDGE_MAINTENANCE_ENABLED", "0")
    monkeypatch.setattr(KnowledgeView, "current", classmethod(lambda cls: view))
    state_path = tmp_path / "context.sqlite"

    def service():
        return KnowledgeContextService(view, state_path, resolver=lambda _view, _repo: binding)

    return service, state_path, view


def unavailable(*_args, **_kwargs):
    raise sqlite3.OperationalError("private SQLite failure at /private/operator/path; bearer credential_value_123456789")


def read_session(service, *, initial_tokens=4000):
    return service.open_session("r", source_pin=PIN, review_id="feedback-recovery-task",
                                budget=ContextBudget(initial_tokens=initial_tokens))["session_id"]


def without_feedback(packet):
    return {key: value for key, value in packet.items() if key != "knowledge_feedback"}


@pytest.mark.parametrize("delivery", ["budget_partial", "complete_first_rule"])
def test_cached_feedback_recovers_exact_original_fragments_after_restart(recovery, monkeypatch, delivery):
    factory, _state_path, _view = recovery
    service = factory()
    session = read_session(service, initial_tokens=500 if delivery == "budget_partial" else 4000)
    parameters = {"limit": 65536 if delivery == "budget_partial" else len(("# Rules\n\n" + FIRST).encode())}
    issue_delivery = KnowledgeFeedbackStore.issue_delivery
    monkeypatch.setattr(KnowledgeFeedbackStore, "issue_delivery", unavailable)
    original = service.read(session, DOCUMENT, **parameters)
    assert original["knowledge_feedback"] == UNAVAILABLE
    original_status = service.status(session)
    fragments = model_content_documents(original["model_content"])
    assert fragments and "UNDISCLOSED_SIBLING" not in fragments[0]["content"]
    if delivery == "budget_partial":
        assert original["truncated"] and FIRST.strip() not in fragments[0]["content"]
    else:
        assert FIRST.strip() in fragments[0]["content"]

    monkeypatch.setattr(KnowledgeFeedbackStore, "issue_delivery", issue_delivery)
    restarted = factory()
    recovered = restarted.read(session, DOCUMENT, **parameters)
    assert without_feedback(recovered) == without_feedback(original)
    assert restarted.status(session) == original_status
    receipt = recovered["knowledge_feedback"]
    assert receipt["protocol"] == "knowledge-feedback/1"
    assert receipt["task_id"] == "feedback-recovery-task"
    assert receipt["repository"] == "r"
    assert receipt["documents"] == [{
        "path": DOCUMENT,
        "page_sha256": hashlib.sha256(PAGE.encode()).hexdigest(),
        "delivered_sha256": hashlib.sha256(fragments[0]["content"].encode()).hexdigest(),
        "delivered_chars": len(fragments[0]["content"]),
    }]
    assert {target.get("block_id") for target in receipt["targets"] if target["kind"] == "rule"} == (
        set() if delivery == "budget_partial" else {"FIRST-1a"})
    # Persistence must update the cached integrity digest, rather than only the
    # returned object: a second restart verifies and returns that exact packet.
    again = factory()
    assert again.read(session, DOCUMENT, **parameters) == recovered
    assert again.status(session) == original_status


def test_repeated_storage_failure_keeps_source_deliverable_and_error_private(recovery, monkeypatch):
    factory, _state_path, _view = recovery
    service = factory()
    session = read_session(service)
    monkeypatch.setattr(KnowledgeFeedbackStore, "issue_delivery", unavailable)
    first = service.read(session, DOCUMENT)
    cached = factory().read(session, DOCUMENT)
    assert cached == first
    assert cached["knowledge_feedback"] == UNAVAILABLE
    assert "Preserve the input ownership" in cached["model_content"]
    assert "/private/operator/path" not in json.dumps(cached)
    assert "credential_value_123456789" not in json.dumps(cached)
    assert factory().status(session)["consumed_tokens"] == first["consumed_tokens"]


def test_tampered_cached_content_is_rejected_before_feedback_reissue(recovery, monkeypatch):
    factory, state_path, _view = recovery
    service = factory()
    session = read_session(service)
    monkeypatch.setattr(KnowledgeFeedbackStore, "issue_delivery", unavailable)
    service.read(session, DOCUMENT)
    with sqlite3.connect(state_path) as db:
        state = json.loads(db.execute("SELECT state FROM sessions WHERE id=?", (session,)).fetchone()[0])
        next(iter(state["requests"].values()))["result"]["model_content"] += "Injected undisclosed sibling text."
        db.execute("UPDATE sessions SET state=? WHERE id=?", (json.dumps(state), session))
    attempts = []

    def forbidden_reissue(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("tampered cached context reached feedback issuance")

    monkeypatch.setattr(KnowledgeFeedbackStore, "issue_delivery", forbidden_reissue)
    with pytest.raises(ContextError, match="integrity"):
        factory().read(session, DOCUMENT)
    assert attempts == []


@pytest.mark.parametrize("surface", ["sdk", "mcp"])
def test_document_delivery_surfaces_hide_storage_exception_details(recovery, monkeypatch, surface):
    monkeypatch.setattr(KnowledgeFeedbackStore, "issue_delivery", unavailable)
    if surface == "sdk":
        result = DirectClient().read_document(DOCUMENT, task_id="safe-failure-task").to_dict()
    else:
        mcp, _ = _fake_mcp(monkeypatch)
        result = mcp.tools["doc_read"](path=DOCUMENT, repo="r", task_id="safe-failure-task")
    assert result["knowledge_feedback"] == UNAVAILABLE
    assert "Preserve the input ownership" in result["content"]
    assert "credential_value_123456789" not in json.dumps(result)
    assert "/private/operator/path" not in json.dumps(result)


@pytest.fixture
def contained(tmp_path, monkeypatch):
    return containment_helpers.adaptive.__wrapped__(tmp_path, monkeypatch)


def test_repaired_cached_packet_keeps_prior_containment_receipts_valid(contained, monkeypatch):
    fixture = contained

    def service():
        return KnowledgeContextService(fixture.view, fixture.context_path,
                                       knowledge_maintenance=fixture.cfg)

    initial = service()
    session = initial.open_session("r", source_pin=PIN, review_id="contained-feedback-recovery")["session_id"]
    issue_delivery = KnowledgeFeedbackStore.issue_delivery
    monkeypatch.setattr(KnowledgeFeedbackStore, "issue_delivery", unavailable)
    original = initial.read(session, containment_helpers.FOLLOWUP)
    assert original["knowledge_feedback"] == UNAVAILABLE
    old_receipt = knowledge_usage_record(original, injected=True, config=fixture.cfg)
    assert knowledge_availability_check(old_receipt, config=fixture.cfg)["allowed"]
    original_status = initial.status(session)

    monkeypatch.setattr(KnowledgeFeedbackStore, "issue_delivery", issue_delivery)
    restarted = service()
    recovered = restarted.read(session, containment_helpers.FOLLOWUP)
    assert recovered["knowledge_feedback"]["protocol"] == "knowledge-feedback/1"
    assert without_feedback(recovered) == without_feedback(original)
    assert restarted.status(session) == original_status
    updated_receipt = knowledge_usage_record(recovered, injected=True, config=fixture.cfg)
    assert updated_receipt["context_sha256"] != old_receipt["context_sha256"]
    assert updated_receipt["pages"] == old_receipt["pages"]
    assert updated_receipt["injected_units"] == old_receipt["injected_units"]
    assert updated_receipt["delivery_session"] == old_receipt["delivery_session"] == session
    assert knowledge_availability_check(old_receipt, config=fixture.cfg)["allowed"]
    assert knowledge_availability_check(updated_receipt, config=fixture.cfg)["allowed"]
    assert service().read(session, containment_helpers.FOLLOWUP) == recovered
    assert knowledge_usage_record(recovered, injected=True, config=fixture.cfg) == updated_receipt
    # A later hold still affects both the original and repaired packet. Recovery
    # cannot detach the delivery from the session's publication dependencies.
    containment_helpers.hold_followup(fixture)
    assert not knowledge_availability_check(old_receipt, config=fixture.cfg)["allowed"]
    assert not knowledge_availability_check(updated_receipt, config=fixture.cfg)["allowed"]
