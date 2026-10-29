import copy
import http.client
import json
import socket
import threading

import pytest

from infermatrix_copilot.knowledge_service.agreement import AgreementStore
from infermatrix_copilot.knowledge_service.feedback import KnowledgeFeedbackStore
from infermatrix_copilot.knowledge_service.feedback_http import (
    MAX_BODY_BYTES,
    create_server,
)
from infermatrix_copilot.knowledge_service.feedback_hub import FeedbackHubStore
from infermatrix_copilot.knowledge_service.feedback_server_cli import main
from infermatrix_copilot.knowledge_view import KnowledgeView

pytestmark = pytest.mark.usefixtures("accepted_learning_feature")

TOKEN = "provider-a-" + "a" * 40
ROTATED_TOKEN = "provider-b-" + "b" * 40
PAGE = "# Rules\n\n## FIRST-1a — Preserve contracts\n\nKeep the source contract intact when modifying the call.\n"
DOCUMENT = "repos/r/rules.md"


@pytest.fixture
def running(tmp_path):
    root = tmp_path / "knowledge"
    (root / "repos/r").mkdir(parents=True)
    (root / DOCUMENT).write_text(PAGE)
    provider = KnowledgeFeedbackStore(tmp_path / "provider")
    receipt = provider.issue_delivery(task_id="host-a:task-1", repository="r", view=KnowledgeView(root, "snapshot-1"),
                                      documents=[{"path": DOCUMENT, "content": PAGE}])
    hub = FeedbackHubStore(tmp_path / "hub")
    server = create_server(hub, token=(TOKEN, ROTATED_TOKEN))
    server.test_agreement = AgreementStore.from_env(state_dir=provider.state_dir).attestation()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server, hub, receipt, provider, root
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def vote(target, **kwargs):
    return {"target_id": target["target_id"], "score": 1, "used": True,
            "reason": "The rule guided the verified fix.", "evidence": ["Focused source contract test passed."], **kwargs}


def request(server, method="GET", path="/v1/feedback/report", payload=None, token=TOKEN, headers=None):
    supplied = {"Authorization": "Bearer " + token} if token else {}
    if payload is not None:
        supplied["Content-Type"] = "application/json"
    if isinstance(payload, dict) and {"receipt", "ratings"} <= set(payload):
        payload = {"agreement": server.test_agreement, **payload}
    supplied.update(headers or {})
    body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.request(method, path, body=body, headers=supplied)
        response = connection.getresponse()
        body = response.read()
        return response.status, json.loads(body) if body else None, dict(response.getheaders())
    finally:
        connection.close()


def raw_request(server, body):
    with socket.create_connection(server.server_address, timeout=3) as connection:
        connection.sendall(body)
        connection.shutdown(socket.SHUT_WR)
        chunks = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
    header, body = b"".join(chunks).split(b"\r\n\r\n", 1)
    return int(header.split(b" ")[1]), json.loads(body)


def test_authenticated_shared_delivery_vote_report_and_global_rotation_dedup(running):
    server, hub, receipt, provider, root = running
    target = receipt["targets"][0]
    event = {"receipt": receipt, "ratings": [vote(target)]}
    status, result, headers = request(server, "POST", "/v1/feedback/events", event)
    assert status == 200 and set(result) == {"task_id", "delivery_id", "ratings"}
    assert headers["Cache-Control"] == "no-store"
    assert request(server, "POST", "/v1/feedback/events", event, token=ROTATED_TOKEN)[1] == result
    another = provider.issue_delivery(task_id=receipt["task_id"], repository="r", view=KnowledgeView(root, "snapshot-1"),
                                       documents=[{"path": DOCUMENT, "content": "Wrapper\n" + PAGE}])
    assert request(server, "POST", "/v1/feedback/events", {"receipt": another, "ratings": [vote(target)]}, token=ROTATED_TOKEN)[0] == 200
    status, report, _ = request(server, path="/v1/feedback/report?repo=r&limit=1")
    assert status == 200 and report["total_ratings"] == 1
    assert report == hub.report(repository="r", limit=1)


def test_conflict_is_409_and_new_votes_in_same_batch_are_rolled_back(running):
    server, hub, receipt, *_ = running
    first, second = receipt["targets"][:2]
    assert request(server, "POST", "/v1/feedback/events", {"receipt": receipt, "ratings": [vote(first)]})[0] == 200
    status, response, _ = request(server, "POST", "/v1/feedback/events", {
        "receipt": receipt, "ratings": [vote(second), vote(first, score=-1)]})
    assert status == 409 and response["error"]["code"] == "feedback_conflict"
    assert hub.report()["total_ratings"] == 1


def test_authentication_precedes_body_read_and_parse_and_health_has_no_data(running, capsys):
    server, hub, *_ = running
    status, _, headers = request(server, "POST", "/v1/feedback/events", b"not-json", token="private-invalid-token")
    assert status == 401 and headers["WWW-Authenticate"].startswith("Bearer")
    # A client that never sends the declared large body must still get 401 now.
    status, _ = raw_request(server, b"POST /v1/feedback/events HTTP/1.1\r\nHost: localhost\r\nContent-Length: 999999\r\n\r\n")
    assert status == 401
    assert request(server, token="")[0] == 401
    assert request(server, path="/healthz", token="")[1] == {"ok": True}
    assert not hub.path.exists()
    assert "private-invalid-token" not in capsys.readouterr().err


@pytest.mark.parametrize("body", [b'{"receipt":null,"receipt":{},"ratings":[]}', b'{"receipt":NaN,"ratings":[]}', b'[]', b'\xff', b'{'])
def test_malformed_json_is_rejected_without_storage(running, body):
    server, hub, *_ = running
    assert request(server, "POST", "/v1/feedback/events", body, headers={"Content-Type": "application/json"})[0] == 400
    assert not hub.path.exists()


def test_transport_bounds_and_framing_are_enforced_before_ingest(running):
    server, hub, *_ = running
    prefix = b"POST /v1/feedback/events HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\nContent-Type: application/json\r\n"
    cases = [
        (b"Content-Length: " + str(MAX_BODY_BYTES + 1).encode() + b"\r\n\r\n", 413),
        (b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}", 400),
        (b"Content-Length: -1\r\n\r\n", 400),
        (b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n", 400),
        (b"\r\n", 411),
        (b"Content-Length: 100\r\n\r\n{}", 400),
    ]
    for suffix, expected in cases:
        assert raw_request(server, prefix + suffix)[0] == expected
    assert request(server, "POST", "/v1/feedback/events", b"{}", headers={"Content-Type": "text/plain"})[0] == 415
    assert not hub.path.exists()


def test_tampered_receipt_strict_rating_schema_and_private_evidence_are_400(running):
    server, hub, receipt, *_ = running
    target = receipt["targets"][0]
    tampered = copy.deepcopy(receipt)
    tampered["task_id"] = "another-task"
    cases = [
        {"receipt": tampered, "ratings": [vote(target)]},
        {"receipt": receipt, "ratings": [vote(target, score=True)]},
        {"receipt": receipt, "ratings": [vote(target, transcript="raw")]},
        {"receipt": receipt, "ratings": [vote(target, evidence="ghp_" + "a" * 30)]},
        {"receipt": receipt, "ratings": [vote(target)], "workspace": "/private/path"},
    ]
    for event in cases:
        status, body, _ = request(server, "POST", "/v1/feedback/events", event)
        assert status == 400
        assert "ghp_" not in json.dumps(body)
    assert hub.report()["total_ratings"] == 0


@pytest.mark.parametrize("path", ["/v1/feedback/report?limit=0", "/v1/feedback/report?limit=true", "/v1/feedback/report?repo=a&repo=b", "/v1/feedback/report?unknown=x"])
def test_report_query_validation(running, path):
    assert request(running[0], path=path)[0] == 400


def test_unknown_routes_cannot_register_receipts_and_no_redirects(running):
    server, hub, receipt, *_ = running
    assert request(server, "POST", "/v1/feedback/deliveries", {"receipt": receipt})[0] == 404
    assert request(server, path="/v1/feedback/report/")[0] == 404
    assert request(server, "POST", "/v1/feedback/events?next=https://example.test", {"receipt": receipt, "ratings": []})[0] == 400
    assert not hub.path.exists()


def test_safe_server_configuration_and_cli_startup_failure(tmp_path, monkeypatch, capsys):
    store = FeedbackHubStore(tmp_path / "hub")
    for kwargs in ({"token": "short"}, {"token": TOKEN, "host": "0.0.0.0"},
                   {"token": TOKEN, "public_url": "http://example.test"},
                   {"token": TOKEN, "public_url": "https://user:secret@example.test"}):
        with pytest.raises(ValueError):
            create_server(store, **kwargs)
    server = create_server(store, TOKEN, host="0.0.0.0", public_url="https://feedback.example.test")
    server.server_close()
    monkeypatch.setenv("KNOWLEDGE_FEEDBACK_SERVER_TOKEN", "private-short-token")
    assert main(["--state-dir", str(tmp_path / "cli")]) == 2
    output = capsys.readouterr()
    assert "startup_failed" in output.err and "private-short-token" not in output.err
    assert not (tmp_path / "cli").exists()
