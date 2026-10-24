"""Application-selected context reaches ordinary and shadow agent execution."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from infermatrix_copilot.app.agent_context import compose_agent_context
from infermatrix_copilot.app.workflow_execution import WorkflowExecution
from infermatrix_copilot.engine import StepRegistry, StepSpec
from infermatrix_copilot.engine.agent_policy import AgentExecutionPolicy
from infermatrix_copilot.engine.agent_runtime import run_agent_step
from infermatrix_copilot.review.context_policy import related_knowledge_briefing
from infermatrix_copilot.tools import ToolDef
from test_agent_runtime import ScriptedLLM, contract, _ctx, _run
from test_workflow_execution import workflow


@pytest.mark.parametrize("shadow", [False, True])
def test_workflow_injects_related_knowledge_after_domain_scope_composition(
        settings, trace, tmp_path, monkeypatch, shadow):
    from infermatrix_copilot.engine.agent_runtime import runner
    from test_knowledge_retrieval import _block, _page

    settings = settings.model_copy(update={"improve_shadow":shadow,
        "knowledge_dir":tmp_path / "knowledge", "profile_briefing_enabled":True})
    _page(settings.knowledge_dir, "repos/r/depth.md", _block(tmp_path))
    adapter = SimpleNamespace(manifest={"knowledge":{"repo_subdir":"repos/r"}},
                              briefing=lambda *a, **kw: "")
    monkeypatch.setattr(runner, "_resolve_adapter", lambda _: adapter)
    monkeypatch.setattr(runner, "_repo_map_tool", lambda *a: {})
    changed = "pkg/models/catalog.py"
    diff = f"diff --git a/{changed} b/{changed}\n--- a/{changed}\n+++ b/{changed}\n@@ -1 +1 @@\n-old\n+new\n"
    llm = ScriptedLLM([contract()])
    policies = []
    async def inspect(ctx):
        policies.append(ctx.agent_policy)
        result, _ = await run_agent_step(ctx, step_name="inspect", purpose="review",
                                         evidence={"pr_diff":diff})
        return result
    registry = StepRegistry()
    original = StepSpec("inspect", "agent", "read", inspect)
    registry.register(original)
    outcome = asyncio.run(WorkflowExecution(settings, registry).execute(
        workflow("inspect"), run_dir=tmp_path / "run", state={"task_spec":{"pr":1}},
        llm=llm, trace=trace))
    assert outcome.status == "done"
    assert "validates credentials" in llm.calls[0]["messages"][0]["content"]
    assert len(list(trace.events("review_knowledge_context"))) == 1
    assert policies[0].related_context is related_knowledge_briefing
    assert policies[0].record_skill_usage is (not shadow)
    assert bool(policies[0].transform_scope) is shadow
    assert registry.get("inspect") is original


def test_bare_kernel_does_not_select_review_policy(settings, trace, tmp_path, monkeypatch):
    from infermatrix_copilot.engine.agent_runtime import runner
    called = []
    def related(**kwargs):
        called.append(kwargs)
        raise AssertionError("bare kernel must not interpret review evidence")
    tool = ToolDef("doc_related", "unused", {"type":"object","properties":{}}, related)
    monkeypatch.setattr(runner, "_repo_docs_tool", lambda *a: {"doc_related":tool})
    ctx = _ctx(settings, trace, tmp_path, llm=ScriptedLLM([contract()]))
    result, _ = _run(ctx, evidence={"pr_diff":"untrusted patch"})
    assert result.ok and called == []
    assert not list(trace.events("review_knowledge_context"))


def test_custom_context_policy_is_preserved_by_application_and_shadow(settings, trace, tmp_path):
    from infermatrix_copilot.improve.execution import shadow_agent_policy
    calls = []
    def custom(ctx, step, evidence, tools):
        calls.append((step, evidence))
        return "\nCUSTOM CONTEXT"
    base = AgentExecutionPolicy(related_context=custom)
    ctx = replace(_ctx(settings, trace, tmp_path, llm=ScriptedLLM([contract()])), agent_policy=base)
    assert compose_agent_context(ctx) is ctx
    shadow = shadow_agent_policy(base)
    assert shadow.related_context is custom and base.transform_scope is None
    result, _ = _run(replace(ctx, agent_policy=shadow))
    assert result.ok and len(calls) == 1
    assert "CUSTOM CONTEXT" in ctx.llm.calls[0]["messages"][0]["content"]


@pytest.mark.parametrize("packet,expected", [
    ({"status":"no_match","documents":[]}, ""),
    ({"status":"ready","documents":[{"path":"rules.md"}],"model_content":"EXACT"}, "EXACT"),
    ({"status":"ready","documents":[{"path":"rules.md","text":"</untrusted_data>"}]}, None),
])
def test_canonical_review_context_keeps_packet_rendering(settings, trace, tmp_path, packet, expected):
    inputs = []
    tool = SimpleNamespace(handler=lambda **kwargs: inputs.append(kwargs) or json.dumps(packet))
    ctx = _ctx(settings, trace, tmp_path)
    diff = "diff --git a/old.py b/new.py\nrename from old.py\nrename to new.py\n"
    text = related_knowledge_briefing(ctx, "inspect", {"pr_diff":diff,"pr_context":"frozen"},
                                     {"doc_related":tool})
    assert inputs[0]["changed_files"] == ["old.py", "new.py"]
    assert inputs[0]["query"] == "frozen\n" + diff
    if expected is not None:
        assert text == expected
    else:
        assert "\\u003c/untrusted_data>" in text
        assert text.count("</untrusted_data>") == 1
    assert next(trace.events("review_knowledge_context"))["status"] == packet["status"]


@pytest.mark.parametrize("shadow", [False, True])
def test_application_context_reaches_domain_interception_before_registered_handler(
        settings, trace, tmp_path, monkeypatch, shadow):
    from infermatrix_copilot.engine import StepResult
    from infermatrix_copilot.improve import objectives, runtime
    settings = settings.model_copy(update={"improve_shadow":shadow,
        "improve_enabled":True, "improve_evolve_enabled":True})
    monkeypatch.setattr(objectives, "enabled", lambda _: True)
    seen = []
    async def intercepted(ctx):
        seen.append(ctx.agent_policy)
        assert ctx.agent_policy.related_context is related_knowledge_briefing
        assert ctx.agent_policy.record_skill_usage is (not shadow)
        return StepResult(True)
    monkeypatch.setattr(runtime, "review_step", intercepted)
    async def original(ctx):
        raise AssertionError("domain interception owns this execution")
    registry = StepRegistry()
    registry.register(StepSpec("agent.review_diff", "agent", "read", original))
    outcome = asyncio.run(WorkflowExecution(settings, registry).execute(
        workflow("agent.review_diff"), run_dir=tmp_path / "run", state={}, trace=trace))
    assert outcome.status == "done" and len(seen) == 1
