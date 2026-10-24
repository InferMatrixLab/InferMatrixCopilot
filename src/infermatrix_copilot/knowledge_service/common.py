"""Shared rule syntax, bounds, hashing, errors, and lock capability."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Final

from ..contracts.models import (
    KnowledgeApplyResult,
    KnowledgeCurationError,
)

from .format import (
    RULE_ID as _RULE_ID, APPEND_HEADING as _HEADING, FRONTMATTER as _FRONTMATTER,
    RULE_PAGE as _RULE_PAGE_NAME, PAGE_MAX_BYTES as _PAGE_MAX_BYTES,
    PAGE_MAX_LINES as _PAGE_MAX_LINES, MAX_SECTION_CHARS as _MAX_SECTION_CHARS,
)

from .validation import VALIDATOR_IDS as _VALIDATOR_IDS

_UPDATED: Final = re.compile(r"^updated:[^\r\n]*$", re.MULTILINE)
_PAGE_TYPE: Final = re.compile(r"^type:\s*(?P<type>[A-Za-z]+)\s*$", re.MULTILINE)
_MAX_SUMMARY_CHARS = 3000
# Diff evidence: enough of a change to see the contract it fixed or added,
# bounded in UTF-8 bytes (markers included) so twenty events still fit one
# model call whatever script the diff is written in.
_MAX_DIFF_BYTES = 8 * 1024
_MAX_BATCH_DIFF_BYTES = 160 * 1024
_DIFF_TRUNCATED = "\n[diff excerpt truncated by the SDK at its per-event bound]"
_DIFF_BUDGET_EXHAUSTED = "[diff excerpt omitted: the batch diff budget is exhausted]"
_MAX_ATTRIBUTE_CHARS = 3000
_MAX_CHANGED_PATHS = 50
_MAX_SOURCE_CHARS = 60

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on platforms without flock
    fcntl = None  # type: ignore[assignment]


class KnowledgeValidatorError(KnowledgeCurationError):
    """The fixed validator gate failed and all target pages were restored."""

    code = "knowledge_validator_failed"

    def __init__(self, message: str, result: KnowledgeApplyResult):
        super().__init__(message)
        self.result = result


def _apply_supported() -> bool:
    """Whether this platform can provide the apply transaction's lock."""
    return fcntl is not None


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json_for_prompt(value: object) -> str:
    """Serialize untrusted data without allowing it to close the data fence."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    return encoded.replace("<", r"\u003c").replace(">", r"\u003e")
