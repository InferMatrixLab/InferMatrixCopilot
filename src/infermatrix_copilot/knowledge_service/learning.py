"""Durable host-model task learning; raw chat is never an API input.

The host distills its own local conversation into bounded positive/negative
lessons. This service freezes owner pages, validates the host's rule proposal,
and applies it only in a provider-owned worktree. It never calls a model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ..sdk.v1.models import (
    KnowledgeEvidenceBatch,
    KnowledgeEvidenceEvent,
    RepositoryRef,
)
from .curation import KnowledgeCurator, KnowledgeValidatorError
from .learning_publication import LearningPublicationError, LearningPublisher
from .lifecycle import Page


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class TaskLearningError(ValueError):
    """The task identity, input, or frozen knowledge packet is invalid."""


class TaskLearningService:
    MAX_LESSONS = 20

    def __init__(self, state_dir, workspace_root=None):
        self.state_dir = Path(state_dir).expanduser().resolve()
        from .learning_config import learning_environment

        configured = workspace_root or learning_environment().get("KNOWLEDGE_LEARNING_WORKSPACE")
        # A source checkout fallback is allowed; installed package data is not
        # a writable canonical knowledge authority.
        self.workspace_root = Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[3]
        if self.state_dir.is_relative_to(self.workspace_root / "knowledge"):
            raise TaskLearningError("learning runtime state must live outside the knowledge tree")
        self.database = self.state_dir / "task-learning.sqlite"

    def _require_agreement(self, action):
        from .agreement import AgreementStore

        return AgreementStore.from_env(state_dir=self.state_dir).require(action)

    @classmethod
    def from_env(cls, workspace_root=None):
        from .learning_config import learning_environment

        state = learning_environment().get("KNOWLEDGE_LEARNING_STATE_DIR")
        return cls(Path(state) if state else Path.home() / ".infermatrix-copilot/learning",
                   workspace_root=workspace_root)

    @contextmanager
    def _db(self):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.database, timeout=60)
        try:
            self.database.chmod(0o600)
            db.execute("PRAGMA busy_timeout=60000")
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS learning_tasks (task_key TEXT PRIMARY KEY, packet TEXT NOT NULL, "
                           "packet_digest TEXT NOT NULL, proposals TEXT, proposals_digest TEXT, result TEXT NOT NULL)")
                yield db
        finally:
            db.close()

    @contextmanager
    def _task_lock(self, key):
        """Serialize one task across processes without blocking other tasks.

        The OS releases this lock on interruption. Durable preparation and
        application receipts remain the authority when the next caller resumes.
        Lock files are never unlinked, which would let waiters lock different
        inodes for the same task.
        """
        from ..trace_store import file_lock

        with file_lock(self.state_dir / "locks" / f"task-{key}.lock", timeout=60) as held:
            if not held:
                raise TaskLearningError("learning task is busy; retry the same task")
            yield

    @staticmethod
    def _task(task_id):
        if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 256:
            raise TaskLearningError("task_id must contain 1..256 characters")
        return _digest(task_id)

    @staticmethod
    def _git(root, *args):
        try:
            done = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True,
                                  timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TaskLearningError(f"learning workspace Git unavailable ({type(exc).__name__})") from exc
        if done.returncode:
            raise TaskLearningError(f"learning workspace Git {args[0]} failed")
        return done.stdout.strip()

    def _source(self):
        root = self.workspace_root
        if self._git(root, "rev-parse", "--show-toplevel") != str(root):
            raise TaskLearningError("learning workspace must be the root of a governed Git checkout")
        if not (root / "knowledge/AGENTS.md").is_file():
            raise TaskLearningError("learning workspace has no governed knowledge tree")
        if self.state_dir.is_relative_to(root / "knowledge"):
            raise TaskLearningError("learning runtime state must live outside the knowledge tree")
        return root

    def _verify_repository(self, source, base, repository):
        """Check the committed slice before persisting a new task identity."""
        try:
            kind = self._git(source, "cat-file", "-t", f"{base}:knowledge/repos/{repository}")
        except TaskLearningError as exc:
            raise TaskLearningError("repository has no verified knowledge slice") from exc
        if kind != "tree":
            raise TaskLearningError("repository has no verified knowledge slice")

    def _correct_invalid_repository(self, packet, result, row, identity):
        """Recover only the exact invalid-slice preparation left by old versions.

        A repository typo used to be detected after persisting identity and
        creating a worktree. Reuse that worktree only when no proposal or
        application exists and it still proves to be the pristine frozen base.
        """
        old_identity = packet.get("identity", {})
        old_repository = old_identity.get("repository")
        if (result != {"status": "preparing"} or packet.get("packet_digest") != ""
                or row[2] is not None or row[3] is not None or row[4] != ""
                or not isinstance(old_repository, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", old_repository)
                or {**old_identity, "repository": identity["repository"]} != identity
                or packet.get("repository") != old_repository
                or packet.get("task_key") != identity["task_key"]
                or packet.get("source_pin") != identity["source_pin"]
                or packet.get("lessons") != identity["lessons"]
                or any(packet.get(field) for field in ("page_hashes", "existing_rules", "prompt"))):
            return None
        base = packet.get("base_sha", "")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base):
            return None
        source = self._source()
        if packet.get("source_workspace") != str(source):
            return None
        # ls-tree distinguishes a genuinely absent path from a Git failure;
        # transient failures must never authorize replacing task identity.
        if self._git(source, "ls-tree", base, "--", f"knowledge/repos/{old_repository}"):
            return None
        self._verify_repository(source, base, identity["repository"])
        checkout = self._checkout(identity["task_key"])
        if checkout.parent.is_symlink() or (checkout.parent.exists() and (
                not checkout.parent.is_dir() or any(path.name != "checkout" for path in checkout.parent.iterdir()))):
            raise TaskLearningError("interrupted preparation has unknown files; preserve it for inspection")
        if checkout.exists() or checkout.is_symlink():
            self._verify_preparing_checkout(packet, checkout)
        return {**packet, "repository": identity["repository"], "identity": identity}

    @classmethod
    def _lessons(cls, lessons):
        if not isinstance(lessons, list) or len(lessons) > cls.MAX_LESSONS:
            raise TaskLearningError(f"lessons must be a list of at most {cls.MAX_LESSONS} distilled lessons")
        result = []
        for item in lessons:
            if not isinstance(item, dict) or set(item) - {"outcome", "summary", "evidence", "changed_paths", "title"}:
                raise TaskLearningError("lesson fields must contain distilled outcomes, never a transcript")
            outcome, summary = item.get("outcome"), item.get("summary")
            if not isinstance(outcome, str) or outcome not in {"positive", "negative"} or not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 4000:
                raise TaskLearningError("each lesson needs a positive/negative outcome and a bounded summary")
            evidence, paths = item.get("evidence", []), item.get("changed_paths", [])
            title = item.get("title", "Task maintenance lesson")
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
                raise TaskLearningError("lesson title must contain 1..200 characters")
            if not isinstance(evidence, list) or len(evidence) > 10 or any(not isinstance(v, str) or not 1 <= len(v) <= 1000 for v in evidence):
                raise TaskLearningError("lesson evidence must contain at most ten bounded references")
            if not isinstance(paths, list) or len(paths) > 30:
                raise TaskLearningError("changed_paths must be a bounded list")
            for value in paths:
                if (not isinstance(value, str) or not value or len(value) > 500 or "\\" in value
                        or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts
                        or re.match(r"^[A-Za-z]:", value) or "\0" in value):
                    raise TaskLearningError("changed_paths must be repository-relative")
            result.append({"outcome": outcome, "summary": summary.strip(), "evidence": evidence,
                           "changed_paths": paths, "title": title.strip()})
        return result

    def _checkout(self, key):
        return self.state_dir / "tasks" / key / "checkout"

    @staticmethod
    def _save(path, value):
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)

    def _clean(self, checkout):
        return not self._git(checkout, "status", "--porcelain", "--untracked-files=all")

    def _batch(self, packet):
        events = tuple(KnowledgeEvidenceEvent(event_id=f"lesson-{i}", source_kind="host_task_learning",
            source_reference=packet["source_references"][i], title=lesson["title"],
            summary=lesson["summary"], changed_paths=tuple(lesson["changed_paths"]),
            attributes={"outcome": lesson["outcome"], "evidence": lesson["evidence"],
                        "source_pin": packet["source_pin"]}) for i, lesson in enumerate(packet["lessons"]))
        return KnowledgeEvidenceBatch(batch_id="chat-" + packet["task_key"][:32],
                                      repository=RepositoryRef(packet["repository"]), events=events)

    @staticmethod
    def _existing_rules(curator, repository):
        """Summaries aid host semantic comparison; they are not duplicate proofs."""
        remaining, documents = 24000, []
        for document in curator.catalog(repository):
            text = curator._document_path(document).read_text(encoding="utf-8")
            rules = Page.parse(text).rules()
            summaries, spent = [], 0
            for rule in rules:
                summary = rule.body_without_footer[:400]
                cost = len(summary)
                if spent + cost > 2400 or cost > remaining:
                    break
                summaries.append({"rule_id": rule.rule_id, "summary": summary,
                                  "truncated": len(summary) < len(rule.body_without_footer)})
                spent += cost
                remaining -= cost
            documents.append({"document_id": document, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                              "rules": summaries, "total_rules": len(rules),
                              "truncated": len(summaries) < len(rules)})
        return documents

    def prepare(self, task_id, repository, source_pin, lessons):
        self._require_agreement("learning_prepare")
        key, lessons = self._task(task_id), self._lessons(lessons)
        from .learning_privacy import validate_distilled_text
        validate_distilled_text(lessons)
        if not isinstance(repository, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", repository):
            raise TaskLearningError("repository must be one verified knowledge repository slug")
        if not isinstance(source_pin, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_pin):
            raise TaskLearningError("source_pin must be a full lowercase Git commit")
        identity = {"task_key": key, "repository": repository, "source_pin": source_pin, "lessons": lessons}
        with self._task_lock(key):
            self._require_agreement("learning_prepare")
            with self._db() as db:
                row = db.execute("SELECT packet,result,proposals,proposals_digest,packet_digest "
                                 "FROM learning_tasks WHERE task_key=?", (key,)).fetchone()
            if row:
                packet, result = json.loads(row[0]), json.loads(row[1])
                if packet["identity"] != identity:
                    packet = self._correct_invalid_repository(packet, result, row, identity)
                    if packet is None:
                        raise TaskLearningError("task already has a different learning packet")
                    with self._db() as db:
                        db.execute("BEGIN IMMEDIATE")
                        db.execute("UPDATE learning_tasks SET packet=? WHERE task_key=?", (_json(packet), key))
                if result["status"] != "preparing":
                    return self._public(packet, result)
            else:
                packet = {**identity, "task_id": task_id, "identity": identity, "source_references": [
                    f"chat-task:{key[:24]}:{i}" for i in range(len(lessons))],
                    "base_sha": "", "page_hashes": {}, "existing_rules": [], "prompt": "", "packet_digest": ""}
                if lessons:
                    source = self._source()
                    base = self._git(source, "rev-parse", "HEAD")
                    self._git(source, "cat-file", "-e", f"{base}:knowledge/AGENTS.md")
                    self._verify_repository(source, base, repository)
                    packet.update(base_sha=base, source_workspace=str(source))
                # Persist intent before creating the worktree; an interrupted
                # prepare can reuse only this exact pristine detached checkout.
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("INSERT INTO learning_tasks VALUES (?,?,?,?,?,?)",
                        (key, _json(packet), "", None, None, _json({"status": "preparing"})))
            if lessons:
                self._verify_repository(Path(packet["source_workspace"]), packet["base_sha"], repository)
                self._finish_prepare(packet)
            packet.pop("packet_digest")
            packet["packet_digest"] = _digest(packet)
            result = {"status": "prepared" if lessons else "no_op"}
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE learning_tasks SET packet=?,packet_digest=?,result=? WHERE task_key=?",
                           (_json(packet), packet["packet_digest"], _json(result), key))
            return self._public(packet, result)

    def _finish_prepare(self, packet):
        self._require_agreement("learning_prepare")
        checkout = self._checkout(packet["task_key"])
        source, base = Path(packet["source_workspace"]), packet["base_sha"]
        checkout.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not checkout.exists():
            self._git(source, "worktree", "add", "--detach", str(checkout), base)
        self._verify_preparing_checkout(packet, checkout)
        if not (checkout / "knowledge/repos" / packet["repository"]).is_dir():
            raise TaskLearningError("repository has no verified knowledge slice")
        curator = KnowledgeCurator(checkout)
        batch = self._batch(packet)
        existing = self._existing_rules(curator, batch.repository)
        packet.update(page_hashes={page: hashlib.sha256((checkout / page).read_bytes()).hexdigest()
                                  for page in curator.catalog(batch.repository)},
            existing_rules=existing,
            prompt=curator.build_prompt(batch) + "\nFrozen existing owner rules (bounded summaries):\n"
                + _json(existing) + "\nCompare meanings and reuse existing rules; propose no duplicate. "
                "Truncation is explicit: if omitted context leaves a possible duplicate unresolved, return no rule. "
                "These summaries do not mechanically prove semantic uniqueness.\n"
                "Preserve positive practices as well as lessons from failures. "
                "Return executable owner rules only. Never quote local chat, paths, secrets, or personal data. "
                "Cite only the opaque source_reference values; evidence is private drafting context.\n")

    def _verify_preparing_checkout(self, packet, checkout):
        source, base = Path(packet["source_workspace"]), packet["base_sha"]
        expected_common = self._git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if (checkout.is_symlink()
                or self._git(checkout, "rev-parse", "--show-toplevel") != str(checkout)
                or self._git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir") != expected_common
                or self._git(checkout, "rev-parse", "HEAD") != base
                or self._git(checkout, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD"
                or not self._clean(checkout)):
            raise TaskLearningError("interrupted preparation workspace differs; preserve it for inspection")

    def _public(self, packet, result):
        return {"task_id": packet["task_id"], "task_key": packet["task_key"], "packet_digest": packet["packet_digest"],
                "repository": packet["repository"], "source_pin": packet["source_pin"],
                "source_references": packet["source_references"], "prompt": packet["prompt"],
                "existing_rules": packet["existing_rules"],
                "outcomes": {kind: sum(l["outcome"] == kind for l in packet["lessons"]) for kind in ("positive", "negative")},
                **result}

    def status(self, task_id):
        if not self.database.exists():
            return {"status": "unknown"}
        with self._db() as db:
            row = db.execute("SELECT packet,result FROM learning_tasks WHERE task_key=?", (self._task(task_id),)).fetchone()
        if not row:
            return {"status": "unknown"}
        return self._public(json.loads(row[0]), json.loads(row[1]))

    def submit(self, task_id, packet_digest, proposals):
        self._require_agreement("learning_submit")
        key = self._task(task_id)
        if not isinstance(packet_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", packet_digest):
            raise TaskLearningError("packet_digest must identify a finished preparation")
        if not isinstance(proposals, dict) or set(proposals) != {"rules"} or not isinstance(proposals["rules"], list):
            raise TaskLearningError("host proposals must contain only a rules list")
        if len(_json(proposals).encode()) > 160000:
            raise TaskLearningError("host proposal batch is too large")
        from .learning_privacy import validate_distilled_text
        validate_distilled_text(proposals)
        proposal_digest = _digest(proposals)
        with self._task_lock(key):
            self._require_agreement("learning_submit")
            # Record exact host output before filesystem/publication effects,
            # then release SQLite's database-wide writer lock. The task file
            # lock alone protects its worktree, receipts and publication.
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT packet,proposals_digest,result FROM learning_tasks WHERE task_key=?", (key,)).fetchone()
                if not row or json.loads(row[0])["packet_digest"] != packet_digest:
                    raise TaskLearningError("unknown or changed learning packet")
                packet, result = json.loads(row[0]), json.loads(row[2])
                if result["status"] == "preparing" or not packet["packet_digest"]:
                    raise TaskLearningError("learning preparation must finish before submission")
                if row[1] and row[1] != proposal_digest:
                    raise TaskLearningError("task already has different host proposals")
                db.execute("UPDATE learning_tasks SET proposals=?,proposals_digest=? WHERE task_key=?", (_json(proposals), proposal_digest, key))
            if result["status"] in {"draft_pr", "rejected", "stale"}:
                return self._public(packet, result)
            if not proposals["rules"]:
                result = {"status": "no_op", "applied": 0}
            elif not packet["lessons"]:
                raise TaskLearningError("an empty lesson packet cannot support rules")
            else:
                result = self._submit(packet, proposals, proposal_digest, result)
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE learning_tasks SET result=? WHERE task_key=?", (_json(result), key))
            return self._public(packet, result)

    def _submit(self, packet, proposals, proposal_digest, previous):
        self._require_agreement("learning_submit")
        checkout = self._checkout(packet["task_key"])
        if not checkout.is_dir() or self._git(checkout, "rev-parse", "--show-toplevel") != str(checkout):
            raise TaskLearningError("provider-owned learning worktree is unavailable")
        receipt_path = checkout.parent / "applied.json"
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
        if receipt:
            if receipt.get("packet_digest") != packet["packet_digest"] or receipt.get("proposals_digest") != proposal_digest:
                raise TaskLearningError("applied learning receipt does not match the task")
            if self._git(checkout, "rev-parse", "HEAD") != receipt["head_sha"] or not self._clean(checkout):
                return {"status": "stale", "reason": "applied task worktree changed"}
            result = receipt["result"]
        else:
            result = self._apply_task(packet, proposals, proposal_digest, checkout)
            if result["status"] != "local":
                return result
            receipt = {"packet_digest": packet["packet_digest"], "proposals_digest": proposal_digest,
                       "head_sha": result["head_sha"], "result": result}
            self._save(receipt_path, receipt)
        try:
            publication = LearningPublisher(state_dir=self.state_dir).publish(checkout, task_digest=packet["task_key"], head=result["head_sha"],
                packet_digest=packet["packet_digest"], rules=result["applied"], base_sha=packet["base_sha"],
                source_pin=packet["source_pin"], source_repository=packet["repository"],
                documents={page: hashlib.sha256((checkout / page).read_bytes()).hexdigest() for page in result["documents"]},
                outcomes={kind: sum(l["outcome"] == kind for l in packet["lessons"]) for kind in ("positive", "negative")})
            return {**result, "status": publication["status"], "publication": publication}
        except LearningPublicationError as exc:
            return {**result, "status": "publication_failed", "publication": {"status": "failed", "reason": str(exc)}}

    def _apply_task(self, packet, proposals, proposal_digest, checkout):
        """Recover only exact provider-generated files/commits, never guess."""
        self._require_agreement("learning_apply")
        intent_path = checkout.parent / "application-intent.json"
        binding = {"packet_digest": packet["packet_digest"], "proposals_digest": proposal_digest,
                   "base_sha": packet["base_sha"]}
        intent = json.loads(intent_path.read_text()) if intent_path.exists() else None
        message = ("knowledge: retain host-distilled maintenance rules\n\n"
                   f"Chat-Learning-Packet: {packet['packet_digest']}\n"
                   f"Chat-Learning-Proposals: {proposal_digest}")
        if intent and any(intent.get(k) != v for k, v in binding.items()):
            raise TaskLearningError("application intent differs from this exact task")
        head = self._git(checkout, "rev-parse", "HEAD")
        if head != packet["base_sha"]:
            if (not intent or not intent.get("tree") or not intent.get("validation")
                    or self._git(checkout, "rev-parse", "HEAD^") != packet["base_sha"]
                    or self._git(checkout, "rev-parse", "HEAD^{tree}") != intent["tree"]
                    or self._git(checkout, "log", "-1", "--format=%B") != message
                    or not self._clean(checkout)):
                raise TaskLearningError("unrecorded task commit differs; preserve it for inspection")
            return {"status": "local", "applied": intent["validation"]["applied"], "head_sha": head,
                    "documents": intent["documents"], "workspace": str(checkout), "validation": intent["validation"]}
        if intent and not self._clean(checkout):
            dirty = set(self._git(checkout, "diff", "--name-only", "HEAD").splitlines())
            untracked = self._git(checkout, "ls-files", "--others", "--exclude-standard")
            if untracked or dirty - set(intent["documents"]):
                return {"status": "stale", "reason": "unexpected files changed during interrupted application"}
            for page in intent["documents"]:
                path = checkout / page
                allowed = {hashlib.sha256(intent["originals"][page].encode()).hexdigest(), intent["expected_hashes"][page]}
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() not in allowed:
                    return {"status": "stale", "reason": "interrupted application has ambiguous owner edits"}
                staged = self._git(checkout, "ls-files", "--stage", "--", page).split()
                if (len(staged) < 3 or staged[2] != "0"
                        or [staged[0], staged[1]] not in intent.get("allowed_index", {}).get(page, [])):
                    return {"status": "stale", "reason": "interrupted application has ambiguous staged owner edits"}
            # These bytes are proved to be either the frozen original or the
            # exact attempted output. Rewind only these owned paths, then rerun
            # the same curator/validators; unknown user changes are retained.
            self._require_agreement("learning_apply")
            self._git(checkout, "restore", "--source", packet["base_sha"], "--staged", "--worktree", "--", *intent["documents"])
        if not self._clean(checkout):
            return {"status": "stale", "reason": "provider worktree contains unexpected edits or files"}
        if self._git(Path(packet["source_workspace"]), "rev-parse", "HEAD") != packet["base_sha"]:
            return {"status": "stale", "reason": "knowledge source advanced after the packet was frozen"}
        for page, digest in packet["page_hashes"].items():
            if hashlib.sha256((checkout / page).read_bytes()).hexdigest() != digest:
                return {"status": "stale", "reason": "owner page changed after packet preparation"}
        curator = KnowledgeCurator(checkout)
        validation = curator.validate_proposals(proposals, self._batch(packet))
        if validation.rejected:
            return {"status": "rejected", "rejected": [r.to_dict() for r in validation.rejected], "applied": 0}
        updated_on = intent["updated_on"] if intent else datetime.now(UTC).date().isoformat()
        documents = sorted({p.page_document_id for p in validation.accepted})
        originals = {page: (checkout / page).read_text(encoding="utf-8") for page in documents}
        expected = {page: curator._updated_page(originals[page], [
            p.section_markdown for p in validation.accepted if p.page_document_id == page], updated_on) for page in documents}
        object_format = self._git(checkout, "rev-parse", "--show-object-format")
        def blob_id(text):
            data = text.encode()
            return hashlib.new(object_format, b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        intent = {**binding, "updated_on": updated_on, "documents": documents, "originals": originals,
                  "expected_hashes": {page: hashlib.sha256(text.encode()).hexdigest() for page, text in expected.items()},
                  "allowed_index": {page: [[self._git(checkout, "ls-files", "--stage", "--", page).split()[0],
                                            blob_id(text)] for text in (originals[page], expected[page])] for page in documents}}
        self._require_agreement("learning_apply")
        self._save(intent_path, intent)
        try:
            self._require_agreement("learning_apply")
            applied = curator.apply(validation, updated_on=updated_on)
        except KnowledgeValidatorError as exc:
            return {"status": "validator_failed", "applied": 0, "validation": exc.result.to_dict()}
        dirty = set(self._git(checkout, "diff", "--name-only", "HEAD").splitlines())
        if dirty != set(documents) or self._git(checkout, "ls-files", "--others", "--exclude-standard"):
            return {"status": "stale", "reason": "validators changed files outside the approved rule pages"}
        if any(hashlib.sha256((checkout / page).read_bytes()).hexdigest() != digest
               for page, digest in intent["expected_hashes"].items()):
            return {"status": "stale", "reason": "applied owner bytes differ from the frozen proposal"}
        self._require_agreement("learning_apply")
        self._git(checkout, "add", "--", *documents)
        intent.update(tree=self._git(checkout, "write-tree"), validation=applied.to_dict())
        self._save(intent_path, intent)
        self._require_agreement("learning_apply")
        self._git(checkout, "-c", "user.name=Knowledge Learning", "-c", "user.email=knowledge-learning@localhost",
                  "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-m", message)
        return {"status": "local", "applied": applied.applied, "head_sha": self._git(checkout, "rev-parse", "HEAD"),
                "documents": documents, "workspace": str(checkout), "validation": applied.to_dict()}
