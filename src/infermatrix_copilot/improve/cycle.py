"""The weekly cycle (design §4, §5, §9, §10 — the P1 slice).

Order inside a cycle: preflight (kill switch, holds) -> Tier 1 lints and
baselines over every unit in the window -> ledger update and Tier 1
proposals for lints that worsened -> stale sweep -> the cycle report. Tier 2
forensics, experiments and publishing arrive in later phases; the cycle
already leaves a `decision(result.type="cycle")` record with everything it
did, and a Markdown report under the ledger directory.

P1 runs in dry-run only: proposals live in the ledger and the trace store,
never on GitHub.
"""

from __future__ import annotations

from ..persistence import atomic_write_bytes

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..trace_store import TraceStore, bind_store, file_lock, trace_context
from .enroll import declarations_for
from .budget import Governor, governed
from .ledger import Ledger
from .lints import COST_POLICY, Baseline, Finding, catalogue, run_lints, unit_cost
from .reader import UNIT_LOOKBACK, Unit, collect_units, cursor_path, week_bounds

WORSEN_RATIO = 1.5      # a lint's defect rate this cycle / last cycle
WORSEN_MIN_UNITS = 3    # ... and at least this many affected units
DEFAULT_LEDGER_DIR = Path.home() / ".infermatrix-copilot" / "improve"


def ledger_dir_for(settings: Any) -> Path:
    """THE ledger directory: settings.improve_ledger_dir, else the shared
    default — the scheduler, the CLI and the playbook must agree on it (they
    share the cursor, the ledgers, the holds and the cycle lock)."""
    configured = str(getattr(settings, "improve_ledger_dir", "") or "")
    return Path(configured).expanduser() if configured else DEFAULT_LEDGER_DIR


class CycleRefused(RuntimeError):
    pass


@dataclass
class WorkflowStats:
    workflow: str
    declared: bool
    tier: int
    units: list[Unit] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    usd: float = 0.0
    cost_unknown_calls: int = 0
    seconds: float = 0.0

    def lint_table(self) -> dict[str, dict]:
        counted = [u for u in self.units if u.unit_id not in self.quarantined]
        n = len(counted) or 1
        table: dict[str, dict] = {}
        for f in self.findings:
            if f.unit_id in self.quarantined and f.lint != "L13":
                continue
            entry = table.setdefault(f.lint, {"units": set(), "findings": 0})
            entry["units"].add(f.unit_id)
            entry["findings"] += 1
        return {lint: {"units": len(v["units"]), "findings": v["findings"], "rate": round(len(v["units"]) / n, 4)}
                for lint, v in sorted(table.items())}


def _cursor(ledger_dir: Path) -> dict:
    path = cursor_path(ledger_dir)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_cursor(ledger_dir: Path, data: dict) -> None:
    path = cursor_path(ledger_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, json.dumps(data, indent=1).encode("utf-8"))


def is_due(now: float, last_run_at: float, *, weekday: int, hour: int, tz: dt.tzinfo | None = None) -> bool:
    """One cycle per week: due once `now` has passed `hour:00` on `weekday`
    (0=Monday) and the last run was before that moment."""
    local = dt.datetime.fromtimestamp(now, tz or dt.timezone.utc)
    days_back = (local.weekday() - weekday) % 7
    slot = (local - dt.timedelta(days=days_back)).replace(hour=hour, minute=0, second=0, microsecond=0)
    if slot > local:
        slot -= dt.timedelta(days=7)
    return last_run_at < slot.timestamp()


def governor_for(settings: Any, ledger_dir: str | Path, *, now: float | None = None) -> Governor:
    """The week's governor from settings (the dollar and judge-call
    envelopes); every model and judge call of a cycle, an experiment or a
    forensics pass is reserved against it."""
    return Governor(ledger_dir, usd_week=float(getattr(settings, "improve_budget_usd_week", 20.0)),
                    judge_calls_week=int(getattr(settings, "improve_budget_judge_calls_week", 300)),
                    settings=settings, clock=(lambda: now) if now is not None else time.time)


def preflight(settings: Any, ledger: Ledger) -> dict:
    """The kill switch and the per-workflow holds; refuses loudly."""
    if not bool(getattr(settings, "improve_enabled", False)):
        raise CycleRefused("improve_enabled is off (the kill switch)")
    holds = {}
    for workflow in ledger.workflows():
        wl = ledger.load(workflow)
        if wl.hold:
            holds[workflow] = wl.hold
    return {"holds": holds}


def run_cycle(store: TraceStore, settings: Any, ledger_dir: str | Path, *, now: float | None = None,
              since: float | None = None, until: float | None = None, dry_run: bool = True,
              force: bool = False) -> dict:
    """Run one cycle over the window ``[since, until)`` (default: since the
    last cycle's `until`, else the previous seven days). Returns the report
    dict (also written as Markdown + JSON under `ledger_dir/reports/`)."""
    now = time.time() if now is None else now
    ledger_dir = Path(ledger_dir)
    # one cycle at a time per ledger directory, whoever starts it (the
    # scheduler, the CLI, a playbook run): the cursor read, the ledgers and
    # the cursor write all happen under this lock
    with file_lock(ledger_dir / "cycle.lock", blocking=False) as held:
        if not held:
            raise CycleRefused(f"another cycle is running on {ledger_dir}")
        return _run_cycle_locked(store, settings, ledger_dir, now=now, since=since, until=until,
                                 dry_run=dry_run, force=force)


def _run_cycle_locked(store: TraceStore, settings: Any, ledger_dir: Path, *, now: float,
                      since: float | None, until: float | None, dry_run: bool, force: bool) -> dict:
    ledger = Ledger(ledger_dir, store, clock=lambda: now)
    if not force:
        gate = preflight(settings, ledger)
    else:
        gate = {"holds": {}}
    cursor = _cursor(ledger_dir)
    if since is None:
        since = float(cursor.get("until") or week_bounds(now)[0])
    if until is None:
        until = now
    declarations = declarations_for(settings)
    started = time.time()
    governor = governor_for(settings, ledger_dir, now=now)
    with bind_store(store), governed(governor), trace_context(playbook="workflow-improve", run_id=f"cycle-{int(now)}"):
        # the engine's own records are units too (self-enrolment, design §11.2):
        # the previous cycles' records fall in this window; this cycle's are
        # written after the read and belong to the next one
        counted = {k for k, ended in (cursor.get("counted") or {}).items() if float(ended) >= since - UNIT_LOOKBACK}
        # the actual deferred state and the engine's origin travel in the
        # cursor; a window that continues the previous one is contiguous
        # coverage, an explicit `--since` elsewhere is not
        previous_until = cursor.get("until")
        contiguous = previous_until is None or abs(float(previous_until) - since) < 1e-6
        # coverage is only continuous while each window starts where the last
        # ended; an explicit window elsewhere breaks it, and the origin moves
        # to that window's start so skipped work never leaks in later
        origin = float(cursor["origin"]) if contiguous and cursor.get("origin") is not None else since
        cursor_migrated = False
        legacy_cutoff = None
        if previous_until is not None and "deferred" not in cursor and contiguous:
            # a cursor written before the deferred set existed: the old cycle
            # counted every unit that was complete at its cutoff, so the ones
            # it left pending are recovered from their own records at that
            # cutoff (never by widening coverage, which would replay work it
            # already counted)
            legacy_cutoff = float(previous_until)
            cursor_migrated = True
        units, deferred_now = collect_units(store, since, until, counted=counted,
                                            deferred=set(cursor.get("deferred") or []),
                                            origin=origin, contiguous=contiguous,
                                            legacy_cutoff=legacy_cutoff)
        by_workflow: dict[str, WorkflowStats] = {}
        for unit in units.values():
            decl = declarations.get(unit.workflow)
            stats = by_workflow.setdefault(unit.workflow, WorkflowStats(
                unit.workflow, unit.declared, 2 if (decl is not None and decl.tier2) else 1))
            stats.units.append(unit)
        proposals_opened: list[dict] = []
        report_workflows: dict[str, dict] = {}
        for workflow, stats in sorted(by_workflow.items()):
            prior = ledger.load(workflow)
            baseline = Baseline(usd=list(prior.baseline.get("planning_usd") or []),
                                seconds=list(prior.baseline.get("seconds") or []),
                                parse_failure_rate=float(prior.baseline.get("parse_failure_rate") or 0.0),
                                settings=settings)
            usd_samples, seconds_samples, parse_failed, parse_total = [], [], 0, 0
            for unit in stats.units:
                from . import objectives
                if objectives.enabled(settings) and settings.improve_evolve_enabled:
                    from .runtime import run_lints as runtime_lints
                    findings = runtime_lints(unit, store, baseline)
                else:
                    findings = run_lints(unit, store, baseline)
                stats.findings.extend(findings)
                if any(f.quarantine for f in findings):
                    stats.quarantined.append(unit.unit_id)
                    continue
                cost = unit_cost(unit, settings)
                stats.usd += cost.known_usd
                stats.cost_unknown_calls += cost.unknown_calls
                stats.seconds += unit.seconds
                if cost.usd is not None:
                    usd_samples.append(cost.usd)
                seconds_samples.append(unit.seconds)
                calls = unit.model_calls
                parse_total += len(calls)
                parse_failed += sum(1 for r in calls if "json" in str(r.get("error") or "").lower())
            table = stats.lint_table()
            wl = ledger.record_cycle(workflow, at=now, declared=stats.declared, tier=stats.tier,
                                     units=len(stats.units), quarantined=len(stats.quarantined),
                                     usd=stats.usd, seconds=stats.seconds, lints=table,
                                     usd_samples=usd_samples, seconds_samples=seconds_samples,
                                     parse_failure_rate=(parse_failed / parse_total) if parse_total else 0.0,
                                     cost_policy=COST_POLICY, cost_unknown_calls=stats.cost_unknown_calls)
            # Tier 1 proposals: a lint that worsened beyond the ratio with enough units
            for lint_id, entry in table.items():
                if lint_id in ("L13", "L00"):
                    continue
                previous = wl.lint_rate(lint_id, back=1)
                if previous is None:
                    continue
                prev_rate, _ = previous
                if entry["units"] >= WORSEN_MIN_UNITS and entry["rate"] > max(prev_rate * WORSEN_RATIO, 0.0) \
                        and (prev_rate > 0 or entry["rate"] >= 0.2):
                    evidence = [rid for f in stats.findings if f.lint == lint_id for rid in f.evidence][:12]
                    desc = next((c["description"] for c in catalogue() if c["id"] == lint_id), lint_id)
                    proposal = ledger.open_proposal(
                        workflow, tier=1, lint=lint_id, evidence=evidence,
                        claim=(f"{lint_id} ({desc}) worsened: {entry['rate']:.0%} of units this cycle "
                               f"vs {prev_rate:.0%} last cycle ({entry['units']} units)"),
                        loss=entry["rate"] - prev_rate, rate_at_open=float(entry["rate"]),
                        proxy=(workflow == "rb-review.review"))
                    proposals_opened.append({"workflow": workflow, "id": proposal.id, "lint": lint_id,
                                             "claim": proposal.claim})
            stale = ledger.stale_sweep(workflow, now)
            report_workflows[workflow] = {
                "tier": stats.tier, "declared": stats.declared, "units": len(stats.units),
                "quarantined": len(stats.quarantined), "usd": round(stats.usd, 4),
                "cost_unknown_calls": stats.cost_unknown_calls, "cost_policy": COST_POLICY,
                "seconds": round(stats.seconds, 1), "lints": table, "stale": stale,
                "channel_dead": ledger.channel_dead(workflow, now), "hold": wl.hold,
                "findings": [{"lint": f.lint, "unit": f.unit_id, "detail": f.detail, "evidence": list(f.evidence)}
                             for f in stats.findings][:200],
            }
        # workflows with a ledger but no unit this window still age: their
        # proposals go stale and a dead channel is reported, otherwise a
        # workflow that stopped producing records would never be swept
        for workflow in ledger.workflows():
            if workflow in report_workflows:
                continue
            wl = ledger.load(workflow)
            stale = ledger.stale_sweep(workflow, now)
            report_workflows[workflow] = {
                "tier": wl.tier, "declared": wl.declared, "units": 0, "quarantined": 0, "usd": 0.0,
                "seconds": 0.0, "lints": {}, "stale": stale, "inactive": True,
                "channel_dead": ledger.channel_dead(workflow, now), "hold": wl.hold, "findings": [],
            }
        report = {
            "at": now, "since": since, "until": until, "dry_run": dry_run, "holds": gate["holds"],
            "units": len(units), "counted_ids": sorted(units), "deferred": sorted(deferred_now),
            "cursor_migrated": cursor_migrated, "workflows": report_workflows,
            "proposals_opened": proposals_opened,
            "lint_catalogue": catalogue(), "seconds": round(time.time() - started, 3),
            "budget": governor.remaining(),
        }
        try:
            store.append("decision", inputs={"report": json.dumps(report, ensure_ascii=False, default=str)},
                         result={"type": "cycle", "at": now, "since": since, "until": until, "units": len(units),
                                 "workflows": len(report_workflows), "proposals_opened": len(proposals_opened),
                                 "dry_run": dry_run})
        except Exception:  # noqa: BLE001 - the report file is the primary output
            pass
    _write_report(ledger_dir, report)
    # the counted set keeps a unit from being counted twice once its late
    # records arrive; pruned to the lookback span the next cycle can see
    counted_now = {k: u.ended for k, u in units.items()}
    kept = {k: e for k, e in (cursor.get("counted") or {}).items() if float(e) >= until - UNIT_LOOKBACK}
    _save_cursor(ledger_dir, {"until": until, "last_run_at": now, "cycles": int(cursor.get("cycles") or 0) + 1,
                              "origin": origin, "deferred": sorted(deferred_now),
                              "counted": {**kept, **counted_now}})
    return report


def open_tier2_proposals(ledger: Ledger, ledger_dir: Path, workflow: str, punch_list: list[dict], *,
                         descriptive_only: bool, items: list[str], metric: str = "recall_gold",
                         min_effect: float = 0.05) -> list[dict]:
    """One proposal per punch-list stage with a loss (design §9.2: one fix,
    one PR), each carrying a suggested pre-registration: the metric, the
    items with gold, the item count the historical sd requires and what the
    arm's fingerprint should cover for that stage. S0 (unattributable) and
    S10 (measurement) never become proposals; a descriptive-only workflow's
    proposals are proxy-labelled."""
    from .experiments import PRIOR_SD, _historical_sd
    from .publish import STAGE_HINTS
    from . import stats

    opened = []
    for bucket in punch_list:
        stage = str(bucket.get("stage") or "")
        loss = int(bucket.get("loss") or 0)
        if stage in ("S0", "S10") or loss < 1:
            continue
        evidence = list(dict.fromkeys(rid for c in bucket.get("cells") or [] for rid in c.get("evidence") or []))[:12]
        if not evidence:
            continue
        sd = _historical_sd(ledger_dir, workflow, metric) or PRIOR_SD
        suggestion = {} if descriptive_only else {
            "metric": metric, "direction": "higher", "min_effect": min_effect,
            "n_required": stats.items_required(sd, min_effect), "sd_item": sd,
            "covers": STAGE_HINTS.get(stage, ""), "items": sorted(items)[:30]}
        units = {c.get("unit_id") for c in bucket.get("cells") or []}
        claim = (f"{stage} ({bucket.get('description', '')}): {loss} missed gold entr{'y' if loss == 1 else 'ies'} "
                 f"lost at this stage" + (f" ({bucket.get('disputed')} disputed)" if bucket.get("disputed") else ""))
        proposal = ledger.open_proposal(workflow, tier=2, stage=stage, claim=claim, evidence=evidence, loss=float(loss),
                                        proxy=descriptive_only, suggestion=suggestion)
        opened.append({"workflow": workflow, "id": proposal.id, "stage": stage, "loss": loss, "units": len(units),
                       "claim": proposal.claim})
    return opened


def _write_report(ledger_dir: Path, report: dict) -> Path:
    reports = ledger_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.fromtimestamp(report["at"], dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    (reports / f"cycle-{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                                                 encoding="utf-8")
    lines = [f"# Improvement cycle {stamp}", ""]
    dead = [w for w, d in report["workflows"].items() if d["channel_dead"]]
    if dead:
        lines.append(f"**CHANNEL MAY BE DEAD** for {', '.join(dead)}: no human or PR touched a proposal in 30 days.")
        lines.append("")
    lines.append(f"Window: {report['since']:.0f} .. {report['until']:.0f} · units {report['units']} · "
                 f"dry-run {report['dry_run']} · holds {report['holds'] or 'none'}")
    lines.append("")
    lines.append("| workflow | tier | units | quarantined | usd | lints (units) |")
    lines.append("|---|---|---|---|---|---|")
    for workflow, d in sorted(report["workflows"].items()):
        lints = ", ".join(f"{k}:{v['units']}" for k, v in d["lints"].items()) or "none"
        cost = f"{d['usd']:.3f}"
        if d.get('cost_unknown_calls'):
            cost += f" + unknown ({d['cost_unknown_calls']} calls)"
        lines.append(f"| {workflow} | {d['tier']}{'' if d['declared'] else ' (undeclared)'} | {d['units']} | "
                     f"{d['quarantined']} | {cost} | {lints} |")
    lines.append("")
    if report["proposals_opened"]:
        lines.append("## Tier 1 proposals opened")
        for p in report["proposals_opened"]:
            lines.append(f"- `{p['id']}` {p['workflow']}: {p['claim']}")
        lines.append("")
    path = reports / f"cycle-{stamp}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def maybe_run_weekly(store: TraceStore, settings: Any, ledger_dir: str | Path, *, now: float | None = None) -> dict | None:
    """The scheduler hook: run a cycle when the weekly slot has passed since
    the last one; None when not due or the kill switch is off."""
    now = time.time() if now is None else now
    if not bool(getattr(settings, "improve_enabled", False)):
        return None
    ledger_dir = Path(ledger_dir)
    # the due check and the run share the lock: two schedulers (or a
    # scheduler and an operator) cannot both decide the slot is free
    with file_lock(ledger_dir / "cycle.lock", blocking=False) as held:
        if not held:
            return None
        cursor = _cursor(ledger_dir)
        if not is_due(now, float(cursor.get("last_run_at") or 0.0),
                      weekday=int(getattr(settings, "improve_cycle_weekday", 0)),
                      hour=int(getattr(settings, "improve_cycle_hour", 5))):
            return None
        return _run_cycle_locked(store, settings, ledger_dir, now=now, since=None, until=None,
                                 dry_run=True, force=False)
