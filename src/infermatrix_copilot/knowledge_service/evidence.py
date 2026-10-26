"""Pure knowledge evidence codecs shared by intake hosts.

Hosts retain authentication, polling cursors and delivery receipts. This module
owns record identity and the bounded evidence presented to knowledge workflows.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import PurePosixPath

BUGFIX_RECORD_MARKER = "<!-- infermatrix-copilot:bugfix-record:v2 -->"
LEGACY_BUGFIX_RECORD_MARKER = "<!-- infermatrix-copilot:bugfix-record:v1 -->"
BUGFIX_MARKERS = (BUGFIX_RECORD_MARKER, LEGACY_BUGFIX_RECORD_MARKER)
DIFF_EXCERPT_BYTES = 8 * 1024


def bugfix_record_identity(record) -> tuple[str, str, str] | None:
    """Return repository, run and event identity, rejecting inconsistent v2 data.

    Legacy aliases are returned unchanged for the host's explicit alias map.
    Parsing identity never grants permission to consume a mailbox or drop file.
    """
    if not isinstance(record, dict):
        return None
    version = record.get("schema_version", 1)
    if type(version) is not int or version not in (1, 2):
        return None
    repo, run = record.get("repo"), record.get("run_id")
    if not isinstance(repo, str) or not repo.strip() or not isinstance(run, str) or not run.strip():
        return None
    event_id = f"bugfix_run:{repo.casefold()}:{run}"
    if version == 2:
        identity = record.get("repository")
        if not isinstance(identity, dict) or identity.get("full_name") != repo:
            return None
        if "/" not in repo or any(not part for part in repo.split("/")):
            return None
        if record.get("event_id") != event_id:
            return None
    else:
        event_id = str(record.get("event_id") or event_id)
    return repo, run, event_id


def resolve_bugfix_repository(record, full_name: str, aliases=()) -> str | None:
    """Resolve a record for one host's canonical repository and explicit aliases."""
    identity = bugfix_record_identity(record)
    if identity is None:
        return None
    repo, _, _ = identity
    if repo.casefold() == full_name.casefold():
        return full_name
    if record.get("schema_version", 1) == 1 and "/" not in repo:
        if repo.casefold() in {str(alias).casefold() for alias in aliases}:
            return full_name
    return None


def parse_bugfix_record(body: str) -> dict | None:
    """Decode the fenced object following a record marker, including quoted fences."""
    if not isinstance(body, str):
        return None
    positions = [(body.find(marker), marker) for marker in BUGFIX_MARKERS if marker in body]
    if not positions:
        return None
    position, marker = min(positions)
    rest = body[position + len(marker):]
    start = rest.find("```json")
    if start < 0:
        return None
    rest = rest[start + len("```json"):].lstrip()
    try:
        record, end = json.JSONDecoder().raw_decode(rest)
    except ValueError:
        return None
    if not rest[end:].lstrip().startswith("```"):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("run_id"), str) or not record["run_id"].strip():
        return None
    return record


def knowledge_changed_paths(values, *, limit: int | None = 50) -> list[str]:
    """Normalize contained relative paths without changing their source order."""
    paths = []
    for value in values:
        normalized = str(value).strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if not normalized or path.is_absolute() or ".." in path.parts or (
            path.parts and path.parts[0].endswith(":")
        ):
            continue
        if normalized not in paths:
            paths.append(normalized)
        if limit is not None and len(paths) >= limit:
            break
    return paths


def _field(changed, name: str, default=""):
    if isinstance(changed, Mapping):
        return changed.get("filename" if name == "path" and "filename" in changed else name, default)
    return getattr(changed, name, default)


def _diff_priority(changed):
    lower = str(_field(changed, "path")).lower()
    if lower.endswith((".md", ".rst", ".txt")):
        return 2, lower
    if lower.startswith(("tests/", "test/", "docs/", "doc/", "examples/")) or "/tests/" in lower or "/test/" in lower:
        return 1, lower
    return 0, lower


def knowledge_diff_excerpt(files, *, max_bytes: int = DIFF_EXCERPT_BYTES) -> str:
    """Select source before tests/docs and bound UTF-8 bytes, marking truncation."""
    if max_bytes <= 0:
        return ""
    parts, used = [], 0
    for changed in sorted(files, key=_diff_priority):
        patch = str(_field(changed, "patch") or "").strip()
        if not patch:
            continue
        path = str(_field(changed, "path"))
        status = str(_field(changed, "status") or "")
        heading = f"--- {path}" + (f" ({status})" if status else "")
        block = f"{heading}\n{patch}\n"
        data = block.encode("utf-8")
        if used + len(data) > max_bytes:
            room = max_bytes - used
            if room >= 256:
                marker = "\n[…]\n"
                parts.append(data[:room - len(marker.encode("utf-8"))].decode("utf-8", "ignore") + marker)
            break
        parts.append(block)
        used += len(data)
    return "".join(parts)
