"""Validate explicit rechecks without deriving resolution from omission."""
from __future__ import annotations

import re
from ..contracts.models import InvalidRequestError, ResultDecodeError


def validate_carried(rows):
    ids = set()
    if not isinstance(rows, (list, tuple)) or not all(isinstance(row, dict) for row in rows):
        raise InvalidRequestError("carried findings must be a list of objects")
    if len(rows) > 200:
        raise InvalidRequestError("at most 200 carried findings per review")
    for row in rows:
        identity = row.get("finding_id")
        if not isinstance(identity, str) or not identity.strip() or identity in ids:
            raise InvalidRequestError("carried findings require unique nonempty identities")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", str(row.get("source_head_sha") or "")):
            raise InvalidRequestError("carried finding source_head_sha must be a full SHA")
        if not str(row.get("title") or "").strip():
            raise InvalidRequestError("carried finding title must not be empty")
        ids.add(identity)
    return ids


def checked_rechecks(carried, records, head_sha):
    """Return only safe records and explicit gaps; duplicates never resolve."""
    expected = {row["finding_id"] for row in carried}
    missing = []
    safe = []
    seen = set()
    duplicates = set()
    if not isinstance(records, (list, tuple)):
        records = ()
    for row in records:
        if not isinstance(row, dict):
            missing.append("invalid finding recheck")
            continue
        identity = row.get("finding_id")
        if not isinstance(identity, str) or identity not in expected:
            missing.append("recheck names an unknown carried finding")
            continue
        if identity in seen:
            duplicates.add(identity)
            missing.append("duplicate finding recheck: " + identity)
        seen.add(identity)
        if (row.get("head_sha") != head_sha
            or not isinstance(row.get("outcome"), str)
            or row.get("outcome") not in {"fixed", "still_affected", "unverified"}
            or not isinstance(row.get("evidence"), str) or not row["evidence"].strip()):
            missing.append("invalid or wrong-head finding recheck: " + identity)
            continue
        safe.append({key: row[key] for key in ("finding_id", "head_sha", "outcome", "evidence")})
    safe = [row for row in safe if row["finding_id"] not in duplicates]
    answered = {row["finding_id"] for row in safe}
    missing.extend("missing finding recheck: " + identity for identity in sorted(expected - answered))
    return safe, missing


# Dispositions the provider uses for a candidate it decided NOT to raise.
# `over_budget` is deliberately absent: that candidate was publishable and
# lost a slot to the comment budget, which is not a review decision.
WITHHELD_DISPOSITIONS = frozenset({
    "excluded", "duplicate", "resolved", "no_issue",
})


def _anchor_of(comment: dict) -> str:
    """`file:line`, matching how the provider anchors its own records.

    Only file-bearing comments are anchored here. The provider renders a
    fileless finding as `general` or `PR description`, which several findings
    can share, so an anchor match there would not identify one finding; those
    are left to the provider's own finalization rather than guessed at.
    """
    file = str(comment.get("file") or "").strip()
    if not file or file == "?":
        return ""
    line = comment.get("line")
    return f"{file}:{line if line is not None else '?'}"


def check_disposition_proof(comments: list, dispositions: object) -> None:
    """Publish nothing the review itself decided not to raise.

    The provider applies its own selection before answering, so this is the
    second lock, not the first: it catches a result whose published list
    disagrees with the finalized set it shipped alongside — the shape of
    JiusiServe/InferMatrixCopilot#141, where a request the summary said to
    drop was published inline anyway.

    An older provider sends no dispositions and is published unchanged. An
    anchor recorded BOTH ways is not a contradiction (two findings can share
    one), so only anchors recorded exclusively as withheld refuse.
    """
    if not isinstance(dispositions, list) or not dispositions:
        return
    withheld: set[str] = set()
    published: set[str] = set()
    for record in dispositions:
        if not isinstance(record, dict):
            continue
        anchor = str(record.get("anchor") or "")
        if not anchor:
            continue
        if str(record.get("disposition") or "") in WITHHELD_DISPOSITIONS:
            withheld.add(anchor)
        else:
            published.add(anchor)
    refused = withheld - published
    if not refused:
        return
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        anchor = _anchor_of(comment)
        if anchor and anchor in refused:
            raise ResultDecodeError(
                f"result publishes {anchor}, which its own finalized set "
                f"withheld; refusing to publish a review that contradicts "
                f"itself"
            )
