"""Pure review result decoding; the host supplies its review policy."""
from __future__ import annotations

import re
from dataclasses import replace
from ..contracts.models import ReviewFinding, ReviewResult
from .dispositions import disposition


class ReviewParseError(ValueError):
    pass


def has_minimality_proof(proof):
    return all(len(str((proof or {}).get(field, "")).strip()) >= 12
               for field in ("scope_ledger", "abstraction_census", "why_no_safe_deletion"))


def valid_subtraction(item):
    if not isinstance(item, dict):
        return False
    action = str(item.get("action", "")).split(maxsplit=1)
    return ":" in str(item.get("anchor", "")).strip() and bool(action) and (
        action[0].upper() in {"DELETE", "DEFER", "INLINE", "MERGE", "MOVE"}) and bool(str(item.get("risk", "")).strip())


def parse_direct_result(payload: dict, *, required_checks=(), normalize_reference=None) -> ReviewResult:
    normalize_reference = normalize_reference or (lambda value: "")
    try:
        raw_checks = payload["review_checks"]
        if not isinstance(raw_checks, dict):
            raise TypeError("review_checks must be an object")
        if set(raw_checks) != set(required_checks):
            raise ValueError(
                "review_checks must contain exactly: "
                + ", ".join(required_checks)
            )
        review_checks = {
            key: str(raw_checks[key]).strip()
            for key in required_checks
        }
        if any(len(value) < 12 for value in review_checks.values()):
            raise ValueError(
                "every review_checks item needs concrete evidence"
            )
        findings = tuple(
            ReviewFinding(
                severity=str(item["severity"]),
                title=str(item["title"]),
                body=str(item["body"]),
                path=str(item["path"]),
                line=item.get("line"),
            )
            for item in payload["findings"]
        )
        feedback_status = str(payload["existing_feedback_status"])
        if feedback_status not in {
            "checked", "disabled", "unavailable", "not_applicable"
        }:
            raise ValueError("existing_feedback_status is invalid")
        raw_dispositions = payload["finding_dispositions"]
        if not isinstance(raw_dispositions, list):
            raise TypeError("finding_dispositions must be an array")
        finding_dispositions = tuple(disposition(item, strict=True, normalize_reference=normalize_reference)
                                     for item in raw_dispositions)
        result = ReviewResult(
            reviewed_head_sha=str(payload["reviewed_head_sha"]),
            summary=str(payload["summary"]),
            findings=findings,
            subtraction_signal=str(payload["subtraction_signal"]),
            review_checks=review_checks,
            subtraction=tuple(payload.get("subtraction") or ()),
            minimality_proof=payload.get("minimality_proof"),
            existing_feedback_status=feedback_status,
            finding_dispositions=finding_dispositions,
            finding_rechecks=tuple(payload.get("finding_rechecks") or ()),
        )
        from .publication_policy import feedback_alignment_error

        problem = feedback_alignment_error(result, feedback_status)
        if problem:
            raise ValueError(problem)
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewParseError(f"review result does not match the schema: {exc}") from exc


def normalize_direct_evidence(result: ReviewResult) -> ReviewResult:
    if result.subtraction_signal == "none":
        return replace(result, subtraction=(), minimality_proof=None)
    if not has_minimality_proof(result.minimality_proof):
        return result
    return replace(result, subtraction=tuple(item for item in result.subtraction if valid_subtraction(item)))



def review_summary_errors(
    result: ReviewResult,
    changed_statuses=(),
) -> tuple[str, ...]:
    summary = result.summary.strip()
    description_heading = "### PR description"
    flow_heading = "### Change flow"
    description_at = summary.find(description_heading)
    flow_at = summary.find(flow_heading)
    if description_at != 0 or flow_at <= len(description_heading):
        return (
            "summary is missing the exact PR description and change flow sections",
        )
    description = summary[
        len(description_heading):flow_at
    ].strip()
    flow = summary[flow_at + len(flow_heading):].strip()
    errors = []
    if len(description) < 20:
        errors.append("PR description must contain at least 20 characters")
    diagram = re.search(
        r"```mermaid\s*\n\s*flowchart\s+(?:LR|TD)\b[\s\S]*?```",
        flow,
        re.IGNORECASE,
    )
    if diagram is None:
        errors.append("change flow is missing a fenced Mermaid flowchart")
        return tuple(errors)

    diagram_text = diagram.group(0)
    # Status tags contain Mermaid delimiters. Outside a quoted label they
    # create nested shapes (as in PR #7078), even though all tags/classes
    # below are present. Feed this error into the existing repair pass.
    unquoted_text = re.sub(r'"[^"\r\n]*"', '', diagram_text)
    if re.search(r"\[(?:EXISTING|CHANGED|NEW|REMOVED)\]", unquoted_text, re.IGNORECASE):
        errors.append(
            'Mermaid status tags must be inside double-quoted node labels, '
            'for example A["[EXISTING] Request"]:::existing'
        )
    required_patterns = (
        (r"\[EXISTING\]", "Mermaid change flow is missing an [EXISTING] node label"),
        (r":::existing\b", "Mermaid change flow is missing a :::existing node class"),
        (r"classDef\s+existing\b", "Mermaid change flow is missing classDef existing"),
        (r"classDef\s+changed\b", "Mermaid change flow is missing classDef changed"),
        (r"classDef\s+new\b", "Mermaid change flow is missing classDef new"),
        (r"classDef\s+removed\b", "Mermaid change flow is missing classDef removed"),
    )
    errors.extend(
        message
        for pattern, message in required_patterns
        if not re.search(pattern, diagram_text, re.IGNORECASE)
    )

    statuses = {status.casefold() for status in changed_statuses}
    required_changes = []
    if "added" in statuses:
        required_changes.append(("NEW", "new"))
    if statuses & {"modified", "renamed", "copied"}:
        required_changes.append(("CHANGED", "changed"))
    if "removed" in statuses:
        required_changes.append(("REMOVED", "removed"))
    if not required_changes:
        has_changed_label = re.search(
            r"\[(?:CHANGED|NEW|REMOVED)\]", diagram_text, re.IGNORECASE
        )
        has_changed_class = re.search(
            r":::(?:changed|new|removed)\b", diagram_text, re.IGNORECASE
        )
        if not has_changed_label:
            errors.append("Mermaid change flow is missing a changed node label")
        if not has_changed_class:
            errors.append("Mermaid change flow is missing a changed node class")
    for label, node_class in required_changes:
        if not re.search(rf"\[{label}\]", diagram_text, re.IGNORECASE):
            errors.append(
                f"Mermaid change flow is missing a [{label}] node label"
            )
        if not re.search(rf":::{node_class}\b", diagram_text, re.IGNORECASE):
            errors.append(
                f"Mermaid change flow is missing a :::{node_class} node class"
            )
    if "-->" not in diagram_text:
        errors.append("Mermaid change flow is missing an arrow")
    if not flow.endswith("```"):
        errors.append("change flow must end with the Mermaid closing fence")
    return tuple(errors)
