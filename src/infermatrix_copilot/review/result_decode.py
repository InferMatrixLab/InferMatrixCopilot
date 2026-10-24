"""Pure review wire decoding and finding projection shared by all consumers."""
from __future__ import annotations

from typing import Any
from .severity import STRICT_TO_PRIORITY
from ..contracts.models import (ResultDecodeError, FindingRecheck, StrictReviewResult,
    QualityReviewResult, ReviewFinding, ReviewResult)

def _object_rows(raw: Any, field: str) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise ResultDecodeError(f"{field} must be an array of objects")
    return tuple(raw)


def _diagnostics(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ResultDecodeError("diagnostics must be an object")
    return raw


def _review_result(raw: Any) -> StrictReviewResult | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ResultDecodeError("Strict result must be an object")
    rechecks = []
    for row in _object_rows(raw.get("finding_rechecks"), "finding_rechecks"):
        outcome = row.get("outcome")
        if not isinstance(outcome, str) or outcome not in {"fixed", "still_affected", "unverified"}:
            raise ResultDecodeError("finding recheck outcome is invalid")
        try:
            rechecks.append(FindingRecheck(**row))
        except (TypeError, ValueError) as exc:
            raise ResultDecodeError(f"invalid finding recheck: {exc}") from exc
    missing = raw.get("recheck_missing") or []
    if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
        raise ResultDecodeError("recheck_missing must be an array of strings")
    complete = raw.get("rechecks_complete", False)
    if not isinstance(complete, bool):
        raise ResultDecodeError("rechecks_complete must be a boolean")
    return StrictReviewResult(
        contract_version=str(raw.get("contract_version") or ""),
        reviewed_head_sha=str(raw.get("reviewed_head_sha") or ""),
        verdict=str(raw.get("verdict") or ""),
        summary_markdown=str(raw.get("summary_markdown") or ""),
        comments=_object_rows(raw.get("comments"), "comments"),
        findings=_object_rows(raw.get("findings"), "findings"),
        finding_rechecks=tuple(rechecks),
        rechecks_complete=complete,
        recheck_missing=tuple(missing),
        stale=bool(raw.get("stale", False)),
        diagnostics=_diagnostics(raw.get("diagnostics")),
        finding_dispositions=_object_rows(raw.get("finding_dispositions"), "finding_dispositions"),
        expected_head_sha=str(raw.get("expected_head_sha") or ""),
        actual_head_sha=str(raw.get("actual_head_sha") or ""),
        direct_result=raw.get("direct_result"),
    )


def _quality_result(raw: Any) -> QualityReviewResult | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ResultDecodeError("quality result must be an object")
    return QualityReviewResult(
        contract_version=str(raw.get("contract_version") or ""),
        reviewed_head_sha=str(raw.get("reviewed_head_sha") or ""),
        verdict=str(raw.get("verdict") or ""),
        confidence=str(raw.get("confidence") or ""),
        summary=str(raw.get("summary") or ""),
        reasons=_object_rows(raw.get("reasons"), "reasons"),
        stale=bool(raw.get("stale", False)),
        diagnostics=_diagnostics(raw.get("diagnostics")),
    )


def decode_review_result(raw, *, expected_head_sha, required_checks=None, finding_decoder=None):
    """Project either mode's wire result into the one host-facing result type."""
    if not raw.get("contract_version"):
        raise ResultDecodeError("result carries no contract_version")
    reviewed = str(raw.get("reviewed_head_sha") or "")
    if reviewed != expected_head_sha:
        raise ResultDecodeError(f"reviewed head {reviewed[:12]} != attempt head {expected_head_sha[:12]}")
    if isinstance(raw.get("direct_result"), dict):
        from .review_result import parse_direct_result

        proof = raw.get("diagnostics") or {}
        references = frozenset(proof.get("thread_references") or ())
        return parse_direct_result(raw["direct_result"],
            required_checks=proof.get("required_checks", ()) if required_checks is None else required_checks,
            normalize_reference=lambda value: value if value in references else "")
    typed = _review_result(raw)
    if not typed.summary_markdown.strip():
        raise ResultDecodeError("result carries no summary_markdown")
    if typed.verdict.strip().upper() not in {"REQUEST CHANGES", "COMMENT", "APPROVE"}:
        raise ResultDecodeError(f"unknown verdict {typed.verdict!r}")
    from .rechecks import check_disposition_proof

    check_disposition_proof(list(typed.comments), list(typed.finding_dispositions))
    return ReviewResult(reviewed_head_sha=reviewed, summary=typed.summary_markdown.strip(),
        findings=tuple((finding_decoder or strict_finding)(comment) for comment in typed.comments),
        subtraction_signal="none", review_checks={},
        finding_rechecks=tuple(raw.get("finding_rechecks") or ()),
        review_complete=raw.get("rechecks_complete", True) is True)


def strict_finding(comment: object, *, title=None) -> ReviewFinding:
    """One contract comment → one `Finding`.

    The shapes differ deliberately: the contract carries
    `file/comment/evidence/suggestion`, this repo renders
    `severity/title/body/path/line`. Evidence and suggestion are folded into
    the body rather than dropped — they are the part a maintainer acts on."""
    if not isinstance(comment, dict):
        raise ResultDecodeError("comment is not an object")
    # A finding without a file is a GENERAL one — a concern about the change
    # as a whole rather than a line of it, which the review taxonomy allows.
    # Rejecting it discarded an entire completed review (a real 40-minute
    # Strict run, over one such finding). It cannot anchor inline, so it
    # renders in the body, which is exactly where a general finding belongs.
    path = str(comment.get("file") or "").strip()
    severity = STRICT_TO_PRIORITY.get(str(comment.get("severity") or "").lower())
    if severity is None:
        # Severity still fails closed: guessing one could silently demote a
        # blocker, which is the opposite of a missing file's consequence.
        raise ResultDecodeError(
            f"unknown severity {comment.get('severity')!r} for "
            f"{path or '(no file)'}"
        )
    text = str(comment.get("comment") or "").strip()
    if not text:
        raise ResultDecodeError(
            f"comment for {path or '(no file)'} is empty"
        )
    line = comment.get("line")
    if not path:
        line = None          # a line number without a file anchors nothing
    if line is not None:
        try:
            line = int(line)
        except (TypeError, ValueError):
            raise ResultDecodeError(
                f"comment for {path or '(no file)'} has a non-numeric line"
            ) from None
        if line <= 0:
            line = None
    body = text
    evidence = str(comment.get("evidence") or "").strip()
    if evidence:
        body += f"\n\n**Evidence:** {evidence}"
    suggestion = str(comment.get("suggestion") or "").strip()
    if suggestion:
        body += f"\n\n**Suggestion:** {suggestion}"
    return ReviewFinding(
        severity=severity, title=title(text) if title else text, body=body,
        path=path, line=line,
    )
