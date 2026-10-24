"""``infermatrix-copilot kb`` — operate the knowledge service.

    kb keygen --out PATH              create a signing key (0600) and print its public key
    kb status                         repositories, modes, generations, queues
    kb pause  (--repo R | --all) --reason TEXT
    kb resume (--repo R | --all)
    kb control                        refresh the signed control record now
    kb accept-unknown SHA             judge an unknown knowledge commit through the gate instead of reverting it
    kb reconcile-review CHANGESET     settle a saved completed gate without model calls or publication
    kb run --playbook kb-intake --repo R   run one knowledge playbook once
    kb calibrate --repo R             run the judge over the repository's calibration set
    kb serve [--once]                 the scheduler (holds the single-writer lease)
    kb activate                       activate the knowledge snapshot of the repository's main now
    kb rollback --to SHA              point `active` back at an earlier snapshot
    kb traces [--kind K] [--changeset ID] [--rule ID] [--limit N]   query trace/1 records
    kb replay --record ID --model PROVIDER:MODEL[:EFFORT]           re-ask a recorded call
    kb recover-attempt --attempt ID --record ID --offline-confirmed finalize an archived call without dispatch
    kb reconcile-init-cost --repo R --stage S --record ID --offline-confirmed settle original init accounting
    kb export --out FILE [--role judge|generator]                   dataset (calibration-safe)
    kb init REPO --stage STAGE [--dry-run] [--pin SHA]
                                      bootstrap a repository's knowledge base, one
                                      human-merged stage at a time (skeleton,
                                      feature-discovery, modules, knowledge, deepen,
                                      pr-history, harvest-calibration);
                                      never touches kb.db
    kb init REPO --suggest-seeds      rank existing knowledge pages worth seeding from (no model call)
    kb widen REPO [--dry-run]         enrich existing feature and file knowledge
    kb deepen REPO [--dry-run]        deepen implementation knowledge per feature
    kb publish (--remote HOST:/STATE_DIR | --local DIR) [--once]
                                      the publisher (GPU box, owner's gh login): perform
                                      the signed outbox items; writes need ALLOW_POST=1
                                      (and ALLOW_PUSH=1 to push branches)

Pause/resume bump the repository's generation, so every outbox item issued
before it is void; they then re-sign the control record and hold list at once.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_STATE_DIR = Path.home() / ".infermatrix-copilot" / "kb"


def _state_dir(value: str | None) -> Path:
    return Path(value or os.environ.get("KB_STATE_DIR") or DEFAULT_STATE_DIR).expanduser()


def _ledger(state_dir: Path):
    from .ledger import Ledger

    return Ledger(state_dir / "kb.db")


def _registry():
    from ..resources import adapters_root
    from .config import general_lifecycle, load_registry

    general = general_lifecycle(
        enabled=os.environ.get("KB_GENERAL_ENABLED", "0") == "1",
        mode=os.environ.get("KB_GENERAL_MODE", "shadow"),
    )
    return load_registry(Path(os.environ.get("ADAPTERS_DIR") or adapters_root()), general=general)


def _signing_key():
    from ..knowledge_service.signing import load_private_key

    path = os.environ.get("KB_SIGNING_KEY")
    if not path:
        raise SystemExit("KB_SIGNING_KEY is not set (path to the service's Ed25519 private key)")
    return load_private_key(path)


def _sync_repos(ledger, registry) -> bool:
    """Record configured modes; True when any mode changed (and so a
    generation was bumped and the signed control record must be re-issued)."""
    changed = False
    for lifecycle in registry.values():
        changed |= ledger.ensure_repo(lifecycle.repo, lifecycle.mode if lifecycle.enabled else "disabled")
    return changed


def _outbox(state_dir: Path, ledger):
    from .outbox import Outbox

    return Outbox(state_dir, _signing_key(), ledger, clock=time.time)


def _unknown_status(ledger, reviewed: set[str] | None = None) -> dict:
    """Unknown knowledge commits not disposed of, with any accept request."""
    from .accept import ACCEPT
    from .audit import DISPOSED, UNKNOWN

    disposed = {n[len(DISPOSED):] for n in ledger.cursors_with_prefix("*", DISPOSED)}
    reviewed = reviewed or set()
    out = {}
    for name, raw in ledger.cursors_with_prefix("*", UNKNOWN).items():
        sha = name[len(UNKNOWN):]
        if sha in disposed or sha in reviewed:
            continue
        record = json.loads(raw)
        request = ledger.get_cursor("*", ACCEPT + sha)
        out[sha] = {"state": record.get("state"), "reason": record.get("reason"),
                    "accept": json.loads(request) if request else None}
    return out


def _refresh(state_dir: Path, ledger, registry) -> None:
    _outbox(state_dir, ledger).transition(lambda: None)


def _init_command(args, state_dir: Path) -> int:
    from ..config import Settings
    from .repo_spec import RepoRegistry
    if RepoRegistry(state_dir).resolve(args.repo) is not None:
        from .portable_commands import run_portable_init
        from .init_support import InitError
        try:
            return run_portable_init(args, state_dir)
        except (InitError, ValueError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if args.suggest_seeds == bool(args.stage):
        print("kb init: pass exactly one of --stage or --suggest-seeds", file=sys.stderr)
        return 2
    if args.unlimited_subscription and (args.stage not in ("feature-discovery", "modules", "knowledge", "knowledge-deepen") or args.budget_usd is not None):
        print("--unlimited-subscription is for feature-discovery, modules, knowledge or knowledge-deepen only and conflicts with --budget-usd", file=sys.stderr)
        return 2
    if args.suggest_seeds:
        from .init_stages import suggest_seeds
        from .init_support import InitError, InitRuntime

        try:
            rt = InitRuntime.from_env(Settings(), state_dir=state_dir)
            lifecycle = rt.registry.get(args.repo)
            if lifecycle is None:
                print(f"no adapter declares knowledge repo {args.repo!r}", file=sys.stderr)
                return 2
            for path, score, shared in suggest_seeds(rt, lifecycle):
                print(f"{score:6.3f}  {path}  ({', '.join(shared[:8])})")
        except InitError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    from .runner import run_playbook

    params = {"stage": args.stage, "dry_run": "true" if args.dry_run else "false", "pin": args.pin or ""}
    from ..engine.steps.knowledge import init_options
    from .init_stages import validate_options
    from .init_support import InitError

    for name in ("foundation_mode", "foundation_record", "acceptance_mode", "from_existing",
                 "subscription_generator", "unlimited_subscription", "retry_unfinished", "pr_count", "budget_usd"):
        value = getattr(args, name, None)
        if value is not None and value is not False and value != "strict":
            params[name] = str(value).lower() if isinstance(value, bool) else str(value)
    try:
        validate_options(args.stage, init_options(params))
    except InitError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    outcome, run_dir = run_playbook(Settings(), "kb-init", args.repo, state_dir=state_dir, params=params)
    print(f"kb init {args.repo} {args.stage}: {outcome.status} ({run_dir})")
    if getattr(outcome, "blocked_reason", ""):
        print(outcome.blocked_reason, file=sys.stderr)
    return 0 if outcome.status == "done" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="infermatrix-copilot kb")
    parser.add_argument("--state-dir", help="default: $KB_STATE_DIR or ~/.infermatrix-copilot/kb")
    sub = parser.add_subparsers(dest="command", required=True)
    keygen = sub.add_parser("keygen")
    keygen.add_argument("--out", required=True)
    sub.add_parser("status")
    for name in ("pause", "resume"):
        cmd = sub.add_parser(name)
        target = cmd.add_mutually_exclusive_group(required=True)
        target.add_argument("--repo")
        target.add_argument("--all", action="store_true")
        if name == "pause":
            cmd.add_argument("--reason", required=True)
    sub.add_parser("control")
    accept = sub.add_parser("accept-unknown")
    accept.add_argument("sha")
    review = sub.add_parser("reconcile-review", help="settle a completed gate receipt without model calls or publication")
    review.add_argument("changeset")
    reconcile = sub.add_parser("reconcile-reviewed", help="verify and admit an exact owner-merged knowledge history")
    action = reconcile.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", type=Path, help="write a signed plan; does not admit any commit")
    action.add_argument("--apply", type=Path, help="revalidate and apply a previously inspected signed plan")
    reconcile.add_argument("--target", help="exact current main SHA, required when preparing a plan")
    reconcile.add_argument("--allow-merger", action="append", default=[])
    reconcile.add_argument("--require-check", action="append", default=[])
    reconcile.add_argument("--reason", default="")
    reconcile.add_argument("--event-coverage", type=Path,
                           help="settle selected source events against already admitted, active owner-merged rules")
    run = sub.add_parser("run")
    run.add_argument("--playbook", required=True)
    run.add_argument("--repo", required=True)
    calibrate = sub.add_parser("calibrate")
    calibrate.add_argument("--repo", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--once", action="store_true")
    maintain = sub.add_parser("maintain", help="plan or queue durable nightly maintenance")
    actions = maintain.add_subparsers(dest="maintenance_action", required=True)
    for name in ("plan", "run", "status"):
        action = actions.add_parser(name)
        target = action.add_mutually_exclusive_group(required=True)
        target.add_argument("--repo")
        target.add_argument("--all", action="store_true")
        if name == "run":
            action.add_argument("--request-id", required=True, help="stable idempotent scheduler request identity")
            extra = action.add_mutually_exclusive_group()
            extra.add_argument("--calibrate", action="store_true")
            extra.add_argument("--drill", action="store_true", help="queue an isolated SDK revocation drill")
    correction = sub.add_parser("correction", help="resolve a maintenance finding through the scheduler")
    resolve = correction.add_subparsers(dest="correction_action", required=True).add_parser("resolve")
    resolve.add_argument("--id", required=True, help="immutable maintenance finding ID")
    resolve.add_argument("--decision", required=True, choices=("confirm", "dismiss"))
    resolve.add_argument("--evidence", required=True, help="JSON evidence object, or @path to a JSON file")
    resolve.add_argument("--request-id", help="optional stable identity; default is a digest of the signed owner input")
    activation = sub.add_parser("activate")
    activation.add_argument("--snapshot", type=Path, help="verify and activate an explicitly selected portable snapshot")
    activation.add_argument("--allow-partial", action="store_true", help="explicitly serve an accepted foundation; initialization remains incomplete")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--to", required=True)
    traces = sub.add_parser("traces")
    traces.add_argument("--kind")
    traces.add_argument("--changeset")
    traces.add_argument("--rule")
    traces.add_argument("--run")
    traces.add_argument("--limit", type=int, default=50)
    recover = sub.add_parser("recover-attempt", help="finalize an interrupted native archive from its original trace")
    recover.add_argument("--attempt", required=True)
    recover.add_argument("--record", required=True)
    recover.add_argument("--outcome", choices=("complete", "failed"), default="complete",
                         help="failed preserves a recorded failure after an explicitly completed native call")
    recover.add_argument("--offline-confirmed", action="store_true",
                         help="confirm all writers to this trace store have stopped")
    reconcile = sub.add_parser("reconcile-init-cost", help="settle an original init reservation from its completed native trace")
    reconcile.add_argument("--repo", required=True)
    reconcile.add_argument("--stage", required=True)
    reconcile.add_argument("--record", required=True)
    reconcile.add_argument("--outcome", choices=("complete", "failed"), default="complete",
                           help="failed settles cost only for an explicitly completed original failure")
    reconcile.add_argument("--offline-confirmed", action="store_true",
                           help="confirm all init and trace writers have stopped")
    review_recovery = sub.add_parser("reconcile-init-review", help="recover an original completed quality result")
    review_recovery.add_argument("--repo", required=True)
    from .init_support import STAGES, INDEPENDENT_STAGES
    review_recovery.add_argument("--stage", choices=STAGES + INDEPENDENT_STAGES, required=True)
    review_recovery.add_argument("--record", required=True)
    review_recovery.add_argument("--offline-confirmed", action="store_true",
                                 help="confirm all init and trace writers have stopped")
    foundation_recovery = sub.add_parser("reconcile-foundation", help="recover an original foundation task from retained local inputs")
    foundation_recovery.add_argument("--repo", required=True)
    foundation_recovery.add_argument("--dispatch", required=True, help="original dispatch SHA-256")
    foundation_recovery.add_argument("--attempt", required=True, type=int)
    foundation_recovery.add_argument("--offline-confirmed", action="store_true",
                                     help="confirm all init and trace writers have stopped")
    discovery_recovery = sub.add_parser("reconcile-discovery", help="settle an original discovery task from retained local inputs")
    discovery_recovery.add_argument("--repo", required=True)
    discovery_recovery.add_argument("--dispatch", required=True, help="original dispatch SHA-256")
    discovery_recovery.add_argument("--attempt", required=True, type=int)
    discovery_recovery.add_argument("--offline-confirmed", action="store_true",
                                    help="confirm all init and trace writers have stopped")
    replay = sub.add_parser("replay")
    replay.add_argument("--record", required=True)
    replay.add_argument("--model", required=True, help="provider:model[:effort], e.g. codex:gpt-6-mini:low")
    export = sub.add_parser("export")
    export.add_argument("--out", required=True)
    export.add_argument("--role", default="judge", choices=("judge", "generator"))
    init = sub.add_parser("init")
    init.add_argument("repo")
    from .init_support import INDEPENDENT_STAGES, STAGES

    init.add_argument("--stage", choices=STAGES + INDEPENDENT_STAGES)
    init.add_argument("--pr-count", type=int, help="PR-history window (default: adapter pr_history_count or 1000)")
    init.add_argument("--budget-usd", type=float, help="incremental discovery/history/depth cumulative ceiling; may be raised to resume")
    init.add_argument("--dry-run", action="store_true",
                      help="write the tree and PR body under the state directory instead of opening a PR")
    init.add_argument("--pin", help="upstream commit to pin (default: the default branch head)")
    init.add_argument("--suggest-seeds", action="store_true")
    init.add_argument("--from-existing", action="store_true",
                      help="enrich a merged KB without local skeleton records (discovery, modules and knowledge stages)")
    init.add_argument("--subscription-generator", action="store_true",
                      help="explicit subscription generator; unreported fees outside stage USD accounting")
    init.add_argument("--unlimited-subscription", action="store_true",
                      help="uncapped discovery/modules/knowledge/depth with authenticated subscription generator and judge; no fallback")
    init.add_argument("--retry-unfinished", action="store_true", help="retry unfinished discovery/depth work, preserving prior spend")
    init.add_argument("--acceptance-mode", choices=("strict", "lightweight"), default="strict",
                      help="depth recognition standard; lightweight uses pinned citations and one independent feature review")
    init.add_argument("--foundation-mode", choices=("strict", "partial"), default="strict",
                      help="explicitly publish retained partial foundation or bind depth to that publication; targets stay unchanged")
    init.add_argument("--foundation-record", type=Path,
                      help="original published partial foundation record, required for partial knowledge-deepen")
    for name, stage in (("widen", "knowledge"), ("deepen", "knowledge-deepen")):
        cmd = sub.add_parser(name, help="feature breadth" if name == "widen" else "feature implementation depth")
        cmd.add_argument("repo")
        cmd.add_argument("--dry-run", action="store_true")
        cmd.add_argument("--pin")
        cmd.add_argument("--subscription-generator", action="store_true")
        cmd.add_argument("--unlimited-subscription", action="store_true")
        cmd.add_argument("--foundation-mode", choices=("strict", "partial"), default="strict")
        cmd.add_argument("--foundation-record", type=Path)
        if name == "deepen":
            cmd.add_argument("--budget-usd", type=float)
            cmd.add_argument("--retry-unfinished", action="store_true")
            cmd.add_argument("--acceptance-mode", choices=("strict", "lightweight"), default="strict")
        cmd.set_defaults(stage=stage, from_existing=True, suggest_seeds=False, pr_count=None,
                         **({"budget_usd": None, "retry_unfinished": False}
                            if name == "widen" else {}))
    publish = sub.add_parser("publish")
    where = publish.add_mutually_exclusive_group(required=True)
    where.add_argument("--remote", help="host:/absolute/path of the service state directory (over ssh)")
    where.add_argument("--local", help="the service state directory on this machine")
    publish.add_argument("--once", action="store_true")
    publish.add_argument("--interval", type=float, default=60.0)
    publish.add_argument("--publisher-state",
                         help="default: $KB_PUBLISHER_STATE or ~/.infermatrix-copilot/kb-publisher")
    from .portable_commands import add_commands
    add_commands(sub)
    args = parser.parse_args(argv)
    supplied = sys.argv[1:] if argv is None else argv
    args.acceptance_mode_explicit = any(value == "--acceptance-mode" or value.startswith("--acceptance-mode=") for value in supplied)

    if args.command in {"maintain", "correction"}:
        from .maintenance_commands import command

        try:
            return command(args, _state_dir(args.state_dir))
        except (ValueError, OSError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if args.command in {"onboard", "repo", "mirror", "update"}:
        from .portable_commands import command
        from .init_support import InitError
        from .sources import SourceError
        from .knowledge_store import KnowledgeStoreError
        try:
            return command(args, _state_dir(args.state_dir))
        except (InitError, SourceError, KnowledgeStoreError, ValueError, OSError, KeyError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.command == "activate" and args.snapshot is not None:
        from .activate import activation_lock, verify_snapshot, switch_active, ActivationError
        from .portable_publication import check_publication
        from .knowledge_store import KnowledgeStoreError
        try:
            snapshot = args.snapshot.resolve(strict=True)
            state_dir = _state_dir(args.state_dir)
            with activation_lock(state_dir):
                view = verify_snapshot(snapshot)
                publication = check_publication(view, allow_partial=args.allow_partial)
                switch_active(state_dir, snapshot)
            print(json.dumps({"active_snapshot": view.public_snapshot, "verified": True, **publication}))
            return 0
        except (ActivationError, KnowledgeStoreError, OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if args.command == "keygen":
        from ..knowledge_service.signing import generate_private_key, public_key_text

        key = generate_private_key(args.out)
        print(public_key_text(key.public_key()))
        return 0

    if args.command == "publish":
        return _publish(args)
    if args.command in {"traces", "replay", "export", "recover-attempt", "reconcile-init-cost", "reconcile-init-review", "reconcile-foundation", "reconcile-discovery"}:
        return _traces_command(args, _state_dir(args.state_dir))
    if args.command in ("init", "widen", "deepen"):
        # before any ledger is opened: kb init never touches the service's kb.db
        return _init_command(args, _state_dir(args.state_dir))
    if args.command == "reconcile-reviewed":
        from ..config import Settings
        from ..knowledge_service.signing import canonical_json
        from .runtime import KbRuntime
        from .reconcile import make_plan, apply_plan

        rt = KbRuntime.from_env(Settings(), state_dir=_state_dir(args.state_dir))
        try:
            key = _signing_key()
            if args.plan:
                options = {"target": args.target or "", "allow_mergers": args.allow_merger,
                           "required_checks": args.require_check, "reason": args.reason}
                if args.event_coverage:
                    from .event_settlement import make_plan as make_event_plan

                    envelope = make_event_plan(rt, key, coverage=json.loads(args.event_coverage.read_text()), **options)
                else:
                    envelope = make_plan(rt, key, **options)
                with args.plan.open("xb") as handle:
                    handle.write(canonical_json(envelope))
                print(f"verified reconciliation plan: {args.plan}; no commit admitted")
            else:
                envelope = json.loads(args.apply.read_text())
                if envelope.get("purpose") == "kb-reviewed-event-plan":
                    from .event_settlement import apply_plan as apply_event_plan

                    receipt = apply_event_plan(rt, envelope, key)
                else:
                    receipt = apply_plan(rt, envelope, key)
                print(f"supervised reconciliation receipt: {receipt}; pause and publication modes unchanged")
            return 0
        except (ValueError, OSError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            rt.ledger.close()

    state_dir = _state_dir(args.state_dir)
    ledger = _ledger(state_dir)
    try:
        if args.command == "reconcile-review":
            from .gate_receipts import settle_gate_result
            from .ledger import LeaseError

            try:
                with ledger.lease() as owner:
                    row = settle_gate_result(ledger, state_dir, owner, args.changeset)
                print(json.dumps({"changeset_id": row["id"], "status": row["status"]}))
                return 0
            except (OSError, ValueError, KeyError, TypeError, LeaseError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
        registry = _registry()
        if _sync_repos(ledger, registry) and os.environ.get("KB_SIGNING_KEY"):
            _refresh(state_dir, ledger, registry)
        if args.command == "status":
            from .intake import unresolved_draft_checkpoints
            from .reconcile import read_receipts
            from .sources import KnowledgeRepo

            public = _signing_key().public_key() if (state_dir / "reconciliations").exists() else None
            reviewed = read_receipts(KnowledgeRepo(Path(os.environ.get("KB_KNOWLEDGE_CLONE") or
                                                      state_dir / "knowledge-repo")), state_dir, public)
            report = {
                "state_dir": str(state_dir),
                "active_snapshot": ledger.active_snapshot(),
                "repos": [
                    {**row, "configured": row["repo"] in registry or row["repo"] == "*",
                     "open_changesets": len(ledger.changesets(row["repo"], ("pr_open", "merge_requested")))
                     if row["repo"] != "*" else None,
                     "unresolved_draft_events": [
                         {"id": event["id"], "detail": event["detail"]}
                         for event in ledger.events(row["repo"], "draft_unknown", limit=-1)
                     ] if row["repo"] != "*" else [],
                     "unresolved_draft_checkpoints": unresolved_draft_checkpoints(ledger, row["repo"])
                     if row["repo"] != "*" else [],
                     "human_queue": len(ledger.human_queue(row["repo"])) if row["repo"] != "*" else None}
                    for row in ledger.all_repo_states()
                ],
                "unknown_commits": _unknown_status(ledger, reviewed),
                "supervised_commits": sorted(reviewed),
            }
            json.dump(report, sys.stdout, indent=2, sort_keys=True)
            print()
            return 0
        if args.command in {"pause", "resume"}:
            repo = "*" if args.all else args.repo
            if repo != "*" and repo not in registry:
                print(f"unknown repository: {repo}", file=sys.stderr)
                return 2
            # the state change and its signed publication happen under one
            # interprocess lock, so no concurrent refresh can publish a
            # pre-pause control record after the pause
            # (the publisher is the only merger: a pause needs nothing on GitHub)
            if args.command == "pause":
                generation = _outbox(state_dir, ledger).transition(
                    lambda: ledger.bump_generation(repo, pause=True, reason=args.reason))
            else:
                generation = _outbox(state_dir, ledger).transition(lambda: ledger.resume(repo))
            print(f"{args.command}d {repo}: generation {generation}")
            return 0
        if args.command == "control":
            _refresh(state_dir, ledger, registry)
            return 0
        if args.command == "accept-unknown":
            from .accept import AcceptError, request_accept

            try:
                sha = request_accept(ledger, args.sha, at=time.time())
            except AcceptError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(f"accept requested for {sha}: `kb serve` judges it through the gate on its next tick "
                  "(`kb status` and the human queue show the outcome)")
            return 0
        if args.command == "run":
            from ..config import Settings
            from .runner import run_playbook

            outcome, run_dir = run_playbook(Settings(), args.playbook, args.repo, state_dir=state_dir)
            print(f"{args.playbook} {args.repo}: {outcome.status} ({run_dir})")
            return 0 if outcome.status == "done" else 1
        if args.command in {"serve", "activate", "rollback"}:
            from ..config import Settings
            from .runtime import KbRuntime

            rt = KbRuntime.from_env(Settings(), state_dir=state_dir)
            if args.command == "serve":
                from .scheduler import Scheduler

                Scheduler(rt).serve(once=args.once)
                return 0
            from .activate import activate, rollback

            if args.command == "activate":
                print(activate(rt, rt.knowledge.fetch()))
            else:
                print(rollback(rt, args.to))
            return 0
        if args.command == "calibrate":
            from ..config import Settings
            from .calibration import run_calibration
            from .models import ModelGateway, roles_from_env

            lifecycle = registry.get(args.repo)
            if lifecycle is None or not lifecycle.calibration_set or lifecycle.adapter_dir is None:
                print(f"{args.repo} has no calibration_set", file=sys.stderr)
                return 2
            _generator, judge = roles_from_env()
            from .calibration import case_set_digest
            from ..trace_store import TraceStore, trace_context
            from .runtime import record_calibration, trace_recorder

            case_dir = lifecycle.adapter_dir / lifecycle.calibration_set
            gateway = ModelGateway(Settings(), recorder=trace_recorder(TraceStore(state_dir / "traces")))
            with trace_context(repo=args.repo):
                report = run_calibration(case_dir, gateway=gateway, judge=judge)
            # publication under auto_merge requires this record to match the
            # current judge and case set exactly
            record_calibration(ledger, args.repo, judge=judge.label(),
                               case_set=case_set_digest(case_dir), passed=report.passed, at=time.time())
            json.dump({"repo": args.repo, "judge": judge.label(), **report.to_dict()},
                      sys.stdout, indent=2, ensure_ascii=False)
            print()
            return 0 if report.passed else 1
    finally:
        ledger.close()
    return 2


def _publish(args) -> int:
    """The publisher never opens the service ledger: it only sees the outbox."""
    from ..knowledge_service.signing import load_private_key, load_public_key
    from .merge import knowledge_repository
    from .publisher import Gh, LocalTransport, Publisher, SshTransport

    for name in ("KB_SERVICE_PUBKEY", "KB_PUBLISHER_KEY", "KB_PUBLISHER_GIT_AUTHOR"):
        if not os.environ.get(name):
            raise SystemExit(f"{name} is not set")
    author = re.fullmatch(r"\s*(.+?)\s*<([^<>\s]+@[^<>\s]+)>\s*", os.environ["KB_PUBLISHER_GIT_AUTHOR"])
    if not author:
        raise SystemExit("KB_PUBLISHER_GIT_AUTHOR must look like 'Name <email>'")
    state = Path(args.publisher_state or os.environ.get("KB_PUBLISHER_STATE")
                 or Path.home() / ".infermatrix-copilot" / "kb-publisher").expanduser()
    state.mkdir(parents=True, exist_ok=True)
    registry = _registry()
    publisher = Publisher(
        transport=SshTransport.parse(args.remote) if args.remote else LocalTransport(Path(args.local)),
        service_public_key=load_public_key(Path(os.environ["KB_SERVICE_PUBKEY"]).read_text(encoding="utf-8")),
        publisher_key=load_private_key(os.environ["KB_PUBLISHER_KEY"]),
        github=Gh(knowledge_repository(), cwd=state),
        state_dir=state,
        author=(author.group(1), author.group(2)),
        repo_flags={name: (lc.publishes, lc.auto_merge) for name, lc in registry.items() if lc.enabled},
        upstreams={name: lc.full_name for name, lc in registry.items()
                   if lc.enabled and lc.publishes and lc.full_name},
        allow_post=os.environ.get("ALLOW_POST") == "1",
        allow_push=os.environ.get("ALLOW_PUSH") == "1",
    )
    if args.once:
        json.dump({**publisher.run_once(), **publisher.sync_archives()}, sys.stdout, sort_keys=True)
        print()
        return 0
    publisher.serve(interval=args.interval)
    return 0


def _traces_command(args, state_dir: Path) -> int:
    """Trace inspection, explicit replay, and offline archive finalization."""
    import zlib
    from ..trace_store import MigrationError, TraceStore

    store = TraceStore(state_dir / "traces")
    if args.command in {"reconcile-init-cost", "reconcile-init-review", "reconcile-foundation", "reconcile-discovery"}:
        from .init_recovery import reconcile_cost, reconcile_review, reconcile_foundation, reconcile_discovery
        from .init_support import InitError
        from .models import ModelUnavailable
        try:
            if args.command in {"reconcile-foundation", "reconcile-discovery"}:
                operation = reconcile_foundation if args.command == 'reconcile-foundation' else reconcile_discovery
                result = operation(state_dir, repo=args.repo, scope=(args.dispatch, args.attempt),
                                   offline_confirmed=args.offline_confirmed)
            else:
                operation = reconcile_review if args.command == "reconcile-init-review" else reconcile_cost
                result = operation(state_dir, repo=args.repo, stage=args.stage, trace_id=args.record,
                                   **({"outcome": args.outcome} if args.command == "reconcile-init-cost" else {}),
                                   offline_confirmed=args.offline_confirmed)
        except (InitError, ModelUnavailable, MigrationError, OSError, ValueError, KeyError, TypeError, AttributeError, EOFError, zlib.error) as exc:
            print(f"init reconciliation refused: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "recover-attempt":
        try:
            attempt = store.recover_native_attempt(args.attempt, args.record,
                                                   offline_confirmed=args.offline_confirmed, outcome=args.outcome)
        except (MigrationError, OSError, ValueError, KeyError, TypeError, AttributeError, EOFError, zlib.error) as exc:
            print(f"native archive recovery refused: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({key: attempt[key] for key in ("id", "trace_id", "status")}, sort_keys=True))
        return 0
    if args.command == "traces":
        records = store.query(kind=args.kind, changeset_id=args.changeset, rule_id=args.rule,
                              run_id=args.run, limit=args.limit)
        for record in records:
            print(json.dumps({k: record[k] for k in ("id", "kind", "at", "context", "model", "result", "error")},
                             ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "replay":
        from ..config import Settings
        from .models import ModelGateway, ModelRole
        from .replay import replay
        from .runtime import trace_recorder

        role_name = (store.get(args.record).get("model") or {}).get("role") or "judge"
        gateway = ModelGateway(Settings(), recorder=trace_recorder(store))  # the replay call is traced too
        result = replay(store, args.record, gateway, ModelRole.parse(role_name, args.model))
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        print()
        return 0
    from ..resources import adapters_root
    from .replay import export_dataset

    adapters = Path(os.environ.get("ADAPTERS_DIR") or adapters_root())
    calibration = [p for p in sorted(adapters.glob("*/kb-calibration")) if p.is_dir()]
    json.dump(export_dataset(store, args.out, role=args.role, calibration_dirs=calibration), sys.stdout)
    print()
    return 0
