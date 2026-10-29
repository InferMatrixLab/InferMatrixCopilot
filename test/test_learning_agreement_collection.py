"""The collector requires a current provider attestation before persistence."""
from __future__ import annotations

import copy
import http.client
import json
import sqlite3
import threading

import pytest

from infermatrix_copilot.knowledge_service.agreement import (
    AgreementRequiredError,
    AgreementStore,
)
from infermatrix_copilot.knowledge_service.feedback import (
    KnowledgeFeedbackStore,
    _digest,
)
from infermatrix_copilot.knowledge_service.feedback_http import create_server
from infermatrix_copilot.knowledge_service.feedback_hub import FeedbackHubStore
from infermatrix_copilot.knowledge_view import KnowledgeView

TOKEN = "test-provider-" + "x" * 40
PAGE = "repos/r/rules.md"
CONTENT = "# Rules\n\n## SOURCE-1a — Preserve contracts\n\nKeep the executable source contract.\n"
PRIVATE_SIGNER = "Synthetic Private Collection Test User"


@pytest.fixture
def provider_event(tmp_path):
    provider = KnowledgeFeedbackStore(tmp_path / "provider")
    agreement = AgreementStore.from_env(state_dir=provider.state_dir)
    document = agreement.document()
    agreement._accept(PRIVATE_SIGNER, version=document["version"], agreement_sha256=document["sha256"],
                      context_sha256=document["context_sha256"], confirmation="I AGREE")
    root = tmp_path / "knowledge"
    (root / PAGE).parent.mkdir(parents=True)
    (root / PAGE).write_text(CONTENT)
    receipt = provider.issue_delivery(task_id="globally-unique-task-1", repository="r",
                                      view=KnowledgeView(root, "snapshot-1"),
                                      documents=[{"path": PAGE, "content": CONTENT}])
    rating = {"target_id": receipt["targets"][0]["target_id"], "score": 1, "used": True,
              "reason": "The rule guided the verified fix.", "evidence": ["Focused contract test passed."]}
    return {"receipt": receipt, "ratings": [rating], "agreement": agreement.attestation()}


@pytest.fixture
def collector(tmp_path):
    hub = FeedbackHubStore(tmp_path / "collector")
    server = create_server(hub, TOKEN)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield hub, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def post(server, event):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.request("POST", "/v1/feedback/events", json.dumps(event).encode(),
                           {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def invalid_event(event, kind):
    event = copy.deepcopy(event)
    if kind == "missing":
        event.pop("agreement")
    elif kind == "stale_version":
        event["agreement"]["version"] = "old-disclosure-version"
    elif kind == "stale_text":
        event["agreement"]["agreement_sha256"] = "0" * 64
    elif kind == "malformed_context":
        event["agreement"]["context_sha256"] = "invalid"
    elif kind == "wrong_purpose":
        event["agreement"]["purpose"] = "arbitrary-data-upload"
    elif kind == "future_timestamp":
        event["agreement"]["accepted_at"] = "2999-01-01T00:00:00Z"
    elif kind == "signer_included":
        event["agreement"]["signer"] = PRIVATE_SIGNER
    elif kind == "malformed_identity":
        event["agreement"]["acceptance_id"] = True
    elif kind == "not_an_attestation":
        event["agreement"] = True
    else:
        raise AssertionError("unknown test case")
    return event


INVALID = ("missing", "stale_version", "stale_text", "malformed_context", "wrong_purpose",
           "future_timestamp", "signer_included", "malformed_identity", "not_an_attestation")


@pytest.mark.parametrize("kind", INVALID)
def test_hub_rejects_missing_stale_or_malformed_attestation_before_database(provider_event, tmp_path, kind):
    event = invalid_event(provider_event, kind)
    hub = FeedbackHubStore(tmp_path / "hub")
    with pytest.raises(AgreementRequiredError) as error:
        hub.ingest(receipt=event["receipt"], ratings=event["ratings"], agreement=event.get("agreement"))
    assert error.value.code == "agreement_required"
    assert PRIVATE_SIGNER not in str(error.value)
    assert not hub.state_dir.exists()


@pytest.mark.parametrize("kind", INVALID)
def test_http_rejects_missing_stale_or_malformed_attestation_without_storage(provider_event, collector, capsys, kind):
    hub, server = collector
    status, response = post(server, invalid_event(provider_event, kind))
    assert status == 428
    assert response["error"]["code"] == "agreement_required"
    assert PRIVATE_SIGNER not in json.dumps(response)
    assert not hub.state_dir.exists()
    captured = capsys.readouterr()
    assert PRIVATE_SIGNER not in captured.out + captured.err
    assert TOKEN not in captured.out + captured.err


def test_current_attestation_deduplicates_and_audits_without_signer(provider_event, collector):
    hub, server = collector
    # The collector operator has no local user acceptance; provider attestation
    # is what authorizes this authenticated ingestion boundary.
    assert AgreementStore.from_env(state_dir=hub.state_dir).status()["status"] == "required"
    status, result = post(server, provider_event)
    assert status == 200
    assert post(server, provider_event) == (status, result)
    receipt = provider_event["receipt"]
    rating = provider_event["ratings"][0]
    assert result["ratings"][0]["feedback_id"] == _digest([receipt["task_id"], rating["target_id"]])
    assert hub.report()["total_ratings"] == 1
    with sqlite3.connect(hub.path) as db:
        rows = db.execute("SELECT acceptance_id,attestation FROM feedback_agreements").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == provider_event["agreement"]["acceptance_id"]
        assert json.loads(rows[0][1]) == provider_event["agreement"]
        assert db.execute("SELECT COUNT(*) FROM feedback_delivery_agreements").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM feedback_outbox").fetchone()[0] == 0
    assert PRIVATE_SIGNER.encode() not in hub.path.read_bytes()
    assert "signer" not in json.dumps(result)
    assert not (hub.state_dir / "agreement").exists()
