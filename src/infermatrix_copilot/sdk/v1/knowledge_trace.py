"""Embedded facade for durable, evidence-backed knowledge-use traces.

Host submissions are declarations. They cannot impersonate provider delivery or
captured tool evidence; reference validation is performed by the trace service.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import threading
from pathlib import Path
from typing import Any

from .models import InvalidRequestError, RepositoryRef


class KnowledgeTraceClient:
    """Record host receipts/assessments and read deterministic trace reports."""

    def __init__(self, *, root: str | Path | None = None, mode: str | None = None):
        # Keep public SDK imports independent of server/config initialization.
        from ...knowledge_trace import KnowledgeTrace

        self.recorder = KnowledgeTrace(root=root, mode=mode)
        self._degraded: set[str] = set()
        self._persisted_gaps: set[str] = set()
        self._capture_lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return self.recorder.enabled

    @property
    def mode(self) -> str:
        return self.recorder.mode

    def is_degraded(self, trace_id: str) -> bool:
        with self._capture_lock:
            return trace_id in self._degraded

    def _flush_capture_gap(self, trace_id: str) -> None:
        """Persist a sticky marker after a transient write failure recovers."""
        with self._capture_lock:
            if not trace_id or trace_id not in self._degraded or trace_id in self._persisted_gaps or not self.enabled:
                return
            event = {"event_type": "gap_recorded", "step_id": "knowledge.capture",
                     "event_id": "ke-capture-gap-" + hashlib.sha256(trace_id.encode()).hexdigest()[:32],
                     "gap": {"reason": "knowledge_capture_degraded",
                             "detail": "At least one knowledge observation was not persisted."}}
            try:
                result = self.recorder.record_events(trace_id, [event])
                if result.get("status") == "recorded":
                    self._persisted_gaps.add(trace_id)
            except Exception:
                # Keep the in-memory marker for the next operation. The original
                # failure retains its required/best_effort behavior.
                pass

    def mark_degraded(self, trace_id: str) -> None:
        if trace_id and self.enabled:
            with self._capture_lock:
                self._degraded.add(trace_id)
            self._flush_capture_gap(trace_id)

    def capture(self, operation: str, trace_id: str, **kwargs) -> dict:
        """Internal provider boundary: observe failures returned as values too."""
        if operation not in {"retrieve", "provide"}:
            raise InvalidRequestError("unsupported provider capture operation")
        try:
            result = getattr(self.recorder, operation)(trace_id, **kwargs)
        except Exception as exc:
            self.mark_degraded(trace_id)
            if self.mode == "required":
                raise
            return {"trace_id": trace_id, "status": "degraded", "error": str(exc)}
        if result.get("status") == "degraded":
            self.mark_degraded(trace_id)
        else:
            self._flush_capture_gap(trace_id)
        return result

    def record_gap(self, trace_id: str, reason: str) -> None:
        """Record a failed scoped operation without copying rejected path data."""
        try:
            result = self.recorder.record_events(trace_id, [{"event_type": "gap_recorded",
                "step_id": "knowledge.access", "gap": {"reason": reason}}])
            if result.get("status") == "degraded":
                self.mark_degraded(trace_id)
        except Exception:
            self.mark_degraded(trace_id)

    @staticmethod
    def _repository(repository: RepositoryRef | str) -> str:
        value = repository.alias if isinstance(repository, RepositoryRef) else repository
        value = str(value).strip().replace("_", "-").casefold()
        if not value or "/" in value or "\\" in value or value in {".", ".."}:
            raise InvalidRequestError("repository must be a canonical knowledge repository alias")
        return value

    def start_session(self, repository: RepositoryRef | str, *, mode: str = "direct", **kwargs) -> dict:
        result = self.recorder.start_session(self._repository(repository), mode=mode, **kwargs)
        if result.get("status") == "degraded":
            self.mark_degraded(result.get("trace_id", ""))
        return result

    def get_report(
        self, trace_id: str, *, repository: RepositoryRef | str = "",
        cursor: str | int | None = None, limit: int | None = None,
    ) -> dict:
        report = self.recorder.report(trace_id, cursor=cursor, limit=limit)
        if not report.get("session"):
            if report.get("status") == "degraded":
                self.mark_degraded(trace_id)
            return report
        if repository:
            expected = self._repository(repository)
            if report["session"].get("repository") != expected:
                raise InvalidRequestError("trace belongs to a different repository")
        if any(gap.get("reason") == "knowledge_capture_degraded" for gap in report.get("gaps", [])):
            with self._capture_lock:
                self._degraded.add(trace_id)
                self._persisted_gaps.add(trace_id)
        self._flush_capture_gap(trace_id)
        if self.is_degraded(trace_id):
            report = self.recorder.report(trace_id, cursor=cursor, limit=limit)
            report["trace_status"] = "partial"
            if not any(g.get("reason") == "knowledge_capture_degraded" for g in report.get("gaps", [])):
                report.setdefault("gaps", []).append({"reason": "knowledge_capture_degraded", "persisted": False})
                if report.get("counts"):
                    report["counts"]["gaps"] = len(report["gaps"])
        return report

    def record_events(
        self, trace_id: str, events: list[dict[str, Any]], *,
        repository: RepositoryRef | str = "", idempotency_key: str = "",
    ) -> dict:
        """Submit host claims; all references remain subject to validation."""
        report = self.get_report(trace_id, repository=repository)
        if not report.get("session"):
            return report
        if not isinstance(events, list) or len(events) > 100:
            raise InvalidRequestError("events must be a list of at most 100 entries")
        submitted = deepcopy(events)
        allowed = {"content_provided", "evidence_registered", "assessment_recorded", "gap_recorded"}
        for event in submitted:
            if not isinstance(event, dict) or event.get("event_type") not in allowed:
                raise InvalidRequestError("unsupported host knowledge event")
            if "payload" in event:
                raise InvalidRequestError("host events must use the flat knowledge-use event format")
            kind = event["event_type"]
            payload_key = {"content_provided": "exposure", "assessment_recorded": "assessment",
                           "evidence_registered": "evidence", "gap_recorded": "gap"}[kind]
            if not isinstance(event.get(payload_key), dict):
                raise InvalidRequestError(f"{kind} requires a {payload_key} object")
            if kind == "content_provided":
                exposure = event.setdefault("exposure", {})
                if exposure.get("boundary", "host_context") != "host_context":
                    raise InvalidRequestError("host receipts must use the host_context boundary")
                exposure["boundary"] = "host_context"
                exposure["reported_by"] = "host"
            elif kind == "assessment_recorded":
                event.setdefault("assessment", {})["reported_by"] = "host"
            elif kind == "evidence_registered":
                event.setdefault("evidence", {})["evidence_origin"] = "host_reported"
        result = self.recorder.record_events(trace_id, submitted, idempotency_key=idempotency_key)
        if result.get("status") == "degraded":
            self.mark_degraded(trace_id)
        return result

    # A deliberately small vocabulary for hosts: receipt + assessment batches.
    record_usage = record_events

    def finish_session(self, trace_id: str, status: str = "complete", *, repository: RepositoryRef | str = "") -> dict:
        report = self.get_report(trace_id, repository=repository)
        if not report.get("session"):
            return report
        if status == "complete" and self.is_degraded(trace_id):
            status = "degraded"
        result = self.recorder.finish_session(trace_id, status=status)
        if result.get("status") == "degraded" and not result.get("session"):
            self.mark_degraded(trace_id)
        return result
