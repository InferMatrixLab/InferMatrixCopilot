"""Task learning is reachable from real delivery and public entry points."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.usefixtures("accepted_learning_feature")
from test_thin_mcp_server import _fake_mcp

from infermatrix_copilot.knowledge_context import ContextBudget, KnowledgeContextService
from infermatrix_copilot.knowledge_view import KnowledgeView
from infermatrix_copilot.sdk.v1 import (
    DirectClient,
    KnowledgeFeedbackClient,
    KnowledgeLearningClient,
)


@pytest.fixture
def delivery(tmp_path, monkeypatch, accept_learning_agreement):
    root = tmp_path / "knowledge"
    owner = root / "repos" / "demo"
    owner.mkdir(parents=True)
    (owner / "_index.md").write_text("# Demo\n")
    path = owner / "guide.md"
    path.write_text("# Runtime guide\n\nCheck the output contract before retrying.\n")
    monkeypatch.setenv("KNOWLEDGE_LEARNING_STATE_DIR", str(tmp_path / "state"))
    accept_learning_agreement()
    monkeypatch.setenv("KNOWLEDGE_MAINTENANCE_ENABLED", "0")
    view = KnowledgeView(root, "unverified")
    monkeypatch.setattr(KnowledgeView, "current", classmethod(lambda cls: view))
    return view, path


def grade(receipt, score=1):
    target = next(row for row in receipt["targets"] if row["kind"] == "document")
    return {"target_id": target["target_id"], "score": score, "used": True,
            "reason": "The output-contract check established the retry result.",
            "evidence": ["tests/test_runtime.py::test_output_contract passed"]}


def test_mcp_read_to_grade_to_report_without_containment(delivery, monkeypatch):
    mcp, _ = _fake_mcp(monkeypatch)
    packet = mcp.tools["doc_read"](path="repos/demo/guide.md", repo="demo", task_id="host-task")
    assert packet["content"].endswith("before retrying.\n")
    receipt = packet["knowledge_feedback"]
    assert receipt["task_id"] == "host-task"
    rating = grade(receipt, -1)
    call = lambda: mcp.tools["knowledge_feedback"](
        task_id="host-task", delivery_id=receipt["delivery_id"], ratings=[rating])
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: call(), range(4)))
    assert all(reply == replies[0] for reply in replies)
    report = mcp.tools["knowledge_feedback_report"](repo="demo")
    assert report["total_ratings"] == 1
    assert report["targets"][0]["mean_score"] == -1
    assert "error" in mcp.tools["knowledge_feedback"](
        task_id="another-task", delivery_id=receipt["delivery_id"], ratings=[rating])


def test_sdk_reads_and_feedback_share_the_public_store(delivery):
    client = DirectClient()
    page = client.read_document("repos/demo/guide.md", task_id="sdk-task")
    receipt = page.to_dict()["knowledge_feedback"]
    feedback = KnowledgeFeedbackClient()
    feedback.record(task_id="sdk-task", delivery_id=receipt["delivery_id"], ratings=[grade(receipt)])
    assert feedback.report(repository="demo")["targets"][0]["positive_count"] == 1


def test_sdk_metadata_reads_stay_available_without_ratable_rules(delivery):
    view, _ = delivery
    (view.root / "repos/demo/_routes.yaml").write_text("owners: []\n")
    page = DirectClient().read_document("repos/demo/_routes.yaml", task_id="metadata-task")
    assert page.content == "owners: []\n"
    assert page.knowledge_feedback["targets"] == []


def test_unavailable_feedback_storage_does_not_break_source_delivery(delivery, monkeypatch):
    from infermatrix_copilot.knowledge_service.feedback import KnowledgeFeedbackStore

    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("feedback database unavailable")

    monkeypatch.setattr(KnowledgeFeedbackStore, "issue_delivery", unavailable)
    mcp, _ = _fake_mcp(monkeypatch)
    packet = mcp.tools["doc_read"](path="repos/demo/guide.md", repo="demo", task_id="storage-task")
    assert "before retrying" in packet["content"]
    assert packet["knowledge_feedback"]["status"] == "unavailable"
    page = DirectClient().read_document("repos/demo/guide.md", task_id="storage-task")
    assert "before retrying" in page.content
    assert page.knowledge_feedback["status"] == "unavailable"


def test_raw_mcp_feedback_pins_the_same_view_as_delivered_content(delivery, tmp_path, monkeypatch):
    view, path = delivery
    mcp, _ = _fake_mcp(monkeypatch)
    new_root = tmp_path / "new-knowledge"
    (new_root / "repos/demo").mkdir(parents=True)
    (new_root / "repos/demo/_index.md").write_text("# New demo\n")
    (new_root / "repos/demo/guide.md").write_text("A replacement rule with different behavior.\n")
    replacement = KnowledgeView(new_root, "replacement")
    resolutions = []

    def activate_between_resolutions(cls):
        resolutions.append(True)
        return view if len(resolutions) == 1 else replacement

    monkeypatch.setattr(KnowledgeView, "current", classmethod(activate_between_resolutions))
    packet = mcp.tools["doc_read"](path="repos/demo/guide.md", repo="demo", task_id="activation-task")
    target = next(t for t in packet["knowledge_feedback"]["targets"] if t["kind"] == "document")
    assert "before retrying" in packet["content"]
    assert target["page_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(resolutions) == 1


def test_adaptive_delivery_receipt_persists_and_exposes_only_delivered_content(delivery, tmp_path):
    view, _ = delivery
    resolver = lambda _view, _repo: {"repo_id": "demo", "knowledge_slice": "repos/demo",
                                    "source_pin": "a" * 40, "catalog_hash": "", "policy_hash": ""}
    service = KnowledgeContextService(view, tmp_path / "context.sqlite", resolver=resolver)
    session = service.open_session("demo", source_pin="a" * 40, review_id="adaptive-task",
                                   budget=ContextBudget(initial_tokens=500))
    packet = service.read(session["session_id"], "repos/demo/guide.md")
    receipt = packet["knowledge_feedback"]
    assert receipt["task_id"] == "adaptive-task"
    assert len(receipt["documents"]) == 1
    assert service.read(session["session_id"], "repos/demo/guide.md") == packet
    KnowledgeFeedbackClient().record(task_id=receipt["task_id"], delivery_id=receipt["delivery_id"],
                                     ratings=[grade(receipt)])
    assert KnowledgeFeedbackClient().report(repository="demo")["total_ratings"] == 1


def test_mcp_and_sdk_share_empty_completion_state(delivery, monkeypatch):
    mcp, _ = _fake_mcp(monkeypatch)
    packet = mcp.tools["knowledge_learning"](action="prepare", task_id="no-lesson", repo="demo",
                                             source_pin="a" * 40, lessons=[])
    assert packet["status"] == "no_op"
    client = KnowledgeLearningClient()
    assert client.status("no-lesson")["packet_digest"] == packet["packet_digest"]
    submitted = client.submit(task_id="no-lesson", packet_digest=packet["packet_digest"],
                              proposals={"rules": []})
    assert submitted["status"] == "no_op"
    assert "error" in mcp.tools["knowledge_learning"](action="execute", task_id="no-lesson")


def test_learning_cli_dispatch_requires_no_model_or_mcp(delivery, capsys):
    from infermatrix_copilot.cli.entry import main

    assert main(["learning", "report", "--repo", "demo"]) == 0
    assert '"total_ratings": 0' in capsys.readouterr().out
