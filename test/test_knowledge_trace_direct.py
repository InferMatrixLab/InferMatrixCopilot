"""Knowledge traces distinguish provider payloads from host-declared usage."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from infermatrix_copilot.sdk.v1 import (
    ChangedPath, DirectClient, DirectCompletionRequest, DirectReviewRequest,
    InvalidRequestError, KnowledgeTraceClient, RepositoryRef,
)
from infermatrix_copilot.knowledge_view import KnowledgeView
from infermatrix_copilot.thin_mcp_server import build_mcp

HEAD = "a" * 40


def test_shipped_review_workflow_carries_trace_identity_and_explicit_usage():
    from infermatrix_copilot.upgrade.workflows import get_workflow

    content = get_workflow("imreview")["content"]
    assert "pinned `expected_head_sha`" in content
    assert "`trace_id` and `repo` to" in content
    assert "record_knowledge_usage" in content
    assert '"source_exposure_id"' in content
    assert '"evidence_registered"' in content
    assert '"assessment_recorded"' in content
    assert '`status="interrupted"`' in content


def request():
    return DirectReviewRequest(
        review_id="trace-attempt", repository=RepositoryRef("vllm-omni"), pr_number=7,
        expected_head_sha=HEAD, title="Fix runtime scheduling",
        body="Correct the affected runtime path.",
        changed_paths=(ChangedPath("vllm_omni/core/sched/scheduler.py", "modified"),),
    )


def test_sdk_records_every_final_document_excerpt_and_embedded_map(tmp_path):
    client = DirectClient(trace_root=tmp_path)
    plan = client.plan(request())
    assert plan.knowledge_trace["status"] == "ready"
    trace_id = plan.knowledge_trace["trace_id"]
    report = client.knowledge_trace.get_report(trace_id, repository="vllm-omni")
    expected = [plan.knowledge_entry, *plan.document_maps, *plan.mandatory_review_guides,
                *(route.document for route in plan.knowledge_routes)]
    for document in expected:
        assert any(exposure["document_id"] == document.document_id
                   and exposure["representation"] in {"excerpt", "full_document"}
                   and exposure["reference"]["document_sha256"] == document.sha256
                   for exposure in report["exposures"])
    quick_map = next(exposure for exposure in report["exposures"] if exposure["representation"] == "quick_map")
    assert quick_map["rule_refs"]
    assert {ref["coverage"] for ref in quick_map["rule_refs"]} == {"reference_only"}
    assert {e["boundary"] for e in report["exposures"]} == {"provider_response"}
    assert report["assessments"] == []
    assert report["session"]["source_revision"] == HEAD


def test_sdk_related_document_refs_are_recorded_beside_selected_prose(tmp_path):
    client = DirectClient(trace_root=tmp_path)
    plan = client.plan(replace(request(), repository=RepositoryRef("jiuwenswarm"),
        title="Model platform configuration", body="models API",
        changed_paths=(ChangedPath("jiuwenswarm/common/model_catalog.py"),)))
    report = client.knowledge_trace.get_report(plan.knowledge_trace["trace_id"])
    for document in plan.related_knowledge["documents"]:
        representations = {e["representation"] for e in report["exposures"] if e["document_id"] == document["path"]}
        assert "related_excerpt" in representations
        assert representations & {"full_document", "excerpt"}


def test_sdk_host_receipts_and_assessments_are_explicit_and_scope_checked(tmp_path):
    trace = KnowledgeTraceClient(root=tmp_path)
    session = trace.start_session("vllm-omni", source_revision=HEAD)
    trace_id = session["trace_id"]
    exposure = trace.recorder.provide(trace_id, step_id="review", document_id="repos/vllm-omni/rules.md",
        content="## TEST-1 Zero values\nPreserve valid zero values.\n", boundary="provider_response", representation="full_document")["result"]
    events = [
        {"event_type": "content_provided", "step_id": "defaults", "exposure": {
            "source_exposure_id": exposure["exposure_id"], "boundary": "host_context"}},
        {"event_type": "evidence_registered", "step_id": "defaults", "evidence": {
            "evidence_id": "zero-test", "path": "test/test_defaults.py", "source_revision": HEAD,
            "evidence_origin": "captured", "content": "zero-value test passed"}},
        {"event_type": "assessment_recorded", "step_id": "defaults", "assessment": {
            "knowledge_ref_id": exposure["rule_refs"][0]["knowledge_ref_id"],
            "exposure_ids": [exposure["exposure_id"]], "applicability": "applicable",
            "verification": "supported", "check": "Check the zero-value default path.",
            "rationale": "The request explicitly accepts zero.", "evidence_ids": ["zero-test"]}},
    ]
    trace.record_events(trace_id, events, repository="vllm-omni", idempotency_key="defaults")
    report = trace.get_report(trace_id)
    assert len(report["assessments"]) == 1
    assert report["evidence"][0]["evidence_origin"] == "host_reported"
    assert any(e["boundary"] == "host_context" for e in report["exposures"])
    with pytest.raises(InvalidRequestError, match="different repository"):
        trace.record_events(trace_id, [], repository="afd-plugin")
    with pytest.raises(InvalidRequestError, match="host_context"):
        trace.record_events(trace_id, [{"event_type": "content_provided", "exposure": {"boundary": "model_request"}}])


def fake_mcp(monkeypatch):
    fast_module = ModuleType("mcp.server.fastmcp")
    class FakeMCP:
        def __init__(self, *_args, **_kwargs):
            self.tools = {}
        def tool(self, **_kwargs):
            def register(fn):
                self.tools[fn.__name__] = fn
                return fn
            return register
    fast_module.FastMCP = FakeMCP
    annotations = ModuleType("mcp.types")
    annotations.ToolAnnotations = lambda **kwargs: SimpleNamespace(**kwargs)
    for name, module in {"mcp": ModuleType("mcp"), "mcp.server": ModuleType("mcp.server"),
                         "mcp.server.fastmcp": fast_module, "mcp.types": annotations}.items():
        monkeypatch.setitem(sys.modules, name, module)
    import tempfile

    return build_mcp(core=SimpleNamespace(settings=SimpleNamespace(
        run_root=Path(tempfile.mkdtemp()), allowed_knowledge_repositories=()))).tools


def knowledge_tree(tmp_path, name, text):
    root = tmp_path / name
    for path, content in {"AGENTS.md": "navigation", "general/_index.md": "general",
                          "repos/vllm-omni/_index.md": "repository", "repos/vllm-omni/rules.md": text,
                          "repos/afd-plugin/_index.md": "other", "repos/afd-plugin/rules.md": "other private rules"}.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return KnowledgeView(root=root.resolve(), snapshot=name)


def test_mcp_context_reads_stay_on_original_view_and_refuse_cross_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_TRACE_ROOT", str(tmp_path / "trace"))
    old = knowledge_tree(tmp_path, "old", "## TEST-1\nold rule\n")
    new = knowledge_tree(tmp_path, "new", "## TEST-1\nnew rule\n")
    active = [old]
    monkeypatch.setattr(KnowledgeView, "current", lambda: active[0])
    tools = fake_mcp(monkeypatch)
    trace_id = tools["start_knowledge_trace"](repo="vllm-omni", source_revision=HEAD)["trace_id"]
    active[0] = new
    result = tools["doc_read"]("repos/vllm-omni/rules.md", trace_id=trace_id)
    assert "old rule" in result["content"]
    assert result["knowledge_trace"]["exposure_id"]
    search = tools["doc_search"]("old rule", trace_id=trace_id)
    assert search["matches"]
    assert "error" in tools["get_knowledge_trace"](trace_id, repo="afd-plugin")
    assert "error" in tools["doc_read"]("repos/afd-plugin/rules.md", trace_id=trace_id)
    report = tools["get_knowledge_trace"](trace_id)
    assert {e["reference"]["snapshot_id"] for e in report["exposures"]} == {"old"}


def test_trace_off_does_not_write_and_sdk_still_completes(tmp_path):
    trace_root = tmp_path / "should-not-exist"
    client = DirectClient(trace_root=trace_root, trace_mode="off")
    plan = client.plan(request())
    assert plan.knowledge_trace == {}
    result = client.validate(DirectCompletionRequest(review_context_id=plan.review_context_id,
        expected_head_sha=HEAD, evidence_head_sha=HEAD, subtraction_signal="none", existing_feedback_status="checked"))
    assert result.review_complete
    assert not trace_root.exists()


def test_quick_map_references_require_existing_heading_ids():
    from infermatrix_copilot.sdk.v1.direct import _quick_map_rule_ids
    source = "## TEST-1aa Rule\nbody\n## TEST-1ab Rule\nbody\n"
    content = "| Zero values | TEST-1aa/1ab and UNKNOWN-9 | source.py |"
    assert _quick_map_rule_ids(content, source) == ["TEST-1aa", "TEST-1ab"]


def test_mcp_review_records_only_embedded_payloads_with_specific_map_refs(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_TRACE_ROOT", str(tmp_path / "traces"))
    tools = fake_mcp(monkeypatch)
    result = tools["review"]("7", title=request().title, body=request().body,
                             changed_files=[request().changed_paths[0].path], expected_head_sha=HEAD)
    assert result["knowledge_trace"]["status"] == "ready"
    report = tools["get_knowledge_trace"](result["knowledge_trace"]["trace_id"])
    assert report["exposures"]
    assert {e["representation"] for e in report["exposures"]} <= {"quick_map", "related_excerpt"}
    quick_map = next(e for e in report["exposures"] if e["representation"] == "quick_map")
    assert quick_map["rule_refs"]
    assert {r["coverage"] for r in quick_map["rule_refs"]} == {"reference_only"}
    assert not any(e["document_id"].endswith("simplification-audit.md") for e in report["exposures"])
    assert any(entry["document_id"].endswith("simplification-audit.md")
               for retrieval in report["retrievals"] for entry in retrieval["entries"])


def test_retrying_identical_sdk_plan_keeps_original_attempt_trace(tmp_path):
    client = DirectClient(trace_root=tmp_path)
    first = client.plan(request())
    second = client.plan(request())
    trace_id = first.knowledge_trace["trace_id"]
    assert second.knowledge_trace["trace_id"] == trace_id
    assert first.review_context_id == second.review_context_id
    page = client.read_document(first.knowledge_routes[0].document.document_id,
        review_context_id=first.review_context_id, trace_id=trace_id)
    assert page.knowledge_trace["status"] == "ready"
    decision = client.validate(DirectCompletionRequest(review_context_id=first.review_context_id,
        expected_head_sha=HEAD, evidence_head_sha=HEAD, subtraction_signal="none", existing_feedback_status="checked"))
    assert decision.review_complete
    assert client.knowledge_trace.get_report(trace_id)["session"]["status"] == "complete"
    assert client.plan(request()).knowledge_trace["trace_id"] == trace_id
    third = client.plan(replace(request(), review_id="another-attempt"))
    assert third.knowledge_trace["trace_id"] != trace_id


def test_sdk_utf8_page_exposure_preserves_original_byte_range(tmp_path, monkeypatch):
    view = knowledge_tree(tmp_path, "unicode", "你")
    client = DirectClient(trace_root=tmp_path / "traces")
    monkeypatch.setattr(client, "_view", lambda: view)
    trace_id = client.knowledge_trace.start_session("vllm-omni", knowledge_snapshot=view.public_snapshot)["trace_id"]
    context_id = "sha256:" + "b" * 64
    client._remember_context(context_id, HEAD, view=view, repository="vllm-omni", trace_id=trace_id)
    page = client.read_document("repos/vllm-omni/rules.md", max_bytes=1, review_context_id=context_id)
    assert page.next_offset == 1
    assert len(page.content.encode("utf-8")) == 3  # Existing byte-page replacement behavior.
    report = client.knowledge_trace.get_report(trace_id)
    exposure = report["exposures"][0]
    assert exposure["included_ranges"] == [[0, 1]]
    assert exposure["offset_unit"] == "utf8_bytes"


@pytest.mark.parametrize("operation,fail_call", [("provide", 1), ("retrieve", 2)])
def test_sdk_missed_observation_remains_degraded_after_storage_recovers(tmp_path, monkeypatch, operation, fail_call):
    client = DirectClient(trace_root=tmp_path)
    original = getattr(client.knowledge_trace.recorder, operation)
    calls = 0
    def transient_failure(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_call:
            return {"status": "degraded", "error": "PermissionError"}
        return original(*args, **kwargs)
    monkeypatch.setattr(client.knowledge_trace.recorder, operation, transient_failure)
    plan = client.plan(request())
    assert plan.knowledge_trace["status"] == "degraded"
    trace_id = plan.knowledge_trace["trace_id"]
    decision = client.validate(DirectCompletionRequest(review_context_id=plan.review_context_id,
        expected_head_sha=HEAD, evidence_head_sha=HEAD, subtraction_signal="none", existing_feedback_status="checked"))
    assert decision.review_complete
    report = client.knowledge_trace.get_report(trace_id)
    assert report["trace_status"] == "partial"
    assert report["session"]["status"] == "degraded"
    assert any(g["reason"] == "knowledge_capture_degraded" for g in report["gaps"])
    # The marker survives constructing a new host facade, and degraded sessions
    # remain repository scoped.
    new_host = KnowledgeTraceClient(root=tmp_path)
    assert new_host.get_report(trace_id)["trace_status"] == "partial"
    with pytest.raises(InvalidRequestError, match="different repository"):
        new_host.get_report(trace_id, repository="afd-plugin")


def test_sdk_later_read_failure_is_sticky_in_context_and_report(tmp_path, monkeypatch):
    client = DirectClient(trace_root=tmp_path)
    plan = client.plan(request())
    original = client.knowledge_trace.recorder.provide
    monkeypatch.setattr(client.knowledge_trace.recorder, "provide", lambda *args, **kwargs: {"status": "degraded"})
    page = client.read_document(plan.knowledge_routes[0].document.document_id,
        review_context_id=plan.review_context_id)
    assert page.knowledge_trace["status"] == "degraded"
    monkeypatch.setattr(client.knowledge_trace.recorder, "provide", original)
    assert client.plan(request()).knowledge_trace["status"] == "degraded"
    client.validate(DirectCompletionRequest(review_context_id=plan.review_context_id,
        expected_head_sha=HEAD, evidence_head_sha=HEAD, subtraction_signal="none", existing_feedback_status="checked"))
    report = client.knowledge_trace.get_report(plan.knowledge_trace["trace_id"])
    assert report["session"]["status"] == "degraded"
    assert report["trace_status"] == "partial"


def test_mcp_missed_retrieval_stays_degraded_at_completion(tmp_path, monkeypatch):
    from infermatrix_copilot.knowledge_trace import KnowledgeTrace
    monkeypatch.setenv("KNOWLEDGE_TRACE_ROOT", str(tmp_path / "traces"))
    original = KnowledgeTrace.retrieve
    calls = 0
    def transient_failure(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:  # An auxiliary retrieval previously ignored its result.
            return {"status": "degraded", "error": "PermissionError"}
        return original(self, *args, **kwargs)
    monkeypatch.setattr(KnowledgeTrace, "retrieve", transient_failure)
    tools = fake_mcp(monkeypatch)
    result = tools["review"]("7", title=request().title, body=request().body,
                             changed_files=[request().changed_paths[0].path], expected_head_sha=HEAD)
    assert result["knowledge_trace"]["status"] == "degraded"
    trace_id = result["knowledge_trace"]["trace_id"]
    tools["validate_direct_review"](subtraction_signal="none", evidence_head_sha=HEAD,
        existing_feedback_status="checked", trace_id=trace_id)
    report = tools["get_knowledge_trace"](trace_id)
    assert report["session"]["status"] == "degraded"
    assert report["trace_status"] == "partial"
    assert any(g["reason"] == "knowledge_capture_degraded" for g in report["gaps"])


def test_scoped_failed_reads_are_reported_without_rejected_path_details(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_TRACE_ROOT", str(tmp_path / "trace"))
    view = knowledge_tree(tmp_path, "missing-doc", "rules")
    monkeypatch.setattr(KnowledgeView, "current", lambda: view)
    tools = fake_mcp(monkeypatch)
    trace_id = tools["start_knowledge_trace"]()["trace_id"]
    assert "error" in tools["doc_read"]("repos/vllm-omni/missing.md", trace_id=trace_id)
    report = tools["get_knowledge_trace"](trace_id)
    assert any(g["reason"] == "document_read_failed" for g in report["gaps"])
    assert "missing.md" not in str(report["gaps"])
    before = len(report["gaps"])
    assert "error" in tools["doc_read"]("repos/afd-plugin/missing.md", repo="afd-plugin", trace_id=trace_id)
    assert len(tools["get_knowledge_trace"](trace_id)["gaps"]) == before
