"""Shared feedback ledger for authenticated, trusted knowledge providers.

An imported receipt is a provider attestation, not an independent proof of
delivery. Only the authenticated transport may expose ``ingest``: end users
must continue rating receipts issued by their own provider. Hash checks bind
all metadata to the existing knowledge-feedback/1 identities without inventing
a second version or deduplication protocol.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

from .agreement import validate_upload_attestation
from .feedback import PROTOCOL, FeedbackError, KnowledgeFeedbackStore, _digest, _json

MAX_RECEIPT_BYTES = 1_500_000
MAX_TARGETS = 4096
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PIN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class FeedbackConflict(FeedbackError):
    """An immutable task/target vote or receipt identity was reused differently."""


def _fields(value, expected, name):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise FeedbackError(f"{name} has an invalid schema")


def _string(value, name, maximum=512, *, empty=False):
    if (not isinstance(value, str) or len(value) > maximum
            or (not empty and not value) or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise FeedbackError(f"{name} is invalid")
    return value


def _sha(value, name, *, empty=False):
    if not isinstance(value, str) or not ((empty and value == "") or _SHA256.fullmatch(value)):
        raise FeedbackError(f"{name} must be a lowercase SHA256 digest")
    return value


def _path(value):
    _string(value, "document path", 1024)
    path = PurePosixPath(value)
    if (path.is_absolute() or "\\" in value or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or path.suffix.casefold() != ".md"):
        raise FeedbackError("document path must be a normalized relative Markdown path")
    return value


def validate_receipt(receipt: dict) -> dict:
    """Validate hashes, exact schemas and all available provenance bindings.

    Source text is deliberately absent. The authenticated provider is responsible
    for checking that complete rule sections really reached the host agent.
    """
    _fields(receipt, {"protocol", "delivery_id", "task_id", "repository", "snapshot",
                      "tree_sha256", "documents", "targets"}, "receipt")
    if receipt["protocol"] != PROTOCOL:
        raise FeedbackError("unsupported feedback protocol")
    _string(receipt["task_id"], "task_id")
    _string(receipt["repository"], "repository", 256)
    _string(receipt["snapshot"], "snapshot")
    _sha(receipt["tree_sha256"], "tree_sha256", empty=True)
    _sha(receipt["delivery_id"], "delivery_id")
    if (not isinstance(receipt["documents"], list) or len(receipt["documents"]) > 256
            or not isinstance(receipt["targets"], list) or len(receipt["targets"]) > MAX_TARGETS):
        raise FeedbackError("receipt contains too many documents or targets")
    if len(_json(receipt).encode("utf-8")) > MAX_RECEIPT_BYTES:
        raise FeedbackError("receipt metadata is too large")
    body = {key: value for key, value in receipt.items() if key != "delivery_id"}
    if _digest(body) != receipt["delivery_id"]:
        raise FeedbackError("delivery identity does not match receipt metadata")

    pages, fragments = {}, set()
    delivered_chars = 0
    for document in receipt["documents"]:
        _fields(document, {"path", "page_sha256", "delivered_sha256", "delivered_chars"}, "document")
        path = _path(document["path"])
        _sha(document["page_sha256"], "page_sha256")
        _sha(document["delivered_sha256"], "delivered_sha256")
        if type(document["delivered_chars"]) is not int or not 1 <= document["delivered_chars"] <= 16_000_000:
            raise FeedbackError("delivered_chars is invalid")
        delivered_chars += document["delivered_chars"]
        if delivered_chars > 16_000_000:
            raise FeedbackError("delivered fragments exceed the delivery limit")
        if path in pages and pages[path] != document["page_sha256"]:
            raise FeedbackError("a document has inconsistent page versions")
        pages[path] = document["page_sha256"]
        identity = _digest(document)
        if identity in fragments:
            raise FeedbackError("receipt cannot repeat a delivered fragment")
        fragments.add(identity)
    if receipt["documents"] != sorted(receipt["documents"], key=lambda row: (row["path"], row["delivered_sha256"])):
        raise FeedbackError("delivered fragments must use provider canonical ordering")

    target_ids, document_targets = set(), set()
    common = {"repository", "path", "kind", "content_sha256", "target_id", "page_sha256", "snapshot"}
    for target in receipt["targets"]:
        if not isinstance(target, dict) or target.get("kind") not in ("document", "rule"):
            raise FeedbackError("target kind is invalid")
        rule = target["kind"] == "rule"
        _fields(target, common | ({"unit_id", "block_id", "upstream_pins"} if rule else set()), "target")
        path = _path(target["path"])
        for field in ("target_id", "page_sha256", "content_sha256"):
            _sha(target[field], field)
        if (target["repository"] != receipt["repository"] or target["snapshot"] != receipt["snapshot"]
                or pages.get(path) != target["page_sha256"]):
            raise FeedbackError("target provenance does not match its delivery")
        version = {key: target[key] for key in ("repository", "path", "kind", "content_sha256")}
        if rule:
            _sha(target["unit_id"], "unit_id")
            _string(target["block_id"], "block_id")
            pins = target["upstream_pins"]
            if (not isinstance(pins, list) or len(pins) > 256
                    or any(not isinstance(pin, str) or not _PIN.fullmatch(pin) for pin in pins)
                    or pins != sorted(set(pins))):
                raise FeedbackError("upstream_pins must be sorted unique source digests")
            parts = PurePosixPath(path).parts
            owner_repo = parts[1] if len(parts) > 2 and parts[0] == "repos" else "general"
            expected_unit = hashlib.sha256("\0".join((owner_repo, path, target["block_id"], "rule")).encode()).hexdigest()
            if target["unit_id"] != expected_unit:
                raise FeedbackError("rule identity does not match its owner and block")
            version.update({key: target[key] for key in ("unit_id", "block_id", "upstream_pins")})
        else:
            if target["content_sha256"] != target["page_sha256"]:
                raise FeedbackError("document version does not match its page")
            document_targets.add(path)
        if _digest(version) != target["target_id"]:
            raise FeedbackError("target identity does not match its content version")
        if target["target_id"] in target_ids:
            raise FeedbackError("receipt cannot repeat a target")
        target_ids.add(target["target_id"])
    if document_targets != set(pages):
        raise FeedbackError("each delivered document requires its document target")
    if receipt["targets"] != sorted(receipt["targets"], key=lambda row: (
            row["path"], row["kind"], row.get("block_id", ""), row["target_id"])):
        raise FeedbackError("targets must use provider canonical ordering")
    # Return an owned copy so caller mutation cannot race persistence.
    return json.loads(_json(receipt))


class FeedbackHubStore(KnowledgeFeedbackStore):
    """One durable team ledger, with global (task_id, target_id) deduplication."""

    _feedback_sync_enabled = False

    def ingest(self, *, receipt: dict, ratings: list[dict], agreement: dict | None = None) -> dict:
        agreement = validate_upload_attestation(agreement)
        receipt = validate_receipt(receipt)
        if not isinstance(ratings, list) or len(ratings) > 256:
            raise FeedbackError("ratings must be a list of at most 256 entries")
        for rating in ratings:
            _fields(rating, {"target_id", "score", "used", "reason", "evidence"}, "rating")
        if len(_json(ratings).encode("utf-8")) > 3_000_000:
            raise FeedbackError("rating metadata is too large")
        from .learning_privacy import LearningPrivacyError, validate_distilled_text

        try:
            validate_distilled_text(receipt)
            validate_distilled_text(ratings)
        except LearningPrivacyError as exc:
            raise FeedbackError("feedback metadata requires redaction before upload") from exc
        # A trusted, structurally valid receipt can remain after an invalid batch;
        # record() applies every vote in a separate single atomic transaction.
        encoded = _json(receipt)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS feedback_agreements (
                    acceptance_id TEXT PRIMARY KEY, attestation TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS feedback_delivery_agreements (
                    delivery_id TEXT NOT NULL, acceptance_id TEXT NOT NULL,
                    PRIMARY KEY(delivery_id, acceptance_id));
            """)
            db.execute("BEGIN IMMEDIATE")
            attestation = _json(agreement)
            prior = db.execute("SELECT attestation FROM feedback_agreements WHERE acceptance_id=?",
                               (agreement["acceptance_id"],)).fetchone()
            if prior and prior["attestation"] != attestation:
                raise FeedbackConflict("agreement identity conflicts with its earlier attestation")
            row = db.execute("SELECT payload FROM feedback_deliveries WHERE id=?", (receipt["delivery_id"],)).fetchone()
            if row and row["payload"] != encoded:
                raise FeedbackConflict("delivery identity conflicts with its earlier receipt")
            db.execute("INSERT OR IGNORE INTO feedback_agreements VALUES (?,?)",
                       (agreement["acceptance_id"], attestation))
            db.execute("INSERT OR IGNORE INTO feedback_deliveries VALUES (?,?,?,?)",
                       (receipt["delivery_id"], receipt["task_id"], receipt["repository"], encoded))
            db.execute("INSERT OR IGNORE INTO feedback_delivery_agreements VALUES (?,?)",
                       (receipt["delivery_id"], agreement["acceptance_id"]))
        try:
            return self.record(task_id=receipt["task_id"], delivery_id=receipt["delivery_id"], ratings=ratings)
        except FeedbackError as exc:
            if str(exc) == "feedback for this task and content version is immutable":
                raise FeedbackConflict(str(exc)) from exc
            raise

    def report(self, repository="", limit=100) -> dict:
        if not isinstance(repository, str) or type(limit) is not int or not 1 <= limit <= 1000:
            raise FeedbackError("report requires a repository string and limit between 1 and 1000")
        repository = repository.strip()
        _string(repository, "repository", 256, empty=True)
        where, parameters = (" WHERE repository=?", (repository,)) if repository else ("", ())
        with self._db() as db:
            db.execute("BEGIN")
            counts = db.execute("SELECT COUNT(*) AS ratings, COUNT(DISTINCT target_id) AS targets "
                                "FROM feedback_ratings" + where, parameters).fetchone()
            rows = db.execute("""
                WITH grouped AS (
                    SELECT target_id, MIN(rowid) AS representative,
                           COUNT(*) AS rating_count,
                           SUM(CASE WHEN json_extract(rating,'$.score')=1 THEN 1 ELSE 0 END) AS positive_count,
                           SUM(CASE WHEN json_extract(rating,'$.score')=-1 THEN 1 ELSE 0 END) AS negative_count,
                           SUM(json_extract(rating,'$.score')) AS net_score
                    FROM feedback_ratings
                """ + where + """
                    GROUP BY target_id
                )
                SELECT r.target, g.rating_count, g.positive_count, g.negative_count, g.net_score
                FROM grouped g JOIN feedback_ratings r ON r.rowid=g.representative
                ORDER BY r.repository, json_extract(r.target,'$.path'), json_extract(r.target,'$.kind'),
                         COALESCE(json_extract(r.target,'$.block_id'),''), r.target_id
                LIMIT ?
                """, (*parameters, limit)).fetchall()
        targets = []
        for row in rows:
            item = json.loads(row["target"])
            item.update({key: row[key] for key in ("rating_count", "positive_count", "negative_count", "net_score")})
            item.update(mean_score=row["net_score"] / row["rating_count"], distinct_tasks=row["rating_count"])
            targets.append(item)
        return {"protocol": PROTOCOL, "repository": repository, "total_targets": counts["targets"],
                "total_ratings": counts["ratings"], "targets": targets, "truncated": counts["targets"] > limit}
