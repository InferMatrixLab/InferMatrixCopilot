"""Unified agent-step runtime (修正方案 P0): dispatch context, evidence pack,
skills, output contract, failure mapping, and the all-agent-steps-unified rule."""

import asyncio
import json

import pytest

from infermatrix_copilot.engine.agent_runtime import (
    BASE_OUTPUT_SCHEMA,
    run_agent_step,
)
from infermatrix_copilot.engine.steps import register_builtin_steps
from infermatrix_copilot.engine.registry import StepRegistry
from infermatrix_copilot.engine.step import FailureKind, StepContext
from infermatrix_copilot.llm import Block, Reply
from infermatrix_copilot.memory.skills import SkillStore


class ScriptedLLM:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []
        self.available = True

    def create(self, *, system, messages, tools=None, model=None,
               max_tokens=None, on_text=None, role=""):
        self.calls.append({"system": system, "messages": [*messages],
                           "tools": tools})
        return self._replies.pop(0)


def contract(status="success", **extra):
    base = {k: ([] if "list" in v else "x") for k, v in BASE_OUTPUT_SCHEMA.items()}
    base.update(status=status, summary="did the thing", confidence="high",
                next_action="none")
    base["failure_kind"] = None
    base.update(extra)
    return Reply(blocks=[Block(type="text", text=json.dumps(base))])


def _ctx(settings, trace, tmp_path, state=None, llm=None):
    return StepContext(settings=settings, state=state or {"task_spec": {"pr": 1}},
                       params={}, run_dir=tmp_path / "run", trace=trace, llm=llm)


def _run(ctx, **kw):
    defaults = dict(step_name="t.step", purpose="test", evidence={"e": "small"})
    defaults.update(kw)
    return asyncio.run(run_agent_step(ctx, **defaults))


def test_evidence_capped_and_archived(settings, trace, tmp_path):
    settings.evidence_item_chars = 100
    llm = ScriptedLLM([contract()])
    ctx = _ctx(settings, trace, tmp_path, llm=llm)
    big = "x" * 500 + "TAIL_MARKER"
    result, output = _run(ctx, evidence={"big_item": big})
    assert result.ok
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "chars omitted" in prompt and "TAIL_MARKER" in prompt
    archived = tmp_path / "run" / "evidence" / "big_item.txt"
    assert archived.exists() and archived.read_text() == big
    assert str(archived) in prompt  # agent told where the full text lives


def test_identical_drafts_keep_their_qualified_agent_identity_during_repair(settings, trace, tmp_path):
    from infermatrix_copilot.trace_store import current_context
    seen = []
    class LLM(ScriptedLLM):
        def create(self, **kwargs):
            if kwargs['system'].startswith("Convert the agent's draft"):
                seen.append(current_context().get('agent_step'))
                return contract()
            return Reply(blocks=[Block(type='text', text='identical draft')])
    ctx = _ctx(settings, trace, tmp_path, llm=LLM([]))
    before = current_context()
    for name in ('review#logic', 'review#contracts'):
        result, _ = _run(ctx, step_name=name)
        assert result.ok
    assert seen == ['review#logic', 'review#contracts']
    assert current_context() == before


@pytest.mark.parametrize("member_kind", ["default", "api", "harness"])
def test_agent_admission_checks_repair_client_before_any_member_dispatch(
    settings, trace, tmp_path, monkeypatch, member_kind,
):
    from types import SimpleNamespace
    from infermatrix_copilot.providers.codex import CodexTransport
    from infermatrix_copilot.providers.harness_llm import HarnessLLM

    transport = CodexTransport(settings)
    monkeypatch.setattr(transport, "cli_path", lambda: "/fake/codex")
    ctx = _ctx(settings, trace, tmp_path,
               llm=HarnessLLM(settings, transport))

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported repair must block before a paid agent call")

    monkeypatch.setattr("infermatrix_copilot.agent_loop.run_agent", forbidden)
    monkeypatch.setattr("infermatrix_copilot.providers.run_harness_step", forbidden)
    kwargs = {}
    if member_kind == "api":
        kwargs["llm_override"] = ScriptedLLM([contract()])
    elif member_kind == "harness":
        kwargs["harness_member"] = SimpleNamespace(provider="cursor", model="test")
    result, output = _run(ctx, **kwargs)
    assert not result.ok and result.failure == FailureKind.BLOCKED
    assert "output-repair completion" in result.summary
    assert "builtin_tools_off" in result.summary
    assert output == {}
    assert not list(trace.events("agent_dispatch"))


@pytest.mark.parametrize("enabled,changed,expected", [
    (True, "pkg/models/catalog.py", True),
    (True, "unrelated.py", False),
    (False, "pkg/models/catalog.py", False),
])
def test_review_prompt_gets_scoped_knowledge_and_respects_ablation(
        settings, trace, tmp_path, monkeypatch, enabled, changed, expected):
    import types
    from infermatrix_copilot.engine.agent_runtime import runner
    from test_knowledge_retrieval import _block, _page

    settings.knowledge_dir = tmp_path / "knowledge"
    settings.profile_briefing_enabled = enabled
    _page(settings.knowledge_dir, "repos/r/depth.md", _block(tmp_path))
    adapter = types.SimpleNamespace(
        manifest={"knowledge": {"repo_subdir": "repos/r"}},
        briefing=lambda *args, **kwargs: "")
    monkeypatch.setattr(runner, "_resolve_adapter", lambda _: adapter)
    monkeypatch.setattr(runner, "_repo_map_tool", lambda *args: {})
    diff = f"diff --git a/{changed} b/{changed}\n--- a/{changed}\n+++ b/{changed}\n@@ -1 +1 @@\n-old\n+new\n"
    llm = ScriptedLLM([contract()])
    from infermatrix_copilot.app.agent_context import compose_agent_context
    ctx = compose_agent_context(_ctx(settings, trace, tmp_path, llm=llm))
    result, _ = _run(ctx, evidence={"pr_diff": diff})
    assert result.ok
    prompt = llm.calls[0]["messages"][0]["content"]
    assert ("validates credentials" in prompt) is expected
    events = list(trace.events("review_knowledge_context"))
    if enabled:
        assert events[0]["status"] == ("ready" if expected else "no_match")
        assert bool(events[0]["pages"]) is expected
    else:
        assert events == []


def test_skills_retrieved_and_injected(settings, trace, tmp_path):
    store = SkillStore(settings.skills_dir)
    store.propose(name="test-skill", description="guidance for t.step testing",
                  body="## Fix\ndo the thing")
    store.promote("test-skill")
    llm = ScriptedLLM([contract()])
    ctx = _ctx(settings, trace, tmp_path, llm=llm)
    _run(ctx, step_name="t.step testing")
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "RELEVANT SKILLS" in prompt and "test-skill" in prompt
    ev = next(trace.events("agent_dispatch"))
    assert "test-skill" in ev["skills"]


def test_read_only_run_never_touches_skill_files(settings, trace, tmp_path):
    """A read-only run (READ_ONLY_KINDS without post — the MCP review case)
    must not rewrite git-tracked SKILL.md frontmatter: run_count bumps were
    dirtying the tree on every MCP review. Write-capable specs still feed the
    usage prior."""
    store = SkillStore(settings.skills_dir)
    store.propose(name="test-skill", description="guidance for t.step testing",
                  body="## Fix\ndo the thing")
    skill_file = store.promote("test-skill")
    before = skill_file.read_text(encoding="utf-8")

    ro_state = {"task_spec": {"kind": "pr_review", "pr": 1, "post": False}}
    result, _ = _run(_ctx(settings, trace, tmp_path, state=ro_state,
                          llm=ScriptedLLM([contract()])),
                     step_name="t.step testing")
    assert result.ok
    assert skill_file.read_text(encoding="utf-8") == before  # byte-identical
    from infermatrix_copilot.memory.paths import KnowledgePaths

    journal = KnowledgePaths.resolve(settings, "").skills_usage_journal
    assert not journal.exists()  # read-only runs suppress usage entirely

    rw_state = {"task_spec": {"kind": "pr_rebase", "pr": 1}}
    result, _ = _run(_ctx(settings, trace, tmp_path, state=rw_state,
                          llm=ScriptedLLM([contract()])),
                     step_name="t.step testing")
    assert result.ok
    # PR-boundary F3: seed SKILL.md files are read-only at run time in
    # EVERY mode now — the usage prior accumulates in the runtime usage
    # journal instead of rewriting checked-in frontmatter
    assert skill_file.read_text(encoding="utf-8") == before
    assert journal.exists()
    assert "test-skill" in journal.read_text(encoding="utf-8")
    # …and the journal has a READ side (round-2 F8): journal usage breaks
    # ranking ties toward the proven skill, exactly like the old
    # frontmatter bump did
    from infermatrix_copilot.engine.agent_runtime.knowledge import (
        _knowledge_stores)
    from infermatrix_copilot.engine.step import StepContext as _SC
    store.propose(name="rival-skill",
                  description="guidance for t.step testing",
                  body="## Fix\nother way")
    store.promote("rival-skill")
    ctx2 = _SC(settings=settings, state={"task_spec": {"pr": 1}},
               params={}, run_dir=tmp_path / "run2", trace=trace)
    ranked = _knowledge_stores(ctx2).find(query="t.step testing", k=2)
    assert ranked and ranked[0].name == "test-skill"  # journal boost wins


def test_skill_candidate_tool_is_gated(settings, trace, tmp_path):
    llm = ScriptedLLM([
        Reply(blocks=[Block(type="tool_use", id="t1", name="skill_update_candidate",
                            input={"name": "new-lesson", "description": "d",
                                   "body": "b"})]),
        contract(),
    ])
    ctx = _ctx(settings, trace, tmp_path, llm=llm)
    result, _ = _run(ctx)
    assert result.ok
    # PR-boundary F3: proposals land RUNTIME-side, never in the shared
    # pool / adapter tree
    from infermatrix_copilot.memory.paths import KnowledgePaths
    runtime = SkillStore(
        KnowledgePaths.resolve(settings, "").skills_runtime_dir)
    assert "new-lesson" in runtime.candidates()  # candidate recorded
    assert runtime.load_all() == []              # no active skill created
    assert SkillStore(settings.skills_dir).candidates() == {}
    assert any(True for _ in trace.events("skill_candidate_proposed"))


def test_status_and_failure_kind_mapping(settings, trace, tmp_path):
    cases = [
        ("blocked", None, FailureKind.BLOCKED),
        ("needs_review", None, FailureKind.ESCALATE),
        ("failed", "retryable", FailureKind.RETRYABLE),
        ("failed", "test_failure", FailureKind.TEST_FAILURE),
        ("failed", "bogus-kind", FailureKind.ESCALATE),
    ]
    for status, kind, expected in cases:
        llm = ScriptedLLM([contract(status=status, failure_kind=kind)])
        result, _ = _run(_ctx(settings, trace, tmp_path, llm=llm))
        assert not result.ok and result.failure is expected, (status, kind)


def test_unparseable_after_repair_is_salvaged_as_escalation(settings, trace,
                                                            tmp_path):
    """T4: a non-empty final answer that fails the contract twice is wrapped
    (needs_review + _raw_text) instead of discarded — a 665-token correct
    diagnosis was thrown away at T3 for a missing field."""
    llm = ScriptedLLM([
        Reply(blocks=[Block(type="text", text="prose")]),
        Reply(blocks=[Block(type="text", text="still prose")]),  # repair fails
    ])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert not result.ok and result.failure is FailureKind.ESCALATE
    assert output.get("_raw_text") == "prose"  # the ORIGINAL final answer, not the repair
    assert output.get("status") == "needs_review"


def test_files_modified_flow_into_changed_files(settings, trace, tmp_path):
    llm = ScriptedLLM([contract(files_modified=["a.py", "b.py"])])
    result, _ = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.changed_files == ["a.py", "b.py"]


@pytest.mark.parametrize("step_name,state", [
    ("agent.review_diff", {"task_spec": {"pr": 1}, "diff_text": "diff"}),
    ("agent.draft_issue_answer", {"task_spec": {"issue": 2}, "issue_text": "help"}),
    ("agent.triage_issues", {"task_spec": {"kind": "issue_filter"},
                             "issue_text": "[]"}),
    ("agent.debug_group", {"task_spec": {"pr": 3}, "repo_path": "/tmp"}),
])
def test_every_agent_step_goes_through_the_runtime(settings, trace, tmp_path,
                                                   step_name, state):
    """修正方案 acceptance #1: agent-kind steps share the unified entry —
    proven by the agent_dispatch trace event each must emit."""
    registry = register_builtin_steps(StepRegistry())
    llm = ScriptedLLM([contract(review_comments=[], answer_draft="a",
                                triage_table=[], root_cause="r",
                                fix_summary="f", verification="v")])
    if step_name == "agent.debug_group":
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        for args in (("init", "-q"), ("config", "user.email", "t@e.c"),
                     ("config", "user.name", "t")):
            subprocess.run(["git", *args], cwd=repo, check=True)
        (repo / "source.py").write_text("value = 1\n")
        subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
        state = {**state, "repo_path": str(repo)}
    ctx = StepContext(settings=settings, state=state, params={},
                      run_dir=tmp_path / "run", trace=trace, llm=llm)
    ctx.item = {"signature": "sig", "jobs": []}  # for debug_group
    result = asyncio.run(registry.get(step_name).handler(ctx))
    if step_name == "agent.debug_group":
        assert not result.ok and "observed commit" in result.summary
    else:
        assert result.ok, f"{step_name}: {result.summary}"
    ev = next(trace.events("agent_dispatch"))
    assert ev["permissions"]["tools"], step_name


def _run_issue_step(settings, trace, tmp_path, step_name, state, reply):
    registry = register_builtin_steps(StepRegistry())
    ctx = StepContext(settings=settings, state=state, params={},
                      run_dir=tmp_path / "run", trace=trace,
                      llm=ScriptedLLM([reply]))
    return asyncio.run(registry.get(step_name).handler(ctx)), ctx


def test_needs_review_triage_with_rows_is_salvaged(settings, trace, tmp_path):
    """A complete triage table self-assessed `needs_review` ships with caveats
    instead of blocking the run — a one-row table renders under the 300-char
    prose floor, so the floor alone discarded a correct high-confidence triage
    (issue5123, run-20260716-175425)."""
    reply = contract(status="needs_review", confidence="high",
                     triage_table=[{"number": 5123, "title": "gradio removed",
                                    "type": "bug", "module": "examples",
                                    "priority": "high", "labels": ["bug"]}])
    result, ctx = _run_issue_step(
        settings, trace, tmp_path, "agent.triage_issues",
        {"task_spec": {"kind": "issue_filter"}, "issue_text": "[]"}, reply)
    assert result.ok, result.summary
    assert "with caveats" in result.summary
    assert "#5123" in ctx.state["triage_table"]
    assert ctx.state["triage_table"].startswith("> ⚠")  # caveat kept visible


def test_needs_review_triage_without_rows_still_escalates(settings, trace,
                                                          tmp_path):
    reply = contract(status="needs_review", triage_table=[])
    result, _ = _run_issue_step(
        settings, trace, tmp_path, "agent.triage_issues",
        {"task_spec": {"kind": "issue_filter"}, "issue_text": "[]"}, reply)
    assert not result.ok and result.failure is FailureKind.ESCALATE


def test_needs_review_thin_draft_still_escalates(settings, trace, tmp_path):
    """The prose floor stays authoritative for drafts: a thin needs_review
    answer is not salvaged."""
    reply = contract(status="needs_review", answer_draft="too thin")
    result, _ = _run_issue_step(
        settings, trace, tmp_path, "agent.draft_issue_answer",
        {"task_spec": {"issue": 2}, "issue_text": "help"}, reply)
    assert not result.ok and result.failure is FailureKind.ESCALATE


def test_injected_shadow_policy_prevents_skill_usage_for_write_capable_task(settings, trace, tmp_path):
    from infermatrix_copilot.improve.execution import shadow_agent_policy
    from infermatrix_copilot.memory.paths import KnowledgePaths

    store = SkillStore(settings.skills_dir)
    store.propose(name="test-skill", description="guidance for t.step testing", body="## Fix\ndo the thing")
    path = store.promote("test-skill")
    before = path.read_bytes()
    ctx = _ctx(settings, trace, tmp_path, state={"task_spec":{"kind":"pr_rebase", "pr":1}},
               llm=ScriptedLLM([contract()]))
    ctx.agent_policy = shadow_agent_policy()
    result, _ = _run(ctx, step_name="t.step testing")
    assert result.ok
    assert path.read_bytes() == before
    assert not KnowledgePaths.resolve(settings, "").skills_usage_journal.exists()


@pytest.mark.parametrize("shadow", [False, True])
def test_injected_scope_reaches_actual_agent_read_tools(settings, trace, tmp_path, shadow):
    from infermatrix_copilot.improve.execution import shadow_agent_policy

    repo = tmp_path / "checkout"
    repo.mkdir()
    inside = repo / "inside.txt"
    inside.write_text("INSIDE_READ_WITNESS")
    outside = tmp_path / "private.txt"
    outside.write_text("OUTSIDE_READ_WITNESS")
    llm = ScriptedLLM([
        Reply(blocks=[Block(type="tool_use", id="inside", name="read_file", input={"path":str(inside)})]),
        Reply(blocks=[Block(type="tool_use", id="outside", name="read_file", input={"path":str(outside)})]),
        contract(),
    ])
    ctx = _ctx(settings, trace, tmp_path, state={"task_spec":{"kind":"pr_review"},
               "repo_path":str(repo)}, llm=llm)
    if shadow:
        ctx.agent_policy = shadow_agent_policy()
    result, _ = _run(ctx)
    assert result.ok
    messages = json.dumps(llm.calls[-1]["messages"])
    assert "INSIDE_READ_WITNESS" in messages
    assert ("OUTSIDE_READ_WITNESS" in messages) is not shadow


@pytest.mark.parametrize('backend', ['api', 'harness'])
def test_task_completion_guidance_reaches_dispatch_and_shared_repair(
    settings, trace, tmp_path, monkeypatch, backend,
):
    from types import SimpleNamespace
    from infermatrix_copilot.agent_loop import AgentOutcome
    from infermatrix_copilot.engine.agent_runtime.dispatch import TASK_STATUS_GUIDANCE
    draft = Reply(blocks=[Block(type='text', text='Completed analysis: the reviewed change has a defect.')])
    llm = ScriptedLLM(([draft] if backend == 'api' else []) + [contract()])
    ctx = _ctx(settings, trace, tmp_path, llm=llm)
    captured = []
    def harness(*args, **kwargs):
        captured.append(kwargs['prompt'])
        return AgentOutcome(text=draft.text, iterations=1, tool_calls=0)
    monkeypatch.setattr('infermatrix_copilot.providers.run_harness_step', harness)
    kwargs = {'harness_member': SimpleNamespace(provider='codex', model='fixture')} if backend == 'harness' else {}
    result, _ = _run(ctx, **kwargs)
    assert result.ok
    prompt = captured[0] if backend == 'harness' else llm.calls[0]['messages'][0]['content']
    assert prompt.count(TASK_STATUS_GUIDANCE) == 1
    assert prompt.index('## OUTPUT CONTRACT') < prompt.index('## TASK STATUS') < prompt.index('## PERMISSIONS')
    assert TASK_STATUS_GUIDANCE in llm.calls[-1]['system']
    assert len(llm.calls) == (1 if backend == 'harness' else 2)


def test_task_status_guidance_does_not_accept_explicit_non_success_recheck(settings, trace, tmp_path):
    llm = ScriptedLLM([contract(status='needs_review', finding_rechecks=[{
        'finding_id': 'carried', 'head_sha': 'a' * 40, 'outcome': 'fixed',
        'evidence': 'The required guard exists on the current head.',
    }])])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm),
        output_extension={'finding_rechecks': 'list of explicit rechecks'})
    assert not result.ok and result.failure == FailureKind.ESCALATE
    assert output['status'] == 'needs_review'
    assert output['finding_rechecks'][0]['outcome'] == 'fixed'
    assert len(llm.calls) == 1  # a declared failure is not rewritten by repair
