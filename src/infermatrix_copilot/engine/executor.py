"""Executor — runs a Playbook's step graph with task-agnostic guarantees:
per-step checkpoint/resume, bounded retries, typed failure routing, RunTrace,
and escalation on BLOCKED/ESCALATE.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from .step import FailureKind, StepContext, StepResult
from .registry import StepRegistry
from ..persistence import atomic_write_bytes, fsync_directory

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from ..llm import LLM
    from ..notify import Notifier
    from ..playbooks.store import Playbook
    from ..run_trace import RunTrace


def _checkpoint_directory(directory):
    try:
        fsync_directory(directory)
    except OSError as exc:
        import errno
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EACCES,
                             errno.EPERM, errno.EISDIR, errno.EBADF):
            raise


@dataclass
class RunOutcome:
    """The result of running a whole playbook: a terminal `status` ("done" once
    every step passed, else "failed" or "blocked"), the per-step `step_results`
    keyed by playbook-step id, and a `blocked_reason` explaining an early halt."""

    status: str  # "done" | "failed" | "blocked"
    step_results: dict[str, StepResult] = field(default_factory=dict)
    blocked_reason: str = ""


class Executor:
    """Runs a Playbook's step graph with task-agnostic guarantees: per-step
    checkpoint/resume (progress.json), bounded retries, typed failure routing,
    RunTrace recording, and escalation on BLOCKED/ESCALATE/FORBIDDEN."""

    def __init__(
        self,
        registry: StepRegistry,
        settings: "Settings",
        *,
        run_dir: Path,
        trace: "RunTrace",
        llm: Optional["LLM"] = None,
        notifier: Optional["Notifier"] = None,
        runtime=None,
        fingerprint: str = "",
        validate_cached: Callable[[str, dict], None] | None = None,
        context_for=None,
        authorize=None,
        on_handler_error: Callable[[Exception], None] | None = None,
    ):
        """Wire the executor to its `registry` (step lookups), `settings`
        (retry bounds, post gates), the `run_dir` where progress.json is
        checkpointed, the `trace` sink, and optional `llm`/`notifier` handed to
        each step's context and used for escalation."""
        self.registry = registry
        self.settings = settings
        self.run_dir = Path(run_dir)
        self.trace = trace
        self.llm = llm
        self.notifier = notifier
        self.runtime = runtime
        self.fingerprint = fingerprint
        self.validate_cached = validate_cached
        self.context_for = context_for
        self.authorize = authorize
        self.on_handler_error = on_handler_error
        self.progress_file = self.run_dir / "progress.json"

    # -- checkpoint / resume ------------------------------------------------
    def _load_progress(self) -> dict:
        """Read the run's checkpoint (a `{"completed": {step_id: ...}}` map) from
        progress.json, or the empty checkpoint when this is a fresh run."""
        if self.progress_file.exists():
            progress = json.loads(self.progress_file.read_text(encoding="utf-8"))
            if self.fingerprint and progress.get("fingerprint") != self.fingerprint:
                raise ValueError("checkpoint fingerprint differs from this execution")
            if not isinstance(progress.get("completed"), dict):
                raise ValueError("checkpoint has no completed-step map")
            for cached in progress["completed"].values():
                if not isinstance(cached, dict):
                    raise ValueError("checkpoint contains an invalid step result")
                outputs = cached.get("outputs")
                if outputs is not None and not isinstance(outputs, dict):
                    raise ValueError("checkpoint contains invalid outputs")
                updates = (outputs or {}).get("state_updates")
                if updates is not None and not isinstance(updates, dict):
                    raise ValueError("checkpoint contains invalid state updates")
            return progress
        return {"completed": {}, **({"fingerprint": self.fingerprint} if self.fingerprint else {})}

    def _save_progress(self, progress: dict) -> None:
        """Persist the checkpoint to progress.json (creating run_dir), so a later
        resume skips completed steps. `default=str` tolerates non-JSON values.
        Written tmp-file + fsync + `os.replace` + directory fsync: a torn or
        lost progress.json strands every resume path, so the checkpoint must
        survive a crash at any point during the write."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        data = json.dumps(progress, indent=2, default=str)
        atomic_write_bytes(self.progress_file, data.encode("utf-8"), directory_fsync=_checkpoint_directory)

    # -- execution ------------------------------------------------------------
    async def run(self, playbook: "Playbook", state: dict) -> RunOutcome:
        """Execute `playbook`'s steps in order against shared `state`, returning a
        RunOutcome. Each step is: gated by its `when:` condition (an unknown key
        blocks rather than silently skips), short-circuited if already in the
        checkpoint (its published `state_updates` are replayed so resume is
        faithful), otherwise fanned out over `foreach` items, merged, traced, and
        checkpointed on success. A BLOCKED/ESCALATE/FORBIDDEN failure notifies and
        halts as "blocked"; any other failure halts as "failed".

        With `settings.trace_store_root` set, the whole run binds that trace/1
        store so the choke points (`tools.dispatch`, `LLM.create`) capture every
        tool and model call under the unit context of the running step."""
        from ..model_usage import bind_model_usage
        with bind_model_usage(self.trace, self.run_dir.name):
            return await self._run_with_trace_store(playbook, state)

    async def _run_with_trace_store(self, playbook, state):
        root = str(getattr(self.settings, "trace_store_root", "") or "")
        if not root:
            return await self._run_steps(playbook, state)
        from ..trace_store import TraceStore, bind_store, current_store
        bound = current_store()
        if bound is not None and bound.root.resolve() == Path(root).expanduser().resolve():
            return await self._run_steps(playbook, state)
        with bind_store(TraceStore(Path(root).expanduser())):
            return await self._run_steps(playbook, state)

    async def _run_steps(self, playbook: "Playbook", state: dict) -> RunOutcome:
        outcome = RunOutcome(status="done")
        try:
            progress = self._load_progress()
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            outcome.status = "blocked"
            outcome.blocked_reason = f"checkpoint refused: {exc}"
            return outcome
        state.setdefault("playbook", playbook.name)
        # Run-scoped params (CLI `--task-param`, or intent-derived) reach every
        # step. Without this a step's `ctx.params.get(...)` only ever saw the
        # playbook's own step params, so `--task-param limit=5` was silently
        # dropped and issue.fetch kept its default of 20.
        task_params = (state.get("task_spec") or {}).get("params") or {}

        for pstep in playbook.steps:
            spec = self.registry.get(pstep.step)
            if pstep.when:
                try:
                    applies = _eval_when(pstep.when, state)
                except KeyError as exc:
                    outcome.status = "blocked"
                    outcome.blocked_reason = (
                        f"step '{pstep.id}': unknown `when:` key {exc} — "
                        "conditions may only reference TaskSpec fields or "
                        "state keys published by earlier steps")
                    return outcome
                if not applies:
                    outcome.step_results[pstep.id] = StepResult(
                        True, summary=f"skipped (when: {pstep.when})")
                    continue
            reason = self.authorize(spec, pstep.id, state) if self.authorize else ""
            if reason:
                self.trace.record("step_refused", step=pstep.id, spec=spec.name,
                                  risk=spec.risk, reason=reason)
                outcome.status = "blocked"
                outcome.blocked_reason = reason
                return outcome
            if spec.checkpoint and pstep.id in progress["completed"]:
                cached = progress["completed"][pstep.id]
                cached_outputs = cached.get("outputs", {}) or {}
                if self.validate_cached is not None:
                    try:
                        self.validate_cached(pstep.id, cached_outputs)
                    except Exception as exc:
                        outcome.status = "blocked"
                        outcome.blocked_reason = f"step '{pstep.id}': cached result refused: {exc}"
                        return outcome
                # steps may publish JSON-simple state keys via outputs.state_updates
                # so resumed runs recover them without re-running the step
                state.update(cached_outputs.get("state_updates") or {})
                state.setdefault("outputs", {})[pstep.id] = cached_outputs
                outcome.step_results[pstep.id] = StepResult(
                    ok=True, summary=cached.get("summary", "(resumed)"),
                    outputs=cached_outputs,
                )
                continue

            items = state.get(pstep.foreach, [None]) if pstep.foreach else [None]
            if pstep.foreach and not isinstance(items, list):
                items = [items]

            _t0 = time.monotonic()
            # A playbook's own step params are authored invariants — several are
            # safety-bearing (`force_push`, `pre_push`) — so they override the
            # run-scoped ones rather than the other way round.
            step_params = {**task_params, **pstep.params}
            if spec.risk in {"write_workspace", "push", "knowledge"}:
                # Foreach items share one checkout and run state. A writer must
                # finish before the next item can edit or commit that checkout.
                results = [await self._run_step(spec, step_params, state, item, pstep.id)
                           for item in items]
            else:
                results = await asyncio.gather(
                    *(self._run_step(spec, step_params, state, item, pstep.id)
                      for item in items)
                )
            result = _merge(results)
            outcome.step_results[pstep.id] = result
            self.trace.record(
                "step_result", step=pstep.id, spec=spec.name, ok=result.ok,
                failure=result.failure.value if result.failure else None,
                summary=result.summary,
                dur_s=round(time.monotonic() - _t0, 2),  # labeled phase timing
            )

            if result.ok:
                if spec.checkpoint and result.checkpoint:
                    progress["completed"][pstep.id] = {
                        "summary": result.summary, "outputs": result.outputs,
                    }
                    self._save_progress(progress)
                state.update((result.outputs or {}).get("state_updates") or {})
                state.setdefault("outputs", {})[pstep.id] = result.outputs
                continue

            # -- typed failure routing --
            if result.failure in (FailureKind.BLOCKED, FailureKind.ESCALATE,
                                  FailureKind.FORBIDDEN):
                reason = f"step '{pstep.id}' ({spec.name}): {result.summary}"
                if self.notifier is not None:
                    extra = result.outputs.get("escalation_summary") or {}
                    self.notifier.escalate(
                        reason=reason, phase=pstep.id, severity="blocked",
                        state_summary={"playbook": playbook.name, **extra},
                        artifacts=[str(self.progress_file),
                                   *result.outputs.get("artifacts", [])],
                    )
                outcome.status = "blocked"
                outcome.blocked_reason = reason
                return outcome

            outcome.status = "failed"
            outcome.blocked_reason = f"step '{pstep.id}' failed: {result.summary}"
            return outcome

        return outcome

    async def _run_step(self, spec, params: dict, state: dict, item,
                        step_id: str = "") -> StepResult:
        """Invoke one step's handler inside a tracing span, retrying only on
        RETRYABLE up to `settings.max_step_retries`. Builds the StepContext from
        `params`, shared `state`, and the current foreach `item`; an unhandled
        exception is caught and converted to a BLOCKED StepResult so a handler bug
        never escapes as a raw exception. Returns the last StepResult produced."""
        from .. import tracing
        from ..trace_store import trace_context

        ctx = StepContext(
            settings=self.settings, state=state, params=params or {},
            run_dir=self.run_dir, trace=self.trace, llm=self.llm, item=item,
            runtime=self.runtime,
        )
        attempts = 1 + max(0, getattr(self.settings, "max_step_retries", 1))
        last: StepResult | None = None
        # `step` alone cannot identify the work: a playbook may run the same spec
        # twice (repo-rebase-v3 runs its module-rebase spec for both waves) and
        # foreach fans it out again, so record the playbook step id and the item.
        ident = {"step_id": step_id} if step_id else {}
        if item is not None:
            ident["item"] = _item_key(item)
        unit = self._unit_context(spec.name, step_id, state, item)
        for attempt in range(1, attempts + 1):
            try:
                with tracing.span("step", step=spec.name, attempt=attempt, **ident), \
                        trace_context(attempt=attempt, **unit):
                    last = await spec.handler(ctx)
            except Exception as exc:  # handler bug != typed failure
                if self.on_handler_error is not None:
                    self.on_handler_error(exc)
                last = StepResult(False, FailureKind.BLOCKED,
                                  f"unhandled error: {type(exc).__name__}: {exc}")
            if last.ok or last.failure is not FailureKind.RETRYABLE:
                self._record_step_decision(spec, unit, attempt, last)
                return last
            self.trace.record("step_retry", spec=spec.name, attempt=attempt)
        self._record_step_decision(spec, unit, attempts, last)
        return last  # exhausted retries

    @staticmethod
    def _record_step_decision(spec, unit: dict, attempt: int, result: StepResult | None) -> None:
        """The unit's terminal ``decision`` record (trace/1): every step call
        ends with one, carrying the rendered artifact a reviewer or judge
        would see (`review_text`, `answer_draft`) and the published findings,
        so an outcome adapter judges what was actually produced — never an
        empty body. Written only when a store is bound; never raises."""
        from ..trace_store import current_store, trace_context

        store = current_store()
        if store is None or result is None:
            return
        outputs = result.outputs or {}
        blobs: dict[str, str] = dict(outputs.get("trace_outputs") or {})
        for key, name in (("review_text", "review"), ("answer_draft", "answer"), ("report_text", "report")):
            text = outputs.get(key)
            if isinstance(text, str) and text.strip():
                blobs[name] = text
        findings = []
        for f in (outputs.get("review_comments") or [])[:60]:
            if isinstance(f, dict):
                findings.append({k: (str(f[k])[:200] if isinstance(f.get(k), str) else f.get(k))
                                 for k in ("id", "file", "path", "line", "severity", "comment", "title", "disposition")
                                 if k in f})
        try:
            with trace_context(attempt=attempt, **unit):
                store.append("decision", inputs=unit.get("_trace_inputs"), outputs=blobs,
                             result={"type": "step_result", "step": spec.name, "status": "ok" if result.ok else "failed",
                                     "failure": result.failure.value if result.failure else "",
                                     "summary": str(result.summary or "")[:300],
                                     "findings": findings, "verdict": str(outputs.get("review_verdict") or "")})
        except Exception:  # noqa: BLE001 - capture never changes a step's outcome
            pass


    # -- trace/1 unit context ---------------------------------------------------
    def _unit_context(self, step: str, step_id: str, state: dict, item) -> dict:
        """Identify a unit; the application may add domain trace bindings."""
        run_id = self.run_dir.name
        playbook = str(state.get("playbook") or "")
        unit_id = f"{run_id}:{step_id or step}"
        if item is not None:
            unit_id += f":{_item_key(item)}"
        context: dict = {"run_id": run_id, "playbook": playbook, "step": step, "unit_id": unit_id}
        if self.context_for:
            context.update(self.context_for(step, state, item))
        return context


def _item_key(item) -> str:
    """A short label identifying one `foreach` item on its step span — a module
    name, a failure-group id — so fan-out siblings are told apart in a trace.
    Length-bounded: this is an identifier for reading, not the item itself."""
    if isinstance(item, str):
        return item[:80]
    if isinstance(item, dict):
        for k in ("id", "name", "module", "label", "key"):
            v = item.get(k)
            if v:
                return str(v)[:80]
    return str(item)[:80]


def _eval_when(when: str, state: dict) -> bool:
    """Evaluate a step condition: TaskSpec fields first (v1 semantics), then
    state keys published by earlier steps. Unknown keys raise KeyError instead
    of silently evaluating false (v2 P0 fix #3)."""
    spec = state.get("task_spec") or {}
    expr = when.strip()
    negate = expr.startswith("not ")
    key = expr[4:].strip() if negate else expr
    if key in spec:
        value = bool(spec.get(key))
    elif key in state:
        value = bool(state.get(key))
    else:
        raise KeyError(key)
    return (not value) if negate else value


def _merge(results: list[StepResult]) -> StepResult:
    """Merge foreach fan-out results: first failure wins, outputs keyed by index."""
    if len(results) == 1:
        return results[0]
    failed = [r for r in results if not r.ok]
    merged_outputs = {str(i): r.outputs for i, r in enumerate(results)}
    # lift state_updates to the top level (last writer wins per key) so the
    # executor's state.update / resume path sees fan-out publications too
    merged_updates: dict = {}
    for r in results:
        merged_updates.update((r.outputs or {}).get("state_updates") or {})
    if merged_updates:
        merged_outputs["state_updates"] = merged_updates
    changed = [f for r in results for f in r.changed_files]
    if failed:
        worst = failed[0]
        # surface the failing item's escalation material at the top level so
        # the notifier (which reads result.outputs directly) still sees it
        for key in ("escalation_summary", "artifacts"):
            if key in worst.outputs:
                merged_outputs[key] = worst.outputs[key]
        return StepResult(False, worst.failure,
                          f"{len(failed)}/{len(results)} items failed: {worst.summary}",
                          merged_outputs, changed)
    return StepResult(True, None, f"all {len(results)} items ok", merged_outputs, changed,
                      checkpoint=all(r.checkpoint for r in results))
