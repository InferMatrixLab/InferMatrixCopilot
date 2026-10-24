"""Meta-improvement engine steps.

`improve.stage_items` runs BEFORE shadow isolation, in the engine's own
scope: it stages every pre-registered item (`repo#pr@head`) into a snapshot
plus an independent shadow clone (design §8.2) and publishes them so a
`pr_context_source=snapshot` run consumes them without touching the network.
"""

from __future__ import annotations

from pathlib import Path

from ..step import FailureKind, StepContext, StepResult
from ._common import step


@step("improve.stage_items", "deterministic", "read",
      "Stage pre-registered PR items into snapshots + shadow clones (pre-isolation).")
async def _stage_items(ctx: StepContext) -> StepResult:
    """Items come from `params.items` (a list or a comma-separated string) or
    `state['improve_items']`. Every item is staged or the step fails: an
    experiment whose inputs are partly approximated is invalid by design.

    Publishes `pr_snapshots` ({item: snapshot}) and, for a single item,
    `pr_snapshot` (what the snapshot-mode PR steps read)."""
    from ...improve.staging import StagingError, stage_item

    raw = ctx.params.get("items") or ctx.state.get("improve_items") or []
    if isinstance(raw, str):
        raw = [x for x in raw.split(",") if x.strip()]
    items = [str(x).strip() for x in raw if str(x).strip()]
    if not items:
        return StepResult(False, FailureKind.BLOCKED, "no items to stage (params.items / state.improve_items)")
    shadow_root = Path(ctx.params.get("shadow_root") or (ctx.run_dir / "shadow"))
    snapshots: dict[str, dict] = {}
    for item in items:
        try:
            snapshots[item] = stage_item(ctx, item, shadow_root=shadow_root)
        except StagingError as exc:
            ctx.trace.record("item_staging_failed", item=item, reason=str(exc)[:300])
            return StepResult(False, FailureKind.BLOCKED, f"staging failed for {item}: {exc}")
    updates: dict = {"pr_snapshots": snapshots}
    if len(snapshots) == 1:
        updates["pr_snapshot"] = next(iter(snapshots.values()))
    ctx.state.update(updates)
    return StepResult(True, summary=f"staged {len(snapshots)} item(s) under {shadow_root}",
                      outputs={"staged": sorted(snapshots), "state_updates": updates})


@step("improve.mode", "deterministic", "read",
      "Publish the run's mode: a weekly cycle, or one meta-benchmark case (self-experiment).")
async def _mode(ctx: StepContext) -> StepResult:
    """`--task-param meta_case=<case>` turns the playbook into the engine's
    own forensics unit over ONE frozen case: `improve_meta` gates every
    cycle step off and `improve_item` (`meta:<case>`) keys the unit."""
    from ._common import task_spec

    params = (task_spec(ctx).get("params") or {}) if isinstance(task_spec(ctx), dict) else {}
    case = str(ctx.params.get("meta_case") or params.get("meta_case") or "").strip()
    if case and ("/" in case or case.startswith(".")):
        return StepResult(False, FailureKind.BLOCKED, f"meta_case must be a case name, got {case!r}")
    updates = {"improve_meta": bool(case), "meta_case": case, "improve_item": f"meta:{case}" if case else "cycle"}
    updates.update(improve_coordinated=not bool(case) and bool(ctx.settings.improve_evolve_enabled),
                   improve_legacy=not bool(case) and not bool(ctx.settings.improve_evolve_enabled),
                   improve_forensics_run=bool(case) or not bool(ctx.settings.improve_evolve_enabled))
    ctx.state.update(updates)
    return StepResult(True, summary=f"mode: {'meta case ' + case if case else 'weekly cycle'}",
                      outputs={"state_updates": updates})


def _ledger_dir(ctx: StepContext) -> Path:
    from ...improve.cycle import ledger_dir_for

    override = str(ctx.params.get("ledger_dir") or "")
    return Path(override).expanduser() if override else ledger_dir_for(ctx.settings)


@step("improve.coordinate", "deterministic", "read", "Run the shared resumable improvement/evolution coordinator.")
async def _coordinate(ctx: StepContext) -> StepResult:
    from ...improve.coordinator import run_async
    store = _trace_store(ctx)
    if store is None: return StepResult(False, FailureKind.BLOCKED, "no trace store")
    report = await run_async(ctx.settings, store, llm=ctx.llm)
    updates = {"improve_cycle": report.get("stages", {}).get("lint", {}), "improve_evolution": report.get("stages", {}).get("evolve", {})}
    ctx.state.update(updates)
    return StepResult(True, summary=f"evolution coordinator: {report.get('state')}", outputs={"report": report, "state_updates": updates})


@step("improve.evolve_publish", "deterministic", "push", "Publish verified evolution candidates to the maintainer outbox.")
async def _evolve_publish(ctx: StepContext) -> StepResult:
    from ._common import task_spec
    from ...improve.evolution_publish import publish
    c = ctx.state.get("improve_evolution", {})
    spec = task_spec(ctx)
    if not spec.get("post") or not ctx.settings.allow_post or not ctx.settings.allow_push or c.get("state") != "pr-ready":
        return StepResult(True, summary="evolution publication preview: post/push gates closed or no passing candidate")
    report = publish(ctx.settings, _trace_store(ctx), c)
    return StepResult(True, summary=str(report), outputs=report)


def _trace_store(ctx: StepContext):
    from ...trace_store import TraceStore, current_store

    store = current_store()
    if store is not None:
        return store
    root = str(getattr(ctx.settings, "trace_store_root", "") or "")
    if not root:
        return None
    return TraceStore(Path(root).expanduser())


@step("improve.preflight", "deterministic", "read",
      "Kill switch + per-workflow holds; refuses the cycle loudly.")
async def _preflight(ctx: StepContext) -> StepResult:
    from ...improve.cycle import CycleRefused, preflight
    from ...improve.ledger import Ledger

    store = _trace_store(ctx)
    if store is None:
        return StepResult(False, FailureKind.BLOCKED, "no trace store (set TRACE_STORE_ROOT)")
    try:
        gate = preflight(ctx.settings, Ledger(_ledger_dir(ctx), store))
    except CycleRefused as exc:
        return StepResult(False, FailureKind.BLOCKED, str(exc))
    updates = {"improve_holds": gate["holds"], "improve_ledger_dir": str(_ledger_dir(ctx))}
    ctx.state.update(updates)
    return StepResult(True, summary=f"preflight ok; holds: {gate['holds'] or 'none'}",
                      outputs={"state_updates": updates})


@step("improve.lint", "deterministic", "read",
      "Tier 1 lints + baselines + ledger update over the cycle window (dry-run).")
async def _lint(ctx: StepContext) -> StepResult:
    """Runs the whole P1 cycle body (lints, baselines, Tier 1 proposals in
    the ledger, stale sweep, report). Window from params `since`/`until`
    (epoch seconds) or the cursor. Publishes `improve_report_path`."""
    from ...improve.cycle import CycleRefused, run_cycle

    store = _trace_store(ctx)
    if store is None:
        return StepResult(False, FailureKind.BLOCKED, "no trace store (set TRACE_STORE_ROOT)")
    since = ctx.params.get("since")
    until = ctx.params.get("until")
    try:
        report = run_cycle(store, ctx.settings, _ledger_dir(ctx),
                           since=float(since) if since else None, until=float(until) if until else None,
                           dry_run=True, force=bool(ctx.params.get("force")))
    except CycleRefused as exc:
        return StepResult(False, FailureKind.BLOCKED, str(exc))
    updates = {"improve_cycle": {k: report[k] for k in ("at", "since", "until", "units", "proposals_opened")},
               "improve_report_dir": str(_ledger_dir(ctx) / "reports")}
    ctx.state.update(updates)
    opened = len(report["proposals_opened"])
    return StepResult(True, summary=f"cycle over {report['units']} units in {len(report['workflows'])} workflow(s); "
                                    f"{opened} Tier 1 proposal(s) opened (dry-run)",
                      outputs={"report": report, "state_updates": updates})


@step("improve.ledger", "deterministic", "read",
      "Summarize the per-workflow ledgers for the run report.")
async def _ledger_summary(ctx: StepContext) -> StepResult:
    from ...improve.ledger import Ledger

    ledger = Ledger(_ledger_dir(ctx))
    lines = []
    for workflow in ledger.workflows():
        wl = ledger.load(workflow)
        open_ = [p for p in wl.proposals if p.state not in ("landed", "closed")]
        last = wl.cycles[-1] if wl.cycles else {}
        lines.append(f"{workflow}: tier {wl.tier}, {len(wl.cycles)} cycle(s), last units {last.get('units', 0)}, "
                     f"{len(open_)} live proposal(s)" + (f", HOLD: {wl.hold}" if wl.hold else ""))
    summary = "\n".join(lines) or "no workflows in the ledger yet"
    return StepResult(True, summary=summary[:400], outputs={"ledger_summary": summary})


def _forensics_agents(ctx: StepContext) -> dict:
    """Two investigator families from the configured tiers (eco and, when
    configured, performance); one family only when that is all there is —
    the report then says `single_family`."""
    from ...agent_loop import run_agent

    if ctx.llm is None or not getattr(ctx.llm, "available", False):
        return {}
    agents = {}
    for mode in ("eco", "performance"):
        try:
            target = ctx.settings.tier_target(mode)
        except Exception:  # noqa: BLE001 - an unconfigured tier is simply absent
            continue
        family = f"{target.provider_id or target.source}:{target.model}"
        if family in agents:
            continue
        llm = ctx.llm.for_target(target) if hasattr(ctx.llm, "for_target") else ctx.llm

        def make(llm_=llm, model_=target.model):
            def agent(system, prompt, scope, extra_tools, max_iters):
                return run_agent(llm_, system=system, prompt=prompt, scope=scope, trace=ctx.trace,
                                 model=model_, max_iters=max_iters, extra_tools=extra_tools).text
            return agent
        agents[family] = make()
    return agents


def _outbox(ctx: StepContext):
    from ...improve.publish import ProposalOutbox

    root = str(ctx.params.get("outbox_dir") or getattr(ctx.settings, "improve_outbox_dir", "") or "")
    return ProposalOutbox(Path(root).expanduser()) if root else None


@step("improve.sync", "deterministic", "read",
      "Read the routine's acks and observations back into the ledger (issue urls, touches, holds, landings).")
async def _sync(ctx: StepContext) -> StepResult:
    """Local bookkeeping only: the outbox directory is the engine's own.
    Publishes `improve_sync`."""
    import time

    from ...improve.ledger import Ledger
    from ...improve.publish import sync

    outbox = _outbox(ctx)
    if outbox is None:
        return StepResult(True, summary="no outbox configured (improve_outbox_dir): nothing to sync",
                          outputs={"state_updates": {"improve_sync": {}}})
    store = _trace_store(ctx)
    report = sync(Ledger(_ledger_dir(ctx), store), outbox, store, now=time.time())
    updates = {"improve_sync": report}
    ctx.state.update(updates)
    return StepResult(True, summary=(f"sync: {len(report['acked'])} ack(s), {len(report['touched'])} touched, "
                                     f"{len(report['landed'])} landed, holds {report['holds'] or 'none'}"),
                      outputs={"sync": report, "state_updates": updates})


@step("improve.publish", "script", "push",
      "Hand publishable proposals to the maintainer routine's outbox (explicit post flag + ALLOW_POST).")
async def _publish(ctx: StepContext) -> StepResult:
    """The cycle's only outward write (design §11.1 rule 1), double-gated
    like `pr.post_review`: without the task's `post` intent nothing is
    planned as a write; with it but ALLOW_POST=0 the plan is printed as a
    dry run; with both, action files are written and every proposal that
    fails the linter is refused and recorded. Publishes `improve_publish`."""
    import time

    from ...improve.ledger import Ledger
    from ...improve.publish import PublishError, plan, publish
    from ._common import task_spec

    spec = task_spec(ctx)
    store = _trace_store(ctx)
    if store is None:
        return StepResult(False, FailureKind.BLOCKED, "no trace store (set TRACE_STORE_ROOT)")
    ledger_dir = _ledger_dir(ctx)
    ledger = Ledger(ledger_dir, store)
    repo = str(ctx.params.get("repo") or getattr(ctx.settings, "improve_proposal_repo", "") or "")
    now = time.time()
    if not spec.get("post"):
        preview = plan(ledger, store, repo=repo or "?", now=now, ledger_ref=str(ledger_dir))
        updates = {"improve_publish": {"mode": "not-requested", "would": len(preview["actions"]),
                                       "refused": preview["refused"], "held": preview["held"]}}
        ctx.state.update(updates)
        return StepResult(True, summary=f"not publishing (post flag not set); {len(preview['actions'])} action(s) "
                                        f"would be written, {len(preview['refused'])} refused by the linter",
                          outputs={"plan": preview, "state_updates": updates})
    outbox = _outbox(ctx)
    if outbox is None:
        return StepResult(False, FailureKind.BLOCKED, "post requested but no outbox configured (improve_outbox_dir)")
    dry_run = not bool(ctx.settings.allow_post)
    try:
        report = publish(ledger, store, outbox, repo=repo, now=now, ledger_ref=str(ledger_dir), dry_run=dry_run)
    except PublishError as exc:
        return StepResult(False, FailureKind.BLOCKED, str(exc))
    updates = {"improve_publish": {"mode": "dry-run" if dry_run else "written", "actions": len(report["actions"]),
                                   "written": report["written"], "refused": report["refused"], "held": report["held"],
                                   "waiting": report["waiting"]}}
    ctx.state.update(updates)
    if dry_run:
        return StepResult(True, summary=f"dry-run (ALLOW_POST=0): would write {len(report['actions'])} outbox action(s) "
                                        f"for {repo}; {len(report['refused'])} refused by the linter",
                          outputs={"dry_run": True, "plan": report, "state_updates": updates})
    for path in report["written"]:
        ctx.trace.record("outbox_written", what="proposal action", path=path, repo=repo)
    return StepResult(True, summary=f"wrote {len(report['written'])} outbox action(s) for {repo}; "
                                    f"{len(report['refused'])} refused by the linter, {len(report['held'])} workflow(s) on hold",
                      outputs={"plan": report, "state_updates": updates})


async def _meta_forensics(ctx: StepContext, store, case_name: str) -> StepResult:
    """The engine's own unit: attribute one frozen meta case with the
    configured investigator families and record the comparison with the
    human labels (`meta_eval`). The case is read from `improve_meta_dir`
    (a staged copy under IMPROVE_META_DIR in a shadow child)."""
    from ...improve.budget import BudgetRefused, governed
    from ...improve.cycle import governor_for
    from ...improve.meta import load_cases, run_case
    from ...trace_store import bind_store

    meta_dir = Path(str(ctx.params.get("meta_dir") or getattr(ctx.settings, "improve_meta_dir", "") or "eval/dataset/meta")).expanduser()
    cases = {c.name: c for c in load_cases(meta_dir)}
    case = cases.get(case_name)
    if case is None:
        return StepResult(False, FailureKind.BLOCKED, f"no meta case {case_name!r} under {meta_dir}")
    agents = _forensics_agents(ctx)
    if not agents:
        return StepResult(False, FailureKind.BLOCKED, "no LLM configured for the forensics agents")
    governor = governor_for(ctx.settings, _ledger_dir(ctx))
    ctx.governor = governor
    try:
        with bind_store(store), governed(governor):
            result = run_case(store, case, agents, meta_dir=meta_dir, max_cells=int(ctx.params.get("max_cells") or 40))
    except BudgetRefused as exc:
        return StepResult(False, FailureKind.BLOCKED, f"budget exhausted: {exc}")
    summary = {k: result.get(k) for k in ("case", "cells", "attributed", "agreement", "kappa", "lint_recall", "disputed")}
    updates = {"improve_meta_eval": summary}
    ctx.state.update(updates)
    return StepResult(True, summary=f"meta case {case_name}: {result['attributed']}/{result['cells']} cells attributed, "
                                    f"agreement {result['agreement']}, kappa {result['kappa']}, lint recall {result['lint_recall']}",
                      outputs={"meta_eval": result, "state_updates": updates})


@step("improve.forensics", "agent", "read",
      "Tier 2: coverage matrices + stage-of-loss attribution for workflows with an outcome adapter.")
async def _forensics(ctx: StepContext) -> StepResult:
    """For each Tier 2 workflow in the last cycle's window: import outcomes,
    build the gold × unit matrix, attribute every miss with two agent
    families, rank the punch list and open one Tier 2 proposal per stage
    with a loss. Params: `workflow` (restrict), `max_cells`. In meta mode
    (`improve_meta`) the step is the engine's own unit over one frozen case.
    Publishes `improve_forensics` (punch lists + measurement health)."""
    from ...improve.adapters import load_adapter
    from ...improve.enroll import declarations_for
    from ...improve.forensics import attribute, coverage_matrix, measurement_health, punch_list, unit_scores
    from ...improve.reader import collect_units
    from ...trace_store import bind_store, trace_context

    from ...improve.budget import BudgetRefused, governed
    from ...improve.cycle import governor_for, open_tier2_proposals
    from ...improve.ledger import Ledger

    store = _trace_store(ctx)
    if store is None:
        return StepResult(False, FailureKind.BLOCKED, "no trace store (set TRACE_STORE_ROOT)")
    if ctx.state.get("improve_meta"):
        return await _meta_forensics(ctx, store, str(ctx.state.get("meta_case") or ""))
    cycle = ctx.state.get("improve_cycle") or {}
    since, until = cycle.get("since"), cycle.get("until")
    if since is None or until is None:
        return StepResult(False, FailureKind.BLOCKED, "improve.forensics needs the cycle window (run improve.lint first)")
    only = str(ctx.params.get("workflow") or "")
    decls = declarations_for(ctx.settings)
    agents = _forensics_agents(ctx)
    results: dict = {}
    governor = governor_for(ctx.settings, _ledger_dir(ctx))
    ctx.governor = governor
    with bind_store(store), governed(governor), trace_context(playbook="workflow-improve", run_id=ctx.run_dir.name):
        units, _ = collect_units(store, float(since), float(until))
        for name, decl in decls.items():
            if not decl.tier2 or (only and name != only):
                continue
            wf_units = [u for u in units.values() if u.workflow == name and (not ctx.params.get("repo") or u.item.split("#", 1)[0] == ctx.params["repo"])]
            if len(wf_units) < decl.tier2_min_items and not ctx.params.get("force"):
                results[name] = {"skipped": f"{len(wf_units)} units < tier2_min_items {decl.tier2_min_items}"}
                continue
            adapter = load_adapter(decl.outcome_adapter, **_adapter_kwargs(ctx, decl))
            collected = _collect_outcomes(ctx, adapter, wf_units, store)
            outcomes: dict = {}
            cells, golds = coverage_matrix(adapter, wf_units, store, outcomes=outcomes)
            scored = unit_scores(adapter, wf_units, outcomes, golds)
            entry: dict = {"units": len(wf_units), "cells": len(cells), "collected": collected,
                           "outcomes": len(outcomes), "finding_labels": scored["findings"],
                           "scores": scored["units"],
                           "misses": sum(1 for c in cells if c.status == "miss"),
                           "unlabeled": sum(1 for c in cells if c.status == "unlabeled"),
                           "descriptive_only": bool(getattr(adapter, "descriptive_only", False)),
                           "families": sorted(agents), "single_family": len(agents) < 2}
            if cells and agents:
                try:
                    attributions = attribute(store, {u.unit_id: u for u in wf_units}, golds, cells, agents=agents,
                                             max_cells=int(ctx.params.get("max_cells") or 40))
                except BudgetRefused as exc:
                    entry["budget_exhausted"] = str(exc)[:200]
                    attributions = []
                entry["punch_list"] = punch_list(attributions, golds, {u.unit_id: u for u in wf_units})
                entry["measurement"] = measurement_health(attributions)
                # one Tier 2 proposal per stage with a loss (design §9.2),
                # carrying the suggested pre-registration; proxy-labelled
                # for a descriptive-only workflow
                entry["proposals"] = open_tier2_proposals(
                    Ledger(_ledger_dir(ctx), store), _ledger_dir(ctx), name, entry["punch_list"],
                    descriptive_only=entry["descriptive_only"], items=sorted(golds))
            elif cells and not agents:
                entry["skipped"] = "no LLM configured for the forensics agents"
            results[name] = entry
    ctx.state["improve_forensics"] = results
    done = [n for n, r in results.items() if "punch_list" in r]
    return StepResult(True, summary=f"forensics: {len(done)} workflow(s) attributed, "
                                    f"{sum(1 for r in results.values() if 'skipped' in r)} skipped",
                      outputs={"forensics": results, "state_updates": {"improve_forensics": results}})


def _adapter_kwargs(ctx: StepContext, decl) -> dict:
    """Construction arguments per adapter, from settings/params (never
    secrets): the eval adapter needs the GT and judgment dirs and the arm
    name; the bot adapter needs nothing."""
    if decl.outcome_adapter.endswith(":ReviewEvalAdapter"):
        return {"gt_dir": str(ctx.params.get("gt_dir") or getattr(ctx.settings, "improve_gt_dir", "") or "eval/dataset/gt"),
                "judgments_dir": ctx.params.get("judgments_dir") or getattr(ctx.settings, "improve_judgments_dir", "") or None,
                "arm": str(ctx.params.get("arm") or getattr(ctx.settings, "improve_eval_arm", "") or "")}
    if decl.outcome_adapter.endswith(":RbReviewAdapter"):
        return {"bot_login": str(getattr(ctx.settings, "improve_rb_bot_login", "") or "")}
    return {}


def _judge_spec(ctx: StepContext):
    """The gold_match judge from settings.improve_judge (or params.judge);
    None when unset (cells stay unlabeled and the report says so)."""
    from ...improve.judges import judge_spec_from

    return judge_spec_from(ctx.settings, str(ctx.params.get("judge") or ""))


def _collect_outcomes(ctx: StepContext, adapter, units, store) -> dict:
    """Outcome collection BEFORE the matrix: the bot adapter fetches thread
    labels inside fetch(); the eval adapter imports the paired judge's
    verdicts and, when a judge is configured, decides every undecided gold
    entry with gold_match. Nothing here is fabricated: without a judge the
    cells stay unlabeled and the report counts them."""
    from ...improve.adapters.review_eval import ReviewEvalAdapter
    from ...improve.judges import JudgeError

    summary = {"verdicts": 0, "gold_matched": 0, "judge": "", "errors": []}
    if not isinstance(adapter, ReviewEvalAdapter):
        return summary
    judge = _judge_spec(ctx)
    if judge is not None:
        adapter.judge = judge
        adapter.llm = ctx.llm if judge.kind == "api" else None
        adapter.governor = getattr(ctx, "governor", None)
        summary["judge"] = f"{judge.kind}:{judge.provider + ':' if judge.provider else ''}{judge.model}"
    for unit in units:
        try:
            summary["verdicts"] += adapter.collect_judge_verdicts(unit, store)
        except Exception as exc:  # noqa: BLE001 - one bad judgment file must not stop the rest
            summary["errors"].append(f"{unit.unit_id}: verdicts: {exc}"[:200])
        gold = adapter.gold(unit.item)
        if gold is None or judge is None:
            continue
        try:
            summary["gold_matched"] += adapter.gold_match(unit, gold, store)
        except JudgeError as exc:
            summary["errors"].append(f"{unit.unit_id}: gold_match: {exc}"[:200])
    summary["inconclusive"] = len(getattr(adapter, "inconclusive", []) or [])
    return summary


@step("improve.experiments", "deterministic", "read",
      "Run every registered experiment (shadow arms, paired verdicts) before forensics; reserved budget first.")
async def _experiments(ctx: StepContext) -> StepResult:
    """Registered experiments run first in a cycle (design §10: their cost
    was reserved at registration); each ends with a label. Params:
    `experiment` (one id), `dry_run` (list only)."""
    from ...improve import experiments as exps
    from ...improve.budget import governed
    from ...improve.cycle import governor_for
    from ...trace_store import bind_store, trace_context

    store = _trace_store(ctx)
    if store is None:
        return StepResult(False, FailureKind.BLOCKED, "no trace store (set TRACE_STORE_ROOT)")
    ledger_dir = _ledger_dir(ctx)
    pending = exps.list_experiments(ledger_dir, state="registered")
    only = str(ctx.params.get("experiment") or "")
    if only:
        pending = [e for e in pending if e.experiment_id == only]
    if ctx.params.get("dry_run"):
        return StepResult(True, summary=f"{len(pending)} registered experiment(s) pending",
                          outputs={"pending": [e.experiment_id for e in pending]})
    governor = governor_for(ctx.settings, ledger_dir)
    results: dict = {}
    with bind_store(store), governed(governor), trace_context(playbook="workflow-improve", run_id=ctx.run_dir.name):
        for exp in pending:
            try:
                done = exps.run(store, ctx.settings, ledger_dir, exp.experiment_id, governor=governor, judge_llm=ctx.llm)
                results[exp.experiment_id] = {"state": done.state, **{k: done.result.get(k) for k in
                                                                       ("mean", "lo", "hi", "n_retained", "n_required")}}
            except Exception as exc:  # noqa: BLE001 - one experiment must not stop the others
                results[exp.experiment_id] = {"state": "error", "error": str(exc)[:300]}
    updates = {"improve_experiments": results}
    ctx.state.update(updates)
    return StepResult(True, summary=f"{len(results)} experiment(s) adjudicated: "
                                    + ", ".join(f"{k}={v['state']}" for k, v in results.items()) if results else "no registered experiments",
                      outputs={"experiments": results, "state_updates": updates})
