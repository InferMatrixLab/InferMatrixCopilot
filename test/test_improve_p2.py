"""Meta-improvement engine P2: the outcome adapter contract, the curated
gold set, the judge runner, statistics, the two adapters, Tier 2 forensics
(coverage matrix + stage-of-loss attribution) and the meta-benchmark."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from infermatrix_copilot.improve import forensics, gold as goldmod, meta, stats
from infermatrix_copilot.improve.adapters import FindingLabel, Match, load_adapter, scores_from
from infermatrix_copilot.improve.adapters.rb_review import RbReviewAdapter, classify
from infermatrix_copilot.improve.adapters.review_eval import ReviewEvalAdapter
from infermatrix_copilot.improve.judges import JudgeError, JudgeSpec, parse_verdict, run_judge
from infermatrix_copilot.improve.reader import units_between
from infermatrix_copilot.trace_store import TraceStore, bind_store, trace_context

from test_improve_p1 import CLEAN, T0, Clock, _call, _decision, _store, _tool, _unit  # noqa: E402
from execution_helpers import contained_codex  # noqa: F401

REVIEW = ("## Review\n\nvllm/config.py:66 [major] — trust_remote_code should default to False; "
          "passing it through from CLI overrides is a bug.\n\nAlso the kernels version check is dead code.")


def _gt(tmp_path, item="demo#1@abc"):
    gt = tmp_path / "gt"
    (gt / "curated").mkdir(parents=True)
    raw = [{"author": "h", "body": "this looks like a bug. trust_remote_code must default to False", "line": 66,
            "path": "vllm/config.py"},
           {"author": "h", "body": "Please add a test for the fallback branch.", "line": 80, "path": "tests/test_x.py"}]
    (gt / "pr1.inline.json").write_text(json.dumps(raw))
    return gt, raw


# -- gold -----------------------------------------------------------------------------------

def test_gold_draft_load_and_id_discipline(tmp_path):
    gt, raw = _gt(tmp_path)
    path = goldmod.write_draft(gt, "demo#1@abc")
    draft = json.loads(path.read_text())
    assert draft["status"] == "draft" and len(draft["entries"]) == 2
    assert draft["entries"][0]["concern"] == "this looks like a bug."           # first sentence
    assert goldmod.gold_for_item(gt, "demo#1@abc") is None                      # drafts are never used
    with pytest.raises(FileExistsError):
        goldmod.write_draft(gt, "demo#1@abc")
    draft["status"] = "curated"
    draft["entries"][0]["concern"] = "trust_remote_code must default to False"  # reworded: id must change
    path.write_text(json.dumps(draft))
    with pytest.raises(ValueError, match="does not match"):
        goldmod.load_gold(path)
    for e in draft["entries"]:
        e["gold_id"] = goldmod.gold_id("demo#1@abc", e["path"], e["concern"])
    path.write_text(json.dumps(draft))
    gold = goldmod.gold_for_item(gt, "demo#1@abc")
    assert gold is not None and gold.status == "curated" and len(gold.entries) == 2 and len(gold.version) == 64
    # the file applies to its own item only: repository + PR, and the head when it names one
    assert goldmod.gold_for_item(gt, "demo#1@abc") is not None
    assert goldmod.gold_for_item(gt, "demo#1@zzz") is None                # another head of the same PR
    assert goldmod.gold_for_item(gt, "other#1@abc") is None               # PR numbers are repository-local
    assert goldmod.same_item("demo#1", "demo#1@abc") and not goldmod.same_item("demo#2", "demo#1")
    assert not goldmod.same_item("demo#1@abc", "demo#1@") and not goldmod.same_item("demo#1@abc", "demo#1")
    assert goldmod.gold_for_item(gt, "demo#1") is None                    # head-specific gold, unknown head
    draft["entries"].append(dict(draft["entries"][0]))
    path.write_text(json.dumps(draft))
    with pytest.raises(ValueError, match="duplicate"):
        goldmod.load_gold(path)


# -- judges + stats ---------------------------------------------------------------------------

def test_judge_runner_paths_and_verdict_parsing(tmp_path, contained_codex):
    assert parse_verdict('prose {"status": "hit", "quote": "x"} trailing')["status"] == "hit"
    with pytest.raises(JudgeError):
        parse_verdict("no json here")
    calls = []

    class Gov:
        def reserve_judge_call(self):
            calls.append("reserve")
            return "2027-W03|judge|abc"

        def settle_judge_call(self, token=None):
            calls.append("settle")
            assert token == "2027-W03|judge|abc"                 # settled against the week-pinned token

    def fake_run(argv, **kw):
        assert argv[0] == "cursor-agent" and "--mode" in argv and "ask" in argv
        stream = json.dumps({"type": "init", "model": "m"}) + "\n" + json.dumps(
            {"type": "result", "result": '{"status": "miss", "quote": ""}', "usage": {"inputTokens": 5, "outputTokens": 2}})
        return SimpleNamespace(returncode=0, stdout=stream, stderr="")

    store = TraceStore(tmp_path / "t", environ={})
    with bind_store(store), trace_context(run_id="j", unit_id="u"):
        verdict = run_judge(JudgeSpec("cli", "gpt-5.6-sol", provider="cursor"), system="s", prompt="p",
                            governor=Gov(), runner=fake_run)
    assert verdict["status"] == "miss" and calls == ["reserve", "settle"]
    rec = store.query(kind="model_call", unit_id="u")[0]
    assert rec["model"]["provider"] == "cursor" and rec["result"]["usd"] == 0.0 and rec["usage"]["input_tokens"] == 5

    def tool_using(argv, **kw):
        stream = json.dumps({"type": "tool_call", "name": "read_file"}) + "\n" + json.dumps(
            {"type": "result", "result": '{"status": "hit"}'})
        return SimpleNamespace(returncode=0, stdout=stream, stderr="")
    with pytest.raises(JudgeError, match="tool calls"):
        run_judge(JudgeSpec("cli", "m", provider="cursor"), system="", prompt="p", runner=tool_using)

    def nested_tool_use(argv, **kw):                     # the assistant-message form of a tool call
        stream = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "read_file", "input": {"path": "gt/pr1.json"}}]}}) + "\n" + json.dumps(
            {"type": "result", "result": '{"status": "hit", "quote": "q"}'})
        return SimpleNamespace(returncode=0, stdout=stream, stderr="")
    with pytest.raises(JudgeError, match="tool_use"):
        run_judge(JudgeSpec("cli", "m", provider="cursor"), system="", prompt="p", runner=nested_tool_use)

    def codex_ok(argv, **kw):
        assert "--skip-git-repo-check" in argv and "-C" in argv
        stream = json.dumps({"type": "item.completed", "item": {"item_type": "agent_message",
                                                                 "text": '{"status": "miss", "quote": ""}'}}) + '\n' + json.dumps({'type':'turn.completed'})
        return SimpleNamespace(returncode=0, stdout=stream, stderr="")
    contained_codex.runner = codex_ok
    assert run_judge(JudgeSpec("cli", "gpt-6", provider="codex"), system="", prompt="p")["status"] == "miss"

    def codex_tooling(argv, **kw):
        stream = json.dumps({"type": "item.completed", "item": {"item_type": "command_execution", "command": "cat gt"}}) + "\n" + \
            json.dumps({"type": "item.completed", "item": {"item_type": "agent_message", "text": '{"status": "hit"}'}}) + '\n' + json.dumps({'type':'turn.completed'})
        return SimpleNamespace(returncode=0, stdout=stream, stderr="")
    contained_codex.runner = codex_tooling
    with pytest.raises(JudgeError, match="command_execution"):
        run_judge(JudgeSpec("cli", "gpt-6", provider="codex"), system="", prompt="p")

    class FakeLLM:
        def create(self, **kw):
            return SimpleNamespace(blocks=[SimpleNamespace(type="text", text='{"status": "hit", "quote": "q"}')])
    assert run_judge(JudgeSpec("api", "claude-sonnet-5"), system="s", prompt="p", llm=FakeLLM())["status"] == "hit"
    with pytest.raises(JudgeError, match="needs an LLM"):
        run_judge(JudgeSpec("api", "m"), system="s", prompt="p")


def test_zcode_judge_spec_parsing():
    from infermatrix_copilot.improve.judges import judge_spec_from

    spec = judge_spec_from(SimpleNamespace(improve_judge="cli:zcode:GLM-5.3"))
    assert spec == JudgeSpec("cli", "GLM-5.3", provider="zcode")
    assert judge_spec_from(SimpleNamespace(improve_judge="")) is None
    with pytest.raises(JudgeError, match="unknown CLI judge provider"):
        judge_spec_from(SimpleNamespace(improve_judge="cli:cursor-agent:m"))
    with pytest.raises(JudgeError, match="improve_judge must be"):
        judge_spec_from(SimpleNamespace(improve_judge="zcode:GLM-5.3"))


def test_zcode_judge_delegates_to_the_production_transport(tmp_path, monkeypatch):
    """cli:zcode judging goes through ZCodeTransport.complete — the same
    tool-less one-shot the production kb gate uses — governed by the
    judge-call envelope and recorded like any CLI judge."""
    from infermatrix_copilot.providers import registry

    calls = []

    class Gov:
        def reserve_judge_call(self):
            calls.append("reserve")
            return "2027-W03|judge|z"

        def settle_judge_call(self, token=None):
            calls.append(("settle", token))

    class Transport:
        def __init__(self, reply):
            self.reply = reply
            self.kwargs = None

        def complete(self, **kw):
            self.kwargs = kw
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

    ok = SimpleNamespace(blocks=[SimpleNamespace(type="text", text='{"status": "hit", "quote": "q"}')],
                         usage={"input_tokens": 7, "output_tokens": 3}, model="GLM-5.3")
    transport = Transport(ok)
    monkeypatch.setattr(registry, "transport_for_id", lambda settings, pid: transport)

    store = TraceStore(tmp_path / "t", environ={})
    with bind_store(store), trace_context(run_id="jz", unit_id="uz"):
        verdict = run_judge(JudgeSpec("cli", "GLM-5.3", provider="zcode"), system="s", prompt="p",
                            governor=Gov())
    assert verdict["status"] == "hit"
    assert transport.kwargs == {"system": "s", "messages": [{"role": "user", "content": "p"}],
                                "model": "GLM-5.3", "role": "judge"}
    assert calls == ["reserve", ("settle", "2027-W03|judge|z")]
    rec = store.query(kind="model_call", unit_id="uz")[0]
    assert rec["model"]["provider"] == "zcode" and rec["model"]["served_model"] == "GLM-5.3"
    assert rec["result"]["usd"] == 0.0 and rec["result"]["subscription"] is True
    assert rec["usage"]["input_tokens"] == 7 and not rec["error"]

    down = Transport(RuntimeError("zcode CLI is not installed"))
    monkeypatch.setattr(registry, "transport_for_id", lambda settings, pid: down)
    with pytest.raises(JudgeError, match="zcode judge unavailable"):
        run_judge(JudgeSpec("cli", "GLM-5.3", provider="zcode"), system="s", prompt="p", governor=Gov())
    assert calls[-1] == ("settle", "2027-W03|judge|z")   # a failed call still settles

    empty = Transport(SimpleNamespace(blocks=[], usage={}, model="GLM-5.3"))
    monkeypatch.setattr(registry, "transport_for_id", lambda settings, pid: empty)
    with pytest.raises(JudgeError, match="empty zcode judge reply"):
        run_judge(JudgeSpec("cli", "GLM-5.3", provider="zcode"), system="s", prompt="p", governor=None)


def test_zcode_judge_serves_the_trusted_kb_gate(monkeypatch, tmp_path):
    """End to end: the trusted driver's Gateway over a cli:zcode judge spec
    drives the production gate (judge_block + consistency) — the
    experiment-internal gate judge is the production instrument, with the
    ModelRole the gate records naming the zcode backend."""
    import json as _json

    from infermatrix_copilot.improve.drivers import Gateway
    from infermatrix_copilot.improve.judges import judge_spec_from
    from infermatrix_copilot.kb_service import gate as kb_gate
    from infermatrix_copilot.kb_service.models import ModelRole
    from infermatrix_copilot.providers import registry
    from test_kb_intake_gate import PAGE, Op, _rule, _tree

    class Transport:
        def complete(self, *, system, messages, model, role, **kw):
            prompt = messages[0]["content"]
            if '"directory"' in prompt:                      # the consistency call
                return SimpleNamespace(blocks=[SimpleNamespace(type="text", text='{"verdict": "consistent", "conflicts": []}')],
                                       usage={}, model=model)
            payload = _json.loads(prompt.split("<untrusted_data>\n", 1)[1].rsplit("\n</untrusted_data>", 1)[0])
            dims = {d: "yes" for d in payload["dimensions_to_answer"]}
            return SimpleNamespace(blocks=[SimpleNamespace(type="text", text=_json.dumps(
                {"dimensions": dims, "reasons": {}}))], usage={}, model=model)

    monkeypatch.setattr(registry, "transport_for_id", lambda settings, pid: Transport())
    spec = judge_spec_from(SimpleNamespace(improve_judge="cli:zcode:GLM-5.3"))
    gateway = Gateway(None, spec, None)
    judge = ModelRole("judge", spec.provider, spec.model)

    from infermatrix_copilot.kb_service.gate import changes_between
    from infermatrix_copilot.knowledge_service.ops import apply_operations

    base = _tree()
    add = [Op("add", PAGE, "DEMO-2a", _rule("DEMO-2a", "PR #11"))]
    head = {**base, **apply_operations(base, add, release="v1", today="2026-09-28").files}
    decision = kb_gate.run_gate(base=base, head=head, changes=changes_between(base, head),
                                external_texts={}, evidence=[{"source_reference": "PR #11"}],
                                gateway=gateway, judge=judge, release="v1", repo_dir="repos/demo")
    assert decision.status == "pass"
    assert all(b.model == "GLM-5.3" for b in decision.blocks)


def test_paired_statistics_labels_power_and_kappa():
    r = stats.paired({f"i{k}": [0.1, 0.12] for k in range(10)}, "recall")
    assert r.n_items == 10 and r.n_verdicts == 20 and abs(r.mean - 0.11) < 1e-9 and r.lo > 0
    assert stats.label(r, n_required=8) == "supported"
    assert stats.label(r, n_required=12) == "underpowered"
    assert stats.label(stats.paired({f"i{k}": [(-1) ** k * 0.1] for k in range(10)}), n_required=8) == "neutral"
    down = stats.paired({f"i{k}": [-0.1 - 0.001 * k] for k in range(10)})
    assert stats.label(down, n_required=8) == "refuted" and stats.label(down, n_required=8, direction="lower") == "supported"
    crossing = stats.paired({f"i{k}": [(-1) ** k * 0.1] for k in range(10)})
    assert stats.label(crossing, n_required=8, direction="lower") == "neutral"     # mirrored, still crossing zero
    assert stats.label(r, n_required=8, direction="lower") == "refuted"           # a rise is bad when lower is better
    assert stats.label(stats.paired({"a": [0.1], "b": [0.2]}), n_required=8) == "invalid"
    assert stats.label(r, n_required=8, invalid=True) == "invalid"
    assert stats.items_required(0.13, 0.07) == 28 and stats.items_required(0.01, 0.5) == stats.MIN_ITEMS
    with pytest.raises(ValueError):
        stats.items_required(0.1, 0)
    assert stats.cohen_kappa(["S2", "S2", "S5"], ["S2", "S2", "S5"]) == 1.0
    assert abs(stats.cohen_kappa(["S2", "S5", "S2", "S5"], ["S2", "S5", "S5", "S2"])) < 1e-9
    assert stats.t95(1) == 12.706 and stats.t95(100) == 1.96


# -- the eval adapter ----------------------------------------------------------------------------

def _curated(gt, item="demo#1@abc"):
    path = goldmod.write_draft(gt, item)
    d = json.loads(path.read_text())
    d["status"] = "curated"
    path.write_text(json.dumps(d))
    return goldmod.load_gold(path)


def test_review_eval_adapter_imports_verdicts_matches_gold_and_derives_scores(tmp_path):
    gt, _ = _gt(tmp_path)
    gold = _curated(gt)
    store = _store(tmp_path)
    _unit(store, "arm1:review", calls=[_call()], tools=[_tool()],
          decisions=[{"outputs": {"review": REVIEW}, "result": {"status": "ok", "findings": [{"id": "f1"}, {"id": "f2"}]}}])
    unit = units_between(store, T0, T0 + 10_000)["arm1:review"]
    judgments = tmp_path / "judgments" / "goal_x"
    judgments.mkdir(parents=True)
    for rep, (mine, theirs, winner) in enumerate([((0.8, 0.7, 0.9), (0.6, 0.9, 0.5), "X"), ((0.6, 0.7, 0.8), (0.7, 0.8, 0.5), "Y")], 1):
        (judgments / f"pr1.r{rep}.json").write_text(json.dumps({
            "x": dict(zip(("recall", "precision", "actionability"), mine)), "y": dict(zip(("recall", "precision", "actionability"), theirs)),
            "winner": winner, "margin": "slight", "_roles": {"arm": "copilot_v1", "baseline": "opus"},
            "_blinding": {"X": "copilot_v1", "Y": "opus"}, "_judge_resolved_model": "sonnet"}))
    (judgments / "pr1.r3.json").write_text(json.dumps({"x": {}, "y": {}, "_roles": {"arm": "other"}, "_blinding": {"X": "other", "Y": "opus"}}))

    answers = iter(['{"status": "hit", "quote": "trust_remote_code should default to False"}'] * 3   # entry 1
                   + ['{"status": "hit", "quote": "not in the review at all"}', '{"status": "miss"}', '{"status": "miss"}'])  # entry 2

    class FakeLLM:
        def create(self, **kw):
            return SimpleNamespace(blocks=[SimpleNamespace(type="text", text=next(answers))])

    adapter = ReviewEvalAdapter(gt_dir=gt, judgments_dir=tmp_path / "judgments", arm="copilot_v1",
                                judge=JudgeSpec("api", "sonnet"), llm=FakeLLM())
    assert adapter.gold("demo#1@abc") == gold and adapter.gold("demo#9@x") is None
    assert adapter.collect_judge_verdicts(unit, store) == 2 and adapter.collect_judge_verdicts(unit, store) == 0  # idempotent
    assert adapter.gold_match(unit, gold, store) == 2 and adapter.gold_match(unit, gold, store) == 0
    assert adapter.inconclusive == []
    outcome = adapter.fetch(unit, store)
    matches = adapter.match(unit, gold, outcome)
    assert [m.status for m in matches] == ["hit", "miss"]                        # the fake quote is a miss
    assert all(m.evidence for m in matches)
    verdicts = outcome.of_type("gold_match")
    assert verdicts[1]["result"]["votes"][0]["quote"].startswith("[quote not in review]")
    labels = adapter.findings(unit, outcome)
    assert [f.finding_id for f in labels] == ["f1", "f2"] and all(f.validity == "unlabeled" for f in labels)
    review = adapter.review_scores(unit, outcome)
    assert {k: round(v, 6) for k, v in review.items()} == {"recall": 0.7, "precision": 0.7, "actionability": 0.85, "win": 0.5}
    scores = scores_from(matches, labels, review)
    assert scores.values["recall_gold"] == 0.5 and scores.sources["recall_gold"] == "gold-matrix"
    assert scores.values["recall_review"] == 0.7 and scores.sources["precision_review"] == "judge-aggregate"
    assert "precision_findings" not in scores.values                            # nothing labelled: no precision claim
    # outcome records are indexed by the unit and point back at it
    assert all(r["context"]["of"] == "arm1:review" for r in outcome.records)
    loaded = load_adapter("infermatrix_copilot.improve.adapters.review_eval:ReviewEvalAdapter", gt_dir=gt)
    assert isinstance(loaded, ReviewEvalAdapter)


# -- the bot adapter -------------------------------------------------------------------------------

def test_rb_adapter_is_descriptive_only_with_proxy_labels(tmp_path):
    threads = [{"path": "a.py", "line": 10, "body": "bot: bug", "resolved": True, "outdated": False, "replies": []},
               {"path": "b.py", "line": 20, "body": "bot: risk", "resolved": False, "outdated": False,
                "replies": [{"author": "author", "body": "I disagree, this is intentional"}]},
               {"path": "c.py", "line": 30, "body": "bot: nit", "resolved": False, "outdated": False, "replies": []}]
    for t, fid in zip(threads, ("f-a", "f-b", "f-c")):
        t["author"] = "bot"
        t["body"] = f"[{fid}] {t['body']}"
    assert classify({"id": "f-a", "path": "a.py", "line": 12}, threads, bot_login="bot") == "accepted"   # its own thread, nearby line
    assert classify({"id": "f-b", "path": "b.py", "line": 20}, threads, bot_login="bot") == "disputed"
    assert classify({"id": "f-c", "path": "c.py", "line": 30}, threads, bot_login="bot") == "silent"
    assert classify({"id": "f-z", "path": "z.py", "line": 1}, threads, bot_login="bot") == "silent"
    assert classify({"id": "f-c", "path": "c.py", "line": 30},
                    [{**threads[2], "replies": [{"author": "author", "body": "good catch, fixed"}]}], bot_login="bot") == "accepted"
    # a HUMAN's resolved thread at the same line is not the bot's: the finding stays silent
    human = [{"path": "a.py", "line": 10, "body": "please fix", "author": "reviewer", "resolved": True, "outdated": False, "replies": []}]
    assert classify({"id": "f-a", "path": "a.py", "line": 10}, human, bot_login="bot") == "silent"
    # with two bot threads at one spot the finding's id marker picks its own
    two = [{"path": "a.py", "line": 10, "body": "[f-1] first", "author": "bot", "resolved": True, "outdated": False, "replies": []},
           {"path": "a.py", "line": 11, "body": "[f-2] second", "author": "bot", "resolved": False, "outdated": False,
            "replies": [{"author": "dev", "body": "this is intentional"}]}]
    assert classify({"id": "f-2", "path": "a.py", "line": 10}, two, bot_login="bot") == "disputed"
    assert classify({"id": "f-1", "path": "a.py", "line": 10}, two, bot_login="bot") == "accepted"
    assert classify({"path": "a.py", "line": 10, "title": "second"}, two, bot_login="bot") == "disputed"              # by whole title
    assert classify({"id": "f-9", "path": "a.py", "line": 10, "title": "second"}, two, bot_login="bot") == "silent"   # id conflicts
    assert classify({"id": "f-9", "path": "a.py", "line": 10}, two, bot_login="bot") == "silent"        # ambiguous: no thread claimed
    # whole markers only: f-1 must not match [f-10]; a lone thread with a DIFFERENT identity is not "close enough"
    ten = [{"path": "a.py", "line": 10, "body": "[f-10] tenth", "author": "bot", "resolved": True, "outdated": False, "replies": []},
           {"path": "a.py", "line": 12, "body": "[f-1] first", "author": "bot", "resolved": False, "outdated": False,
            "replies": [{"author": "dev", "body": "won't fix"}]}]
    assert classify({"id": "f-1", "path": "a.py", "line": 10}, ten, bot_login="bot") == "disputed"
    assert classify({"id": "f-2", "path": "a.py", "line": 10, "title": "other"}, ten[:1], bot_login="bot") == "silent"
    assert classify({"path": "a.py", "line": 10}, ten[:1], bot_login="bot") == "accepted"             # nothing to match on
    # two findings sharing a long prefix: each matches only its own whole title, never the other's
    prefix = "the fallback branch on kernels version below fifteen is dead code because the guard above"
    shared = [{"path": "k.py", "line": 5, "body": f"{prefix} always raises", "author": "bot", "resolved": True,
               "outdated": False, "replies": []},
              {"path": "k.py", "line": 6, "body": f"{prefix} never fires", "author": "bot", "resolved": False,
               "outdated": False, "replies": [{"author": "dev", "body": "not a bug"}]}]
    assert classify({"path": "k.py", "line": 5, "title": f"{prefix} never fires"}, shared, bot_login="bot") == "disputed"
    assert classify({"path": "k.py", "line": 5, "title": f"{prefix} always raises"}, shared, bot_login="bot") == "accepted"
    assert classify({"path": "k.py", "line": 5, "title": f"{prefix} never fires"}, shared[:1], bot_login="bot") == "silent"
    assert classify({"path": "k.py", "line": 5, "title": prefix}, shared, bot_login="bot") == "silent"   # a prefix is not a title
    # a thread whose title EXTENDS the requested title belongs to another finding, and so does one with another id
    ext = [{"path": "e.py", "line": 1, "body": "[f-2] Reject empty input only when strict mode is enabled", "author": "bot",
            "resolved": True, "outdated": False, "replies": []}]
    assert classify({"id": "f-1", "path": "e.py", "line": 1, "title": "Reject empty input"}, ext, bot_login="bot") == "silent"
    assert classify({"path": "e.py", "line": 1, "title": "Reject empty input"}, ext, bot_login="bot") == "silent"
    assert classify({"id": "f-1", "path": "e.py", "line": 1, "title": "Reject empty input only when strict mode is enabled"},
                    ext, bot_login="bot") == "silent"                                                   # conflicting marker
    assert classify({"path": "e.py", "line": 1, "title": "reject empty input only when strict mode is enabled."},
                    ext, bot_login="bot") == "accepted"                                                 # the whole title
    # a finding without a title matches by its FULL comment (the bot posts it verbatim), multiline included
    body = "Guard is too strict.\n\nExisting callers pass None here; this now raises."
    multi = [{"path": "m.py", "line": 3, "body": "[f-7] " + body, "author": "bot", "resolved": False, "outdated": False,
              "replies": [{"author": "dev", "body": "this is fine, intentional"}]},
             {"path": "m.py", "line": 4, "body": body + " Also the docs.", "author": "bot", "resolved": True, "outdated": False,
              "replies": []}]
    assert classify({"path": "m.py", "line": 3, "comment": body}, multi, bot_login="bot") == "disputed"
    assert classify({"path": "m.py", "line": 3, "comment": body + " Also the docs."}, multi, bot_login="bot") == "accepted"
    assert classify({"path": "m.py", "line": 3, "comment": "Guard is too strict."}, multi, bot_login="bot") == "silent"   # a prefix
    assert classify({"id": "f-8", "path": "m.py", "line": 3, "comment": body}, multi, bot_login="bot") == "silent"  # id conflicts
    # the bot's real publication format (publish.py::_inline_comment): **[severity]** comment + Evidence trailer
    published = [{"path": "p.py", "line": 20, "body": "**[major]** Guard is too strict.\n\nEvidence: caller.py:20 passes None",
                  "author": "bot", "resolved": True, "outdated": False, "replies": []},
                 {"path": "p.py", "line": 21, "body": "**[minor]** Guard is too strict for the docs path.", "author": "bot",
                  "resolved": False, "outdated": False, "replies": [{"author": "dev", "body": "won't fix"}]}]
    assert classify({"file": "p.py", "line": 20, "severity": "major", "comment": "Guard is too strict.",
                     "evidence": "caller.py:20 passes None"}, published, bot_login="bot") == "accepted"
    assert classify({"file": "p.py", "line": 20, "comment": "Guard is too strict for the docs path."}, published,
                    bot_login="bot") == "disputed"
    assert classify({"file": "p.py", "line": 20, "comment": "Guard is too strict"}, published, bot_login="bot") == "silent"  # evidence differs
    assert classify({"file": "p.py", "line": 20, "comment": "Guard is"}, published, bot_login="bot") == "silent"
    # a finding whose comment ITSELF ends in an Evidence line is its own finding, distinct from one with evidence appended
    own_evidence = [{"path": "q.py", "line": 8, "body": "**[major]** Guard is too strict.\n\nEvidence: existing callers pass None.",
                     "author": "bot", "resolved": True, "outdated": False, "replies": []}]
    assert classify({"file": "q.py", "line": 8, "comment": "Guard is too strict.\n\nEvidence: existing callers pass None."},
                    own_evidence, bot_login="bot") == "accepted"
    assert classify({"file": "q.py", "line": 8, "comment": "Guard is too strict.", "evidence": "existing callers pass None."},
                    own_evidence, bot_login="bot") == "accepted"                                         # the same rendering
    assert classify({"file": "q.py", "line": 8, "comment": "Guard is too strict."}, own_evidence, bot_login="bot") == "silent"
    store = _store(tmp_path)
    with trace_context(run_id="demo#7@h", playbook="rb-review", step="review", repo="demo", pr=7):
        store.append("decision", outputs={"review": "body"},
                     result={"status": "posted", "findings": [{"id": "f-a", "path": "a.py", "line": 10},
                                                                {"id": "f-b", "path": "b.py", "line": 20},
                                                                {"id": "f-c", "path": "c.py", "line": 30}]})
    unit = units_between(store, T0, T0 + 10_000)["demo#7@h:review"]
    fetched = []
    adapter = RbReviewAdapter(fetch_threads=lambda repo, pr: (fetched.append((repo, pr)) or {"state": "MERGED", "head": "h", "threads": threads}),
                              bot_login="bot")
    outcome = adapter.fetch(unit, store)
    assert fetched == [("demo", 7)] and adapter.fetch(unit, store) and fetched == [("demo", 7)]   # cached in the store
    labels = {f.finding_id: f.validity for f in adapter.findings(unit, outcome)}
    assert labels == {"f-a": "valid", "f-b": "invalid", "f-c": "unlabeled"}
    assert adapter.match(unit, None, outcome) == [] and adapter.review_scores(unit, outcome) is None
    scores = scores_from([], adapter.findings(unit, outcome), None, descriptive_only=True)
    assert scores.values["precision_proxy"] == 0.5 and scores.sources["precision_proxy"].endswith(";descriptive-only")
    assert adapter.descriptive_only and outcome.of_type("rb_thread")[0]["result"]["proxy"]
    failing = RbReviewAdapter(fetch_threads=lambda repo, pr: (_ for _ in ()).throw(RuntimeError("gh down")))
    with trace_context(run_id="demo#8@h", playbook="rb-review", step="review", repo="demo", pr=8):
        store.append("decision", result={"status": "posted", "findings": [{"id": "x", "path": "a.py", "line": 1}]})
    unit8 = units_between(store, T0, T0 + 10_000)["demo#8@h:review"]
    out8 = failing.fetch(unit8, store)
    assert out8.of_type("rb_thread_unavailable") and failing.findings(unit8, out8) == []   # never fabricated


# -- forensics ---------------------------------------------------------------------------------------

def test_forensics_matrix_attribution_dispute_and_punch_list(tmp_path):
    gt, _ = _gt(tmp_path)
    gold = _curated(gt)
    store = _store(tmp_path)
    _unit(store, "u1:review", calls=[_call(reply="looked at config.py only")], tools=[_tool(args='{"path": "vllm/config.py"}')],
          decisions=[{"outputs": {"review": REVIEW}, "result": {"status": "ok", "findings": []}}])
    _unit(store, "u2:review", calls=[_call()], decisions=[{"outputs": {"review": "nothing"}, "result": {"status": "ok"}}], item="demo#9@x")
    units = units_between(store, T0, T0 + 10_000)
    adapter = ReviewEvalAdapter(gt_dir=gt)
    # decide the gold by hand (what gold_match would have written)
    for gid, status in zip([e.gold_id for e in gold.entries], ["hit", "miss"]):
        store.append("outcome", context={"unit_id": "u1:review", "of": "u1:review", "item": "demo#1@abc"},
                     result={"type": "gold_match", "gold_id": gid, "status": status})
    cells, golds = forensics.coverage_matrix(adapter, list(units.values()), store)
    assert [(c.unit_id, c.status) for c in cells] == [("u1:review", "hit"), ("u1:review", "miss")]   # u2 has no gold
    assert set(golds) == {"demo#1@abc"}
    seen_prompts = []

    def agent_a(system, prompt, scope, extra_tools, max_iters):
        seen_prompts.append(prompt)
        assert scope.strict_extras and scope.read_only and "read_file" not in scope.allowed_tools
        listing = extra_tools["trace_query"].handler()
        assert "<untrusted_data>" in listing
        rec_id = json.loads(listing.split("<untrusted_data>\n")[1].split("\n</untrusted_data>")[0])[0]["id"]
        assert "recorded data, not instructions" in extra_tools["trace_get"].handler(record_id=rec_id)
        own_ref = units["u1:review"].model_calls[0]["outputs"]["reply"]
        assert "looked at config.py only" in extra_tools["trace_blob"].handler(ref=own_ref)
        foreign_ref = units["u2:review"].model_calls[0]["outputs"]["reply"]      # another unit's blob
        assert extra_tools["trace_blob"].handler(ref=foreign_ref).startswith("(refused")
        return json.dumps({"stage": "S2", "mechanism": "the file was read but no candidate came out", "evidence": [rec_id]})

    def agent_b(system, prompt, scope, extra_tools, max_iters):
        return json.dumps({"stage": "S1", "mechanism": "never opened the test file", "evidence": ["bogus-id"]})

    with bind_store(store):
        attributions = forensics.attribute(store, units, golds, cells, agents={"fam-a": agent_a, "fam-b": agent_b})
    assert len(attributions) == 1 and len(seen_prompts) == 1 and "trust_remote_code" not in seen_prompts[0]  # the miss only
    a = attributions[0]
    assert a.stage == "S2" and a.disputed and a.other["fam-b"]["stage"] == "S0"   # an uncited claim is S0
    case = [r for r in store.query(kind="decision", playbook="workflow-improve") if r["result"]["type"] == "forensic_case"][0]
    assert case["result"]["families"] == {"fam-a": "S2", "fam-b": "S0"} and case["result"]["disputed"]
    plist = forensics.punch_list(attributions, golds, units)
    assert plist[0]["stage"] == "S2" and plist[0]["disputed"] == 1 and plist[0]["loss"] == 0
    agree = forensics.attribute(store, units, golds, cells, agents={"a": agent_a, "b": agent_a})
    plist = forensics.punch_list(agree, golds, units)
    assert plist[0]["loss"] == 1 and not agree[0].disputed
    health = forensics.measurement_health(agree)
    assert health == {"cells": 1, "s10": 0, "s10_share": 0.0, "disputed": 0}
    unparseable = forensics.attribute_cell(store, units["u1:review"], gold, gold.entries[1].gold_id,
                                           agent=lambda *a: "I refuse", family="x")
    assert unparseable.stage == "S0"


# -- meta-benchmark ------------------------------------------------------------------------------------

def test_meta_case_export_and_load_and_lint_samples(tmp_path):
    gt, _ = _gt(tmp_path)
    gold = _curated(gt)
    store = _store(tmp_path)
    _unit(store, "u1:review", **CLEAN)
    unit = units_between(store, T0, T0 + 10_000)["u1:review"]
    meta_dir = tmp_path / "meta"
    dest = meta.export_case(store, unit, gt / "curated" / "pr1.gold.json", {gold.entries[1].gold_id: "S2"},
                            meta_dir / "cases" / "case-1")
    assert (dest / "records.jsonl").exists() and list((dest / "blobs").rglob("*.gz"))
    cases = meta.load_cases(meta_dir)
    assert len(cases) == 1 and cases[0].unit.unit_id == "u1:review" and cases[0].labels == {gold.entries[1].gold_id: "S2"}
    assert cases[0].store.blob(cases[0].unit.model_calls[0]["outputs"]["reply"]) == "a fine reply"
    lint_dir = meta_dir / "lints" / "L01"
    lint_dir.mkdir(parents=True)
    lint_store = TraceStore(lint_dir, environ={})
    with trace_context(run_id="s", unit_id="s:review"):
        lint_store.append("model_call", **_call(out_tokens=1000, max_tokens=1000, stop="max_tokens"))
        lint_store.append("decision", **_decision())
    day = next((lint_dir / "records").glob("*.jsonl"))
    (lint_dir / "sample.jsonl").write_text(day.read_text())
    samples = meta.lint_samples(meta_dir)
    assert set(samples) == {"L01"} and samples["L01"][0].unit_id == "s:review"
    from infermatrix_copilot.improve import cli
    assert cli.main(["meta", "lint-check", "--meta-dir", str(meta_dir)]) == 0
    assert cli.main(["gold", "check", "--gt-dir", str(gt)]) == 0


def test_forensics_step_collects_outcomes_before_the_matrix(tmp_path, settings):
    """Nothing pre-seeded: the step must import the paired judge's verdicts
    and run gold_match itself, then attribute the misses."""
    import asyncio

    from infermatrix_copilot.engine.steps import improve as steps

    gt, _ = _gt(tmp_path)
    gold = _curated(gt)
    store = _store(tmp_path)
    _unit(store, "arm1:review", calls=[_call()], tools=[_tool()],
          decisions=[{"outputs": {"review": REVIEW}, "result": {"status": "ok", "findings": [{"id": "f1"}]}}])
    judgments = tmp_path / "judgments" / "goal_x"
    judgments.mkdir(parents=True)
    (judgments / "pr1.r1.json").write_text(json.dumps({
        "x": {"recall": 0.5, "precision": 0.7, "actionability": 0.9}, "y": {"recall": 0.9, "precision": 0.8, "actionability": 0.5},
        "winner": "Y", "margin": "clear", "_roles": {"arm": "copilot_v1", "baseline": "opus"},
        "_blinding": {"X": "copilot_v1", "Y": "opus"}}))
    answers = {"gold": iter(['{"status": "hit", "quote": "trust_remote_code should default to False"}'] * 3
                            + ['{"status": "miss", "quote": ""}'] * 3)}

    from infermatrix_copilot.llm import Block, Reply

    class FakeLLM:
        available = True

        def for_target(self, target):
            return self

        def create(self, **kw):
            if kw.get("role") == "gold_match":
                text = next(answers["gold"])
            else:   # the forensics investigator answers without citing anything
                text = json.dumps({"stage": "S1", "mechanism": "never opened tests/", "evidence": []})
            return Reply(blocks=[Block(type="text", text=text)], stop_reason="end_turn", usage={}, model="m")

    st = settings.model_copy(update={"trace_store_root": str(store.root), "improve_gt_dir": str(gt),
                                     "improve_judgments_dir": str(tmp_path / "judgments"), "improve_eval_arm": "copilot_v1",
                                     "improve_judge": "api:sonnet"})
    ctx = SimpleNamespace(settings=st, state={"improve_cycle": {"since": T0, "until": T0 + 10_000}}, params={"force": True},
                          run_dir=tmp_path / "run", trace=SimpleNamespace(record=lambda *a, **k: None), llm=FakeLLM())
    result = asyncio.run(steps._forensics(ctx))
    entry = result.outputs["forensics"]["pr-review.agent.review_diff"]
    assert entry["collected"] == {"verdicts": 1, "gold_matched": 2, "judge": "api:sonnet", "errors": [], "inconclusive": 0}
    assert entry["cells"] == 2 and entry["misses"] == 1 and entry["unlabeled"] == 0
    assert entry["punch_list"] and entry["punch_list"][0]["stage"] == "S0"      # an uncited claim is never a stage
    outcomes = store.query(kind="outcome", unit_id="arm1:review")
    assert {r["result"]["type"] for r in outcomes} == {"judge_verdict", "gold_match"}
    # a second run imports nothing new and decides nothing twice
    result = asyncio.run(steps._forensics(ctx))
    assert result.outputs["forensics"]["pr-review.agent.review_diff"]["collected"]["gold_matched"] == 0


def test_inconclusive_gold_votes_are_never_cached(tmp_path):
    gt, _ = _gt(tmp_path)
    gold = _curated(gt)
    store = _store(tmp_path)
    _unit(store, "arm1:review", calls=[_call()], decisions=[{"outputs": {"review": REVIEW}, "result": {"status": "ok"}}])
    unit = units_between(store, T0, T0 + 10_000)["arm1:review"]
    scripted = iter([
        # entry 1: hit / miss / error -> no majority
        '{"status": "hit", "quote": "trust_remote_code should default to False"}', '{"status": "miss"}', "garbage",
        # entry 2: three errors -> no verdict at all
        "x", "y", "z",
        # second run, entry 1: a clean majority; entry 2: two misses and an error -> still inconclusive
        '{"status": "hit", "quote": "trust_remote_code should default to False"}',
        '{"status": "hit", "quote": "trust_remote_code should default to False"}', '{"status": "miss"}',
        '{"status": "miss"}', '{"status": "miss"}', "??",
    ])

    class FlakyLLM:
        def create(self, **kw):
            return SimpleNamespace(blocks=[SimpleNamespace(type="text", text=next(scripted))])
    adapter = ReviewEvalAdapter(gt_dir=gt, judge=JudgeSpec("api", "m"), llm=FlakyLLM())
    assert adapter.gold_match(unit, gold, store) == 0 and len(adapter.inconclusive) == 2
    assert all(m.status == "unlabeled" for m in adapter.match(unit, gold, adapter.fetch(unit, store)))
    adapter.inconclusive.clear()
    # entry 1: 2 hits of 3 is a majority; entry 2: 2 misses of 3 (one error) is a majority too
    assert adapter.gold_match(unit, gold, store) == 2 and adapter.inconclusive == []
    statuses = {m.gold_id: m.status for m in adapter.match(unit, gold, adapter.fetch(unit, store))}
    assert statuses[gold.entries[0].gold_id] == "hit" and statuses[gold.entries[1].gold_id] == "miss"
    assert adapter.gold_match(unit, gold, store) == 0                                  # decided cells are not re-judged


def test_executor_records_the_rendered_review_as_the_units_terminal_decision(tmp_path, settings):
    """The real review step publishes review_text through step outputs and
    never writes a decision itself: the executor must, or the adapter judges
    an empty body."""
    import asyncio

    from execution_helpers import application_executor as Executor
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepResult, StepSpec
    from infermatrix_copilot.playbooks.store import Playbook, PlaybookStep
    from infermatrix_copilot.run_trace import RunTrace

    async def review(ctx):
        return StepResult(True, summary="reviewed", outputs={
            "review_text": REVIEW, "review_verdict": "request_changes",
            "review_comments": [{"file": "vllm/config.py", "line": 66, "severity": "major", "comment": "default"}],
            "state_updates": {"review_text": REVIEW}})

    async def broken(ctx):
        raise RuntimeError("boom")

    registry = StepRegistry()
    registry.register(StepSpec("agent.review_diff", "agent", "read", review, ""))
    registry.register(StepSpec("x.broken", "deterministic", "read", broken, ""))
    st = settings.model_copy(update={"trace_store_root": str(tmp_path / "traces")})
    run_dir = tmp_path / "run-1"
    ex = Executor(registry, st, run_dir=run_dir, trace=RunTrace(run_dir / "t.jsonl"))
    pb = Playbook(name="pr-review", version=1, status="active", task_kinds=["pr_review"], repos=[],
                  steps=[PlaybookStep(id="review", step="agent.review_diff"), PlaybookStep(id="b", step="x.broken")])
    asyncio.run(ex.run(pb, {"task_spec": {"repo": "demo", "pr": 1}, "pr_head_sha": "abc"}))
    store = TraceStore(tmp_path / "traces")
    unit = units_between(store, 0, 2e12)["run-1:review"]
    decision = unit.decisions[-1]
    assert decision["result"]["type"] == "step_result" and decision["result"]["status"] == "ok"
    assert decision["result"]["findings"][0]["file"] == "vllm/config.py" and decision["result"]["verdict"] == "request_changes"
    assert store.blob(decision["outputs"]["review"]) == REVIEW
    adapter = ReviewEvalAdapter(gt_dir=tmp_path)
    assert adapter._review_text(unit, store) == REVIEW
    failed = units_between(store, 0, 2e12)["run-1:b"].decisions[-1]
    assert failed["result"]["status"] == "failed" and "boom" in failed["result"]["summary"]
    # no rendered review -> gold_match judges nothing (cells stay unlabeled), no cached false misses
    gt, _ = _gt(tmp_path)
    gold = _curated(gt)
    bare = _store(tmp_path / "bare")
    _unit(bare, "bare:review", calls=[_call()], decisions=[{"result": {"status": "ok"}}])
    bare_unit = units_between(bare, T0, T0 + 10_000)["bare:review"]
    calls = []

    class CountingLLM:
        def create(self, **kw):
            calls.append(1)
            return SimpleNamespace(blocks=[SimpleNamespace(type="text", text='{"status": "miss"}')])
    a2 = ReviewEvalAdapter(gt_dir=gt, judge=JudgeSpec("api", "m"), llm=CountingLLM())
    assert a2.gold_match(bare_unit, gold, bare) == 0 and calls == []
    assert all(m.status == "unlabeled" for m in a2.match(bare_unit, gold, a2.fetch(bare_unit, bare)))


def test_forensics_step_fetches_bot_outcomes_and_honours_external_declarations(tmp_path, settings, monkeypatch):
    import asyncio

    from infermatrix_copilot.engine.steps import improve as steps
    from infermatrix_copilot.improve.adapters import rb_review

    store = _store(tmp_path)
    with trace_context(run_id="demo#7@h", playbook="rb-review", step="review", repo="demo", pr=7):
        store.append("decision", outputs={"review": "body"},
                     result={"status": "posted", "findings": [{"id": "f-a", "path": "a.py", "line": 10},
                                                                {"id": "f-b", "path": "b.py", "line": 20}]})
    fetched = []
    monkeypatch.setattr(rb_review, "gh_threads", lambda repo, pr, **kw: (fetched.append((repo, pr)) or {
        "state": "MERGED", "head": "h", "threads": [
            {"path": "a.py", "line": 10, "body": "[f-a] x", "author": "bot", "resolved": True, "outdated": False, "replies": []},
            {"path": "b.py", "line": 20, "body": "[f-b] y", "author": "bot", "resolved": False, "outdated": False,
             "replies": [{"author": "bot", "body": "I disagree"}, {"author": "dev", "body": "not a bug"}]}]}))
    # an external declaration directory overrides the builtin bot declaration (lower min items)
    decls = tmp_path / "decls"
    decls.mkdir()
    (decls / "rb.yaml").write_text(
        "workflow: rb-review.review\nkind: dynamic\nunit: agent_loop\nitem_key: '{repo}#{pr}'\n"
        "fingerprint:\n  covers: [copilot_sha]\n"
        "outcome_adapter: infermatrix_copilot.improve.adapters.rb_review:RbReviewAdapter\ntier2_min_items: 1\n")
    st = settings.model_copy(update={"trace_store_root": str(store.root), "improve_workflows_dirs": str(decls),
                                     "improve_rb_bot_login": "bot"})
    ctx = SimpleNamespace(settings=st, state={"improve_cycle": {"since": T0, "until": T0 + 10_000}}, params={},
                          run_dir=tmp_path / "run", trace=SimpleNamespace(record=lambda *a, **k: None), llm=None)
    result = asyncio.run(steps._forensics(ctx))
    entry = result.outputs["forensics"]["rb-review.review"]
    assert fetched == [("demo", 7)] and entry["outcomes"] == 1 and entry["cells"] == 0
    assert entry["finding_labels"] == {"valid": 1, "invalid": 1, "unlabeled": 0}      # the bot's own reply ignored
    scores = entry["scores"]["demo#7@h:review"]
    assert scores["values"]["precision_proxy"] == 0.5 and scores["sources"]["precision_proxy"].endswith(";descriptive-only")
    assert entry["descriptive_only"] and "skipped" not in entry
    # without the external directory the builtin declaration's min items (8) skips it
    ctx_builtin = SimpleNamespace(settings=st.model_copy(update={"improve_workflows_dirs": ""}), state=ctx.state, params={},
                                  run_dir=tmp_path / "run2", trace=ctx.trace, llm=None)
    assert "skipped" in asyncio.run(steps._forensics(ctx_builtin)).outputs["forensics"]["rb-review.review"]


def test_forensics_step_skips_below_min_items_and_without_llm(tmp_path, settings):
    import asyncio

    from infermatrix_copilot.engine.steps import improve as steps

    store = _store(tmp_path)
    _unit(store, "u1:review", **CLEAN)
    st = settings.model_copy(update={"trace_store_root": str(store.root), "improve_gt_dir": str(tmp_path / "gt")})
    ctx = SimpleNamespace(settings=st, state={"improve_cycle": {"since": T0, "until": T0 + 10_000}}, params={},
                          run_dir=tmp_path / "run", trace=SimpleNamespace(record=lambda *a, **k: None), llm=None)
    result = asyncio.run(steps._forensics(ctx))
    assert result.ok and "skipped" in result.outputs["forensics"]["pr-review.agent.review_diff"]
    ctx.params = {"force": True}
    result = asyncio.run(steps._forensics(ctx))
    entry = result.outputs["forensics"]["pr-review.agent.review_diff"]
    assert entry["units"] == 1 and entry["cells"] == 0                         # no curated gold: no rows
    missing = SimpleNamespace(settings=st, state={}, params={}, run_dir=tmp_path / "run",
                              trace=SimpleNamespace(record=lambda *a, **k: None), llm=None)
    assert not asyncio.run(steps._forensics(missing)).ok
