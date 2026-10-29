"""Provider-bound knowledge feedback, stored outside the product knowledge tree.

Only trusted delivery code calls ``issue_delivery``. Public callers can rate an
issued target, but cannot turn an arbitrary path or hash into evidence of use.
The ledger retains hashes and feedback metadata, never delivered source text.
It operates independently of the optional knowledge containment policy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from ..knowledge_view import KnowledgeView, KnowledgeViewError
from .agreement import AgreementRequiredError, AgreementStore

PROTOCOL = "knowledge-feedback/1"


class FeedbackError(ValueError):
    """Invalid delivery, feedback, or reuse of an immutable feedback identity."""


def model_content_documents(content: str) -> list[dict]:
    """Extract only exact provider-rendered adaptive source fragments.

    Adaptive rendering escapes literal ``<`` inside JSON values. Parsing each
    complete fence therefore never mistakes quoted source text for a delivery.
    This helper does not issue a receipt and must receive trusted provider output.
    """
    if not isinstance(content, str):
        raise FeedbackError("model content must be a string")
    documents = []
    for match in re.finditer(r"\n<untrusted_data>\n(.*?)\n</untrusted_data>\n", content, re.DOTALL):
        try:
            fragment = json.loads(match[1])
        except (ValueError, TypeError) as exc:
            raise FeedbackError("adaptive source fragment is not valid JSON") from exc
        if isinstance(fragment, dict) and isinstance(fragment.get("path"), str) \
                and isinstance(fragment.get("content"), str):
            documents.append({"path": fragment["path"], "content": fragment["content"]})
    return documents


def _json(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FeedbackError("feedback metadata must be finite JSON") from exc


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value, name, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise FeedbackError(f"{name} must be a nonempty string of at most {maximum} characters")
    return value.strip()


def _evidence(value):
    def has_text(node):
        if isinstance(node, str):
            return bool(node.strip())
        if isinstance(node, list):
            return bool(node) and all(has_text(item) for item in node)
        if isinstance(node, dict):
            return bool(node) and any(has_text(item) for item in node.values())
        return False

    if not isinstance(value, (str, list, dict)) or not has_text(value):
        raise FeedbackError("evidence must contain concrete nonempty references or observations")
    encoded = _json(value)
    if len(encoded.encode("utf-8")) > 8192:
        raise FeedbackError("evidence exceeds 8192 bytes; provide references instead of transcripts")
    return json.loads(encoded)


class KnowledgeFeedbackStore:
    _feedback_sync_enabled = True

    def __init__(self, state_dir: Path, *, sync_config=None, sync_configuration_error=False):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.path = self.state_dir / "feedback.sqlite3"
        self.sync_config = sync_config
        self._sync_configuration_error = sync_configuration_error

    def _agreement(self):
        return AgreementStore.from_env(state_dir=self.state_dir,
                                       feedback_url=self.sync_config.url if self.sync_config else None)

    def _require_agreement(self, action):
        if self._feedback_sync_enabled:
            return self._agreement().require(action)
        return None

    def _inspection_rows(self, query, parameters=()):
        """Inspect existing state without creating a ledger or migrating it."""
        if not self.path.exists():
            return []
        if self.path.is_symlink():
            raise FeedbackError("feedback database cannot be a symbolic link")
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            try:
                return db.execute(query, parameters).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise
        finally:
            db.close()

    def _agreement_blocked_status(self):
        totals = self._inspection_rows("SELECT COUNT(*) FROM feedback_ratings")
        total = totals[0][0] if totals else 0
        destination = self._destination_hash()
        rows = self._inspection_rows("SELECT COUNT(*) FROM feedback_outbox WHERE acknowledged=1 "
                                     "AND destination_hash=?", (destination,)) if destination else []
        acknowledged = rows[0][0] if rows else 0
        return {"status": "agreement_required", "configured": self.is_sync_configured,
                "pending_count": max(total - acknowledged, 0), "acknowledged_count": acknowledged}

    @classmethod
    def from_env(cls, *, state_dir=None):
        from .feedback_sync import FeedbackSyncConfig, FeedbackSyncError
        from .learning_config import learning_environment

        environ = learning_environment()
        configured = environ.get("KNOWLEDGE_LEARNING_STATE_DIR", "").strip()
        try:
            sync_config = FeedbackSyncConfig.from_environment(environ)
            invalid = False
        except FeedbackSyncError:
            sync_config, invalid = None, True
        storage = (Path(state_dir) if state_dir is not None else Path(configured) if configured
                   else Path.home() / ".infermatrix-copilot" / "learning")
        return cls(storage,
                   sync_config=sync_config, sync_configuration_error=invalid)

    @contextmanager
    def _db(self):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise FeedbackError("feedback database cannot be a symbolic link")
        def secure_file(path, *, create=False):
            if path.is_symlink():
                raise FeedbackError("feedback database files cannot be symbolic links")
            flags = (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            try:
                if path.is_symlink():
                    raise FeedbackError("feedback database files cannot be symbolic links")
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o600)
                else:
                    os.chmod(path, 0o600)
            finally:
                os.close(descriptor)

        secure_file(self.path, create=True)

        def secure_sidecars():
            for suffix in ("-wal", "-shm"):
                try:
                    secure_file(Path(str(self.path) + suffix))
                except FileNotFoundError:
                    continue

        secure_sidecars()
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=30000")
            deadline = time.monotonic() + 30
            while True:
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if (not any(code in str(exc).lower() for code in ("locked", "busy"))
                            or time.monotonic() >= deadline):
                        raise
                    time.sleep(0.01)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS feedback_deliveries (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    repository TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS feedback_ratings (
                    task_id TEXT NOT NULL, target_id TEXT NOT NULL,
                    repository TEXT NOT NULL, rating TEXT NOT NULL,
                    target TEXT NOT NULL, delivery_id TEXT NOT NULL,
                    created_at REAL NOT NULL, PRIMARY KEY (task_id, target_id));
                CREATE INDEX IF NOT EXISTS feedback_ratings_by_repository
                    ON feedback_ratings(repository, target_id);
                CREATE INDEX IF NOT EXISTS feedback_deliveries_by_task
                    ON feedback_deliveries(task_id, repository);
                CREATE TABLE IF NOT EXISTS feedback_outbox (
                    feedback_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    target_id TEXT NOT NULL, delivery_id TEXT NOT NULL,
                    destination_hash TEXT NOT NULL DEFAULT '',
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt REAL, acknowledged_at REAL, last_error TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS feedback_outbox_identity
                    ON feedback_outbox(task_id,target_id);
                CREATE TABLE IF NOT EXISTS feedback_sync_meta (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            secure_sidecars()
            with db:
                yield db
        finally:
            db.close()

    def repositories_for_task(self, task_id: str) -> tuple[str, ...]:
        """Resolve existing task context without rewriting any issued receipt."""
        task_id = _text(task_id, "task_id", 512)
        rows = self._inspection_rows("SELECT DISTINCT repository FROM feedback_deliveries "
                                     "WHERE task_id=? ORDER BY repository", (task_id,))
        return tuple(row["repository"] for row in rows)

    def issue_delivery(self, *, task_id: str, repository: str, view: KnowledgeView,
                       documents: list[dict]) -> dict:
        """Issue metadata for content actually delivered by trusted provider code.

        Documents carry ``path``/``document_id`` and ``content``/``excerpt``/
        ``text``. Rendered provider excerpts may include presentation wrappers.
        A rule is eligible only when its complete source section was delivered.
        """
        try:
            self._require_agreement("feedback_delivery")
        except AgreementRequiredError:
            return {"status": "agreement_required", "protocol": PROTOCOL, "targets": []}
        task_id = _text(task_id, "task_id", 512)
        repository = _text(repository, "repository", 256)
        root = view.root.resolve()
        if self.state_dir.is_relative_to(root) or self.path.resolve().is_relative_to(root):
            raise FeedbackError("feedback state must live outside the knowledge tree")
        if not isinstance(documents, list) or len(documents) > 256:
            raise FeedbackError("documents must be a list of at most 256 delivered fragments")
        from ..kb_service.maintenance_units import page_units

        targets, fragments = {}, {}
        delivered_bytes = 0
        for document in documents:
            if not isinstance(document, dict):
                raise FeedbackError("each delivered document must be an object")
            supplied = document.get("path", document.get("document_id"))
            if not isinstance(supplied, str) or not supplied.strip():
                raise FeedbackError("each delivered document requires a path")
            content = next((document[key] for key in ("content", "excerpt", "text")
                            if key in document), None)
            if not isinstance(content, str):
                raise FeedbackError("each delivered document requires string content")
            try:
                relative = view.relative(supplied) if Path(supplied).is_absolute() else supplied
                path = view.path(relative)
                relative = path.relative_to(root).as_posix()
                if path.suffix.casefold() != ".md":
                    raise FeedbackError("feedback delivery requires a Markdown document")
                data = path.read_bytes()
                source = data.decode("utf-8")
            except (ValueError, OSError, KnowledgeViewError) as exc:
                raise FeedbackError(f"invalid knowledge document: {supplied}") from exc
            if not content.strip():
                continue
            delivered_bytes += len(content.encode("utf-8"))
            if delivered_bytes > 16_000_000:
                raise FeedbackError("delivered fragments exceed 16000000 bytes")
            page_sha = hashlib.sha256(data).hexdigest()
            delivered_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            fragment = {"path": relative, "page_sha256": page_sha,
                        "delivered_sha256": delivered_sha, "delivered_chars": len(content)}
            fragments[_digest(fragment)] = fragment
            common = {"repository": repository, "path": relative,
                      "page_sha256": page_sha, "snapshot": view.public_snapshot}
            version = {"repository": repository, "path": relative,
                       "kind": "document", "content_sha256": page_sha}
            target_id = _digest(version)
            targets[target_id] = {**common, **version, "target_id": target_id}
            parts = Path(relative).parts
            owner_repo = parts[1] if len(parts) > 2 and parts[0] == "repos" else "general"
            for unit in page_units(relative, source, owner_repo, view.public_snapshot):
                if unit["kind"] != "rule" or unit["structural_issues"]:
                    continue
                section = unit["text"].strip()
                if not section or section not in content:
                    continue
                version = {"repository": repository, "path": relative, "kind": "rule",
                           "unit_id": unit["unit_id"], "block_id": unit["block_id"],
                           "content_sha256": unit["content_sha256"],
                           "upstream_pins": sorted(unit["upstream_pins"])}
                target_id = _digest(version)
                targets[target_id] = {**common, **version, "target_id": target_id}
        body = {"protocol": PROTOCOL, "task_id": task_id, "repository": repository,
                "snapshot": view.public_snapshot, "tree_sha256": view.tree_sha256,
                "documents": sorted(fragments.values(), key=lambda row: (row["path"], row["delivered_sha256"])),
                "targets": sorted(targets.values(), key=lambda row: (row["path"], row["kind"], row.get("block_id", ""), row["target_id"]))}
        receipt = {**body, "delivery_id": _digest(body)}
        encoded = _json(receipt)
        try:
            self._require_agreement("feedback_delivery")
        except AgreementRequiredError:
            return {"status": "agreement_required", "protocol": PROTOCOL, "targets": []}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM feedback_deliveries WHERE id=?", (receipt["delivery_id"],)).fetchone()
            if row and row["payload"] != encoded:
                raise FeedbackError("delivery identity conflicts with existing issuance")
            db.execute("INSERT OR IGNORE INTO feedback_deliveries VALUES (?,?,?,?)",
                       (receipt["delivery_id"], task_id, repository, encoded))
        return receipt

    def record(self, *, task_id, delivery_id, ratings: list[dict]) -> dict:
        self._require_agreement("feedback_record")
        task_id = _text(task_id, "task_id", 512)
        delivery_id = _text(delivery_id, "delivery_id", 64)
        if not isinstance(ratings, list) or len(ratings) > 256:
            raise FeedbackError("ratings must be a list of at most 256 entries")
        result = []
        self._require_agreement("feedback_record")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM feedback_deliveries WHERE id=?", (delivery_id,)).fetchone()
            if row is None or row["task_id"] != task_id:
                raise FeedbackError("delivery was not issued for this task")
            receipt = json.loads(row["payload"])
            body = {key: value for key, value in receipt.items() if key != "delivery_id"}
            if receipt.get("delivery_id") != delivery_id or _digest(body) != delivery_id \
                    or receipt.get("task_id") != task_id or receipt.get("repository") != row["repository"]:
                raise FeedbackError("stored delivery integrity check failed")
            eligible = {target["target_id"]: target for target in receipt["targets"]}
            seen = set()
            for rating in ratings:
                if not isinstance(rating, dict):
                    raise FeedbackError("each rating must be an object")
                target_id = rating.get("target_id")
                if not isinstance(target_id, str) or target_id not in eligible:
                    raise FeedbackError("target was not delivered for this task")
                if target_id in seen:
                    raise FeedbackError("a feedback batch cannot repeat a target")
                seen.add(target_id)
                if type(rating.get("score")) is not int or rating["score"] not in (-1, 1):
                    raise FeedbackError("score must be the integer -1 or 1")
                if rating.get("used") is not True:
                    raise FeedbackError("feedback requires used=true for knowledge actually used")
                canonical = {"target_id": target_id, "score": rating["score"], "used": True,
                             "reason": _text(rating.get("reason"), "reason"),
                             "evidence": _evidence(rating.get("evidence"))}
                encoded = _json(canonical)
                existing = db.execute("SELECT rating,target,delivery_id FROM feedback_ratings WHERE task_id=? AND target_id=?",
                                      (task_id, target_id)).fetchone()
                if existing and existing["rating"] != encoded:
                    raise FeedbackError("feedback for this task and content version is immutable")
                target = json.loads(existing["target"]) if existing else eligible[target_id]
                if not existing:
                    db.execute("INSERT INTO feedback_ratings VALUES (?,?,?,?,?,?,?)",
                               (task_id, target_id, row["repository"], encoded, _json(target), delivery_id, time.time()))
                # Bind retries to the first immutable delivery, even if the same
                # rule was read again under a different receipt or snapshot.
                if self._feedback_sync_enabled:
                    db.execute("INSERT OR IGNORE INTO feedback_outbox "
                               "(feedback_id,task_id,target_id,delivery_id) VALUES (?,?,?,?)",
                               (_digest([task_id, target_id]), task_id, target_id,
                                existing["delivery_id"] if existing else delivery_id))
                result.append({**canonical, "feedback_id": _digest([task_id, target_id]), "target": target})
        accepted = {"task_id": task_id, "delivery_id": delivery_id, "ratings": result}
        if not self._feedback_sync_enabled:
            return accepted
        # Local acceptance and the outbox commit precede all network activity.
        # Flush is deliberately bounded; further work remains durable for retry.
        try:
            sync = self.sync_pending(limit=min(max(len(result), 1), 10))
        except (OSError, sqlite3.Error, FeedbackError):
            sync = {"status": "pending", "last_error": "local_sync_unavailable"}
        return {**accepted, "sync": sync}

    @property
    def is_sync_configured(self) -> bool:
        return self.sync_config is not None or self._sync_configuration_error

    def _destination_hash(self) -> str:
        return _digest(self.sync_config.url) if self.sync_config else ""

    def _backfill_outbox(self, db):
        if not self._feedback_sync_enabled or db.execute(
                "SELECT 1 FROM feedback_sync_meta WHERE name='initial_backfill'").fetchone():
            return
        rows = db.execute("SELECT r.task_id,r.target_id,r.delivery_id FROM feedback_ratings r "
                          "LEFT JOIN feedback_outbox o ON r.task_id=o.task_id AND r.target_id=o.target_id "
                          "WHERE o.feedback_id IS NULL").fetchall()
        for row in rows:
            db.execute("INSERT OR IGNORE INTO feedback_outbox "
                       "(feedback_id,task_id,target_id,delivery_id) VALUES (?,?,?,?)",
                       (_digest([row["task_id"], row["target_id"]]), row["task_id"],
                        row["target_id"], row["delivery_id"]))
        db.execute("INSERT OR IGNORE INTO feedback_sync_meta VALUES ('initial_backfill','1')")

    def sync_status(self) -> dict:
        """Return durable queue counts without contacting the collector."""
        try:
            self._require_agreement("feedback_status")
        except AgreementRequiredError:
            return self._agreement_blocked_status()
        destination = self._destination_hash()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._backfill_outbox(db)
            total = db.execute("SELECT COUNT(*) FROM feedback_outbox").fetchone()[0]
            acknowledged = db.execute("SELECT COUNT(*) FROM feedback_outbox "
                                      "WHERE acknowledged=1 AND destination_hash=?",
                                      (destination,)).fetchone()[0] if destination else 0
            last = db.execute("SELECT last_error FROM feedback_outbox WHERE last_error IS NOT NULL "
                              "ORDER BY last_attempt DESC LIMIT 1").fetchone()
        pending = total - acknowledged
        status = ("configuration_error" if self._sync_configuration_error else "disabled"
                  if not self.sync_config else "pending" if pending else "acknowledged")
        result = {"status": status, "configured": self.is_sync_configured,
                  "pending_count": pending, "acknowledged_count": acknowledged}
        if last:
            result["last_error"] = last[0]
        return result

    def sync_pending(self, limit=100) -> dict:
        """Best-effort bounded replay; failures never remove accepted feedback."""
        from .feedback_sync import FeedbackSyncClient, FeedbackSyncError

        try:
            self._require_agreement("feedback_sync")
        except AgreementRequiredError:
            return self._agreement_blocked_status()
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise FeedbackError("sync limit must be between 1 and 1000")
        if not self.sync_config:
            return self.sync_status()
        destination = self._destination_hash()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._backfill_outbox(db)
            rows = db.execute("SELECT * FROM feedback_outbox WHERE acknowledged=0 OR destination_hash<>? "
                              "ORDER BY COALESCE(last_attempt,0),feedback_id LIMIT ?",
                              (destination, limit)).fetchall()
        client = FeedbackSyncClient(self.sync_config, state_dir=self.state_dir)
        attempted = succeeded = 0
        # One flush has a wall-time budget as well as an event-count limit.
        deadline = time.monotonic() + 30
        for event in rows:
            if time.monotonic() >= deadline:
                break
            try:
                self._require_agreement("feedback_sync")
            except AgreementRequiredError:
                return {**self._agreement_blocked_status(), "attempted_count": attempted,
                        "acknowledged_this_run": succeeded}
            attempted += 1
            error = None
            try:
                with self._db() as db:
                    rating = db.execute("SELECT * FROM feedback_ratings WHERE task_id=? AND target_id=?",
                                        (event["task_id"], event["target_id"])).fetchone()
                    delivery = db.execute("SELECT * FROM feedback_deliveries WHERE id=?",
                                          (event["delivery_id"],)).fetchone()
                if (not rating or not delivery or rating["delivery_id"] != event["delivery_id"]
                        or delivery["task_id"] != event["task_id"]):
                    raise FeedbackError("invalid local feedback event")
                receipt = json.loads(delivery["payload"])
                body = {key: value for key, value in receipt.items() if key != "delivery_id"}
                canonical, target = json.loads(rating["rating"]), json.loads(rating["target"])
                if (receipt.get("delivery_id") != event["delivery_id"]
                        or _digest(body) != event["delivery_id"]
                        or receipt.get("task_id") != event["task_id"]
                        or target not in receipt.get("targets", [])
                        or canonical.get("target_id") != event["target_id"]
                        or _digest([event["task_id"], event["target_id"]]) != event["feedback_id"]):
                    raise FeedbackError("invalid local feedback event")
                client.send(receipt=receipt, rating=canonical, target=target,
                            feedback_id=event["feedback_id"])
                succeeded += 1
            except AgreementRequiredError:
                return {**self._agreement_blocked_status(), "attempted_count": attempted,
                        "acknowledged_this_run": succeeded}
            except FeedbackSyncError as exc:
                error = (str(exc) if str(exc) in {"privacy_blocked", "feedback_conflict", "authentication_failed"}
                         else "collector_unavailable")
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                error = "invalid_local_event"
            with self._db() as db:
                if error is None:
                    db.execute("UPDATE feedback_outbox SET destination_hash=?,acknowledged=1,"
                               "attempts=attempts+1,last_attempt=?,acknowledged_at=?,last_error=NULL "
                               "WHERE feedback_id=?",
                               (destination, time.time(), time.time(), event["feedback_id"]))
                else:
                    # A failed concurrent attempt cannot revoke a valid ack.
                    db.execute("UPDATE feedback_outbox SET "
                               "destination_hash=CASE WHEN acknowledged=1 THEN destination_hash ELSE ? END,"
                               "attempts=attempts+1,last_attempt=?,"
                               "last_error=CASE WHEN acknowledged=1 AND destination_hash=? "
                               "THEN last_error ELSE ? END WHERE feedback_id=?",
                               (destination, time.time(), destination, error, event["feedback_id"]))
        return {**self.sync_status(), "attempted_count": attempted,
                "acknowledged_this_run": succeeded}

    def central_report(self, repository="", limit=100) -> dict:
        from .feedback_sync import FeedbackSyncClient, FeedbackSyncError

        self._require_agreement("feedback_report")
        if not isinstance(repository, str) or type(limit) is not int or not 1 <= limit <= 1000:
            raise FeedbackError("report requires a repository string and limit between 1 and 1000")
        if not self.sync_config:
            raise FeedbackError("knowledge feedback collector is not configured correctly")
        try:
            return {**FeedbackSyncClient(self.sync_config, state_dir=self.state_dir).report(repository=repository.strip(), limit=limit),
                    "scope": "central"}
        except FeedbackSyncError:
            raise FeedbackError("knowledge feedback collector report is unavailable") from None

    def report_for_scope(self, repository="", limit=100, scope="auto") -> dict:
        """A configured but unavailable collector must never appear as local totals."""
        if scope not in {"auto", "local", "central"}:
            raise FeedbackError("report scope must be auto, local, or central")
        if scope == "central" or (scope == "auto" and self.is_sync_configured):
            return self.central_report(repository=repository, limit=limit)
        return {**self.report(repository=repository, limit=limit), "scope": "local"}

    def report(self, repository="", limit=100) -> dict:
        if not isinstance(repository, str) or type(limit) is not int or not 1 <= limit <= 1000:
            raise FeedbackError("report requires a repository string and limit between 1 and 1000")
        repository = repository.strip()
        rows = self._inspection_rows("SELECT * FROM feedback_ratings" + (" WHERE repository=?" if repository else ""),
                                     (repository,) if repository else ())
        grouped = {}
        for row in rows:
            rating, target = json.loads(row["rating"]), json.loads(row["target"])
            item = grouped.setdefault(row["target_id"], {**target, "positive_count": 0,
                                      "negative_count": 0, "rating_count": 0, "net_score": 0})
            item["positive_count" if rating["score"] > 0 else "negative_count"] += 1
            item["rating_count"] += 1
            item["net_score"] += rating["score"]
        for item in grouped.values():
            item["mean_score"] = item["net_score"] / item["rating_count"]
            item["distinct_tasks"] = item["rating_count"]
        targets = sorted(grouped.values(), key=lambda row: (row["repository"], row["path"],
                         row["kind"], row.get("block_id", ""), row["target_id"]))
        return {"protocol": PROTOCOL, "repository": repository, "total_targets": len(targets),
                "total_ratings": len(rows), "targets": targets[:limit], "truncated": len(targets) > limit}


def from_env() -> KnowledgeFeedbackStore:
    return KnowledgeFeedbackStore.from_env()
