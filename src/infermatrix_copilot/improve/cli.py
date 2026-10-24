"""``infermatrix-copilot improve`` — operate the meta-improvement engine.

    improve migrate-index   [--trace-root DIR] [--offline-confirmed] [--no-backup]
                            upgrade the live trace index to the current schema
                            (backup, ALTER, backfill, verify); refused unless
                            every writer is paused
    improve rebuild-index   [--to-schema 0|2] [--offline-confirmed]
                            rebuild the index from the JSONL records into a temp
                            file and swap it in atomically (same gate); --to-schema 0
                            is the pin-downgrade path
    improve rollback-index  alias of rebuild-index --to-schema 0
    improve compare-index   read-only: rebuild a throwaway copy and diff it
                            against the live index (safe with live writers)
    improve verify-index    read-only: id sets and schema-2 columns vs the records
    improve cycle           [--since EPOCH] [--until EPOCH] [--force] [--ledger-dir DIR]
                            run one weekly cycle now (Tier 1 lints, baselines,
                            ledger, report; dry-run — proposals stay local)
    improve ledger          [--workflow W] [--ledger-dir DIR]   show the ledgers
    improve lints           the Tier 1 lint catalogue (ids, versions, origins)
    improve gold draft --item repo#N [--gt-dir DIR]   draft a curated gold file from raw comments
    improve gold check [--gt-dir DIR]                  validate every curated gold file
    improve meta lint-check [--meta-dir DIR]           every meta lint sample triggers its lint
    improve budget          [--ledger-dir DIR]          the week's envelopes: settled, reserved, remaining
    improve experiment register --workflow W --hypothesis TEXT --metric M --items a,b,c
                            --arm KEY=VAL [--arm ...] [--incumbent KEY=VAL ...] [--min-effect X]
                            [--replicates N] [--direction higher|lower] [--proposal ID]
    improve experiment run ID          run a registered experiment now (shadow; reserved budget)
    improve experiment list [--state S]

The writer-quiescence gate (design §7.1). Pause flags alone do not stop an
intake or sweep that is already running, so the gate requires evidence that
no writer is executing: the knowledge service's ledger shows every repository
(and the global row) paused AND holds no live lease (``kb serve`` renews its
lease while it runs; a stopped service has released it or let it expire), and
the review bot's pause marker file (``RB_REVIEW_PAUSE_MARKER``) exists and is
non-empty — the bot writes it, with its ack time, only after its in-flight
reviews have drained. ``--offline-confirmed`` skips the gate and is for an
operator who has verified quiescence by hand. Independently of the gate, the
migration and rebuild paths catch up any JSONL record the index lacks and
verify id-set equality before declaring the index good.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable

DEFAULT_KB_STATE_DIR = Path.home() / ".infermatrix-copilot" / "kb"


def _trace_root(value: str | None) -> Path:
    if value:
        return Path(value).expanduser()
    env = os.environ.get("TRACE_STORE_ROOT")
    if env:
        return Path(env).expanduser()
    return Path(os.environ.get("KB_STATE_DIR") or DEFAULT_KB_STATE_DIR).expanduser() / "traces"


def writers_paused_check(kb_state_dir: Path | None = None, rb_marker: str | None = None,
                         environ: dict | None = None) -> Callable[[], tuple[bool, str]]:
    """The gate the store's migration commands call: ``(paused, detail)``."""
    env = os.environ if environ is None else environ
    state_dir = kb_state_dir or Path(env.get("KB_STATE_DIR") or DEFAULT_KB_STATE_DIR).expanduser()
    marker = rb_marker if rb_marker is not None else env.get("RB_REVIEW_PAUSE_MARKER", "")

    def check() -> tuple[bool, str]:
        problems: list[str] = []
        ledger_path = state_dir / "kb.db"
        if ledger_path.exists():
            from ..kb_service.ledger import Ledger

            ledger = Ledger(ledger_path)
            live = [row["repo"] for row in ledger.all_repo_states() if not int(row.get("paused") or 0)]
            if live:
                problems.append(f"knowledge service not paused for {live} (run `kb pause --all`)")
            holder = ledger.live_lease()
            if holder:
                problems.append(f"knowledge service is running (lease held by {holder}); stop `kb serve` first")
        if not marker:
            problems.append("RB_REVIEW_PAUSE_MARKER is unset: name the review bot's pause marker file")
        else:
            path = Path(marker).expanduser()
            if not path.exists():
                problems.append(f"review bot pause marker missing: {marker}")
            elif not path.read_text(encoding="utf-8").strip():
                problems.append(f"review bot pause marker is empty (no drain ack): {marker}")
        return (not problems, "; ".join(problems) or "all writers quiescent")

    return check


def _store(args):
    from ..trace_store import TraceStore

    return TraceStore(_trace_root(args.trace_root))


def _gate(args):
    return None if args.offline_confirmed else writers_paused_check()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="infermatrix-copilot improve",
                                     description="operate the meta-improvement engine's trace index")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, choices in (("workflows", ["list", "check", "prepare", "import-meta"]), ("evolve", ["run", "list", "show"])):
        p = sub.add_parser(name)
        p.add_argument("action", choices=choices)
        p.add_argument("id", nargs="?", default="")
        p.add_argument("--workflow", default="all")
        p.add_argument("--repo", default="")
        p.add_argument("--post", action="store_true")
        p.add_argument("--trace-root", default=None)
        p.add_argument("--ledger-dir", default=None)
        p.add_argument("--data-dir", default=None)
    ex = sub.add_parser("experiment")
    ex.add_argument("action", choices=["register", "run", "list"])
    ex.add_argument("id", nargs="?", default="")
    ex.add_argument("--trace-root", default=None)
    ex.add_argument("--ledger-dir", default=None)
    ex.add_argument("--workflow", default="")
    ex.add_argument("--hypothesis", default="")
    ex.add_argument("--metric", default="recall_review")
    ex.add_argument("--items", default="")
    ex.add_argument("--arm", action="append", default=[])
    ex.add_argument("--incumbent", action="append", default=[])
    ex.add_argument("--min-effect", type=float, default=0.05)
    ex.add_argument("--replicates", type=int, default=3)
    ex.add_argument("--direction", default="higher")
    ex.add_argument("--proposal", default="")
    ex.add_argument("--state", default=None)
    bp = sub.add_parser("budget")
    bp.add_argument("--ledger-dir", default=None)
    bp.add_argument("--trace-root", default=None)
    reconciliation = bp.add_mutually_exclusive_group()
    reconciliation.add_argument('--reconcile-native', default='', metavar='RESERVATION_ID')
    reconciliation.add_argument('--reconcile-judge', default='', metavar='RESERVATION_ID')
    bp.add_argument('--offline-confirmed', action='store_true')
    for name in ("publish", "sync"):
        p = sub.add_parser(name, help="proposal publication through the maintainer routine's outbox (publish needs ALLOW_POST=1)")
        p.add_argument("--ledger-dir", default=None)
        p.add_argument("--trace-root", default=None)
        p.add_argument("--outbox-dir", default=None)
        p.add_argument("--repo", default=None)
        if name == "publish":
            p.add_argument("--dry-run", action="store_true", help="plan only; write nothing")
    for name in ("gold", "meta"):
        p = sub.add_parser(name)
        p.add_argument("action", choices=["draft", "check"] if name == "gold" else ["lint-check", "bench", "export", "annotate"])
        p.add_argument("--item", default="")
        p.add_argument("--gt-dir", default="eval/dataset/gt")
        p.add_argument("--meta-dir", default="eval/dataset/meta")
        if name == "meta":
            p.add_argument("--case", default="", help="bench/export/annotate: case name")
            p.add_argument("--unit-id", default="")
            p.add_argument("--gold-file", default="")
            p.add_argument("--labels-file", default="")
            p.add_argument("--human-verified", action="store_true")
            p.add_argument("--split", choices=["development", "holdout"], default="development")
            p.add_argument("--trace-root", default=None)
            p.add_argument("--ledger-dir", default=None)
    for name in ("migrate-index", "rebuild-index", "rollback-index", "compare-index", "verify-index",
                 "cycle", "ledger", "lints"):
        p = sub.add_parser(name)
        p.add_argument("--trace-root", default=None, help="trace store root (TRACE_STORE_ROOT / KB_STATE_DIR/traces)")
        if name in ("migrate-index", "rebuild-index", "rollback-index"):
            p.add_argument("--offline-confirmed", action="store_true",
                           help="skip the writer-pause gate (operator verified both writers by hand)")
        if name == "migrate-index":
            p.add_argument("--no-backup", action="store_true")
        if name == "rebuild-index":
            p.add_argument("--to-schema", type=int, default=None, help="0 (pin downgrade) or the current schema")
        if name in ("cycle", "ledger"):
            p.add_argument("--ledger-dir", default=None)
        if name == "cycle":
            p.add_argument("--since", type=float, default=None)
            p.add_argument("--until", type=float, default=None)
            p.add_argument("--force", action="store_true", help="run even when improve_enabled is off (operator)")
        if name == "ledger":
            p.add_argument("--workflow", default=None)
    args = parser.parse_args(argv)
    if args.command in ("workflows", "evolve"):
        from ..config import Settings
        from . import evolution
        settings = Settings()
        updates = {}
        if args.ledger_dir: updates["improve_ledger_dir"] = args.ledger_dir
        if args.data_dir: updates["improve_evolve_data_dir"] = args.data_dir
        settings = settings.model_copy(update=updates)
        store = _store(args)
        try:
            if args.command == "workflows":
                if args.action == "list": result = evolution.list_workflows(settings, store)
                elif args.action == "check": result = evolution.check(settings, store, args.workflow, repo=args.repo)
                elif args.action == "prepare":
                    from .objectives import prepare
                    result = prepare(settings, store, workflow=args.workflow, repo=args.repo)
                else:
                    from .drivers import export_meta
                    if not settings.improve_evolve_data_dir: raise ValueError("import-meta needs --data-dir or IMPROVE_EVOLVE_DATA_DIR")
                    result = export_meta(settings, Path(settings.improve_evolve_data_dir))
            elif args.action == "list": result = evolution.candidates(settings)
            elif args.action == "show": result = evolution.candidate(settings, args.id)
            else:
                from .coordinator import run
                coordinated = run(settings, store, workflow=args.workflow, repo=args.repo, post=args.post)
                result = coordinated.get("stages", {}).get("evolve", coordinated)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 3 if isinstance(result, dict) and (result.get("state") in ("disabled", "deferred", "rejected") or result.get("ready") is False) else 0
        except (ValueError, OSError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
    if args.command in ("gold", "meta"):
        return _gold_meta_commands(args)
    if args.command in ("experiment", "budget"):
        return _experiment_commands(args)
    if args.command in ("publish", "sync"):
        return _publish_commands(args)
    if args.command == "lints":
        from .lints import catalogue

        print(json.dumps(catalogue(), ensure_ascii=False, indent=1))
        return 0
    if args.command in ("cycle", "ledger"):
        return _cycle_commands(args)
    store = _store(args)
    try:
        if args.command == "migrate-index":
            report = store.migrate_index(writers_paused=_gate(args), offline_confirmed=args.offline_confirmed,
                                         backup=not args.no_backup)
        elif args.command in ("rebuild-index", "rollback-index"):
            from ..trace_store import SCHEMA_VERSION

            target = 0 if args.command == "rollback-index" else (
                SCHEMA_VERSION if args.to_schema is None else args.to_schema)
            count = store.rebuild_index(to_schema=target, writers_paused=_gate(args),
                                        offline_confirmed=args.offline_confirmed)
            report = {"rebuilt": count, "schema": store.index_version(), "verify": store.verify_index()}
        elif args.command == "compare-index":
            report = store.compare_index()
        else:
            report = store.verify_index()
    except Exception as exc:  # noqa: BLE001 - the CLI reports, the exit code decides
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return 0 if report.get("ok", True) else 1



def _ledger_dir(args) -> Path:
    from ..config import Settings
    from .cycle import ledger_dir_for

    return Path(args.ledger_dir).expanduser() if args.ledger_dir else ledger_dir_for(Settings())


def _cycle_commands(args) -> int:
    from ..config import Settings
    from .cycle import CycleRefused, run_cycle
    from .ledger import Ledger

    ledger_dir = _ledger_dir(args)
    if args.command == "ledger":
        ledger = Ledger(ledger_dir)
        names = [args.workflow] if args.workflow else ledger.workflows()
        out = {}
        for name in names:
            wl = ledger.load(name)
            out[name] = {"tier": wl.tier, "declared": wl.declared, "hold": wl.hold, "cycles": wl.cycles[-4:],
                         "proposals": [{"id": p.id, "state": p.state, "lint": p.lint, "stage": p.stage,
                                        "claim": p.claim, "issue": p.issue} for p in wl.proposals]}
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        return 0
    settings = Settings()
    if args.command == "cycle" and settings.improve_evolve_enabled:
        from .coordinator import run
        if args.ledger_dir:
            settings = settings.model_copy(update={"improve_ledger_dir": args.ledger_dir})
        report = run(settings, _store(args))
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    store = _store(args)
    try:
        report = run_cycle(store, settings, ledger_dir, since=args.since, until=args.until,
                           dry_run=True, force=args.force)
    except CycleRefused as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({k: report[k] for k in ("at", "since", "until", "units", "proposals_opened", "seconds")},
                     ensure_ascii=False, indent=1, default=str))
    print(f"report: {ledger_dir / 'reports'}", file=sys.stderr)
    return 0


def _gold_meta_commands(args) -> int:
    from .gold import CURATED_DIRNAME, load_gold, write_draft

    if args.command == "gold" and args.action == "draft":
        if not args.item:
            print("gold draft needs --item repo#N", file=sys.stderr)
            return 2
        path = write_draft(Path(args.gt_dir), args.item)
        print(json.dumps({"drafted": str(path)}))
        return 0
    if args.command == "gold":
        problems = {}
        curated = Path(args.gt_dir) / CURATED_DIRNAME
        for path in sorted(curated.glob("*.gold.json")):
            try:
                gold = load_gold(path)
                problems[path.name] = "ok" if gold.status == "curated" else "draft (not used)"
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                problems[path.name] = f"INVALID: {exc}"
        print(json.dumps(problems, ensure_ascii=False, indent=1))
        return 0 if not any(v.startswith("INVALID") for v in problems.values()) else 1
    if args.command == "meta" and args.action in ("export", "annotate"):
        from .annotations import operate
        try:
            report = operate(args, _store(args))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        except (ValueError, OSError, KeyError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
    if args.command == "meta" and args.action == "bench":
        return _meta_bench(args)
    from ..trace_store import TraceStore
    from .lints import Baseline, run_lints
    from .meta import lint_samples

    report = {}
    ok = True
    for lint_id, units in lint_samples(Path(args.meta_dir)).items():
        for unit in units:
            found = {f.lint for f in run_lints(unit, TraceStore(Path(args.meta_dir) / "lints" / lint_id), Baseline())}
            hit = lint_id in found
            ok &= hit
            report[f"{lint_id}/{unit.unit_id}"] = "ok" if hit else f"MISSED (found {sorted(found)})"
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if ok else 1


def _meta_bench(args) -> int:
    """Run the engine's forensics role over the frozen cases in-process
    (operator use: the acceptance's blind test) and report agreement, kappa
    and lint recall per case; every model call is traced and governed."""
    from ..config import Settings
    from ..llm import LLM
    from ..trace_store import bind_store, trace_context
    from .budget import governed
    from .cycle import governor_for
    from .meta import load_cases, run_case

    settings = Settings()
    store = _store(args)
    meta_dir = Path(args.meta_dir)
    cases = [c for c in load_cases(meta_dir) if not args.case or c.name == args.case]
    if not cases:
        print(json.dumps({"error": f"no cases under {meta_dir}" + (f" named {args.case}" if args.case else "")}), file=sys.stderr)
        return 1
    llm = LLM(settings)
    if not llm.available:
        print(json.dumps({"error": "no LLM configured for the forensics agents"}), file=sys.stderr)
        return 1
    from ..agent_loop import run_agent

    agents = {}
    for mode in ("eco", "performance"):
        try:
            target = settings.tier_target(mode)
        except Exception:  # noqa: BLE001 - an unconfigured tier is absent
            continue
        family = f"{target.provider_id or target.source}:{target.model}"
        if family in agents:
            continue
        member = llm.for_target(target) if hasattr(llm, "for_target") else llm

        def make(llm_=member, model_=target.model):
            def agent(system, prompt, scope, extra_tools, max_iters):
                return run_agent(llm_, system=system, prompt=prompt, scope=scope, model=model_, max_iters=max_iters,
                                 extra_tools=extra_tools).text
            return agent
        agents[family] = make()
    governor = governor_for(settings, _ledger_dir(args))
    report = {}
    with bind_store(store), governed(governor):
        for case in cases:
            with trace_context(playbook="workflow-improve", step="improve.forensics", run_id=f"bench-{case.name}",
                               unit_id=f"bench-{case.name}:improve.forensics", item=f"meta:{case.name}",
                               workflow="workflow-improve.improve.forensics"):
                result = run_case(store, case, agents, meta_dir=meta_dir)
            report[case.name] = {k: result.get(k) for k in ("cells", "attributed", "agreement", "kappa", "lint_recall",
                                                             "disputed", "engine", "human")}
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return 0


def _publish_commands(args) -> int:
    import os
    import time

    from ..config import Settings
    from .ledger import Ledger
    from .publish import ProposalOutbox, PublishError, plan, publish, sync

    settings = Settings()
    ledger_dir = _ledger_dir(args)
    store = _store(args)
    root = args.outbox_dir or getattr(settings, "improve_outbox_dir", "") or ""
    if not root:
        print(json.dumps({"error": "no outbox configured (--outbox-dir / IMPROVE_OUTBOX_DIR)"}), file=sys.stderr)
        return 2
    outbox = ProposalOutbox(Path(root).expanduser())
    ledger = Ledger(ledger_dir, store)
    now = time.time()
    if args.command == "sync":
        print(json.dumps(sync(ledger, outbox, store, now=now), ensure_ascii=False, indent=1, default=str))
        return 0
    repo = args.repo or getattr(settings, "improve_proposal_repo", "") or ""
    try:
        if args.dry_run:
            report = plan(ledger, store, repo=repo or "?", now=now, ledger_ref=str(ledger_dir))
            report["dry_run"] = True
        else:
            # the CLI is an operator's hand: the same ALLOW_POST gate as the step
            report = publish(ledger, store, outbox, repo=repo, now=now, ledger_ref=str(ledger_dir),
                             dry_run=os.environ.get("ALLOW_POST") != "1")
    except PublishError as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in report.items() if k != "actions"} | {
        "actions": [{k: a[k] for k in ("id", "action", "proposal", "workflow", "state")} for a in report["actions"]]},
        ensure_ascii=False, indent=1, default=str))
    return 0


def _kv(pairs: list[str]) -> dict:
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"override must be KEY=VALUE, got {pair!r}")
        out[key.strip()] = value
    return out


def _experiment_commands(args) -> int:
    from ..config import Settings
    from . import experiments as exps
    from .cycle import governor_for

    settings = Settings()
    ledger_dir = _ledger_dir(args)
    if args.command == "budget":
        governor = governor_for(settings, ledger_dir)
        if args.reconcile_native or args.reconcile_judge:
            from .budget import BudgetRefused, BudgetBreach
            from ..trace_store import MigrationError
            try:
                reconcile = governor.reconcile_judge if args.reconcile_judge else governor.reconcile_native
                result = reconcile(args.reconcile_judge or args.reconcile_native, offline_confirmed=args.offline_confirmed)
            except (BudgetRefused, BudgetBreach, ValueError, OSError, KeyError, MigrationError) as exc:
                print(json.dumps({'error': str(exc), 'replacement_execution_authorized': False}), file=sys.stderr)
                return 2
        else:
            result = governor.remaining()
        print(json.dumps(result, indent=1))
        return 0
    store = _store(args)
    try:
        if args.action == "list":
            rows = [{"id": e.experiment_id, "workflow": e.workflow, "state": e.state, "metric": e.metric,
                     "n_required": e.n_required, "n_available": e.n_available, "label": e.result.get("label")}
                    for e in exps.list_experiments(ledger_dir, state=args.state)]
            print(json.dumps(rows, indent=1))
            return 0
        if args.action == "register":
            exp = exps.register(store, settings, ledger_dir, workflow=args.workflow, hypothesis=args.hypothesis,
                                metric=args.metric, items=[i for i in args.items.split(",") if i.strip()],
                                arm_overrides=_kv(args.arm), incumbent_overrides=_kv(args.incumbent),
                                direction=args.direction, min_effect=args.min_effect, replicates=args.replicates,
                                proposal_id=args.proposal, governor=governor_for(settings, ledger_dir),
                                registered_by="owner")
            print(json.dumps({"registered": exp.experiment_id, "n_required": exp.n_required,
                              "n_available": exp.n_available, "adequately_powered": exp.adequately_powered,
                              "cost_estimate_usd": exp.cost_estimate_usd, "fingerprint_diff": exp.fingerprint_diff},
                             indent=1, default=str))
            return 0
        if not args.id:
            print("experiment run needs an id", file=sys.stderr)
            return 2
        from ..trace_store import bind_store
        from .budget import governed

        governor = governor_for(settings, ledger_dir)
        with bind_store(store), governed(governor):
            exp = exps.run(store, settings, ledger_dir, args.id, governor=governor)
        print(json.dumps({"experiment": exp.experiment_id, "state": exp.state, **exp.result}, indent=1, default=str))
        return 0
    except exps.ExperimentError as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
