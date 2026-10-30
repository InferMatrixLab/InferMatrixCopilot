"""Public SDK deliveries retain one feedback identity across aliases and reads."""
import shutil
import sqlite3
from contextlib import contextmanager
from threading import Thread

import pytest

pytestmark = pytest.mark.usefixtures("accepted_learning_feature")
import yaml

from infermatrix_copilot.kb_service.repo_spec import RepoSpec
from infermatrix_copilot.knowledge_service.feedback import KnowledgeFeedbackStore
from infermatrix_copilot.knowledge_service.feedback_http import create_server
from infermatrix_copilot.knowledge_service.feedback_hub import FeedbackHubStore
from infermatrix_copilot.knowledge_view import KnowledgeView
from infermatrix_copilot.sdk.v1 import (
    ChangedPath,
    DirectClient,
    DirectReviewRequest,
    InvalidRequestError,
    KnowledgeFeedbackClient,
    RepositoryRef,
    UnsupportedRepositoryError,
)

RULE_PATH = "repos/demo-repo/rules.md"
GUIDE_PATH = "general/review/guides/simplification-audit.md"
RULE = "## CONTRACT-1a — Check the result\n\nCheck the result before accepting the task outcome.\n"
GUIDE = "## REVIEW-1a — Verify the source\n\nVerify the source before publishing a conclusion.\n"


@pytest.fixture
def knowledge(tmp_path, monkeypatch):
    root = tmp_path / "knowledge"
    files = {
        "AGENTS.md": "# Knowledge navigation\n",
        "README.md": "# Knowledge\n",
        "general/_index.md": "# General\n",
        "repos/_index.md": "# Repositories\n",
        "repos/demo-repo/_index.md": "# Demo repository\n",
        RULE_PATH: "# Rules\n\n" + RULE,
        GUIDE_PATH: "# Review\n\n" + GUIDE,
    }
    spec = RepoSpec("demo-repo", aliases=("demo_repo", "owner/demo-repo"), source_pin="a" * 40)
    files["_repositories.yaml"] = yaml.safe_dump({"schema_version": 1, "repos": {"demo-repo": spec.to_dict()}})
    files["repos/demo-repo/_routes.yaml"] = yaml.safe_dump({"schema_version": 1, "owners": [{
        "owner": "contract", "path": RULE_PATH, "signals": ["contract"], "scope_prefixes": ["pkg/"]}]})
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    view = KnowledgeView(root, "identity-snapshot")
    monkeypatch.setattr(KnowledgeView, "current", classmethod(lambda cls: view))
    monkeypatch.setenv("KNOWLEDGE_MAINTENANCE_ENABLED", "0")
    return view


def request(alias="demo-repo", task_id="identity-task"):
    return DirectReviewRequest(task_id, RepositoryRef(alias), 1, "a" * 40,
                               "Review contract", "", (ChangedPath("pkg/contract.py"),))


def target(receipt, path=RULE_PATH):
    return next(row for row in receipt["targets"] if row["kind"] == "rule" and row["path"] == path)


def record(client, receipt, path=RULE_PATH):
    return client.record(task_id=receipt["task_id"], delivery_id=receipt["delivery_id"], ratings=[{
        "target_id": target(receipt, path)["target_id"], "score": 1, "used": True,
        "reason": "The result check determined the task outcome.",
        "evidence": "tests/test_contract.py::test_result passed",
    }])


@contextmanager
def collector(tmp_path, monkeypatch):
    hub = FeedbackHubStore(tmp_path / "hub")
    token = "identity-test-provider-secret-1234567890"
    server = create_server(hub, token)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    monkeypatch.setenv("KNOWLEDGE_FEEDBACK_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("KNOWLEDGE_FEEDBACK_TOKEN", token)
    try:
        yield hub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_supported_aliases_keep_one_public_sdk_and_central_vote(knowledge, tmp_path, monkeypatch, accept_learning_agreement):
    with collector(tmp_path, monkeypatch) as hub:
        accept_learning_agreement()
        sdk = DirectClient()
        receipts = [sdk.plan(request(alias)).knowledge_feedback
                    for alias in ("demo-repo", "demo_repo", "owner/demo-repo")]
        assert {receipt["repository"] for receipt in receipts} == {"demo-repo"}
        assert len({target(receipt)["target_id"] for receipt in receipts}) == 1
        feedback = KnowledgeFeedbackClient()
        for receipt in receipts:
            assert record(feedback, receipt)["sync"]["status"] == "acknowledged"
        assert feedback.report(scope="local")["total_ratings"] == 1
        assert feedback.report(scope="central")["total_ratings"] == 1
        assert hub.report()["total_targets"] == 1
        # Frozen knowledge-feedback/1 canonical identifier, independent of aliases.
        assert target(receipts[0])["target_id"] == "7574eb3b1441d00b21ff43c8e24df88d6d2e7805edf8486748328a00bc7f37ee"


def test_general_task_read_reuses_plan_repository_across_clients(knowledge):
    sdk = DirectClient()
    plan = sdk.plan(request())
    receipts = [plan.knowledge_feedback,
                sdk.read_document(GUIDE_PATH, task_id="identity-task").knowledge_feedback,
                DirectClient().read_document(GUIDE_PATH, task_id="identity-task").knowledge_feedback,
                sdk.read_document(GUIDE_PATH, review_context_id=plan.review_context_id).knowledge_feedback]
    assert {receipt["repository"] for receipt in receipts} == {"demo-repo"}
    assert len({target(receipt, GUIDE_PATH)["target_id"] for receipt in receipts}) == 1
    feedback = KnowledgeFeedbackClient()
    for receipt in receipts:
        record(feedback, receipt, GUIDE_PATH)
    assert feedback.report(scope="local")["total_ratings"] == 1


def test_general_read_before_plan_requires_explicit_task_repository(knowledge):
    sdk = DirectClient()
    with pytest.raises(InvalidRequestError, match="require repository"):
        sdk.read_document(GUIDE_PATH, task_id="identity-task")
    with pytest.raises(UnsupportedRepositoryError, match="served knowledge"):
        sdk.read_document(GUIDE_PATH, task_id="identity-task", repository="unserved-arbitrary-namespace")
    first = sdk.read_document(GUIDE_PATH, task_id="identity-task", repository="demo_repo").knowledge_feedback
    planned = sdk.plan(request()).knowledge_feedback
    assert target(first, GUIDE_PATH)["target_id"] == target(planned, GUIDE_PATH)["target_id"]
    assert sdk.read_document(GUIDE_PATH).knowledge_feedback["repository"] == "general"


def test_ambiguous_task_repository_requires_explicit_context(knowledge):
    sdk = DirectClient()
    plan = sdk.plan(request())
    sdk.read_document(GUIDE_PATH, task_id="identity-task", repository="general")
    with pytest.raises(InvalidRequestError, match="multiple repository contexts"):
        DirectClient().read_document(GUIDE_PATH, task_id="identity-task")
    explicit = DirectClient().read_document(GUIDE_PATH, task_id="identity-task", repository="demo_repo")
    assert target(explicit.knowledge_feedback, GUIDE_PATH)["target_id"] == target(plan.knowledge_feedback, GUIDE_PATH)["target_id"]
    with pytest.raises(InvalidRequestError, match="does not match"):
        sdk.read_document(GUIDE_PATH, review_context_id=plan.review_context_id, repository="general")


def test_alias_resolution_and_context_reads_pin_one_snapshot(knowledge, tmp_path, monkeypatch):
    replacement_root = tmp_path / "replacement"
    shutil.copytree(knowledge.root, replacement_root)
    (replacement_root / RULE_PATH).write_text("# Rules\n\n" + RULE.replace("Check the result", "Check both results"))
    replacement = KnowledgeView(replacement_root, "replacement-snapshot")
    calls = []

    def activate(cls):
        calls.append(True)
        return knowledge if len(calls) == 1 else replacement

    monkeypatch.setattr(KnowledgeView, "current", classmethod(activate))
    sdk = DirectClient()
    plan = sdk.plan(request("demo_repo"))
    assert len(calls) == 1
    original = sdk.read_document(RULE_PATH, review_context_id=plan.review_context_id, repository="demo_repo")
    assert len(calls) == 1
    assert original.knowledge_feedback["snapshot"] == "identity-snapshot"
    assert target(original.knowledge_feedback)["target_id"] == target(plan.knowledge_feedback)["target_id"]
    current = sdk.read_document(RULE_PATH, task_id="identity-task", repository="demo_repo")
    assert current.knowledge_feedback["snapshot"] == "replacement-snapshot"
    assert target(current.knowledge_feedback)["target_id"] != target(original.knowledge_feedback)["target_id"]


def test_adaptive_plan_aliases_and_followup_share_repository(knowledge, tmp_path):
    receipts = []
    initial = []
    for index, alias in enumerate(("demo-repo", "demo_repo")):
        sdk = DirectClient(knowledge_context_path=tmp_path / f"context-{index}.sqlite")
        plan = sdk.plan_adaptive(request(alias))
        initial.append(plan["knowledge_feedback"])
        followup = sdk.read_document(RULE_PATH, review_context_id=plan["review_context_id"])
        assert followup.knowledge_feedback["repository"] == "demo-repo"
        receipts.append(followup.knowledge_feedback)
    assert {receipt["repository"] for receipt in initial} == {"demo-repo"}
    assert {row["target_id"] for row in initial[0]["targets"]} == {
        row["target_id"] for row in initial[1]["targets"]}
    assert {receipt["repository"] for receipt in receipts} == {"demo-repo"}
    assert len({target(receipt)["target_id"] for receipt in receipts}) == 1


@pytest.mark.parametrize("failure", [OSError, sqlite3.OperationalError])
def test_unavailable_task_lookup_and_issuance_keep_source_private(knowledge, monkeypatch, failure):
    def unavailable(self):
        raise failure("private/token-file and authenticated database endpoint")

    monkeypatch.setattr(KnowledgeFeedbackStore, "_db", unavailable)
    monkeypatch.setattr(KnowledgeFeedbackStore, "_inspection_rows", lambda self, *_args: unavailable(self))
    sdk = DirectClient()
    pages = [sdk.read_document(GUIDE_PATH, task_id="unknown-task"),
             sdk.read_document(GUIDE_PATH)]
    for page in pages:
        assert "Verify the source" in page.content
        assert page.knowledge_feedback == {"status": "unavailable", "error": "feedback_storage_unavailable"}
