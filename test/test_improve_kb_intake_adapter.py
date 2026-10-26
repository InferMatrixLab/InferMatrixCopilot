"""The knowledge-intake outcome adapter: gold files, gate outcomes as finding
labels and review scores, gold_match with majority votes, and the workflow
declaration it is wired to."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.improve.adapters import load_adapter, scores_from
from infermatrix_copilot.improve.adapters.kb_intake import (
    KbIntakeAdapter, gold_from_incumbent, gold_id, load_gold, write_gold,
)
from infermatrix_copilot.improve.enroll import load_declarations
from infermatrix_copilot.improve.judges import JudgeSpec
from infermatrix_copilot.improve.reader import units_between
from infermatrix_copilot.trace_store import TraceStore, trace_context
from execution_helpers import contained_codex  # noqa: F401

ITEM = "demo#12"
SECTION = "## DEMO-2b — keep the queue bounded\n\n- 触发：x\n- 强制：reject when full ^[PR #12]\n"


def _unit(store: TraceStore, unit_id: str, operations: list[dict], *, summary: dict | None = None,
          blocks: list[dict] | None = None):
    ctx = dict(playbook="kb-intake", step="draft", workflow="kb-intake.draft", unit_id=unit_id, item=ITEM,
               fingerprint="fp1", run_id="r1")
    with trace_context(**ctx):
        store.append("model_call", inputs={"system": "s", "prompt": "p"}, outputs={"reply": "{}"},
                     model={"role": "generator", "provider": "zcode", "model": "GLM-5.3-Flash"},
                     usage={"input_tokens": 1, "output_tokens": 1}, seconds=1.0)
        store.append("decision", result={"type": "draft_result", "operations": operations,
                                         "empty": not operations, "rejected": False})
    outcome_ctx = {"unit_id": unit_id, "item": ITEM, "of": unit_id, "workflow": "kb-intake.draft"}
    for block in blocks or []:
        store.append("outcome", context=outcome_ctx, result={"type": "gate_block", **block})
    if summary is not None:
        store.append("outcome", context=outcome_ctx, result={"type": "gate_summary", **summary})


def _units(store):
    return units_between(store, 0.0, float("inf"), grace=0.0, lookback=0.0)


def test_declaration_is_tier2_and_repo_neutral():
    decl = load_declarations()["kb-intake.draft"]
    assert decl.tier2 and decl.kind == "static" and decl.unit == "step_call"
    assert decl.item_key == "{repo}#{pr}"
    assert {"prompts": ["kb_service/intake.py", "kb_service/judge_tuning.py"]} in decl.fingerprint_covers
    assert {"routing": ["KB_GENERATOR", "KB_DRAFT_STRATEGY", "ZCODE_REASONING_LEVEL",
                        "KB_JUDGE", "KB_JUDGE_FAMILY_WAIVER"]} in decl.fingerprint_covers
    adapter = load_adapter(decl.outcome_adapter)
    assert isinstance(adapter, KbIntakeAdapter) and not adapter.descriptive_only


def test_fingerprint_is_complete_on_default_settings_and_moves_with_the_knobs():
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.improve.fingerprint import compute

    decl = load_declarations()["kb-intake.draft"]
    settings = Settings(_env_file=None)
    base_env = {"KB_GENERATOR": "claude-code:claude-opus-5-5", "KB_DRAFT_STRATEGY": "v1"}
    fp1, manifest = compute(decl, settings, environ=base_env)
    assert fp1 and manifest["complete"] and manifest["missing"] == []
    assert manifest["covers"]["routing"]["KB_GENERATOR"] == "claude-code:claude-opus-5-5"
    fp2, _ = compute(decl, settings, environ={**base_env, "KB_DRAFT_STRATEGY": "v2"})
    fp3, _ = compute(decl, settings, environ={**base_env, "ZCODE_REASONING_LEVEL": "high"})
    fp4, _ = compute(decl, settings, environ={**base_env, "KB_JUDGE": "zcode:GLM-5.3"})
    assert len({fp1, fp2, fp3, fp4}) == 4


def test_gold_roundtrip_versioning_and_refusals(tmp_path):
    path = write_gold(tmp_path, ITEM, [{"rule_id": "DEMO-2b", "path": "repos/demo/rules.md",
                                        "concern": "keep the queue bounded", "section": SECTION}],
                      source="gate:codex:gpt-6-sol:medium")
    gold = load_gold(path)
    assert gold.item == ITEM and gold.status == "curated" and len(gold.entries) == 1
    assert gold.entries[0].gold_id == gold_id(ITEM, "DEMO-2b", "keep the queue bounded")
    assert gold.entries[0].severity_hint == "DEMO-2b"
    adapter = KbIntakeAdapter(gold_dir=tmp_path)
    assert adapter.gold(ITEM).version == gold.version
    assert adapter.gold("demo#99") is None
    # a reworded concern is a new entry: the stored id no longer matches
    data = json.loads(path.read_text(encoding="utf-8"))
    data["entries"][0]["concern"] = "something else"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="gold_id"):
        load_gold(path)
    # an empty gold set is legitimate (the incumbent passed nothing) and versioned too
    empty = write_gold(tmp_path, "demo#13", [], source="gate:x")
    assert load_gold(empty).entries == () and KbIntakeAdapter(gold_dir=tmp_path).gold("demo#13") is not None


def test_gate_outcomes_become_labels_and_review_scores(tmp_path):
    store = TraceStore(tmp_path / "t", environ={})
    ops = [{"kind": "add", "page": "repos/demo/rules.md", "rule_id": "DEMO-2b", "section_markdown": SECTION},
           {"kind": "add", "page": "repos/demo/rules.md", "rule_id": "DEMO-2c", "section_markdown": SECTION.replace("2b", "2c")},
           {"kind": "add", "page": "repos/demo/rules.md", "rule_id": "DEMO-2d", "section_markdown": SECTION.replace("2b", "2d")}]
    _unit(store, "u1", ops,
          blocks=[{"kind": "rule", "rule_id": "DEMO-2b", "op": "add", "verdict": "pass", "block_id": "b1"},
                  {"kind": "rule", "rule_id": "DEMO-2c", "op": "add", "verdict": "fail", "block_id": "b2"},
                  {"kind": "rule", "rule_id": "DEMO-2d", "op": "add", "verdict": "human", "block_id": "b3"},
                  {"kind": "prose", "rule_id": "", "op": "prose", "verdict": "pass", "block_id": "b4"}],
          summary={"pass": 1, "fail": 1, "human": 1, "empty": False, "rejected": False, "l1_ok": True})
    _unit(store, "u2", [], summary={"pass": 0, "fail": 0, "human": 0, "empty": True, "rejected": False, "l1_ok": True})
    _unit(store, "u3", ops)   # never judged
    adapter = KbIntakeAdapter(gold_dir=tmp_path)
    units = _units(store)
    outcome = adapter.fetch(units["u1"], store)
    labels = adapter.findings(units["u1"], outcome)
    assert [(l.finding_id, l.validity, l.source) for l in labels] == [
        ("DEMO-2b#j1", "valid", "judge"), ("DEMO-2c#j1", "invalid", "judge"), ("DEMO-2d#j1", "unlabeled", "judge")]
    scores = adapter.review_scores(units["u1"], outcome)
    assert scores["net_pass"] == 0.0 and scores["yield_pass"] == 1.0 and scores["precision"] == 0.5
    assert scores["gate_score"] == pytest.approx(0.5) and scores["empty"] == 0.0
    derived = scores_from([], labels, scores)
    assert derived.values["precision_findings"] == 0.5 and derived.sources["precision_findings"] == "finding-labels:judge"
    assert derived.values["net_pass_review"] == 0.0 and derived.sources["net_pass_review"] == "judge-aggregate"
    empty = adapter.fetch(units["u2"], store)
    assert adapter.review_scores(units["u2"], empty) == {"net_pass": 0.0, "gate_score": 0.0, "gate_pass": 1.0,
                                                         "yield_pass": 0.0, "fail": 0.0, "human": 0.0, "empty": 1.0}
    assert adapter.fetch(units["u3"], store) is None   # unjudged: nothing is scored


def test_gold_from_incumbent_takes_only_gate_passing_rules(tmp_path):
    store = TraceStore(tmp_path / "t", environ={})
    ops = [{"kind": "add", "page": "repos/demo/rules.md", "rule_id": "DEMO-2b", "section_markdown": SECTION},
           {"kind": "replace", "page": "repos/demo/rules.md", "rule_id": "DEMO-1a", "new_rule_id": "DEMO-2c",
            "section_markdown": SECTION.replace("2b — keep the queue bounded", "2c — reject on overflow")}]
    _unit(store, "inc", ops,
          blocks=[{"kind": "rule", "rule_id": "DEMO-2b", "op": "add", "verdict": "fail", "block_id": "b1"},
                  {"kind": "rule", "rule_id": "DEMO-2c", "op": "supersede", "verdict": "pass", "block_id": "b2"}],
          summary={"pass": 1, "fail": 1, "human": 0, "empty": False, "rejected": False, "l1_ok": True})
    entries = gold_from_incumbent(store, _units(store)["inc"])
    assert [e["rule_id"] for e in entries] == ["DEMO-2c"]
    # the contract is the complete section, never a title alone
    assert entries[0]["concern"].startswith("## DEMO-2c — reject on overflow\n") and "- 触发" in entries[0]["concern"]
    assert entries[0]["section"] == entries[0]["concern"]


def test_gold_match_votes_majority_verbatim_quote_and_deterministic_empty(tmp_path, contained_codex):
    store = TraceStore(tmp_path / "t", environ={})
    ops = [{"kind": "add", "page": "repos/demo/rules.md", "rule_id": "DEMO-2b", "section_markdown": SECTION}]
    _unit(store, "arm", ops, summary={"pass": 1, "fail": 0, "human": 0, "empty": False, "rejected": False, "l1_ok": True})
    _unit(store, "none", [], summary={"pass": 0, "fail": 0, "human": 0, "empty": True, "rejected": False, "l1_ok": True})
    write_gold(tmp_path, ITEM, [{"rule_id": "G-1", "path": "p", "concern": "keep the queue bounded"},
                                {"rule_id": "G-2", "path": "p", "concern": "retry on timeout"}], source="gate:x")
    answers = iter([
        '{"status": "hit", "quote": "reject when full"}', '{"status": "hit", "quote": "reject when full"}',
        '{"status": "miss", "quote": ""}',                                             # G-1: 2 hits of 3
        '{"status": "hit", "quote": "not in the proposal"}', '{"status": "miss", "quote": ""}',
        '{"status": "miss", "quote": ""}',                                             # G-2: quote check -> miss
    ])

    def codex(argv, **kw):
        text = next(answers)
        return SimpleNamespace(returncode=0, stdout=json.dumps(
            {"type": "item.completed", "item": {"item_type": "agent_message", "text": text}}) + '\n' +
            json.dumps({'type':'turn.completed'}), stderr="")

    contained_codex.runner = codex
    adapter = KbIntakeAdapter(gold_dir=tmp_path, judge=JudgeSpec("cli", "gpt-6-sol", provider="codex"))
    units = _units(store)
    gold = adapter.gold(ITEM)
    assert adapter.gold_match(units["arm"], gold, store) == 2
    matches = adapter.match(units["arm"], gold, adapter.fetch(units["arm"], store))
    assert [(m.gold_id[:0] or gid, m.status) for m, gid in zip(matches, ["G-1", "G-2"])] == [("G-1", "hit"), ("G-2", "miss")]
    assert scores_from(matches, [], None).values["recall_gold"] == 0.5
    # decided cells are not re-judged; an empty draft misses deterministically without a judge call
    assert adapter.gold_match(units["arm"], gold, store) == 0
    assert adapter.gold_match(units["none"], gold, store) == 2
    statuses = [m.status for m in adapter.match(units["none"], gold, adapter.fetch(units["none"], store))]
    assert statuses == ["miss", "miss"]


def test_review_scores_average_the_judge_replicates_of_the_newest_gate(tmp_path):
    store = TraceStore(tmp_path / "t", environ={})
    ops = [{"kind": "add", "page": "repos/demo/rules.md", "rule_id": "DEMO-2b", "section_markdown": SECTION}]
    _unit(store, "u", ops,
          blocks=[{"kind": "rule", "rule_id": "DEMO-2b", "op": "add", "verdict": "pass", "block_id": "b1",
                   "gate_version": 1}],
          summary={"pass": 1, "fail": 0, "human": 0, "empty": False, "rejected": False, "l1_ok": True})  # old gate
    ctx = {"unit_id": "u", "item": ITEM, "of": "u", "workflow": "kb-intake.draft"}
    for rep, verdict in ((1, "pass"), (2, "fail")):
        store.append("outcome", context=ctx, result={"type": "gate_block", "gate_version": 2, "judge_rep": rep,
                                                     "kind": "rule", "rule_id": "DEMO-2b", "op": "add",
                                                     "verdict": verdict, "block_id": f"b{rep}"})
        store.append("outcome", context=ctx, result={"type": "gate_summary", "gate_version": 2, "judge_rep": rep,
                                                     "pass": 1 if verdict == "pass" else 0,
                                                     "fail": 0 if verdict == "pass" else 1, "human": 0,
                                                     "empty": False, "rejected": False, "l1_ok": True,
                                                     "gate_status": verdict})
    adapter = KbIntakeAdapter(gold_dir=tmp_path)
    unit = _units(store)["u"]
    outcome = adapter.fetch(unit, store)
    scores = adapter.review_scores(unit, outcome)
    assert scores["net_pass"] == 0.0 and scores["gate_pass"] == 0.5 and scores["precision"] == 0.5
    labels = adapter.findings(unit, outcome)
    assert sorted((l.finding_id, l.validity) for l in labels) == [("DEMO-2b#j1", "valid"), ("DEMO-2b#j2", "invalid")]
    assert scores_from([], labels, scores).values["precision_findings"] == 0.5


def _judging(store, unit_id, *, rep, verdict, judging_id, gate_version=2):
    ctx = {"unit_id": unit_id, "item": ITEM, "of": unit_id, "workflow": "kb-intake.draft"}
    store.append("outcome", context=ctx, result={"type": "gate_block", "gate_version": gate_version, "judge_rep": rep,
                                                 "judging_id": judging_id, "kind": "rule", "rule_id": "DEMO-2b",
                                                 "op": "add", "verdict": verdict, "block_id": judging_id})
    store.append("outcome", context=ctx, result={"type": "gate_summary", "gate_version": gate_version, "judge_rep": rep,
                                                 "judging_id": judging_id, "pass": 1 if verdict == "pass" else 0,
                                                 "fail": 0 if verdict == "pass" else 1, "human": 0, "empty": False,
                                                 "rejected": False, "l1_ok": True, "gate_status": verdict})


def test_a_rejudged_replicate_replaces_its_earlier_judging_for_labels_and_gold(tmp_path):
    store = TraceStore(tmp_path / "t", environ={})
    ops = [{"kind": "add", "page": "repos/demo/rules.md", "rule_id": "DEMO-2b", "section_markdown": SECTION}]
    _unit(store, "u", ops)
    _judging(store, "u", rep=1, verdict="pass", judging_id="old-version", gate_version=1)   # an older gate: obsolete
    _judging(store, "u", rep=1, verdict="fail", judging_id="first")
    _judging(store, "u", rep=1, verdict="pass", judging_id="second")                          # rep 1 judged again
    adapter = KbIntakeAdapter(gold_dir=tmp_path)
    unit = _units(store)["u"]
    outcome = adapter.fetch(unit, store)
    labels = adapter.findings(unit, outcome)
    assert [(l.finding_id, l.validity) for l in labels] == [("DEMO-2b#j1", "valid")]
    assert adapter.review_scores(unit, outcome)["precision"] == 1.0
    assert scores_from([], labels, None).values["precision_findings"] == 1.0
    # gold follows the current judgings only: passed in the latest rep-1 judging
    assert [e["rule_id"] for e in gold_from_incumbent(store, unit)] == ["DEMO-2b"]
    # ... and an obsolete pass never rescues a rule the current gate fails
    _judging(store, "u", rep=1, verdict="fail", judging_id="third")
    unit = _units(store)["u"]
    assert gold_from_incumbent(store, unit) == []
    assert adapter.review_scores(unit, adapter.fetch(unit, store))["precision"] == 0.0
    # two replicates: a rule passed by one of two is gold (half), by none is not
    _judging(store, "u", rep=2, verdict="pass", judging_id="rep2")
    unit = _units(store)["u"]
    assert [e["rule_id"] for e in gold_from_incumbent(store, unit)] == ["DEMO-2b"]
    assert adapter.review_scores(unit, adapter.fetch(unit, store))["precision"] == 0.5
