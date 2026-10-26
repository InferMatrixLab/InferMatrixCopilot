"""Outcome adapters: how a Tier 2 workflow is scored (design §6.1).

Two data models, never mixed:

* the **gold matrix** (gold entry × unit) carries recall and the stage-of-loss
  attribution — `match()` returns one `Match` per gold entry
  (hit / miss / disputed / unlabeled);
* **finding-level validity** carries precision — `findings()` returns one
  `FindingLabel` per finding the unit produced (valid / invalid / unlabeled),
  from a judge or a proxy, never from the gold matrix;
* **review-level scores** a paired judge gave the whole review are kept as
  they are (`review_scores()`), never recomputed.

Every method consumes only `outcome` records already in the trace store, so
a matrix or a score can be recomputed at any time and always comes out the
same. The engine derives the aggregate numbers (`scores_from`) and stamps
each with its source, so a pre-registered experiment names exactly which
one it uses.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Protocol

MATCH_STATUSES = ("hit", "miss", "disputed", "unlabeled")
VALIDITY = ("valid", "invalid", "unlabeled")


@dataclass(frozen=True)
class GoldEntry:
    gold_id: str
    path: str
    concern: str
    kind: str = "defect"          # defect | validation | question
    severity_hint: str = ""
    source_comments: tuple[int, ...] = ()


@dataclass(frozen=True)
class Gold:
    item: str
    entries: tuple[GoldEntry, ...]
    version: str = ""             # sha256 of the curated file
    status: str = "curated"       # draft | curated


@dataclass(frozen=True)
class Outcome:
    """What the store knows happened after a unit (the outcome records)."""
    unit_id: str
    records: tuple[dict, ...] = ()

    def of_type(self, kind_type: str) -> list[dict]:
        return [r for r in self.records if (r.get("result") or {}).get("type") == kind_type]


@dataclass(frozen=True)
class Match:
    gold_id: str
    status: str                   # hit | miss | disputed | unlabeled
    finding_id: str | None = None
    evidence: tuple[str, ...] = ()   # record ids


@dataclass(frozen=True)
class FindingLabel:
    finding_id: str
    validity: str                 # valid | invalid | unlabeled
    source: str                   # judge | proxy
    evidence: tuple[str, ...] = ()


def collect_gold_votes(count, vote, *, text, quote_label):
    """Collect independent votes, requiring every claimed hit to quote its input."""
    from ..judges import JudgeError

    votes = []
    for _ in range(count):
        try:
            verdict = vote()
        except JudgeError as exc:
            votes.append({"status": "error", "quote": "", "error": str(exc)[:200]})
            continue
        status = str(verdict.get("status") or "").lower()
        quote = str(verdict.get("quote") or "").strip()
        if status == "hit" and (not quote or quote not in text):
            status, quote = "miss", quote and f"[quote not in {quote_label}] {quote[:120]}"
        votes.append({"status": status if status in ("hit", "miss") else "error", "quote": quote[:300]})
    return votes


def record_gold_match(unit, entry, gold, store, *, votes, judge, inconclusive):
    """Cache only a majority of all votes; failures leave the cell retryable."""
    hits = sum(1 for vote in votes if vote["status"] == "hit")
    misses = sum(1 for vote in votes if vote["status"] == "miss")
    if hits * 2 > len(votes):
        status = "hit"
    elif misses * 2 > len(votes):
        status = "miss"
    else:
        inconclusive.append({"unit_id": unit.unit_id, "gold_id": entry.gold_id, "votes": votes})
        return False
    quote = next((vote["quote"] for vote in votes if vote["status"] == "hit"), "")
    store.append("outcome", context={"unit_id": unit.unit_id, "item": unit.item, "of": unit.unit_id,
                                     "workflow": unit.workflow},
                 result={"type": "gold_match", "gold_id": entry.gold_id, "status": status, "quote": quote,
                         "votes": votes, "gold_version": gold.version, "judge": judge})
    return True


def gold_matches(gold: Gold, outcome: Outcome) -> list[Match]:
    """Replay the newest recorded decision for every gold entry, in gold order."""
    latest = {str(row["result"].get("gold_id")): row for row in outcome.of_type("gold_match")}
    out = []
    for entry in gold.entries:
        row = latest.get(entry.gold_id)
        out.append(Match(entry.gold_id, str(row["result"].get("status") or "unlabeled"), evidence=(row["id"],))
                   if row is not None else Match(entry.gold_id, "unlabeled"))
    return out


class OutcomeAdapter(Protocol):
    name: str
    descriptive_only: bool

    def fetch(self, unit: Any, store: Any) -> Outcome | None: ...
    def gold(self, item: str) -> Gold | None: ...
    def match(self, unit: Any, gold: Gold, outcome: Outcome) -> list[Match]: ...
    def findings(self, unit: Any, outcome: Outcome) -> list[FindingLabel]: ...
    def review_scores(self, unit: Any, outcome: Outcome) -> dict[str, float] | None: ...


@dataclass
class Scores:
    """Aggregate numbers with their source, derived by the engine."""
    values: dict[str, float] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)


def scores_from(matches: list[Match], labels: list[FindingLabel],
                review: dict[str, float] | None, *, descriptive_only: bool = False) -> Scores:
    out = Scores()
    hit = sum(1 for m in matches if m.status == "hit")
    miss = sum(1 for m in matches if m.status == "miss")
    if hit + miss:
        out.values["recall_gold"] = hit / (hit + miss)
        out.sources["recall_gold"] = "gold-matrix"
    valid = sum(1 for f in labels if f.validity == "valid")
    invalid = sum(1 for f in labels if f.validity == "invalid")
    if valid + invalid:
        src = {f.source for f in labels if f.validity != "unlabeled"}
        key = "precision_proxy" if src == {"proxy"} else "precision_findings"
        out.values[key] = valid / (valid + invalid)
        out.sources[key] = "finding-labels:" + "+".join(sorted(src))
    for k, v in (review or {}).items():
        out.values[f"{k}_review"] = float(v)
        out.sources[f"{k}_review"] = "judge-aggregate"
    if descriptive_only:
        out.sources = {k: v + ";descriptive-only" for k, v in out.sources.items()}
    return out


def load_adapter(spec: str, **kwargs: Any) -> OutcomeAdapter:
    """``"module:Class"`` -> an adapter instance (constructed with ``kwargs``)."""
    module, _, cls = spec.partition(":")
    if not module or not cls:
        raise ValueError(f"adapter spec must be 'module:Class', got {spec!r}")
    return getattr(importlib.import_module(module), cls)(**kwargs)
