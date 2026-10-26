"""Meta-improvement engine P4: proposal publication through omni-maintainer's
outbox (linter, issue template, acks/inbox sync, the double-gated publish
step), Tier 2 proposals from the punch list, the engine's self-declaration
and the meta-benchmark adapter (design §9.2, §11.1, §11.2)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from infermatrix_copilot.improve import cycle as cyc
from infermatrix_copilot.improve import experiments as exps
from infermatrix_copilot.improve import meta, publish as pub
from infermatrix_copilot.improve.adapters import scores_from
from infermatrix_copilot.improve.adapters.meta_bench import MetaBenchAdapter
from infermatrix_copilot.improve.ledger import Ledger, LedgerError
from infermatrix_copilot.improve.reader import units_between
from infermatrix_copilot.trace_store import TraceStore, bind_store, trace_context

from test_improve_p1 import CLEAN, T0, Clock, _call, _store, _tool, _unit  # noqa: E402
from test_improve_p2 import REVIEW, _curated, _gt  # noqa: E402

SECRET_REVIEW = ("## Review\n\nvllm/config.py:66 [major] — trust_remote_code should default to False.\n"
                 "contact: someone@example.com token ghp_" + "A" * 30 + "\n" + "x" * 400 + "\n")


def _review_unit(store, uid="r1:review", review=SECRET_REVIEW, item="demo#1@abc"):
    _unit(store, uid, calls=[_call(reply="looked at config.py")], tools=[_tool()],
          decisions=[{"outputs": {"review": review}, "result": {"status": "ok", "findings": []}}], item=item)
    return units_between(store, T0, T0 + 10_000)[uid]


# -- citations and the linter (rule 4) ----------------------------------------------------------

def test_citations_are_verbatim_redacted_and_recoverable(tmp_path):
    store = _store(tmp_path)
    unit = _review_unit(store)
    decision = unit.decisions[-1]
    c = pub.excerpt_for(store, decision["id"])
    assert c.blob == decision["outputs"]["review"] and c.source == "outputs.review"
    assert "ghp_" not in "\n".join(c.excerpt) and "@example.com" not in "\n".join(c.excerpt)
    assert "[redacted]" in "\n".join(c.excerpt)
    long = [ln for ln in c.excerpt if ln.endswith("…")]
    assert long and len(long[0]) == pub.EXCERPT_MAX_LINE_CHARS + 1      # a cut line stays a prefix of the blob line
    assert pub.verify_excerpt(store, c) == ""
    tampered = pub.Citation(c.record_id, c.blob, c.source, c.excerpt + ["a line the blob never had"])
    assert "not a line of the cited blob" in pub.verify_excerpt(store, tampered)
    too_long = pub.Citation(c.record_id, c.blob, c.source, c.excerpt * 30)
    assert "longer than" in pub.verify_excerpt(store, too_long)
    # a record without a text blob cites by id only
    tool = unit.tool_calls[0]
    plain = pub.excerpt_for(store, tool["id"])
    assert plain.blob and plain.source == "outputs.result"               # tool results are blobs too
    call = unit.model_calls[0]
    assert pub.excerpt_for(store, call["id"]).source == "outputs.reply"  # outputs before inputs, reply preferred
    with pytest.raises(KeyError):
        pub.excerpt_for(store, "no-such-record")
    # a whole private-key block is redacted, not just its header; a block cut before its END takes the rest
    key = ("## Review\n-----BEGIN OPENSSH PRIVATE KEY-----\n"
           "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\nQyNTUxOQAAACBv\n"
           "-----END OPENSSH PRIVATE KEY-----\nafter the key\n")
    keyed = _review_unit(store, uid="k1:review", review=key, item="demo#2@abc")
    c = pub.excerpt_for(store, keyed.decisions[-1]["id"])
    # (the trace store already redacts key material at write time — "[REDACTED]" — the publisher is the second layer)
    assert [c.excerpt[0], c.excerpt[1].lower(), c.excerpt[2]] == ["## Review", "[redacted]", "after the key"]
    assert pub.verify_excerpt(store, c) == ""
    assert "b3BlbnNzaC" not in pub.redact(key) and pub.redact("x\n-----BEGIN RSA PRIVATE KEY-----\nMIIE\nMIIF") == "x\n[redacted]"


def test_linter_refuses_what_cannot_be_checked(tmp_path):
    store = _store(tmp_path)
    unit = _review_unit(store)
    ids = [r["id"] for r in unit.records]
    ledger = Ledger(tmp_path / "ledger", store, clock=Clock())
    good = ledger.open_proposal("pr-review.agent.review_diff", tier=1, lint="L01", claim="L01 worsened", evidence=ids[:3],
                                rate_at_open=0.4)
    assert pub.lint_proposal(store, good, pub.cite(store, good)) == []
    thin = ledger.open_proposal("pr-review.agent.review_diff", tier=1, lint="L02", claim="L02 worsened", evidence=ids[:2])
    assert any("at least 3" in p for p in pub.lint_proposal(store, thin, pub.cite(store, thin)))
    bogus = ledger.open_proposal("pr-review.agent.review_diff", tier=2, stage="S4", claim="c", loss=2,
                                 evidence=["nope-1", "nope-2"])
    problems = pub.lint_proposal(store, bogus, pub.cite(store, bogus))
    assert any("unresolvable record id nope-1" in p for p in problems) and "no citation" in problems
    s0 = ledger.open_proposal("pr-review.agent.review_diff", tier=2, stage="S0", claim="c", loss=1, evidence=ids[:1])
    assert any("S1-S9" in p for p in pub.lint_proposal(store, s0, pub.cite(store, s0)))
    nothing = ledger.open_proposal("pr-review.agent.review_diff", tier=2, stage="S3", claim="", loss=0, evidence=ids[:1])
    problems = pub.lint_proposal(store, nothing, pub.cite(store, nothing))
    assert "empty claim" in problems and any("quantifies" in p for p in problems)
    foreign = pub.Citation(ids[0], unit.decisions[-1]["outputs"]["review"], "outputs.review", ["## Review"])
    assert any("not in the proposal's evidence" in p for p in pub.lint_proposal(store, bogus, [foreign]))
    # the rendered body: template sections, marker, labels, redaction
    tier2 = ledger.open_proposal("pr-review.agent.review_diff", tier=2, stage="S2", claim="seen but not raised", loss=3,
                                 evidence=[ids[2], ids[0]], suggestion={"metric": "recall_gold", "n_required": 12,
                                                                        "covers": "lens prompts", "items": ["demo#1@abc"]})
    body = pub.render_issue(tier2, pub.cite(store, tier2), ledger_ref="~/.infermatrix-copilot/improve")
    assert pub.lint_body(body, tier2) == []
    for needle in ("**Claim.** seen but not raised", "**Stage.** `S2`", "3 missed gold entries", "## Evidence",
                   f"record `{ids[2]}`", "blob `sha256:", "## Suggested experiment", "items required: 12",
                   "--proposal " + tier2.id, "## Ledger", pub.marker(tier2), "[redacted]"):
        assert needle in body, needle
    assert "ghp_" not in body and "@example.com" not in body
    assert pub.labels_for(tier2) == ["improve:proposal", "improve:pr-review.agent.review_diff", "improve:tier2", "improve:open"]
    assert pub.title_for(tier2) == "[improve] pr-review.agent.review_diff: seen but not raised"
    proxy = ledger.open_proposal("rb-review.review", tier=2, stage="S6", claim="calibration", loss=1, evidence=ids[:1], proxy=True)
    pbody = pub.render_issue(proxy, pub.cite(store, proxy), ledger_ref="x")
    assert "**proxy** · **descriptive-only**" in pbody and "improve:proxy" in pub.labels_for(proxy)
    assert pub.lint_body(pbody.replace("descriptive-only", ""), proxy) == ["a proxy proposal must be labelled proxy and descriptive-only"]
    assert pub.lint_body("no marker here", tier2) == ["body lacks the proposal marker"]


# -- the outbox: plan, publish, sync ---------------------------------------------------------------

def _seed(tmp_path, clock):
    store = _store(tmp_path, clock)
    unit = _review_unit(store)
    ids = [r["id"] for r in unit.records]
    ledger = Ledger(tmp_path / "ledger", store, clock=clock)
    t1 = ledger.open_proposal("pr-review.agent.review_diff", tier=1, lint="L01", claim="L01 worsened", evidence=ids[:3],
                              rate_at_open=0.4)
    t2 = ledger.open_proposal("pr-review.agent.review_diff", tier=2, stage="S2", claim="seen but not raised", loss=2,
                              evidence=ids[:2], suggestion={"metric": "recall_gold", "n_required": 8, "covers": "x"})
    bad = ledger.open_proposal("pr-review.agent.review_diff", tier=2, stage="S5", claim="assembly", loss=1, evidence=["nope"])
    return store, ledger, t1, t2, bad


def test_publish_writes_actions_once_and_sync_applies_acks_and_observations(tmp_path):
    clock = Clock()
    store, ledger, t1, t2, bad = _seed(tmp_path, clock)
    outbox = pub.ProposalOutbox(tmp_path / "outbox")
    with pytest.raises(pub.PublishError, match="no proposal repository"):
        pub.publish(ledger, store, outbox, repo="", now=clock.t)
    preview = pub.publish(ledger, store, outbox, repo="o/r", now=clock.t, dry_run=True)
    assert [a["proposal"] for a in preview["actions"]] == [t1.id, t2.id] and list(preview["refused"]) == [bad.id]
    assert preview["written"] == [] and not (tmp_path / "outbox").exists()
    report = pub.publish(ledger, store, outbox, repo="o/r", now=clock.t)
    assert len(report["written"]) == 2 and len(outbox.pending()) == 2
    action = outbox.pending()[0]
    assert action["protocol"] == pub.PROTOCOL and action["action"] == "open" and action["repo"] == "o/r"
    assert action["labels"][:2] == ["improve:proposal", "improve:pr-review.agent.review_diff"] and action["marker"] in action["body"]
    assert action["title"].startswith("[improve] pr-review.agent.review_diff: L01 worsened")
    kinds = [r["result"]["type"] for r in store.query(kind="decision", playbook="workflow-improve")]
    assert kinds.count("proposal_publish") == 2 and kinds.count("proposal_lint_failed") == 1   # the dry run records nothing
    # a pending action is not re-issued while it awaits its ack
    again = pub.publish(ledger, store, outbox, repo="o/r", now=clock.t)
    assert again["actions"] == [] and sorted(again["waiting"]) == sorted([t1.id, t2.id])
    # the routine answers: one ok, one dry-run (issues_live off), one failure for an unknown proposal
    a1, a2 = (a for a in outbox.pending() if a["proposal"] == t1.id), (a for a in outbox.pending() if a["proposal"] == t2.id)
    a1, a2 = next(a1), next(a2)
    (outbox.acks_dir).mkdir()
    (outbox.acks_dir / f"{a1['id']}.json").write_text(json.dumps({"action": "open", "proposal": t1.id, "workflow": t1.workflow,
                                                                   "ok": True, "issue": 12, "url": "https://x/12",
                                                                   "state": "open", "at": clock.t}))
    (outbox.acks_dir / f"{a2['id']}.json").write_text(json.dumps({"action": "open", "proposal": t2.id, "ok": True, "dry_run": True}))
    (outbox.acks_dir / "ghost.json").write_text(json.dumps({"action": "open", "proposal": "prop-0-zz", "ok": False}))
    synced = pub.sync(ledger, outbox, store, now=clock.t)
    assert synced["acked"] == [t1.id] and synced["dry_run_acks"] == [t2.id] and synced["unknown"] == ["ghost.json"]
    assert outbox.acks() == [] and outbox.pending() == []                       # consumed, with their action files
    wl = ledger.load(t1.workflow)
    p1 = next(p for p in wl.proposals if p.id == t1.id)
    p2 = next(p for p in wl.proposals if p.id == t2.id)
    assert p1.issue == "https://x/12" and p1.published_state == "open" and p1.issue_state == "open" and not p1.pending_action
    assert not p2.issue and not p2.pending_action                              # the dry run published nothing: re-planned next time
    assert [a["proposal"] for a in pub.plan(ledger, store, repo="o/r", now=clock.t)["actions"]] == [t2.id]
    # observations: a human comment is a touch; `maintainer: hold` pauses the workflow's publication only
    outbox.inbox_dir.mkdir()
    obs = {"proposal": t1.id, "workflow": t1.workflow, "issue": 12, "url": "https://x/12", "state": "open",
           "observed_at": clock.t + 5, "human_comments": [{"at": clock.t + 1, "author": "h", "hold": True}], "references": []}
    (outbox.inbox_dir / f"{t1.id}.json").write_text(json.dumps(obs))
    synced = pub.sync(ledger, outbox, store, now=clock.t + 10)
    assert synced["touched"] == [t1.id] and synced["holds"] == {t1.workflow: "channel"}
    wl = ledger.load(t1.workflow)
    assert wl.hold.startswith("maintainer: hold") and wl.hold_source == "channel" and wl.last_human_touch > 0
    planned = pub.plan(ledger, store, repo="o/r", now=clock.t + 10)
    assert planned["actions"] == [] and t1.workflow in planned["held"]
    assert pub.sync(ledger, outbox, store, now=clock.t + 10)["touched"] == []   # the same observation applies once
    # the hold comment is gone (newer observation): the channel hold is released; a merged PR lands the proposal
    obs.update({"observed_at": clock.t + 20, "human_comments": [{"at": clock.t + 1, "author": "h", "hold": False}],
                "references": [{"url": "https://x/pull/40", "number": 40, "merged": True, "at": clock.t + 15}]})
    (outbox.inbox_dir / f"{t1.id}.json").write_text(json.dumps(obs))
    synced = pub.sync(ledger, outbox, store, now=clock.t + 30)
    assert synced["released"] == [t1.workflow] and synced["landed"] == [t1.id]
    p1 = next(p for p in ledger.load(t1.workflow).proposals if p.id == t1.id)
    assert p1.state == "landed" and p1.landed_by == "https://x/pull/40" and not ledger.load(t1.workflow).hold
    # Tier 1 landed: an update now, the close only after a later cycle shows the lint rate below the opening rate
    planned = pub.plan(ledger, store, repo="o/r", now=clock.t + 30)
    upd = [a for a in planned["actions"] if a["proposal"] == t1.id]
    assert upd and upd[0]["action"] == "update" and "awaiting the next cycle" in upd[0]["comment"]
    ledger.record_cycle(t1.workflow, at=clock.t + 40, declared=True, tier=2, units=5, quarantined=0, usd=0, seconds=0,
                        lints={"L01": {"units": 3, "findings": 3, "rate": 0.6}}, usd_samples=[], seconds_samples=[],
                        parse_failure_rate=0)
    assert [a["action"] for a in pub.plan(ledger, store, repo="o/r", now=clock.t + 50)["actions"] if a["proposal"] == t1.id] == ["update"]
    ledger.record_cycle(t1.workflow, at=clock.t + 60, declared=True, tier=2, units=5, quarantined=0, usd=0, seconds=0,
                        lints={"L01": {"units": 1, "findings": 1, "rate": 0.2}}, usd_samples=[], seconds_samples=[],
                        parse_failure_rate=0)
    closing = [a for a in pub.plan(ledger, store, repo="o/r", now=clock.t + 70)["actions"] if a["proposal"] == t1.id]
    assert closing[0]["action"] == "close" and "defect rate dropped" in closing[0]["comment"]
    pub.publish(ledger, store, outbox, repo="o/r", now=clock.t + 70)
    close_action = next(a for a in outbox.pending() if a["action"] == "close")
    (outbox.acks_dir / f"{close_action['id']}.json").write_text(json.dumps({"action": "close", "proposal": t1.id, "ok": True,
                                                                             "state": "landed", "url": "https://x/12"}))
    pub.sync(ledger, outbox, store, now=clock.t + 80)
    p1 = next(p for p in ledger.load(t1.workflow).proposals if p.id == t1.id)
    later = pub.plan(ledger, store, repo="o/r", now=clock.t + 90)
    assert p1.issue_state == "closed" and later["actions"] == [] and later["waiting"] == [t2.id]   # t2's open went out at +70
    # a failed ack is recorded and frees the proposal for a retry; a refuted proposal closes its issue then itself
    a2 = next(a for a in outbox.pending() if a["proposal"] == t2.id)
    (outbox.acks_dir / f"{a2['id']}.json").write_text(json.dumps({"action": "open", "proposal": t2.id, "ok": False, "error": "403"}))
    assert pub.sync(ledger, outbox, store, now=clock.t + 100)["failed"] == [t2.id]
    assert [r for r in store.query(kind="decision", playbook="workflow-improve") if r["result"]["type"] == "proposal_publish_failed"]
    ledger.transition(t2.workflow, t2.id, "experiment-registered", experiment_id="exp-1")
    ledger.transition(t2.workflow, t2.id, "refuted")
    ledger.note(t2.workflow, t2.id, issue="https://x/13", published_state="open", issue_state="open")
    act = pub.plan(ledger, store, repo="o/r", now=clock.t + 110)["actions"]
    assert [a["action"] for a in act if a["proposal"] == t2.id] == ["update"]
    pub.publish(ledger, store, outbox, repo="o/r", now=clock.t + 110)
    act = next(a for a in outbox.pending() if a["proposal"] == t2.id)
    (outbox.acks_dir / f"{act['id']}.json").write_text(json.dumps({"action": "update", "proposal": t2.id, "ok": True, "state": "refuted"}))
    pub.sync(ledger, outbox, store, now=clock.t + 120)
    ledger.transition(t2.workflow, t2.id, "closed", closed_reason="refuted")
    act = pub.plan(ledger, store, repo="o/r", now=clock.t + 130)["actions"]
    assert [a["action"] for a in act if a["proposal"] == t2.id] == ["close"]
    with pytest.raises(LedgerError, match="may not set"):
        ledger.note(t2.workflow, t2.id, state="open")


def test_a_channel_hold_survives_another_proposals_silence_and_needs_usable_observations(tmp_path):
    clock = Clock()
    store, ledger, t1, t2, bad = _seed(tmp_path, clock)
    outbox = pub.ProposalOutbox(tmp_path / "outbox")
    outbox.inbox_dir.mkdir(parents=True)

    def observe(p, at, *, hold, refs=()):
        (outbox.inbox_dir / f"{p.id}.json").write_text(json.dumps({
            "proposal": p.id, "workflow": p.workflow, "url": f"https://x/{p.id}", "state": "open", "observed_at": at,
            "human_comments": [{"at": at - 1, "author": "h", "hold": hold}], "references": list(refs)}))

    # t1's issue carries the hold; t2's does not: one observation of t2 must not release it
    observe(t1, clock.t + 10, hold=True)
    observe(t2, clock.t + 10, hold=False)
    synced = pub.sync(ledger, outbox, store, now=clock.t + 20)
    assert synced["holds"] == {t1.workflow: "channel"} and synced["released"] == []
    assert ledger.load(t1.workflow).hold.startswith("maintainer: hold on https://x/" + t1.id)
    observe(t2, clock.t + 30, hold=False)                          # only t2 observed again
    synced = pub.sync(ledger, outbox, store, now=clock.t + 40)
    assert synced["holds"] == {t1.workflow: "channel"} and synced["released"] == [] and ledger.load(t1.workflow).hold
    observe(t1, clock.t + 50, hold=False)                          # the hold comment is gone on t1's issue
    synced = pub.sync(ledger, outbox, store, now=clock.t + 60)
    assert synced["released"] == [t1.workflow] and not ledger.load(t1.workflow).hold
    # an operator's own hold is never released by the channel
    ledger.set_hold(t1.workflow, "operator hold")
    observe(t2, clock.t + 70, hold=False)
    pub.sync(ledger, outbox, store, now=clock.t + 80)
    assert ledger.load(t1.workflow).hold == "operator hold"
    ledger.clear_hold(t1.workflow)
    # landing + the rate check: a cycle whose units were all quarantined verifies nothing
    observe(t1, clock.t + 90, hold=False, refs=[{"url": "https://x/pull/41", "merged": True}])
    assert pub.sync(ledger, outbox, store, now=clock.t + 100)["landed"] == [t1.id]
    ledger.note(t1.workflow, t1.id, issue="https://x/12", published_state="landed", issue_state="open")
    ledger.record_cycle(t1.workflow, at=clock.t + 110, declared=True, tier=2, units=4, quarantined=4, usd=0, seconds=0,
                        lints={"L13": {"units": 4, "findings": 4, "rate": 1.0}}, usd_samples=[], seconds_samples=[],
                        parse_failure_rate=0)
    acts = [a["action"] for a in pub.plan(ledger, store, repo="o/r", now=clock.t + 120)["actions"] if a["proposal"] == t1.id]
    assert acts == []                                               # nothing measured: no close, nothing new to say
    ledger.record_cycle(t1.workflow, at=clock.t + 130, declared=True, tier=2, units=3, quarantined=1, usd=0, seconds=0,
                        lints={}, usd_samples=[], seconds_samples=[], parse_failure_rate=0)
    acts = [a["action"] for a in pub.plan(ledger, store, repo="o/r", now=clock.t + 140)["actions"] if a["proposal"] == t1.id]
    assert acts == ["close"]                                        # two usable units, no L01 hit: the rate dropped


def test_concurrent_publishers_emit_one_action_and_stale_acks_never_clear_a_newer_one(tmp_path):
    import threading

    clock = Clock()
    store, ledger, t1, t2, bad = _seed(tmp_path, clock)
    outbox = pub.ProposalOutbox(tmp_path / "outbox")
    results, errors = [], []

    def worker():
        try:
            results.append(pub.publish(ledger, store, outbox, repo="o/r", now=clock.t))
        except Exception as exc:  # noqa: BLE001 - reported by the assertion below
            errors.append(exc)
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and sum(len(r["written"]) for r in results) == 2       # one action per proposal, whoever ran
    pending = outbox.pending()
    assert sorted(a["proposal"] for a in pending) == sorted([t1.id, t2.id])
    # the first open action for t1 gets no answer for a week: a retry goes out, then the OLD ack arrives late
    first = next(a for a in pending if a["proposal"] == t1.id)
    retry_report = pub.publish(ledger, store, outbox, repo="o/r", now=clock.t + pub.PENDING_TTL + 1)
    second = next(a for a in outbox.pending() if a["proposal"] == t1.id and a["id"] != first["id"])
    assert len(retry_report["written"]) == 2 and second["action"] == "open"    # both unanswered actions are retried
    p1 = next(p for p in ledger.load(t1.workflow).proposals if p.id == t1.id)
    assert p1.pending_action == second["id"]
    outbox.acks_dir.mkdir(exist_ok=True)
    (outbox.acks_dir / f"{first['id']}.json").write_text(json.dumps({"action": "open", "proposal": t1.id, "workflow": t1.workflow,
                                                                      "ok": True, "url": "https://x/12", "state": "open"}))
    synced = pub.sync(ledger, outbox, store, now=clock.t + pub.PENDING_TTL + 5)
    assert synced["stale"] == [t1.id] and synced["acked"] == []
    p1 = next(p for p in ledger.load(t1.workflow).proposals if p.id == t1.id)
    assert p1.pending_action == second["id"] and p1.published_state == "" and p1.issue == "https://x/12"   # the fact it adds
    assert not (outbox.actions_dir / f"{first['id']}.json").exists()          # the stale pair is consumed
    assert [a["proposal"] for a in pub.plan(ledger, store, repo="o/r", now=clock.t + pub.PENDING_TTL + 6)["actions"]] == []
    (outbox.acks_dir / f"{second['id']}.json").write_text(json.dumps({"action": "open", "proposal": t1.id, "workflow": t1.workflow,
                                                                       "ok": True, "url": "https://x/12", "state": "open"}))
    assert pub.sync(ledger, outbox, store, now=clock.t + pub.PENDING_TTL + 9)["acked"] == [t1.id]
    p1 = next(p for p in ledger.load(t1.workflow).proposals if p.id == t1.id)
    assert p1.pending_action == "" and p1.published_state == "open"
    # publisher versus sync: the ack check-and-apply waits for the publisher's lock, so a retry
    # and a delayed ack can never interleave between the pending check and its clearing
    import time as _time

    from infermatrix_copilot.trace_store import file_lock
    (outbox.acks_dir / f"{second['id']}.json").write_text(json.dumps({"action": "open", "proposal": t1.id, "ok": True}))
    finished = []
    with file_lock(outbox.root / ".publish.lock", blocking=False) as held:
        assert held
        t = threading.Thread(target=lambda: finished.append(pub.sync(ledger, outbox, store, now=clock.t + pub.PENDING_TTL + 20)))
        t.start()
        t.join(1.0)
        assert t.is_alive() and finished == []                                # blocked behind the publisher
    t.join(30.0)
    assert not t.is_alive() and finished and finished[0]["stale"] == [t1.id]  # applied once the publisher let go


def test_publish_step_is_double_gated_and_hold_pauses_only_publication(tmp_path, settings):
    from infermatrix_copilot.engine.steps import improve as steps

    clock = Clock()
    store, ledger, t1, t2, bad = _seed(tmp_path, clock)
    base = settings.model_copy(update={"trace_store_root": str(store.root), "improve_ledger_dir": str(tmp_path / "ledger"),
                                       "improve_proposal_repo": "o/r", "improve_enabled": True})
    trace = SimpleNamespace(events=[], record=lambda *a, **k: trace.events.append((a, k)))

    def ctx(st, post):
        return SimpleNamespace(settings=st, state={"task_spec": {"kind": "workflow_improve", "repo": "demo", "post": post}},
                               params={}, run_dir=tmp_path / "run", trace=trace, llm=None)

    # no post intent: nothing is even planned as a write; the preview says what would happen
    out = asyncio.run(steps._publish(ctx(base, False)))
    assert out.ok and "not publishing" in out.summary and out.outputs["state_updates"]["improve_publish"]["would"] == 2
    # post intent without an outbox: blocked loudly, never a silent no-op
    out = asyncio.run(steps._publish(ctx(base, True)))
    assert not out.ok and "no outbox configured" in out.summary
    with_outbox = base.model_copy(update={"improve_outbox_dir": str(tmp_path / "outbox")})
    # post intent + ALLOW_POST=0: a dry run, no file written
    out = asyncio.run(steps._publish(ctx(with_outbox, True)))
    assert out.ok and out.outputs["dry_run"] and not (tmp_path / "outbox" / "actions").exists()
    # both gates: files written, the outward write traced
    out = asyncio.run(steps._publish(ctx(with_outbox.model_copy(update={"allow_post": True}), True)))
    assert out.ok and len(out.outputs["state_updates"]["improve_publish"]["written"]) == 2
    assert [k.get("what") for a, k in trace.events if a[0] == "outbox_written"] == ["proposal action"] * 2
    # the sync step reads the outbox back (a read step); without an outbox it is a no-op
    assert asyncio.run(steps._sync(ctx(base, False))).ok
    out = asyncio.run(steps._sync(ctx(with_outbox, False)))
    assert out.ok and out.outputs["sync"]["acked"] == []
    # rule 5: a hold pauses publication for that workflow; lints and the cycle still run
    ledger.set_hold("pr-review.agent.review_diff", "maintainer: hold")
    _unit(store, "r2:review", **CLEAN)
    report = cyc.run_cycle(store, with_outbox, tmp_path / "ledger", now=clock.t + 100, since=T0, until=clock.t + 50)
    # the two review units plus the engine's own records (self-enrolment); the hold is reported, not obeyed here
    assert report["workflows"]["pr-review.agent.review_diff"]["units"] == 2
    assert report["holds"] == {"pr-review.agent.review_diff": "maintainer: hold"}
    out = asyncio.run(steps._publish(ctx(with_outbox.model_copy(update={"allow_post": True}), True)))
    assert out.ok and out.outputs["state_updates"]["improve_publish"]["held"] == {"pr-review.agent.review_diff": "maintainer: hold"}
    # rule 1: in a shadow run the executor refuses the push step before it runs
    from execution_helpers import application_executor as Executor
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.playbooks.store import PlaybookStore
    from infermatrix_copilot.run_trace import RunTrace

    registry = register_builtin_steps(StepRegistry())
    pbs = PlaybookStore(Path(__file__).resolve().parents[1] / "playbooks", registry)
    pbs.load()
    shadow = with_outbox.model_copy(update={"allow_post": True, "improve_shadow": True})
    ledger.clear_hold("pr-review.agent.review_diff")
    run_dir = tmp_path / "shadow-run"
    ex = Executor(registry, shadow, run_dir=run_dir, trace=RunTrace(run_dir / "t.jsonl"))
    state = {"task_spec": {"kind": "workflow_improve", "repo": "demo", "post": True,
                           "params": {"since": T0, "until": clock.t + 200}}}
    outcome = asyncio.run(ex.run(pbs.get("workflow-improve"), state))
    assert outcome.status == "blocked" and "shadow run refused step 'publish'" in outcome.blocked_reason
    # the same playbook outside the shadow, post not requested: runs to the report
    run_dir = tmp_path / "cycle-run"
    ex = Executor(registry, with_outbox, run_dir=run_dir, trace=RunTrace(run_dir / "t.jsonl"))
    state = {"task_spec": {"kind": "workflow_improve", "repo": "demo", "params": {"since": T0, "until": clock.t + 300}}}
    outcome = asyncio.run(ex.run(pbs.get("workflow-improve"), state))
    assert outcome.status == "done", outcome.blocked_reason
    assert state["improve_item"] == "cycle" and state["improve_meta"] is False
    assert state["improve_publish"]["mode"] == "not-requested"


# -- Tier 2 proposals from the punch list ------------------------------------------------------------

def test_tier2_proposals_come_from_the_punch_list_one_per_stage(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    unit = _review_unit(store)
    ids = [r["id"] for r in unit.records]
    ledger = Ledger(tmp_path / "ledger", store, clock=clock)
    plist = [
        {"workflow": "w", "stage": "S2", "description": "seen but not raised", "loss": 3, "disputed": 1,
         "cells": [{"gold_id": "g1", "unit_id": "r1:review", "mechanism": "m", "evidence": ids[:2]},
                   {"gold_id": "g2", "unit_id": "r1:review", "mechanism": "m", "evidence": ids[1:3]}]},
        {"workflow": "w", "stage": "S0", "description": "cannot attribute", "loss": 4, "disputed": 0,
         "cells": [{"gold_id": "g3", "unit_id": "u", "mechanism": "", "evidence": ids[:1]}]},
        {"workflow": "w", "stage": "S10", "description": "measurement", "loss": 2, "disputed": 0,
         "cells": [{"gold_id": "g4", "unit_id": "u", "mechanism": "", "evidence": ids[:1]}]},
        {"workflow": "w", "stage": "S6", "description": "calibration", "loss": 0, "disputed": 2, "cells": []},
        {"workflow": "w", "stage": "S5", "description": "assembly", "loss": 1, "disputed": 0,
         "cells": [{"gold_id": "g5", "unit_id": "u", "mechanism": "", "evidence": []}]},
    ]
    opened = cyc.open_tier2_proposals(ledger, tmp_path / "ledger", "pr-review.agent.review_diff", plist,
                                      descriptive_only=False, items=["demo#1@abc"])
    assert [o["stage"] for o in opened] == ["S2"]                       # S0/S10 never; no loss or no evidence never
    p = ledger.load("pr-review.agent.review_diff").proposals[0]
    assert p.tier == 2 and p.stage == "S2" and p.loss == 3 and p.evidence == ids[:3] and "(1 disputed)" in p.claim
    assert p.suggestion["metric"] == "recall_gold" and p.suggestion["n_required"] == 53   # prior sd .13, effect .05
    assert "lens prompts" in p.suggestion["covers"] and p.suggestion["items"] == ["demo#1@abc"]
    assert pub.lint_proposal(store, p, pub.cite(store, p)) == []
    # a second cycle refreshes the same open proposal instead of duplicating it
    again = cyc.open_tier2_proposals(ledger, tmp_path / "ledger", "pr-review.agent.review_diff", plist[:1],
                                     descriptive_only=False, items=["demo#1@abc"])
    assert again[0]["id"] == p.id and len(ledger.load("pr-review.agent.review_diff").proposals) == 1
    proxy = cyc.open_tier2_proposals(ledger, tmp_path / "ledger", "rb-review.review", plist[:1],
                                     descriptive_only=True, items=[])
    q = ledger.load("rb-review.review").proposals[0]
    assert proxy and q.proxy and q.suggestion == {}                     # descriptive-only: no experiment suggested


# -- the engine's own benchmark ------------------------------------------------------------------------

def _meta_case(tmp_path, store, labels, name="case-1"):
    gt, _ = _gt(tmp_path)
    gold = _curated(gt)
    unit = _review_unit(store, review=REVIEW)
    meta_dir = tmp_path / "meta"
    human = {gold.entries[i].gold_id: stage for i, stage in enumerate(labels)}
    meta.export_case(store, unit, gt / "curated" / "pr1.gold.json", human, meta_dir / "cases" / name)
    lint_dir = meta_dir / "lints" / "L01"
    lint_dir.mkdir(parents=True)
    lint_store = TraceStore(lint_dir, environ={})
    with trace_context(run_id="s", unit_id="s:review"):
        lint_store.append("model_call", **_call(out_tokens=1000, max_tokens=1000, stop="max_tokens"))
        lint_store.append("decision", outputs={"review": "r"}, result={"status": "ok"})
    day = next((lint_dir / "records").glob("*.jsonl"))
    (lint_dir / "sample.jsonl").write_text(day.read_text())
    return meta_dir, gold, human


def _agents(answers):
    """Investigator families answering from a script: {family: {gold_id: stage}}; evidence = any record read."""
    def make(family):
        def agent(system, prompt, scope, extra_tools, max_iters):
            listing = extra_tools["trace_query"].handler()
            rec_id = json.loads(listing.split("<untrusted_data>\n")[1].split("\n</untrusted_data>")[0])[0]["id"]
            gid = prompt.split("concern [")[1].split("]")[0] if "concern [" in prompt else ""
            stage = answers[family].get(gid, "S0")
            return json.dumps({"stage": stage, "mechanism": "scripted", "evidence": [rec_id]})
        return agent
    return {f: make(f) for f in answers}


def test_meta_case_run_and_adapter_score_agreement_with_human_labels(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    meta_dir, gold, human = _meta_case(tmp_path, store, ["S2", "S1"])
    g1, g2 = [e.gold_id for e in gold.entries]
    case = meta.load_cases(meta_dir)[0]
    assert case.labels == human
    # the engine's unit: two families agree on g1 (right) and disagree on g2
    sink = _store(tmp_path / "engine", clock)
    with trace_context(run_id="run-1", playbook="workflow-improve", step="improve.forensics",
                       unit_id="run-1:forensics", workflow="workflow-improve.improve.forensics", item="meta:case-1"):
        result = meta.run_case(sink, case, _agents({"fam-a": {g1: "S2", g2: "S1"}, "fam-b": {g1: "S2", g2: "S3"}}),
                               meta_dir=meta_dir)
    assert result["engine"] == {g1: "S2", g2: "S1"} and result["disputed"] == [g2] and result["human"] == human
    assert result["agreement"] == 1.0 and result["lint_recall"] == 1.0 and result["attributed"] == 2
    unit = units_between(sink, T0, T0 + 10_000)["run-1:forensics"]
    adapter = MetaBenchAdapter(meta_dir=meta_dir)
    assert adapter.human_labelled and adapter.gold("demo#1@abc") is None and adapter.gold("meta:nope") is None
    mgold = adapter.gold("meta:case-1")
    assert [e.gold_id for e in mgold.entries] == sorted([g1, g2]) and mgold.version == meta.case_version(meta_dir / "cases" / "case-1")
    outcome = adapter.fetch(unit, sink)
    matches = {m.gold_id: m.status for m in adapter.match(unit, mgold, outcome)}
    assert matches == {g1: "hit", g2: "disputed"}
    scores = scores_from(adapter.match(unit, mgold, outcome), adapter.findings(unit, outcome), adapter.review_scores(unit, outcome))
    assert scores.values["recall_gold"] == 1.0 and scores.values["lint_recall_review"] == 1.0 and scores.values["agreement_review"] == 1.0
    assert "kappa_review" in scores.values and scores.sources["recall_gold"] == "gold-matrix"
    # a wrong, undisputed label is a miss; S0 is unlabeled, never a miss
    with trace_context(run_id="run-2", playbook="workflow-improve", step="improve.forensics",
                       unit_id="run-2:forensics", workflow="workflow-improve.improve.forensics", item="meta:case-1"):
        r2 = meta.run_case(sink, case, _agents({"fam-a": {g1: "S3", g2: "S0"}, "fam-b": {g1: "S3", g2: "S0"}}), meta_dir=meta_dir)
    unit2 = units_between(sink, T0, T0 + 10_000)["run-2:forensics"]
    m2 = {m.gold_id: m.status for m in adapter.match(unit2, mgold, adapter.fetch(unit2, sink))}
    assert m2 == {g1: "miss", g2: "unlabeled"} and r2["agreement"] == 0.0 and r2["kappa"] is None
    assert adapter.fetch(units_between(store, T0, T0 + 10_000)["r1:review"], store) is None    # no meta_eval: no outcome
    # staging a case for a shadow child copies the case and the lint samples, nothing narrative
    (meta_dir / "cases" / "case-1" / "doc").mkdir()
    (meta_dir / "cases" / "case-1" / "doc" / "report.md").write_text("the wave-2 narrative")
    staged = meta.stage_case(meta_dir, "case-1", tmp_path / "shadow" / "meta")
    assert (staged / "cases" / "case-1" / "case.json").exists() and (staged / "lints" / "L01" / "sample.jsonl").exists()
    assert not (staged / "cases" / "case-1" / "doc").exists()
    with pytest.raises(FileNotFoundError):
        meta.stage_case(meta_dir, "case-9", tmp_path / "shadow2")


def test_self_experiment_runs_without_a_judge_and_refuses_the_benchmark_as_an_arm(tmp_path, settings, monkeypatch):
    clock = Clock()
    store = _store(tmp_path, clock)
    meta_dir, gold, human = _meta_case(tmp_path, store, ["S2", "S1"])
    from infermatrix_copilot.improve.stats import MIN_ITEMS

    for n in range(2, MIN_ITEMS + 1):
        meta.export_case(store, units_between(store, T0, T0 + 10_000)["r1:review"], tmp_path / "gt" / "curated" / "pr1.gold.json",
                         human, meta_dir / "cases" / f"case-{n}")
    st = settings.model_copy(update={"trace_store_root": str(store.root), "improve_ledger_dir": str(tmp_path / "ledger"),
                                     "improve_meta_dir": str(meta_dir), "eco_model": "claude-sonnet-5"})
    items = [f"meta:case-{n}" for n in range(1, MIN_ITEMS + 1)]
    base = dict(workflow="workflow-improve.improve.forensics", hypothesis="a shorter completion budget attributes as well",
                metric="recall_gold", items=items, arm_overrides={"llm_max_tokens": "9000"}, cost_per_unit_usd=0.01,
                sd_item=0.02, min_effect=0.05, now=T0)
    with pytest.raises(exps.ExperimentError, match="engine's own configuration"):
        exps.register(store, st, tmp_path / "ledger", **{**base, "arm_overrides": {"IMPROVE_META_DIR": "/tmp/x"}})
    with pytest.raises(exps.ExperimentError, match="no curated gold"):
        exps.register(store, st, tmp_path / "ledger", **{**base, "items": ["meta:case-9"]})
    exp = exps.register(store, st, tmp_path / "ledger", **base)
    assert exp.fingerprint_diff == {"settings": {"llm_max_tokens": (settings.llm_max_tokens, 9000)}}
    assert exp.gold_versions == {i: meta.case_version(meta_dir / "cases" / i[5:]) for i in items}
    shadow = TraceStore(tmp_path / "shadow-traces", environ={})
    fps = {"arm": exp.arm_fingerprint, "incumbent": exp.incumbent_fingerprint}
    g1, g2 = [e.gold_id for e in gold.entries]
    seen = []

    def run_unit(*, side, item, replicate, env, snapshot_path, shadow_dir, run_root, playbook, repo, pr):
        # what the child would leave behind: the forensics unit (declared) and a later undeclared report unit
        seen.append((side, item, replicate, Path(shadow_dir), json.loads(Path(snapshot_path).read_text())))
        assert env["IMPROVE_SHADOW"] == "1" and repo == "meta" and pr == 0 and playbook == "workflow-improve"
        assert (Path(shadow_dir) / "meta" / "cases" / item[5:] / "case.json").exists()
        uid = f"{side}-{item}-r{replicate}"
        engine = {g1: "S2", g2: "S1"} if side == "arm" else {g1: "S2", g2: "S3"}
        with trace_context(run_id=uid, playbook="workflow-improve", step="improve.forensics", unit_id=f"{uid}:forensics",
                           workflow="workflow-improve.improve.forensics", fingerprint=fps[side], item=item,
                           unit_tag=env["IMPROVE_UNIT_TAG"]):
            shadow.append("model_call", **_call())
            shadow.append("outcome", context={"of": f"{uid}:forensics"},
                          result={"type": "meta_eval", "human": human, "engine": engine, "disputed": [],
                                  "agreement": 1.0 if side == "arm" else 0.5, "kappa": None, "lint_recall": 1.0})
            shadow.append("decision", outputs={"report": "x"}, result={"status": "ok", "type": "step_result"})
        with trace_context(run_id=uid, playbook="workflow-improve", step="report.final_summary", unit_id=f"{uid}:report",
                           unit_tag=env["IMPROVE_UNIT_TAG"]):
            shadow.append("decision", result={"status": "ok", "type": "step_result"})
        return {"rc": 0, "tag": env["IMPROVE_UNIT_TAG"]}

    done = exps.run(store, st, tmp_path / "ledger", exp.experiment_id, run_unit=run_unit, shadow_store=shadow, now=T0 + 100)
    assert done.state == "supported", done.result                       # arm 1.0 vs incumbent 0.5 on every item
    assert done.result["n_retained"] == MIN_ITEMS and done.result["n_excluded"] == 0 and abs(done.result["mean"] - 0.5) < 1e-9
    assert len(seen) == MIN_ITEMS * 6 and all(s[4]["meta_case"] == s[1][5:] for s in seen)
    assert all(u.endswith(":forensics") for u in done.result["units"].values())   # never the report unit
    # the meta runner's argv and environment (the child reads the staged copy only)
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(exps, "run_command", fake_run)
    out = exps.meta_run_unit(side="arm", item="meta:case-1", replicate=1, env={"IMPROVE_UNIT_TAG": "t"},
                             snapshot_path=tmp_path / "s.json", shadow_dir=tmp_path / "sd", run_root=tmp_path / "rr",
                             playbook="workflow-improve", repo="meta", pr=0)
    argv, kw = calls[0]
    assert argv[-6:] == ["--playbook", "workflow-improve", "--task-param", "repo=meta", "--task-param", "meta_case=case-1"]
    assert out["rc"] == 0 and kw["env"]["IMPROVE_META_DIR"] == str(tmp_path / "sd" / "meta") and kw["cwd"] == str(tmp_path / "sd")


def test_the_real_entry_accepts_the_shadow_childs_repo_alias(tmp_path):
    """The actual CLI, configured only through the shadow environment: the
    task's repo is the one alias REPO_PATHS (JSON) exposes, so run_playbook
    accepts it and reaches the executor (here: the LLM gate, since no key
    is configured — never the unknown-alias refusal)."""
    import subprocess
    import sys

    from infermatrix_copilot.improve.shadow import make_executables_dir, shadow_env

    clock = Clock()
    store = _store(tmp_path, clock)
    meta_dir, gold, human = _meta_case(tmp_path, store, ["S2", "S1"])
    shadow_dir = tmp_path / "shadow" / "case-1"
    shadow_dir.mkdir(parents=True)
    meta.stage_case(meta_dir, "case-1", shadow_dir / "meta")
    exe = make_executables_dir(tmp_path / "bin", ("python3", "git", "grep"))
    env = shadow_env(shadow_dir=shadow_dir, run_dir=tmp_path / "runs", trace_root=tmp_path / "shadow-traces",
                     executables_dir=exe, repo_name="meta", ledger_dir=tmp_path / "ledger", environ={"PATH": os.environ["PATH"]})
    env.update({"HOME": str(tmp_path / "home"), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "IMPROVE_META_DIR": str(shadow_dir / "meta"), "RUN_ROOT": str(tmp_path / "runs"),
                "ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": ""})           # no model may be called from a test
    plan = subprocess.run(exps.meta_argv("workflow-improve", "meta", "case-1", "--plan-only"), cwd=str(shadow_dir), env=env,
                          capture_output=True, text=True, timeout=240)
    assert plan.returncode == 0, plan.stdout[-800:] + plan.stderr[-800:]
    assert "workflow_improve on meta" in plan.stdout and "'improve.forensics'" in plan.stdout
    run = subprocess.run(exps.meta_argv("workflow-improve", "meta", "case-1"), cwd=str(shadow_dir), env=env,
                         capture_output=True, text=True, timeout=240)
    assert "unknown repo alias" not in run.stdout and "no reviewer LLM" in run.stdout, run.stdout[-800:] + run.stderr[-800:]


def test_meta_mode_runs_the_forensics_step_as_the_engines_own_unit(tmp_path, settings):
    from execution_helpers import application_executor as Executor
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.llm import Block, Reply
    from infermatrix_copilot.playbooks.store import PlaybookStore
    from infermatrix_copilot.run_trace import RunTrace

    clock = Clock()
    store = _store(tmp_path, clock)
    meta_dir, gold, human = _meta_case(tmp_path, store, ["S2", "S1"])
    g1, g2 = [e.gold_id for e in gold.entries]

    class FakeLLM:
        available = True

        def for_target(self, target):
            return self

        def create(self, **kw):
            prompt = json.dumps(kw.get("messages") or [], default=str)
            gid = prompt.split("concern [")[1].split("]")[0] if "concern [" in prompt else ""
            return Reply(blocks=[Block(type="text", text=json.dumps({"stage": human.get(gid, "S0"), "mechanism": "m",
                                                                     "evidence": []}))],
                         stop_reason="end_turn", usage={}, model="m")

    engine_store = _store(tmp_path / "engine", clock)
    st = settings.model_copy(update={"trace_store_root": str(engine_store.root), "improve_ledger_dir": str(tmp_path / "ledger"),
                                     "improve_meta_dir": str(meta_dir), "eco_model": "claude-sonnet-5"})
    registry = register_builtin_steps(StepRegistry())
    pbs = PlaybookStore(Path(__file__).resolve().parents[1] / "playbooks", registry)
    pbs.load()
    run_dir = tmp_path / "meta-run"
    ex = Executor(registry, st, run_dir=run_dir, trace=RunTrace(run_dir / "t.jsonl"), llm=FakeLLM())
    state = {"task_spec": {"kind": "workflow_improve", "repo": "demo", "params": {"meta_case": "case-1"}}}
    outcome = asyncio.run(ex.run(pbs.get("workflow-improve"), state))
    assert outcome.status == "done", outcome.blocked_reason
    assert state["improve_meta"] is True and state["improve_item"] == "meta:case-1"
    assert "improve_cycle" not in state and "improve_publish" not in state           # every cycle step skipped
    ev = state["improve_meta_eval"]
    assert ev["case"] == "case-1" and ev["cells"] == 2 and ev["attributed"] == 0       # uncited answers are S0
    units = units_between(engine_store, 0.0, float("inf"), grace=0.0, lookback=0.0)   # the executor's own clock
    unit = next(u for u in units.values() if u.step == "improve.forensics")
    assert unit.workflow == "workflow-improve.improve.forensics" and unit.item == "meta:case-1" and unit.fingerprint
    outcomes = [r for r in unit.records if r["kind"] == "outcome"]
    assert len(outcomes) == 1 and outcomes[0]["result"]["type"] == "meta_eval" and outcomes[0]["context"]["unit_id"] == unit.unit_id
    # the same unit is what the adapter scores
    adapter = MetaBenchAdapter(meta_dir=meta_dir)
    matches = {m.gold_id: m.status for m in adapter.match(unit, adapter.gold("meta:case-1"), adapter.fetch(unit, engine_store))}
    assert matches == {g1: "unlabeled", g2: "unlabeled"}
    # an unknown case blocks; a case name that is a path is refused at the mode step
    bad = {"task_spec": {"kind": "workflow_improve", "repo": "demo", "params": {"meta_case": "case-9"}}}
    ex = Executor(registry, st, run_dir=tmp_path / "r2", trace=RunTrace(tmp_path / "r2" / "t.jsonl"), llm=FakeLLM())
    assert "no meta case" in asyncio.run(ex.run(pbs.get("workflow-improve"), bad)).blocked_reason
    worse = {"task_spec": {"kind": "workflow_improve", "repo": "demo", "params": {"meta_case": "../x"}}}
    ex = Executor(registry, st, run_dir=tmp_path / "r3", trace=RunTrace(tmp_path / "r3" / "t.jsonl"), llm=FakeLLM())
    assert "meta_case must be a case name" in asyncio.run(ex.run(pbs.get("workflow-improve"), worse)).blocked_reason


def test_cli_publish_sync_and_meta_bench(tmp_path, monkeypatch, capsys):
    from infermatrix_copilot.improve import cli

    clock = Clock()
    store, ledger, t1, t2, bad = _seed(tmp_path, clock)
    monkeypatch.setenv("TRACE_STORE_ROOT", str(store.root))
    monkeypatch.delenv("ALLOW_POST", raising=False)
    common = ["--ledger-dir", str(tmp_path / "ledger"), "--outbox-dir", str(tmp_path / "outbox"), "--repo", "o/r"]
    assert cli.main(["publish", "--dry-run", *common]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] and [a["action"] for a in out["actions"]] == ["open", "open"] and list(out["refused"]) == [bad.id]
    assert cli.main(["publish", *common]) == 0                                     # ALLOW_POST unset: still a dry run
    assert json.loads(capsys.readouterr().out)["dry_run"] and not (tmp_path / "outbox" / "actions").exists()
    monkeypatch.setenv("ALLOW_POST", "1")
    assert cli.main(["publish", *common]) == 0
    assert len(json.loads(capsys.readouterr().out)["written"]) == 2
    assert cli.main(["sync", *common]) == 0 and json.loads(capsys.readouterr().out)["acked"] == []
    assert cli.main(["publish", "--ledger-dir", str(tmp_path / "ledger"), "--repo", "o/r"]) == 2    # no outbox anywhere
    capsys.readouterr()
    assert cli.main(["meta", "bench", "--meta-dir", str(tmp_path / "nometa")]) == 1             # no cases
    assert "no cases" in capsys.readouterr().err
