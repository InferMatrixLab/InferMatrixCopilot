"""Canonical knowledge syntax and capacity; admission policies live with callers."""

from __future__ import annotations

import re

import yaml

RULE_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9-]{1,40}"
RULE_ID = re.compile(RULE_ID_PATTERN)
APPEND_HEADING = re.compile(rf"^##\s+(?P<rule>{RULE_ID_PATTERN})\s+[—-]\s+\S")
RULE_HEADING = re.compile(rf"^(?P<level>#{{2,3}})\s+(?P<rule>{RULE_ID_PATTERN})\s+[—-]\s+\S")
ANY_RULE_HEADING = re.compile(rf"^#{{2,6}}\s+(?P<rule>{RULE_ID_PATTERN})(?=\s|$)", re.MULTILINE)
RULE_PAGE = re.compile(r"rules(?:-[a-z0-9][a-z0-9-]*)?\.md")
FRONTMATTER = re.compile(
    r"\A---(?P<open>\r?\n)(?P<body>.*?)(?P<close>\r?\n)---(?P<after>\r?\n|\Z)",
    re.DOTALL,
)
PAGE_MAX_BYTES = 32 * 1024
PAGE_MAX_LINES = 500
PAGE_WARN_BYTES = 16 * 1024
PAGE_WARN_LINES = 300
MAX_SECTION_CHARS = 16 * 1024


def frontmatter_value(text: str):
    """Decode only a leading, complete metadata block; preserve YAML value types."""
    match = FRONTMATTER.match(text)
    return yaml.safe_load(match.group("body")) if match else None


def page_over_capacity(text: str) -> str:
    size = len(text.encode("utf-8"))
    lines = sum(1 for line in text.splitlines() if line.strip())
    if size >= PAGE_MAX_BYTES or lines >= PAGE_MAX_LINES:
        return f"{size} bytes / {lines} non-empty lines (limit < {PAGE_MAX_BYTES} / < {PAGE_MAX_LINES})"
    return ""
