import copy
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from infermatrix_copilot.knowledge_service.agreement import AgreementStore
from infermatrix_copilot.knowledge_service.feedback import (
    FeedbackError,
    KnowledgeFeedbackStore,
    _digest,
)
from infermatrix_copilot.knowledge_service.feedback_hub import (
    FeedbackConflict,
    FeedbackHubStore,
    validate_receipt,
)
from infermatrix_copilot.knowledge_view import KnowledgeView, build_manifest

pytestmark = pytest.mark.usefixtures("accepted_learning_feature")

FIRST = "## FIRST-1a — Preserve the source contract\n\nKeep the source contract intact when modifying the call.\n\n"
SECOND = "## SECOND-1a — Preserve the independent contract\n\nKeep the independent contract intact when changing the return.\n"
PAGE = "# Rules\n\n" + FIRST + SECOND
DOCUMENT = "repos/r/rules.md"


@pytest.fixture
def provider(tmp_path):
    root = tmp_path / "knowledge"
    (root / "repos/r").mkdir(parents=True)
    (root / DOCUMENT).write_text(PAGE)
    return KnowledgeFeedbackStore(tmp_path / "provider"), KnowledgeView(root, "snapshot-1")


@pytest.fixture
def agreement(provider):
    return AgreementStore.from_env(state_dir=provider[0].state_dir).attestation()


def issue(provider, task="task-1", content=PAGE):
    store, view = provider
    return store.issue_delivery(task_id=task, repository="r", view=view,
                                documents=[{"path": DOCUMENT, "content": content}])


def vote(target, **kwargs):
    return {"target_id": target["target_id"], "score": 1, "used": True,
            "reason": "The rule guided the verified fix.", "evidence": ["Focused source contract test passed."], **kwargs}


def redigest(receipt):
    receipt["delivery_id"] = _digest({key: value for key, value in receipt.items() if key != "delivery_id"})
    return receipt


def test_shared_import_uses_existing_identity_and_is_durable(provider, tmp_path, agreement):
    receipt = issue(provider)
    hub = FeedbackHubStore(tmp_path / "hub")
    target = next(target for target in receipt["targets"] if target["kind"] == "rule")
    result = hub.ingest(receipt=receipt, ratings=[vote(target)], agreement=agreement)
    assert result["ratings"][0]["feedback_id"] == _digest(["task-1", target["target_id"]])
    restarted = FeedbackHubStore(hub.state_dir)
    assert restarted.ingest(receipt=receipt, ratings=[vote(target)], agreement=agreement) == result
    second = issue(provider, content="Presentation wrapper\n" + PAGE)
    assert second["delivery_id"] != receipt["delivery_id"]
    restarted.ingest(receipt=second, ratings=[vote(target)], agreement=agreement)
    assert restarted.report()["total_ratings"] == 1
    with sqlite3.connect(hub.path) as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_outbox").fetchone()[0] == 0
    assert FIRST.encode() not in hub.path.read_bytes()
    assert not (hub.path.stat().st_mode & 0o077)


def test_multiple_tasks_and_versions_aggregate_like_local_report(provider, tmp_path, agreement):
    hub = FeedbackHubStore(tmp_path / "hub")
    local = provider[0]
    for task, score in (("host-a:task-1", 1), ("host-b:task-2", -1), ("host-a:task-3", 1)):
        receipt = issue(provider, task=task)
        targets = [target for target in receipt["targets"] if target.get("block_id") == "FIRST-1a"]
        ratings = [vote(targets[0], score=score)]
        local.record(task_id=task, delivery_id=receipt["delivery_id"], ratings=ratings)
        hub.ingest(receipt=receipt, ratings=ratings, agreement=agreement)
    changed = FIRST.replace("Keep the source", "Always preserve the source")
    (provider[1].root / DOCUMENT).write_text("# Rules\n\n" + changed + SECOND)
    receipt = issue(provider, task="host-c:task-4", content=changed)
    target = next(target for target in receipt["targets"] if target["kind"] == "rule")
    local.record(task_id=receipt["task_id"], delivery_id=receipt["delivery_id"], ratings=[vote(target)])
    hub.ingest(receipt=receipt, ratings=[vote(target)], agreement=agreement)
    for repo, limit in (("", 100), ("r", 1), ("another", 100)):
        assert hub.report(repository=repo, limit=limit) == local.report(repository=repo, limit=limit)
    row = next(target for target in hub.report()["targets"] if target["rating_count"] == 3)
    assert (row["positive_count"], row["negative_count"], row["distinct_tasks"], row["mean_score"]) == (2, 1, 3, pytest.approx(1 / 3))


def test_concurrent_shared_imports_deduplicate_and_conflict_atomically(provider, tmp_path, agreement):
    receipt = issue(provider)
    target, another = receipt["targets"][:2]
    hub = FeedbackHubStore(tmp_path / "hub")
    def ingest(_):
        return FeedbackHubStore(hub.state_dir).ingest(receipt=receipt, ratings=[vote(target)], agreement=agreement)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(ingest, range(12)))
    assert all(result == results[0] for result in results)
    with pytest.raises(FeedbackConflict, match="immutable"):
        hub.ingest(receipt=receipt, ratings=[vote(another), vote(target, score=-1)], agreement=agreement)
    assert hub.report()["total_ratings"] == 1


@pytest.mark.parametrize("change", [
    lambda r: r.update(protocol="feedback/other"),
    lambda r: r.update(task_id=" task "),
    lambda r: r.update(tree_sha256="f" * 40),
    lambda r: r.update(delivery_id="0" * 64),
    lambda r: r.update(transcript="private chat"),
    lambda r: r["documents"][0].update(path="../outside.md"),
    lambda r: r["documents"][0].update(path="C:/outside.md"),
    lambda r: r["documents"][0].update(delivered_chars=True),
    lambda r: r["documents"].append(copy.deepcopy(r["documents"][0])),
    lambda r: r["targets"][0].update(repository="another"),
    lambda r: r["targets"][0].update(snapshot="another"),
    lambda r: r["targets"][0].update(page_sha256="b" * 64),
    lambda r: r["targets"][0].update(content_sha256="b" * 64),
    lambda r: r["targets"][1].update(unit_id="b" * 64),
    lambda r: r["targets"][1].update(block_id="OTHER-1a"),
    lambda r: r["targets"][1].update(upstream_pins=["b" * 40, "a" * 40]),
    lambda r: r["targets"][1].update(target_id="b" * 64),
    lambda r: r["targets"][1].update(kind=[]),
    lambda r: r["targets"].append(copy.deepcopy(r["targets"][0])),
    lambda r: r["targets"].pop(0),
])
def test_receipt_metadata_cannot_be_rebound_even_with_recomputed_envelope(provider, tmp_path, change, agreement):
    receipt = copy.deepcopy(issue(provider))
    change(receipt)
    if receipt.get("delivery_id") != "0" * 64:
        redigest(receipt)
    hub = FeedbackHubStore(tmp_path / "hub")
    with pytest.raises(FeedbackError):
        hub.ingest(receipt=receipt, ratings=[], agreement=agreement)
    assert not hub.path.exists()


def test_receipt_cannot_grant_undelivered_sibling(provider, tmp_path, agreement):
    full = issue(provider)
    partial = issue(provider, content=FIRST)
    sibling = next(target for target in full["targets"] if target.get("block_id") == "SECOND-1a")
    hub = FeedbackHubStore(tmp_path / "hub")
    with pytest.raises(FeedbackError, match="not delivered"):
        hub.ingest(receipt=partial, ratings=[vote(sibling)], agreement=agreement)
    assert hub.report()["total_ratings"] == 0


def test_verified_manifest_receipt_and_source_pins_keep_existing_protocol(provider):
    store, view = provider
    pinned = "---\nsources: [https://example.test/r/blob/" + "a" * 40 + "/api.py]\n---\n" + PAGE
    (view.root / DOCUMENT).write_text(pinned)
    manifest = build_manifest(view.root, "activated")
    verified = KnowledgeView(view.root, "activated", manifest["files"])
    receipt = issue((store, verified), content=pinned)
    assert validate_receipt(receipt) == receipt
    assert receipt["tree_sha256"]
    assert next(target for target in receipt["targets"] if target["kind"] == "rule")["upstream_pins"] == ["a" * 40]


def test_private_or_noncanonical_ratings_rejected_without_partial_votes(provider, tmp_path, agreement):
    receipt = issue(provider)
    target = receipt["targets"][0]
    hub = FeedbackHubStore(tmp_path / "hub")
    for change in ({"transcript": "raw"}, {"evidence": "ghp_" + "A" * 30}, {"score": True}):
        with pytest.raises(FeedbackError):
            hub.ingest(receipt=receipt, ratings=[vote(target, **change)], agreement=agreement)
    assert hub.report()["total_ratings"] == 0


def test_noop_and_limits_and_unknown_task_delivery(provider, tmp_path, agreement):
    store, view = provider
    empty = store.issue_delivery(task_id="empty", repository="r", view=view, documents=[])
    hub = FeedbackHubStore(tmp_path / "hub")
    assert hub.ingest(receipt=empty, ratings=[], agreement=agreement)["ratings"] == []
    for limit in (True, 0, 1001, "100"):
        with pytest.raises(FeedbackError):
            hub.report(limit=limit)
    with pytest.raises(FeedbackError, match="not issued"):
        hub.record(task_id="unknown", delivery_id=empty["delivery_id"], ratings=[])
    with sqlite3.connect(hub.path) as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_ratings").fetchone()[0] == 0
