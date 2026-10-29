"""Executor — runs a Playbook's step graph with task-agnostic guarantees:
per-step checkpoint/resume, bounded retries, typed failure routing, RunTrace,
and escalation on BLOCKED/ESCALATE.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .step import FailureKind, StepContext, StepResult
from .registry import StepRegistry

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from ..llm import LLM
    from ..notify import Notifier
    from ..playbooks.store import Playbook
    from ..run_trace import RunTrace


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
        self.progress_file = self.run_dir / "progress.json"

    # -- checkpoint / resume ------------------------------------------------
    def _load_progress(self) -> dict:
        """Read the run's checkpoint (a `{"completed": {step_id: ...}}` map) from
        progress.json, or the empty checkpoint when this is a fresh run."""
        if self.progress_file.exists():
            return json.loads(self.progress_file.read_text(encoding="utf-8"))
        return {"completed": {}}

    def _save_progress(self, progress: dict) -> None:
        """Persist the checkpoint to progress.json (creating run_dir), so a later
        resume skips completed steps. `default=str` tolerates non-JSON values.
        Written tmp-file + fsync + `os.replace` + directory fsync: a torn or
        lost progress.json strands every resume path, so the checkpoint must
        survive a crash at any point during the write."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        data = json.dumps(progress, indent=2, default=str)
        tmp = self.progress_file.with_name(self.progress_file.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.progress_file)
        try:
            # directory fsync makes the rename itself durable
            dir_fd = os.open(self.run_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            # opening/fsyncing a directory is unsupported on some platforms
            # (Windows) and filesystems — those degrade to rename atomicity.
            # Real storage failures (EIO) mean the durability guarantee is
            # gone and MUST propagate rather than continue on a bad disk.
            import errno
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EACCES,
                                 errno.EPERM, errno.EISDIR, errno.EBADF):
                raise

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
        root = str(getattr(self.settings, "trace_store_root", "") or "")
        governed_run = bool(getattr(self.settings, "improve_governed", False))
        if not root and not governed_run:
            return await self._run_steps(playbook, state)
        from contextlib import ExitStack

        from ..trace_store import TraceStore, bind_store

        with ExitStack() as stack:
            if root:
                stack.enter_context(bind_store(TraceStore(Path(root).expanduser())))
            if governed_run:
                # a shadow child of an experiment: every model call of this
                # process is reserved against the week's envelope (design §10)
                from ..improve.budget import governed
                from ..improve.cycle import governor_for, ledger_dir_for

                stack.enter_context(governed(governor_for(self.settings, ledger_dir_for(self.settings))))
            return await self._run_steps(playbook, state)

    def _drop_from(self, playbook: "Playbook", progress: dict,
                   step_id: str) -> str:
        """Operator `--rerun-from`: forget `step_id` and every later step so
        they execute again. Returns an error string for an unknown id."""
        ids = [s.id for s in playbook.steps]
        if step_id not in ids:
            return (f"--rerun-from: step '{step_id}' is not in playbook "
                    f"'{playbook.name}'")
        later = ids[ids.index(step_id):]
        dropped = [i for i in later if i in progress["completed"]]
        for i in dropped:
            del progress["completed"][i]
        self._save_progress(progress)
        self.trace.record("rerun_from", step=step_id, dropped=dropped)
        return ""

    async def _run_steps(self, playbook: "Playbook", state: dict) -> RunOutcome:
        progress = self._load_progress()
        outcome = RunOutcome(status="done")
        if state.get("rerun_from"):
            error = self._drop_from(playbook, progress, state["rerun_from"])
            if error:
                outcome.status = "blocked"
                outcome.blocked_reason = error
                return outcome
        state.setdefault("playbook", playbook.name)
        # Run-scoped params (CLI `--task-param`, or intent-derived) reach every
        # step. Without this a step's `ctx.params.get(...)` only ever saw the
        # playbook's own step params, so `--task-param limit=5` was silently
        # dropped and issue.fetch kept its default of 20.
        task_params = (state.get("task_spec") or {}).get("params") or {}

        for pstep in playbook.steps:
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
            if pstep.id in progress["completed"]:
                cached = progress["completed"][pstep.id]
                cached_outputs = cached.get("outputs", {}) or {}
                # steps may publish JSON-simple state keys via outputs.state_updates
                # so resumed runs recover them without re-running the step
                state.update(cached_outputs.get("state_updates") or {})
                state.setdefault("outputs", {})[pstep.id] = cached_outputs
                outcome.step_results[pstep.id] = StepResult(
                    ok=True, summary=cached.get("summary", "(resumed)"),
                    outputs=cached_outputs,
                )
                continue

            spec = self.registry.get(pstep.step)
            if getattr(self.settings, "improve_shadow", False) and spec.risk not in ("read", "report"):
                # a shadow (experiment) run may never reach a writing or
                # posting step, whatever the playbook says: refused at the
                # execution boundary, recorded, and the run stops as blocked
                # so the experiment is marked invalid (design §8.2 layer 2)
                self.trace.record("step_refused", step=pstep.id, spec=spec.name, risk=spec.risk,
                                  reason="shadow run refuses non-read steps")
                outcome.status = "blocked"
                outcome.blocked_reason = (f"shadow run refused step '{pstep.id}' ({spec.name}, "
                                          f"risk={spec.risk}): only read/report steps may run")
                state["shadow_violation"] = outcome.blocked_reason
                return outcome
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

        from ..improve.artifacts import runtime_settings
        effective_settings = runtime_settings(self.settings, f"{state.get('playbook', '')}.{spec.name}")
        step_llm = self.llm
        if effective_settings is not self.settings and hasattr(self.llm, "settings"):
            from copy import copy
            step_llm = copy(self.llm)
            step_llm.settings = effective_settings
        ctx = StepContext(
            settings=effective_settings, state=state, params=params or {},
            run_dir=self.run_dir, trace=self.trace, llm=step_llm, item=item,
        )
        attempts = 1 + max(0, self.settings.max_step_retries)
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
                    from ..improve import objectives
                    last = None
                    if spec.name == "agent.review_diff" and objectives.enabled(ctx.settings) and ctx.settings.improve_enabled and ctx.settings.improve_evolve_enabled:
                        from ..improve.runtime import review_step
                        last = await review_step(ctx)
                    if last is None:
                        last = await spec.handler(ctx)
            except Exception as exc:  # handler bug != typed failure
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
        blobs: dict[str, str] = {}
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
                store.append("decision", outputs=blobs,
                             result={"type": "step_result", "step": spec.name, "status": "ok" if result.ok else "failed",
                                     "failure": result.failure.value if result.failure else "",
                                     "summary": str(result.summary or "")[:300],
                                     "findings": findings, "verdict": str(outputs.get("review_verdict") or "")})
        except Exception:  # noqa: BLE001 - capture never changes a step's outcome
            pass


    # -- trace/1 unit context ---------------------------------------------------
    def _declarations(self) -> dict:
        """Workflow declarations, loaded once per executor (a malformed file is a
        loud configuration error, never a silent Tier 1 downgrade)."""
        cached = getattr(self, "_decls", None)
        if cached is None:
            from ..improve.enroll import declarations_for

            cached = declarations_for(self.settings)
            self._decls = cached
        return cached

    def _unit_context(self, step: str, step_id: str, state: dict, item) -> dict:
        """The trace/1 context of one unit of work (design §3.1): run, playbook,
        step, ``unit_id``; plus ``workflow``, ``item`` and the declared
        ``fingerprint`` when the step is enrolled. An incomplete fingerprint is
        recorded as ``fingerprint_missing`` (the unit stays Tier 1)."""
        from ..improve.enroll import item_for, lookup

        run_id = self.run_dir.name
        playbook = str(state.get("playbook") or "")
        unit_id = f"{run_id}:{step_id or step}"
        if item is not None:
            unit_id += f":{_item_key(item)}"
        context: dict = {"run_id": run_id, "playbook": playbook, "step": step, "unit_id": unit_id}
        tag = os.environ.get("IMPROVE_UNIT_TAG", "")
        if tag:
            context["unit_tag"] = tag        # an experiment's per-run identity (never reused)
        decl = lookup(self._declarations(), playbook, step)
        if decl is None:
            return context
        from ..improve.fingerprint import compute

        context["workflow"] = decl.workflow
        context["item"] = item_for(decl, state)
        from ..improve.artifacts import runtime_settings
        digest, manifest = compute(decl, runtime_settings(self.settings, decl.workflow), state=state)
        if digest:
            context["fingerprint"] = digest
        else:
            context["fingerprint_missing"] = list(manifest.get("missing") or [])
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
    return StepResult(True, None, f"all {len(results)} items ok", merged_outputs, changed)
