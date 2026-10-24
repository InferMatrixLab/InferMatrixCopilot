"""Canonical disposition semantics with explicit wire compatibility policies."""
from __future__ import annotations

import re


class DispositionError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_disposition(value, *, strict=False, normalize_reference=None, allowed_references=None):
    """Validate and return a normalized reference without performing host I/O."""
    kind, thread, recheck = (value[key] for key in ("disposition", "existing_thread", "head_recheck"))
    if (re.fullmatch(r".+:(?:[1-9][0-9]*|general)", value["anchor"]) is None if strict else ":" not in value["anchor"]):
        raise DispositionError("anchor", "finding disposition anchor is invalid")
    if kind not in {"new", "duplicate", "extends_existing", "resolved_or_outdated"}:
        raise DispositionError("classification", "finding disposition is invalid")
    if kind != "new":
        thread = normalize_reference(thread) if normalize_reference else thread
        if not thread:
            raise DispositionError("reference", "non-new finding disposition needs a canonical existing_thread")
        if allowed_references is not None and thread not in allowed_references:
            raise DispositionError("membership", "existing_thread is not a member of the frozen pr_review_context")
    elif strict and thread:
        raise DispositionError("new_reference", "new finding disposition cannot name existing_thread")
    if kind == "resolved_or_outdated" and recheck not in {"fixed", "still_affected"}:
        raise DispositionError("recheck", "resolved finding needs fixed/still_affected head_recheck")
    if strict and kind != "resolved_or_outdated" and recheck:
        raise DispositionError("recheck", "head_recheck is only valid for resolved findings")
    return thread


def disposition(item, *, strict=False, normalize_reference=None):
    """Shared finding semantics; strict wire decoding also checks shape and thread identity."""
    keys = {"anchor", "disposition", "existing_thread", "head_recheck"}
    if not isinstance(item, dict) or (strict and set(item) != keys):
        raise ValueError("each finding disposition must contain exactly " + ", ".join(sorted(keys)))
    value = {key: str(item.get(key, "")) for key in keys}
    if not strict:
        value = {key: text.strip().casefold() if key in ("disposition", "head_recheck") else text.strip()
                 for key, text in value.items()}
    value["existing_thread"] = validate_disposition(
        value, strict=strict, normalize_reference=normalize_reference if strict else None)
    return value


def feedback_disposition_error(result, *, normalize_reference, allowed_references):
    """Repeat canonical semantics at delivery, tolerating legacy missing fields."""
    for item in result.finding_dispositions:
        value = {key: str(item.get(key, "")) for key in
                 ("anchor", "disposition", "existing_thread", "head_recheck")}
        try:
            validate_disposition(value, strict=True, normalize_reference=normalize_reference,
                                 allowed_references=allowed_references)
        except DispositionError as exc:
            if exc.code == "reference":
                return "non-new finding disposition has invalid or unsafe existing_thread"
            return str(exc)
    return ""
