"""Refine and recheck: a change the gate rejected is never merged; it is refined and judged again."""

from __future__ import annotations

import json
import pytest

from infermatrix_copilot.kb_service import merge
from infermatrix_copilot.kb_service.refine import MAX_REFINES
from infermatrix_copilot.kb_service.runtime import collect_events, run_intake
from test_kb_flow import _ack, _flow_runtime, _items, _open_pr
from test_kb_intake_gate import PAGE, ScriptedGateway, _judge_all, _rule

HEAD = "d" * 40


class Judges:
    """The generator adds a rule; the judge answers from a script, one verdict per judging round."""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)   # "yes"/"no" per round of block judging
        self.prompts: list[str] = []

    def __call__(self, role, prompt):
        if role.name == "generator":
            self.prompts.append(prompt)
            return {"operations": [{"kind": "add", "page": PAGE, "rule_id": "DEMO-2a",
                                    "section_markdown": _rule("DEMO-2a", "PR #11")}], "rationale": "r"}
        if '"directory"' in prompt:
            return {"verdict": "consistent", "conflicts": []}
        value = self.verdicts[0] if self.verdicts else "yes"
        answer = _judge_all(value)(role, prompt)
        if value == "no":
            answer["reasons"] = {d: "the claim is not in the evidence" for d in answer["dimensions"]}
        return answer

    def next_round(self):
        if self.verdicts:
            self.verdicts.pop(0)


def _runtime(tmp_path, verdicts):
    rt, lifecycle = _flow_runtime(tmp_path)
    judges = Judges(verdicts)
    rt.gateway = ScriptedGateway(judges)
    rt.lease_owner = rt.ledger.acquire_lease("scheduler")
    return rt, lifecycle, judges


@pytest.mark.parametrize('fault', ['unknown', 'crash'])
def test_refinement_generation_intent_survives_unknown_or_interrupted_call(tmp_path, fault):
    from infermatrix_copilot.kb_service.models import ModelUnavailable
    from infermatrix_copilot.kb_service.refine import refine
    rt, lifecycle, judges = _runtime(tmp_path, ['no'])
    collect_events(rt, lifecycle)
    parent = rt.ledger.changeset(run_intake(rt, lifecycle))

    def interrupted(role, prompt):
        if fault == 'crash':
            raise KeyboardInterrupt('worker interrupted during native call')
        error = ModelUnavailable('native result unknown', allow_fallback=False)
        error.native_call_sent, error.native_call_completed = True, False
        raise error

    rt.gateway.answer = interrupted
    if fault == 'crash':
        with pytest.raises(KeyboardInterrupt):
            refine(rt, lifecycle, parent)
    else:
        assert refine(rt, lifecycle, parent) is None
    state = json.loads(rt.ledger.get_cursor('demo', 'draft:refine:' + parent['id']))
    assert state['draft_dispatched'] and 'draft_result' not in state
    assert state['draft_binding']['base_sha'] == rt.knowledge.fetch()
    assert state['draft_binding']['generator']['model'] == rt.generator.model
    calls = list(rt.gateway.calls)
    rt.gateway.answer = judges
    assert refine(rt, lifecycle, parent) is None
    assert rt.gateway.calls == calls
    assert rt.ledger.changeset(parent['id']) == parent


def test_refinement_reuses_completed_draft_when_interrupted_before_gate(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service import runtime
    from infermatrix_copilot.kb_service.refine import refine
    rt, lifecycle, judges = _runtime(tmp_path, ['no', 'yes'])
    collect_events(rt, lifecycle)
    parent = rt.ledger.changeset(run_intake(rt, lifecycle))
    judges.next_round()
    gate = runtime.gate_and_stage

    def interrupted(*args, **kwargs):
        raise OSError('crash before gate reservation')

    monkeypatch.setattr(runtime, 'gate_and_stage', interrupted)
    with pytest.raises(OSError, match='before gate reservation'):
        refine(rt, lifecycle, parent)
    generated = len(judges.prompts)
    saved = json.loads(rt.ledger.get_cursor('demo', 'draft:refine:' + parent['id']))
    assert saved['draft_result']['draft']['operations']
    from dataclasses import replace
    generator = rt.generator
    rt.generator = replace(generator, model=generator.model + '-changed')
    assert refine(rt, lifecycle, parent) is None
    assert len(judges.prompts) == generated
    rt.generator = generator

    def resumed_gate(*args, **kwargs):
        trace_key = saved['draft_result']['trace_key']
        assert trace_key and kwargs['draft_keys'] == [trace_key]
        return gate(*args, **kwargs)

    monkeypatch.setattr(runtime, 'gate_and_stage', resumed_gate)
    child = refine(rt, lifecycle, parent)
    assert child and rt.ledger.changeset(child)['kind'] == 'refine'
    assert len(judges.prompts) == generated


def test_a_rejected_intake_is_refined_with_the_reasons_and_judged_again(tmp_path):
    rt, lifecycle, judges = _runtime(tmp_path, ["no", "yes"])
    collect_events(rt, lifecycle)
    first = run_intake(rt, lifecycle)
    assert rt.ledger.changeset(first)["status"] == "failed"
    judges.next_round()
    events = merge.advance(rt, lifecycle)
    (event,) = [e for e in events if e.startswith("refined")]
    new_id = event.rsplit(" ", 1)[1]
    assert "gate_feedback" in judges.prompts[-1] and "not in the evidence" in judges.prompts[-1]
    old, new = rt.ledger.changeset(first), rt.ledger.changeset(new_id)
    assert old["status"] == "refined" and old["detail"]["refined_as"] == new_id
    assert new["kind"] == "refine" and new["detail"]["refine_round"] == 1
    assert new["status"] == "pr_requested"        # passed the whole gate again, then handed over
    assert merge.advance(rt, lifecycle) == []     # refined once, not again


def test_a_change_still_rejected_after_two_refinements_stays_unmerged_for_people(tmp_path):
    rt, lifecycle, judges = _runtime(tmp_path, ["no"] * (MAX_REFINES + 1))
    collect_events(rt, lifecycle)
    run_intake(rt, lifecycle)
    for _ in range(MAX_REFINES + 1):
        merge.advance(rt, lifecycle)
    exhausted = rt.ledger.changesets("demo", ("refine_exhausted",))
    assert len(exhausted) == 1 and exhausted[0]["detail"]["refine_round"] == MAX_REFINES
    assert len(exhausted[0]["detail"]["refine_history"]) == MAX_REFINES + 1
    (row,) = rt.ledger.human_queue("demo")
    assert f"still rejected after {MAX_REFINES} refinements" in row["reason"] and "round 0" in row["reason"]
    assert not _items(tmp_path, "open_pr") and not _items(tmp_path, "merge")
    calls = len(judges.prompts)
    merge.advance(rt, lifecycle)
    assert len(judges.prompts) == calls          # no more paid generator calls


def test_a_pr_the_local_gate_refused_is_replaced_by_a_refined_one(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    changeset_id, publisher = _open_pr(tmp_path, rt, lifecycle)
    _ack(tmp_path, rt, publisher, kind="open_pr", changeset_id=changeset_id, ok=True, pr=42, head_sha=HEAD)
    rt.github.prs[42] = {"state": "open", "merged": False, "head": {"sha": HEAD}}
    merge.advance(rt, lifecycle)
    _ack(tmp_path, rt, publisher, kind="merge", changeset_id=changeset_id, ok=False,
         error="local gate: L1 dangling_reference repos/demo/x.md DEMO-1a",
         problems=["L1 dangling_reference repos/demo/x.md DEMO-1a"])
    assert rt.ledger.changeset(changeset_id)["status"] == "refine_needed"
    assert rt.ledger.human_queue("demo") == []    # not people yet: refined first
    judges = Judges(["yes"])
    rt.gateway = ScriptedGateway(judges)
    rt.lease_owner = rt.ledger.acquire_lease("scheduler")
    events = merge.advance(rt, lifecycle)
    assert any(e.startswith(f"refined {changeset_id} as ") for e in events)
    assert "dangling_reference" in judges.prompts[-1]
    old = rt.ledger.changeset(changeset_id)
    assert old["status"] == "superseding"
    (close,) = _items(tmp_path, "close")
    assert close["body"]["pr"] == 42
    assert rt.ledger.changeset(old["detail"]["refined_as"])["status"] == "pr_requested"


def test_a_refinement_is_never_staged_twice(tmp_path):
    rt, lifecycle, judges = _runtime(tmp_path, ["no", "yes"])
    collect_events(rt, lifecycle)
    first = run_intake(rt, lifecycle)
    judges.next_round()
    real = rt.ledger.update_changeset
    crashed = {"n": 0}

    def crash_once(cs_id, **fields):
        if cs_id == first and fields.get("status") == "refined" and not crashed["n"]:
            crashed["n"] += 1
            raise RuntimeError("crash after staging the refinement")
        return real(cs_id, **fields)
    rt.ledger.update_changeset = crash_once
    import pytest
    with pytest.raises(RuntimeError):
        merge.advance(rt, lifecycle)
    rt.ledger.update_changeset = real
    merge.advance(rt, lifecycle)
    assert len([cs for cs in rt.ledger.changesets("demo", ("pr_requested", "gated", "failed"))
                if cs["kind"] == "refine"]) == 1


def test_an_external_pr_refused_by_the_local_gate_is_told_why(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    owner = rt.ledger.acquire_lease("t")
    cs = rt.ledger.new_changeset_id("demo", "external")
    rt.ledger.stage_intake(owner, "demo", cs, kind="external", status="merge_requested", verdicts=[],
                           human_reason="", drafted_events=[], detail={})
    rt.ledger.update_changeset(cs, pr_number=7, head_sha=HEAD, pending_item={"id": "m1", "kind": "merge",
                                                                              "expires_at": rt.clock() + 60})
    merge.apply_acks(rt, [{"item_id": "m1", "kind": "merge", "changeset_id": cs, "ok": False,
                           "error": "local gate: L1 x", "problems": ["L1 x"]}])
    assert rt.ledger.changeset(cs)["status"] == "gate_failed"
    assert rt.ledger.human_queue("demo") == []           # its author is told, and it is checked again daily
    (findings,) = [item["body"] for item in _items(tmp_path, "post_findings")]
    assert findings["pr"] == 7 and "- L1 x" in findings["comment"]
    assert json.dumps(rt.ledger.changeset(cs)["detail"]["gate_problems"]) == '["L1 x"]'


def _failed_intake(tmp_path, verdicts):
    rt, lifecycle, judges = _runtime(tmp_path, verdicts)
    collect_events(rt, lifecycle)
    first = run_intake(rt, lifecycle)
    assert rt.ledger.changeset(first)["status"] == "failed"
    return rt, lifecycle, judges, first


def test_every_piece_of_evidence_reaches_the_refinement(tmp_path):
    rt, lifecycle, judges, first = _failed_intake(tmp_path, ["no"])
    data = rt.load_changeset_files(first)
    data["evidence"] = [{"source_reference": "PR #11", "title": "Bound the queue", "changed_files": ["a.py"]},
                        {"source_reference": "PR #12", "title": "Retry the upload", "changed_files": ["b.py"]}]
    rt.save_changeset_files(first, data)
    merge.advance(rt, lifecycle)
    prompt = judges.prompts[-1]
    assert "Bound the queue" in prompt and "Retry the upload" in prompt and "PR #12" in prompt


def test_a_rebuild_keeps_the_refine_limit(tmp_path):
    rt, lifecycle, judges, first = _failed_intake(tmp_path, ["no"])
    detail = rt.ledger.changeset(first)["detail"]
    rt.ledger.update_changeset(first, status="rebuild_needed", pr_number=42, head_sha=HEAD, detail={
        **detail, "refine_round": MAX_REFINES, "refine_history": [{"changeset": "a", "reasons": ["x"]},
                                                                  {"changeset": "b", "reasons": ["y"]}]})
    new_id = merge.rebuild(rt, lifecycle, rt.ledger.changeset(first), "context changed since ...")
    assert new_id is None                                   # the rebuild was rejected by its own gate
    (rebuilt,) = [cs for cs in rt.ledger.changesets_of_kind("demo", "rebuild")]
    assert rebuilt["detail"]["refine_round"] == MAX_REFINES  # the lineage carried over
    calls = len(judges.prompts)
    merge.advance(rt, lifecycle)
    assert rt.ledger.changeset(rebuilt["id"])["status"] == "refine_exhausted"   # no third refinement
    assert len(judges.prompts) == calls


def test_a_refinement_staged_before_a_crash_is_found_in_any_status(tmp_path):
    from infermatrix_copilot.kb_service.refine import REFINE_MARK

    rt, lifecycle, judges, first = _failed_intake(tmp_path, ["no"])
    staged = rt.ledger.new_changeset_id("demo", "refine")
    rt.save_changeset_files(staged, {"base_sha": "x", "files": {}, "deleted": [],
                                     "evidence": [{"source_reference": REFINE_MARK + first}]})
    rt.ledger.stage_intake(rt.lease_owner, "demo", staged, kind="refine", status="companion_pending",
                           verdicts=[], human_reason="", drafted_events=[], detail={})
    calls = len(judges.prompts)
    merge.advance(rt, lifecycle)
    assert len(judges.prompts) == calls                     # not drafted again
    assert rt.ledger.changeset(first)["detail"]["refined_as"] == staged


def test_a_refinement_may_carry_as_many_operations_as_the_change_set(tmp_path):
    rt, lifecycle, judges, first = _failed_intake(tmp_path, ["no"])
    data = rt.load_changeset_files(first)
    data["evidence"] = [{"source_reference": f"PR #{n}", "title": f"event {n}"} for n in (11, 12)]
    rt.save_changeset_files(first, data)
    many = [{"kind": "add", "page": PAGE, "rule_id": f"DEMO-{n}a", "section_markdown": _rule(f"DEMO-{n}a", "PR #11")}
            for n in range(3, 10)]                         # 7 operations: more than one event may carry

    class Many(Judges):
        def __call__(self, role, prompt):
            if role.name == "generator" and "gate_feedback" in prompt:
                self.prompts.append(prompt)
                return {"operations": many, "rationale": "r"}
            return super().__call__(role, prompt)
    judges = Many(["yes"])
    rt.gateway = ScriptedGateway(judges)
    events = merge.advance(rt, lifecycle)
    (event,) = [e for e in events if e.startswith(f"refined {first} as ")]
    refined = rt.ledger.changeset(event.rsplit(" ", 1)[1])
    assert len(refined["detail"]["operations"]) == 7
