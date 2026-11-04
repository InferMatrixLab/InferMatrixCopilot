"""Observe exact adaptive provider packets without reading their sources again."""

from __future__ import annotations

import hashlib
import json
import re

from .references import KnowledgeTraceError

_FENCE = re.compile(r"\n<untrusted_data>\n(.*?)\n</untrusted_data>\n", re.DOTALL)


def packet_fragments(packet: dict) -> list[dict]:
    """Extract delivered text and its conservative source coordinates.

    ``documents`` contains references, not the budgeted delivery. Only complete
    provider-rendered fences in ``model_content`` can produce exposures. A
    continuation or a transformed/search fragment has unknown source offsets.
    ``wire_content`` is retained for matching the final Strict request boundary.
    """
    content = packet.get("model_content", "")
    if not isinstance(content, str):
        raise KnowledgeTraceError("adaptive model_content must be a string")
    expected = packet.get("model_content_sha256")
    if expected and expected != hashlib.sha256(content.encode("utf-8")).hexdigest():
        raise KnowledgeTraceError("adaptive model_content hash mismatch")
    fragments = []
    matches = list(_FENCE.finditer(content))
    # A malformed packet must not silently claim an empty, complete capture.
    if _FENCE.sub("", content).strip():
        raise KnowledgeTraceError("adaptive model_content has unframed content")
    for match in matches:
        try:
            fragment = json.loads(match[1])
        except (ValueError, TypeError) as exc:
            raise KnowledgeTraceError("adaptive source fragment is not valid JSON") from exc
        if (not isinstance(fragment, dict) or not isinstance(fragment.get("path"), str)
                or not fragment["path"] or not isinstance(fragment.get("content"), str)):
            raise KnowledgeTraceError("adaptive source fragment must name its path and content")
        offset = fragment.get("offset")
        if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
                or fragment.get("continuation")):
            offset = None
        fragments.append({**fragment, "document_id": fragment["path"],
            "offset": offset, "offset_unit": "unicode_codepoints",
            "source_length": len(fragment["content"]),
            "truncated": bool(fragment.get("partial")),
            "source_pins": fragment.get("source_pins", []),
            "representation": "excerpt", "wire_content": match[0]})
    return fragments
