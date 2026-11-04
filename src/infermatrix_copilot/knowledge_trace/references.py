"""Versioned knowledge identities and conservative Markdown rule references."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath


class KnowledgeTraceError(ValueError):
    """Invalid knowledge trace input or unavailable required recording."""


def digest(value) -> str:
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def document_scope(document_id: str, repository: str) -> str:
    """Accept only a relative Markdown path inside general or this repository."""
    if not isinstance(document_id, str) or not document_id or "\\" in document_id:
        raise KnowledgeTraceError("document_id must be a knowledge-relative Markdown path")
    path = PurePosixPath(document_id)
    if path.is_absolute() or any(p in ("", ".", "..") for p in document_id.split("/")):
        raise KnowledgeTraceError("document_id escapes the knowledge root")
    if path.suffix.lower() != ".md":
        raise KnowledgeTraceError("knowledge references must name Markdown documents")
    if document_id in {"AGENTS.md", "README.md", "repos/_index.md"}:
        return "navigation"
    parts = path.parts
    if len(parts) >= 2 and parts[0] == "general":
        return "general"
    if len(parts) >= 3 and parts[:2] == ("repos", repository):
        return repository
    raise KnowledgeTraceError("knowledge document is outside the selected repository scope")


_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_RULE = re.compile(r"^`?([A-Z][A-Z0-9_]*(?:-[A-Z0-9_]+)*-\d+[a-z]*)`?(?=\s|[—–:：]|$)")


def extract_rule_sections(content: str) -> list[dict]:
    """Only exact heading IDs count; prose and quick-map mentions never do.

    Positions are Unicode code points within the supplied visible text. Fenced
    examples are ignored, and a section stops at the next peer/ancestor heading.
    A section reaching the end of an excerpt is not evidence of completeness.
    """
    headings = []
    fenced = False
    fence_character = ""
    offset = 0
    for line in content.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            char = stripped[0]
            if not fenced:
                fenced, fence_character = True, char
            elif char == fence_character:
                fenced = False
            offset += len(line)
            continue
        match = _HEADING.match(line.rstrip("\r\n")) if not fenced else None
        if match:
            headings.append((offset, len(match.group(1)), match.group(2), offset + len(line)))
        offset += len(line)
    sections = []
    for position, (start, level, title, body_start) in enumerate(headings):
        match = _RULE.match(title)
        if not match:
            continue
        following = next((h[0] for h in headings[position + 1:] if h[1] <= level), len(content))
        sections.append({"rule_id": match.group(1), "start": start, "end": following,
                         "body_start": body_start, "has_body": bool(content[body_start:following].strip()),
                         "closed_by_heading": following < len(content),
                         "section_sha256": digest(content[start:following])})
    return sections


def knowledge_reference(document_id: str, repository: str, snapshot: str, document_sha256: str,
                        *, rule_id: str = "", section_sha256: str = "") -> dict:
    namespace = document_scope(document_id, repository)
    identity = {"knowledge_namespace": namespace, "document_id": document_id, "rule_id": rule_id,
                "snapshot_id": snapshot, "document_sha256": document_sha256,
                "section_sha256": section_sha256}
    # An excerpt's section hash is evidence about that exposure, not a new
    # rule identity. The versioned document and exact heading own the identity.
    identity_fields = {k: v for k, v in identity.items() if k != "section_sha256"}
    return {"knowledge_ref_id": "kr-" + digest(identity_fields)[:32], **identity}
