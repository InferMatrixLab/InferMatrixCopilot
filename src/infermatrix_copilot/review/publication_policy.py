"""Effect-free publication rules shared by standalone and bot hosts."""

from __future__ import annotations

from collections import Counter


def review_evidence_error(*, mode, result=None, expected_status=None,
                          normalize_reference=None, allowed_references=frozenset(),
                          comments=(), dispositions=()):
    """Mode-specific evidence gate, shared by execution and publishing hosts.

    Hosts supply frozen identities and identifier normalization. Final GitHub
    head reads and delivery protections remain host responsibilities.
    """
    if mode == "strict":
        from .rechecks import check_disposition_proof
        from ..contracts.models import ResultDecodeError

        try:
            check_disposition_proof(list(comments), list(dispositions))
        except ResultDecodeError as exc:
            return str(exc)
        return ""
    if mode != "direct":
        raise ValueError("unsupported review evidence mode")
    from .dispositions import feedback_disposition_error

    problem = feedback_alignment_error(result, expected_status)
    if problem:
        return problem
    return feedback_disposition_error(result, normalize_reference=normalize_reference,
                                      allowed_references=allowed_references)


def feedback_alignment_error(result, expected_status: str) -> str:
    """Bind feedback dispositions to the actual final findings and frozen policy."""
    if result.existing_feedback_status != expected_status:
        return ("existing_feedback_status does not match the frozen review "
                f"context ({result.existing_feedback_status!r} != {expected_status!r})")
    dispositions = result.finding_dispositions
    expected = Counter(f"{finding.path}:{finding.line or 'general'}" for finding in result.findings)
    if any(count != 1 for count in expected.values()):
        return "final findings must use unique path:line anchors; merge concerns that share one location"
    if expected_status != "checked":
        return "finding dispositions require checked existing feedback" if dispositions else ""
    if len(dispositions) != len(result.findings):
        return ("checked feedback needs exactly one disposition per finding "
                f"({len(dispositions)} != {len(result.findings)})")
    actual = Counter(str(item.get("anchor") or "") for item in dispositions)
    if actual != expected:
        return "finding disposition anchors do not match final findings"
    return ""


def review_head_error(reviewed_head: str, current_head: str) -> str:
    """An empty result authorizes a head-bound payload; every other result refuses it."""
    if not reviewed_head:
        return "reviewed PR head SHA is missing; review the current head before publication"
    if not current_head:
        return "current PR head SHA is missing; publication cannot be verified"
    if reviewed_head != current_head:
        return (f"PR head changed after review ({reviewed_head[:12]} → {current_head[:12]}); "
                "review the new head before publication")
    return ""
