"""CLI entry: argument parsing, the one-shot/REPL dispatch, and the built-in
command router. The orchestration lives in `Copilot` (copilot.py); this file is
the wiring that turns argv/stdin into calls on it.
"""

from __future__ import annotations

import argparse
import sys

from ..intent import parse_intents
from .copilot import Copilot
from .utils import parse_task_params


def _handle_line(copilot: Copilot, line: str, assume_yes: bool,
                 plan_only: bool, force_performance: bool = False,
                 oneshot: bool = False) -> int | None:
    """Route one input `line`: `/`-built-ins (status/logs/playbooks/resume/quit)
    are handled inline; anything else is parsed into one-or-more TaskSpecs and
    run as a queue. `force_performance` (the --performance flag) pins every
    resolved spec to the high-performance model tier, an explicit override on top
    of intent's eco-by-default detection. Returns None when nothing runs
    (built-in or blank/clarify), -1 to signal quit, or the queue's exit code."""
    line = line.strip()
    if not line:
        return None
    if line in ("/quit", "/exit", "exit", "quit"):
        return -1
    if line == "/status":
        print(copilot.status())
        return None
    if line.startswith("/logs"):
        parts = line.split()
        print(copilot.logs(int(parts[1]) if len(parts) > 1 else 20))
        return None
    if line == "/playbooks":
        print(copilot.playbooks())
        return None
    if line == "/resume":
        return copilot.resume_last()

    results = parse_intents(line, llm=copilot.llm,
                            default_repo=copilot.settings.default_repo,
                            model=copilot.settings.intent,
                            settings=copilot.settings)
    unclear = [r for r in results if r.needs_clarification]
    if unclear:
        print(f"? {unclear[0].clarify}")
        # headless one-shot (-p): a clarify is a FAILURE the caller must see —
        # exit 0 hid misroutes from scripts/eval harnesses
        from ..notify import BLOCKED_EXIT
        return BLOCKED_EXIT if oneshot else None
    specs = [r.spec for r in results]
    if force_performance:  # explicit --performance overrides eco-by-default
        for s in specs:
            s.mode = "performance"
    return copilot.run_queue(specs, assume_yes=assume_yes, plan_only=plan_only)


def _write_invocation_record(inv_dir, inv_id: str, copilot, rc: int) -> None:
    """Persist `<inv_id>.json` (W7): the pre-run llm spend (priced from this
    invocation's own trace via the canonical calculator) + the outcome, joined
    to any run by invocation_id — every paid attempt leaves an artifact even
    when no run dir was created (the clarify-retry case)."""
    import json as _json

    from ..metrics import cost_from_spans
    from ..notify import BLOCKED_EXIT

    pre = cost_from_spans(inv_dir / f"{inv_id}.trace.jsonl", copilot.settings)
    if copilot.last_run_dir:
        outcome = "run:" + copilot.last_run_dir.name
    elif rc == BLOCKED_EXIT:
        outcome = "blocked"  # incl. upfront clarify in one-shot mode
    else:
        outcome = "error" if rc else "no_run"
    try:
        (inv_dir / f"{inv_id}.json").write_text(_json.dumps({
            "invocation_id": inv_id, "outcome": outcome, "rc": rc,
            "pre_run": pre}, indent=1), encoding="utf-8")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Parses argv and dispatches: --resume / --playbook /
    --prompt run one-shot and return their exit code; otherwise start the
    interactive interface — the conversational chat REPL when an LLM is
    configured (and not --no-chat), else the plain command REPL. `argv` defaults
    to sys.argv. Returns the process exit code."""
    parser = argparse.ArgumentParser(prog="infermatrix-copilot",
                                     description="Conversational repo-maintenance copilot")
    parser.add_argument("command", nargs="?",
                        choices=["doctor", "migrate-knowledge", "rfc"],
                        help="doctor: preflight diagnostics with exact "
                             "fixes; migrate-knowledge: the PR4d "
                             "deployment-time knowledge migration "
                             "(explicit owner action; see "
                             "doc/RUNBOOK-rebase.md); rfc: portable RFC "
                             "drafting and progress service")
    parser.add_argument("--repo", default="",
                        help="with migrate-knowledge: target repo; "
                             "with --resume: assert the saved repo name")
    parser.add_argument("--dry-run", action="store_true",
                        help="with migrate-knowledge: report only, write "
                             "nothing else")
    parser.add_argument("--json", action="store_true",
                        help="with doctor: machine-readable output")
    parser.add_argument("--probe", action="store_true",
                        help="with doctor: 1-token live probe per model tier "
                             "(the only paid doctor check) — prints "
                             "requested→served and fails on a mismatch")
    parser.add_argument("-p", "--prompt", help="one-shot natural-language command")
    parser.add_argument("--yes", action="store_true",
                        help="skip confirmation prompts (headless)")
    parser.add_argument("--plan-only", action="store_true",
                        help="resolve and print the plan without executing")
    parser.add_argument("--resume", nargs="?", const="", default=None,
                        metavar="RUN_ID",
                        help="resume RUN_ID, or the most recent run when omitted; "
                             "repo/playbook/task params assert its saved identity")
    parser.add_argument("--playbook",
                        help="run a specific playbook by name "
                             "(incl. planner-invisible candidates); "
                             "with --resume: assert the saved playbook")
    parser.add_argument("--report-only", action="store_true",
                        help="with --playbook: read-only variant of the task")
    parser.add_argument("--task-param", action="append", default=[],
                        metavar="KEY=VALUE",
                        help="with --playbook: task param; with --resume: "
                             "assert a saved task param (repeatable), "
                             "e.g. --task-param local_ci_only=true")
    parser.add_argument("--no-chat", action="store_true",
                        help="use the plain command REPL instead of the "
                             "conversational interface")
    parser.add_argument("--performance", action="store_true",
                        help="use the high-performance model tier for this run "
                             "(default: eco / cost-effective)")
    # MCP-only internal entry: execute a run previously reserved by the MCP
    # server, in this fresh subprocess. Hidden from --help.
    parser.add_argument("--execute-reserved", metavar="RUN_ID",
                        help=argparse.SUPPRESS)
    parser.add_argument("--execute-strict-reserved", metavar="RUN_ID",
                        help=argparse.SUPPRESS)
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "rfc":
        from ..rfc_service.cli import main as rfc_main

        return rfc_main(raw[1:])
    if raw and raw[0] == "kb":  # the knowledge service has its own subcommands
        from ..kb_service.cli import main as kb_main

        return kb_main(raw[1:])
    if raw and raw[0] == "improve":  # the meta-improvement engine's commands
        from ..improve.cli import main as improve_main

        return improve_main(raw[1:])
    args = parser.parse_args(argv)

    if args.command == "doctor":  # diagnostics only — no Copilot/LLM spin-up
        from .doctor import run_doctor

        return run_doctor(as_json=args.json, probe=args.probe)

    if args.command == "migrate-knowledge":  # explicit owner action, no LLM
        if not args.repo:
            print("migrate-knowledge needs --repo <name>")
            return 2
        from ..config import Settings
        from ..rebase_engine.knowledge_migrate import (MigrationError,
                                                       migrate_knowledge)
        try:
            report = migrate_knowledge(Settings(), args.repo,
                                       dry_run=args.dry_run)
        except MigrationError as exc:
            print(f"✋ migration refused: {exc}")
            return 3
        print(f"{'DRY RUN — ' if args.dry_run else ''}migration report: "
              f"{report.ingested} ingested, "
              f"{report.skipped_unchanged} unchanged, "
              f"{report.reversioned} re-versioned, "
              f"{report.retired_by_dedup} retired by dedup, "
              f"{len(report.skills_added)} seed skills added, "
              f"{report.candidates_merged} candidates merged — see "
              "MIGRATION_REPORT.md in the state dir"
              + ("" if args.dry_run else
                 "; next: review the adapter git diff, then activate via "
                 "IMX_KNOWLEDGE_RUNTIME (RUNBOOK)"))
        return 0

    # per-invocation accounting (W7): intent parsing runs BEFORE any run dir
    # exists, so pre-run spend (clarify retries!) needs its own artifact. The
    # id is honored from env so eval harnesses can stamp each attempt and
    # correlate exactly; the run later re-points tracing at its own dir, so
    # this trace holds exactly the pre-run llm spans.
    import os
    import uuid as _uuid
    from pathlib import Path as _Path

    from .. import tracing as _tracing

    inv_id = os.environ.get("IMX_INVOCATION_ID") or f"inv-{_uuid.uuid4().hex[:8]}"
    inv_dir = _Path.home() / ".infermatrix-copilot" / "invocations"
    inv_dir.mkdir(parents=True, exist_ok=True)
    _tracing.init(inv_id, inv_dir / f"{inv_id}.trace.jsonl")
    os.environ["IMX_INVOCATION_ID"] = inv_id  # visible to the run subprocess/task

    copilot = Copilot()

    if args.execute_reserved:
        return copilot.execute_reserved(args.execute_reserved)
    if args.execute_strict_reserved:
        return copilot.execute_strict_reserved(args.execute_strict_reserved)
    if args.resume is not None:
        return copilot.resume_last(args.resume, repo=args.repo,
                                   playbook=args.playbook or "",
                                   params=parse_task_params(args.task_param))
    if args.playbook:
        params = parse_task_params(args.task_param)
        return copilot.run_playbook(args.playbook, params=params,
                                    report_only=args.report_only,
                                    assume_yes=args.yes, plan_only=args.plan_only)
    if args.prompt:
        code = _handle_line(copilot, args.prompt, args.yes, args.plan_only,
                            force_performance=args.performance, oneshot=True)
        rc = int(code) if code not in (None, -1) else 0
        _write_invocation_record(inv_dir, inv_id, copilot, rc)
        return rc

    # Interactive: conversational chat (Claude-Code-style) when an LLM is
    # configured; plain command REPL otherwise / with --no-chat.
    if copilot.llm.available and not args.no_chat:
        from ..chat import chat_repl

        return chat_repl(
            copilot, assume_yes=args.yes,
            handle_builtin=lambda line: _handle_line(copilot, line, args.yes,
                                                     args.plan_only,
                                                     force_performance=args.performance),
        )

    print("infermatrix-copilot — natural-language repo maintenance. "
          "/status /logs /playbooks /resume /quit")
    while True:
        try:
            line = input("copilot> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        code = _handle_line(copilot, line, args.yes, args.plan_only,
                            force_performance=args.performance)
        if code == -1:
            return 0
