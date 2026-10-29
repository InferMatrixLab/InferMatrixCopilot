import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("accepted_learning_feature")

from infermatrix_copilot.knowledge_service.feedback import (
    FeedbackError,
    KnowledgeFeedbackStore,
    from_env,
    model_content_documents,
)
from infermatrix_copilot.knowledge_view import KnowledgeView, build_manifest

FIRST = "## FIRST-1a — Preserve the source contract\n\nKeep the first executable contract intact when modifying the call.\n\n"
SECOND = "## SECOND-1a — Preserve the independent contract\n\nKeep the second independent source contract intact when changing the return.\n"
PAGE = "# Rules\n\n" + FIRST + SECOND
DOCUMENT = "repos/r/rules.md"


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "knowledge"
    (root / "repos/r").mkdir(parents=True)
    (root / DOCUMENT).write_text(PAGE)
    return KnowledgeFeedbackStore(tmp_path / "learning"), KnowledgeView(root, "snapshot-1")


def issue(world, task="task-1", content=PAGE, **kwargs):
    store, view = world
    return store.issue_delivery(task_id=task, repository="r", view=view,
                                documents=[{"path": DOCUMENT, "content": content}], **kwargs)


def vote(target, **kwargs):
    return {"target_id": target["target_id"], "score": 1, "used": True,
            "reason": "The contract guided the successful fix.",
            "evidence": ["Focused test run: source contract passed."], **kwargs}


def test_delivery_is_provider_bound_hash_only_and_works_without_containment(world, monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_MAINTENANCE_ENABLED", raising=False)
    store, view = world
    receipt = issue(world)
    assert receipt["task_id"] == "task-1"
    assert {target["kind"] for target in receipt["targets"]} == {"document", "rule"}
    assert {target.get("block_id") for target in receipt["targets"] if target["kind"] == "rule"} == {"FIRST-1a", "SECOND-1a"}
    assert receipt["documents"][0]["page_sha256"] == hashlib.sha256(PAGE.encode()).hexdigest()
    assert receipt["documents"][0]["delivered_sha256"] == hashlib.sha256(PAGE.encode()).hexdigest()
    stored = store.path.read_bytes()
    assert FIRST.encode() not in stored
    assert str(view.root) not in json.dumps(receipt)
    assert issue(world) == receipt


def test_only_complete_delivered_rules_are_eligible(world):
    receipt = issue(world, content="Provider wrapper\n" + FIRST.strip() + "\nEnd wrapper")
    assert [target["block_id"] for target in receipt["targets"] if target["kind"] == "rule"] == ["FIRST-1a"]
    partial = issue(world, content=FIRST[:65])
    assert [target["kind"] for target in partial["targets"]] == ["document"]


def test_empty_delivery_does_not_invent_targets(world):
    store, view = world
    receipt = store.issue_delivery(task_id="empty", repository="r", view=view, documents=[])
    assert receipt["targets"] == []
    assert store.record(task_id="empty", delivery_id=receipt["delivery_id"], ratings=[])["ratings"] == []
    with pytest.raises(FeedbackError, match="not delivered"):
        store.record(task_id="empty", delivery_id=receipt["delivery_id"], ratings=[vote({"target_id": "a" * 64})])


@pytest.mark.parametrize("document", ["../outside.md", "/tmp/unrelated.md", "repos/r/nonmarkdown.txt"])
def test_document_path_and_markdown_containment(world, document):
    store, view = world
    (view.root / "repos/r/nonmarkdown.txt").write_text("not Markdown")
    with pytest.raises(FeedbackError, match="knowledge document"):
        store.issue_delivery(task_id="task", repository="r", view=view,
                             documents=[{"path": document, "content": "delivered"}])


def test_symlink_escape_and_state_inside_knowledge_are_refused(world, tmp_path):
    store, view = world
    outside = tmp_path / "outside.md"
    outside.write_text(PAGE)
    (view.root / "repos/r/escape.md").symlink_to(outside)
    with pytest.raises(FeedbackError, match="knowledge document"):
        store.issue_delivery(task_id="task", repository="r", view=view,
                             documents=[{"path": "repos/r/escape.md", "content": PAGE}])
    inside = KnowledgeFeedbackStore(view.root / "learning")
    with pytest.raises(FeedbackError, match="outside the knowledge tree"):
        issue((inside, view))
    assert not inside.path.exists()


def test_verified_view_rejects_manifest_tampering(world):
    store, view = world
    manifest = build_manifest(view.root, "verified")
    verified = KnowledgeView(view.root, "verified", manifest["files"])
    (view.root / DOCUMENT).write_text("changed source")
    with pytest.raises(FeedbackError, match="knowledge document"):
        issue((store, verified))


@pytest.mark.parametrize("change", [
    {"score": True}, {"score": False}, {"score": 0}, {"score": 1.0}, {"score": "1"},
    {"used": False}, {"used": 1}, {"reason": " "}, {"evidence": []}, {"evidence": {}},
    {"evidence": [""]}, {"evidence": None}, {"target_id": "unknown"},
])
def test_rating_requires_eligible_used_target_strict_score_reason_and_evidence(world, change):
    store, _ = world
    receipt = issue(world)
    with pytest.raises(FeedbackError):
        store.record(task_id="task-1", delivery_id=receipt["delivery_id"], ratings=[vote(receipt["targets"][0], **change)])
    assert store.report()["total_ratings"] == 0


def test_task_binding_unknown_delivery_and_unread_sibling_are_refused(world):
    store, _ = world
    all_targets = issue(world)
    partial = issue(world, content=FIRST)
    second = next(target for target in all_targets["targets"] if target.get("block_id") == "SECOND-1a")
    with pytest.raises(FeedbackError, match="not delivered"):
        store.record(task_id="task-1", delivery_id=partial["delivery_id"], ratings=[vote(second)])
    for task_id, delivery_id in (("another-task", partial["delivery_id"]), ("task-1", "a" * 64)):
        with pytest.raises(FeedbackError, match="not issued"):
            store.record(task_id=task_id, delivery_id=delivery_id, ratings=[])


def test_feedback_is_immutable_idempotent_across_restart_and_repeat_deliveries(world):
    store, view = world
    receipt = issue(world)
    rating = vote(receipt["targets"][0])
    first = store.record(task_id="task-1", delivery_id=receipt["delivery_id"], ratings=[rating])
    restarted = KnowledgeFeedbackStore(store.state_dir)
    assert restarted.record(task_id="task-1", delivery_id=receipt["delivery_id"], ratings=[rating]) == first
    second_receipt = issue((restarted, view), content="Wrapped\n" + PAGE)
    restarted.record(task_id="task-1", delivery_id=second_receipt["delivery_id"], ratings=[rating])
    assert restarted.report()["total_ratings"] == 1
    with pytest.raises(FeedbackError, match="immutable"):
        restarted.record(task_id="task-1", delivery_id=receipt["delivery_id"], ratings=[{**rating, "score": -1}])


def test_feedback_batch_is_atomic_on_conflict(world):
    store, _ = world
    receipt = issue(world)
    first, second = receipt["targets"][:2]
    store.record(task_id="task-1", delivery_id=receipt["delivery_id"], ratings=[vote(second)])
    with pytest.raises(FeedbackError, match="immutable"):
        store.record(task_id="task-1", delivery_id=receipt["delivery_id"],
                     ratings=[vote(first), vote(second, score=-1)])
    assert store.report()["total_ratings"] == 1


def test_concurrent_retries_count_only_one_vote(world):
    store, _ = world
    receipt = issue(world)
    rating = vote(receipt["targets"][0], score=-1)
    def record(_):
        return KnowledgeFeedbackStore(store.state_dir).record(
            task_id="task-1", delivery_id=receipt["delivery_id"], ratings=[rating])
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(record, range(12)))
    assert all(result == results[0] for result in results)
    assert store.report()["total_ratings"] == 1


def test_report_groups_exact_content_versions_and_distinct_tasks(world):
    store, view = world
    receipt = issue(world)
    rule = next(target for target in receipt["targets"] if target.get("block_id") == "FIRST-1a")
    for task, score in (("task-1", 1), ("task-2", -1), ("task-3", 1)):
        delivered = issue(world, task=task)
        store.record(task_id=task, delivery_id=delivered["delivery_id"], ratings=[vote(rule, score=score)])
    # An unrelated sibling edit preserves this rule's identity; its own edit does not.
    (view.root / DOCUMENT).write_text("# Different preamble\n\n" + FIRST + SECOND + "Additional sibling text.\n")
    unchanged = issue(world, task="task-4", content=FIRST)
    assert next(target for target in unchanged["targets"] if target["kind"] == "rule")["target_id"] == rule["target_id"]
    changed = FIRST.replace("Keep the first", "Always preserve the first")
    (view.root / DOCUMENT).write_text("# Rules\n\n" + changed + SECOND)
    newer = issue(world, task="task-4", content=changed)
    new_rule = next(target for target in newer["targets"] if target["kind"] == "rule")
    assert new_rule["target_id"] != rule["target_id"]
    store.record(task_id="task-4", delivery_id=newer["delivery_id"], ratings=[vote(new_rule, score=-1)])
    report = store.report(repository="r")
    old = next(target for target in report["targets"] if target["target_id"] == rule["target_id"])
    assert (old["positive_count"], old["negative_count"], old["net_score"], old["rating_count"], old["distinct_tasks"]) == (2, 1, 1, 3, 3)
    assert old["mean_score"] == pytest.approx(1 / 3)
    assert report["total_targets"] == 2
    assert store.report(repository="another")["total_ratings"] == 0
    assert store.report(limit=1)["truncated"]


def test_document_version_changes_with_page_and_rule_version_with_upstream_pin(world):
    first = issue(world)
    _store, view = world
    pinned = f"---\nsources: [https://example.test/r/blob/{'a' * 40}/api.py]\n---\n" + PAGE
    (view.root / DOCUMENT).write_text(pinned)
    second = issue(world, content=pinned)
    changed_pin = pinned.replace("a" * 40, "b" * 40)
    (view.root / DOCUMENT).write_text(changed_pin)
    third = issue(world, content=changed_pin)
    assert {target["target_id"] for target in first["targets"]}.isdisjoint(
        target["target_id"] for target in second["targets"])
    assert {target["target_id"] for target in second["targets"]}.isdisjoint(
        target["target_id"] for target in third["targets"])


def test_environment_and_default_state_path(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_LEARNING_STATE_DIR", str(tmp_path / "chosen"))
    assert from_env().path == tmp_path / "chosen/feedback.sqlite3"
    assert KnowledgeFeedbackStore.from_env().path == from_env().path
    monkeypatch.delenv("KNOWLEDGE_LEARNING_STATE_DIR")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert from_env().path == tmp_path / ".infermatrix-copilot/learning/feedback.sqlite3"


def test_adaptive_fragments_extract_only_actual_json_deliveries():
    def render(path, content):
        return "\n<untrusted_data>\n" + json.dumps({"path": path, "content": content}).replace("<", "\\u003c") + "\n</untrusted_data>\n"
    content = FIRST + "\n<untrusted_data>\nquoted, not a new delivery\n</untrusted_data>\n"
    rendered = render(DOCUMENT, content) + render("general/api.md", "Additional actual context")
    assert model_content_documents(rendered) == [
        {"path": DOCUMENT, "content": content},
        {"path": "general/api.md", "content": "Additional actual context"},
    ]
    assert model_content_documents("Plain text is not an adaptive source delivery") == []
