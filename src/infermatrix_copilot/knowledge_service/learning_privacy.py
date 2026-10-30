"""Reject recognizable secrets and chat exports before automatic learning.

This is a guard against accidental disclosure, not a claim to detect arbitrary
private information. The host must still select and redact distilled lessons.
"""

from __future__ import annotations

import re


class LearningPrivacyError(ValueError):
    """A distilled payload needs redaction before it may be retained or sent."""


_CREDENTIAL = re.compile(
    r"(?:\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b|"
    r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b|\bAKIA[0-9A-Z]{16}\b|"
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|"
    r"(?i:\bbearer\s+[A-Za-z0-9._~+/=-]{20,})|"
    r"https?://[^\s/:@]+:[^\s/@]+@)"
)
_ROLE = re.compile(r'''["']role["']\s*:\s*["'](?:user|assistant|system)["']''', re.IGNORECASE)
_CHAT_LINE = re.compile(r"^\s*(?:user|assistant|system)\s*:\s*", re.IGNORECASE | re.MULTILINE)


def validate_distilled_text(value) -> None:
    """Validate bounded structured lessons/proposals without echoing secrets."""
    if isinstance(value, dict):
        for key, item in value.items():
            validate_distilled_text(key)
            validate_distilled_text(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_distilled_text(item)
    elif isinstance(value, str):
        if _CREDENTIAL.search(value):
            raise LearningPrivacyError("distilled knowledge contains credential-like material; redact it before retrying")
        if len(value) > 400 and (len(_ROLE.findall(value)) > 1 or len(_CHAT_LINE.findall(value)) > 1):
            raise LearningPrivacyError("send distilled lessons instead of a conversation export")
