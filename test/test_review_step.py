"""pr-review on the unified agent runtime: gate check + governed review step."""

import asyncio
import json

from infermatrix_copilot.engine.steps import register_builtin_steps
from infermatrix_copilot.engine.registry import StepRegistry
from infermatrix_copilot.engine.step import FailureKind, StepContext
from infermatrix_copilot.llm import Block, Reply


class ScriptedLLM:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []
        self.available = True

    def create(self, *, system, messages, tools=None, model=None, max_tokens=None,
               on_text=None):
        self.calls.append({"system": system, "messages": [*messages],
                           "tools": tools})
        return self._replies.pop(0)


def _ctx(settings, trace, tmp_path, state, llm=None):
    return StepContext(settings=settings, state=state, params={},
                       run_dir=tmp_path / "run", trace=trace, llm=llm)


def _registry():
    return register_builtin_steps(StepRegistry())


def _contract_reply(comments, status="success"):
    return Reply(blocks=[Block(type="text", text=json.dumps({
        "status": status, "summary": "reviewed", "findings": [],
        "files_read": [], "files_modified": [], "tests_requested": [],
        "tests_run": [], "assumptions": [], "blockers": [],
        "confidence": "high", "failure_kind": None, "next_action": "post",
        "review_comments": comments,
    }))])


def test_gate_check_injected_and_missing_pr(settings, trace, tmp_path):
    gate = _registry().get("pr.gate_check")
    ctx = _ctx(settings, trace, tmp_path, {"gate_report": "gates clean"})
    assert asyncio.run(gate.handler(ctx)).ok

    ctx = _ctx(settings, trace, tmp_path, {"task_spec": {}})
    result = asyncio.run(gate.handler(ctx))
    assert not result.ok and result.failure is FailureKind.BLOCKED


def test_review_runtime_flow(settings, trace, tmp_path, git_repo):
    llm = ScriptedLLM([
        # investigation: one evidence lookup, then the contract JSON
        Reply(blocks=[Block(type="tool_use", id="t1", name="read_file",
                            input={"path": str(git_repo / "mod_a.py")})]),
        _contract_reply([
            {"file": "mod_a.py", "line": 1, "severity": "nit",
             "comment": "rename A for clarity", "evidence": "read mod_a.py"},
            {"file": "mod_b.py", "line": 1, "severity": "major",
             "comment": "B breaks consumers", "evidence": "grep"},
        ]),
    ])
    state = {"diff_text": "diff --git a/mod_a.py b/mod_a.py\n+A = 1",
             "gate_report": "MERGE STATE: DIRTY", "task_spec": {"pr": 9},
             "repo_path": str(git_repo)}
    result = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary

    # rendered markdown: severity-ordered, verdict from major finding
    review = state["review_text"]
    assert review.index("mod_b.py") < review.index("mod_a.py")
    assert review.endswith("**Verdict:** REQUEST CHANGES")
    assert state["review_comments"][0]["file"] == "mod_b.py"
    assert state["review_summary"].endswith("**Verdict:** REQUEST CHANGES")
    updates = result.outputs["state_updates"]
    assert updates["review_comments"] == state["review_comments"]
    assert updates["review_summary"] == state["review_summary"]

    # dispatch context reached the model: evidence fenced, gate report included,
    # checklist guidance at the prompt TAIL (the system prompt stays static so
    # sibling lenses share one cached prefix), contract demanded
    first = llm.calls[0]
    prompt = first["messages"][0]["content"]
    assert "MERGE STATE: DIRTY" in prompt and "OUTPUT CONTRACT" in prompt
    assert "PERMISSIONS" in prompt and "<untrusted_data>" in prompt
    assert "Breaking behavior" in prompt
    from infermatrix_copilot.review.severity import SEVERITY_GUIDANCE
    assert SEVERITY_GUIDANCE in prompt
    assert "Breaking behavior" not in first["system"]  # static-system invariant
    # knowledge tools offered alongside scoped read tools
    tool_names = {t["name"] for t in first["tools"]}
    assert {"read_file", "skill_search", "gh_pr_view"} <= tool_names
    assert "write_file" not in tool_names  # read-only scope enforced

    # RunTrace: unified-runtime events present
    assert any(True for _ in trace.events("agent_dispatch"))
    out_ev = next(trace.events("agent_output"))
    assert out_ev["status"] == "success" and out_ev["tool_calls"] == 1


def test_review_checklist_resolves_from_adapter_knowledge(settings, trace,
                                                          tmp_path, git_repo):
    """`knowledge.review_checklist` in the adapter manifest injects the named
    knowledge page (first 4k chars) into the reviewer guidance; a path that
    escapes the knowledge root is ignored."""
    kroot = tmp_path / "knowledge"
    (kroot / "repos" / "repo_x").mkdir(parents=True)
    # The marker sits past the old 7k cap, which the vllm-omni checklist was
    # already flush against, and the tail sits past the current one.
    (kroot / "repos" / "repo_x" / "checklist.md").write_text(
        "filler line\n" * 600
        + "REPO-X-CHECKLIST-MARKER: check the frobnicator\n"
        + "pad\n" * 300
        + "REPO-X-BEYOND-BUDGET")
    settings.knowledge_dir = kroot
    adir = settings.adapters_dir / "repo_x"
    adir.mkdir(parents=True)
    (adir / "manifest.yaml").write_text(
        "name: repo_x\nstatus: active\n"
        f"repo: {{path: {git_repo}}}\n"
        "knowledge: {review_checklist: repos/repo_x/checklist.md}\n")
    llm = ScriptedLLM([_contract_reply([])])
    state = {"diff_text": "diff --git a/mod_a.py b/mod_a.py\n"
                          "--- a/mod_a.py\n+++ b/mod_a.py\n+x = 1",
             "task_spec": {"pr": 9, "repo": "repo-x"},
             "repo_path": str(git_repo)}
    result = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "REPO-X-CHECKLIST-MARKER" in prompt
    assert "REPO-X-BEYOND-BUDGET" not in prompt

    # escape guard: a traversal path is ignored, not read
    (adir / "manifest.yaml").write_text(
        "name: repo_x\nstatus: active\n"
        f"repo: {{path: {git_repo}}}\n"
        "knowledge: {review_checklist: ../../../etc/passwd}\n")
    llm2 = ScriptedLLM([_contract_reply([])])
    state2 = dict(state, diff_text=state["diff_text"])
    result2 = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state2, llm=llm2)))
    assert result2.ok
    assert "root:" not in llm2.calls[0]["messages"][0]["content"]


def test_review_contract_repair_round(settings, trace, tmp_path, git_repo):
    """Prose final output triggers exactly one repair call that must yield JSON."""
    llm = ScriptedLLM([
        Reply(blocks=[Block(type="text", text="here is my review in prose")]),
        _contract_reply([]),  # the repair call converts it
    ])
    state = {"diff_text": "diff", "task_spec": {"pr": 9},
             "repo_path": str(git_repo)}
    result = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok
    assert state["review_text"].endswith("**Verdict:** APPROVE")
    assert "Convert the agent's draft output" in llm.calls[-1]["system"]


def test_review_blocked_without_diff(settings, trace, tmp_path):
    result = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, {"task_spec": {"pr": 1}},
             llm=ScriptedLLM([]))))
    assert not result.ok and "no diff_text" in result.summary


def test_pr_review_playbook_shape():
    from infermatrix_copilot.config import _resource_dir
    from infermatrix_copilot.playbooks.store import PlaybookStore

    store = PlaybookStore(_resource_dir("playbooks"), _registry())
    pb = store.get("pr-review")
    assert pb.version == 6  # v6 = declared review_depth param (adaptive depth)
    assert [s.step for s in pb.steps] == [
        "pr.fetch_diff", "pr.gate_check", "agent.review_diff",
        "pr.post_review", "report.final_summary"]
    assert "review_depth" in pb.params  # reuse (L0) with the depth override


def test_review_salvaged_when_agent_escalates_with_comments(settings, trace,
                                                            tmp_path, git_repo):
    """A review carrying comments ships as success even when the agent sets an
    escalating status — finding a blocking defect IS a successful review."""
    reply = json.dumps({
        "status": "needs_review", "summary": "found a blocking survivor",
        "findings": [], "files_read": [], "files_modified": [],
        "tests_requested": [], "tests_run": [], "assumptions": [],
        "blockers": [], "confidence": "high", "failure_kind": "escalate",
        "next_action": "block merge",
        "review_comments": [{"file": "a.py", "line": 1, "severity": "major",
                             "comment": "removed-API survivor", "evidence": "grep"}],
    })
    llm = ScriptedLLM([Reply(blocks=[Block(type="text", text=reply)])])
    state = {"task_spec": {"kind": "pr_review", "pr": 9, "repo": "r"},
             "repo_path": str(git_repo), "diff_text": "+++ b/a.py\n@@ +1\n+x=1"}
    result = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    assert "salvaged" in result.summary
    assert "removed-API survivor" in state["review_text"]
    assert state["review_text"].rstrip().endswith("**Verdict:** REQUEST CHANGES")


def test_anchor_snippet_fixes_the_line_in_both_body_and_comments(
        settings, trace, tmp_path, git_repo):
    """End to end: the model quotes the code and gets the line wrong. The derived
    line must appear in BOTH surfaces — `_render_review_md` prints `file:line` into
    the body, so resolving later (at publish) would show one position in the body and
    anchor the inline thread at another."""
    diff = ("diff --git a/mod_a.py b/mod_a.py\n--- a/mod_a.py\n+++ b/mod_a.py\n"
            "@@ -1,1 +1,3 @@\n A = 1\n+B = items[0]\n+C = 3\n")
    llm = ScriptedLLM([_contract_reply([
        {"file": "mod_a.py", "line": 87, "anchor_snippet": "B = items[0]",
         "severity": "major", "comment": "unguarded index",
         "evidence": "read mod_a.py"},
    ])])
    state = {"diff_text": diff, "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    result = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok
    comment = result.outputs["review_comments"][0]
    assert comment["line"] == 2, "declared line 87 should have been replaced"
    assert "mod_a.py:2" in result.outputs["review_text"]
    assert "mod_a.py:87" not in result.outputs["review_text"]


def test_review_without_snippets_is_untouched(settings, trace, tmp_path, git_repo):
    """The compatibility path: no snippet means the declared line stands, exactly as
    before this feature existed."""
    diff = ("diff --git a/mod_a.py b/mod_a.py\n--- a/mod_a.py\n+++ b/mod_a.py\n"
            "@@ -1,1 +1,2 @@\n A = 1\n+B = 2\n")
    llm = ScriptedLLM([_contract_reply([
        {"file": "mod_a.py", "line": 2, "severity": "nit", "comment": "c",
         "evidence": "e"},
    ])])
    state = {"diff_text": diff, "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    result = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    comment = result.outputs["review_comments"][0]
    assert comment["line"] == 2 and "_anchor_unverified" not in comment


def _dispositions(result):
    return {r["anchor"]: r["disposition"]
            for r in result.outputs["state_updates"]["review_finding_dispositions"]}


def test_an_excluded_candidate_reaches_no_published_surface(
        settings, trace, tmp_path, git_repo):
    """vllm-project/vllm-omni#6959: the summary said to drop the repo-id
    validation request because the evidence packet already covered it, and the
    request was published inline as a P2 anyway. The decision lived in prose
    while publication read the list."""
    diff = ("diff --git a/mod_a.py b/mod_a.py\n--- a/mod_a.py\n+++ b/mod_a.py\n"
            "@@ -1,1 +1,3 @@\n A = 1\n+B = 2\n+C = 3\n")
    llm = ScriptedLLM([_contract_reply([
        {"file": "mod_a.py", "line": 2, "severity": "major",
         "comment": "lock scope is too narrow", "evidence": "read mod_a.py",
         "disposition": "publish"},
        {"file": "mod_a.py", "line": 3, "severity": "minor",
         "comment": "validate the repo id before use",
         "evidence": "read mod_a.py", "disposition": "excluded"},
    ])])
    state = {"diff_text": diff, "task_spec": {"pr": 6959},
             "repo_path": str(git_repo)}

    result = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))

    assert result.ok
    published = result.outputs["review_comments"]
    assert [c["comment"] for c in published] == ["lock scope is too narrow"]
    # Not in the inline comments, not in the body, not in the summary.
    assert "validate the repo id" not in result.outputs["review_text"]
    assert "validate the repo id" not in result.outputs["review_summary"]
    assert _dispositions(result)["mod_a.py:3"] == "excluded"


def test_a_no_issue_confirmation_never_carries_a_finding_priority(
        settings, trace, tmp_path, git_repo):
    """vllm-project/vllm-omni#6959 again: a confirmation that the inspected
    SPDX headers left no stale comments was published as a P2 finding. It
    reports no defect and asks for nothing, so it cannot be ranked — but the
    check itself is worth keeping, as a note."""
    diff = ("diff --git a/mod_a.py b/mod_a.py\n--- a/mod_a.py\n+++ b/mod_a.py\n"
            "@@ -1,1 +1,3 @@\n A = 1\n+B = 2\n+C = 3\n")
    llm = ScriptedLLM([_contract_reply([
        {"file": "mod_a.py", "line": 2, "severity": "minor",
         "comment": "the SPDX headers leave no stale docstrings",
         "evidence": "read mod_a.py", "disposition": "no_issue"},
    ])])
    state = {"diff_text": diff, "task_spec": {"pr": 6959},
             "repo_path": str(git_repo)}

    result = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))

    assert result.ok
    assert result.outputs["review_comments"] == []
    summary = result.outputs["review_summary"]
    assert "Checked, no defect found:" in summary
    assert "no stale docstrings" in summary
    # A review that collected observations but has nothing to ask for is a
    # valid zero-finding result, not a REQUEST CHANGES.
    assert summary.rstrip().endswith("**Verdict:** APPROVE")


def test_withheld_candidates_do_not_consume_the_comment_budget(
        settings, trace, tmp_path, git_repo):
    """Finalization runs before the eight-comment cut, so an excluded
    candidate cannot push a real finding out of the published set."""
    diff = ("diff --git a/mod_a.py b/mod_a.py\n--- a/mod_a.py\n+++ b/mod_a.py\n"
            "@@ -1,1 +1,12 @@\n A = 1\n" + "".join(
                f"+L{n} = {n}\n" for n in range(2, 13)))
    comments = [
        {"file": "mod_a.py", "line": n, "severity": "nit",
         "comment": f"withheld {n}", "evidence": "e", "disposition": "duplicate"}
        for n in range(2, 10)
    ] + [
        {"file": "mod_a.py", "line": 11, "severity": "nit",
         "comment": "the surviving finding", "evidence": "e"},
    ]
    llm = ScriptedLLM([_contract_reply(comments)])
    state = {"diff_text": diff, "task_spec": {"pr": 1},
             "repo_path": str(git_repo)}

    result = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))

    assert result.ok
    published = [c["comment"] for c in result.outputs["review_comments"]]
    assert published == ["the surviving finding"]


def test_a_review_without_dispositions_publishes_exactly_as_before(
        settings, trace, tmp_path, git_repo):
    """The field is additive: an older run that carries none is unchanged."""
    diff = ("diff --git a/mod_a.py b/mod_a.py\n--- a/mod_a.py\n+++ b/mod_a.py\n"
            "@@ -1,1 +1,2 @@\n A = 1\n+B = 2\n")
    llm = ScriptedLLM([_contract_reply([
        {"file": "mod_a.py", "line": 2, "severity": "major",
         "comment": "unguarded index", "evidence": "e"},
    ])])
    state = {"diff_text": diff, "task_spec": {"pr": 1},
             "repo_path": str(git_repo)}

    result = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))

    assert result.ok
    assert [c["comment"] for c in result.outputs["review_comments"]] == [
        "unguarded index"]
    assert _dispositions(result) == {"mod_a.py:2": "publish"}


def test_an_unreadable_disposition_publishes_and_is_recorded(
        settings, trace, tmp_path, git_repo):
    """Losing a real finding to a typo is worse than publishing one whose
    bookkeeping we cannot read; the declared value is kept so it is visible."""
    diff = ("diff --git a/mod_a.py b/mod_a.py\n--- a/mod_a.py\n+++ b/mod_a.py\n"
            "@@ -1,1 +1,2 @@\n A = 1\n+B = 2\n")
    llm = ScriptedLLM([_contract_reply([
        {"file": "mod_a.py", "line": 2, "severity": "major",
         "comment": "unguarded index", "evidence": "e", "disposition": "drop"},
    ])])
    state = {"diff_text": diff, "task_spec": {"pr": 1},
             "repo_path": str(git_repo)}

    result = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))

    assert result.ok
    assert len(result.outputs["review_comments"]) == 1
    record = result.outputs["state_updates"]["review_finding_dispositions"][0]
    assert record["disposition"] == "publish"
    assert record["declared"] == "drop"


def test_a_budget_cut_is_not_recorded_as_a_review_decision(
        settings, trace, tmp_path, git_repo):
    """Nine publishable findings, eight slots. The ninth was not withheld by
    the review, so a consumer checking that nothing withheld got published
    must not trip over it."""
    diff = ("diff --git a/mod_a.py b/mod_a.py\n--- a/mod_a.py\n+++ b/mod_a.py\n"
            "@@ -1,1 +1,11 @@\n A = 1\n" + "".join(
                f"+L{n} = {n}\n" for n in range(2, 12)))
    llm = ScriptedLLM([_contract_reply([
        {"file": "mod_a.py", "line": n, "severity": "nit",
         "comment": f"finding {n}", "evidence": "e"}
        for n in range(2, 11)
    ])])
    state = {"diff_text": diff, "task_spec": {"pr": 1},
             "repo_path": str(git_repo)}

    result = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))

    assert result.ok
    assert len(result.outputs["review_comments"]) == 8
    recorded = [r["disposition"]
                for r in result.outputs["state_updates"][
                    "review_finding_dispositions"]]
    assert recorded.count("publish") == 8
    assert recorded.count("over_budget") == 1
    assert "excluded" not in recorded


def test_carried_findings_are_rechecked_on_frozen_head_and_persisted(settings, trace, tmp_path, git_repo):
    from infermatrix_copilot.contract import build_review_result
    head = "a" * 40
    carried = [{"finding_id": "old-7", "source_head_sha": "b" * 40,
                "severity": "blocker", "path": "mod_a.py", "title": "Old defect"}]
    reply = _contract_reply([])
    payload = json.loads(reply.blocks[0].text)
    payload["finding_rechecks"] = [{"finding_id": "old-7", "head_sha": head,
        "outcome": "fixed", "evidence": "mod_a.py now guards the None input"}]
    llm = ScriptedLLM([_contract_reply([]), Reply(blocks=[Block(type="text", text=json.dumps(payload))])])
    state = {"diff_text": "diff --git a/mod_a.py b/mod_a.py\n+A = 1",
             "pr_head_sha": head, "repo_path": str(git_repo),
             "task_spec": {"kind": "pr_review", "pr": 9, "params": {"carried_findings": carried}}}
    result = asyncio.run(_registry().get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    prompt = llm.calls[-1]["messages"][0]["content"]
    assert "old-7" in prompt and head in prompt and "untrusted_data" in prompt
    assert "write_file" not in {tool["name"] for tool in llm.calls[-1]["tools"]}
    updates = result.outputs["state_updates"]
    updates["pr_head_sha"] = head
    run_dir = tmp_path / "assembled"
    run_dir.mkdir()
    (run_dir / "progress.json").write_text(json.dumps({"completed": {
        "review": {"outputs": {"state_updates": updates}}}}))
    structured = build_review_result(run_dir)
    assert structured["rechecks_complete"]
    assert structured["finding_rechecks"] == payload["finding_rechecks"]
