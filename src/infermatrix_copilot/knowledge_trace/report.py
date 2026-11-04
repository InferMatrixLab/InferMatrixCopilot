"""Deterministic, bounded projections of knowledge-use declarations."""

from __future__ import annotations

from .references import KnowledgeTraceError


def project(records, store, *, cursor=None, limit=None) -> dict:
    """Project a complete bounded session; paginate the raw event list only.

    A complete session means capture ended successfully, not that the model
    understood the content or that a conclusion has been independently verified.
    """
    try:
        offset = int(cursor or 0)
    except (TypeError, ValueError) as exc:
        raise KnowledgeTraceError("invalid knowledge trace cursor") from exc
    if offset < 0:
        raise KnowledgeTraceError("invalid knowledge trace cursor")
    page_size = 1000 if limit is None else limit
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 1000:
        raise KnowledgeTraceError("report limit must be between 1 and 1000")
    session = dict(records[0]["result"]["session"])
    retrievals, exposures, evidence, assessments, gaps = [], [], [], [], []
    references = {}
    steps = {}
    finished = False
    superseded = {r["result"].get("supersedes") for r in records if r["result"].get("supersedes")}
    available = {}
    for record in records:
        event = record["result"]
        if event["event_id"] in superseded:
            continue
        event_type = event["event_type"]
        step_id = event.get("step_id", "")
        if step_id:
            steps.setdefault(step_id, {"step_id": step_id, "retrieval_event_ids": [], "exposure_ids": [],
                                       "assessment_ids": [], "evidence_ids": [], "gaps": []})
        if event_type == "retrieval_completed":
            item = {k: event[k] for k in ("event_id", "step_id", "mechanism", "query", "reason", "status", "entries") if k in event}
            retrievals.append(item)
            if step_id:
                steps[step_id]["retrieval_event_ids"].append(event["event_id"])
        elif event_type == "content_provided":
            item = dict(event["exposure"])
            blob_ref = item.get("payload_ref", "")
            if blob_ref not in available:
                try:
                    store.blob(blob_ref)
                    available[blob_ref] = True
                except (OSError, KeyError, ValueError, EOFError):
                    available[blob_ref] = False
            item["content_available"] = available[blob_ref]
            exposures.append(item)
            references[item["knowledge_ref_id"]] = item["reference"]
            for ref in item.get("rule_refs", []):
                references[ref["knowledge_ref_id"]] = ref
            if step_id:
                steps[step_id]["exposure_ids"].append(item["exposure_id"])
            if not item["content_available"]:
                gaps.append({"reason": "archived_content_unavailable", "exposure_id": item["exposure_id"], "step_id": step_id})
        elif event_type == "evidence_registered":
            item = dict(event["evidence"])
            if item.get("payload_ref"):
                try:
                    store.blob(item["payload_ref"])
                    item["content_available"] = True
                except (OSError, KeyError, ValueError, EOFError):
                    item["content_available"] = False
                    gaps.append({"reason": "evidence_content_unavailable", "evidence_id": item["evidence_id"], "step_id": step_id})
            evidence.append(item)
            if step_id:
                steps[step_id]["evidence_ids"].append(item["evidence_id"])
        elif event_type == "assessment_recorded":
            item = dict(event["assessment"])
            item["step_id"] = step_id
            assessments.append(item)
            if item.get("evidence_revision_status") == "stale":
                gaps.append({"reason": "assessment_evidence_revision_stale", "assessment_id": item["assessment_id"], "step_id": step_id})
            if not item.get("exposure_ids"):
                gaps.append({"reason": "assessment_exposure_not_reported", "assessment_id": item["assessment_id"], "step_id": step_id})
            if item.get("applicability") == "applicable" and not item.get("evidence_ids"):
                gaps.append({"reason": "assessment_evidence_not_reported", "assessment_id": item["assessment_id"], "step_id": step_id})
            if step_id:
                steps[step_id]["assessment_ids"].append(item["assessment_id"])
        elif event_type == "gap_recorded":
            item = {"step_id": step_id, **event["gap"]}
            gaps.append(item)
            if step_id:
                steps[step_id]["gaps"].append(item)
        elif event_type == "session_finished":
            session["status"] = event["status"]
            finished = True
    assessed = {a["knowledge_ref_id"] for a in assessments}
    provided_rule_ids = {(r["knowledge_ref_id"]) for x in exposures for r in x.get("rule_refs", [])}
    unassessed = sorted(provided_rule_ids - assessed)
    next_offset = offset + page_size
    return {"schema": "knowledge-use-report/1", "trace_id": session["trace_id"],
            "status": session["status"], "session": session,
            "trace_status": "complete" if finished and session["status"] == "complete" and not gaps else "partial",
            "instrumentation": session.get("instrumentation", {}),
            "retrievals": retrievals, "exposures": exposures, "evidence": evidence,
            "assessments": assessments, "references": list(references.values()), "gaps": gaps,
            "steps": list(steps.values()), "unassessed_reference_ids": unassessed,
            "counts": {"events": len(records), "retrievals": len(retrievals), "exposures": len(exposures),
                       "provided_rules": len(provided_rule_ids), "assessments": len(assessments),
                       "unassessed_rules": len(unassessed), "evidence": len(evidence), "gaps": len(gaps)},
            "events": records[offset:next_offset],
            "next_cursor": str(next_offset) if next_offset < len(records) else None,
            "limits": {"session_events": 10000, "page_events": page_size},
            "semantics": {"provided": "content observed at the recorded boundary; attention is not observable",
                          "assessment": "explicit producer declaration; reference validation does not prove its conclusion",
                          "unassessed": "no application declaration; applicability is unknown"}}


def _cell(value) -> str:
    return str("—" if value is None or value == "" else value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def markdown(report: dict) -> str:
    """Render a portable reasoning-evidence report, without archived raw text."""
    session = report.get("session", {})
    counts = report.get("counts", {})
    lines = ["# Knowledge-use trace", "",
        f"Trace: `{_cell(report.get('trace_id'))}`. Status: **{_cell(report.get('status'))}**; capture: **{_cell(report.get('trace_status'))}**.", "",
        f"Repository: `{_cell(session.get('repository'))}`; mode: `{_cell(session.get('mode'))}`.",
        f"Source revision: `{_cell(session.get('source_revision'))}`; knowledge snapshot: `{_cell(session.get('knowledge_snapshot'))}`.", "",
        "Delivery records describe the stated boundary. Assessments are explicit declarations; references and evidence allow review, and do not establish causation or model attention.", "",
        f"Retrievals: {counts.get('retrievals', 0)}; exposures: {counts.get('exposures', 0)}; provided rules: {counts.get('provided_rules', 0)}; assessments: {counts.get('assessments', 0)}; unassessed rules: {counts.get('unassessed_rules', 0)}.", "",
        "## Capture boundaries", ""]
    instrumentation = report.get("instrumentation", {})
    lines.extend(f"- {_cell(key)}: {_cell(value)}" for key, value in sorted(instrumentation.items()))
    if not instrumentation:
        lines.append("Capture capabilities were not declared.")
    lines.extend(["", "## Steps", "", "| Step | Retrieved knowledge / reason | Provided content | Assessment / evidence |",
                  "| --- | --- | --- | --- |"])
    retrievals = {r["event_id"]: r for r in report.get("retrievals", [])}
    exposures = {r["exposure_id"]: r for r in report.get("exposures", [])}
    assessments = {r["assessment_id"]: r for r in report.get("assessments", [])}
    for step in report.get("steps", []):
        retrieved = []
        for event_id in step["retrieval_event_ids"]:
            event = retrievals[event_id]
            if not event["entries"]:
                retrieved.append(f"no match ({event.get('mechanism', '')})")
            for entry in event["entries"]:
                path = entry.get("document_id") or entry.get("path") or entry.get("name") or "candidate"
                reason = entry.get("reason") or event.get("reason") or event.get("mechanism", "")
                retrieved.append(f"{path}: {reason}")
        supplied = []
        for exposure_id in step["exposure_ids"]:
            exposure = exposures[exposure_id]
            rules = ", ".join(f"{r['rule_id']} ({r['coverage']})" for r in exposure.get("rule_refs", []))
            supplied.append(f"{exposure['document_id']} @ {exposure['boundary']} [{exposure['representation']}]"
                            + (f"; {rules}" if rules else "")
                            + ("; truncated" if exposure.get("truncated") else ""))
        assessed = []
        for assessment_id in step["assessment_ids"]:
            assessment = assessments[assessment_id]
            evidence = ", ".join(assessment.get("evidence_ids", [])) or "none declared"
            decisions = ", ".join(assessment.get("decision_ids", []))
            decision_text = "; ".join(
                f"{ref.get('decision_id', '')}: {ref.get('finding', '')}"
                for ref in assessment.get("decision_refs", []) if isinstance(ref, dict))
            assessed.append(f"{assessment.get('rule_id') or assessment.get('document_id')}: "
                f"{assessment['applicability']}/{assessment['verification']}; {assessment.get('rationale', '')}; "
                f"check: {assessment.get('check', 'not declared')}; "
                f"evidence: {evidence}" + (f"; decisions: {decisions}" if decisions else "")
                + (f"; findings: {decision_text}" if decision_text else ""))
        lines.append("| " + " | ".join(_cell(x) for x in
            (step["step_id"], "; ".join(retrieved), "; ".join(supplied), "; ".join(assessed) or "not assessed")) + " |")
    if not report.get("steps"):
        lines.append("| — | — | — | not assessed |")
    lines.extend(["", "## Evidence", ""])
    for item in report.get("evidence", []):
        lines.append(f"- `{_cell(item['evidence_id'])}`: {_cell(item.get('kind'))}; "
                     f"{_cell(item.get('path') or item.get('tool_call_id'))}; revision: {_cell(item.get('revision_status'))}; "
                     f"source: {_cell(item.get('source_revision'))}; origin: {_cell(item.get('evidence_origin'))}.")
        for key in ("command", "result_summary", "summary", "environment_fingerprint", "payload_ref",
                    "workspace_state", "observed_head", "commit_verified"):
            if key in item and item[key] is not None and item[key] != "":
                lines.append(f"  - {_cell(key)}: {_cell(item[key])}")
    if not report.get("evidence"):
        lines.append("No evidence declared.")
    lines.extend(["", "## Gaps", ""])
    lines.extend(f"- {_cell(g.get('step_id'))}: {_cell(g.get('reason'))}" for g in report.get("gaps", []))
    if not report.get("gaps"):
        lines.append("No capture gaps recorded. Unassessed knowledge remains unknown.")
    return "\n".join(lines) + "\n"
