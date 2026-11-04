"""Adaptive trace capture follows the budgeted bytes through final injection."""

import asyncio
import hashlib
import json

import pytest
import yaml

from infermatrix_copilot.engine.agent_runtime.knowledge import _repo_docs_tool
from infermatrix_copilot.engine.agent_runtime.knowledge_trace import StepKnowledgeTrace, get_knowledge_trace
from infermatrix_copilot.engine.agent_runtime.runner import run_agent_step
from infermatrix_copilot.engine.step import StepContext
from infermatrix_copilot.kb_service.repo_spec import RepoSpec
from infermatrix_copilot.knowledge_context import ContextError
from infermatrix_copilot.knowledge_trace import KnowledgeTrace
from infermatrix_copilot.knowledge_view import build_manifest
from infermatrix_copilot.llm import Block, Reply


PIN = "a" * 40


@pytest.fixture
def adaptive_context(settings, trace, tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_TRACE_MODE", "required")
    monkeypatch.delenv("KNOWLEDGE_TRACE_ROOT", raising=False)
    root = tmp_path / "snapshot" / "knowledge"
    (root / "general").mkdir(parents=True)
    (root / "AGENTS.md").write_text("Knowledge navigation")
    (root / "general/briefing.md").write_text("## TRACE-1 Preserve values\nCheck <zero> and \\\"quoted\\\" values.\n")
    specs = {}
    for name in ("r", "other"):
        (root / "repos" / name).mkdir(parents=True)
        (root / "repos" / name / "_index.md").write_text("Repository navigation")
        (root / "repos" / name / "doc.md").write_text(
            f"---\ntitle: API\ntype: guide\nfeature: api\nsources: [acme/{name}@{PIN}:src/api.py]\n---\n\n"
            "## TRACE-2 Source contract\nCheck <zero> and \\\"quoted\\\" values.\n" + "more context 中文. " * 500)
        specs[name] = RepoSpec(name, aliases=(f"git.example/subgroup/{name}",), source_pin=PIN,
            catalog_hash="b" * 64, policy_hash="c" * 64,
            dependencies=({"repo_id": "other", "status": "confirmed", "source_pin": PIN,
                           "catalog_hash": "b" * 64},) if name == "r" else ())
    (root / "_repositories.yaml").write_text(yaml.safe_dump({"schema_version": 1,
        "repos": {key: spec.to_dict() for key, spec in specs.items()}}))
    manifest = build_manifest(root, "adaptive-trace")
    (root.parent / "MANIFEST.json").write_text(json.dumps(manifest))
    monkeypatch.setenv("KNOWLEDGE_ROOT", str(root.parent))
    settings.knowledge_dir = root
    settings.knowledge_general_docs = ["general/briefing.md"]
    settings.allowed_knowledge_repositories = ("other",)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return StepContext(settings=settings, trace=trace, run_dir=run_dir,
        params={"knowledge_context_profile": "adaptive", "knowledge_context_budget": {
            "initial_tokens": 1400, "max_tokens": 5000}},
        state={"task_spec": {"kind": "pr_review", "repo": "git.example/subgroup/r",
                             "expected_head_sha": PIN}})


def report(ctx):
    return KnowledgeTrace(ctx.state["knowledge_trace_root"]).report(ctx.state["knowledge_trace_id"])


@pytest.mark.parametrize("wire", ["model_content", "tool_packet"])
def test_adaptive_strict_traces_only_budgeted_fragments_at_request(adaptive_context, wire):
    ctx = adaptive_context
    recorder = StepKnowledgeTrace(get_knowledge_trace(ctx), "review#adaptive")
    tools = _repo_docs_tool(ctx, None, recorder)
    packet = json.loads(tools["doc_read"].handler("repos/r/doc.md"))
    assert packet["truncated"]
    assert packet["consumed_tokens"] <= 1400
    assert "content" not in packet["documents"][0]
    assert report(ctx)["session"]["repository"] == "r"
    assert report(ctx)["exposures"] == []
    # A model request which omitted the packet must not count as delivery.
    recorder.observe([{"content": "source evidence only"}])
    assert report(ctx)["exposures"] == []
    text = packet["model_content"] if wire == "model_content" else json.dumps(packet, ensure_ascii=False)
    recorder.observe([{"content": text}])
    exposed = report(ctx)["exposures"]
    assert len(exposed) == 1
    assert exposed[0]["boundary"] == "model_request"
    assert exposed[0]["truncated"] is True
    payload = recorder.trace.service.store.blob(exposed[0]["payload_ref"])
    assert "<zero>" in payload
    assert payload in (ctx.settings.knowledge_dir / "repos/r/doc.md").read_text()
    assert exposed[0]["included_ranges"] == [[0, len(payload)]]


def test_adaptive_strict_continuation_keeps_range_unknown(adaptive_context):
    ctx = adaptive_context
    recorder = StepKnowledgeTrace(get_knowledge_trace(ctx), "review")
    tools = _repo_docs_tool(ctx, None, recorder)
    packet = json.loads(tools["doc_read"].handler("repos/r/doc.md"))
    recorder.observe([{"content": packet["model_content"]}])
    tools["doc_context_expand"].handler(target_tokens=5000, reason="Read the rest of the source contract")
    continued = json.loads(tools["doc_read"].handler("repos/r/doc.md"))
    assert continued["documents"][0]["continuation"]
    recorder.observe([{"content": continued["model_content"]}])
    exposed = report(ctx)["exposures"]
    assert len(exposed) == 2
    assert exposed[1]["range_status"] == "unknown"
    assert exposed[1]["included_ranges"] == []


@pytest.mark.parametrize("tool", ["doc_search", "doc_related"])
def test_adaptive_search_and_related_keep_only_returned_prose(adaptive_context, tool):
    ctx = adaptive_context
    recorder = StepKnowledgeTrace(get_knowledge_trace(ctx), "review")
    tools = _repo_docs_tool(ctx, None, recorder)
    args = {"query": "Source contract"} if tool == "doc_search" else {"changed_files": ["src/api.py"]}
    text = tools[tool].handler(**args)
    packet = json.loads(text)
    assert packet["model_content"]
    recorder.observe([{"content": text}])
    records = report(ctx)
    assert records["exposures"]
    assert records["retrievals"][-1]["mechanism"] == tool
    assert all(e["range_status"] == "unknown" for e in records["exposures"])
    for exposure in records["exposures"]:
        payload = recorder.trace.service.store.blob(exposure["payload_ref"])
        assert json.dumps(payload, ensure_ascii=False)[1:-1].replace("<", "\\u003c") in packet["model_content"]


def test_adaptive_no_new_context_does_not_invent_exposure(adaptive_context):
    ctx = adaptive_context
    recorder = StepKnowledgeTrace(get_knowledge_trace(ctx), "review")
    tools = _repo_docs_tool(ctx, None, recorder)
    first = json.loads(tools["doc_search"].handler(query="Preserve values"))
    recorder.observe([{"content": first["model_content"]}])
    count = len(report(ctx)["exposures"])
    second = json.loads(tools["doc_search"].handler(query="preserve VALUES"))
    assert second["status"] == "no_new_context"
    assert second["consumed_tokens"] == first["consumed_tokens"]
    assert second["model_content"] == ""
    recorder.observe([{"content": json.dumps(second)}])
    assert len(report(ctx)["exposures"]) == count
    assert report(ctx)["retrievals"][-1]["status"] == "no_new_context"


def test_adaptive_strict_briefing_is_captured_by_final_model_call(adaptive_context):
    ctx = adaptive_context

    class LLM:
        available = True

        def create(self, **kwargs):
            assert "Preserve values" in kwargs["messages"][0]["content"]
            return Reply(blocks=[Block(type="text", text='{"status":"success","summary":"checked"}')])

    ctx.llm = LLM()
    result, _ = asyncio.run(run_agent_step(ctx, step_name="review", purpose="Inspect", evidence={}))
    assert result.ok
    records = report(ctx)
    assert records["session"]["knowledge_snapshot"] == "adaptive-trace"
    exposed = records["exposures"]
    assert len(exposed) == 1
    assert exposed[0]["document_id"] == "general/briefing.md"
    assert exposed[0]["boundary"] == "model_request"


def test_adaptive_crossrepo_permission_is_not_granted_by_tracer(adaptive_context):
    ctx = adaptive_context
    recorder = StepKnowledgeTrace(get_knowledge_trace(ctx), "review")
    ctx.settings.allowed_knowledge_repositories = ()
    denied = _repo_docs_tool(ctx, None, recorder)
    with pytest.raises(ContextError, match="authorized"):
        denied["doc_read"].handler("repos/other/doc.md", repository="other")
    ctx.settings.allowed_knowledge_repositories = ("other",)
    allowed = _repo_docs_tool(ctx, None, recorder)
    packet = json.loads(allowed["doc_read"].handler("repos/other/doc.md", repository="other"))
    assert packet["documents"][0]["repo_id"] == "other"
    recorder.observe([{"content": packet["model_content"]}])
    records = report(ctx)
    assert records["exposures"] == []
    assert any(g["reason"] == "context_cross_repository_unobserved" for g in records["gaps"])


def test_adaptive_corrupt_packet_is_reported_without_legacy_fallback(adaptive_context):
    recorder = StepKnowledgeTrace(get_knowledge_trace(adaptive_context), "review")
    packet = {"model_content": "substituted", "model_content_sha256": hashlib.sha256(b"original").hexdigest(),
              "documents": [{"path": "repos/r/doc.md"}], "status": "ready"}
    recorder.adaptive_packet(packet, "doc_read")
    records = report(adaptive_context)
    assert records["exposures"] == []
    assert any(g["reason"] == "adaptive_packet_invalid" for g in records["gaps"])
