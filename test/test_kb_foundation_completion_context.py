"""Completion localizes old source references without changing their approvals."""
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service.foundation_context import context_version, feature_context
from infermatrix_copilot.kb_service.init_knowledge_inputs import (
    SYSTEM_KNOWLEDGE_V4, SYSTEM_KNOWLEDGE_V5, knowledge_system,
)
from infermatrix_copilot.kb_service.init_support import InitError, InitRecord
from infermatrix_copilot.knowledge_service.lifecycle import Page
from test_kb_init_skeleton import world  # noqa: F401


def _stage():
    pin = "a" * 40
    lines = [f"line {n}" for n in range(1, 2001)]
    page = Page.parse("---\ntitle: Feature\ntype: architecture\nsources: []\n---\n\nOriginal approved prose.\n").with_sources([
        f"o/toy@{pin}:src/large.py:L1500-L1504",
        f"o/toy@{'b' * 40}:src/large.py:L1900-L1901",
        f"o/toy@{pin}:outside.py:L1-L2",
    ]).render()
    stage = SimpleNamespace(record=InitRecord("knowledge", "toy", pin=pin),
        lifecycle=SimpleNamespace(full_name="o/toy"), head={"repos/toy/feature-demo.md": page},
        source_index=SimpleNamespace(identity={"pin": pin}, entries={"src/large.py": {
            "status": "ready", "lines": lines, "language": "python", "sha256": "digest",
            "kind": "source", "symbols": [{"name": "late", "start": 1490, "end": 1540}]}}))
    return stage, SimpleNamespace(id="demo", page="repos/toy/feature-demo.md")


def test_completion_reads_late_enclosing_implementation_preserves_old_page_and_ignores_other_pin():
    stage, feature = _stage()
    before = dict(stage.head)
    files, docs = feature_context(stage, feature, [], [], source_limit=100000, doc_limit=30000)
    assert docs == []
    assert [(f["start"], f["end"]) for f in files] == [(1500, 1504), (1488, 1540)]
    assert "1500: line 1500" in files[0]["text"]
    assert stage.head == before
    assert stage.record.unfinished == ["feature demo: existing source locator unavailable: outside.py"]


def test_completion_bound_keeps_real_lines_and_pinned_index_mismatch_fails():
    stage, feature = _stage()
    files, _ = feature_context(stage, feature, [], [], source_limit=70, doc_limit=30000)
    assert 1500 <= files[0]["end"] < 1504
    assert files[0]["text"].splitlines()[-1] == f"{files[0]['end']}: line {files[0]['end']}"
    stage.source_index.identity["pin"] = "b" * 40
    with pytest.raises(InitError, match="index source pin differs"):
        feature_context(stage, feature, [], [], source_limit=100000, doc_limit=30000)


def test_context_version_opt_in_preserves_historical_system_and_checks_invalid_setting():
    assert context_version(SimpleNamespace(environ={})) == 4
    assert knowledge_system({"foundation_prompt_version": 4}) == SYSTEM_KNOWLEDGE_V4
    assert context_version(SimpleNamespace(environ={"KB_KNOWLEDGE_CONTEXT_VERSION": "5"})) == 5
    assert knowledge_system({"foundation_prompt_version": 5}) == SYSTEM_KNOWLEDGE_V5
    assert "Never claim that a test was run or passed" in SYSTEM_KNOWLEDGE_V5
    with pytest.raises(InitError, match="must be 4 or 5"):
        context_version(SimpleNamespace(environ={"KB_KNOWLEDGE_CONTEXT_VERSION": "6"}))


def test_long_implementation_locator_cannot_displace_reserved_test_assertions():
    stage, feature = _stage()
    test = {"path": "tests/test_late.py", "start": 1, "end": 2, "total_lines": 2,
            "language": "python", "text": "1: def test_late():\n2:     assert late() == 1",
            "test_context": "test not executed"}
    stage.source_index.entries[test["path"]] = {"kind": "test"}
    files, _ = feature_context(stage, feature, [test], [], source_limit=180, doc_limit=30000)
    assert files[0] == test
    assert "assert late() == 1" in files[0]["text"]
    assert sum(len(item["text"].encode()) for item in files) <= 180
    assert any(item["path"] == "src/large.py" for item in files)


def test_version5_retains_native_generator_and_independent_judge_binding(world, monkeypatch):
    import test_kb_foundation_parallel as fixtures
    from infermatrix_copilot.kb_service.init_knowledge_parallel import validate_checkpoint
    from test_kb_foundation_context import _audit_stage

    original = fixtures._runtime
    def runtime(*args, **kwargs):
        rt = original(*args, **kwargs)
        rt.environ = {**rt.environ, "KB_KNOWLEDGE_CONTEXT_VERSION": "5"}
        return rt
    monkeypatch.setattr(fixtures, "_runtime", runtime)
    rt, record, _, traces = fixtures._complete_native(world)
    stage = _audit_stage(rt, record)
    stage.record = record
    assert record.coverage["foundation_jobs"]["tasks"]
    validate_checkpoint(stage, record, record.inputs_digest)
    for result in record.coverage["foundation_jobs"]["tasks"].values():
        assert result["payload"]["foundation_prompt_version"] == 5
        for artifact in result["artifacts"]:
            call = traces.get(artifact["generator_receipt"]["native_trace_id"])
            assert result["payload"]["foundation_generation_policy_version"] == 1
            assert traces.blob(call["inputs"]["system"]) == knowledge_system(result["payload"])
