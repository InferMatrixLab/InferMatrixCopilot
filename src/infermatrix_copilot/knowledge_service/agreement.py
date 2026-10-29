"""Explicit, versioned local acknowledgement for optional task learning.

This is a product disclosure and named acknowledgement, not a claim about legal
enforceability or independently verified identity. Only the interactive CLI
calls the private acceptance method; ordinary APIs can inspect or revoke it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

AGREEMENT_VERSION = "2026-10-29.1"
AGREEMENT_PROTOCOL = "knowledge-learning-agreement/1"
AGREEMENT_TEXT = """InferMatrixCopilot optional learning and feedback disclosure

Learning and knowledge feedback are disabled until you explicitly acknowledge
this version of the disclosure. Signing enables the optional learning and
feedback workflow for this local operating-system user and the destinations
shown below. Ordinary knowledge reading remains available without signing.

1. Extra model work and cost. At task completion or on an explicit learning
request, your current host agent may inspect relevant conversation context,
distill reusable positive and negative lessons, evaluate knowledge actually
used, and draft rule proposals. This consumes additional host-model tokens,
execution time and possibly paid model/API quota. Cost depends on your host,
model and account; no fixed charge or free allowance is promised.

2. Selected conversation-derived data. Lessons, evidence references, rating
reasons, outcomes and task identifiers can contain personal, proprietary or
confidential information from your conversation or work. The workflow selects
bounded summaries and evidence instead of uploading raw chat histories or
complete command logs by design. You and your agent must select and redact
appropriate material. Automated checks do not guarantee perfect redaction or
detection of every secret, personal detail or confidential fact.

3. Private local storage. Selected lessons, evidence, delivery metadata,
feedback, upload queues and this named acknowledgement are retained in private
local runtime state outside the product knowledge tree. Local retention is
separate from publication or transmission to another service.

4. Knowledge pull requests. When draft-PR publication and its existing write
gates are configured, distilled rules and their selected evidence/provenance
may be sent to the configured GitHub repository and branch shown below. A
public repository exposes contributions publicly; a private repository exposes
them to its authorized collaborators and service operators. PRs require the
existing review process and are not automatically merged by this workflow.

5. Central feedback. When a collector URL is configured, knowledge-version
metadata, task identifiers, +1/-1 ratings, reasons and selected evidence are
sent to that collector for team aggregation. Its operators and authorized
readers may access stored feedback. Signing does not replace your own review
of the destination's access, retention or data-handling policies. Credentials
are deployment configuration and are not part of the acknowledgement or
feedback payload.

6. Choice and revocation. You may decline, keep the workflow disabled, inspect
the current acknowledgement, or revoke it later. Changed disclosure terms or
configured publication/collector destinations require a new acknowledgement.
Revocation prevents future learning, grading and uploads after the next gate
check. It does not erase data already stored locally or sent elsewhere, and
an operation already in flight may complete. Remove prior data through the
relevant local storage, GitHub or collector administration process.

By entering your name and the exact words I AGREE in the interactive terminal,
you acknowledge these disclosures and enable this optional workflow for the
displayed configuration. This is a named product acknowledgement; it does not
assert a verified legal signature or a particular jurisdiction's legal effect.
"""
AGREEMENT_SHA256 = hashlib.sha256(AGREEMENT_TEXT.encode("utf-8")).hexdigest()
AGREEMENT_SHA = AGREEMENT_SHA256
_MAX_RECORD_BYTES = 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_MESSAGE = "Learning and feedback require your current interactive agreement; run infermatrix-copilot learning agreement."


class AgreementRequiredError(ValueError):
    code = "agreement_required"

    def __init__(self):
        super().__init__(_MESSAGE)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp(value):
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
        raise ValueError("invalid acknowledgement timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("invalid acknowledgement timestamp")
    if parsed > datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5):
        raise ValueError("invalid acknowledgement timestamp")
    return value


def _context(value):
    fields = {"publication_mode", "publication_repository", "publication_base", "feedback_url"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("invalid agreement configuration")
    if any(not isinstance(item, str) or len(item) > 2048 or item != item.strip()
           or any(ord(char) < 32 or ord(char) == 127 for char in item) for item in value.values()):
        raise ValueError("invalid agreement configuration")
    if value["publication_mode"] not in {"local", "draft_pr"}:
        raise ValueError("invalid agreement configuration")
    repository = value["publication_repository"]
    if repository and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid agreement configuration")
    base = value["publication_base"]
    if (not base or base.startswith(("-", "/")) or base.endswith(("/", "."))
            or any(char.isspace() or char in "~^:?*[\\" for char in base)
            or ".." in base or "@{" in base or "//" in base):
        raise ValueError("invalid agreement configuration")
    url = value["feedback_url"]
    if url:
        from .feedback_sync import FeedbackSyncConfig

        # Reuse the transport's destination validation without reading a token.
        url = FeedbackSyncConfig(url, "x" * 32).url
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None:
            raise ValueError("invalid agreement configuration")
    from .learning_privacy import validate_distilled_text

    validate_distilled_text(value)
    return {**value, "feedback_url": url}


def _environment_context(environ, feedback_url=None):
    return _context({
        "publication_mode": environ.get("KNOWLEDGE_LEARNING_PUBLICATION", "local").strip(),
        "publication_repository": environ.get("KNOWLEDGE_LEARNING_REPOSITORY", "").strip(),
        "publication_base": environ.get("KNOWLEDGE_LEARNING_BASE", "main").strip(),
        "feedback_url": (environ.get("KNOWLEDGE_FEEDBACK_URL", "") if feedback_url is None else feedback_url).strip(),
    })


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate acknowledgement field")
        result[key] = value
    return result


class AgreementStore:
    def __init__(self, state_dir, *, context=None):
        supplied = Path(state_dir).expanduser().absolute()
        # HOME and parent aliases can legitimately be symlinks. Canonicalize
        # them exactly as runtime state does, while refusing an explicit state
        # leaf symlink and all acknowledgement-owned symlink entries.
        self._unsafe_state_alias = False
        try:
            self._unsafe_state_alias = supplied.is_symlink()
            self.state_dir = supplied.resolve()
        except (OSError, RuntimeError, ValueError):
            # An optional agreement path must fail closed without breaking
            # ordinary knowledge reads. Never mutate this unresolved path.
            self._unsafe_state_alias = True
            self.state_dir = supplied
        self.directory = self.state_dir / "agreement"
        self.path = self.directory / "acceptance.json"
        self._context_source = None
        self._context_error = False
        try:
            self.context = _context(context) if context is not None else _environment_context({})
        except (ValueError, TypeError, AttributeError):
            self.context, self._context_error = {}, True

    @classmethod
    def from_env(cls, *, state_dir=None, environ=None, feedback_url=None):
        from .learning_config import learning_environment

        values = learning_environment() if environ is None else dict(environ)
        configured = values.get("KNOWLEDGE_LEARNING_STATE_DIR", "").strip()
        storage = state_dir if state_dir is not None else configured or Path.home() / ".infermatrix-copilot" / "learning"
        try:
            context = _environment_context(values, feedback_url)
        except (ValueError, TypeError, AttributeError):
            context = {}
        instance = cls(storage, context=context)
        if environ is None:
            instance._context_source = lambda: _environment_context(learning_environment(), feedback_url)
        return instance

    def _current_context(self):
        if self._context_error:
            raise AgreementRequiredError()
        try:
            return _context(self._context_source() if self._context_source else self.context)
        except (ValueError, TypeError, AttributeError):
            raise AgreementRequiredError() from None

    def document(self):
        context = self._current_context()
        return {"version": AGREEMENT_VERSION, "text": AGREEMENT_TEXT, "sha256": AGREEMENT_SHA256,
                "context": context, "context_sha256": _digest(context)}

    def _safe_paths(self):
        if self._unsafe_state_alias:
            raise ValueError("unsafe acknowledgement state")
        for path in (self.state_dir, self.directory, self.path,
                     self.directory / "agreement.lock"):
            if path.is_symlink():
                raise ValueError("unsafe acknowledgement state")

    @staticmethod
    def _private(path, *, directory=False):
        metadata = path.stat()
        if (directory and not stat.S_ISDIR(metadata.st_mode)) or (not directory and not stat.S_ISREG(metadata.st_mode)):
            raise ValueError("unsafe acknowledgement state")
        if os.name != "nt" and metadata.st_mode & 0o077:
            raise ValueError("unsafe acknowledgement permissions")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ValueError("acknowledgement belongs to another operating-system user")

    def _read(self):
        self._safe_paths()
        if not self.path.exists():
            return None
        self._private(self.directory, directory=True)
        self._private(self.path)
        descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(_MAX_RECORD_BYTES + 1)
        if len(raw) > _MAX_RECORD_BYTES:
            raise ValueError("acknowledgement record is too large")
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        if (not isinstance(record, dict) or set(record) != {"protocol", "state", "acceptance", "audit"}
                or record["protocol"] != AGREEMENT_PROTOCOL or record["state"] not in {"accepted", "revoked"}
                or not isinstance(record["audit"], list) or not record["audit"]):
            raise ValueError("invalid acknowledgement record")
        acceptance = record["acceptance"]
        if acceptance is not None:
            self._validate_acceptance(acceptance)
        elif record["state"] == "accepted":
            raise ValueError("invalid acknowledgement record")
        for event in record["audit"]:
            if not isinstance(event, dict) or event.get("action") not in {"accepted", "revoked"}:
                raise ValueError("invalid acknowledgement audit")
            expected = {"action", "at", "acceptance" if event["action"] == "accepted" else "acceptance_id"}
            if set(event) != expected:
                raise ValueError("invalid acknowledgement audit")
            _timestamp(event.get("at"))
            if event["action"] == "accepted":
                self._validate_acceptance(event.get("acceptance"))
        last = record["audit"][-1]
        if (last["action"] != record["state"] or (record["state"] == "accepted" and last.get("acceptance") != acceptance)
                or (record["state"] == "revoked" and last.get("acceptance_id") != (acceptance or {}).get("acceptance_id"))):
            raise ValueError("invalid acknowledgement audit state")
        return record

    @staticmethod
    def _validate_acceptance(value):
        fields = {"signer", "accepted_at", "acceptance_id", "version", "agreement_text", "agreement_sha256",
                  "context", "context_sha256", "owner_uid", "confirmation"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("invalid acknowledgement")
        if (not isinstance(value["signer"], str) or not value["signer"].strip() or len(value["signer"]) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in value["signer"])
                or not isinstance(value["version"], str) or not value["version"] or len(value["version"]) > 80
                or not isinstance(value["agreement_text"], str)
                or not _SHA.fullmatch(value["agreement_sha256"])
                or hashlib.sha256(value["agreement_text"].encode()).hexdigest() != value["agreement_sha256"]
                or not isinstance(value["acceptance_id"], str) or not _ID.fullmatch(value["acceptance_id"])
                or value["confirmation"] != "I AGREE"):
            raise ValueError("invalid acknowledgement")
        _timestamp(value["accepted_at"])
        from .learning_privacy import validate_distilled_text

        validate_distilled_text(value["signer"])
        context = _context(value["context"])
        if _digest(context) != value["context_sha256"]:
            raise ValueError("invalid acknowledgement context")
        if hasattr(os, "getuid") and (type(value["owner_uid"]) is not int or value["owner_uid"] != os.getuid()):
            raise ValueError("acknowledgement belongs to another operating-system user")

    def status(self):
        try:
            document = self.document()
        except (OSError, ValueError, TypeError):
            return {"status": "unavailable", "version": AGREEMENT_VERSION, "agreement_sha256": AGREEMENT_SHA256}
        result = {"status": "required", "version": document["version"], "agreement_sha256": document["sha256"],
                  "context": document["context"], "context_sha256": document["context_sha256"]}
        try:
            record = self._read()
            if record is None:
                return result
            acceptance = record["acceptance"]
            if record["state"] == "revoked":
                return {**result, "status": "revoked"}
            if (acceptance["version"] != AGREEMENT_VERSION or acceptance["agreement_sha256"] != AGREEMENT_SHA256
                    or acceptance["context_sha256"] != document["context_sha256"]):
                return {**result, "status": "renewal_required"}
            return {**result, "status": "accepted", "accepted_at": acceptance["accepted_at"],
                    "acceptance_id": acceptance["acceptance_id"]}
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return {**result, "status": "unavailable"}

    def require(self, action: str):
        if not isinstance(action, str) or not action.strip():
            raise AgreementRequiredError()
        current = self.status()
        if current["status"] != "accepted":
            raise AgreementRequiredError()
        return {key: current[key] for key in ("version", "agreement_sha256", "context_sha256", "acceptance_id", "accepted_at")}

    def attestation(self):
        return {"protocol": AGREEMENT_PROTOCOL, "purpose": "feedback_upload", **self.require("feedback_upload")}

    @contextmanager
    def _locked(self):
        self._safe_paths()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self._safe_paths()
        self._private(self.directory, directory=True)
        lock = self.directory / "agreement.lock"
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            self._private(lock)
            from ..trace_store import _try_lock_exclusive, _unlock

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            deadline = time.monotonic() + 5
            while not _try_lock_exclusive(descriptor):
                if time.monotonic() >= deadline:
                    raise AgreementRequiredError()
                time.sleep(0.02)
            try:
                yield
            finally:
                _unlock(descriptor)
        finally:
            os.close(descriptor)

    def _write(self, record):
        # Keep a bounded recent audit. Retention cannot prevent revocation, and
        # the latest exact acknowledgement remains alongside the current state.
        record = {**record, "audit": record["audit"][-128:]}
        encoded = (_json(record) + "\n").encode("utf-8")
        while len(encoded) > _MAX_RECORD_BYTES and len(record["audit"]) > 1:
            record["audit"] = record["audit"][1:]
            encoded = (_json(record) + "\n").encode("utf-8")
        if len(encoded) > _MAX_RECORD_BYTES:
            raise AgreementRequiredError()
        descriptor, temporary = tempfile.mkstemp(prefix=".acceptance-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            self._safe_paths()
            os.replace(temporary, self.path)
            if os.name != "nt":
                directory_fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _accept(self, signer, *, version, agreement_sha256, context_sha256, confirmation="I AGREE"):
        """CLI/tests only: persist the exact terms and destinations just shown."""
        if (not isinstance(signer, str) or not signer.strip() or len(signer.strip()) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in signer)
                or confirmation != "I AGREE"):
            raise AgreementRequiredError()
        try:
            from .learning_privacy import validate_distilled_text

            validate_distilled_text(signer)
            with self._locked():
                document = self.document()
                if (version != document["version"] or agreement_sha256 != document["sha256"]
                        or context_sha256 != document["context_sha256"]):
                    raise AgreementRequiredError()
                previous = self._read()
                acceptance = {"signer": signer.strip(), "accepted_at": _now(), "acceptance_id": uuid.uuid4().hex,
                              "version": version, "agreement_text": document["text"],
                              "agreement_sha256": agreement_sha256, "context": document["context"],
                              "context_sha256": context_sha256,
                              "owner_uid": os.getuid() if hasattr(os, "getuid") else None,
                              "confirmation": confirmation}
                audit = previous["audit"] if previous else []
                audit.append({"action": "accepted", "at": acceptance["accepted_at"], "acceptance": acceptance})
                self._write({"protocol": AGREEMENT_PROTOCOL, "state": "accepted", "acceptance": acceptance, "audit": audit})
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise AgreementRequiredError() from None
        return self.status()

    def revoke(self):
        try:
            with self._locked():
                previous = self._read()
                acceptance = previous["acceptance"] if previous else None
                audit = previous["audit"] if previous else []
                audit.append({"action": "revoked", "at": _now(),
                              "acceptance_id": (acceptance or {}).get("acceptance_id")})
                self._write({"protocol": AGREEMENT_PROTOCOL, "state": "revoked", "acceptance": acceptance, "audit": audit})
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise AgreementRequiredError() from None
        return self.status()


def validate_upload_attestation(value):
    """Validate a trusted provider's attestation, not independent human proof."""
    fields = {"protocol", "purpose", "version", "agreement_sha256", "context_sha256", "acceptance_id", "accepted_at"}
    try:
        if (not isinstance(value, dict) or set(value) != fields or value["protocol"] != AGREEMENT_PROTOCOL
                or value["purpose"] != "feedback_upload" or value["version"] != AGREEMENT_VERSION
                or value["agreement_sha256"] != AGREEMENT_SHA256
                or not isinstance(value["context_sha256"], str) or not _SHA.fullmatch(value["context_sha256"])
                or not isinstance(value["acceptance_id"], str) or not _ID.fullmatch(value["acceptance_id"])):
            raise AgreementRequiredError()
        _timestamp(value["accepted_at"])
        return json.loads(_json(value))
    except (ValueError, TypeError, KeyError):
        raise AgreementRequiredError() from None
