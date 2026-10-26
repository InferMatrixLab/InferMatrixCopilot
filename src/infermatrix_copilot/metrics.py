"""Run metrics — the CATQ family from eval/METRICS_RESEARCH.md.

Per task kind:  CATQ = Q · S / C   (higher is better)

- Q: weighted arithmetic mean of task-specific quality components, computed
  only over components that are actually known (weights renormalized, the
  result flagged `partial`). Judged components (recall/precision juries,
  groundedness) are filled by the eval pipeline or the feedback collector —
  this module never fabricates them.
- S: safety multiplier from typed incidents; geometric decay per incident
  (two severe incidents cost 75%, not 2×25%) and a hard zero on catastrophic.
- C: log-scale cost index over USD and wall-clock minutes against explicit
  per-task reference budgets (RQS3e precedent: at the reference the discount
  is ~23%; each order of magnitude costs one further log step).

Facts only: everything here derives from RunTrace events, progress.json and
task.json in the run dir. `collect_run_metrics` is called after every run and
writes metrics.json next to RUN_REPORT.md; failures never break the run.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover
    from .config import Settings
    from .run_trace import RunTrace

# -- quality ------------------------------------------------------------------

TASK_WEIGHTS: dict[str, dict[str, float]] = {
    "pr_review": {"recall_w": 0.30, "precision_v": 0.25, "useful": 0.15,
                  "calib": 0.10, "decision": 0.20},
    "pr_debug": {"repro": 0.10, "rootcause": 0.10, "builds": 0.05, "f2p": 0.30,
                 "p2p": 0.20, "accepted": 0.15, "explain": 0.10},
    "pr_rebase": {"completed": 0.20, "conflict": 0.30, "tests": 0.25,
                  "purity": 0.15, "push_safe": 0.10},
    "issue_answer": {"correct": 0.35, "grounded": 0.30, "complete": 0.15,
                     "helpful": 0.20},
    "issue_filter": {"label_f1": 0.30, "route": 0.20, "dup": 0.20,
                     "prio": 0.15, "accept": 0.15},
    # repo_rebase delegates to the parent orchestrator; score like pr_rebase
    "repo_rebase": {"completed": 0.20, "conflict": 0.30, "tests": 0.25,
                    "purity": 0.15, "push_safe": 0.10},
}

# Fixed scores for a *justified* safe abstain (abort+escalate with the
# workspace restored / "cannot verify" / honest per-group escalation).
# Deliberately below any decent success so abstention never becomes the
# profitable strategy, and above 0 because a wrong action is worse.
ABSTAIN_SCORES: dict[str, float] = {
    "pr_rebase": 0.35, "repo_rebase": 0.35,
    "issue_answer": 0.30, "pr_debug": 0.25,
}


def quality_score(kind: str, components: dict[str, float | None],
                  ) -> tuple[float | None, float]:
    """Weighted arithmetic mean over the KNOWN components.

    Returns (q, weight_coverage). Unknown (None/missing) components drop out
    and the remaining weights are renormalized; `weight_coverage` says how
    much of the full weight mass was actually measured, so a q built from a
    sliver of the metric is visibly partial. q is None when nothing is known.
    """
    weights = TASK_WEIGHTS.get(kind)
    if not weights:
        return None, 0.0
    known = {k: float(v) for k, v in (components or {}).items()
             if k in weights and v is not None}
    if not known:
        return None, 0.0
    mass = sum(weights[k] for k in known)
    q = sum(weights[k] * min(1.0, max(0.0, v)) for k, v in known.items()) / mass
    return q, mass


# -- cost -----------------------------------------------------------------------

# USD per 1M tokens (input, output); first substring match wins. Estimates at
# list rates — override per deployment with settings.token_price_*_per_mtok.
MODEL_PRICES: tuple[tuple[str, tuple[float, float]], ...] = (
    ("claude-fable", (12.0, 60.0)),
    ("claude-opus", (15.0, 75.0)),
    ("claude-sonnet", (3.0, 15.0)),
    ("claude-haiku", (1.0, 5.0)),
    ("deepseek-reasoner", (0.55, 2.19)),
    ("deepseek", (0.27, 1.10)),
    # estimated public rates for MoA members (cap enforcement needs SOME
    # price; conservative-side estimates, override via settings if needed)
    ("mimo", (0.35, 1.40)),
    ("qwen", (0.40, 1.20)),
)
_DEFAULT_PRICE = (3.0, 15.0)


def model_price(model: str, settings: "Settings | None" = None,
                ) -> tuple[float, float]:
    """(input, output) USD-per-1M-token price for `model`. A configured
    `settings` override wins; otherwise the first `MODEL_PRICES` substring match
    on the model name, falling back to `_DEFAULT_PRICE`."""
    if settings is not None and (settings.token_price_in_per_mtok > 0
                                 or settings.token_price_out_per_mtok > 0):
        return (settings.token_price_in_per_mtok or _DEFAULT_PRICE[0],
                settings.token_price_out_per_mtok or _DEFAULT_PRICE[1])
    name = (model or "").lower()
    for key, price in MODEL_PRICES:
        if key in name:
            return price
    return _DEFAULT_PRICE


def model_price_known(model: str, settings: "Settings | None" = None) -> bool:
    """True when `model` has a real price source (settings override or a
    MODEL_PRICES row). When False, callers must flag the cost as partial —
    pricing an unknown model at the default row fabricates a number (plan v2)."""
    if settings is not None and (settings.token_price_in_per_mtok > 0
                                 or settings.token_price_out_per_mtok > 0):
        return True
    name = (model or "").lower()
    return any(key in name for key, _ in MODEL_PRICES)


def cost_index(usd: float, minutes: float, usd_ref: float, min_ref: float) -> float:
    """C = (1+log10(1+usd/usd_ref)) · (1+log10(1+min/min_ref)); ≥1, lower better."""
    usd_ref = max(usd_ref, 1e-9)
    min_ref = max(min_ref, 1e-9)
    return ((1.0 + math.log10(1.0 + max(0.0, usd) / usd_ref))
            * (1.0 + math.log10(1.0 + max(0.0, minutes) / min_ref)))


# -- risk -----------------------------------------------------------------------

SEVERITIES = ("catastrophic", "severe", "moderate", "minor")
SEVERITY_DECAY = {"severe": 0.5, "moderate": 0.8, "minor": 0.95}

# Existing RunTrace events that already ARE incidents; new code should record
# explicit `incident` events via record_incident() for anything beyond these.
_EVENT_SEVERITY = {
    "out_of_scope_edit": "moderate",
    "tool_refused": "minor",
}


def record_incident(trace: "RunTrace", severity: str, kind: str,
                    detail: str = "") -> None:
    """Typed risk-event ledger entry (the S input). Facts recorded freely."""
    if severity not in SEVERITIES:
        severity = "moderate"
    trace.record("incident", severity=severity, incident_kind=kind,
                 detail=detail[:500])


def derive_incidents(events: Iterable[dict]) -> dict[str, int]:
    """Count incidents by severity from a run's trace events."""
    counts = {s: 0 for s in SEVERITIES}
    for ev in events:
        kind = ev.get("kind")
        if kind == "incident":
            sev = ev.get("severity")
            counts[sev if sev in counts else "moderate"] += 1
        elif kind in _EVENT_SEVERITY:
            counts[_EVENT_SEVERITY[kind]] += 1
        elif kind == "patch_review" and ev.get("verdict") == "revise":
            counts["minor"] += 1
    return counts


def safety_multiplier(incidents: dict[str, int]) -> float:
    """S = 0 on any catastrophic event, else Π severity_decay^count."""
    if incidents.get("catastrophic", 0) > 0:
        return 0.0
    s = 1.0
    for sev, decay in SEVERITY_DECAY.items():
        s *= decay ** incidents.get(sev, 0)
    return s


# -- composites -------------------------------------------------------------------

def catq(q: float | None, s: float, c: float) -> float | None:
    """The headline composite CATQ = q·s/c (quality × safety over cost index).
    None when quality `q` is unknown."""
    return None if q is None else q * s / max(c, 1e-9)


def tus(q: float | None, s: float, c: float,
        lambda_c: float = 0.3, lambda_r: float = 0.7) -> float | None:
    """Task Utility Score: quality minus additive cost and risk penalties
    (weighted by `lambda_c`/`lambda_r`) — a bounded alternative to CATQ's ratio.
    None when quality `q` is unknown."""
    return None if q is None else q - lambda_c * (1.0 - 1.0 / max(c, 1e-9)) \
        - lambda_r * (1.0 - s)


# -- run collector -----------------------------------------------------------------

def _read_json(path: Path) -> dict:
    """Load a JSON object from `path`, returning `{}` on any read/parse error —
    metrics collection tolerates missing or partial run files."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _durable_rebase_signals(run_dir: Path, substate: dict) -> dict[str, Any]:
    """Counts and timings recoverable from the rebase WAL/checkpoints."""
    pushed = 0
    build_ids: set[str] = set()
    for path in (run_dir / "push_wal").glob("*.json") \
            if (run_dir / "push_wal").is_dir() else ():
        if _read_json(path).get("state") == "pushed":
            pushed += 1
    for path in (run_dir / "ci_ops").glob("*.json") \
            if (run_dir / "ci_ops").is_dir() else ():
        record = _read_json(path)
        build_id = str(record.get("build_id") or "")
        if build_id and record.get("state") in ("created", "cancelled"):
            build_ids.add(build_id)

    durations: list[float] = []
    measured_ids: set[str] = set()
    for round_ in ((substate.get("ci") or {}).get("rounds") or []):
        build_id = str(round_.get("build_id") or "")
        if build_id:
            build_ids.add(build_id)
        duration = round_.get("monitor_duration_sec")
        if isinstance(duration, (int, float)) and duration >= 0:
            durations.append(float(duration))
            if build_id:
                measured_ids.add(build_id)
    ci_partial = bool(build_ids - measured_ids)
    ci_minutes = round(sum(durations) / 60.0, 4) if durations else (
        None if build_ids else 0.0)
    return {
        "pushes": pushed,
        "ci_builds": len(build_ids),
        "ci_minutes": ci_minutes,
        "ci_minutes_partial": ci_partial,
    }


def _trace_events(run_dir: Path) -> list[dict]:
    """Read `run_dir/run_trace.jsonl` into a list of event dicts, skipping blank
    and malformed lines. Empty when the file is absent."""
    path = run_dir / "run_trace.jsonl"
    events: list[dict] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


# cache-token billing factors relative to the input price (W7): Anthropic
# documents cache reads at 0.1x and cache writes at 1.25x the input rate;
# configurable for other providers via settings.cache_read_price_factor
CACHE_READ_FACTOR = 0.1
CACHE_CREATE_FACTOR = 1.25


def _cost_from_model_calls(records, settings=None):
    """One usage/pricing projection for canonical facts and legacy span adapters.

    Monetary values are reported costs or priced subtotals, never fabricated
    prices for unknown models. Missing token facts remain explicit partials.
    """
    from .budgeting import usage_cost, usage_observation, normalized_cost, valid_token_counts
    read_f = float(getattr(settings, "cache_read_price_factor", 0) or 0) or CACHE_READ_FACTOR
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
              "cache_creation_tokens": 0, "usd": 0.0, "llm_calls": 0,
              "unpriced_calls": 0, "not_sent_calls": 0, "usage_partial": False,
              "usd_is_estimate": False}
    actual = 0.0
    actual_known = True
    cache_inclusion = set()
    by_role = {}
    for rec in records:
        result = rec.get("result") or {}
        if result.get("native_call_sent") is False:
            totals["not_sent_calls"] += 1
            continue
        identity = rec.get("model") or {}
        model = str(identity.get("served_model") or identity.get("model") or "")
        usage = usage_observation(rec.get("usage") or {})
        cache_inclusion.add(usage.get("input_tokens_include_cache"))
        if "cost_usd" not in usage and "cost_usd" in result:
            usage["cost_usd"] = normalized_cost(result["cost_usd"])
        tin, tout = usage.get("input_tokens") or 0, usage.get("output_tokens") or 0
        cread = usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0
        ccreate = usage.get("cache_creation_input_tokens") or usage.get("cache_creation_tokens") or 0
        totals["usage_partial"] |= (not valid_token_counts(usage.get("input_tokens"), usage.get("output_tokens"))
                                    or result.get("native_outcome_pending") is True
                                    or any(v is None for k, v in usage.items() if k.endswith("tokens")))
        p_in, p_out = model_price(model, settings)
        usd = usage_cost(usage, p_in, p_out, cache_read_factor=read_f, cache_create_factor=CACHE_CREATE_FACTOR) \
            if "cost_usd" in usage or model_price_known(model, settings) else None
        paid = normalized_cost(usage.get("cost_usd"))
        actual_known &= paid is not None
        actual += paid if paid is not None else 0.0
        totals["input_tokens"] += tin
        totals["output_tokens"] += tout
        totals["cache_read_tokens"] += cread
        totals["cache_creation_tokens"] += ccreate
        totals["llm_calls"] += 1
        if usd is None:
            totals["unpriced_calls"] += 1
            continue
        totals["usd_is_estimate"] |= paid is None
        totals["usd"] += usd
        role = str(identity.get("role") or "agent")
        r = by_role.setdefault(role, {"usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0})
        r["usd"] += usd
        r["input_tokens"] += tin
        r["output_tokens"] += tout
        r["calls"] += 1
    totals["usd"] = round(totals["usd"], 6)
    totals["actual_usd"] = round(actual, 6) if actual_known else None
    totals["input_tokens_include_cache"] = next(iter(cache_inclusion)) if len(cache_inclusion) == 1 else None
    totals["cost_partial"] = totals["unpriced_calls"] > 0 or totals["usage_partial"]
    totals["by_role"] = {k: {**v, "usd": round(v["usd"], 6)} for k, v in by_role.items()}
    return totals


def cost_from_spans(trace_path: "Path", settings: "Settings | None" = None):
    """Compatibility reader for runs with spans but no model_call records."""
    try:
        lines = Path(trace_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    records = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("name") != "llm":
            continue
        a = rec.get("attr") or {}
        usage = a["usage"] if "usage" in a else {
            "input_tokens": a.get("prompt_tokens"), "output_tokens": a.get("completion_tokens"),
            "cache_read_tokens": a.get("cache_read_tokens", 0),
            "cache_creation_tokens": a.get("cache_creation_tokens", 0)}
        records.append({"model": a, "usage": usage})
    return {**_cost_from_model_calls(records, settings), "source": "spans"} if records else None


def cost_from_records(run_dir: "Path", settings: "Settings | None" = None, *, events=None):
    """Read canonical per-request records, including retained session stores.

    Read the authoritative journals, not a capped or possibly stale index.
    This is a pure projection: no call, settlement, receipt rewrite or repair.
    """
    from .execution_artifacts import model_call_trace_roots
    from .trace_store import TraceStore
    from .model_usage import model_usage_fact
    records = {}
    conflicts = set()
    errors = []
    run_dir = Path(run_dir)

    def add(record):
        if (record.get("context") or {}).get("run_id") != run_dir.name:
            return
        fact = model_usage_fact(record)
        result = fact['result']
        key = ("native", result["native_attempt_id"]) if result.get("native_attempt_id") else ("record", fact["id"])
        comparable = {k: fact[k] for k in ("model", "usage", "result")}
        if key in records and records[key] != comparable:
            conflicts.add(key)
        else:
            records[key] = comparable

    events = list(_trace_events(run_dir) if events is None else events)
    starts = set()
    ends = {}
    emitted = {}
    for event in events:
        if event.get('kind') == 'model_call_usage':
            try:
                add(event['call'])
                capture_id = event.get('capture_id')
                emitted[capture_id] = emitted.get(capture_id, 0) + 1
            except (ValueError, KeyError, TypeError):
                errors.append({'error_type': 'invalid_usage_projection'})
        elif event.get('kind') == 'model_usage_capture':
            capture_id = event.get('capture_id')
            if event.get('phase') == 'start':
                starts.add(capture_id)
            elif event.get('phase') == 'end':
                ends[capture_id] = event
    capture_incomplete = any(
        key not in starts or key not in ends or ends[key].get('errors') or
        ends[key].get('saved') != ends[key].get('observed') or
        emitted.get(key, 0) != ends[key].get('saved')
        for key in starts | emitted.keys() | ends.keys())
    for root in model_call_trace_roots(run_dir, getattr(settings, "trace_store_root", None)):
        try:
            for record, _ in TraceStore(root).iter_records(kind="model_call"):
                add(record)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({"root": str(root), "error_type": type(exc).__name__})
    if not records and not errors and not capture_incomplete:
        return None  # a legacy run or a client without capture: explicit fallback
    for key in conflicts:
        records[key] = {"model": {}, "usage": {}, "result": {"cost_usd": None}}
    totals = _cost_from_model_calls(records.values(), settings)
    if conflicts or errors or capture_incomplete:
        totals.update(cost_partial=True, usage_partial=True, actual_usd=None)
    return {**totals, "source": "model_calls", "conflicting_calls": len(conflicts),
            "record_read_errors": errors, "usage_capture_incomplete": bool(capture_incomplete)}


def _phase_timings(events: Iterable[dict]) -> dict:
    """Labeled phase timings (W7): per-step durations from `step_result`
    events' `dur_s` (fetch/gate/review/report ARE the run's phases) plus
    `time_to_first_result_s` — the first agent-step completion relative to
    run start, the closest thing a batch run has to time-to-first-useful-
    result."""
    events = list(events)
    ts = [float(ev["ts"]) for ev in events
          if isinstance(ev.get("ts"), (int, float))]
    start = min(ts) if ts else 0.0
    steps: dict[str, float] = {}
    ttfr = None
    for ev in events:
        if ev.get("kind") != "step_result":
            continue
        if isinstance(ev.get("dur_s"), (int, float)):
            steps[str(ev.get("step"))] = round(float(ev["dur_s"]), 2)
        if ttfr is None and str(ev.get("spec", "")).startswith("agent.") \
                and isinstance(ev.get("ts"), (int, float)):
            ttfr = round(float(ev["ts"]) - start, 2)
    return {"steps": steps, "time_to_first_result_s": ttfr}


def usage_from_events(events: Iterable[dict]) -> tuple[int, int, int]:
    """(input_tokens, output_tokens, tool_calls) summed over usage-bearing
    events — agent_output, agent_ensemble (reducer), llm_usage."""
    tokens_in = tokens_out = tool_calls = 0
    for ev in events:
        if ev.get("kind") in ("agent_output", "agent_ensemble", "llm_usage"):
            tokens_in += int(ev.get("input_tokens") or 0)
            tokens_out += int(ev.get("output_tokens") or 0)
            tool_calls += int(ev.get("tool_calls") or 0)
    return tokens_in, tokens_out, tool_calls


def estimate_usd(tokens_in: int, tokens_out: int, ci_minutes: float,
                 settings: "Settings") -> float:
    """Estimated run cost in USD: token cost at the agent model's price plus CI
    minutes at `settings.ci_rate_usd_per_min`."""
    price_in, price_out = model_price(settings.agent_model, settings)
    usd = tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out
    return usd + ci_minutes * settings.ci_rate_usd_per_min


def _auto_components(kind: str, events: list[dict], progress: dict,
                     status: str, *, substate: dict | None = None,
                     durable: dict | None = None) -> dict[str, float | None]:
    """The honestly auto-derivable slice of each task's components. Everything
    that needs juries, GT, CI re-runs, or maintainer feedback stays None."""
    completed = progress.get("completed", {}) if isinstance(progress, dict) else {}
    components: dict[str, float | None] = {
        k: None for k in TASK_WEIGHTS.get(kind, {})}
    if kind in ("pr_rebase", "repo_rebase"):
        substate = substate or {}
        durable = durable or {}
        if ((status == "done" and (substate.get("phase") == "done"
                                   or "finalize" in completed))
                or "push" in completed
                or ("rebase" in completed and status == "done")):
            components["completed"] = 1.0
        elif status in ("failed", "blocked"):
            components["completed"] = 0.0
        # push_safe: guard violations surface as incidents; a completed push
        # with no push-related incident was a with-lease PR-head push by
        # construction (single choke point)
        if "push" in completed or durable.get("pushes", 0) > 0:
            components["push_safe"] = 1.0
        if "verify" in completed and "gate" in completed:
            components["tests"] = 1.0
        modules = substate.get("modules") or {}
        active_modules = [m or {} for m in modules.values()
                          if not (m or {}).get("skip")]
        if active_modules:
            components["conflict"] = 1.0 if all(
                m.get("status") == "done" for m in active_modules) else 0.0
        tests = substate.get("tests") or {}
        pipeline = tests.get("pipeline") or {}
        precommit = (tests.get("precommit") or {}).get("result")
        ci_result = (substate.get("ci") or {}).get("result")
        test_failed = (int(pipeline.get("failed") or 0) > 0
                       or bool(tests.get("infra_failures"))
                       or precommit == "failed"
                       or (ci_result and ci_result != "passed"))
        if test_failed:
            components["tests"] = 0.0
        elif ci_result == "passed" or (
                int(pipeline.get("passed") or 0) > 0
                and int(pipeline.get("skipped") or 0) == 0):
            components["tests"] = 1.0
        if substate and components.get("completed") is not None:
            components["purity"] = 0.0 if any(
                ev.get("kind") == "out_of_scope_edit" for ev in events) else 1.0
    elif kind == "pr_debug":
        # repro: fraction of debug groups whose output recorded tests_run
        # (foreach fan-out keys outputs by index; a single group is the
        # contract dict itself)
        debug = completed.get("debug")
        if isinstance(debug, dict):
            outs = debug.get("outputs") or {}
            per_item = [o for o in outs.values() if isinstance(o, dict)] \
                or ([outs] if "tests_run" in outs else [])
            if per_item:
                ran = [o for o in per_item if o.get("tests_run")]
                components["repro"] = len(ran) / len(per_item)
    return components


def _abstained(kind: str, events: list[dict], status: str) -> bool:
    """A justified safe abstain: the run escalated instead of acting."""
    if kind not in ABSTAIN_SCORES or status != "blocked":
        return False
    return any(ev.get("kind") == "escalation" for ev in events)


def collect_run_metrics(run_dir: Path, settings: "Settings",
                        status: str = "", *,
                        extra_components: dict[str, float] | None = None,
                        ) -> dict[str, Any]:
    """Compute and persist metrics.json for one run directory.

    `extra_components` lets the eval pipeline / feedback collector merge in
    judged or GT-derived components (recall_w, grounded, conflict, ...) and
    re-emit; auto components never overwrite an explicitly passed value.
    """
    run_dir = Path(run_dir)
    events = _trace_events(run_dir)
    task = _read_json(run_dir / "task.json")
    progress = _read_json(run_dir / "progress.json")
    substate = _read_json(run_dir / "substate.json")
    spec = task.get("spec") or task or {}
    kind = str(spec.get("kind") or "")

    ts = [float(ev["ts"]) for ev in events if isinstance(ev.get("ts"), (int, float))]
    minutes = (max(ts) - min(ts)) / 60.0 if len(ts) >= 2 else 0.0
    durable = _durable_rebase_signals(run_dir, substate)
    event_ci_minutes = [float(ev.get("minutes") or 0.0) for ev in events
                        if ev.get("kind") == "ci_build"
                        and ev.get("minutes") is not None]
    ci_minutes = (sum(event_ci_minutes) if event_ci_minutes
                  else durable["ci_minutes"])
    ci_minutes_source = ("trace" if event_ci_minutes else
                         "monitor_wall" if ci_minutes is not None
                         and durable["ci_builds"] else "none")
    ci_time_partial = bool(durable["ci_minutes_partial"])
    # Per-request records are authoritative. Old spans/events only adapt runs
    # without those records; never add overlapping views of the same calls.
    span_cost = cost_from_records(run_dir, settings, events=events)
    if span_cost is None:
        span_cost = cost_from_spans(run_dir / "trace.jsonl", settings)
    legacy_in, legacy_out, declared_tool_calls = usage_from_events(events)
    if span_cost is not None and span_cost["source"] == "model_calls" and (
            legacy_in > span_cost["input_tokens"] or legacy_out > span_cost["output_tokens"]):
        # A capture gap or duplicate legacy aggregate cannot be resolved by
        # guessing. Retain the canonical subtotal and make uncertainty visible.
        span_cost.update(cost_partial=True, usage_partial=True, actual_usd=None,
                         legacy_usage_exceeds_records=True)
    explicit_tool_calls = sum(1 for ev in events
                              if ev.get("kind") == "tool_call")
    tool_calls = max(declared_tool_calls, explicit_tool_calls)
    if span_cost is not None:
        tokens_in = span_cost["input_tokens"]
        tokens_out = span_cost["output_tokens"]
        usd = (span_cost["usd"]
               + (ci_minutes or 0.0) * settings.ci_rate_usd_per_min)
        cost_source = span_cost["source"]
    else:
        tokens_in, tokens_out, _ = usage_from_events(events)
        usd = estimate_usd(tokens_in, tokens_out, ci_minutes or 0.0, settings)
        cost_source = "events"

    incidents = derive_incidents(events)
    s = safety_multiplier(incidents)
    usd_ref = settings.cost_ref_usd.get(kind, settings.cost_ref_usd.get("default", 1.0))
    min_ref = settings.cost_ref_min.get(kind, settings.cost_ref_min.get("default", 10.0))
    llm_requests = sum(1 for ev in events
                       if ev.get("kind") == "rebase_llm_request")
    usage_missing = span_cost is None and llm_requests > 0 \
        and tokens_in == 0 and tokens_out == 0
    cost_partial = bool((span_cost or {}).get("cost_partial")) \
        or ci_time_partial or usage_missing
    # a partial usd is a lower bound — a cost index over it would flatter the
    # run, so it is omitted rather than computed from a fabrication
    c = None if cost_partial else cost_index(usd, minutes, usd_ref, min_ref)

    components = _auto_components(kind, events, progress, status,
                                  substate=substate, durable=durable)
    for key, value in (extra_components or {}).items():
        components[key] = value
    abstained = _abstained(kind, events, status)
    if abstained:
        q, coverage = ABSTAIN_SCORES[kind], 1.0
    else:
        q, coverage = quality_score(kind, components)

    metrics: dict[str, Any] = {
        "schema": 1,
        "run": run_dir.name,
        "task_kind": kind,
        "status": status,
        "quality": {
            "q": q,
            "weight_coverage": round(coverage, 3),
            "partial": (not abstained) and coverage < 0.999,
            "abstained": abstained,
            "components": components,
        },
        "cost": {
            "usd": round(usd, 4),
            "minutes": round(minutes, 2),
            "ci_minutes": (round(ci_minutes, 2)
                           if ci_minutes is not None else None),
            "ci_minutes_partial": ci_time_partial,
            "ci_minutes_source": ci_minutes_source,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "tool_calls": tool_calls,
            "usd_ref": usd_ref,
            "min_ref": min_ref,
            "cost_index": round(c, 4) if c is not None else None,
            "cost_partial": cost_partial,
            "usage_partial": (span_cost or {}).get("usage_partial", usage_missing),
            "actual_model_usd": (span_cost or {}).get("actual_usd"),
            "actual_usd": ((span_cost or {}).get("actual_usd")
                           if not ci_time_partial and not (ci_minutes or 0) else None),
            "usd_is_estimate": (span_cost or {}).get("usd_is_estimate", True) or bool(ci_minutes),
            "legacy_usage_exceeds_records": (span_cost or {}).get("legacy_usage_exceeds_records", False),
            "record_read_error_count": len((span_cost or {}).get("record_read_errors", [])),
            "conflicting_calls": (span_cost or {}).get("conflicting_calls", 0),
            "usage_capture_incomplete": (span_cost or {}).get("usage_capture_incomplete", False),
            "input_tokens_include_cache": (span_cost or {}).get("input_tokens_include_cache"),
            "unpriced_calls": (span_cost or {}).get("unpriced_calls", 0),
            "source": cost_source,
            "llm_calls": ((span_cost or {}).get("llm_calls")
                          if span_cost is not None else llm_requests or None),
            "by_role": (span_cost or {}).get("by_role"),
            "cache_read_tokens": (span_cost or {}).get("cache_read_tokens"),
            "cache_creation_tokens": (span_cost or {}).get("cache_creation_tokens"),
        },
        "timings": _phase_timings(events),
        "risk": {
            "incidents": incidents,
            "safety_multiplier": round(s, 4),
        },
        "catq": None if (q is None or c is None) else round(catq(q, s, c), 4),
        "tus": None if (q is None or c is None) else round(tus(q, s, c), 4),
        "signals": {
            "escalations": sum(1 for ev in events if ev.get("kind") == "escalation"),
            "pushes": (durable["pushes"] or sum(
                1 for ev in events if ev.get("kind") == "push_requested")),
            "ci_builds": durable["ci_builds"],
            "posted": sum(1 for ev in events if ev.get("kind") == "posted_artifact"),
            "steps_completed": len((progress or {}).get("completed", {})),
        },
    }
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "metrics.json").write_text(
            json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return metrics


def format_metrics_line(m: dict, run_dir: Path) -> str:
    """One-line run-cost/quality summary from a collected metrics dict."""
    cost, risk, catq = m["cost"], m["risk"], m["catq"]
    catq_str = (f" CATQ={catq:.3f}" + ("*" if m["quality"]["partial"] else "")
                if catq is not None else "")
    partial = "+? (unpriced calls)" if cost.get("cost_partial") else ""
    return (f"  metrics: usd≈{cost['usd']:.2f}{partial} {cost['minutes']:.1f}min "
            f"S={risk['safety_multiplier']:.2f}{catq_str}"
            f"  ({run_dir / 'metrics.json'})")
