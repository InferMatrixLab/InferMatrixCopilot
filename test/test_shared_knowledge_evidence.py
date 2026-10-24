"""Conformance of record identities and bounded evidence across intake hosts."""

import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.knowledge_service.evidence import (
    BUGFIX_RECORD_MARKER, bugfix_record_identity, knowledge_changed_paths,
    knowledge_diff_excerpt, parse_bugfix_record, resolve_bugfix_repository,
)


def record():
    return {"schema_version": 2, "repo": "Org/Repo", "run_id": "run-1",
            "repository": {"full_name": "Org/Repo"},
            "event_id": "bugfix_run:org/repo:run-1"}


def test_identity_and_explicit_legacy_aliases():
    assert bugfix_record_identity(record()) == ("Org/Repo", "run-1", "bugfix_run:org/repo:run-1")
    assert resolve_bugfix_repository(record(), "org/repo") == "org/repo"
    legacy = {"repo": "local", "run_id": "old"}
    assert resolve_bugfix_repository(legacy, "org/repo") is None
    assert resolve_bugfix_repository(legacy, "org/repo", ("local",)) == "org/repo"
    assert resolve_bugfix_repository(record(), "other/repo", ("Org/Repo",)) is None


@pytest.mark.parametrize("patch", [
    {"schema_version": True}, {"schema_version": 3}, {"run_id": " "},
    {"repo": "other/repo"}, {"repository": {}}, {"event_id": "foreign"},
])
def test_inconsistent_identity_is_rejected(patch):
    assert bugfix_record_identity({**record(), **patch}) is None


def test_codec_uses_marked_object_and_preserves_embedded_fences():
    value = {**record(), "details": "trace:\n```\nerror\n```"}
    body = '```json\n{"untrusted": true}\n```\n' + BUGFIX_RECORD_MARKER + "\n```json\n" + json.dumps(value) + "\n```"
    assert parse_bugfix_record(body) == value
    assert parse_bugfix_record(body.rsplit("```", 1)[0]) is None


def test_mapping_and_object_evidence_have_identical_priority_and_byte_bound():
    values = [
        {"filename": "docs/readme.md", "status": "modified", "patch": "+doc"},
        {"filename": "test/test_core.py", "status": "modified", "patch": "+test"},
        {"filename": "src/core.py", "status": "modified", "patch": "+" + "界" * 5000},
    ]
    objects = [SimpleNamespace(path=x["filename"], status=x["status"], patch=x["patch"]) for x in values]
    output = knowledge_diff_excerpt(values)
    assert output == knowledge_diff_excerpt(objects)
    assert output.startswith("--- src/core.py (modified)")
    assert output.endswith("[…]\n") and len(output.encode()) <= 8192
    assert knowledge_changed_paths(["a.py", "../escape", "a.py", "/root", "C:\\x", "test\\a.py"]) == ["a.py", "test/a.py"]
