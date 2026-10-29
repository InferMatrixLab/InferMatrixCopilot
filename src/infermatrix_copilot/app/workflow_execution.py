"""Execute an already selected workflow with run and knowledge locks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ..adapters.base import AdapterError
from ..config import Settings
from ..engine.executor import Executor
from ..engine.lifecycle import (RunLock, RunLockHeld,
                                install_run_signal_handlers, run_guarded,
                                shutdown_requested)
from ..engine.registry import StepRegistry
from ..metrics import format_metrics_line
from ..notify import BLOCKED_EXIT, Notifier
from ..push import PushPolicy
from ..run_trace import RunTrace
from ..task_spec import TaskSpec
from ..ui import style
from .repository_context import RepositoryContext, RepositoryContextResolver


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    blocked_reason: str = ""


class WorkflowExecution:
    """Own the executor, locks, tracing and terminal outcome for one run."""

    def __init__(
        self, settings: Settings, registry: StepRegistry,
        repository_context: RepositoryContextResolver,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.repository_context = repository_context

    def run(
        self, playbook, spec: TaskSpec, run_dir: Path, *, llm,
        resolution_mode: str = "resume", tier: str = "?",
        resuming: bool = False, held_lock: RunLock | None = None,
        planned_context: RepositoryContext | None = None,
        rerun_from: str = "",
    ) -> ExecutionResult:
        """Run a resolved `playbook` to completion in `run_dir`: init tracing +
        notifier, seed the shared state (repo path, push policy, protected
        branches / high-risk modules from the adapter when present), drive the
        Executor, then print per-step marks, optional run metrics, and the final
        status. `resuming` seeds the state so steps can pick up where they left
        off. Returns the exit code and blocked reason to the application, which owns
        terminal `run_status.json` writes for reserved runs."""
        blocked_reason = ""
        try:
            repo_context = self.repository_context.for_spec(spec)
        except AdapterError as exc:
            blocked_reason = f"repository context invalid: {exc}"
            print(style("✋ ", "red", "bold") + blocked_reason)
            return ExecutionResult(BLOCKED_EXIT, blocked_reason)
        if planned_context is not None and repo_context != planned_context:
            blocked_reason = "repository context changed after planning"
            print(style("✋ ", "red", "bold") + blocked_reason)
            return ExecutionResult(BLOCKED_EXIT, blocked_reason)
        lock = held_lock
        if lock is None:
            try:
                # First, before any trace/status write: a losing concurrent
                # invocation (second --resume) must leave the active run's
                # artifacts completely untouched. execute_reserved holds the
                # lock across its whole lifecycle and passes it in instead.
                lock = RunLock(run_dir).acquire()
            except RunLockHeld as exc:
                print(style("✋ ", "red", "bold") + str(exc))
                return ExecutionResult(BLOCKED_EXIT, blocked_reason)
        # Per-repo knowledge lock, SHARED, held for the run's lifetime:
        # never contends with other runs; it exists so the knowledge
        # migration's EXCLUSIVE acquire can prove no potential store
        # writer is alive (and so no run starts mid-migration).
        knowledge_lock = None
        if spec.repo:
            from ..memory.paths import (KnowledgeLockHeld,
                                        KnowledgePaths,
                                        KnowledgeRunLock,
                                        KnowledgeStateError)
            try:
                knowledge_lock = KnowledgeRunLock(
                    KnowledgePaths.resolve(self.settings, spec.repo)
                    .knowledge_run_lock).acquire_shared()
            except (KnowledgeLockHeld, KnowledgeStateError) as exc:
                # BOTH refusals take the terminal protocol: an invalid
                # activation marker (KnowledgeStateError) must exit
                # blocked/3 with the run lock RELEASED, never escape as
                # a traceback that leaves the lock held in a long-lived
                # process (PR-boundary F4)
                if held_lock is None:
                    lock.release()
                print(style("✋ ", "red", "bold") + str(exc))
                return ExecutionResult(BLOCKED_EXIT, blocked_reason)
        try:
            from .. import tracing
            tracing.init(run_dir.name, run_dir / "trace.jsonl")
            # Stamp the workflow into the span file itself, so a trace lifted out of
            # its run directory still says which playbook and task produced it.
            tracing.run_meta(playbook=f"{playbook.name}@{playbook.version}",
                             task_kind=spec.kind, repo=spec.repo, tier=tier,
                             mode=spec.mode, resolution=resolution_mode,
                             report_only=spec.report_only, post=spec.post,
                             resuming=resuming, params=spec.params)
            trace = RunTrace(run_dir / "run_trace.jsonl")
            notifier = Notifier(self.settings, run_dir, trace, run_dir.name)
            trace.record("task", spec=spec.model_dump(), resolution=resolution_mode,
                         playbook=playbook.name, tier=tier)
            state: dict = {
                "task_spec": spec.model_dump(),
                "repo_path": repo_context.repo_path,
                "push_policy": PushPolicy(),  # steps may replace with a derived policy
                "protected_branches": list(repo_context.protected_branches),
                "resuming": resuming,
            }
            if rerun_from:
                state["rerun_from"] = rerun_from
            if repo_context.high_risk_modules:
                state["high_risk_modules"] = list(repo_context.high_risk_modules)
            executor = Executor(self.registry, self.settings, run_dir=run_dir,
                                trace=trace, llm=llm, notifier=notifier)
            # run_guarded finalizes inside the event loop: playbooks that
            # register run finalizers (lifecycle.register_finalizer) get
            # teardown on every exit path. Nothing registered == no-op.
            # SIGINT/SIGTERM (a GPU scheduler ending the reservation, an
            # operator) become a bounded graceful shutdown: harness children
            # and marked background processes are stopped, the run task is
            # cancelled through the same finalizers, and the run is left
            # resumable and marked `interrupted` — never `running`.
            restore = install_run_signal_handlers(run_dir, run_dir.name)
            try:
                outcome = asyncio.run(
                    run_guarded(executor.run(playbook, state), run_dir))
            except (asyncio.CancelledError, KeyboardInterrupt):
                req = shutdown_requested()
                if req is None:
                    raise
                return self._interrupted(run_dir, trace, req)
            finally:
                restore()
            req = shutdown_requested()
            if req is not None:
                # the cooperative path: a step noticed the request and
                # returned blocked before cancellation reached it
                return self._interrupted(run_dir, trace, req, outcome)

            if outcome.status == "done":
                notifier.resolve()

            if self.settings.metrics_enabled:
                try:  # metrics are facts about the run; never let them break it
                    from ..metrics import collect_run_metrics
                    m = collect_run_metrics(run_dir, self.settings, outcome.status)
                    print(format_metrics_line(m, run_dir))
                except Exception as exc:
                    trace.record("metrics_error", error=f"{type(exc).__name__}: {exc}")

            for step_id, r in outcome.step_results.items():
                mark = style("✓", "green") if r.ok else style("✗", "red", "bold")
                print(f"  {mark} {step_id}: {r.summary}")
            print(f"run {run_dir.name}: {outcome.status}  ({run_dir})")
            if outcome.status == "blocked":
                blocked_reason = str(outcome.blocked_reason or "")
                print(style("  ⚠ ", "yellow", "bold") + f"{outcome.blocked_reason}\n  see {run_dir / 'ESCALATION.md'}")
                return ExecutionResult(BLOCKED_EXIT, blocked_reason)
            return ExecutionResult(0 if outcome.status == "done" else 1, blocked_reason)
        finally:
            if knowledge_lock is not None:
                knowledge_lock.release()
            if held_lock is None:
                lock.release()

    @staticmethod
    def _interrupted(run_dir: Path, trace: RunTrace, req,
                     outcome=None) -> ExecutionResult:
        """Terminal protocol for a signalled run: durable `interrupted`
        status (run_status.json names this process as the child that ran,
        so no reconciler mistakes it for a never-launched reservation), a
        trace event, the operator-facing line, and `128 + signal`."""
        from .. import run_status as rs

        reason = (f"interrupted by {req.signal_name} — run is resumable "
                  f"(--resume {run_dir.name})")
        try:
            rs.mark_interrupted(run_dir, note=reason)
        except OSError:
            pass
        trace.record("run_interrupted", signal=req.signal_name,
                     exit_code=req.exit_code,
                     steps_completed=sorted(
                         k for k, r in (getattr(outcome, "step_results", None)
                                        or {}).items() if r.ok))
        for step_id, r in (getattr(outcome, "step_results", None) or {}).items():
            mark = style("✓", "green") if r.ok else style("✗", "red", "bold")
            print(f"  {mark} {step_id}: {r.summary}")
        print(f"run {run_dir.name}: interrupted ({req.signal_name})  ({run_dir})")
        print(style("  ⚠ ", "yellow", "bold") + reason)
        return ExecutionResult(req.exit_code, reason)
