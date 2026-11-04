"""Knowledge retrieval is observable separately from actual model delivery."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.adapters.base import render_briefing_docs
from infermatrix_copilot.engine.agent_runtime.knowledge import _repo_docs_tool
from infermatrix_copilot.engine.agent_runtime.knowledge_trace import (
    StepKnowledgeTrace, finish_knowledge_trace, get_knowledge_trace,
)
from infermatrix_copilot.engine.agent_runtime.runner import run_agent_step
from infermatrix_copilot.engine.step import StepContext
from infermatrix_copilot.knowledge_trace import KnowledgeTrace
from infermatrix_copilot.llm import Block, Reply


@pytest.fixture
def context(settings, trace, tmp_path, git_repo, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_TRACE_MODE", "best_effort")
    monkeypatch.delenv("KNOWLEDGE_TRACE_ROOT", raising=False)
    root = tmp_path / "knowledge"
    (root / "general").mkdir(parents=True)
    (root / "general/rules.md").write_text(
        "## TEST-1: Preserve zero\nUse an explicit None check for a numeric default.\n",
        encoding="utf-8")
    settings.knowledge_dir = root
    settings.knowledge_general_docs = ["general/rules.md"]
    return StepContext(settings=settings, trace=trace, run_dir=tmp_path / "run",
        params={}, state={"task_spec": {"kind": "pr_review", "repo": "widget"},
                          "repo_path": str(git_repo)})


def report(ctx):
    return KnowledgeTrace(ctx.state["knowledge_trace_root"]).report(ctx.state["knowledge_trace_id"])


def test_briefing_cap_records_only_final_included_fragments(context):
    root = context.settings.knowledge_dir
    (root / "general/long.md").write_text("A" * 60, encoding="utf-8")
    (root / "general/after.md").write_text("must not be provided", encoding="utf-8")
    recorder = StepKnowledgeTrace(get_knowledge_trace(context), "review#lens")
    rendered = render_briefing_docs(root, ["general/long.md", "general/after.md"],
        header="header", cap=40, on_document=recorder.briefing_document)
    before = report(context)
    assert len(before["retrievals"]) == 2
    assert before["exposures"] == []
    recorder.observe([{"content": rendered}])
    after = report(context)
    assert len(after["exposures"]) == 1
    exposed = after["exposures"][0]
    assert exposed["document_id"] == "general/long.md"
    assert exposed["truncated"] is True
    assert exposed["included_ranges"] == [[0, 32]]
    assert recorder.trace.service.store.blob(exposed["payload_ref"]) == "A" * 32


def test_document_tool_return_is_not_delivery_until_request(context):
    recorder = StepKnowledgeTrace(get_knowledge_trace(context), "review")
    tools = _repo_docs_tool(context, None, recorder)
    text = tools["doc_read"].handler(path="general/rules.md")
    assert report(context)["exposures"] == []
    recorder.observe([{"role": "user", "content": [{"type": "tool_result",
        "tool_use_id": "read1", "content": text}]}])
    exposure = report(context)["exposures"][0]
    assert exposure["representation"] == "full_document"
    assert exposure["boundary"] == "model_request"
    assert exposure["rule_refs"][0]["coverage"] == "body_complete"
    # Repeated requests carry prior results without inventing a second read.
    recorder.observe([{"content": text}])
    assert len(report(context)["exposures"]) == 1


def test_search_hit_uses_visible_later_line_offset(context):
    path = context.settings.knowledge_dir / "general/search.md"
    content = "# Topic\nfirst line\nneedle occurs here\n"
    path.write_text(content, encoding="utf-8")
    recorder = StepKnowledgeTrace(get_knowledge_trace(context), "search")
    tools = _repo_docs_tool(context, None, recorder)
    text = tools["doc_search"].handler(query="needle")
    recorder.observe([{"content": text}])
    exposure = report(context)["exposures"][0]
    start = len("# Topic\nfirst line\n")
    assert exposure["included_ranges"] == [[start, start + len("needle occurs here")]]


def test_disjoint_related_excerpt_marks_unknown_source_range(context):
    content = "first selected section\n\nomitted source\n\nlast selected section"
    path = context.settings.knowledge_dir / "general/disjoint.md"
    path.write_text(content, encoding="utf-8")
    recorder = StepKnowledgeTrace(get_knowledge_trace(context), "related")
    excerpt = "first selected section\n\nlast selected section"
    result = {"documents": [{"path": "general/disjoint.md", "content": excerpt,
                             "more_available": True}], "status": "ready"}
    encoded = json.dumps(result, ensure_ascii=False)
    recorder.tool_result("doc_related", result, encoded)
    recorder.observe([{"content": encoded}])
    exposure = report(context)["exposures"][0]
    assert exposure["included_ranges"] == []
    assert exposure["range_status"] == "unknown"


def test_dirty_source_evidence_does_not_claim_session_head_binding(context):
    from pathlib import Path

    recorder = StepKnowledgeTrace(get_knowledge_trace(context), "source")
    (Path(context.state["repo_path"]) / "mod_a.py").write_text("A = 0\n")
    text = (context.settings.knowledge_dir / "general/rules.md").read_text()
    recorder.queue("general/rules.md", text, representation="full_document")
    recorder.observe([{"content": text}, {"content": [{"type": "tool_result",
        "tool_use_id": "source1", "content": "A = 0\n"}]}])
    recorder.assessments({"knowledge_assessments": [{
        "document_id": "general/rules.md", "rule_id": "TEST-1",
        "applicability": "applicable", "verification": "unresolved",
        "rationale": "Inspect the dirty source", "check": "Check zero value", "evidence_ids": ["tool.source1"]}]})
    evidence = report(context)["evidence"][0]
    assert evidence["workspace_state"] == "dirty"
    assert evidence["revision_status"] == "unknown"
    assert evidence["source_revision"] == ""
    assert evidence["commit_verified"] is False


def test_strict_agent_reports_check_with_captured_source_evidence(context):
    class SourceCheckLLM:
        available = True

        def __init__(self):
            self.calls = []
            self.ref = None

        def create(self, **kwargs):
            self.calls.append(copy.deepcopy(kwargs["messages"]))
            if len(self.calls) == 1:
                return Reply(blocks=[Block(type="tool_use", id="kb1", name="doc_read",
                                          input={"path": "general/rules.md"})])
            if len(self.calls) == 2:
                note = kwargs["messages"][-1]["content"][-1]["text"]
                refs = json.loads(note.split("\n", 1)[1])
                self.ref = next(ref for ref in refs if ref["rule_id"] == "TEST-1")
                return Reply(blocks=[Block(type="tool_use", id="source1", name="read_file",
                                          input={"path": "mod_a.py"})])
            return Reply(blocks=[Block(type="text", text=json.dumps({
                "status": "success", "summary": "Checked default handling", "findings": ["No numeric default exists in mod_a.py."],
                "knowledge_assessments": [{**self.ref, "applicability": "applicable",
                    "verification": "unresolved", "rationale": "The source was inspected; this module has no numeric default.",
                    "check": "Inspect the source for a truthiness default.",
                    "evidence_ids": ["tool.source1"], "decision_ids": ["finding:0"]}]}))])

    llm = SourceCheckLLM()
    context.llm = llm
    result, _ = asyncio.run(run_agent_step(context, step_name="review#defaults",
        purpose="Check default handling", evidence={"source": "mod_a.py"}))
    assert result.ok
    records = report(context)
    assert len(llm.calls) == 3
    assert {e["boundary"] for e in records["exposures"]} == {"model_request"}
    assert len(records["assessments"]) == 1
    assert records["assessments"][0]["reference_validation"] == "valid"
    assert records["assessments"][0]["decision_refs"] == [{
        "decision_id": "finding:0", "finding": "No numeric default exists in mod_a.py."}]
    assert records["evidence"][0]["evidence_origin"] == "captured"
    assert records["evidence"][0]["revision_status"] == "matched"
    blob = get_knowledge_trace(context).service.store.blob(records["evidence"][0]["payload_ref"])
    assert "A = 1" in blob
    finish_knowledge_trace(context.state, "complete")
    artifact = json.loads((context.run_dir / "KNOWLEDGE_TRACE.json").read_text())
    assert artifact["trace_root"] == context.state["knowledge_trace_root"]
    assert artifact["session"]["run_id"] == context.run_dir.name
    assert (context.run_dir / "KNOWLEDGE_TRACE.md").is_file()
    markdown = (context.run_dir / "KNOWLEDGE_TRACE.md").read_text()
    assert "finding:0: No numeric default exists in mod_a.py." in markdown


def test_harness_records_handoff_without_claiming_model_request(context, monkeypatch):
    from infermatrix_copilot.agent_loop import AgentOutcome
    import infermatrix_copilot.providers as providers

    monkeypatch.setattr(type(context.settings), "tier_target", lambda *_: SimpleNamespace(
        kind="harness", model="test", host="local", source="test"))
    monkeypatch.setattr(providers, "run_harness_step", lambda *_, **__: AgentOutcome(
        '{"status":"success","summary":"ok"}', iterations=1, tool_calls=0))
    context.llm = SimpleNamespace(available=True)
    result, _ = asyncio.run(run_agent_step(context, step_name="review", purpose="check", evidence={}))
    assert result.ok
    captured = report(context)
    assert {x["boundary"] for x in captured["exposures"]} == {"host_context"}
    assert any(g["reason"] == "harness_model_requests_unobserved" for g in captured["gaps"])
    assert captured["instrumentation"]["native_file_reads"] == "unknown"


def test_repeated_runtime_step_keeps_input_evidence_per_invocation(context, monkeypatch):
    """Foreach/retry calls may reuse a step name and input evidence names."""
    monkeypatch.setenv("KNOWLEDGE_TRACE_MODE", "required")

    class InputCheckLLM:
        available = True

        def create(self, **kwargs):
            return Reply(blocks=[Block(type="text", text=json.dumps({
                "status": "success", "summary": "Checked the supplied source",
                "knowledge_assessments": [{
                    "document_id": "general/rules.md", "rule_id": "TEST-1",
                    "applicability": "applicable", "verification": "supported",
                    "rationale": "Checked the numeric default in the supplied source.",
                    "check": "Inspect whether the default preserves zero.",
                    "evidence_ids": ["input.source"]}]}))])

    context.llm = InputCheckLLM()
    sources = ["return value or 1", "return 1 if value is None else value"]
    for source in sources:
        result, _ = asyncio.run(run_agent_step(
            context, step_name="review#defaults", purpose="Check defaults",
            evidence={"source": source}))
        assert result.ok

    records = report(context)
    assert context.state["knowledge_trace_status"] == "running"
    assert len(records["assessments"]) == 2
    assert len(records["evidence"]) == 2
    assert len({a["invocation_id"] for a in records["assessments"]}) == 2
    evidence = {e["evidence_id"]: e for e in records["evidence"]}
    store = get_knowledge_trace(context).service.store
    for assessment, source in zip(records["assessments"], sources):
        captured = evidence[assessment["evidence_ids"][0]]
        assert captured["invocation_id"] == assessment["invocation_id"]
        assert store.blob(captured["payload_ref"]) == source
    finish_knowledge_trace(context.state, "complete")
    artifact = json.loads((context.run_dir / "KNOWLEDGE_TRACE.json").read_text())
    assert artifact["status"] == "complete"
    assert artifact["counts"]["assessments"] == 2


def test_off_does_not_create_trace_files(context, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_TRACE_MODE", "off")
    assert get_knowledge_trace(context) is None
    finish_knowledge_trace(context.state, "complete")
    assert not (context.run_dir / "knowledge_trace").exists()
    assert "knowledge_trace_id" not in context.state


def test_storage_degradation_is_visible(context, monkeypatch):
    monkeypatch.setattr(KnowledgeTrace, "start_session", lambda *_, **__: {
        "status": "degraded", "error": "PermissionError"})
    recorder = get_knowledge_trace(context)
    assert recorder.error
    assert context.state["knowledge_trace_status"] == "degraded"
    assert list(context.trace.events("knowledge_trace_error"))


def test_required_storage_failure_blocks_before_model_call(context, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_TRACE_MODE", "required")
    monkeypatch.setattr(KnowledgeTrace, "start_session", lambda *_, **__: {
        "status": "degraded", "error": "PermissionError"})
    context.llm = SimpleNamespace(available=True)
    result, _ = asyncio.run(run_agent_step(context, step_name="review", purpose="check", evidence={}))
    assert not result.ok
    assert "required knowledge trace failed" in result.summary


def test_separate_execution_attempts_preserve_interrupted_session(context):
    first = get_knowledge_trace(context)
    first.finish("interrupted")
    other_state = {k: v for k, v in context.state.items() if not k.startswith("_knowledge")}
    second_ctx = StepContext(settings=context.settings, trace=context.trace,
        run_dir=context.run_dir, params={}, state=other_state)
    second = get_knowledge_trace(second_ctx)
    assert first.trace_id != second.trace_id
    assert first.service.report(first.trace_id)["status"] == "interrupted"
    assert second.service.report(second.trace_id)["status"] == "running"
