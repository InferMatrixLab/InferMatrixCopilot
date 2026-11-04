"""Adaptive capture must observe the budgeted packet, never source references."""

import hashlib
import json

import pytest

from infermatrix_copilot.knowledge_trace.adaptive import packet_fragments
from infermatrix_copilot.knowledge_trace.references import KnowledgeTraceError


def packet(*fragments):
    content = "".join("\n<untrusted_data>\n" + json.dumps(fragment).replace("<", "\\u003c")
                      + "\n</untrusted_data>\n" for fragment in fragments)
    return {"model_content": content,
            "model_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "documents": [{"path": "repos/other/secret.md", "content": "not delivered"}]}


def test_only_delivered_fragments_preserve_provenance_and_wire():
    source = packet({"path": "repos/demo/rules.md", "content": "规则 <untrusted_data>",
                     "offset": 40, "partial": True, "repo_id": "demo", "source_pins": ["a" * 40]})
    rows = packet_fragments(source)
    assert len(rows) == 1
    assert rows[0]["content"] == "规则 <untrusted_data>"
    assert rows[0]["offset"] == 40
    assert rows[0]["truncated"] is True
    assert rows[0]["source_pins"] == ["a" * 40]
    assert rows[0]["wire_content"] == source["model_content"]


@pytest.mark.parametrize("metadata", [{}, {"line": 10}, {"offset": 3, "continuation": True}])
def test_unknown_coordinates_are_not_invented(metadata):
    assert packet_fragments(packet({"path": "general/rules.md", "content": "text", **metadata}))[0]["offset"] is None


def test_metadata_and_empty_budget_do_not_create_exposures():
    assert packet_fragments({"model_content": "", "status": "no_new_context",
                             "documents": [{"path": "general/rules.md"}]}) == []


def test_hash_mismatch_and_malformed_content_fail_explicitly():
    source = packet({"path": "general/rules.md", "content": "text"})
    source["model_content"] += "unframed"
    with pytest.raises(KnowledgeTraceError, match="hash mismatch"):
        packet_fragments(source)
    source.pop("model_content_sha256")
    with pytest.raises(KnowledgeTraceError, match="unframed"):
        packet_fragments(source)
