"""Per-workflow improvement ledgers and the proposal state machine (design §9).

One JSON file per workflow under the ledger directory: rolling baselines
(units, cost, latency), per-cycle lint defect rates, open proposals with
their state, the channel-liveness stamp. Every change is also a trace/1
``decision`` when a store is given, so the ledger can be rebuilt from the
records.

Proposal states::

    open -> experiment-registered -> supported | neutral | refuted | underpowered
    open | neutral | underpowered -> stale   (30 days without a human or PR touch)
    supported | open (Tier 1) -> landed      (a PR referenced the proposal)
    refuted -> closed
"""

from __future__ import annotations

from ..persistence import atomic_write_bytes

import json
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..trace_store import file_lock

STATES = ("open", "experiment-registered", "supported", "neutral", "refuted", "underpowered",
          "landed", "closed", "stale")
TRANSITIONS: dict[str, set[str]] = {
    "open": {"experiment-registered", "landed", "stale", "closed"},
    "experiment-registered": {"supported", "neutral", "refuted", "underpowered", "closed"},
    "supported": {"landed", "stale", "closed"},
    "neutral": {"experiment-registered", "stale", "closed"},
    "underpowered": {"experiment-registered", "stale", "closed"},
    "refuted": {"closed"},
    "landed": set(),
    "closed": set(),
    "stale": {"experiment-registered", "landed", "closed"},
}
STALE_AFTER = 30 * 24 * 3600.0


class LedgerError(ValueError):
    pass


@dataclass
class Proposal:
    id: str
    workflow: str
    tier: int                     # 1 or 2
    claim: str
    state: str = "open"
    lint: str = ""                # Tier 1: the lint that worsened
    stage: str = ""               # Tier 2: the stage-of-loss label
    loss: float = 0.0
    evidence: list[str] = field(default_factory=list)   # record ids
    opened_at: float = 0.0
    updated_at: float = 0.0
    last_human_touch: float = 0.0
    experiment_id: str = ""
    issue: str = ""               # URL once published
    history: list[dict] = field(default_factory=list)
    proxy: bool = False           # RB descriptive-only proposals
    # Tier 2: the suggested pre-registration (metric, min effect, items
    # required, what the arm's fingerprint should cover); Tier 1: empty
    suggestion: dict = field(default_factory=dict)
    rate_at_open: float = 0.0     # Tier 1: the lint's defect rate when opened (the landed check)
    # publication bookkeeping (publish.py): the outbox action awaiting an
    # ack, the state the issue currently shows, whether it is closed
    pending_action: str = ""
    pending_since: float = 0.0
    published_state: str = ""
    issue_state: str = ""
    landed_by: str = ""
    closed_reason: str = ""
    synced_at: float = 0.0        # the last inbox observation applied
    channel_hold: bool = False    # this proposal's issue carries a `maintainer: hold` comment
    hold_url: str = ""


@dataclass
class WorkflowLedger:
    workflow: str
    tier: int = 1
    declared: bool = False
    cycles: list[dict] = field(default_factory=list)     # per cycle: at, units, usd, seconds, lint rates
    baseline: dict = field(default_factory=lambda: {"usd": [], "seconds": [], "parse_failure_rate": 0.0})
    proposals: list[Proposal] = field(default_factory=list)
    hold: str = ""                                       # maintainer hold reason (pauses publishing)
    hold_source: str = ""                                # operator | channel (a `maintainer: hold` comment)
    last_human_touch: float = 0.0

    def lint_rate(self, lint_id: str, back: int = 0) -> tuple[float, int] | None:
        """``(rate, units)`` for the cycle ``back`` cycles ago, or None."""
        if len(self.cycles) <= back:
            return None
        cycle = self.cycles[-1 - back]
        rates = cycle.get("lints") or {}
        if lint_id not in rates:
            return 0.0, int(cycle.get("units") or 0)
        entry = rates[lint_id]
        return float(entry.get("rate") or 0.0), int(cycle.get("units") or 0)


class Ledger:
    def __init__(self, directory: str | Path, store=None, *, clock=time.time):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store = store
        self._clock = clock

    # -- persistence ----------------------------------------------------------------
    def _path(self, workflow: str) -> Path:
        return self.dir / (re.sub(r"[^A-Za-z0-9._-]+", "_", workflow) + ".json")

    def load(self, workflow: str) -> WorkflowLedger:
        path = self._path(workflow)
        if not path.exists():
            return WorkflowLedger(workflow=workflow)
        data = json.loads(path.read_text(encoding="utf-8"))
        proposals = [Proposal(**p) for p in data.pop("proposals", [])]
        return WorkflowLedger(proposals=proposals, **data)

    def save(self, ledger: WorkflowLedger) -> None:
        data = asdict(ledger)
        atomic_write_bytes(self._path(ledger.workflow),
                           json.dumps(data, ensure_ascii=False, indent=1, default=str).encode("utf-8"))

    @contextmanager
    def locked(self, workflow: str) -> Iterator[None]:
        """Serialize a read-modify-write of one workflow's ledger across
        processes (the scheduler, the CLI and a playbook run may all touch
        it); a lock that cannot be taken within the timeout raises."""
        with file_lock(self._path(workflow).with_suffix(".lock"), blocking=True, timeout=30.0) as held:
            if not held:
                raise LedgerError(f"ledger for {workflow} is locked by another process")
            yield

    def workflows(self) -> list[str]:
        names = []
        for p in self.dir.glob("*.json"):
            if p.name == "cursor.json" or p.name.startswith("."):
                continue
            try:
                names.append(json.loads(p.read_text(encoding="utf-8"))["workflow"])
            except (OSError, ValueError, KeyError):
                continue
        return sorted(names)

    def _record(self, kind_type: str, **fields: Any) -> None:
        if self.store is None:
            return
        try:
            self.store.append("decision", context={"playbook": "workflow-improve"},
                              result={"type": kind_type, **fields})
        except Exception:  # noqa: BLE001 - the ledger file is the state; the record is the audit copy
            pass

    # -- cycles ---------------------------------------------------------------------
    def record_cycle(self, workflow: str, *, at: float, declared: bool, tier: int, units: int,
                     quarantined: int, usd: float, seconds: float, lints: dict[str, dict],
                     usd_samples: list[float], seconds_samples: list[float],
                     parse_failure_rate: float, cost_policy: str = '', cost_unknown_calls: int = 0) -> WorkflowLedger:
        with self.locked(workflow):
            return self._record_cycle(workflow, at=at, declared=declared, tier=tier, units=units,
                                      quarantined=quarantined, usd=usd, seconds=seconds, lints=lints,
                                      usd_samples=usd_samples, seconds_samples=seconds_samples,
                                      parse_failure_rate=parse_failure_rate,
                                      cost_policy=cost_policy, cost_unknown_calls=cost_unknown_calls)

    def _record_cycle(self, workflow: str, *, at: float, declared: bool, tier: int, units: int,
                      quarantined: int, usd: float, seconds: float, lints: dict[str, dict],
                      usd_samples: list[float], seconds_samples: list[float],
                      parse_failure_rate: float, cost_policy: str = '', cost_unknown_calls: int = 0) -> WorkflowLedger:
        ledger = self.load(workflow)
        ledger.declared = declared
        ledger.tier = tier
        ledger.cycles.append({"at": at, "units": units, "quarantined": quarantined, "usd": round(usd, 4),
                              "seconds": round(seconds, 1), "lints": lints})
        ledger.cycles = ledger.cycles[-52:]
        base = ledger.baseline
        base["usd"] = (base.get("usd") or [])[-400:] + [round(x, 6) for x in usd_samples]
        base["seconds"] = (base.get("seconds") or [])[-400:] + [round(x, 3) for x in seconds_samples]
        base["parse_failure_rate"] = parse_failure_rate
        if cost_policy:
            ledger.cycles[-1].update(cost_policy=cost_policy, cost_unknown_calls=cost_unknown_calls)
            base['planning_usd'] = (base.get('planning_usd') or [])[-400:] + [round(x, 6) for x in usd_samples]
            base['cost_policy'] = cost_policy
        self.save(ledger)
        self._record("ledger_cycle", workflow=workflow, at=at, units=units, quarantined=quarantined,
                     usd=round(usd, 4), lints={k: v.get("rate") for k, v in lints.items()})
        return ledger

    # -- proposals ------------------------------------------------------------------
    def open_proposal(self, workflow: str, *, tier: int, claim: str, evidence: list[str],
                      lint: str = "", stage: str = "", loss: float = 0.0, proxy: bool = False,
                      suggestion: dict | None = None, rate_at_open: float = 0.0) -> Proposal:
        with self.locked(workflow):
            return self._open_proposal(workflow, tier=tier, claim=claim, evidence=evidence, lint=lint,
                                       stage=stage, loss=loss, proxy=proxy, suggestion=suggestion,
                                       rate_at_open=rate_at_open)

    def _open_proposal(self, workflow: str, *, tier: int, claim: str, evidence: list[str],
                       lint: str = "", stage: str = "", loss: float = 0.0, proxy: bool = False,
                       suggestion: dict | None = None, rate_at_open: float = 0.0) -> Proposal:
        ledger = self.load(workflow)
        # one open proposal per (workflow, lint|stage): repeats update, never duplicate
        for p in ledger.proposals:
            if p.state in ("open", "experiment-registered") and p.lint == lint and p.stage == stage and (lint or stage):
                p.updated_at = self._clock()
                p.evidence = list(dict.fromkeys(p.evidence + evidence))[:20]
                p.loss = max(p.loss, loss)
                if suggestion:
                    p.suggestion = dict(suggestion)
                p.history.append({"at": p.updated_at, "event": "refreshed", "claim": claim[:200]})
                self.save(ledger)
                return p
        now = self._clock()
        proposal = Proposal(id=f"prop-{int(now)}-{uuid.uuid4().hex[:6]}", workflow=workflow, tier=tier,
                            claim=claim, lint=lint, stage=stage, loss=loss, evidence=evidence[:20],
                            opened_at=now, updated_at=now, proxy=proxy, suggestion=dict(suggestion or {}),
                            rate_at_open=rate_at_open, history=[{"at": now, "event": "opened"}])
        ledger.proposals.append(proposal)
        self.save(ledger)
        self._record("proposal_state", workflow=workflow, proposal=proposal.id, state="open", tier=tier,
                     lint=lint, stage=stage, claim=claim[:300], evidence=evidence[:20])
        return proposal

    def transition(self, workflow: str, proposal_id: str, state: str, *, human: bool = False, **detail: Any) -> Proposal:
        if state not in STATES:
            raise LedgerError(f"unknown proposal state {state!r}")
        with self.locked(workflow):
            return self._transition(workflow, proposal_id, state, human=human, **detail)

    def _transition(self, workflow: str, proposal_id: str, state: str, *, human: bool = False, **detail: Any) -> Proposal:
        ledger = self.load(workflow)
        for p in ledger.proposals:
            if p.id == proposal_id:
                if state not in TRANSITIONS[p.state]:
                    raise LedgerError(f"proposal {proposal_id}: {p.state} -> {state} is not a valid transition")
                now = self._clock()
                p.history.append({"at": now, "event": state, **{k: str(v)[:200] for k, v in detail.items()}})
                p.state = state
                p.updated_at = now
                if human:
                    p.last_human_touch = now
                    ledger.last_human_touch = now
                for key, value in detail.items():
                    if hasattr(p, key) and key not in ("history", "state"):
                        setattr(p, key, value)
                self.save(ledger)
                self._record("proposal_state", workflow=workflow, proposal=proposal_id, state=state, **{
                    k: str(v)[:200] for k, v in detail.items()})
                return p
        raise LedgerError(f"no proposal {proposal_id} in {workflow}")

    def human_touch(self, workflow: str, proposal_id: str | None = None) -> None:
        with self.locked(workflow):
            self._human_touch(workflow, proposal_id)

    def _human_touch(self, workflow: str, proposal_id: str | None = None) -> None:
        ledger = self.load(workflow)
        now = self._clock()
        ledger.last_human_touch = now
        for p in ledger.proposals:
            if proposal_id is None or p.id == proposal_id:
                p.last_human_touch = now
        self.save(ledger)

    def stale_sweep(self, workflow: str, now: float | None = None) -> list[str]:
        """Proposals untouched by a human or a PR for STALE_AFTER become stale;
        returns their ids. A workflow whose proposals are all stale has a dead
        channel (the weekly report says so first)."""
        now = self._clock() if now is None else now
        with self.locked(workflow):
            return self._stale_sweep(workflow, now)

    def _stale_sweep(self, workflow: str, now: float) -> list[str]:
        ledger = self.load(workflow)
        stale: list[str] = []
        for p in ledger.proposals:
            if p.state in ("open", "neutral", "underpowered", "supported"):
                last = max(p.last_human_touch, p.opened_at)
                if now - last >= STALE_AFTER:
                    p.history.append({"at": now, "event": "stale"})
                    p.state = "stale"
                    p.updated_at = now
                    stale.append(p.id)
        if stale:
            self.save(ledger)
            for pid in stale:
                self._record("proposal_state", workflow=workflow, proposal=pid, state="stale")
        return stale

    def channel_dead(self, workflow: str, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        ledger = self.load(workflow)
        live = [p for p in ledger.proposals if p.state not in ("landed", "closed")]
        if not live:
            return False
        return now - max([ledger.last_human_touch] + [p.last_human_touch for p in live]) >= STALE_AFTER \
            and all(p.state == "stale" for p in live)

    def set_hold(self, workflow: str, reason: str, *, source: str = "operator") -> None:
        with self.locked(workflow):
            ledger = self.load(workflow)
            ledger.hold = reason
            ledger.hold_source = source
            self.save(ledger)
        self._record("hold", workflow=workflow, reason=reason[:200], source=source)

    def clear_hold(self, workflow: str) -> None:
        with self.locked(workflow):
            ledger = self.load(workflow)
            ledger.hold = ""
            ledger.hold_source = ""
            self.save(ledger)
        self._record("hold_cleared", workflow=workflow)

    # publication bookkeeping (never the state: that goes through transition)
    NOTE_FIELDS = ("issue", "pending_action", "pending_since", "published_state", "issue_state", "landed_by",
                   "closed_reason", "synced_at", "channel_hold", "hold_url")

    def note(self, workflow: str, proposal_id: str, *, event: str = "", detail: str = "", **fields: Any) -> Proposal:
        unknown = sorted(set(fields) - set(self.NOTE_FIELDS))
        if unknown:
            raise LedgerError(f"note() may not set {unknown}")
        with self.locked(workflow):
            ledger = self.load(workflow)
            for p in ledger.proposals:
                if p.id == proposal_id:
                    for key, value in fields.items():
                        setattr(p, key, value)
                    if event:
                        entry = {"at": self._clock(), "event": event}
                        if detail:
                            entry["detail"] = detail[:200]
                        p.history.append(entry)
                    self.save(ledger)
                    if event:
                        self._record("proposal_note", workflow=workflow, proposal=proposal_id, event=event,
                                     **{k: str(v)[:200] for k, v in fields.items()})
                    return p
        raise LedgerError(f"no proposal {proposal_id} in {workflow}")
