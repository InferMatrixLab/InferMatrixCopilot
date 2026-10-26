"""Tier 1: mechanical forensics over trace/1 units (design §5).

Every lint is deterministic, calls no model, and names the records that
support each finding. The catalogue is the campaign record's defect classes
(T3, wave-2, wave-3, strict-vs-opus5); each entry carries a version that
enters the engine's own fingerprint. A lint whose evidence shape a producer
does not emit yet simply finds nothing — it never guesses.

Evidence shapes (what a lint reads):

* ``model_call``: ``result.stop_reason``, ``result.max_tokens``, ``usage``,
  ``outputs.reply`` blob, ``error``, ``model.model`` vs ``model.served_model``
* ``tool_call``: ``result.tool/ok/refused/out_of_scope``, ``inputs.args`` blob
* ``decision``: ``result.type`` in ``budget_exhausted``, ``contract_reject``,
  ``cap``, ``evidence_cap``, ``fallback``, ``planner``; ``result.status``;
  the review body in ``outputs.review``/``outputs.reply``; ``result.findings``
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from dataclasses import dataclass, field
from typing import Callable

from ..trace_store import TraceStore
from .reader import Unit, blob_ok, blob_text, output_text

EMPTY_SHA = hashlib.sha256(b"").hexdigest()
LINT_VERSION = 1
COST_POLICY = 'model-facts/v1'


@dataclass(frozen=True)
class Finding:
    lint: str
    unit_id: str
    workflow: str
    detail: str
    evidence: tuple[str, ...]      # record ids
    quarantine: bool = False       # the unit is excluded from every statistic


@dataclass
class Baseline:
    """Per-workflow history the rate/outlier lints compare against (robust:
    median and MAD over the previous cycles' units)."""
    usd: list[float] = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)
    parse_failure_rate: float = 0.0
    settings: object = None   # the run's Settings: prices must match the baseline's

    @staticmethod
    def robust_z(value: float, samples: list[float]) -> float | None:
        if len(samples) < 8:
            return None
        med = statistics.median(samples)
        # a perfectly uniform history has MAD 0, which would flag any change
        # as an infinite outlier: floor the scale at a tenth of the median
        mad = max(statistics.median(abs(s - med) for s in samples), 0.1 * abs(med), 1e-9)
        return 0.6745 * (value - med) / mad


LintFn = Callable[[Unit, TraceStore, Baseline], list[Finding]]


@dataclass(frozen=True)
class Lint:
    id: str
    version: int
    description: str
    origin: str
    fn: LintFn


REGISTRY: dict[str, Lint] = {}


def lint(id: str, description: str, origin: str, version: int = LINT_VERSION):
    def deco(fn: LintFn) -> LintFn:
        REGISTRY[id] = Lint(id, version, description, origin, fn)
        return fn
    return deco


def _f(lint_id: str, unit: Unit, detail: str, *records: dict, quarantine: bool = False) -> Finding:
    return Finding(lint_id, unit.unit_id, unit.workflow, detail, tuple(r["id"] for r in records), quarantine)


def _reply_is_empty(store: TraceStore, record: dict) -> bool:
    ref = (record.get("outputs") or {}).get("reply")
    if ref is None:
        return False
    return str(ref).removeprefix("sha256:") == EMPTY_SHA or blob_text(store, ref).strip() == ""


def _final_model_call(unit: Unit) -> dict | None:
    calls = unit.model_calls
    return calls[-1] if calls else None


def _decisions_of(unit: Unit, *types: str) -> list[dict]:
    out = []
    for d in unit.decisions:
        res = d.get("result") or {}
        if str(res.get("type") or res.get("status") or "") in types:
            out.append(d)
    return out


# -- the catalogue -----------------------------------------------------------------------

@lint("L01", "final reply truncated at the completion ceiling", "wave-3 attempt 1 (16k ceiling on 7/10)")
def l01_truncated(unit, store, baseline):
    out = []
    for r in unit.model_calls:
        res = r.get("result") or {}
        usage = r.get("usage") or {}
        stop = str(res.get("stop_reason") or "").lower()
        cap = res.get("max_tokens")
        hit_cap = isinstance(cap, int) and cap > 0 and int(usage.get("output_tokens") or 0) >= cap
        if stop in ("max_tokens", "length") or hit_cap:
            out.append(_f("L01", unit, f"stop_reason={stop or '?'} output_tokens={usage.get('output_tokens')} max_tokens={cap}", r))
    return out


@lint("L02", "forced final reply after the budget was exhausted", "T3 issue4842")
def l02_budget_exhausted(unit, store, baseline):
    out = [_f("L02", unit, "budget_exhausted decision", d) for d in _decisions_of(unit, "budget_exhausted")]
    for r in unit.model_calls:
        res = r.get("result") or {}
        if res.get("session") and res.get("truncated"):
            out.append(_f("L02", unit, f"harness session truncated after {res.get('iterations')} iterations", r))
    return out


@lint("L03", "empty final reply after tool calls", "wave-2 pr5976 (two passes, zero candidates)")
def l03_empty_final(unit, store, baseline):
    final = _final_model_call(unit)
    if final is None or not unit.tool_calls or final.get("error"):
        return []
    if _reply_is_empty(store, final) and not json.loads(blob_text(store, (final.get("outputs") or {}).get("tool_calls", "")) or "[]"):
        return [_f("L03", unit, f"empty final reply after {len(unit.tool_calls)} tool calls", final)]
    return []


@lint("L04", "non-empty reply discarded for a missing contract field", "T3 (665-token answer discarded)")
def l04_contract_reject(unit, store, baseline):
    return [_f("L04", unit, str((d.get("result") or {}).get("reason") or "contract reject"), d)
            for d in _decisions_of(unit, "contract_reject")]


@lint("L05", "JSON parse failure / repair round rate above the workflow baseline", "iter-3 reducer")
def l05_parse_failures(unit, store, baseline):
    failed = [r for r in unit.model_calls
              if re.search(r"json|parse|repair", str(r.get("error") or "") + str((r.get("result") or {}).get("parse") or ""), re.I)]
    if not failed or not unit.model_calls:
        return []
    rate = len(failed) / len(unit.model_calls)
    if rate > max(0.2, 2 * baseline.parse_failure_rate):
        return [_f("L05", unit, f"{len(failed)}/{len(unit.model_calls)} calls failed to parse (baseline {baseline.parse_failure_rate:.2f})", *failed)]
    return []


@lint("L06", "the same text block rendered more than once in the artifact", "T3 review_text x3")
def l06_duplicate_rendering(unit, store, baseline):
    out = []
    for d in unit.decisions:
        text = output_text(store, d)
        if len(text) < 400:
            continue
        blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if len(b.strip()) >= 200]
        seen: dict[str, int] = {}
        for b in blocks:
            seen[b] = seen.get(b, 0) + 1
        dups = [b for b, n in seen.items() if n > 1]
        if dups:
            out.append(_f("L06", unit, f"{len(dups)} block(s) repeated ({max(seen.values())}x)", d))
    return out


@lint("L07", "silent fallback (member, tier or planner) or served model differs from the requested one", "wave-3 gray-zone planner")
def l07_silent_fallback(unit, store, baseline):
    out = [_f("L07", unit, str((d.get("result") or {}).get("type")), d)
           for d in _decisions_of(unit, "moa_member_fallback", "tier_fallback", "planner_zero_output", "fallback")]
    from ..llm import canonical_model

    aliases = getattr(baseline.settings, "model_aliases", None) or {}
    for r in unit.model_calls:
        m = r.get("model") or {}
        if m.get("served_model") and m.get("model") \
                and canonical_model(m["served_model"], aliases) != canonical_model(m["model"], aliases) \
                and not str(m.get("provider", "")).startswith("harness:"):
            out.append(_f("L07", unit, f"requested {m['model']} served {m['served_model']}", r))
    return out


@lint("L08", "a pass with tool calls but zero candidates", "T3 candidate-yield bottleneck")
def l08_zero_yield(unit, store, baseline):
    if len(unit.tool_calls) < 5:
        return []
    out = []
    for d in unit.decisions:
        res = d.get("result") or {}
        findings = res.get("findings") if "findings" in res else res.get("candidates")
        if isinstance(findings, list) and not findings and not res.get("no_issue"):
            out.append(_f("L08", unit, f"{len(unit.tool_calls)} tool calls, 0 candidates", d))
    return out


@lint("L09", "candidates dropped by a deterministic cap", "strict-vs-opus5 (5-cap deleted GT)")
def l09_cap_drops(unit, store, baseline):
    out = []
    for d in _decisions_of(unit, "cap"):
        res = d.get("result") or {}
        dropped = int(res.get("dropped") or 0)
        if dropped > 0:
            out.append(_f("L09", unit, f"{dropped} candidate(s) dropped at cap {res.get('cap')}", d))
    return out


@lint("L10", "unresolved anchors rendered in the artifact", "wave-2 report assembly (file:?)")
def l10_unresolved_anchors(unit, store, baseline):
    out = []
    for d in unit.decisions:
        text = output_text(store, d)
        n = len(re.findall(r"(?<!\S)\S+:(?:\?|~declared)(?!\S)", text))
        if n:
            out.append(_f("L10", unit, f"{n} unresolved anchor(s)", d))
    return out


@lint("L11", "cost or latency outlier versus the workflow's own history", "RQS3e time penalty", version=2)
def l11_outliers(unit, store, baseline):
    out = []
    usd = unit_usd(unit, baseline.settings)
    z = Baseline.robust_z(usd, baseline.usd) if usd is not None else None
    if z is not None and z > 3:
        out.append(_f("L11", unit, f"usd {usd:.3f} robust z={z:.1f}", *unit.model_calls[:3]))
    z = Baseline.robust_z(unit.seconds, baseline.seconds)
    if z is not None and z > 3:
        out.append(_f("L11", unit, f"seconds {unit.seconds:.0f} robust z={z:.1f}", *unit.records[:3]))
    return out


@lint("L12", "tool refused, out-of-scope edit or full-file write", "invariant 5")
def l12_refusals(unit, store, baseline):
    out = []
    for r in unit.tool_calls:
        res = r.get("result") or {}
        if res.get("refused") or res.get("out_of_scope"):
            out.append(_f("L12", unit, f"{res.get('tool')}: {'refused' if res.get('refused') else 'out of scope'}", r))
    return out


_PROVIDER_ERR = re.compile(r"\b(402|429|5\d\d)\b|insufficient balance|rate ?limit|overloaded|provider down|connection", re.I)


@lint("L13", "provider error interrupted the unit (quarantined from every statistic)", "wave-4 r2 INVALID (402)")
def l13_provider_error(unit, store, baseline):
    hits = [r for r in unit.model_calls if r.get("error") and _PROVIDER_ERR.search(str(r["error"]))]
    if not hits:
        return []
    return [_f("L13", unit, str(hits[0]["error"])[:120], *hits, quarantine=True)]


@lint("L14", "trace completeness (trace/1 native rules)", "verify_traces.py")
def l14_completeness(unit, store, baseline):
    problems: list[str] = []
    evidence: list[dict] = []
    for r in unit.model_calls:
        has_reply = "reply" in (r.get("outputs") or {})
        if not has_reply and not r.get("error"):
            problems.append("model_call with neither a reply nor an error")
            evidence.append(r)
        if r.get("seconds") is None:
            problems.append("model_call without seconds")
            evidence.append(r)
        if has_reply and not r.get("error") and not (r.get("usage") or {}):
            problems.append("model_call without usage")
            evidence.append(r)
    for r in unit.records:
        for ref in list((r.get("inputs") or {}).values()) + list((r.get("outputs") or {}).values()):
            if not blob_ok(store, ref):
                problems.append(f"unresolvable blob {str(ref)[:20]}")
                evidence.append(r)
    if unit.declared:
        first = unit.records[0].get("context") or {}
        for key in ("unit_id", "item", "fingerprint"):
            if not first.get(key) and not getattr(unit, key if key != "unit_id" else "unit_id", ""):
                problems.append(f"declared unit without context.{key}")
                evidence.append(unit.records[0])
        if unit.model_calls and not unit.decisions:
            problems.append("declared unit with model calls but no terminal decision")
            evidence.append(unit.records[-1])
    if not problems:
        return []
    uniq = {p: None for p in problems}
    return [_f("L14", unit, "; ".join(list(uniq)[:4]), *evidence[:6])]


@lint("L15", "the same tool called with identical arguments three or more times", "agent loops")
def l15_repeated_tool_calls(unit, store, baseline):
    seen: dict[tuple[str, str], list[dict]] = {}
    for r in unit.tool_calls:
        key = (str((r.get("result") or {}).get("tool")), str((r.get("inputs") or {}).get("args")))
        seen.setdefault(key, []).append(r)
    out = []
    for (tool, _), recs in seen.items():
        if len(recs) >= 3:
            out.append(_f("L15", unit, f"{tool} x{len(recs)} with identical arguments", *recs[:3]))
    return out


@lint("L16", "evidence pack dropped hunks/files at a cap", "170k diff losing 30% of hunks")
def l16_evidence_cap(unit, store, baseline):
    out = []
    for d in _decisions_of(unit, "evidence_cap"):
        res = d.get("result") or {}
        ratio = float(res.get("dropped_ratio") or 0)
        if ratio > 0:
            out.append(_f("L16", unit, f"{ratio:.0%} of the evidence dropped at the cap", d))
    return out


_CLAIM = re.compile(r"\b(verified|confirmed|tests? pass(?:ed|es)?|is merged|was merged|ran the tests)\b", re.I)


@lint("L17", "an epistemic claim (verified/merged/tests pass) without a supporting tool call", "T3 #7 issue4891")
def l17_unbacked_claims(unit, store, baseline):
    tools_used = {str((r.get("result") or {}).get("tool")) for r in unit.tool_calls if (r.get("result") or {}).get("ok")}
    backed = any(t.startswith(("gh_", "run_", "show_commit", "search_history", "diff_stat")) for t in tools_used)
    out = []
    for d in unit.decisions:
        if (d.get("result") or {}).get("tests_run"):
            backed = True
        text = output_text(store, d)
        if text and _CLAIM.search(text) and not backed:
            out.append(_f("L17", unit, "claim without a gh/test/archaeology tool call", d))
    return out


# -- helpers ------------------------------------------------------------------------------

@dataclass(frozen=True)
class UnitCost:
    known_usd: float
    unknown_calls: int

    @property
    def usd(self) -> float | None:
        return None if self.unknown_calls else self.known_usd


def unit_cost(unit: Unit, settings=None) -> UnitCost:
    from .budget import record_usd

    total, unknown = 0.0, 0
    for record in unit.model_calls:
        amount = record_usd(record, settings)
        if amount is None:
            unknown += 1
        else:
            total += amount
    return UnitCost(round(total, 6), unknown)


def unit_usd(unit: Unit, settings=None) -> float | None:
    return unit_cost(unit, settings).usd


def run_lints(unit: Unit, store: TraceStore, baseline: Baseline,
              only: tuple[str, ...] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for lint_id, entry in REGISTRY.items():
        if only and lint_id not in only:
            continue
        try:
            findings.extend(entry.fn(unit, store, baseline))
        except Exception as exc:  # noqa: BLE001 - one broken lint must not hide the others
            findings.append(Finding("L00", unit.unit_id, unit.workflow,
                                    f"lint {lint_id} crashed: {type(exc).__name__}: {exc}", ()))
    return findings


def catalogue() -> list[dict]:
    return [{"id": e.id, "version": e.version, "description": e.description, "origin": e.origin}
            for e in REGISTRY.values()]
