"""Real HTTP collector exercised through provider, SDK, CLI and MCP entry points."""

from contextlib import contextmanager
from threading import Thread

import pytest

pytestmark = pytest.mark.usefixtures("accepted_learning_feature")

from infermatrix_copilot.knowledge_service.feedback import (
    FeedbackError,
    KnowledgeFeedbackStore,
)
from infermatrix_copilot.knowledge_service.feedback_http import create_server
from infermatrix_copilot.knowledge_service.feedback_hub import FeedbackHubStore
from infermatrix_copilot.knowledge_service.feedback_sync import (
    FeedbackSyncClient,
    FeedbackSyncConfig,
    FeedbackSyncError,
)
from infermatrix_copilot.knowledge_view import KnowledgeView
from infermatrix_copilot.sdk.v1 import DirectClient, KnowledgeFeedbackClient

TOKEN = "collector-integration-secret-1234567890"
RULE = "## CONTRACT-1a — Verify the result\n\nCheck the result before accepting the task outcome.\n"
PATH = "repos/demo/rules.md"


@contextmanager
def running_collector(state):
    store = FeedbackHubStore(state)
    server = create_server(store, TOKEN)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield store, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def knowledge(tmp_path, monkeypatch):
    root = tmp_path / "knowledge"
    (root / "repos/demo").mkdir(parents=True)
    (root / "repos/demo/_index.md").write_text("# Demo\n")
    (root / PATH).write_text("# Rules\n\n" + RULE)
    view = KnowledgeView(root, "integration-snapshot")
    monkeypatch.setenv("KNOWLEDGE_MAINTENANCE_ENABLED", "0")
    monkeypatch.setattr(KnowledgeView, "current", classmethod(lambda cls: view))
    return view


def issue(store, view, task):
    return store.issue_delivery(task_id=task, repository="demo", view=view,
                                documents=[{"path": PATH, "content": view.read_text(PATH)}])


def grade(receipt, score=1):
    target = next(row for row in receipt["targets"] if row["kind"] == "rule")
    return {"target_id": target["target_id"], "score": score, "used": True,
            "reason": "The result check determined the task outcome.",
            "evidence": "tests/test_contract.py::test_result passed"}


def test_two_hosts_retry_same_task_then_aggregate_new_tasks_and_versions(tmp_path, knowledge):
    with running_collector(tmp_path / "hub") as (hub, url):
        config = FeedbackSyncConfig(url, TOKEN)
        first = KnowledgeFeedbackStore(tmp_path / "host-a", sync_config=config)
        second = KnowledgeFeedbackStore(tmp_path / "host-b", sync_config=config)
        receipt = issue(first, knowledge, "chat-uuid-shared-task")
        first.record(task_id=receipt["task_id"], delivery_id=receipt["delivery_id"], ratings=[grade(receipt)])

        # Same unchanged rule, different page/snapshot provenance on another host.
        (knowledge.root / PATH).write_text("# Revised navigation\n\n" + RULE)
        newer_view = KnowledgeView(knowledge.root, "new-navigation-snapshot")
        duplicate = issue(second, newer_view, receipt["task_id"])
        assert duplicate["delivery_id"] != receipt["delivery_id"]
        assert grade(duplicate)["target_id"] == grade(receipt)["target_id"]
        result = second.record(task_id=duplicate["task_id"], delivery_id=duplicate["delivery_id"],
                               ratings=[grade(duplicate)])
        assert result["sync"]["status"] == "acknowledged"
        assert hub.report()["total_ratings"] == 1

        negative = issue(second, newer_view, "another-chat-uuid")
        second.record(task_id=negative["task_id"], delivery_id=negative["delivery_id"],
                      ratings=[grade(negative, -1)])
        report = first.report_for_scope(repository="demo")
        assert report["scope"] == "central"
        assert report["total_ratings"] == 2
        target = report["targets"][0]
        assert (target["positive_count"], target["negative_count"], target["distinct_tasks"]) == (1, 1, 2)
        assert target["net_score"] == target["mean_score"] == 0

        (knowledge.root / PATH).write_text("# Rules\n\n" + RULE.replace("Check the result", "Validate both results"))
        changed = issue(second, newer_view, "changed-version-task-uuid")
        assert grade(changed)["target_id"] != grade(receipt)["target_id"]
        second.record(task_id=changed["task_id"], delivery_id=changed["delivery_id"], ratings=[grade(changed)])
        assert hub.report()["total_targets"] == 2
        assert hub.report()["total_ratings"] == 3
        # The service itself never queues feedback back to another collector.
        with hub._db() as db:
            assert db.execute("SELECT COUNT(*) FROM feedback_outbox").fetchone()[0] == 0


def test_auth_failure_restart_and_lost_ack_do_not_lose_or_duplicate_grades(tmp_path, knowledge, monkeypatch):
    with running_collector(tmp_path / "hub") as (hub, url):
        state = tmp_path / "host"
        wrong = KnowledgeFeedbackStore(state, sync_config=FeedbackSyncConfig(url, "wrong-secret-" * 4))
        receipt = issue(wrong, knowledge, "retry-chat-uuid")
        result = wrong.record(task_id=receipt["task_id"], delivery_id=receipt["delivery_id"], ratings=[grade(receipt)])
        assert result["sync"]["status"] == "pending"
        assert wrong.report()["total_ratings"] == 1
        assert hub.report()["total_ratings"] == 0

        restarted = KnowledgeFeedbackStore(state, sync_config=FeedbackSyncConfig(url, TOKEN))
        request = FeedbackSyncClient._request

        def lose_ack(self, path, payload=None):
            result = request(self, path, payload)
            if payload is not None:
                raise FeedbackSyncError("simulated response loss after server commit")
            return result

        monkeypatch.setattr(FeedbackSyncClient, "_request", lose_ack)
        assert restarted.sync_pending()["status"] == "pending"
        assert hub.report()["total_ratings"] == 1
        monkeypatch.setattr(FeedbackSyncClient, "_request", request)
        assert restarted.sync_pending()["status"] == "acknowledged"
        assert restarted.sync_pending()["attempted_count"] == 0
        assert hub.report()["total_ratings"] == 1

    # A failed team query must never masquerade as the successful local report.
    with pytest.raises(FeedbackError, match="unavailable"):
        restarted.report_for_scope()
    assert restarted.report_for_scope(scope="local")["total_ratings"] == 1
    assert FeedbackHubStore(tmp_path / "hub").report()["total_ratings"] == 1


def test_live_collector_routes_sdk_mcp_and_learning_cli(tmp_path, knowledge, monkeypatch, capsys, accept_learning_agreement):
    from test_thin_mcp_server import _fake_mcp

    from infermatrix_copilot.cli.entry import main

    with running_collector(tmp_path / "hub") as (hub, url):
        monkeypatch.setenv("KNOWLEDGE_FEEDBACK_URL", url)
        monkeypatch.setenv("KNOWLEDGE_FEEDBACK_TOKEN", TOKEN)
        monkeypatch.setenv("KNOWLEDGE_LEARNING_STATE_DIR", str(tmp_path / "sdk-host"))
        accept_learning_agreement()
        receipt = DirectClient().read_document(PATH, task_id="sdk-chat-uuid").knowledge_feedback
        client = KnowledgeFeedbackClient()
        result = client.record(task_id=receipt["task_id"], delivery_id=receipt["delivery_id"], ratings=[grade(receipt)])
        assert result["sync"]["status"] == "acknowledged"
        assert client.report()["scope"] == "central"
        explicit = KnowledgeFeedbackClient(state_dir=tmp_path / "sdk-host")
        assert explicit.report()["scope"] == "central"
        assert explicit.sync_status()["status"] == "acknowledged"

        monkeypatch.setenv("KNOWLEDGE_LEARNING_STATE_DIR", str(tmp_path / "mcp-host"))
        accept_learning_agreement()
        mcp, _ = _fake_mcp(monkeypatch)
        packet = mcp.tools["doc_read"](path=PATH, repo="demo", task_id="mcp-chat-uuid")
        receipt = packet["knowledge_feedback"]
        result = mcp.tools["knowledge_feedback"](task_id=receipt["task_id"], delivery_id=receipt["delivery_id"],
                                                 ratings=[grade(receipt, -1)])
        assert result["sync"]["status"] == "acknowledged"
        assert mcp.tools["knowledge_feedback_report"](repo="demo")["total_ratings"] == 2
        assert mcp.tools["knowledge_feedback_report"](repo="demo", scope="local")["total_ratings"] == 1
        assert mcp.tools["knowledge_feedback_sync"](action="status")["pending_count"] == 0
        assert mcp.tools["knowledge_feedback_sync"]()["attempted_count"] == 0

        assert main(["learning", "report", "--scope", "central", "--repo", "demo"]) == 0
        assert '"total_ratings": 2' in capsys.readouterr().out
        assert main(["learning", "sync-status"]) == 0
        assert '"acknowledged"' in capsys.readouterr().out
        assert hub.report()["total_ratings"] == 2


def test_credential_in_evidence_key_is_blocked_before_automatic_upload(tmp_path, knowledge):
    with running_collector(tmp_path / "hub") as (hub, url):
        store = KnowledgeFeedbackStore(tmp_path / "host", sync_config=FeedbackSyncConfig(url, TOKEN))
        receipt = issue(store, knowledge, "privacy-chat-uuid")
        rating = {**grade(receipt), "evidence": {"ghp_" + "A" * 30: "test passed"}}
        result = store.record(task_id=receipt["task_id"], delivery_id=receipt["delivery_id"], ratings=[rating])
        assert result["sync"]["status"] == "pending"
        assert result["sync"]["last_error"] == "privacy_blocked"
        assert store.report()["total_ratings"] == 1
        assert hub.report()["total_ratings"] == 0
