"""Meta-improvement engine P1: the unit reader, the 17 Tier 1 lints, the
ledger and proposal state machine, the weekly cycle (dry-run), the task
kind/playbook and the operator commands (design §4, §5, §9)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

import pytest

from infermatrix_copilot.improve import cycle as cyc
from infermatrix_copilot.improve import lints
from infermatrix_copilot.improve.ledger import Ledger, LedgerError
from infermatrix_copilot.improve.reader import Unit, units_between
from infermatrix_copilot.trace_store import TraceStore, trace_context

T0 = 1_800_000_000.0   # 2027-01-15 (a Friday), far from any real data
PLAYBOOKS = Path(__file__).resolve().parents[1] / "playbooks"


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        self.t += 1.0
        return self.t


def _store(tmp_path, clock=None):
    return TraceStore(tmp_path / "traces", environ={}, clock=clock or Clock())


def _unit(store, uid, *, workflow="pr-review.agent.review_diff", playbook="pr-review", step="agent.review_diff",
          calls=(), tools=(), decisions=(), fingerprint="f" * 64, item="demo#1@abc"):
    """Write a unit: `calls` = list of model_call kwargs, `tools` = tool_call
    kwargs, `decisions` = decision kwargs."""
    ctx = {"run_id": uid.split(":")[0], "playbook": playbook, "step": step, "unit_id": uid}
    if workflow:
        ctx.update(workflow=workflow, fingerprint=fingerprint, item=item)
    with trace_context(**ctx):
        for c in calls:
            store.append("model_call", **c)
        for t in tools:
            store.append("tool_call", **t)
        for d in decisions:
            store.append("decision", **d)


def _call(reply="a fine reply", out_tokens=100, max_tokens=1000, stop="end_turn", error="", model="claude-sonnet-5",
          served=None, tool_calls="[]"):
    kw = {"model": {"role": "lens", "provider": "anthropic", "model": model, "served_model": served or model},
          "usage": {"input_tokens": 1000, "output_tokens": out_tokens}, "seconds": 2.0,
          "result": {"format": "messages/1", "stop_reason": stop, "max_tokens": max_tokens, "n_tools": 1},
          "inputs": {"system": "s", "messages": "[]"}}
    if error:
        kw["error"] = error
        kw["outputs"] = {}
    else:
        kw["outputs"] = {"reply": reply, "tool_calls": tool_calls}
    return kw


def _tool(tool="read_file", args='{"path": "a"}', ok=True, refused=False, out_of_scope=False):
    return {"inputs": {"args": args}, "outputs": {"result": "x"}, "seconds": 0.01,
            "result": {"tool": tool, "ok": ok, "refused": refused, "out_of_scope": out_of_scope, "bytes": 1}}


def _decision(review="## Review\n\nsome finding text that is long enough " * 2, **result):
    return {"outputs": {"review": review}, "result": {"status": "ok", **result}}


CLEAN = dict(calls=[_call()], tools=[_tool(), _tool(args='{"path": "b"}')], decisions=[_decision(findings=[{"x": 1}])])


# -- reader -----------------------------------------------------------------------------

def test_units_group_records_and_undeclared_workflows_fall_back(tmp_path):
    store = _store(tmp_path)
    _unit(store, "r1:review", **CLEAN)
    _unit(store, "r2:report", workflow="", calls=[_call()], step="report.final_summary")
    with trace_context(run_id="demo#9@sha", playbook="rb-review", step="review"):   # the bot: no unit_id
        store.append("decision", result={"status": "posted"})
    units = units_between(store, T0, T0 + 10_000)
    assert set(units) == {"r1:review", "r2:report", "demo#9@sha:review"}
    assert units["r1:review"].declared and units["r1:review"].workflow == "pr-review.agent.review_diff"
    assert not units["r2:report"].declared and units["r2:report"].workflow == "pr-review.report.final_summary"
    assert units["demo#9@sha:review"].workflow == "rb-review.review"
    assert len(units["r1:review"].tool_calls) == 2 and units["r1:review"].tokens() == (1000, 100)
    assert units_between(store, T0 + 10_000, T0 + 20_000, counted=set(units)) == {}   # counted once
    assert units_between(store, T0 + 10 * 86400, T0 + 11 * 86400) == {}                # beyond the lookback


def test_a_unit_spanning_the_cutoff_is_attributed_whole_to_the_cycle_it_ends_in(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    ctx = {"run_id": "r1", "playbook": "pr-review", "step": "agent.review_diff", "unit_id": "r1:review",
           "workflow": "pr-review.agent.review_diff", "fingerprint": "f" * 64, "item": "demo#1@abc"}
    with trace_context(**ctx):
        store.append("tool_call", **_tool())                       # before the cutoff
    cutoff = clock.t + 0.5
    with trace_context(**ctx):
        store.append("model_call", **_call(reply=""))              # the empty final, after the cutoff
        store.append("decision", **_decision())
    first = units_between(store, T0, cutoff)
    assert first == {}                                             # no decision yet, not quiet: deferred
    second = units_between(store, cutoff, clock.t + 10)
    unit = second["r1:review"]
    assert len(unit.records) == 3                                  # whole, including the pre-cutoff tool call
    assert "L03" in {f.lint for f in lints.run_lints(unit, store, lints.Baseline())}
    assert units_between(store, cutoff, clock.t + 10, counted={"r1:review"}) == {}   # counted once
    # a unit that never got a decision counts once it has been quiet for the grace period
    with trace_context(**{**ctx, "unit_id": "r2:review", "run_id": "r2"}):
        store.append("tool_call", **_tool())
    quiet = clock.t
    assert "r2:review" not in units_between(store, T0, quiet + 10)
    assert "r2:review" in units_between(store, T0, quiet + 3601)


def test_work_completed_before_the_window_is_not_pulled_in_by_the_lookback(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    _unit(store, "old:review", **CLEAN)                             # complete, long before the window
    since = clock.t + 3 * 3600                                      # a window starting three hours later
    _unit(store, "new:review", **CLEAN)                             # complete, still before the window
    clock.t = since - 1800                                          # half an hour before the window...
    with trace_context(run_id="d", playbook="pr-review", step="agent.review_diff", unit_id="d:review",
                       workflow="pr-review.agent.review_diff", fingerprint="f" * 64, item="demo#2@x"):
        store.append("tool_call", **_tool())                        # ...a unit still within its grace: deferred
    # the first cycle ever (empty cursor) sees only the window's own work plus what was deferred
    clock.t = since + 10
    _unit(store, "inwindow:review", **CLEAN)
    units = units_between(store, since, clock.t + 3700, deferred={"d:review"})   # the cursor's deferred set
    assert "old:review" not in units and "new:review" not in units    # completed before `since`: historical
    assert "inwindow:review" in units and "d:review" in units          # in the window / deferred into it
    # without the recorded deferred state (and no contiguous origin) nothing before `since` is pulled in
    assert set(units_between(store, since, clock.t + 3700)) == {"inwindow:review"}


def test_a_decision_that_becomes_visible_after_the_cutoff_is_still_counted_once(tmp_path, settings):
    """The reviewer's case: the call is visible at the cutoff, its decision
    is a torn trailing line; the first cycle defers the unit, the second must
    count it (once), never conclude it was complete before its window."""
    clock = Clock()
    store = _store(tmp_path, clock)
    st = _weekly_settings(settings, tmp_path)
    ctx = {"run_id": "torn", "playbook": "pr-review", "step": "agent.review_diff", "unit_id": "torn:review",
           "workflow": "pr-review.agent.review_diff", "fingerprint": "f" * 64, "item": "demo#1@abc"}
    with trace_context(**ctx):
        store.append("model_call", **_call())                       # at ~T0+1, visible
        decision = store.append("decision", **_decision())          # at ~T0+2, will be torn
    day = next((store.root / "records").glob("*.jsonl"))
    lines = day.read_text().splitlines()
    torn = lines[-1][:40]                                           # the decision line, unterminated
    day.write_text("\n".join(lines[:-1]) + "\n" + torn)
    cutoff = decision["at"] + 10
    r1 = cyc.run_cycle(store, st, tmp_path / "ledger", now=cutoff, since=T0, until=cutoff)
    assert r1["units"] == 0 and r1["deferred"] == ["torn:review"]
    day.write_text("\n".join(lines) + "\n")                        # the writer finished the line
    r2 = cyc.run_cycle(store, st, tmp_path / "ledger", now=cutoff + 100)
    assert r2["workflows"]["pr-review.agent.review_diff"]["units"] == 1 and r2["deferred"] == []
    r3 = cyc.run_cycle(store, st, tmp_path / "ledger", now=cutoff + 200)
    assert not any(d["units"] for d in r3["workflows"].values() if "pr-review" in d and False) and \
        r3["workflows"].get("pr-review.agent.review_diff", {}).get("units", 0) == 0
    # a whole unit that became visible late (both records torn at the cutoff) inside
    # contiguous coverage is counted too; before the engine's origin it is historical
    with trace_context(**{**ctx, "unit_id": "late:review", "run_id": "late"}):
        store.append("model_call", **_call())
        store.append("decision", **_decision())
    r4 = cyc.run_cycle(store, st, tmp_path / "ledger", now=cutoff + 300)
    assert r4["workflows"]["pr-review.agent.review_diff"]["units"] == 1
    cursor = json.loads((tmp_path / "ledger" / "cursor.json").read_text())
    assert cursor["origin"] == T0 and "late:review" in cursor["counted"]


def test_an_explicit_window_breaks_coverage_and_skipped_work_never_leaks_in(tmp_path, settings):
    clock = Clock()
    store = _store(tmp_path, clock)
    st = _weekly_settings(settings, tmp_path)
    t = T0
    _unit(store, "a:review", **CLEAN)                               # ~T0+1..: inside the first window
    cyc.run_cycle(store, st, tmp_path / "ledger", now=t + 100, since=t, until=t + 100)
    clock.t = t + 900
    _unit(store, "skipped:review", **CLEAN)                         # ~T0+901: between the windows
    clock.t = t + 1050
    _unit(store, "b:review", **CLEAN)                               # inside the explicit window
    r2 = cyc.run_cycle(store, st, tmp_path / "ledger", now=t + 1100, since=t + 1000, until=t + 1100)
    review_ids = lambda r: {i for i in r["counted_ids"] if i.endswith(":review")}   # noqa: E731
    assert review_ids(r2) == {"b:review"}
    clock.t = t + 1150
    _unit(store, "c:review", **CLEAN)
    r3 = cyc.run_cycle(store, st, tmp_path / "ledger", now=t + 1200)        # resumes contiguously
    assert review_ids(r3) == {"c:review"}                             # the skipped unit stays skipped
    cursor = json.loads((tmp_path / "ledger" / "cursor.json").read_text())
    assert cursor["origin"] == t + 1000 and "skipped:review" not in cursor["counted"]


def test_a_legacy_cursor_without_deferred_state_keeps_pending_units(tmp_path, settings):
    clock = Clock()
    store = _store(tmp_path, clock)
    st = _weekly_settings(settings, tmp_path)
    ctx = {"run_id": "p", "playbook": "pr-review", "step": "agent.review_diff", "unit_id": "p:review",
           "workflow": "pr-review.agent.review_diff", "fingerprint": "f" * 64, "item": "demo#1@abc"}
    _unit(store, "done-before:review", **CLEAN)                     # complete before the old cutoff: was counted
    with trace_context(**ctx):
        store.append("model_call", **_call())                       # pending at the old cycle's cutoff
    old_until = clock.t + 1
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / "cursor.json").write_text(json.dumps(
        {"until": old_until, "last_run_at": old_until, "cycles": 1}))    # the pre-deferred, pre-counted format
    report = cyc.run_cycle(store, st, tmp_path / "ledger", now=old_until + 4000)
    assert report["cursor_migrated"]
    assert "p:review" in report["counted_ids"]                      # pending then, quiet past the grace now
    assert "done-before:review" not in report["counted_ids"]        # never replayed
    cursor = json.loads((tmp_path / "ledger" / "cursor.json").read_text())
    assert "deferred" in cursor and cursor["origin"] == old_until
    again = cyc.run_cycle(store, st, tmp_path / "ledger", now=old_until + 8000)
    assert not again["cursor_migrated"] and "p:review" not in again["counted_ids"]


def test_l07_respects_model_aliases(tmp_path, settings):
    from types import SimpleNamespace

    store = _store(tmp_path)
    _unit(store, "r1:review", calls=[_call(model="sonnet", served="claude-sonnet-5-20260101")], decisions=[_decision()])
    unit = units_between(store, T0, T0 + 10_000)["r1:review"]
    assert "L07" in {f.lint for f in lints.run_lints(unit, store, lints.Baseline())}
    aliased = SimpleNamespace(model_aliases={"sonnet": "claude-sonnet-5-20260101"},
                              token_price_in_per_mtok=0.0, token_price_out_per_mtok=0.0)
    assert "L07" not in {f.lint for f in lints.run_lints(unit, store, lints.Baseline(settings=aliased))}


def test_cycles_count_a_late_finishing_unit_exactly_once(tmp_path, settings):
    clock = Clock()
    store = _store(tmp_path, clock)
    st = _weekly_settings(settings, tmp_path)
    ctx = {"run_id": "late", "playbook": "pr-review", "step": "agent.review_diff", "unit_id": "late:review",
           "workflow": "pr-review.agent.review_diff", "fingerprint": "f" * 64, "item": "demo#1@abc"}
    with trace_context(**ctx):
        store.append("model_call", **_call())                      # running at the first cutoff
    cut1 = clock.t + 1
    r1 = cyc.run_cycle(store, st, tmp_path / "ledger", now=cut1, since=T0, until=cut1)
    assert "pr-review.agent.review_diff" not in r1["workflows"]     # deferred, not counted
    with trace_context(**ctx):
        store.append("decision", **_decision())                     # finishes later
    r2 = cyc.run_cycle(store, st, tmp_path / "ledger", now=clock.t + 1)
    assert r2["workflows"]["pr-review.agent.review_diff"]["units"] == 1
    r3 = cyc.run_cycle(store, st, tmp_path / "ledger", now=clock.t + 1)
    assert "pr-review.agent.review_diff" not in {w for w, d in r3["workflows"].items() if d["units"]}
    assert "late:review" in json.loads((tmp_path / "ledger" / "cursor.json").read_text())["counted"]


def test_reader_tolerates_a_partially_written_trailing_line(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    _unit(store, "r1:review", **CLEAN)
    day = next((store.root / "records").glob("*.jsonl"))
    with day.open("a", encoding="utf-8") as fh:
        fh.write('{"schema": "trace/1", "id": "torn", "kind": "decision", "at": %f, "conte' % (clock.t + 1))
    units = units_between(store, T0, clock.t + 100)
    assert set(units) == {"r1:review"}                             # the torn line is deferred, nothing aborts


# -- lints --------------------------------------------------------------------------------

def _findings(store, uid, baseline=None):
    unit = units_between(store, T0, T0 + 10_000)[uid]
    return {f.lint for f in lints.run_lints(unit, store, baseline or lints.Baseline())}


def test_clean_unit_has_no_findings(tmp_path):
    store = _store(tmp_path)
    _unit(store, "r1:review", **CLEAN)
    assert _findings(store, "r1:review") == set()


@pytest.mark.parametrize("lint_id,kwargs", [
    ("L01", dict(calls=[_call(out_tokens=1000, max_tokens=1000, stop="max_tokens")], decisions=[_decision()])),
    ("L02", dict(calls=[_call()], decisions=[_decision(), {"result": {"type": "budget_exhausted"}}])),
    ("L03", dict(calls=[_call(reply="")], tools=[_tool()], decisions=[_decision()])),
    ("L04", dict(calls=[_call()], decisions=[_decision(), {"result": {"type": "contract_reject", "reason": "missing field"}}])),
    ("L06", dict(calls=[_call()], decisions=[_decision(review=("A" * 250 + "\n\n" + "B" * 250 + "\n\n" + "A" * 250))])),
    ("L07", dict(calls=[_call(served="other-model")], decisions=[_decision()])),
    ("L08", dict(calls=[_call()], tools=[_tool(args=f'{{"p": {i}}}') for i in range(6)], decisions=[_decision(findings=[])])),
    ("L09", dict(calls=[_call()], decisions=[_decision(), {"result": {"type": "cap", "dropped": 2, "cap": 5}}])),
    ("L10", dict(calls=[_call()], decisions=[_decision(review="anchor unresolved at vllm/x.py:? " * 10)])),
    ("L12", dict(calls=[_call()], tools=[_tool(tool="write_file", ok=False, refused=True)], decisions=[_decision()])),
    ("L13", dict(calls=[_call(error="HTTP 402 Insufficient Balance")], decisions=[_decision()])),
    ("L15", dict(calls=[_call()], tools=[_tool()] * 3, decisions=[_decision()])),
    ("L16", dict(calls=[_call()], decisions=[_decision(), {"result": {"type": "evidence_cap", "dropped_ratio": 0.3}}])),
    ("L17", dict(calls=[_call()], tools=[_tool()], decisions=[_decision(review="I verified the fix and the tests pass. " * 10)])),
])
def test_each_lint_fires_on_its_fixture_only(tmp_path, lint_id, kwargs):
    store = _store(tmp_path)
    _unit(store, "r1:review", **kwargs)
    found = _findings(store, "r1:review")
    assert lint_id in found, (lint_id, found)
    unexpected = found - {lint_id, "L14"} - ({"L17"} if lint_id == "L03" else set()) - ({"L12"} if lint_id == "L03" else set())
    assert not (unexpected - {"L08"}), (lint_id, found)


def test_l05_l11_use_the_baseline_and_l14_completeness(tmp_path):
    store = _store(tmp_path)
    _unit(store, "r1:review", calls=[_call(error="JSON parse failed"), _call(error="json decode"), _call()],
          decisions=[_decision()])
    assert "L05" in _findings(store, "r1:review", lints.Baseline(parse_failure_rate=0.05))
    assert "L05" not in _findings(store, "r1:review", lints.Baseline(parse_failure_rate=0.5))
    _unit(store, "r2:review", calls=[_call(out_tokens=200_000)], decisions=[_decision()])
    base = lints.Baseline(usd=[0.01] * 10, seconds=[2.0] * 10)
    found = _findings(store, "r2:review", base)
    assert "L11" in found
    # L10 with question-mark anchors only, and the price scale follows the settings the baseline used
    _unit(store, "r4:review", calls=[_call()], decisions=[_decision(review="see vllm/x.py:? and y.py:? " * 10)])
    assert "L10" in _findings(store, "r4:review")
    from types import SimpleNamespace
    cheap = SimpleNamespace(token_price_in_per_mtok=0.001, token_price_out_per_mtok=0.001)
    unit = units_between(store, T0, T0 + 10_000)["r2:review"]
    assert lints.unit_usd(unit, cheap) < lints.unit_usd(unit, None)
    priced = lints.Baseline(usd=[lints.unit_usd(unit, cheap)] * 10, settings=cheap)
    assert "L11" not in {f.lint for f in lints.run_lints(unit, store, priced)}   # same scale: no outlier
    assert "L11" not in _findings(store, "r2:review", lints.Baseline())                  # no history: no outlier
    # completeness: a declared unit with model calls but no terminal decision, and a call with neither reply nor error
    _unit(store, "r3:review", calls=[{"model": {"model": "m"}, "usage": {}, "seconds": None,
                                      "inputs": {"system": "s"}, "outputs": {}, "result": {}}])
    unit = units_between(store, T0, T0 + 10_000)["r3:review"]
    f = [x for x in lints.run_lints(unit, store, lints.Baseline()) if x.lint == "L14"]
    assert f and "neither a reply nor an error" in f[0].detail and "no terminal decision" in f[0].detail
    assert lints.catalogue()[0]["id"] == "L01" and len(lints.catalogue()) == 17


def test_quarantined_units_leave_the_statistics(tmp_path):
    store = _store(tmp_path)
    _unit(store, "r1:review", calls=[_call(error="429 rate limit")], decisions=[_decision()])
    unit = units_between(store, T0, T0 + 10_000)["r1:review"]
    findings = lints.run_lints(unit, store, lints.Baseline())
    assert any(f.lint == "L13" and f.quarantine for f in findings)


# -- ledger ----------------------------------------------------------------------------------

def test_ledger_proposal_state_machine_and_stale_sweep(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    ledger = Ledger(tmp_path / "ledger", store, clock=clock)
    p = ledger.open_proposal("w.s", tier=1, lint="L01", claim="L01 worsened", evidence=["id1"])
    again = ledger.open_proposal("w.s", tier=1, lint="L01", claim="L01 worsened more", evidence=["id2"])
    assert again.id == p.id and again.evidence == ["id1", "id2"]                   # one live proposal per lint
    with pytest.raises(LedgerError, match="not a valid transition"):
        ledger.transition("w.s", p.id, "supported")
    ledger.transition("w.s", p.id, "experiment-registered", experiment_id="exp-1")
    ledger.transition("w.s", p.id, "supported")
    ledger.transition("w.s", p.id, "landed", human=True, issue="https://x/1")
    wl = ledger.load("w.s")
    assert wl.proposals[0].state == "landed" and wl.proposals[0].issue == "https://x/1"
    assert [h["event"] for h in wl.proposals[0].history] == ["opened", "refreshed", "experiment-registered", "supported", "landed"]
    with pytest.raises(LedgerError):
        ledger.transition("w.s", p.id, "open")
    q = ledger.open_proposal("w.s", tier=2, stage="S2", claim="seen but not raised", evidence=["id3"])
    assert not ledger.channel_dead("w.s", clock.t)
    later = clock.t + 31 * 24 * 3600
    assert ledger.stale_sweep("w.s", later) == [q.id]
    assert ledger.channel_dead("w.s", later)
    ledger.human_touch("w.s", q.id)
    assert not ledger.channel_dead("w.s", clock.t)
    kinds = [r["result"]["type"] for r in store.query(kind="decision", playbook="workflow-improve")]
    assert kinds.count("proposal_state") >= 5
    assert ledger.workflows() == ["w.s"]


# -- the cycle ---------------------------------------------------------------------------------

def _weekly_settings(settings, tmp_path, **extra):
    return settings.model_copy(update={"improve_enabled": True, "trace_store_root": str(tmp_path / "traces"),
                                       "improve_ledger_dir": str(tmp_path / "ledger"), **extra})


def test_cycle_runs_lints_updates_ledger_opens_tier1_proposals_and_advances_the_cursor(tmp_path, settings):
    clock = Clock()
    store = _store(tmp_path, clock)
    st = _weekly_settings(settings, tmp_path)
    for i in range(4):
        _unit(store, f"w1r{i}:review", **CLEAN)
    first_end = clock.t + 1
    report = cyc.run_cycle(store, st, tmp_path / "ledger", now=first_end, since=T0, until=first_end)
    wf = report["workflows"]["pr-review.agent.review_diff"]
    assert wf["units"] == 4 and wf["lints"] == {} and report["proposals_opened"] == []
    assert (tmp_path / "ledger" / "cursor.json").exists()
    assert list((tmp_path / "ledger" / "reports").glob("cycle-*.md"))
    # week 2: L01 hits three of four units -> a Tier 1 proposal
    for i in range(3):
        _unit(store, f"w2r{i}:review", calls=[_call(out_tokens=1000, max_tokens=1000, stop="max_tokens")],
              decisions=[_decision()])
    _unit(store, "w2r3:review", **CLEAN)
    _unit(store, "w2bad:review", calls=[_call(error="HTTP 402 Insufficient Balance")], decisions=[_decision()])
    second_end = clock.t + 1
    report2 = cyc.run_cycle(store, st, tmp_path / "ledger", now=second_end)     # window from the cursor
    assert report2["since"] == first_end
    assert report2["units"] == 6                     # 5 review units + the previous cycle's own records
    wf2 = report2["workflows"]["pr-review.agent.review_diff"]
    assert wf2["units"] == 5 and wf2["quarantined"] == 1 and wf2["lints"]["L01"]["units"] == 3
    assert wf2["lints"]["L01"]["rate"] == 0.75                                       # 3 of 4 counted units
    assert [p["lint"] for p in report2["proposals_opened"]] == ["L01"]
    ledger = Ledger(tmp_path / "ledger")
    wl = ledger.load("pr-review.agent.review_diff")
    assert len(wl.cycles) == 2 and wl.proposals[0].lint == "L01" and wl.proposals[0].state == "open"
    assert "L01" in wl.proposals[0].claim and len(wl.proposals[0].evidence) >= 3
    assert len(wl.baseline["usd"]) == 8                                                 # quarantined unit excluded
    cycles = [r for r in store.query(kind="decision", playbook="workflow-improve") if r["result"]["type"] == "cycle"]
    assert len(cycles) == 2 and cycles[1]["result"]["proposals_opened"] == 1
    assert json.loads(store.blob(cycles[1]["inputs"]["report"]))["units"] == 6
    # week 3: the engine's own cycle records are units of the engine workflow, and a
    # steady L01 rate opens no second proposal (one live proposal per lint)
    _unit(store, "w3r0:review", calls=[_call(out_tokens=1000, max_tokens=1000, stop="max_tokens")], decisions=[_decision()])
    report3 = cyc.run_cycle(store, st, tmp_path / "ledger", now=clock.t + 1)
    assert any(w.startswith("workflow-improve") for w in report3["workflows"])   # self-enrolled, Tier 1
    assert report3["proposals_opened"] == []


def test_inactive_workflows_are_still_swept_and_reported(tmp_path, settings):
    clock = Clock()
    store = _store(tmp_path, clock)
    st = _weekly_settings(settings, tmp_path)
    ledger = Ledger(tmp_path / "ledger", store, clock=clock)
    ledger.open_proposal("old.workflow", tier=1, lint="L01", claim="from long ago", evidence=["x"])
    later = clock.t + 31 * 24 * 3600
    report = cyc.run_cycle(store, st, tmp_path / "ledger", now=later, since=later - 7 * 86400, until=later)
    inactive = report["workflows"]["old.workflow"]
    assert inactive["inactive"] and inactive["units"] == 0 and inactive["stale"]
    assert inactive["channel_dead"] and ledger.load("old.workflow").proposals[0].state == "stale"
    md = next((tmp_path / "ledger" / "reports").glob("cycle-*.md")).read_text()
    assert "CHANNEL MAY BE DEAD" in md and "old.workflow" in md


def test_cycles_and_ledger_writes_are_serialized_across_writers(tmp_path, settings):
    import threading

    clock = Clock()
    store = _store(tmp_path, clock)
    st = _weekly_settings(settings, tmp_path)
    for i in range(3):
        _unit(store, f"r{i}:review", **CLEAN)
    # two cycles at once on one ledger directory: exactly one runs
    results: list = []
    barrier = threading.Barrier(2, timeout=5)

    def run():
        barrier.wait()
        try:
            results.append(cyc.run_cycle(store, st, tmp_path / "ledger", now=clock.t + 1, since=T0, until=clock.t + 1))
        except cyc.CycleRefused as exc:
            results.append(exc)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(isinstance(r, dict) for r in results) == 1 and sum(isinstance(r, cyc.CycleRefused) for r in results) == 1
    assert "another cycle is running" in str(next(r for r in results if isinstance(r, cyc.CycleRefused)))
    # concurrent proposal writers on one workflow ledger: nothing is lost
    ledger = Ledger(tmp_path / "ledger", store, clock=clock)
    start = threading.Barrier(8, timeout=5)

    def propose(i):
        start.wait()
        Ledger(tmp_path / "ledger", store, clock=clock).open_proposal(
            "w.s", tier=2, stage=f"S{i}", claim=f"claim {i}", evidence=[f"e{i}"])
    writers = [threading.Thread(target=propose, args=(i,)) for i in range(8)]
    for t in writers:
        t.start()
    for t in writers:
        t.join()
    assert sorted(p.stage for p in ledger.load("w.s").proposals) == [f"S{i}" for i in range(8)]
    assert not list((tmp_path / "ledger").glob("*.tmp"))


def test_kill_switch_holds_and_weekly_slot(tmp_path, settings):
    store = _store(tmp_path)
    off = settings.model_copy(update={"improve_enabled": False})
    with pytest.raises(cyc.CycleRefused, match="kill switch"):
        cyc.run_cycle(store, off, tmp_path / "ledger", now=T0 + 10)
    assert cyc.maybe_run_weekly(store, off, tmp_path / "ledger", now=T0 + 10) is None
    on = _weekly_settings(settings, tmp_path)
    Ledger(tmp_path / "ledger").set_hold("w.s", "maintainer: hold")
    report = cyc.run_cycle(store, on, tmp_path / "ledger", now=T0 + 10, since=T0, until=T0 + 10)
    assert report["holds"] == {"w.s": "maintainer: hold"}
    # the slot: Monday 05:00 UTC
    monday_0430 = dt.datetime(2027, 1, 18, 4, 30, tzinfo=dt.timezone.utc).timestamp()
    monday_0501 = dt.datetime(2027, 1, 18, 5, 1, tzinfo=dt.timezone.utc).timestamp()
    sunday = dt.datetime(2027, 1, 17, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    assert not cyc.is_due(monday_0430, sunday, weekday=0, hour=5)
    assert cyc.is_due(monday_0501, sunday, weekday=0, hour=5)
    assert not cyc.is_due(monday_0501 + 3600, monday_0501, weekday=0, hour=5)     # already ran this slot
    assert cyc.is_due(monday_0501 + 7 * 86400, monday_0501, weekday=0, hour=5)
    first = cyc.maybe_run_weekly(store, on, tmp_path / "ledger", now=monday_0501)
    assert first is not None and cyc.maybe_run_weekly(store, on, tmp_path / "ledger", now=monday_0501 + 60) is None


def test_scheduler_tick_runs_the_weekly_cycle_in_isolation(tmp_path, settings):
    from types import SimpleNamespace

    from infermatrix_copilot.kb_service.scheduler import Scheduler

    store = _store(tmp_path)
    monday = dt.datetime(2027, 1, 18, 5, 1, tzinfo=dt.timezone.utc).timestamp()
    rt = SimpleNamespace(traces=store, state_dir=tmp_path / "state", clock=lambda: monday,
                         settings=_weekly_settings(settings, tmp_path))
    sched = Scheduler(rt)
    report = sched._improve_cycle()
    assert report is not None and report["units"] == 0
    assert (tmp_path / "ledger" / "cursor.json").exists()          # the SAME directory the CLI/playbook use
    assert cyc.ledger_dir_for(settings.model_copy(update={"improve_ledger_dir": ""})) == cyc.DEFAULT_LEDGER_DIR
    assert sched._improve_cycle() is None                                            # same slot: once
    rt.settings = settings.model_copy(update={"improve_enabled": False})
    assert sched._improve_cycle() is None


# -- task kind, playbook, steps, CLI --------------------------------------------------------------

def test_workflow_improve_is_a_read_only_l2_kind_with_a_vetted_playbook(tmp_path, settings):
    from infermatrix_copilot.engine.planner import Planner
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.playbooks.store import PlaybookStore
    from infermatrix_copilot.task_spec import KIND_TIER, READ_ONLY_KINDS, TaskSpec

    assert KIND_TIER["workflow_improve"] == "L2" and "workflow_improve" in READ_ONLY_KINDS
    spec = TaskSpec(kind="workflow_improve", repo="demo")
    assert spec.tier == "L2" and spec.read_only
    registry = register_builtin_steps(StepRegistry())
    # design §13.6: every improve.* step is read-only except the publisher
    for name in ("improve.mode", "improve.preflight", "improve.sync", "improve.lint", "improve.experiments",
                 "improve.forensics", "improve.ledger", "improve.stage_items"):
        assert registry.get(name).risk == "read", name
    assert registry.get("improve.publish").risk == "push"
    store = PlaybookStore(PLAYBOOKS, registry)
    store.load()
    pb = store.get("workflow-improve")
    assert pb is not None and [s.step for s in pb.steps] == ["improve.mode", "improve.preflight", "improve.sync",
                                                              "improve.coordinate", "improve.lint", "improve.experiments", "improve.forensics",
                                                              "improve.ledger", "improve.publish", "improve.evolve_publish", "report.final_summary"]
    # the meta mode gates every cycle step off; forensics and the report always run
    assert [s.when for s in pb.steps if s.step not in ("improve.mode", "improve.forensics", "report.final_summary")] \
        == ["not improve_meta", "not improve_meta", "improve_coordinated", "improve_legacy", "improve_legacy",
            "not improve_meta", "not improve_meta", "improve_coordinated"]
    resolution = Planner(store, registry).resolve(spec)
    assert resolution.mode == "reuse" and resolution.playbook.name == "workflow-improve"


def test_steps_run_a_dry_run_cycle_end_to_end(tmp_path, settings):
    from execution_helpers import application_executor as Executor
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.playbooks.store import PlaybookStore
    from infermatrix_copilot.run_trace import RunTrace

    clock = Clock()
    store = _store(tmp_path, clock)
    _unit(store, "r1:review", **CLEAN)
    st = _weekly_settings(settings, tmp_path)
    registry = register_builtin_steps(StepRegistry())
    pbs = PlaybookStore(PLAYBOOKS, registry)
    pbs.load()
    run_dir = tmp_path / "run-x"
    ex = Executor(registry, st, run_dir=run_dir, trace=RunTrace(run_dir / "run_trace.jsonl"))
    state = {"task_spec": {"kind": "workflow_improve", "repo": "demo", "params": {"since": T0, "until": clock.t + 5}}}
    outcome = asyncio.run(ex.run(pbs.get("workflow-improve"), state))
    assert outcome.status == "done", outcome.blocked_reason
    assert state["improve_cycle"]["units"] == 1 and (tmp_path / "ledger" / "cursor.json").exists()
    off = st.model_copy(update={"improve_enabled": False})
    ex2 = Executor(registry, off, run_dir=tmp_path / "run-y", trace=RunTrace(tmp_path / "run-y" / "t.jsonl"))
    blocked = asyncio.run(ex2.run(pbs.get("workflow-improve"), {"task_spec": {"kind": "workflow_improve", "repo": "demo"}}))
    assert blocked.status == "blocked" and "kill switch" in blocked.blocked_reason


def test_cli_cycle_ledger_and_lints(tmp_path, monkeypatch, capsys):
    from infermatrix_copilot.improve import cli

    store = _store(tmp_path)
    _unit(store, "r1:review", **CLEAN)
    monkeypatch.setenv("TRACE_STORE_ROOT", str(store.root))
    monkeypatch.setenv("IMPROVE_ENABLED", "1")
    assert cli.main(["lints"]) == 0 and json.loads(capsys.readouterr().out)[0]["id"] == "L01"
    assert cli.main(["cycle", "--ledger-dir", str(tmp_path / "ledger"), "--since", str(T0), "--until", str(T0 + 10_000)]) == 0
    assert json.loads(capsys.readouterr().out)["units"] == 1
    assert cli.main(["ledger", "--ledger-dir", str(tmp_path / "ledger")]) == 0
    assert "pr-review.agent.review_diff" in json.loads(capsys.readouterr().out)
    monkeypatch.setenv("IMPROVE_ENABLED", "0")
    assert cli.main(["cycle", "--ledger-dir", str(tmp_path / "ledger")]) == 1
    assert cli.main(["cycle", "--ledger-dir", str(tmp_path / "ledger"), "--force"]) == 0
    # the module entry point reaches the helpers defined after main()
    import subprocess, sys
    proc = subprocess.run([sys.executable, "-m", "infermatrix_copilot.improve.cli", "ledger",
                           "--ledger-dir", str(tmp_path / "ledger")], capture_output=True, text=True,
                          env={**__import__("os").environ, "PYTHONPATH": str(Path(cli.__file__).parents[2])})
    assert proc.returncode == 0 and "pr-review.agent.review_diff" in proc.stdout
