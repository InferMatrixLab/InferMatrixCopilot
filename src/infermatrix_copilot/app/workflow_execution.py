"""Execute an already selected workflow with run and knowledge locks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ..adapters.base import AdapterError
from .. import run_status as rs
from ..providers.outcomes import requires_reconciliation
from ..config import Settings
from ..engine.executor import Executor, RunOutcome
from ..engine.lifecycle import RunLock, RunLockHeld, run_guarded
from ..engine.registry import StepRegistry
from ..metrics import format_metrics_line
from ..notify import BLOCKED_EXIT, Notifier
from ..push import PushPolicy
from ..run_trace import RunTrace
from ..task_spec import TaskSpec
from ..ui import style
from .repository_context import RepositoryContext, RepositoryContextResolver
from .agent_context import bind_agent_context


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    blocked_reason: str = ""
    state: str = ""


class WorkflowExecution:
    """Own the executor, locks, tracing and terminal outcome for one run."""

    def __init__(
        self, settings: Settings, registry: StepRegistry,
        repository_context: RepositoryContextResolver | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.repository_context = repository_context

    async def execute(
        self, playbook, *, run_dir: Path, state: dict, runtime=None,
        llm=None, trace=None, notifier=None, held_lock: RunLock | None = None,
        fingerprint: str = "", validate_cached=None,
    ) -> RunOutcome:
        """Execute a bound workflow without planning or application authority.

        Callers own repository, publication and domain leases. This kernel owns
        only a newly acquired run lock and injects resources without persisting
        them. An existing lock remains the caller's to release.
        """
        run_dir = Path(run_dir)
        lock = held_lock
        if lock is None:
            try:
                lock = RunLock(run_dir).acquire()
            except RunLockHeld as exc:
                return RunOutcome("blocked", blocked_reason=str(exc))
        try:
            trace = trace if trace is not None else RunTrace(run_dir / "run_trace.jsonl")
            from ..improve.execution import bind_execution

            pending = False

            def observe_error(error):
                nonlocal pending
                pending = pending or requires_reconciliation(error)

            with bind_execution(self.settings, self.registry) as (registry, context_for, authorize):
                executor = Executor(
                    bind_agent_context(registry), self.settings, run_dir=run_dir, trace=trace,
                    llm=llm, notifier=notifier, runtime=runtime,
                    fingerprint=fingerprint, validate_cached=validate_cached,
                    context_for=context_for, authorize=authorize,
                    on_handler_error=observe_error,
                )
                outcome = await run_guarded(executor.run(playbook, state), run_dir)
                if pending:
                    outcome.status = rs.INTERRUPTED
                return outcome
        finally:
            if held_lock is None:
                lock.release()

    def run(
        self, playbook, spec: TaskSpec, run_dir: Path, *, llm,
        resolution_mode: str = "resume", tier: str = "?",
        resuming: bool = False, held_lock: RunLock | None = None,
        planned_context: RepositoryContext | None = None,
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
            from .completion_execution import completion_settings_for_run
            settings = completion_settings_for_run(self.settings, run_dir, resuming=resuming, allow_create=True)
            execution = self
            if settings is not self.settings:
                execution = WorkflowExecution(settings, self.registry, self.repository_context)
                from ..providers import HarnessLLM, llm_for
                if isinstance(llm, HarnessLLM):
                    llm = llm_for(settings)
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
            notifier = Notifier(settings, run_dir, trace, run_dir.name)
            trace.record("task", spec=spec.model_dump(), resolution=resolution_mode,
                         playbook=playbook.name, tier=tier)
            state: dict = {
                "task_spec": spec.model_dump(),
                "repo_path": repo_context.repo_path,
                "push_policy": PushPolicy(),  # steps may replace with a derived policy
                "protected_branches": list(repo_context.protected_branches),
                "resuming": resuming,
            }
            if repo_context.high_risk_modules:
                state["high_risk_modules"] = list(repo_context.high_risk_modules)
            outcome = asyncio.run(
                execution.execute(playbook, run_dir=run_dir, state=state,
                             trace=trace, llm=llm, notifier=notifier, held_lock=lock))

            if outcome.status == "done":
                notifier.resolve()

            if settings.metrics_enabled:
                try:  # metrics are facts about the run; never let them break it
                    from ..metrics import collect_run_metrics
                    m = collect_run_metrics(run_dir, settings, outcome.status)
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
                return ExecutionResult(BLOCKED_EXIT, blocked_reason, outcome.status)
            return ExecutionResult(0 if outcome.status == "done" else 1, blocked_reason, outcome.status)
        finally:
            if knowledge_lock is not None:
                knowledge_lock.release()
            if held_lock is None:
                lock.release()
