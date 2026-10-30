import copy
import hashlib
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("accepted_learning_feature")

from infermatrix_copilot.knowledge_service.feedback import (
    FeedbackError,
    KnowledgeFeedbackStore,
)
from infermatrix_copilot.knowledge_service.feedback_sync import (
    MAX_MESSAGE_BYTES,
    FeedbackSyncConfig,
    FeedbackSyncError,
)
from infermatrix_copilot.knowledge_view import KnowledgeView

PAGE = "# Rules\n\n## SOURCE-1a — Preserve the source\n\nKeep the executable source contract.\n"
DOCUMENT = "repos/r/rules.md"
TOKEN = "test-provider-token-" + "z" * 32


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


@pytest.fixture
def collector():
    state = {"mode": "ok", "events": [], "unique": {}, "headers": [], "on_post": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_json(self, value, status=200):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            state["headers"].append(dict(self.headers))
            event = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["events"].append(event)
            if state["on_post"]:
                state["on_post"]()
            if state["mode"] == "fail":
                self.send_json({"error": TOKEN}, status=503)
                return
            if state["mode"] in ("conflict", "unauthorized"):
                self.send_json({"error": TOKEN}, status=409 if state["mode"] == "conflict" else 401)
                return
            if state["mode"] == "redirect":
                self.send_response(307)
                self.send_header("Location", state["url"] + "/stolen")
                self.end_headers()
                return
            receipt = event["receipt"]
            votes = []
            for rating in event["ratings"]:
                target = next(t for t in receipt["targets"] if t["target_id"] == rating["target_id"])
                key = (receipt["task_id"], rating["target_id"])
                previous = state["unique"].get(key)
                vote = {**rating, "feedback_id": digest(list(key)), "target": target}
                if previous is not None:
                    vote["target"] = previous["target"]
                state["unique"][key] = copy.deepcopy(vote)
                votes.append(vote)
            response = {"task_id": receipt["task_id"], "delivery_id": receipt["delivery_id"],
                        "ratings": votes}
            if state["mode"] == "wrong_ack":
                response["ratings"][0]["score"] *= -1
            elif state["mode"] == "wrong_version":
                response["ratings"][0]["target"]["content_sha256"] = "f" * 64
            elif state["mode"] == "other_provenance":
                response["ratings"][0]["target"]["snapshot"] = "first-provider-snapshot"
                response["ratings"][0]["target"]["page_sha256"] = "f" * 64
            self.send_json(response)

        def do_GET(self):
            state["headers"].append(dict(self.headers))
            if state["mode"] == "fail":
                self.send_json({"error": TOKEN}, status=503)
            elif state["mode"] == "large":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(MAX_MESSAGE_BYTES + 1))
                self.end_headers()
            elif state["mode"] == "wrong_report":
                self.send_json({"error": TOKEN})
            elif state["mode"] == "malformed_target":
                self.send_json({"protocol": "knowledge-feedback/1", "repository": "r",
                                "total_targets": 1, "total_ratings": 1,
                                "targets": [{}], "truncated": False})
            else:
                self.send_json({"protocol": "knowledge-feedback/1", "repository": "r",
                                "total_targets": 0, "total_ratings": 0,
                                "targets": [], "truncated": False})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def world(tmp_path, collector):
    root = tmp_path / "knowledge"
    (root / "repos/r").mkdir(parents=True)
    (root / DOCUMENT).write_text(PAGE)
    config = FeedbackSyncConfig(collector["url"], TOKEN, 1)
    return KnowledgeFeedbackStore(tmp_path / "state", sync_config=config), KnowledgeView(root, "snapshot-1")


def issue(world, task="global-task-1", content=PAGE):
    store, view = world
    return store.issue_delivery(task_id=task, repository="r", view=view,
                                documents=[{"path": DOCUMENT, "content": content}])


def vote(receipt, **overrides):
    target = next(t for t in receipt["targets"] if t["kind"] == "rule")
    return {"target_id": target["target_id"], "score": 1, "used": True,
            "reason": "The rule guided a successful fix.",
            "evidence": ["Focused source contract test passed."], **overrides}


def record(world, receipt=None, **overrides):
    receipt = receipt or issue(world)
    return world[0].record(task_id=receipt["task_id"], delivery_id=receipt["delivery_id"],
                           ratings=[vote(receipt, **overrides)])


def test_auto_upload_reuses_identity_and_does_not_store_token(world, collector):
    receipt = issue(world)
    first = record(world, receipt)
    assert first["sync"]["status"] == "acknowledged"
    assert first["sync"]["pending_count"] == 0
    assert collector["events"] == [{"receipt": receipt, "ratings": [vote(receipt)],
                                      "agreement": world[0]._agreement().attestation()}]
    assert collector["headers"][0]["Authorization"] == "Bearer " + TOKEN
    assert record(world, receipt)["ratings"] == first["ratings"]
    assert len(collector["events"]) == 1
    assert TOKEN not in repr(world[0].sync_config)
    assert TOKEN.encode() not in world[0].path.read_bytes()
    assert world[0].report()["total_ratings"] == 1


def test_network_outage_persists_then_restart_replays_without_double_count(world, collector):
    collector["mode"] = "fail"
    result = record(world)
    assert result["sync"]["status"] == "pending"
    assert result["sync"]["last_error"] == "collector_unavailable"
    assert TOKEN not in json.dumps(result)
    assert world[0].report()["total_ratings"] == 1
    restarted = KnowledgeFeedbackStore(world[0].state_dir, sync_config=world[0].sync_config)
    collector["mode"] = "ok"
    assert restarted.sync_pending()["status"] == "acknowledged"
    assert len(collector["unique"]) == 1
    assert restarted.sync_pending()["attempted_count"] == 0


@pytest.mark.parametrize("mode", ["wrong_ack", "wrong_version", "redirect"])
def test_wrong_ack_and_redirect_never_claim_synced(world, collector, mode):
    collector["mode"] = mode
    result = record(world)
    assert result["sync"]["status"] == "pending"
    assert result["sync"]["acknowledged_count"] == 0
    assert len(collector["events"]) == 1
    collector["mode"] = "ok"
    assert world[0].sync_pending()["status"] == "acknowledged"
    assert len(collector["unique"]) == 1


def test_global_duplicate_accepts_first_receipt_provenance(world, collector):
    collector["mode"] = "other_provenance"
    assert record(world)["sync"]["status"] == "acknowledged"


def test_new_receipt_retry_keeps_original_immutable_delivery(world, collector):
    first = issue(world)
    collector["mode"] = "fail"
    record(world, first)
    second = issue(world, content="Wrapper\n" + PAGE)
    collector["mode"] = "ok"
    result = record(world, second)
    assert result["sync"]["status"] == "acknowledged"
    assert collector["events"][-1]["receipt"]["delivery_id"] == first["delivery_id"]
    assert result["delivery_id"] == second["delivery_id"]


def test_pre_sync_local_ratings_backfill_and_disabled_record_survive(world, collector, accept_learning_agreement):
    store, view = world
    local = KnowledgeFeedbackStore(store.state_dir)
    local_world = local, view
    receipt = issue(local_world)
    assert record(local_world, receipt)["sync"]["status"] == "disabled"
    # Simulate a database created before the outbox table existed.
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE feedback_outbox")
        db.execute("DROP TABLE feedback_sync_meta")
    accept_learning_agreement(store.state_dir, feedback_url=store.sync_config.url)
    assert store.sync_pending()["status"] == "acknowledged"
    assert len(collector["unique"]) == 1
    assert store.sync_pending()["attempted_count"] == 0


def test_outbox_commit_is_atomic_and_network_holds_no_local_transaction(world, collector):
    store, _ = world

    def inspect():
        with sqlite3.connect(store.path, timeout=0.2) as db:
            db.execute("BEGIN IMMEDIATE")
            assert db.execute("SELECT COUNT(*) FROM feedback_outbox").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM feedback_ratings").fetchone()[0] == 1
    collector["on_post"] = inspect
    assert record(world)["sync"]["status"] == "acknowledged"


def test_conflicting_feedback_is_not_queued_or_sent(world, collector):
    receipt = issue(world)
    record(world, receipt)
    with pytest.raises(FeedbackError, match="immutable"):
        record(world, receipt, score=-1)
    assert len(collector["events"]) == 1
    assert world[0].sync_status()["acknowledged_count"] == 1


def test_secret_shaped_evidence_stays_local_and_pending(world, collector):
    result = record(world, evidence=["ghp_" + "a" * 30])
    assert result["sync"]["status"] == "pending"
    assert result["sync"]["last_error"] == "privacy_blocked"
    assert collector["events"] == []
    assert KnowledgeFeedbackStore(world[0].state_dir,
                                  sync_config=world[0].sync_config).sync_pending()["pending_count"] == 1
    assert world[0].report()["total_ratings"] == 1


@pytest.mark.parametrize("mode,code", [("conflict", "feedback_conflict"),
                                      ("unauthorized", "authentication_failed")])
def test_safe_permanent_failure_codes_do_not_echo_response(world, collector, mode, code):
    collector["mode"] = mode
    result = record(world)
    assert result["sync"]["status"] == "pending"
    assert result["sync"]["last_error"] == code
    assert TOKEN not in json.dumps(result)


def test_report_auto_scope_does_not_silently_return_local_on_failure(world, collector):
    store, _ = world
    record(world)
    assert store.report_for_scope(repository="r")["scope"] == "central"
    assert store.report_for_scope(repository="r", scope="local")["total_ratings"] == 1
    for mode in ("fail", "large", "wrong_report", "malformed_target"):
        collector["mode"] = mode
        with pytest.raises(FeedbackError, match="unavailable") as exc:
            store.report_for_scope(repository="r")
        assert TOKEN not in str(exc.value)
    assert KnowledgeFeedbackStore(store.state_dir).report_for_scope(repository="r")["scope"] == "local"


def test_concurrent_flushes_global_dedup(world, collector):
    collector["mode"] = "fail"
    record(world)
    collector["mode"] = "ok"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: world[0].sync_pending(), range(4)))
    assert len(collector["unique"]) == 1
    assert world[0].sync_status()["acknowledged_count"] == 1


def test_configuration_from_env_keeps_invalid_destination_local(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_LEARNING_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KNOWLEDGE_FEEDBACK_URL", "http://remote.invalid")
    monkeypatch.setenv("KNOWLEDGE_FEEDBACK_TOKEN", TOKEN)
    store = KnowledgeFeedbackStore.from_env()
    assert store.sync_status()["status"] == "agreement_required"
    from infermatrix_copilot.knowledge_service.agreement import AgreementRequiredError

    with pytest.raises(AgreementRequiredError):
        store.report_for_scope()
    assert store.report_for_scope(scope="local")["total_ratings"] == 0


@pytest.mark.parametrize("url", [
    "http://example.org", "https://user:password@example.org", "https://example.org?token=x",
    "https://example.org#x", "https://example.org?", "https://example.org#",
    "ftp://127.0.0.1", "https://example.org:0", "https://example.org:65536",
    "https://example.org\\@evil.invalid", "https://example.org/ bad",
])
def test_unsafe_urls_rejected_without_echo(url):
    with pytest.raises(FeedbackSyncError) as exc:
        FeedbackSyncConfig(url, TOKEN)
    assert url not in str(exc.value)
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize("token", ["short", "x" * 31, "x" * 32 + "\n", "x" * 32 + " "])
def test_invalid_tokens_rejected(token):
    with pytest.raises(FeedbackSyncError):
        FeedbackSyncConfig("https://collector.example", token)


@pytest.mark.parametrize("timeout", [0, -1, True, 31, "nan", "inf", "bad"])
def test_invalid_timeouts_rejected(timeout):
    with pytest.raises(FeedbackSyncError):
        FeedbackSyncConfig("https://collector.example", TOKEN, timeout)


def test_valid_loopback_and_https_configuration():
    for url in ("http://127.0.0.1:8888/", "http://[::1]:8888", "http://localhost:8888",
                "https://collector.example/feedback/"):
        assert FeedbackSyncConfig(url, TOKEN).url == url.rstrip("/")


def test_configured_endpoint_change_replays_to_new_aggregate(world, collector):
    record(world)
    previous = world[0].sync_config
    # The new service can share an origin behind a distinct configured prefix.
    next_config = FeedbackSyncConfig(previous.url + "/new-service", TOKEN)
    next_store = KnowledgeFeedbackStore(world[0].state_dir, sync_config=next_config)
    assert next_store.sync_status()["pending_count"] == 1
    assert next_store.sync_pending()["status"] == "acknowledged"
    assert len(collector["events"]) == 2


def test_existing_public_state_directory_never_exposes_database_sidecars(tmp_path):
    directory = tmp_path / "existing-state"
    directory.mkdir(mode=0o755)
    store = KnowledgeFeedbackStore(directory)
    with store._db() as db:
        db.execute("INSERT INTO feedback_sync_meta VALUES ('permission-test','1')")
        for path in (store.path, Path(str(store.path) + "-wal"), Path(str(store.path) + "-shm")):
            assert path.exists()
            assert path.stat().st_mode & 0o077 == 0


def test_explicit_state_directory_retains_transport_from_environment(monkeypatch, collector, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_LEARNING_STATE_DIR", str(tmp_path / "default"))
    monkeypatch.setenv("KNOWLEDGE_FEEDBACK_URL", collector["url"])
    monkeypatch.setenv("KNOWLEDGE_FEEDBACK_TOKEN", TOKEN)
    store = KnowledgeFeedbackStore.from_env(state_dir=tmp_path / "override")
    assert store.state_dir == tmp_path / "override"
    assert store.sync_config.url == collector["url"]
    assert store.sync_config.token == TOKEN


def test_database_supports_platform_without_fchmod_or_nofollow(monkeypatch, tmp_path):
    monkeypatch.delattr(os, "fchmod")
    monkeypatch.delattr(os, "O_NOFOLLOW")
    store = KnowledgeFeedbackStore(tmp_path / "state")
    assert store.report()["total_ratings"] == 0


def test_failed_concurrent_flush_cannot_revoke_successful_ack(world, collector, monkeypatch):
    from infermatrix_copilot.knowledge_service.feedback_sync import FeedbackSyncClient

    collector["mode"] = "fail"
    record(world)
    second_started, success_finished = threading.Event(), threading.Event()
    original = FeedbackSyncClient.send

    def send(client, **kwargs):
        if threading.current_thread().name == "failed-upload":
            second_started.set()
            assert success_finished.wait(5)
            raise FeedbackSyncError("collector request failed")
        assert second_started.wait(5)
        return original(client, **kwargs)

    monkeypatch.setattr(FeedbackSyncClient, "send", send)
    collector["mode"] = "ok"
    errors = []

    def failed_upload():
        try:
            world[0].sync_pending()
        except (OSError, sqlite3.Error, ValueError, AssertionError) as exc:
            errors.append(exc)

    thread = threading.Thread(target=failed_upload, name="failed-upload")
    thread.start()
    try:
        assert second_started.wait(5)
        assert world[0].sync_pending()["status"] == "acknowledged"
    finally:
        success_finished.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    assert world[0].sync_status()["status"] == "acknowledged"
    assert "last_error" not in world[0].sync_status()
