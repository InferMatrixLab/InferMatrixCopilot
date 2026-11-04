"""Append-only knowledge-use protocol backed by the shared trace/1 store.

The root lock serializes duplicate detection and append across processes. No
knowledge, source or model work is performed by this module.
"""

from __future__ import annotations

import copy
import os
import re
import sqlite3
import uuid
from pathlib import Path

from ..trace_store import TraceStore, file_lock
from .references import (KnowledgeTraceError, digest, document_scope,
                         extract_rule_sections, knowledge_reference)
from .report import project

SCHEMA = "knowledge-use/1"
MAX_EVENTS = 10_000
MAX_CONTENT = 1_048_576
EVENT_TYPES = {"session_started", "retrieval_completed", "content_provided", "evidence_registered",
               "assessment_recorded", "gap_recorded", "session_finished"}
BOUNDARIES = {"provider_response", "host_context", "model_request"}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise KnowledgeTraceError(f"{name} must be a bounded identifier")
    return value


def _rule_ids(values) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or any(not isinstance(v, str) or not v or len(v) > 100 for v in values):
        raise KnowledgeTraceError("rule_ids must be a list of bounded strings")
    return list(dict.fromkeys(values))


class KnowledgeTrace:
    """Collect bounded, versioned knowledge-use sessions.

    Invalid claims always raise :class:`KnowledgeTraceError`. Storage failures
    return ``status=degraded`` in best_effort mode and raise in required mode.
    The optional constructor ``mode`` is the recording policy, while
    ``start_session(mode=...)`` selects direct or strict execution.
    """

    def __init__(self, root: Path | str | None = None, mode: str | None = None):
        self.mode = mode or os.environ.get("KNOWLEDGE_TRACE_MODE", "best_effort")
        if self.mode not in {"off", "best_effort", "required"}:
            raise KnowledgeTraceError("KNOWLEDGE_TRACE_MODE must be off, best_effort or required")
        self.root = Path(root or os.environ.get("KNOWLEDGE_TRACE_ROOT") or
                         Path.home() / ".infermatrix-copilot" / "knowledge-traces").expanduser()
        self.store = TraceStore(self.root)
        self.producer_id = "producer-" + uuid.uuid4().hex

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def _operate(self, trace_id, operation):
        if not self.enabled:
            return {"trace_id": "", "status": "off"}
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with file_lock(self.root / "knowledge.lock") as held:
                if not held:
                    raise OSError("knowledge trace writer lock timed out")
                # The JSONL archive is authoritative even after deleting an index.
                if not self.store.index_path.exists() and any((self.root / "records").glob("*.jsonl")):
                    self.store.rebuild_index(offline_confirmed=True)
                return operation()
        except (OSError, sqlite3.Error) as exc:
            if self.mode == "required":
                raise KnowledgeTraceError(f"required knowledge recording failed: {type(exc).__name__}") from exc
            return {"trace_id": trace_id, "status": "degraded", "error": type(exc).__name__,
                    "gaps": [{"reason": "trace_storage_unavailable"}]}

    def _records(self, trace_id: str) -> list[dict]:
        _identifier(trace_id, "trace_id")
        records = self.store.query(kind="knowledge_event", changeset_id=trace_id, limit=MAX_EVENTS + 8)
        if len(records) > MAX_EVENTS + 2:
            raise KnowledgeTraceError("knowledge trace exceeds its bounded event budget")
        return sorted(records, key=lambda r: r["result"].get("session_seq", 0))

    def _session(self, records: list[dict]) -> dict:
        if not records or records[0]["result"].get("event_type") != "session_started":
            raise KnowledgeTraceError("unknown knowledge trace session")
        return records[0]["result"]["session"]

    def _append(self, trace_id: str, session: dict, records: list[dict], event: dict) -> dict:
        event_type = event.get("event_type")
        if event_type not in EVENT_TYPES:
            raise KnowledgeTraceError("unsupported knowledge trace event_type")
        event = copy.deepcopy(event)
        step_id = event.get("step_id", "")
        # Step names are opaque engine labels, not paths or entity IDs. MoA
        # names include '#moa/<provider>/<model>' and must retain that identity.
        if (not isinstance(step_id, str) or len(step_id) > 200
                or any(ord(character) < 32 or ord(character) == 127 for character in step_id)):
            raise KnowledgeTraceError("step_id must be a bounded printable string")
        event_id = _identifier(event.pop("event_id", "ke-" + uuid.uuid4().hex), "event_id")
        # Fingerprint the declaration before automatically generated identity fields.
        fingerprint = digest(event)
        prior = next((r for r in records if r["result"].get("event_id") == event_id), None)
        if prior:
            if prior["result"].get("declaration_sha256") != fingerprint:
                raise KnowledgeTraceError("event_id conflicts with an immutable prior event")
            return prior
        if records and records[-1]["result"].get("event_type") == "session_finished":
            raise KnowledgeTraceError("knowledge trace session is already finished")
        if len(records) >= MAX_EVENTS and event_type not in {"session_finished", "gap_recorded"}:
            raise KnowledgeTraceError("knowledge trace event budget exhausted")
        parent_ids = event.get("parent_event_ids", [])
        known_ids = {r["result"]["event_id"] for r in records}
        if not isinstance(parent_ids, list) or any(p not in known_ids for p in parent_ids):
            raise KnowledgeTraceError("parent_event_ids must refer to existing events in this session")
        supersedes = event.get("supersedes")
        if supersedes and supersedes not in known_ids:
            raise KnowledgeTraceError("supersedes must refer to an existing event")
        if supersedes:
            replaced = next(r["result"] for r in records if r["result"]["event_id"] == supersedes)
            if (replaced["event_type"] != event_type
                    or event_type not in {"assessment_recorded", "gap_recorded"}):
                raise KnowledgeTraceError("only assessments and gaps may supersede events of the same type; observations and evidence are immutable")
        producer_id = event.pop("producer_id", self.producer_id)
        _identifier(producer_id, "producer_id")
        previous = max((r["result"].get("producer_seq", 0) for r in records
                        if r["result"].get("producer_id") == producer_id), default=0)
        producer_seq = event.pop("producer_seq", previous + 1)
        if isinstance(producer_seq, bool) or not isinstance(producer_seq, int) or producer_seq <= previous:
            raise KnowledgeTraceError("producer_seq must increase monotonically")
        result = {"schema": SCHEMA, "trace_id": trace_id, "event_id": event_id,
                  "producer_id": producer_id, "producer_seq": producer_seq,
                  "session_seq": max((r["result"].get("session_seq", 0) for r in records), default=0) + 1,
                  "declaration_sha256": fingerprint, **event}
        rule_ids = list(dict.fromkeys(self._event_rules(result)))
        record = self.store.append("knowledge_event", context={
            "run_id": session.get("run_id") or trace_id, "changeset_id": trace_id,
            "step": event.get("step_id", ""), "repo": session["repository"], "rule_ids": rule_ids,
        }, result=result)
        records.append(record)
        return record

    @staticmethod
    def _event_rules(event):
        for entry in event.get("entries", []):
            yield from entry.get("rule_ids", [])
            if entry.get("rule_id"):
                yield entry["rule_id"]
        exposure = event.get("exposure", {})
        yield from exposure.get("rule_ids", [])
        if event.get("assessment", {}).get("rule_id"):
            yield event["assessment"]["rule_id"]

    def start_session(self, repository: str, mode: str, run_id: str = "", review_context_id: str = "",
                      source_revision: str = "", knowledge_snapshot: str = "", resource_revision: str = "",
                      instrumentation: dict | None = None, trace_id: str = "") -> dict:
        _identifier(repository, "repository")
        if mode not in {"direct", "strict"}:
            raise KnowledgeTraceError("session mode must be direct or strict")
        if instrumentation is not None and not isinstance(instrumentation, dict):
            raise KnowledgeTraceError("instrumentation must be an object")
        trace_id = _identifier(trace_id or "kt-" + uuid.uuid4().hex, "trace_id")
        session = {"trace_id": trace_id, "repository": repository, "mode": mode, "run_id": run_id,
                   "review_context_id": review_context_id, "source_revision": source_revision,
                   "knowledge_snapshot": knowledge_snapshot, "resource_revision": resource_revision,
                   "instrumentation": instrumentation or {}, "status": "running"}

        def write():
            records = self._records(trace_id)
            if records:
                if self._session(records) != session:
                    raise KnowledgeTraceError("trace_id conflicts with an immutable session")
            else:
                self._append(trace_id, session, records, {"event_type": "session_started", "session": session})
            return {**session, "status": "running"}
        return self._operate(trace_id, write)

    def retrieve(self, trace_id: str, *, step_id: str, entries: list[dict], mechanism: str,
                 query: str = "", reason: str = "", status: str = "ready") -> dict:
        if not isinstance(entries, list) or len(entries) > 1000:
            raise KnowledgeTraceError("retrieval entries must be a bounded list")

        def write():
            records = self._records(trace_id)
            session = self._session(records)
            for entry in entries:
                path = entry.get("document_id") or entry.get("path")
                if path:
                    document_scope(path, session["repository"])
                _rule_ids(entry.get("rule_ids"))
            return self._append(trace_id, session, records, {"event_type": "retrieval_completed",
                "step_id": step_id, "entries": entries, "mechanism": mechanism,
                "query": query, "reason": reason, "status": status})
        return self._operate(trace_id, write)

    def provide(self, trace_id: str, *, step_id: str, document_id: str, content: str, boundary: str,
                representation: str = "excerpt", rule_ids: list[str] | None = None, offset: int | None = 0,
                offset_unit: str = "unicode_codepoints", truncated: bool = False, snapshot: str = "",
                document_sha256: str = "", parent_event_ids: list[str] | None = None,
                source_length: int | None = None) -> dict:
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_CONTENT:
            raise KnowledgeTraceError("knowledge exposure content exceeds the 1 MiB budget")
        if boundary not in BOUNDARIES:
            raise KnowledgeTraceError("unsupported knowledge delivery boundary")
        if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
            raise KnowledgeTraceError("offset must be a non-negative integer or unknown (None)")
        if source_length is not None and (isinstance(source_length, bool)
                or not isinstance(source_length, int) or source_length < 0):
            raise KnowledgeTraceError("source_length must be a non-negative integer")
        if offset_unit not in {"unicode_codepoints", "utf8_bytes"}:
            raise KnowledgeTraceError("unsupported offset_unit")
        ids = _rule_ids(rule_ids)

        def write():
            records = self._records(trace_id)
            session = self._session(records)
            effective_snapshot = snapshot or session.get("knowledge_snapshot", "")
            if snapshot and session.get("knowledge_snapshot") and snapshot != session["knowledge_snapshot"]:
                raise KnowledgeTraceError("knowledge snapshot differs from the immutable session")
            document_scope(document_id, session["repository"])
            content_sha = digest(content)
            payload_ref = self.store.put_blob(content)
            # A pinned snapshot supplies a stable document version across
            # paginated reads. With neither hash nor snapshot, only the visible
            # payload version is known; never claim a partial hash is a file hash.
            source_hash = document_sha256
            ref = knowledge_reference(document_id, session["repository"], effective_snapshot, source_hash)
            if not effective_snapshot and not source_hash:
                ref = knowledge_reference(document_id, session["repository"], "visible:" + content_sha, "")
                effective_snapshot = ref["snapshot_id"]
            sections = extract_rule_sections(content)
            section_ids = [s["rule_id"] for s in sections]
            ids_all = list(dict.fromkeys([*ids, *section_ids]))
            rule_refs = []
            for rule_id in ids_all:
                section = next((s for s in sections if s["rule_id"] == rule_id), None)
                rule_ref = knowledge_reference(document_id, session["repository"], effective_snapshot, source_hash,
                    rule_id=rule_id, section_sha256=section["section_sha256"] if section else "")
                complete = bool(section and section["has_body"] and representation in {"full_document", "full_section"}
                                and (not truncated or section["closed_by_heading"]))
                rule_refs.append({**rule_ref, "coverage": "body_complete" if complete else
                    "body_excerpt" if section and section["has_body"] else "reference_only"})
            length = (source_length if source_length is not None else
                      len(content.encode("utf-8")) if offset_unit == "utf8_bytes" else len(content))
            exposure_id = "kx-" + uuid.uuid4().hex
            exposure = {"exposure_id": exposure_id, "knowledge_ref_id": ref["knowledge_ref_id"],
                "reference": ref, "document_id": document_id, "rule_ids": ids_all, "rule_refs": rule_refs,
                "step_id": step_id, "boundary": boundary, "representation": representation,
                "payload_ref": payload_ref, "visible_text_sha256": content_sha,
                "archived_sha256": payload_ref.removeprefix("sha256:"),
                "included_ranges": [] if offset is None else [[offset, offset + length]],
                "range_status": "unknown" if offset is None else "known", "offset_unit": offset_unit,
                "truncated": bool(truncated), "delivery_status": "observed",
                "reported_by": "provider"}
            return self._append(trace_id, session, records, {"event_type": "content_provided", "step_id": step_id,
                "exposure_id": exposure_id, "knowledge_ref_id": ref["knowledge_ref_id"], "rule_refs": rule_refs,
                "exposure": exposure, "parent_event_ids": parent_event_ids or []})
        return self._operate(trace_id, write)

    @staticmethod
    def _entities(records):
        refs, exposures, evidence = {}, {}, {}
        for record in records:
            result = record["result"]
            exposure = result.get("exposure")
            if exposure:
                exposures[exposure["exposure_id"]] = exposure
                ref = exposure["reference"]
                refs[ref["knowledge_ref_id"]] = ref
                for rule in exposure.get("rule_refs", []):
                    refs[rule["knowledge_ref_id"]] = rule
            if result.get("evidence"):
                item = result["evidence"]
                evidence[item["evidence_id"]] = item
        return refs, exposures, evidence

    def _validate_declaration(self, event: dict, records: list[dict], session: dict) -> dict:
        event = copy.deepcopy(event)
        event_type = event.get("event_type")
        if event_type not in {"assessment_recorded", "evidence_registered", "content_provided", "gap_recorded"}:
            raise KnowledgeTraceError("record_events accepts usage declarations and gaps only")
        refs, exposures, evidence = self._entities(records)
        if event_type == "gap_recorded":
            if not isinstance(event.get("gap"), dict) or not event["gap"].get("reason"):
                raise KnowledgeTraceError("gap_recorded requires a gap reason")
        elif event_type == "evidence_registered":
            item = event.get("evidence")
            if not isinstance(item, dict):
                raise KnowledgeTraceError("evidence_registered requires evidence")
            item.setdefault("evidence_id", "kevidence-" + digest(item)[:32])
            _identifier(item["evidence_id"], "evidence_id")
            path = item.get("path", "")
            if path and (path.startswith(("/", "\\")) or ".." in path.replace("\\", "/").split("/")):
                raise KnowledgeTraceError("evidence path must be repository-relative")
            revision = item.get("source_revision", "")
            expected = session.get("source_revision", "")
            item["revision_status"] = "stale" if revision and expected and revision != expected else \
                "matched" if revision and expected else "unknown"
            item.setdefault("evidence_origin", "host_reported")
            if "content" in item:
                content = item.pop("content")
                if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_CONTENT:
                    raise KnowledgeTraceError("evidence content exceeds the 1 MiB budget")
                item["payload_ref"] = self.store.put_blob(content)
            if item["evidence_id"] in evidence and evidence[item["evidence_id"]] != item:
                raise KnowledgeTraceError("evidence_id conflicts with immutable evidence")
        elif event_type == "content_provided":
            item = event.get("exposure")
            if not isinstance(item, dict) or item.get("boundary") != "host_context":
                raise KnowledgeTraceError("host delivery receipts must name the host_context boundary")
            source = exposures.get(item.get("source_exposure_id"))
            if source is None:
                raise KnowledgeTraceError("host receipt references an unknown source_exposure_id")
            allowed = {"source_exposure_id", "boundary", "exposure_id", "reported_by", "delivered_at"}
            if set(item) - allowed:
                raise KnowledgeTraceError("host delivery receipts cannot override source content identity")
            exposure_id = item.get("exposure_id") or "kx-" + digest(event)[:32]
            _identifier(exposure_id, "exposure_id")
            event["exposure"] = {**source, **item, "exposure_id": exposure_id,
                                 "reported_by": "host", "delivery_status": "host_reported"}
            if exposure_id in exposures and exposures[exposure_id] != event["exposure"]:
                raise KnowledgeTraceError("exposure_id conflicts with immutable exposure")
            event["exposure_id"] = exposure_id
            event["knowledge_ref_id"] = source["knowledge_ref_id"]
            event["rule_refs"] = source.get("rule_refs", [])
        else:
            item = event.get("assessment")
            if not isinstance(item, dict):
                raise KnowledgeTraceError("assessment_recorded requires assessment")
            if not item.get("knowledge_ref_id") and item.get("document_id"):
                document_scope(item["document_id"], session["repository"])
                candidates = [ref for ref in refs.values() if ref["document_id"] == item["document_id"]
                              and ref.get("rule_id", "") == item.get("rule_id", "")]
                if len(candidates) != 1:
                    raise KnowledgeTraceError("assessment requires an unambiguous versioned knowledge reference")
                item["knowledge_ref_id"] = candidates[0]["knowledge_ref_id"]
            ref = refs.get(item.get("knowledge_ref_id"))
            if ref is None:
                raise KnowledgeTraceError("assessment references unknown knowledge_ref_id")
            item.setdefault("document_id", ref["document_id"])
            item.setdefault("rule_id", ref.get("rule_id", ""))
            if item["document_id"] != ref["document_id"] or item["rule_id"] != ref.get("rule_id", ""):
                raise KnowledgeTraceError("assessment document/rule identity conflicts with knowledge_ref_id")
            if item.get("applicability") not in {"applicable", "not_applicable", "unknown"}:
                raise KnowledgeTraceError("assessment requires explicit applicability")
            if item.get("verification") not in {"supported", "contradicted", "unresolved"}:
                raise KnowledgeTraceError("assessment requires explicit verification")
            if not isinstance(item.get("rationale"), str) or not item["rationale"].strip():
                raise KnowledgeTraceError("assessment requires a brief rationale")
            if item["applicability"] == "applicable" and not str(item.get("check", "")).strip():
                raise KnowledgeTraceError("applicable assessment requires the check performed")
            if item["verification"] in {"supported", "contradicted"} and not item.get("evidence_ids"):
                raise KnowledgeTraceError("verified assessments require evidence references")
            for exposure_id in item.get("exposure_ids", []):
                exposure = exposures.get(exposure_id)
                if exposure is None:
                    raise KnowledgeTraceError("assessment references unknown exposure_id")
                allowed_refs = {exposure["knowledge_ref_id"], *(r["knowledge_ref_id"] for r in exposure.get("rule_refs", []))}
                if item["knowledge_ref_id"] not in allowed_refs:
                    raise KnowledgeTraceError("assessment exposure does not contain the referenced knowledge")
            for evidence_id in item.get("evidence_ids", []):
                if evidence_id not in evidence:
                    raise KnowledgeTraceError("assessment references unknown evidence_id")
            if not isinstance(item.get("rationale", ""), str) or len(item.get("rationale", "")) > 4000:
                raise KnowledgeTraceError("assessment rationale must be at most 4000 characters")
            item.setdefault("assessment_id", "ka-" + digest(item)[:32])
            _identifier(item["assessment_id"], "assessment_id")
            for name in ("evidence_ids", "exposure_ids", "decision_ids"):
                _rule_ids(item.get(name))
            item.setdefault("reported_by", "host")
            item["reference_validation"] = "valid"
            item["exposure_status"] = "reported" if item.get("exposure_ids") else "not_reported"
            stale = [e for e in item.get("evidence_ids", []) if evidence[e].get("revision_status") == "stale"]
            item["evidence_revision_status"] = "stale" if stale else "current_or_unknown"
            previous = next((r["result"] for r in reversed(records)
                if r["result"].get("assessment", {}).get("assessment_id") == item["assessment_id"]), None)
            if previous and previous["assessment"] != item:
                if (event.get("supersedes") != previous["event_id"] or
                        previous["assessment"]["knowledge_ref_id"] != item["knowledge_ref_id"]):
                    raise KnowledgeTraceError("assessment_id conflicts; corrections must supersede the same knowledge assessment")
        return event

    def record_events(self, trace_id: str, events: list[dict], idempotency_key: str = "") -> dict:
        if not isinstance(events, list) or len(events) > 100:
            raise KnowledgeTraceError("events must be a batch of at most 100 declarations")

        def write():
            records = self._records(trace_id)
            session = self._session(records)
            saved = []
            # Validation is sequential so an assessment may refer to earlier evidence in the batch.
            for index, raw in enumerate(events):
                if not isinstance(raw, dict):
                    raise KnowledgeTraceError("each event must be an object")
                event = copy.deepcopy(raw.get("payload", raw))
                if "payload" in raw:
                    event.setdefault("event_type", raw.get("event_type"))
                    event.setdefault("step_id", raw.get("step_id", ""))
                if idempotency_key:
                    event.setdefault("event_id", "ke-" + digest([idempotency_key, index])[:32])
                event = self._validate_declaration(event, records, session)
                saved.append(self._append(trace_id, session, records, event))
            return {"trace_id": trace_id, "status": "recorded", "events": saved}
        return self._operate(trace_id, write)

    def finish_session(self, trace_id: str, status: str = "complete") -> dict:
        if status not in {"complete", "interrupted", "degraded"}:
            raise KnowledgeTraceError("invalid finished session status")

        def write():
            records = self._records(trace_id)
            session = self._session(records)
            if records[-1]["result"].get("event_type") == "session_finished":
                if records[-1]["result"]["status"] != status:
                    raise KnowledgeTraceError("finished session status is immutable")
            else:
                self._append(trace_id, session, records, {"event_type": "session_finished", "status": status})
            return {"trace_id": trace_id, "status": status}
        return self._operate(trace_id, write)

    def report(self, trace_id: str, cursor: str | int | None = None, limit: int | None = None) -> dict:
        def read():
            records = self._records(trace_id)
            self._session(records)
            return project(records, self.store, cursor=cursor, limit=limit)
        return self._operate(trace_id, read)

    def query(self, repository: str = "", rule_id: str = "", limit: int = 100) -> list[dict] | dict:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise KnowledgeTraceError("query limit must be between 1 and 1000")
        if repository:
            _identifier(repository, "repository")
        if not self.enabled:
            return []

        def read():
            records = self.store.query(kind="knowledge_event", repo=repository or None,
                rule_id=rule_id or None, limit=limit)
            return [{"trace_id": r["result"]["trace_id"], "event_id": r["result"]["event_id"],
                     "event_type": r["result"]["event_type"], "repository": r["context"]["repo"],
                     "step_id": r["result"].get("step_id", ""), "at": r["at"],
                     "rule_ids": r["context"].get("rule_ids", [])} for r in records]
        return self._operate("", read)
