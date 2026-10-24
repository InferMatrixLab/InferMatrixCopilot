"""The `Copilot` orchestrator: NL-resolved TaskSpec → plan (reuse > adapt >
generate) → plan-review gate → executor, plus the compound-command queue,
resume, and the /status /logs /playbooks built-ins.

This is the orchestration core; the argparse/REPL wiring lives in `entry.py`,
and executor lifecycle lives in `app.workflow_execution`.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import NamedTuple

from .. import run_status as rs
from ..adapters.base import AdapterError
from ..config import Settings, TierNotConfiguredError
from ..engine.lifecycle import RunLock, RunLockHeld
from ..engine.planner import Planner, PlanningError, Resolution
from ..engine.registry import StepRegistry
from ..engine.steps import register_builtin_steps
from ..notify import BLOCKED_EXIT
from ..playbooks.store import PlaybookStore, parse_playbook, playbook_to_doc
from ..review.plan_gate import _mode_review_context, review_plan_gate
from ..review.reviewer import run_plan_review
from ..task_spec import TaskSpec
from ..ui import style
from .reservation import RunReservation
from .repository_context import RepositoryContext, RepositoryContextResolver
from .workflow_execution import WorkflowExecution


class GateOutcome(NamedTuple):
    """Result of the pre-execution gates. `proceed` says whether to run; when
    False, `exit_code` is the process code the caller should return. Read at
    the call site as: ``if not gate.proceed: return gate.exit_code``."""

    proceed: bool
    exit_code: int = 0

    @classmethod
    def go(cls) -> "GateOutcome":
        """Gates passed — run the task."""
        return cls(proceed=True)

    @classmethod
    def stop(cls, exit_code: int) -> "GateOutcome":
        """A gate halted the run — return this exit code."""
        return cls(proceed=False, exit_code=exit_code)


class Copilot:
    """Orchestration core: resolves a TaskSpec to a playbook (reuse > adapt >
    generate), runs it through the plan-review gate + confirmation into the
    Executor, and owns the compound-command queue, resume, and the /status
    /logs /playbooks built-ins. Holds the long-lived collaborators (LLM,
    step registry, playbook store, planner) and tracks `last_run_dir` for the
    built-ins."""

    def __init__(self, settings: Settings | None = None):
        """Wire up the collaborators from `settings` (default `Settings()`):
        the LLM, the built-in step registry, the playbook store rooted at the
        configured dir, and the planner over both. `last_run_dir` starts unset
        and is filled by the first execution."""
        self.settings = settings or Settings()
        self.reservations = RunReservation(self.settings)
        self.repository_context = RepositoryContextResolver(self.settings)
        # provider-registry seam: a real llm.LLM under backend "api"
        # (byte-identical), a HarnessLLM adapter under a harness backend
        from ..providers import llm_for

        self.llm = llm_for(self.settings)
        self.registry = register_builtin_steps(StepRegistry())
        self.store = PlaybookStore(self.settings.playbooks_dir, self.registry)
        self.planner = Planner(self.store, self.registry)
        self.workflow_execution = WorkflowExecution(
            self.settings, self.registry, self.repository_context,
        )
        self.last_run_dir: Path | None = None
        # why the last execution stopped, when it stopped blocked — read by
        # `_execute_reserved_locked`, which owns the terminal run_status write
        self.last_blocked_reason: str = ""

    # -- planning ---------------------------------------------------------------
    def resolve(self, spec: TaskSpec) -> Resolution:
        """Resolve `spec` to a Resolution via the planner, passing the repo's
        capability set so the planner only reuses playbooks the target supports.
        Capabilities come from the repo's adapter (if any), plus `repo.path` when
        a path is resolvable even without a adapter (REPO_PATHS works adapter-less)."""
        return self._resolve_with_context(spec)[0]

    def _resolve_with_context(
        self, spec: TaskSpec,
    ) -> tuple[Resolution, RepositoryContext]:
        context = self.repository_context.for_spec(spec)
        return self.planner.resolve(
            spec, capabilities=context.capabilities,
        ), context

    def _plan_review_gate(self, resolution: Resolution, spec: TaskSpec,
                          assume_yes: bool) -> bool:
        """Apply the reviewer verdict policy before human confirmation."""
        result = review_plan_gate(
            resolution, spec, self.llm, self.settings.reviewer, assume_yes,
            review_fn=run_plan_review,
        )
        for notice in result.notices:
            print(notice)
        return result.allowed

    def _gate_and_confirm(self, resolution: Resolution, spec: TaskSpec,
                          assume_yes: bool, *, prompt: str = "Proceed?",
                          force_confirm: bool = False) -> GateOutcome:
        """Plan-review gate + [y/N] confirm (concision K6). Returns
        `GateOutcome.go()` to run, or `GateOutcome.stop(code)` to halt with a
        process exit code (BLOCKED_EXIT on a reviewer block, 1 on a user
        abort). Confirm fires for confirm_required or a review-requiring/
        explicit plan, unless assume_yes."""
        if not self._plan_review_gate(resolution, spec, assume_yes):
            return GateOutcome.stop(BLOCKED_EXIT)  # reviewer blocked the plan
        need = force_confirm or spec.confirm_required or resolution.requires_review
        if need and not assume_yes:
            if input(f"{prompt} [y/N] ").strip().lower() not in ("y", "yes"):
                print("aborted.")
                return GateOutcome.stop(1)  # user declined
        return GateOutcome.go()

    # -- execution -----------------------------------------------------------------
    def run_task(self, spec: TaskSpec, *, assume_yes: bool = False,
                 plan_only: bool = False) -> int:
        """Full path for one task: resolve → print plan → gate/confirm →
        persist task.json → execute. Returns a process exit code — 0 done,
        1 failed/aborted, BLOCKED_EXIT when planning fails or a gate blocks.
        `plan_only` prints the resolved plan and returns 0 without running;
        `assume_yes` skips the interactive confirm."""
        try:
            resolution, repo_context = self._resolve_with_context(spec)
        except (PlanningError, AdapterError) as exc:
            print(style("✋ cannot plan: ", "red", "bold") + str(exc))
            return BLOCKED_EXIT
        # tier preflight (plan v2): backend availability is DEPLOYMENT state,
        # so it is enforced here at resolution — before the echo, the gate,
        # and the run dir — never discovered as a BLOCKED step minutes in.
        try:
            tier_target = self.settings.tier_target(spec.mode)
        except TierNotConfiguredError as exc:
            print(style("✋ cannot run: ", "red", "bold") + str(exc))
            return BLOCKED_EXIT

        # Mode governance (Rev 8 §2.1): for mode-aware playbooks the ONE
        # authority is params.rebase_mode, resolved + WRITTEN BACK before the
        # plan echo and the confirmation gate (spec.report_only reflects the
        # canonical mode, so every TaskSpec-derived consumer sees one truth).
        # The locked delegating playbook does not declare mode_aware and is
        # untouched — byte-identical behavior.
        if getattr(resolution.playbook, "mode_aware", False):
            from ..rebase_engine.modes import (ModeConflictError,
                                               resolve_effective_mode)
            try:
                resolve_effective_mode(spec)
            except ModeConflictError as exc:
                print(style("✋ blocked: ", "red", "bold") + str(exc))
                return BLOCKED_EXIT

        print(style("→ task: ", "bold", "cyan") + spec.describe())
        print(style("→ plan: ", "bold", "magenta") + f"{resolution.mode} {resolution.playbook.name}"
              f"@{resolution.playbook.version} ({resolution.playbook.status}) "
              f"steps={[s.step for s in resolution.playbook.steps]}")
        for note in resolution.notes:
            print(f"  · {note}")
        print(style("→ path: ", "bold", "cyan")
              + f"{spec.mode} · agent-model={tier_target.model}"
                f" @ {tier_target.host}")
        if plan_only:
            return 0

        gate = self._gate_and_confirm(resolution, spec, assume_yes)
        if not gate.proceed:
            return gate.exit_code

        run_id = (f"run-{time.strftime('%Y%m%d-%H%M%S')}"
                  f"-{uuid.uuid4().hex[:6]}")  # unique — same-second runs collided
        run_dir = self.settings.run_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "task.json").write_text(json.dumps({
            "spec": spec.model_dump(), "playbook": playbook_to_doc(resolution.playbook),
            "invocation_id": os.environ.get("IMX_INVOCATION_ID", ""),
        }, indent=2))
        return self._execute(resolution.playbook, spec, run_dir,
                             resolution_mode=resolution.mode, tier=resolution.tier,
                             planned_context=repo_context)

    def run_playbook(self, name: str, *, params: dict | None = None,
                     report_only: bool = False, assume_yes: bool = False,
                     plan_only: bool = False) -> int:
        """Explicit playbook override — the only way to execute a CANDIDATE
        (candidates stay planner-invisible). Always treated as requiring
        review + confirmation."""
        playbook = self.store.get(name)
        if playbook is None:
            print(f"✋ no playbook named {name!r} (see /playbooks)")
            return BLOCKED_EXIT
        kind = playbook.task_kinds[0]
        # lift target params into first-class spec fields so the explicit
        # playbook path can address a PR/issue/repo like every other surface
        params = dict(params or {})
        pr = params.pop("pr", None)
        issue = params.pop("issue", None)
        repo = str(params.pop("repo", "") or self.settings.default_repo)
        try:
            pr = int(pr) if pr is not None else None
            issue = int(issue) if issue is not None else None
        except (TypeError, ValueError):
            print("✋ pr/issue task params must be integers")
            return BLOCKED_EXIT
        if repo not in (self.settings.repo_paths or {repo: ""}):
            print(f"✋ unknown repo alias {repo!r} "
                  f"(known: {', '.join(sorted(self.settings.repo_paths or {}))})")
            return BLOCKED_EXIT
        spec = TaskSpec(kind=kind, repo=repo, pr=pr, issue=issue,
                        report_only=report_only, params=params)
        from ..intent import validate_spec
        err = validate_spec(spec)
        if err:
            print(f"✋ {err} (pass e.g. --task-param pr=5134)")
            return BLOCKED_EXIT
        # mode governance applies to explicit invocations too — the ONLY
        # way to run the candidate v3/v1 playbooks today (Rev 8 §2.1)
        if getattr(playbook, "mode_aware", False):
            from ..rebase_engine.modes import (ModeConflictError,
                                               resolve_effective_mode)
            try:
                resolve_effective_mode(spec)
            except ModeConflictError as exc:
                print(style("✋ blocked: ", "red", "bold") + str(exc))
                return BLOCKED_EXIT
        print(style("→ task: ", "bold", "cyan") + spec.describe()
              + style("  [explicit playbook override]", "yellow"))
        print(style("→ plan: ", "bold", "magenta") + f"explicit {playbook.name}@{playbook.version} "
              f"({playbook.status}) steps={[s.step for s in playbook.steps]}")
        if plan_only:
            return 0
        resolution = Resolution(mode="explicit", playbook=playbook,
                                tier=spec.tier, requires_review=True)
        gate = self._gate_and_confirm(
            resolution, spec, assume_yes, force_confirm=True,
            prompt=f"Run {playbook.status} playbook '{name}'?")
        if not gate.proceed:
            return gate.exit_code
        run_id = (f"run-{time.strftime('%Y%m%d-%H%M%S')}"
                  f"-{uuid.uuid4().hex[:6]}")  # unique — same-second runs collided
        run_dir = self.settings.run_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "task.json").write_text(json.dumps({
            "spec": spec.model_dump(), "playbook": playbook_to_doc(playbook),
            "invocation_id": os.environ.get("IMX_INVOCATION_ID", ""),
        }, indent=2))
        return self._execute(playbook, spec, run_dir,
                             resolution_mode="explicit", tier=spec.tier)

    def _execute(self, playbook, spec: TaskSpec, run_dir: Path, *,
                 resolution_mode: str = "resume", tier: str = "?",
                 resuming: bool = False, held_lock: RunLock | None = None,
                 planned_context: RepositoryContext | None = None) -> int:
        """Compatibility entry point; application keeps the last-run summary."""
        self.last_run_dir = run_dir
        self.last_blocked_reason = ""
        result = self.workflow_execution.run(
            playbook, spec, run_dir, resolution_mode=resolution_mode,
            tier=tier, resuming=resuming, held_lock=held_lock,
            planned_context=planned_context, llm=self.llm,
        )
        self.last_blocked_reason = result.blocked_reason
        return result.exit_code

    def run_queue(self, specs: list[TaskSpec], *, assume_yes: bool = False,
                  plan_only: bool = False) -> int:
        """Ordered task queue for compound commands; stops on failure/blocked."""
        if len(specs) > 1:
            print(f"⧉ queued {len(specs)} tasks:")
            for i, s in enumerate(specs, 1):
                print(f"  {i}. {s.describe()}")
        for i, spec in enumerate(specs, 1):
            if len(specs) > 1:
                print(f"\n── task {i}/{len(specs)} ──")
            code = self.run_task(spec, assume_yes=assume_yes, plan_only=plan_only)
            if code != 0:
                if i < len(specs):
                    print(f"⏸ queue stopped: {len(specs) - i} task(s) not run")
                return code
        return 0

    def resume_last(self, run_id: str = "", *, repo: str = "",
                    playbook: str = "", params: dict | None = None) -> int:
        """Resume a selected run, or the latest run when no id is supplied.

        Target arguments are assertions against the saved task, never edits to
        it. Release launchers can therefore bind resume to an immutable campaign
        without accidentally entering a newer, unrelated task.
        """
        if run_id:
            try:
                runs = [self._contained_run_dir(run_id)]
            except (OSError, ValueError, RuntimeError) as exc:
                print(f"✋ cannot resume: {exc}")
                return BLOCKED_EXIT
        else:
            runs = sorted(self.settings.run_root.glob("run-*"), reverse=True) \
                if self.settings.run_root.exists() else []
        for run_dir in runs:
            task_file = run_dir / "task.json"
            if not task_file.exists():
                if run_id:
                    print(f"✋ cannot resume {run_id}: task.json is missing")
                    return BLOCKED_EXIT
                continue
            try:
                saved = json.loads(task_file.read_text(encoding="utf-8"))
                spec = TaskSpec(**saved["spec"])
                saved_playbook = parse_playbook(saved["playbook"], str(task_file))
            except (OSError, UnicodeError, ValueError, KeyError, TypeError,
                    AttributeError) as exc:
                print(f"✋ cannot resume {run_dir.name}: invalid task.json ({exc})")
                return BLOCKED_EXIT
            mismatches = []
            if repo and repo != spec.repo:
                mismatches.append("repo")
            if playbook and playbook != saved_playbook.name:
                mismatches.append("playbook")
            for key, expected in (params or {}).items():
                if key not in spec.params or (
                    json.dumps(spec.params[key], sort_keys=True)
                    != json.dumps(expected, sort_keys=True)
                ):
                    mismatches.append(f"task param {key!r}")
            if mismatches:
                print(f"✋ cannot resume {run_dir.name}: saved task does not match "
                      + ", ".join(mismatches)
                      + "; resume preserves the recorded task")
                return BLOCKED_EXIT
            try:  # tier preflight holds on re-entry too (deployment state
                # may have changed since the run was created)
                self.settings.tier_target(spec.mode)
            except TierNotConfiguredError as exc:
                print(style("✋ cannot resume: ", "red", "bold") + str(exc))
                return BLOCKED_EXIT
            print(f"↻ resuming {run_dir.name}: {spec.describe()}")
            return self._execute(saved_playbook, spec, run_dir, resuming=True)
        print("no resumable run found")
        return 1

    # -- MCP child execution and compatibility reservation facade -----------
    @staticmethod
    def _new_run_id() -> str:
        return RunReservation.new_run_id()

    def _contained_run_dir(self, run_id: str, *, must_exist: bool = True) -> Path:
        return self.reservations.contained_run_dir(run_id, must_exist=must_exist)

    def reserve_run(self, spec: TaskSpec, *, owner_server_id: str,
                    owner_server_pid: int,
                    idempotency_key: str = "") -> tuple[str, bool]:
        """Compatibility entry for callers that still hold a ``Copilot``."""
        return self.reservations.reserve(
            spec, owner_server_id=owner_server_id,
            owner_server_pid=owner_server_pid,
            idempotency_key=idempotency_key,
        )

    def execute_reserved(self, run_id: str) -> int:
        """MCP-only child entry (subprocess, stdout -> console.log). Writes its
        own pid FIRST (single-writer invariant), **re-enforces** the read-only
        MCP policy on the persisted request (request.json is untrusted — a host
        could have rewritten it), plans, then executes, driving
        `run_status.json` planning -> running -> terminal. Returns the exit code."""
        from .request_policy import enforce_mcp_policy

        return self._execute_reserved(run_id, enforce_mcp_policy)

    def execute_strict_reserved(self, run_id: str) -> int:
        """Execute a reserved Strict review using the previous Eco workflow."""
        from .request_policy import enforce_strict_review_policy

        return self._execute_reserved(run_id, enforce_strict_review_policy)

    def _execute_reserved(self, run_id: str, policy) -> int:
        """Shared reserved-run executor with an authoritative child policy."""
        run_dir = self._contained_run_dir(run_id)
        try:
            # Lock the whole reserved-run lifecycle before the first status
            # write: a duplicate child must not overwrite child_pid, flip
            # run_status.json through PLANNING, or mark the active run
            # BLOCKED while the winning process is still executing.
            lock = RunLock(run_dir).acquire()
        except RunLockHeld as exc:
            print(style("✋ ", "red", "bold") + str(exc))
            return BLOCKED_EXIT
        try:
            return self._execute_reserved_locked(run_dir, lock, policy)
        finally:
            lock.release()

    def _execute_reserved_locked(self, run_dir: Path, lock: RunLock,
                                 policy) -> int:
        """The body of `_execute_reserved`, run while holding the run lock."""
        from .request_policy import PolicyError

        # The claim, not the lock, is what makes execution at-most-once.
        # `RunLock` excludes concurrent children; it says nothing about a second
        # child that arrives after the first finished and released. A losing
        # child exits here without planning, executing, or writing status — it
        # is a duplicate, not a failure.
        if not rs.claim_for_execution(run_dir, child_pid=os.getpid()):
            state = (rs.read_status(run_dir) or {}).get("state", "?")
            print(style("↷ ", "yellow") +
                  f"run {run_dir.name} is already claimed (state={state}) — "
                  "not executing it twice")
            return 0
        try:
            raw = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
            spec = policy(
                raw, allowed_repos=self.settings.mcp_allowed_repos,
                settings=self.settings)
        except (PolicyError, OSError, json.JSONDecodeError, ValueError) as exc:
            rs.mark(run_dir, rs.FAILED, note=f"policy/request rejected: {exc}")
            return 1
        try:
            resolution, repo_context = self._resolve_with_context(spec)
        except (PlanningError, AdapterError) as exc:
            rs.mark(run_dir, rs.BLOCKED, note=f"cannot plan: {exc}")
            return BLOCKED_EXIT
        try:  # tier preflight is re-enforced in the MCP child (authoritative)
            self.settings.tier_target(spec.mode)
        except TierNotConfiguredError as exc:
            rs.mark(run_dir, rs.BLOCKED, note=f"cannot run: {exc}")
            return BLOCKED_EXIT
        (run_dir / "task.json").write_text(json.dumps({
            "spec": spec.model_dump(),
            "playbook": playbook_to_doc(resolution.playbook),
        }, indent=2))
        rs.mark(run_dir, rs.RUNNING)
        try:
            code = self._execute(resolution.playbook, spec, run_dir,
                                 resolution_mode=resolution.mode, tier=resolution.tier,
                                 held_lock=lock, planned_context=repo_context)
        except Exception as exc:  # a crash still leaves a terminal record
            rs.mark(run_dir, rs.FAILED, note=f"{type(exc).__name__}: {exc}")
            raise
        # Carry WHY it stopped into the terminal record. An MCP client only ever
        # sees `run_status.json`, and a bare `blocked` with no note gives it
        # nothing to distinguish a stale-head refusal from a missing tier or a
        # failed checkout — the reason was previously printed to the child's
        # console and discarded.
        rs.mark(run_dir, {0: rs.DONE, BLOCKED_EXIT: rs.BLOCKED}.get(code, rs.FAILED),
                note=self.last_blocked_reason if code == BLOCKED_EXIT else "")
        return code

    def _adapter_for(self, repo: str):
        """Compatibility lookup; a broken known adapter remains an error."""
        return self.repository_context.adapter_for(repo)

    def _repo_path_for(self, spec: TaskSpec) -> str:
        """The checkout THIS spec runs against: its frozen `repo_path` first,
        else the ambient resolution by alias.

        Every place a run learns its checkout must agree. Resolution derives the
        `repo.path` capability from this, and `_execute` seeds `state` from it:
        with an explicit path but no ambient `REPO_PATHS` and no adapter
        `repo.path`, an alias-only lookup would drop the capability and fail to
        resolve a playbook that requires it — while a perfectly valid checkout
        sat in the spec. With both configured and differing, planning would
        evaluate one checkout and execution would run against another."""
        return self.repository_context.repo_path_for(spec)

    def _resolve_repo_path(self, repo: str) -> str:
        """REPO_PATHS first; fall back to the repo's adapter manifest (adapter zero
        declares repo.path), so runs work even without a .env in reach.

        Alias-only: callers that hold a TaskSpec want `_repo_path_for`, which
        honors a frozen per-run path."""
        return self.repository_context.ambient_repo_path(repo)

    # -- built-ins ---------------------------------------------------------------
    def status(self) -> str:
        """Human-readable status of the current (or most recent) run: completed
        steps from progress.json, plus a rebase-phase line (module/test counts,
        CI result) when rebase_status.json exists. Falls back to the newest
        run-* dir when no run has executed this session."""
        if not self.last_run_dir:
            runs = sorted(self.settings.run_root.glob("run-*")) \
                if self.settings.run_root.exists() else []
            if not runs:
                return "no runs yet"
            self.last_run_dir = runs[-1]
        progress = self.last_run_dir / "progress.json"
        lines = []
        if progress.exists():
            done = list(json.loads(progress.read_text(encoding="utf-8")).get("completed", {}))
            lines.append(f"{self.last_run_dir.name}: completed steps: {done}")
        else:
            lines.append(f"{self.last_run_dir.name}: no progress recorded")
        rebase_status = self.last_run_dir / "rebase_status.json"
        if rebase_status.exists():
            s = json.loads(rebase_status.read_text(encoding="utf-8"))
            mods = s.get("modules", {})
            tests = s.get("tests", {})
            lines.append(
                f"  rebase: phase={s.get('phase')} modules(done={mods.get('done', 0)} "
                f"failed={mods.get('failed', 0)}) tests(completed={tests.get('completed', 0)} "
                f"failed={len(tests.get('failed', []))}"
                + (f" current={tests.get('current')}" if tests.get("current") else "")
                + f") ci={s.get('ci_result') or '-'}"
            )
        return "\n".join(lines)

    def logs(self, n: int = 20) -> str:
        """Return the last `n` lines of the current run's run_trace.jsonl, or a
        placeholder string when there is no run / no trace yet."""
        if not self.last_run_dir:
            return "no runs yet"
        tracefile = self.last_run_dir / "run_trace.jsonl"
        if not tracefile.exists():
            return "no trace"
        return "".join(tracefile.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-n:])

    def playbooks(self) -> str:
        """One line per registered playbook (name@version, status, task kinds),
        or "(none)" when the store is empty."""
        return "\n".join(
            f"{p.name}@{p.version} [{p.status}] kinds={p.task_kinds}"
            for p in self.store.all()
        ) or "(none)"
