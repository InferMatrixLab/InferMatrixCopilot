"""Ensemble agent steps: perspective-diverse fan-out + verify-and-merge
(run_agent_step_ensemble) and its wiring into agent.review_diff."""

import asyncio
import json
import pytest

from infermatrix_copilot.engine.agent_runtime import (
    BASE_OUTPUT_SCHEMA,
    run_agent_step_ensemble,
)
from infermatrix_copilot.engine.steps import register_builtin_steps
from infermatrix_copilot.engine.steps.review import _REVIEW_LENSES
from infermatrix_copilot.engine.registry import StepRegistry
from infermatrix_copilot.engine.step import FailureKind, StepContext
from infermatrix_copilot.llm import Block, Reply


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


def verdicts_reply(*vs, summary="merged", **extra):
    """A reducer reply in the per-candidate verdict contract."""
    return Reply(blocks=[Block(type="text", text=json.dumps(
        {"verdicts": list(vs), "summary": summary, **extra}))])


def _ctx(settings, trace, tmp_path, state=None, llm=None):
    return StepContext(settings=settings, state=state or {"task_spec": {"pr": 1}},
                       params={}, run_dir=tmp_path / "run", trace=trace, llm=llm)


LENSES = [{"name": "a", "focus": "look at A"}, {"name": "b", "focus": "look at B"}]


def _run(ctx, **kw):
    defaults = dict(step_name="t.step", purpose="test",
                    evidence={"e": "the evidence"}, lenses=LENSES,
                    merge_key="items",
                    output_extension={"items": "list of {name}"})
    defaults.update(kw)
    return asyncio.run(run_agent_step_ensemble(ctx, **defaults))


def test_ensemble_fans_out_and_merges(settings, trace, tmp_path):
    llm = ScriptedLLM([
        contract(items=[{"name": "shared"}, {"name": "only-a"}]),   # lens a
        contract(items=[{"name": "shared"}, {"name": "only-b"}]),   # lens b
        verdicts_reply({"i": 0, "action": "keep"},
                       {"i": 1, "action": "keep"},
                       {"i": 2, "action": "keep"}),                 # reduce
    ])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    assert [i["name"] for i in output["items"]] == ["shared", "only-a",
                                                    "only-b"]
    assert "lenses" not in output["items"][0]  # tags stripped from the result
    assert result.summary.startswith("[ensemble x2]")

    # each lens got its focus (at the prompt tail — static-system invariant);
    # the merger got the numbered candidates + evidence
    assert "look at A" in llm.calls[0]["messages"][0]["content"]
    assert "look at B" in llm.calls[1]["messages"][0]["content"]
    merge = llm.calls[2]
    assert "verify-and-merge" in merge["system"]
    body = merge["messages"][0]["content"]
    # "shared" collapsed to ONE candidate carrying cross-lens consensus
    assert '"consensus": 2' in body and '"only-a"' in body
    assert "the evidence" in body

    ev = next(trace.events("agent_ensemble"))
    assert ev["lenses"] == ["a", "b"] and ev["candidates"] == 3
    assert ev["merged"] == 3 and ev["verified"] is True


def test_ensemble_small_union_skips_reduction(settings, trace, tmp_path):
    """A small union whose every item has cross-lens consensus needs no
    arbitration — the reducer is never called (its latency was ~25% of
    ensemble wall-clock on small PRs)."""
    llm = ScriptedLLM([
        contract(items=[{"name": "x"}]),   # lens a
        contract(items=[{"name": "x"}]),   # lens b — same item, consensus 2
    ])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    assert [i["name"] for i in output["items"]] == ["x"]
    assert "lenses" not in output["items"][0]
    assert len(llm.calls) == 2  # no reducer call
    ev = next(trace.events("agent_ensemble"))
    assert ev["candidates"] == 1 and ev["merged"] == 1


def test_ensemble_singleton_without_consensus_still_verified(settings, trace,
                                                             tmp_path):
    """An unreplicated single-lens claim must face the reducer — a
    hallucinated blocker once skipped verification via the small-union fast
    path and became the entire review."""
    llm = ScriptedLLM([
        contract(items=[{"name": "maybe-hallucinated"}]),  # lens a only
        contract(items=[]),                                # lens b: nothing
        verdicts_reply({"i": 0, "action": "drop",
                        "why": "not grounded in the evidence"}),
    ])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    assert output["items"] == []
    assert len(llm.calls) == 3  # reducer WAS called
    ev = next(trace.events("agent_ensemble"))
    assert ev["dropped"] == 1 and ev["verified"] is True


def test_ensemble_merge_failure_falls_open_to_union(settings, trace, tmp_path):
    llm = ScriptedLLM([
        contract(items=[{"name": "x"}, {"name": "x2"}]),
        contract(items=[{"name": "y"}]),
        Reply(blocks=[Block(type="text", text="prose")]),   # merge unparseable
        Reply(blocks=[Block(type="text", text="still prose")]),  # repair fails
    ])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    assert [i["name"] for i in output["items"]] == ["x", "x2", "y"]
    assert "lenses" not in output["items"][0]
    assert "unverified union" in output["summary"]
    assert next(trace.events("agent_ensemble"))["verified"] is False


def test_ensemble_empty_merge_reply_falls_open_without_repair(settings, trace,
                                                              tmp_path):
    """An empty reducer reply must NOT go through the repair round (which would
    hallucinate a contract from nothing — live bug on PR 4678)."""
    llm = ScriptedLLM([
        contract(items=[{"name": "x"}, {"name": "x2"}]),
        contract(items=[{"name": "y"}]),
        Reply(blocks=[Block(type="text", text="")]),   # reducer returns nothing
    ])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    assert [i["name"] for i in output["items"]] == ["x", "x2", "y"]  # union
    assert len(llm.calls) == 3  # no repair call happened


def test_ensemble_status_comes_from_samples_not_reducer(settings, trace,
                                                        tmp_path):
    """Reducers conflate step status with the reviewed artifact's verdict
    (live bug: merge said needs_review about the PR, step got escalated)."""
    llm = ScriptedLLM([
        contract(items=[{"name": "x"}]),
        contract(items=[{"name": "x"}]),
        verdicts_reply({"i": 0, "action": "keep"}, status="needs_review"),
    ])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok and output["status"] == "success"


def test_ensemble_reducer_losing_payload_falls_open(settings, trace, tmp_path):
    llm = ScriptedLLM([
        contract(items=[{"name": "x"}, {"name": "x2"}]),
        contract(items=[{"name": "y"}]),
        contract(),  # contract-shaped but no verdicts key
        contract(),  # ...and the repair round loses it again
    ])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    assert [i["name"] for i in output["items"]] == ["x", "x2", "y"]
    assert next(trace.events("agent_ensemble"))["verified"] is False


def test_ensemble_survives_one_failed_lens(settings, trace, tmp_path):
    llm = ScriptedLLM([
        Reply(blocks=[Block(type="text", text="prose")]),    # lens a: no contract
        Reply(blocks=[Block(type="text", text="prose")]),    # ...repair fails too
        contract(items=[{"name": "y"}]),                     # lens b ok
        verdicts_reply({"i": 0, "action": "keep"}),          # merge
    ])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    # T4 salvage: the failed lens still contributes a (candidate-less) sample,
    # so it appears in the lens list; its salvaged text adds no items.
    assert next(trace.events("agent_ensemble"))["lenses"] == ["a", "b"]


def test_ensemble_all_lenses_failed(settings, trace, tmp_path):
    """T4 salvage: unparseable lens finals wrap as needs_review, so an
    all-failed ensemble ESCALATES (raw texts preserved) instead of RETRY."""
    llm = ScriptedLLM([Reply(blocks=[Block(type="text", text="prose")])] * 4)
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert not result.ok and result.failure is FailureKind.ESCALATE


class KeyedLLM:
    """Thread-safe fake for the PARALLEL ensemble path: picks the reply by a
    key found in the system prompt, so lens completion order doesn't matter."""

    def __init__(self, by_key: dict):
        import threading
        self._by_key = by_key
        self._lock = threading.Lock()
        self.calls = []
        self.available = True

    def create(self, *, system, messages, tools=None, model=None,
               max_tokens=None, on_text=None, role=""):
        with self._lock:
            self.calls.append({"system": system, "messages": [*messages]})
        # lens markers ride the user prompt now (static-system invariant);
        # match on system + first-message content so either placement keys.
        haystack = system + "\n" + str(messages[0].get("content", ""))
        for key, reply in self._by_key.items():
            if key in haystack:
                return reply
        raise AssertionError(f"no scripted reply matches: {haystack[:80]}")


def test_ensemble_parallel_lenses_merge(settings, trace, tmp_path):
    """ensemble_parallel=True runs lenses concurrently; the merge must still
    see every lens's candidates and samples keep lens order."""
    settings.ensemble_parallel = True
    llm = KeyedLLM({
        "Your assigned lens: a": contract(items=[{"name": "from-a"},
                                                 {"name": "from-a2"}]),
        "Your assigned lens: b": contract(items=[{"name": "from-b"}]),
        "verify-and-merge": verdicts_reply({"i": 0, "action": "keep"},
                                           {"i": 1, "action": "keep"},
                                           {"i": 2, "action": "keep"}),
    })
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    assert [i["name"] for i in output["items"]] == ["from-a", "from-a2",
                                                    "from-b"]
    ev = next(trace.events("agent_ensemble"))
    assert ev["lenses"] == ["a", "b"] and ev["candidates"] == 3
    merge_call = next(c for c in llm.calls if "verify-and-merge" in c["system"])
    body = merge_call["messages"][0]["content"]
    assert '"from-a"' in body and '"from-b"' in body


def test_ensemble_reducer_verdicts_drop_dup_and_failopen(settings, trace,
                                                         tmp_path):
    """Per-candidate reduction: drops need a why, dups consolidate into the
    survivor, and any candidate the reducer does not mention is KEPT (the
    fail-open is per item — free-form reducers silently lost findings)."""
    llm = ScriptedLLM([
        contract(items=[{"name": "real", "comment": "c1"},
                        {"name": "misread", "comment": "c2"},
                        {"name": "same-as-real", "comment": "c3"}]),  # lens a
        contract(items=[{"name": "unjudged", "comment": "c4"}]),      # lens b
        verdicts_reply({"i": 0, "action": "keep", "comment": "rewritten"},
                       {"i": 1, "action": "drop", "why": "misreads evidence"},
                       {"i": 2, "action": "dup", "of": 0}),
        # candidate 3 (unjudged) is never mentioned -> kept unchanged
    ])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    assert [i["name"] for i in output["items"]] == ["real", "unjudged"]
    assert output["items"][0]["comment"] == "rewritten"
    assert output["items"][1]["comment"] == "c4"
    ev = next(trace.events("agent_ensemble"))
    assert ev["candidates"] == 4 and ev["merged"] == 2 and ev["dropped"] == 1
    assert ev["verified"] is True


def test_ensemble_samples_per_lens_union(settings, trace, tmp_path):
    """Repeat samples per lens: identical items collapse with a consensus
    count; the union across samples is what reaches the reducer."""
    settings.ensemble_samples_per_lens = 2
    llm = ScriptedLLM([
        contract(items=[{"name": "x"}]),   # a/1
        contract(items=[{"name": "x"}]),   # a/2 — identical -> consensus 2
        contract(items=[{"name": "y"}]),   # b/1
        contract(items=[{"name": "z"}]),   # b/2
        verdicts_reply({"i": 0, "action": "keep"}, {"i": 1, "action": "keep"},
                       {"i": 2, "action": "keep"}),
    ])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    assert [i["name"] for i in output["items"]] == ["x", "y", "z"]
    assert result.summary.startswith("[ensemble x4]")
    assert '"consensus": 2' in llm.calls[4]["messages"][0]["content"]
    dispatches = [e["step"] for e in trace.events("agent_dispatch")]
    assert dispatches == ["t.step#a/1", "t.step#a/2",
                          "t.step#b/1", "t.step#b/2"]


def test_review_step_caps_comments_deterministically(settings, trace, tmp_path,
                                                     git_repo):
    """The comment budget is enforced in code (severity-ordered, cap 8) — the
    low-signal nit tail goes first; on a review that is not evidence-rich
    (<2 major findings) the overflow tail does not render at all (val gate:
    a thin-GT item rendering 12 findings lost 3/3 on noise). Reducers
    ignored a prompted cap."""
    settings.review_ensemble = True
    settings.review_depth = "full"  # pin: this test exercises ensemble mechanics
    many = ([{"file": "m.py", "line": i, "severity": "nit",
              "comment": f"n{i}", "evidence": "hunk"} for i in range(6)]
            + [{"file": "m.py", "line": 9, "severity": "major",
                "comment": "big", "evidence": "hunk"}]
            + [{"file": "m.py", "line": 20 + i, "severity": "minor",
                "comment": f"m{i}", "evidence": "hunk"} for i in range(5)])
    from infermatrix_copilot.engine.steps import register_builtin_steps
    llm = ScriptedLLM(
        [contract(review_comments=many)]
        + [contract(review_comments=[])] * (len(_REVIEW_LENSES) - 1)
        + [verdicts_reply()])   # reducer silent -> all 12 kept, then capped
    state = {"diff_text": "diff --git a/m.py b/m.py\n+A = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    kept = result.outputs["review_comments"]
    assert len(kept) == 8
    sevs = [c["severity"] for c in kept]
    assert sevs == ["major", "minor", "minor", "minor", "minor", "minor",
                    "nit", "nit"]
    # 1 major < the rich threshold: the overflow tail does not render
    assert "Additional observations" not in state["review_text"]
    assert state["review_text"].endswith("**Verdict:** REQUEST CHANGES")


def test_review_overflow_renders_reducer_kept_minors(settings, trace, tmp_path,
                                                     git_repo):
    """On an evidence-rich review (≥2 major findings), minor-and-above
    findings past the cap render as one-line observations (the pr4859 class:
    reducer-kept ground-truth minors must stay visible); nits past the cap
    are dropped silently."""
    settings.review_ensemble = True
    settings.review_depth = "full"
    many = ([{"file": "m.py", "line": i, "severity": "major",
              "comment": f"maj{i}", "evidence": "hunk"} for i in range(8)]
            + [{"file": "m.py", "line": 30, "severity": "minor",
                "comment": "gt-minor-survives", "evidence": "hunk"}]
            + [{"file": "m.py", "line": 40, "severity": "nit",
                "comment": "nit-vanishes", "evidence": "hunk"}])
    from infermatrix_copilot.engine.steps import register_builtin_steps
    llm = ScriptedLLM(
        [contract(review_comments=many)]
        + [contract(review_comments=[])] * (len(_REVIEW_LENSES) - 1)
        + [verdicts_reply()])
    state = {"diff_text": "diff --git a/m.py b/m.py\n+A = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    assert len(result.outputs["review_comments"]) == 8
    assert "Additional observations" in state["review_text"]
    assert "gt-minor-survives" in state["review_text"]
    assert "nit-vanishes" not in state["review_text"]


def test_cross_lens_corroboration_ranks_first_at_the_cap(settings, trace,
                                                         tmp_path, git_repo):
    """Two lenses independently flagging the same file within a few lines is
    the strongest signal in the ensemble; under the comment budget those
    findings outrank same-severity singletons (which previously survived on
    list order while corroborated ground-truth concerns were cut)."""
    settings.review_ensemble = True
    settings.review_depth = "full"
    settings.ensemble_zero_yield_retry = False
    corro_a = {"file": "hot.py", "line": 10, "severity": "minor",
               "comment": "shared concern", "evidence": "hunk"}
    corro_b = {"file": "hot.py", "line": 12, "severity": "minor",
               "comment": "shared concern, other wording", "evidence": "hunk"}
    singles = [{"file": f"s{i}.py", "line": 1, "severity": "minor",
                "comment": f"single {i}", "evidence": "hunk"}
               for i in range(8)]
    llm = ScriptedLLM([
        contract(review_comments=[corro_a] + singles),   # lens 1
        contract(review_comments=[corro_b]),             # lens 2
        # remaining lenses contribute nothing — count follows the lens list
        *[contract(review_comments=[])] * (len(_REVIEW_LENSES) - 2),
        verdicts_reply(),                                # reducer silent: keep all
    ])
    state = {"diff_text": "diff --git a/hot.py b/hot.py\n+A = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    kept = result.outputs["review_comments"]
    assert len(kept) == 8
    assert {kept[0]["file"], kept[1]["file"]} == {"hot.py"}
    assert all("corroborated_by" not in c for c in kept)


def test_coverage_promotion_converts_findings_to_comments(settings, trace,
                                                          tmp_path, git_repo):
    """A maintainer-relevant concern that a lens recorded in `findings` but
    never emitted as a comment is promoted (grounded in the recorded line) by
    the post-budget coverage pass; a scripted no-addition reply leaves the
    review untouched."""
    settings.review_ensemble = True
    settings.review_depth = "full"
    settings.ensemble_zero_yield_retry = False
    base = {"file": "m.py", "line": 1, "severity": "major",
            "comment": "real defect", "evidence": "hunk"}
    lens1 = contract(review_comments=[base],
                     findings=["[sweep] linked issue #99 reports two "
                               "regressions; this PR fixes only one"])
    promotion = Reply(blocks=[Block(type="text", text=json.dumps({
        "additions": [{"file": "m.py", "line": 2, "severity": "minor",
                       "comment": "issue #99's second regression remains "
                                  "unfixed — track or split it",
                       "evidence": "[sweep] linked issue #99 reports two "
                                   "regressions; this PR fixes only one"}]}))])
    llm = ScriptedLLM([
        lens1,
        # lenses 2..N contribute nothing — count follows the lens list
        *[contract(review_comments=[])] * (len(_REVIEW_LENSES) - 1),
        verdicts_reply({"i": 0, "action": "keep"}),
        promotion,
    ])
    state = {"diff_text": "diff --git a/m.py b/m.py\n+A = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    kept = result.outputs["review_comments"]
    assert len(kept) == 2
    assert "remains unfixed" in kept[1]["comment"]
    from infermatrix_copilot.review.severity import SEVERITY_GUIDANCE
    assert SEVERITY_GUIDANCE in llm.calls[-1]["system"]
    # The reducer and investigator use the same definitions as promotion.
    assert SEVERITY_GUIDANCE in llm.calls[-2]["system"]
    assert SEVERITY_GUIDANCE in llm.calls[0]["messages"][0]["content"]
    assert next(trace.events("review_coverage_promoted"))["added"] == 1
    assert "2 actionable finding(s)." in result.outputs["review_summary"]
    assert "merged" not in result.outputs["review_summary"]
    assert not llm._replies


def test_rejected_facts_stay_available_and_non_actionable_additions_drop(
        settings, trace, tmp_path, git_repo):
    """A true unused-key observation must not become a published defect
    merely because coverage revisits it after the reducer rejected it."""
    settings.review_ensemble = True
    settings.review_depth = "full"
    settings.review_second_round = True
    settings.review_verify_comments = True
    settings.review_verify_concurrency = 1
    harmless = {"file": "config.py", "line": 10, "severity": "minor",
                "comment": "optional key also reaches an unused consumer",
                "evidence": "config.py:10 `allowed |= {'model_config'}`"}
    future = {"file": "config.py", "line": 40, "severity": "minor",
              "comment": "validator may reject this if its fields change later",
              "evidence": "config.py:40 `fields = current_fields()`"}
    reason = "No current affected consumer; forwarding is harmless"
    llm = ScriptedLLM([
        contract(review_comments=[harmless],
                 findings=["[validated] optional key is forwarded"]),
        *[contract(review_comments=[])] * (len(_REVIEW_LENSES) - 1),
        verdicts_reply({"i": 0, "action": "drop", "why": reason},
                       summary="No actionable findings."),
        Reply(blocks=[Block(type="text", text=json.dumps(
            {"additions": [harmless]}))]),
        contract(review_comments=[future]),
        contract(verdict="not_actionable", evidence=reason),
        contract(verdict="not_actionable",
                 evidence="current_fields excludes the key on this head"),
    ])
    state = {"diff_text": "diff --git a/config.py b/config.py\n"
                          "+++ b/config.py\n@@ -1,0 +1,1 @@\n+allowed = set()",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    result = asyncio.run(register_builtin_steps(StepRegistry()).get(
        "agent.review_diff").handler(
            _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok
    assert result.outputs["review_comments"] == []
    assert result.outputs["review_summary"].endswith("**Verdict:** APPROVE")
    assert "optional key also reaches" not in result.outputs["review_text"]
    assert next(trace.events("review_comments_verified"))["dropped"] == 2
    # The decision survives every boundary, including the second round and
    # independent verifiers, instead of only living in a reducer archive.
    for call in llm.calls[-4:]:
        assert reason in json.dumps(call["messages"])
    assert not llm._replies


def test_verification_removal_clears_stale_reducer_summary(
        settings, trace, tmp_path, git_repo):
    settings.review_ensemble = True
    settings.review_depth = "full"
    settings.review_verify_comments = True
    llm = ScriptedLLM([
        contract(review_comments=[{"file": "config.py", "line": 1,
                                   "severity": "major", "comment": "broken",
                                   "evidence": "config.py:1"}]),
        *[contract(review_comments=[])] * (len(_REVIEW_LENSES) - 1),
        verdicts_reply({"i": 0, "action": "keep"},
                       summary="One blocking defect remains."),
        contract(verdict="refuted"),
    ])
    state = {"diff_text": "diff --git a/config.py b/config.py\n+x = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    result = asyncio.run(register_builtin_steps(StepRegistry()).get(
        "agent.review_diff").handler(
            _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok
    assert result.outputs["review_comments"] == []
    for field in ("review_text", "review_summary"):
        assert "blocking defect remains" not in result.outputs[field]
        assert "No actionable findings." in result.outputs[field]
    assert not llm._replies


def test_deep_engine_runs_investigator_plus_adversary(settings, trace,
                                                      tmp_path, git_repo):
    """review_deep_engine runs the hybrid pass set: full depth = deep
    investigator + adversary plus the behavior + verification breadth
    lenses (4 dispatches); standard = investigator + behavior. Depth still
    comes from the planner."""
    settings.review_ensemble = True
    settings.review_deep_engine = True
    settings.review_depth = "full"
    settings.ensemble_zero_yield_retry = False
    c1 = [{"file": "a.py", "line": 1, "severity": "major",
           "comment": "central-change defect", "evidence": "read a.py"}]
    c2 = [{"file": "b.py", "line": 2, "severity": "minor",
           "comment": "blast radius question", "evidence": "read b.py"}]
    llm = ScriptedLLM([
        contract(review_comments=c1),                 # investigator
        contract(review_comments=c2),                 # adversary
        contract(review_comments=[]),                 # behavior
        contract(review_comments=[]),                 # verification
        verdicts_reply({"i": 0, "action": "keep"},
                       {"i": 1, "action": "keep"}),
    ])
    state = {"diff_text": "diff --git a/a.py b/a.py\n+A = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    steps_run = [e["step"] for e in trace.events("agent_dispatch")]
    assert steps_run == ["agent.review_diff#investigator",
                         "agent.review_diff#adversary",
                         "agent.review_diff#behavior",
                         "agent.review_diff#verification"]
    ens = next(trace.events("agent_ensemble"))
    assert ens["lenses"] == ["investigator", "adversary",
                             "behavior", "verification"]
    assert len(result.outputs["review_comments"]) == 2
    assert not llm._replies


def test_verify_pass_drops_refuted_demotes_unverifiable(settings, trace,
                                                        tmp_path, git_repo):
    """Per-comment verification: refuted comments drop, unverifiable ones
    keep but demote one severity step, confirmed ones may be tightened; a
    garbage verdict fails open (comment kept unchanged)."""
    settings.review_ensemble = True
    settings.review_depth = "full"
    settings.ensemble_zero_yield_retry = False
    settings.review_verify_comments = True
    settings.review_verify_concurrency = 1  # ordered ScriptedLLM needs determinism
    cs = [
        {"file": "a.py", "line": 1, "severity": "major",
         "comment": "refute me", "evidence": "hunk"},
        {"file": "b.py", "line": 2, "severity": "major",
         "comment": "confirm me", "evidence": "hunk"},
        {"file": "c.py", "line": 3, "severity": "major",
         "comment": "unverifiable claim", "evidence": "hunk"},
        {"file": "d.py", "line": 4, "severity": "minor",
         "comment": "garbage verdict for me", "evidence": "hunk"},
    ]
    llm = ScriptedLLM([
        contract(review_comments=cs),                 # lens 1
        # lenses 2..N contribute nothing — count follows the lens list
        *[contract(review_comments=[])] * (len(_REVIEW_LENSES) - 1),
        verdicts_reply({"i": 0, "action": "keep"},    # reducer keeps all
                       {"i": 1, "action": "keep"},
                       {"i": 2, "action": "keep"},
                       {"i": 3, "action": "keep"}),
        contract(verdict="refuted"),                              # verify c0
        contract(verdict="confirmed", review_comments=[],
                 comment="tightened: verified against b.py"),     # verify c1
        contract(verdict="unverifiable"),                         # verify c2
        contract(verdict="banana"),                               # verify c3
    ])
    state = {"diff_text": "diff --git a/a.py b/a.py\n+A = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    kept = result.outputs["review_comments"]
    by_file = {c["file"]: c for c in kept}
    assert "a.py" not in by_file                       # refuted → dropped
    assert by_file["b.py"]["comment"].startswith("tightened:")
    assert by_file["c.py"]["severity"] == "minor"      # major demoted
    assert by_file["d.py"]["comment"] == "garbage verdict for me"  # fail-open
    ev = next(trace.events("review_comments_verified"))
    assert (ev["total"], ev["dropped"], ev["demoted"]) == (4, 1, 1)
    from infermatrix_copilot.review.severity import SEVERITY_GUIDANCE
    for call in llm.calls[-4:]:
        assert SEVERITY_GUIDANCE in call["messages"][0]["content"]
    assert not llm._replies


def test_render_verdict_calibration():
    """T4 calibration: only verified blocker/major block; other comments are
    COMMENT (mergeable with asks); none -> APPROVE. Self-declared-uncertain
    majors never block (T3 forensics: 14/15 human-approved PRs got REQUEST
    CHANGES under the old minor-blocks rule). [validated] findings render."""
    from infermatrix_copilot.engine.steps.review import _render_review_md

    major = {"review_comments": [{"file": "a.py", "line": 1, "severity":
                                  "major", "comment": "breaks X", "evidence": "hunk"}]}
    assert _render_review_md(major).endswith("**Verdict:** REQUEST CHANGES")
    minor = {"review_comments": [{"file": "a.py", "line": 1, "severity":
                                  "minor", "comment": "simplify", "evidence": "hunk"}]}
    assert _render_review_md(minor).endswith("**Verdict:** COMMENT")
    uncertain_major = {"review_comments": [
        {"file": "a.py", "line": 1, "severity": "major",
         "comment": "potential gap; comment is uncertain", "evidence": "hunk"}]}
    assert _render_review_md(uncertain_major).endswith("**Verdict:** COMMENT")
    assert _render_review_md({"summary": "clean"}).endswith(
        "**Verdict:** APPROVE")
    validated = {"summary": "clean",
                 "findings": ["[upstream-verify] vllm/x.py:12 — API confirmed"]}
    out = _render_review_md(validated)
    assert "**Validated:**" in out and "x.py:12" in out


def test_review_step_uses_ensemble_when_enabled(settings, trace, tmp_path,
                                                git_repo):
    settings.review_ensemble = True
    settings.review_depth = "full"  # pin: tiny diff would auto-plan light
    comments = [{"file": "mod_a.py", "line": 1, "severity": "major",
                 "comment": "the diff sets A=1 which breaks B — guard it",
                 "evidence": "hunk"}]
    llm = ScriptedLLM(
        [contract(review_comments=comments)] * len(_REVIEW_LENSES)
        + [verdicts_reply({"i": 0, "action": "keep"})]     # merge
    )
    state = {"diff_text": "diff --git a/mod_a.py b/mod_a.py\n+A = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    assert state["review_text"].endswith("**Verdict:** REQUEST CHANGES")
    ev = next(trace.events("agent_ensemble"))
    assert ev["lenses"] == [lens["name"] for lens in _REVIEW_LENSES]
    # every lens sample went through the unified runtime (agent_dispatch each)
    dispatches = list(trace.events("agent_dispatch"))
    assert len(dispatches) == len(_REVIEW_LENSES)
    plan_ev = next(trace.events("review_plan"))
    assert plan_ev["depth"] == "full" and plan_ev["planner"] == "override"


def test_review_step_auto_plans_light_for_tiny_diff(settings, trace, tmp_path,
                                                    git_repo):
    """A tiny low-risk diff under review_depth=auto runs ONE full-checklist
    pass — no ensemble, no reducer (the Codex-measured 1.1M-token 4-lens run
    on a 2-file/+60-line PR is the regime this removes)."""
    settings.review_ensemble = True
    comments = [{"file": "mod_a.py", "line": 1, "severity": "minor",
                 "comment": "tighten the guard", "evidence": "hunk"}]
    llm = ScriptedLLM([contract(review_comments=comments)])  # exactly one pass
    state = {"diff_text": "diff --git a/mod_a.py b/mod_a.py\n"
                          "--- a/mod_a.py\n+++ b/mod_a.py\n+x = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    plan_ev = next(trace.events("review_plan"))
    assert plan_ev["depth"] == "light" and plan_ev["planner"] == "rules"
    assert len(list(trace.events("agent_dispatch"))) == 1
    assert not list(trace.events("agent_ensemble"))
    assert result.outputs["review_plan"]["depth"] == "light"
    assert "depth=light via rules" in result.summary


def test_review_step_invalid_override_blocks_before_any_llm(settings, trace,
                                                            tmp_path, git_repo):
    """A typo like review_depth=ful must fail fast, never silently downgrade
    an explicitly requested full review."""
    settings.review_ensemble = True
    llm = ScriptedLLM([])   # any LLM call would pop from an empty script
    state = {"diff_text": "diff --git a/m.py b/m.py\n+x = 1",
             "task_spec": {"pr": 9, "params": {"review_depth": "ful"}},
             "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert not result.ok and result.failure is FailureKind.BLOCKED
    assert "invalid review_depth" in result.summary
    assert not llm.calls


def test_review_step_gray_zone_falls_back_to_standard(settings, trace,
                                                      tmp_path, git_repo):
    """Mid-size diff + unparseable planner reply → deterministic standard
    (logic+behavior+verification): 1 garbage planner call, 3 lens passes,
    1 reducer. Verification rides the fallback so the test-integrity class
    is never silently skipped on planner failure."""
    settings.review_ensemble = True
    body = "\n".join(f"+line {i}" for i in range(60))
    state = {"diff_text": "\n".join(
        f"diff --git a/src/f{i}.py b/src/f{i}.py\n"
        f"--- a/src/f{i}.py\n+++ b/src/f{i}.py\n{body}" for i in range(4)),
        "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    c1 = [{"file": "src/f0.py", "line": 1, "severity": "major",
           "comment": "breaks the consumer contract", "evidence": "hunk"}]
    c2 = [{"file": "src/f1.py", "line": 2, "severity": "minor",
           "comment": "stale docstring", "evidence": "hunk"}]
    c3 = [{"file": "src/f2.py", "line": 3, "severity": "minor",
           "comment": "changed path has no test", "evidence": "hunk"}]
    llm = ScriptedLLM([
        Reply(blocks=[Block(type="text", text="prose, not json")]),  # planner
        contract(review_comments=c1),                                # lens 1
        contract(review_comments=c2),                                # lens 2
        contract(review_comments=c3),                                # lens 3
        verdicts_reply({"i": 0, "action": "keep"},                   # reducer
                       {"i": 1, "action": "keep"},
                       {"i": 2, "action": "keep"}),
    ])
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    plan_ev = next(trace.events("review_plan"))
    assert plan_ev["planner"] == "llm-fallback"
    assert plan_ev["lenses"] == ["logic", "behavior", "verification"]
    assert len(list(trace.events("agent_dispatch"))) == 3
    assert not llm._replies  # the whole script was consumed


def test_review_step_cap_applies_to_light_path(settings, trace, tmp_path,
                                               git_repo):
    """The 8-comment severity-ordered budget is a product cap, not ensemble
    mechanics — it applies to the light single pass too."""
    settings.review_ensemble = True
    many = ([{"file": "m.py", "line": i, "severity": "nit", "comment": f"n{i}",
              "evidence": "hunk"} for i in range(9)]
            + [{"file": "m.py", "line": 9, "severity": "major",
                "comment": "big", "evidence": "hunk"}])
    llm = ScriptedLLM([contract(review_comments=many)])
    state = {"diff_text": "diff --git a/mod_a.py b/mod_a.py\n"
                          "--- a/mod_a.py\n+++ b/mod_a.py\n+x = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    kept = result.outputs["review_comments"]
    assert len(kept) == 8 and kept[0]["severity"] == "major"


def test_zero_yield_lens_gets_one_retry(settings, trace, tmp_path):
    """A lens whose candidate list is empty is re-asked once (single lens, not
    a full ensemble re-run); the retry's candidates flow into the merge."""
    settings.ensemble_parallel = False
    settings.ensemble_zero_yield_retry = True
    llm = KeyedLLM({
        "first pass yielded zero": contract(items=[{"name": "late-find"}]),
        "Your assigned lens: a": contract(items=[]),
        "Your assigned lens: b": contract(items=[{"name": "from-b"}]),
        "verify-and-merge": verdicts_reply({"i": 0, "action": "keep"},
                                           {"i": 1, "action": "keep"}),
    })
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    assert any(True for _ in trace.events("lens_zero_yield_retry"))
    assert {i["name"] for i in output["items"]} == {"late-find", "from-b"}


def test_reducer_keeps_each_survivor_anchor_snippet(settings, trace, tmp_path):
    """`anchor_snippet` is bound to its candidate. The reducer drops, dup-merges and
    rewrites text in place, but never constructs a candidate — so a survivor must keep
    its OWN snippet, never inherit one from the item merged into it. If that ever
    changed, a comment would be anchored at another finding's code."""
    settings.review_ensemble = True
    a = {"file": "m.py", "line": 1, "anchor_snippet": "alpha = 1", "comment": "A"}
    b = {"file": "m.py", "line": 9, "anchor_snippet": "beta = 2", "comment": "B"}
    llm = ScriptedLLM([
        contract(items=[dict(a)]),
        contract(items=[dict(b)]),
        # keep both; rewrite the first's text — its location is unchanged
        verdicts_reply({"i": 0, "action": "keep", "comment": "A, reworded"},
                       {"i": 1, "action": "keep"}),
    ])
    out = _run(_ctx(settings, trace, tmp_path, llm=llm))
    items = {i["comment"][0]: i for i in out[1]["items"]}
    assert items["A"]["anchor_snippet"] == "alpha = 1"
    assert items["B"]["anchor_snippet"] == "beta = 2"


def test_dup_merge_does_not_move_a_snippet_onto_the_survivor(settings, trace, tmp_path):
    settings.review_ensemble = True
    llm = ScriptedLLM([
        contract(items=[{"file": "m.py", "line": 1, "anchor_snippet": "keep = 1",
                         "comment": "survivor"}]),
        contract(items=[{"file": "m.py", "line": 40, "anchor_snippet": "gone = 2",
                         "comment": "duplicate"}]),
        verdicts_reply({"i": 0, "action": "keep"},
                       {"i": 1, "action": "dup", "of": 0}),
    ])
    out = _run(_ctx(settings, trace, tmp_path, llm=llm))
    items = out[1]["items"]
    assert len(items) == 1
    assert items[0]["anchor_snippet"] == "keep = 1"   # not the dup's


def _truncated(text="{\"verdicts\": [{\"i\": 0, \"action\": \"ke"):
    """A reducer reply cut off mid-JSON by the output ceiling."""
    return Reply(blocks=[Block(type="text", text=text)], stop_reason="max_tokens")


def test_truncated_reducer_retries_verdict_only_instead_of_repairing(
        settings, trace, tmp_path):
    """A reply cut off at max_tokens is re-asked WITHOUT the rewrite field.

    The generic repair round cannot fix truncation — it re-sends the oversized draft
    asking to keep all substance under the same ceiling — so truncation must take a
    different path: drop `comment` from the contract so every candidate still gets a
    verdict. Measured live: 11 of 14 reductions died exactly here.
    """
    settings.review_ensemble = True
    llm = ScriptedLLM([
        contract(items=[{"name": "x"}, {"name": "y"}]),
        contract(items=[{"name": "z"}]),
        _truncated(),
        verdicts_reply({"i": 0, "action": "keep"},
                       {"i": 1, "action": "drop", "why": "misread"},
                       {"i": 2, "action": "keep"}),
    ])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    # the drop verdict was applied — i.e. adjudication survived the truncation
    assert [i["name"] for i in output["items"]] == ["x", "z"]
    assert "unverified union" not in output["summary"]
    assert next(trace.events("agent_ensemble"))["verified"] is True
    # the retry asked for verdicts only, and never ran the doomed repair round
    retry_system = llm.calls[-1]["system"]
    assert "optional self-contained rewrite" not in retry_system
    assert "Do NOT rewrite any text" in retry_system
    assert not any("Convert the draft" in c["system"] for c in llm.calls)


def test_truncated_reducer_is_traced_with_its_candidate_count(
        settings, trace, tmp_path):
    settings.review_ensemble = True
    llm = ScriptedLLM([
        contract(items=[{"name": "x"}, {"name": "y"}]),
        contract(items=[{"name": "z"}]),
        _truncated(),
        verdicts_reply({"i": 0, "action": "keep"}),
    ])
    _run(_ctx(settings, trace, tmp_path, llm=llm))
    event = next(trace.events("lens_reduce_truncated"))
    assert event["candidates"] == 3


def test_truncated_reducer_still_falls_open_when_the_retry_also_fails(
        settings, trace, tmp_path):
    """The retry is an extra chance, not a new way to lose findings."""
    settings.review_ensemble = True
    llm = ScriptedLLM([
        contract(items=[{"name": "x"}, {"name": "y"}]),
        contract(items=[{"name": "z"}]),
        _truncated(),
        _truncated("still cut off"),
    ])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    assert [i["name"] for i in output["items"]] == ["x", "y", "z"]
    assert "unverified union" in output["summary"]
    assert next(trace.events("agent_ensemble"))["verified"] is False


def test_unparseable_but_complete_reply_still_uses_the_repair_round(
        settings, trace, tmp_path):
    """Only truncation takes the retry path; prose still gets the original repair."""
    settings.review_ensemble = True
    llm = ScriptedLLM([
        contract(items=[{"name": "x"}]),
        contract(items=[{"name": "y"}]),
        Reply(blocks=[Block(type="text", text="prose, not JSON")]),
        verdicts_reply({"i": 0, "action": "keep"}, {"i": 1, "action": "keep"}),
    ])
    result, _ = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    assert any("Convert the draft" in c["system"] for c in llm.calls)
    assert not any(e for e in trace.events("lens_reduce_truncated"))


def test_reduction_archive_records_truncation_and_the_retry(
        settings, trace, tmp_path):
    """The archive is the only durable evidence of a reducer failure."""
    settings.review_ensemble = True
    llm = ScriptedLLM([
        contract(items=[{"name": "x"}]),
        contract(items=[{"name": "y"}]),
        _truncated(),
        verdicts_reply({"i": 0, "action": "keep"}, {"i": 1, "action": "keep"}),
    ])
    ctx = _ctx(settings, trace, tmp_path, llm=llm)
    _run(ctx)
    archive = json.loads((ctx.run_dir / "ensemble_t.step.json").read_text())
    assert archive["truncated"] is True
    assert archive["retry_reply"] is not None
    assert len(archive["candidates"]) == 2


def test_light_pass_carries_the_single_pass_protocol(settings, trace, tmp_path,
                                                     git_repo):
    """Light was the only reviewer surface running with no protocol at all, and
    measurement put that at ~70% of its anchored findings. The protocol is
    repo-neutral, so it rides the system guidance, not the evidence."""
    settings.review_ensemble = True
    llm = ScriptedLLM([contract(review_comments=[
        {"file": "mod_a.py", "line": 1, "severity": "minor",
         "comment": "c", "evidence": "hunk"}])])
    state = {"diff_text": "diff --git a/mod_a.py b/mod_a.py\n"
                          "--- a/mod_a.py\n+++ b/mod_a.py\n+x = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    rendered = json.dumps(llm.calls[0]["messages"])
    assert "Single-pass protocol" in rendered
    assert "Enumerate every changed semantic path" in rendered
    assert "exactly one consolidated review" in rendered.lower()


def test_zero_yield_light_pass_escalates_once_to_standard(settings, trace,
                                                          tmp_path, git_repo):
    """Silence is the one result the cheapest tier is least entitled to."""
    settings.review_ensemble = True
    found = [{"file": "mod_a.py", "line": 1, "severity": "major",
              "comment": "real finding", "evidence": "hunk"}]
    llm = ScriptedLLM([
        contract(review_comments=[]),        # light pass: nothing
        contract(review_comments=found),     # standard lens 1
        contract(review_comments=found),     # standard lens 2
        contract(review_comments=found),     # standard lens 3
        verdicts_reply({"i": 0, "action": "keep"}),
    ])
    state = {"diff_text": "diff --git a/mod_a.py b/mod_a.py\n"
                          "--- a/mod_a.py\n+++ b/mod_a.py\n+x = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    event = next(trace.events("review_depth_escalated"))
    assert (event["depth_from"], event["depth_to"]) == ("light", "standard")
    assert list(trace.events("agent_ensemble"))          # the ensemble did run
    assert result.outputs["review_plan"]["depth"] == "standard"
    assert [c["comment"] for c in result.outputs["review_comments"]] \
        == ["real finding"]


def test_light_pass_with_findings_does_not_escalate(settings, trace, tmp_path,
                                                    git_repo):
    settings.review_ensemble = True
    llm = ScriptedLLM([contract(review_comments=[
        {"file": "mod_a.py", "line": 1, "severity": "minor",
         "comment": "c", "evidence": "hunk"}])])
    state = {"diff_text": "diff --git a/mod_a.py b/mod_a.py\n"
                          "--- a/mod_a.py\n+++ b/mod_a.py\n+x = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert not list(trace.events("review_depth_escalated"))
    assert result.outputs["review_plan"]["depth"] == "light"


def test_zero_yield_escalation_can_be_switched_off(settings, trace, tmp_path,
                                                   git_repo):
    settings.review_ensemble = True
    settings.review_light_zero_yield_escalate = False
    llm = ScriptedLLM([contract(review_comments=[])])
    state = {"diff_text": "diff --git a/mod_a.py b/mod_a.py\n"
                          "--- a/mod_a.py\n+++ b/mod_a.py\n+x = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert not list(trace.events("review_depth_escalated"))
    assert result.outputs["review_plan"]["depth"] == "light"


def test_second_round_targets_uncovered_files(settings, trace, tmp_path,
                                              git_repo):
    """Coverage-driven second round (RFC q3): changed files with no comment
    and no findings line seed one extra pass; its non-duplicate comments and
    verification findings merge into the output."""
    settings.review_ensemble = True
    settings.review_deep_engine = True
    settings.review_depth = "full"
    settings.review_second_round = True
    c1 = [{"file": "a.py", "line": 1, "severity": "major",
           "comment": "central defect", "evidence": "a.py:1 `A = 1`"}]
    round2 = [{"file": "a.py", "line": 3, "severity": "minor",
               "comment": "duplicate-ish of the kept one",
               "evidence": "a.py:1 `A = 1`"},
              {"file": "c.py", "line": 7, "severity": "minor",
               "comment": "uncovered-file finding",
               "evidence": "c.py:7 `C = 1`"}]
    llm = ScriptedLLM([
        contract(review_comments=c1),                  # investigator
        contract(review_comments=[]),                  # adversary
        contract(review_comments=[]),                  # behavior
        contract(review_comments=[]),                  # verification
        verdicts_reply({"i": 0, "action": "keep"}),    # reduce
        contract(review_comments=round2,               # second round
                 findings=["[claim-verified] body claim holds: a.py:1"]),
    ])
    state = {"diff_text": ("diff --git a/a.py b/a.py\n+++ b/a.py\n"
                           "@@ -1,0 +1,1 @@\n+A = 1\n"
                           "diff --git a/c.py b/c.py\n+++ b/c.py\n"
                           "@@ -7,0 +7,1 @@\n+C = 1"),
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    ev = next(trace.events("review_second_round"))
    assert ev["uncovered"] == 1 and ev["added_comments"] == 1
    files = [c["file"] for c in result.outputs["review_comments"]]
    assert files.count("c.py") == 1
    assert files.count("a.py") == 1        # near-dup was filtered
    assert not llm._replies


def test_second_round_skips_when_everything_covered(settings, trace, tmp_path,
                                                    git_repo):
    """Full coverage (every changed file mentioned, claims checked) skips the
    round entirely — no extra LLM call."""
    settings.review_ensemble = True
    settings.review_deep_engine = True
    settings.review_depth = "full"
    settings.review_second_round = True
    c1 = [{"file": "a.py", "line": 1, "severity": "major",
           "comment": "covered", "evidence": "a.py:1 `A = 1`"}]
    llm = ScriptedLLM([
        contract(review_comments=c1,
                 findings=["[claim-verified] the body claim checks out"]),
        contract(review_comments=[]),
        contract(review_comments=[]),
        contract(review_comments=[]),
        verdicts_reply({"i": 0, "action": "keep"}),
    ])
    state = {"diff_text": "diff --git a/a.py b/a.py\n+++ b/a.py\n+A = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    ev = next(trace.events("review_second_round"))
    assert ev.get("skipped") == "no coverage holes"
    assert not llm._replies


def test_overflow_renders_verify_confirmed_tail_without_rich(settings, trace,
                                                             tmp_path,
                                                             git_repo):
    """A budget-cut comment the verify pass CONFIRMED renders in the overflow
    even when the review is not 'rich' (all minors) — wave-2 lost three
    verified findings to the rich-only gate."""
    settings.review_ensemble = True
    settings.review_deep_engine = True
    settings.review_depth = "standard"
    settings.review_verify_comments = True
    minors = [{"file": f"f{i}.py", "line": i + 1, "severity": "minor",
               "comment": f"minor {i}", "evidence": f"f{i}.py:{i + 1} `x`"}
              for i in range(9)]
    llm = ScriptedLLM(
        [contract(review_comments=minors),             # investigator
         contract(review_comments=[]),                 # adversary (v15:
                                                       # standard runs both
                                                       # deep passes)
         contract(review_comments=[]),                 # behavior
         verdicts_reply(*[{"i": k, "action": "keep"} for k in range(9)])]
        + [contract(verdict="confirmed")] * 9)         # verify fan-out
    state = {"diff_text": "diff --git a/f0.py b/f0.py\n+++ b/f0.py\n+x = 1",
             "task_spec": {"pr": 9}, "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    assert len(result.outputs["review_comments"]) == 8
    assert "Additional observations" in result.outputs["review_text"]
    assert not llm._replies


def test_docs_heavy_diff_swaps_in_the_docs_pass(settings, trace, tmp_path,
                                                git_repo):
    """A mid-size docs-only diff plans standard depth and runs the docs
    claims-audit pass instead of the code breadth lens."""
    settings.review_ensemble = True
    settings.review_deep_engine = True
    body = "\n".join(f"+doc line {i}" for i in range(120))
    diff = f"diff --git a/docs/big.md b/docs/big.md\n+++ b/docs/big.md\n{body}"
    llm = ScriptedLLM([
        contract(review_comments=[]),                  # investigator
        contract(review_comments=[]),                  # adversary (v15)
        contract(review_comments=[{"file": "docs/big.md", "line": 3,
                                   "severity": "minor",
                                   "comment": "claim does not hold",
                                   "evidence": "docs/big.md:3 `doc line 1`"}]),
        verdicts_reply({"i": 0, "action": "keep"}),
    ])
    state = {"diff_text": diff, "task_spec": {"pr": 9},
             "repo_path": str(git_repo)}
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("agent.review_diff").handler(
        _ctx(settings, trace, tmp_path, state, llm=llm)))
    assert result.ok, result.summary
    plan = next(trace.events("review_plan"))
    assert plan["depth"] == "standard"
    steps_run = [e["step"] for e in trace.events("agent_dispatch")]
    assert steps_run == ["agent.review_diff#investigator",
                         "agent.review_diff#adversary",
                         "agent.review_diff#docs"]
    assert not llm._replies


def test_validated_ledger_ranks_resolved_and_claims_first():
    """The rendered Validated section ranks [resolved]/[claim-*] confirmations
    ahead of mechanics [sweep] notes — arrival-order truncation was cutting
    exactly the resolved-thread confirmations the reader checks a post-fix
    review against.

    The cap was 14 and is now 6. 14 saturated on 9 of 10 items in BOTH
    measured arms, which makes it a quota being filled rather than a ceiling
    protecting the reader, and judges scored the result as burial: "buries the
    same insight under ~10 near-duplicate 'Validated'/'claim-refuted' log
    entries". Ranking is what this test pins; the cap rides along.
    """
    from infermatrix_copilot.engine.steps.review.utils import (
        _review_summary_parts,
    )
    findings = ([f"[sweep] mechanics note {i}" for i in range(20)]
                + ["[resolved] prior concern X: fixed at f.py:3 `guard`",
                   "[claim-verified] body claim holds: g.py:9 `line`"])
    parts = _review_summary_parts({"findings": findings, "review_comments": []})
    validated = next(p for p in parts if p.startswith("**Validated:**"))
    lines = validated.splitlines()[1:]
    assert len(lines) == 6
    assert "[resolved]" in lines[0]
    assert "[claim-verified]" in lines[1]


def test_render_falls_back_to_declared_line_for_unresolved_anchor():
    """A repo-side comment whose anchor the diff index cannot corroborate
    renders with its declared line marked approximate — `file:?` was measured
    as a judge penalty on findings that were otherwise correct."""
    from infermatrix_copilot.engine.steps.review.utils import (
        _render_review_md,
    )
    md = _render_review_md({"review_comments": [
        {"file": "ci.yml", "_declared_line": 19, "_anchor_unverified": True,
         "severity": "minor", "comment": "lane duplicates a bucket",
         "evidence": "ci.yml:19 `pytest ...`"}]})
    assert "`ci.yml:~19`" in md


def test_lens_backend_member_parses_the_role_split_map(settings):
    """review_lens_backends routes named passes to a harness provider;
    unmapped passes return None (normal backend)."""
    from infermatrix_copilot.engine.agent_runtime.ensemble import (
        lens_backend_member,
    )
    settings.review_lens_backends = {"investigator": "cursor:composer-2.5",
                                     "docs": "cursor"}
    settings.strict_backend_model = "composer-2.5"
    m = lens_backend_member(settings, "investigator")
    assert m.provider == "cursor" and m.model == "composer-2.5"
    m2 = lens_backend_member(settings, "docs")     # bare provider id
    assert m2.provider == "cursor" and m2.model == "composer-2.5"
    assert lens_backend_member(settings, "adversary") is None
    settings.review_lens_backends = {}
    assert lens_backend_member(settings, "investigator") is None


def test_zero_yield_retry_keeps_the_seat_routing(settings, trace, tmp_path,
                                                 git_repo, monkeypatch):
    """A routed seat that yields nothing must be RETRIED ON THE SAME ROUTE.
    Dropping the override silently moved the seat onto the default backend,
    so an arm labelled "Fable in the adversary seat" measured DeepSeek there
    (2026-08-16: a model-quota exhaustion degraded 18 of 28 holdout seats
    this way while every run still reported success)."""
    from infermatrix_copilot.engine.agent_runtime import ensemble as ens

    settings.ensemble_zero_yield_retry = True
    settings.review_lens_backends = {"b": "cursor:composer-2.5"}
    seen: list = []

    async def fake_step(ctx, **kw):
        seen.append((kw.get("step_name"), kw.get("harness_member"),
                     kw.get("model_override")))
        from infermatrix_copilot.engine.step import StepResult
        return StepResult(True, summary="ok"), {"status": "success",
                                                "items": []}

    monkeypatch.setattr(ens, "run_agent_step", fake_step)
    asyncio.run(ens.run_agent_step_ensemble(
        _ctx(settings, trace, tmp_path, {"task_spec": {"pr": 1}},
             llm=ScriptedLLM([])),
        step_name="t.step", purpose="p", evidence={"e": "x"},
        lenses=[{"name": "b", "focus": "look at B"}], merge_key="items",
        output_extension={"items": "list"}))
    retry = [s for s in seen if s[0].endswith("/retry")]
    assert retry, "zero-yield retry did not run"
    assert retry[0][1] is not None, "retry lost the harness member"
    assert retry[0][2] == "composer-2.5", "retry lost the model override"


def test_outcome_blocked_distinguishes_dead_seat_from_quiet_seat():
    """A transport failure arrives as a contract-shaped `blocked` with zero
    counters; a model that genuinely found nothing still shows tool work."""
    from infermatrix_copilot.engine.agent_runtime.ensemble import outcome_blocked
    from infermatrix_copilot.engine.step import StepResult
    ok = StepResult(True, summary="ok")
    assert outcome_blocked(ok, {"status": "blocked", "_tools_used": []})
    assert not outcome_blocked(ok, {"status": "success",
                                    "_tools_used": ["grep", "read_file"]})
    assert not outcome_blocked(ok, {"status": "blocked",
                                    "_tools_used": ["grep"]})


# vllm-project/vllm-omni#6968, verbatim: the `[resolved]` line that the old
# prose promotion published as a P2 even though it says it is not re-raised.
_DECLINED_RESOLVED = (
    "[resolved] TI2I silent guidance-mode flip on co-batch: request-unique key "
    "(`_boogu_batch_compatibility_key` → (\"boogu_image\", \"ti2i\", request_id)) "
    "+ RuntimeError fail-closed at pipeline_boogu_image.py:667; residual: TI2I "
    "stays batch=1 until RequestBatchSamplingParamsKey adds "
    "`guidance_scale_2_provided` (author follow-up) — not re-raised.")


def test_resolved_line_is_never_mined_into_a_comment(settings, trace, tmp_path):
    """#155: a `[resolved]` findings line is the review's record, not an ask.
    The prose promotion that turned residual-sounding lines into comments is
    gone, and the coverage editor no longer receives `[resolved]` lines as raw
    material, so the #6968 line cannot become a published finding."""
    from types import SimpleNamespace

    from infermatrix_copilot.engine.steps.review import refinement

    assert not hasattr(refinement, "_promote_resolved_residuals")
    seen: list[str] = []

    class _LLM:
        available = True

        def create(self, *, messages, **_kw):
            seen.append(messages[0]["content"])
            return SimpleNamespace(text='{"additions": []}')

    ctx = _ctx(settings, trace, tmp_path, llm=_LLM())
    out = asyncio.run(refinement._promote_uncovered(
        ctx, {"review_comments": [],
              "findings": [_DECLINED_RESOLVED,
                           "[validated] pipeline_boogu_image.py:690 image_gs "
                           "forced to 1.0 without a reference"]}, {}))
    assert out["review_comments"] == []
    assert seen and "TI2I silent guidance-mode flip" not in seen[0]
    assert "image_gs forced to 1.0" in seen[0]


def test_empty_final_lens_is_retried_not_discarded(settings, trace, tmp_path):
    """A pass that burns its ceiling and returns no contract-conformant final
    used to fall through BOTH retry paths and be dropped silently — five
    whole passes and ~2.1M input tokens of investigation lost on one
    measured holdout."""
    settings.ensemble_zero_yield_retry = True
    llm = ScriptedLLM([
        Reply(blocks=[Block(type="text", text="")]),      # lens a: no final
        contract(items=[{"name": "recovered"}]),           # lens a: retry
        contract(items=[{"name": "b"}]),                   # lens b
        verdicts_reply({"i": 0, "action": "keep"},
                       {"i": 1, "action": "keep"}),
    ])
    result, output = _run(_ctx(settings, trace, tmp_path, llm=llm))
    assert result.ok
    assert {i["name"] for i in output["items"]} == {"recovered", "b"}
    assert any(e for e in trace.events("lens_zero_yield_retry"))


# -- a dead routed member must never sink the ensemble (PR5) ------------------
# A member dies in TWO shapes: raw-API members RAISE (PermissionDeniedError on
# a revoked key), harness transports convert a dead session into an empty typed
# outcome ("a dead harness is an outcome"). The first pass guarded only the
# raise; the zero-yield retry guarded neither, so a 403 seat raised through
# asyncio.gather and failed the whole review step.

def _routed_run(settings, trace, tmp_path, fake_step, monkeypatch):
    from infermatrix_copilot.engine.agent_runtime import ensemble as ens

    settings.ensemble_zero_yield_retry = True
    settings.review_lens_backends = {"b": "cursor:composer-2.5"}
    monkeypatch.setattr(ens, "run_agent_step", fake_step)
    return asyncio.run(ens.run_agent_step_ensemble(
        _ctx(settings, trace, tmp_path, {"task_spec": {"pr": 1}},
             llm=ScriptedLLM([])),
        step_name="t.step", purpose="p", evidence={"e": "x"},
        lenses=[{"name": "b", "focus": "look at B"}], merge_key="items",
        output_extension={"items": "list"}))


def _ok(items=()):
    from infermatrix_copilot.engine.step import StepResult
    return StepResult(True, summary="ok"), {"status": "success",
                                            "items": list(items),
                                            "_tools_used": ["grep"]}


def _blocked():
    """The dead-harness shape: contract-shaped, zero counters, no raise."""
    from infermatrix_copilot.engine.step import StepResult
    return StepResult(True, summary="ok"), {"status": "blocked",
                                            "items": [], "_tools_used": []}


@pytest.mark.parametrize("phase", ["first", "retry"])
@pytest.mark.parametrize("kind", ["pending", "unfinished", "forbidden", "sandbox", "host_timeout"])
def test_unresolved_member_never_dispatches_tier(settings, trace, tmp_path,
                                                monkeypatch, phase, kind):
    error = RuntimeError("original native outcome requires reconciliation")
    if kind == "pending":
        error.native_outcome_pending = True
    elif kind == "unfinished":
        error.native_call_sent = True
        error.native_call_completed = False
    elif kind == "forbidden":
        error.allow_fallback = False
    elif kind == "sandbox":
        from infermatrix_copilot.sandbox.base import SandboxPending
        error = SandboxPending("original execution unresolved")
    else:
        from infermatrix_copilot.providers.json_session import JSONSessionError
        error = JSONSessionError("legacy host timeout", timeout=True)
    calls = []

    async def fake_step(ctx, **kw):
        calls.append(kw)
        if phase == "first" or kw["step_name"].endswith("/retry"):
            raise error
        return _ok()

    with pytest.raises(RuntimeError) as caught:
        _routed_run(settings, trace, tmp_path, fake_step, monkeypatch)
    assert caught.value is error
    assert len(calls) == (1 if phase == "first" else 2)
    assert all(call.get("model_override") == "composer-2.5" for call in calls)
    assert not list(trace.events("moa_member_fallback"))


def test_first_pass_typed_failure_falls_back_to_tier(settings, trace, tmp_path,
                                                     git_repo, monkeypatch):
    """The ACTUAL dsh failure shape — a typed non-OK, not an exception. The
    old guard only caught raises, so this walked on with a dead seat."""
    seen: list = []

    async def fake_step(ctx, **kw):
        seen.append((kw.get("step_name"), kw.get("model_override")))
        if kw.get("model_override"):        # the routed member is dead
            return _blocked()
        return _ok()

    _routed_run(settings, trace, tmp_path, fake_step, monkeypatch)
    assert any(s[0].endswith("/tier") for s in seen), (
        "a typed non-OK from a routed member must fall back to the tier model")
    ev = list(trace.events("moa_member_fallback"))
    assert ev and ev[0].get("phase") == "first"
    assert ev[0].get("requested") == "composer-2.5"
    # CONCRETE, not the word "tier": a reader must be able to see what actually
    # served the seat, and whether the fallback changed anything at all
    assert ev[0].get("effective") not in ("", "tier", None)
    assert ev[0].get("same_model") is False


def test_retry_exception_is_guarded_not_raised(settings, trace, tmp_path,
                                               git_repo, monkeypatch):
    """The codex-run failure: the member survived the first pass, yielded
    nothing, and the zero-yield retry re-asked the same seat — which 403'd and
    raised through asyncio.gather, failing the whole step."""
    async def fake_step(ctx, **kw):
        if str(kw.get("step_name", "")).endswith("/retry") \
                and kw.get("model_override"):
            raise RuntimeError("PermissionDeniedError: 403 Unpurchased")
        if str(kw.get("step_name", "")).endswith("/retry/tier"):
            return _ok()
        return _ok()                        # ran fine, but zero candidates

    _routed_run(settings, trace, tmp_path, fake_step, monkeypatch)
    ev = [e for e in trace.events("moa_member_fallback")
          if e.get("phase") == "retry"]
    assert ev, "a raise on the retry must fall back, not sink the ensemble"


def test_retry_typed_failure_is_guarded_too(settings, trace, tmp_path,
                                            git_repo, monkeypatch):
    """Same as above in the harness shape: non-OK instead of a raise."""
    async def fake_step(ctx, **kw):
        name = str(kw.get("step_name", ""))
        if name.endswith("/retry") and kw.get("model_override"):
            return _blocked()
        if name.endswith("/retry/tier"):
            return _ok()
        return _ok()

    _routed_run(settings, trace, tmp_path, fake_step, monkeypatch)
    ev = [e for e in trace.events("moa_member_fallback")
          if e.get("phase") == "retry"]
    assert ev, "a typed non-OK on the retry must fall back to tier"


def test_a_quiet_seat_keeps_its_route(settings, trace, tmp_path, git_repo,
                                      monkeypatch):
    """The other half of the invariant: a seat that RAN and simply found
    nothing is not dead, so its retry must stay on the routed member."""
    seen: list = []

    async def fake_step(ctx, **kw):
        seen.append((kw.get("step_name"), kw.get("model_override")))
        return _ok()                        # success, zero candidates, tools used

    _routed_run(settings, trace, tmp_path, fake_step, monkeypatch)
    retry = [s for s in seen if str(s[0]).endswith("/retry")]
    assert retry and retry[0][1] == "composer-2.5", (
        "a quiet seat must keep its route — dropping it silently is how an "
        "arm gets mislabelled")
    assert not list(trace.events("moa_member_fallback")), (
        "a quiet seat is not a dead member and must not record a fallback")


def test_a_dead_member_and_a_dead_tier_stop_after_two_calls(
        settings, trace, tmp_path, git_repo, monkeypatch):
    """When the tier fallback ALSO did not run there is nothing left to fall
    back to: re-asking it duplicates cost and buries the typed failure. A tier
    that RAN and merely found nothing still earns its re-ask (covered above)."""
    seen: list = []

    async def fake_step(ctx, **kw):
        seen.append(kw.get("step_name"))
        return _blocked()                   # member dead, then tier dead too

    _routed_run(settings, trace, tmp_path, fake_step, monkeypatch)
    assert len(seen) == 2, (
        f"expected the routed pass + one tier fallback, got {seen}")
    assert seen[1].endswith("/tier")
    assert not any(str(s).endswith("/retry") for s in seen)


def test_a_truncated_seat_keeps_its_route(settings, trace, tmp_path, git_repo,
                                          monkeypatch):
    """A seat that RAN its tool loop and then blew the reply ceiling returns
    (StepResult(False), {}) from runner.py — `outcome_blocked` says True for
    that, but the seat is ALIVE. Routing on that signal rerouted a merely
    truncated seat onto the tier model, silently changing the arm's
    configuration: the exact 2026-08-16 defect the invariant forbids."""
    from infermatrix_copilot.engine.step import FailureKind, StepResult
    seen: list = []

    async def fake_step(ctx, **kw):
        seen.append((kw.get("step_name"), kw.get("model_override")))
        # the empty-final shape: ran, no contract-conformant output
        return StepResult(False, FailureKind.RETRYABLE, "no output"), {}

    _routed_run(settings, trace, tmp_path, fake_step, monkeypatch)
    assert not any(str(s[0]).endswith("/tier") for s in seen), (
        "a truncated seat must NOT be rerouted to the tier model")
    retry = [s for s in seen if str(s[0]).endswith("/retry")]
    assert retry and retry[0][1] == "composer-2.5", (
        "the re-ask must stay on the seat's own route")
    assert not list(trace.events("moa_member_fallback"))


def test_an_unrouted_dead_tier_is_not_re_asked(settings, trace, tmp_path,
                                               git_repo, monkeypatch):
    """No route was ever configured and the tier itself did not run: there is
    nothing to fall back to, so re-asking it only duplicates cost and buries
    the typed failure."""
    settings.ensemble_zero_yield_retry = True
    settings.review_lens_backends = {}           # unrouted lens
    seen: list = []

    async def fake_step(ctx, **kw):
        seen.append(kw.get("step_name"))
        return _blocked()

    from infermatrix_copilot.engine.agent_runtime import ensemble as ens
    monkeypatch.setattr(ens, "run_agent_step", fake_step)
    asyncio.run(ens.run_agent_step_ensemble(
        _ctx(settings, trace, tmp_path, {"task_spec": {"pr": 1}},
             llm=ScriptedLLM([])),
        step_name="t.step", purpose="p", evidence={"e": "x"},
        lenses=[{"name": "b", "focus": "look at B"}], merge_key="items",
        output_extension={"items": "list"}))
    assert len(seen) == 1, f"a dead unrouted tier must not be re-asked: {seen}"
