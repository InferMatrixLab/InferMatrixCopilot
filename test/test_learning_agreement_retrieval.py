"""Source retrieval stays available across optional learning consent changes."""

from __future__ import annotations

import pytest
import test_feedback_recovery as helpers

from infermatrix_copilot.knowledge_service.agreement import AgreementStore
from infermatrix_copilot.knowledge_view import KnowledgeView, build_manifest
from infermatrix_copilot.sdk.v1 import DirectClient


@pytest.fixture
def retrieval(tmp_path, monkeypatch):
    return helpers.recovery.__wrapped__(tmp_path, monkeypatch)


def test_unsigned_general_source_read_does_not_require_feedback_repository(
    retrieval, monkeypatch,
):
    _factory, _state_path, view = retrieval
    (view.root / "general").mkdir()
    (view.root / "general/rules.md").write_text(helpers.PAGE)
    manifest = build_manifest(view.root, "general-source")
    active = KnowledgeView(view.root, "general-source", manifest["files"])
    monkeypatch.setattr(KnowledgeView, "current", classmethod(lambda cls: active))
    result = DirectClient().read_document("general/rules.md", task_id="ordinary-read")
    assert "Preserve the input ownership" in result.content
    assert result.knowledge_feedback["status"] == "agreement_required"
    assert not AgreementStore.from_env().state_dir.exists()


def test_cached_source_receipt_tracks_signature_and_revocation_without_extra_tokens(
    retrieval, accept_learning_agreement,
):
    factory, _state_path, _view = retrieval
    service = factory()
    session = helpers.read_session(service)
    unsigned = service.read(session, helpers.DOCUMENT)
    assert unsigned["knowledge_feedback"]["status"] == "agreement_required"
    assert not AgreementStore.from_env().state_dir.exists()
    before = service.status(session)

    agreement = accept_learning_agreement()
    signed = factory().read(session, helpers.DOCUMENT)
    assert signed["knowledge_feedback"]["targets"]
    assert helpers.without_feedback(signed) == helpers.without_feedback(unsigned)
    assert factory().status(session) == before
    assert factory().read(session, helpers.DOCUMENT) == signed

    agreement.revoke()
    revoked = factory().read(session, helpers.DOCUMENT)
    assert revoked["knowledge_feedback"]["status"] == "agreement_required"
    assert helpers.without_feedback(revoked) == helpers.without_feedback(unsigned)
    assert factory().status(session) == before
    assert factory().read(session, helpers.DOCUMENT) == revoked

    accept_learning_agreement()
    renewed = factory().read(session, helpers.DOCUMENT)
    assert renewed == signed  # the same delivered bytes keep their existing identity
    assert factory().status(session) == before


def test_invalid_optional_state_keeps_normal_source_available(retrieval, monkeypatch, tmp_path):
    _factory, _state_path, _view = retrieval
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    monkeypatch.setenv("KNOWLEDGE_LEARNING_STATE_DIR", str(loop))
    result = DirectClient().read_document(helpers.DOCUMENT, task_id="ordinary-read")
    assert "Preserve the input ownership" in result.content
    assert result.knowledge_feedback["status"] == "agreement_required"
