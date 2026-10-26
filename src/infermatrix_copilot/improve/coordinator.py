"""Weekly business facts and leases; shared execution drives its stages."""
from __future__ import annotations
import asyncio
import json
import time

from ..app.workflow_execution import WorkflowExecution
from ..engine.step import StepResult, StepSpec
from ..engine.registry import StepRegistry
from ..playbooks.store import Playbook, PlaybookStep
from ..trace_store import bind_store, file_lock
from .artifacts import atomic_json


def run(settings, store, *, workflow="all", repo="", post=False, now=None, llm=None, sandbox=None):
    """Synchronous CLI/scheduler compatibility entry."""
    return asyncio.run(run_async(settings, store, workflow=workflow, repo=repo, post=post,
                                 now=now, llm=llm, sandbox=sandbox))


async def run_async(settings, store, *, workflow="all", repo="", post=False, now=None, llm=None, sandbox=None):
    from .cycle import run_cycle, ledger_dir_for
    from .budget import iso_week
    from . import experiments, evolution, objectives

    if not settings.improve_enabled or not settings.improve_evolve_enabled:
        return {"state": "disabled", "reason": "improvement kill switch"}
    now = time.time() if now is None else now
    root = ledger_dir_for(settings)
    root.mkdir(parents=True, exist_ok=True)
    with file_lock(root / "coordinator.lock", blocking=False) as held:
        if not held:
            return {"state": "deferred", "reason": "cycle coordinator locked"}
        path = root / "coordinator.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        if state.get("state") == "complete":
            if iso_week(state["at"]) == iso_week(now):
                state["stages"].pop("evolve", None)
            else:
                state = {}
        if not state:
            state = {"state": "running", "at": now, "stages": {}}
            atomic_json(path, state)
        stages = state["stages"]
        forensic_key = "forensics" if workflow == "all" and not repo else f"forensics:{workflow}:{repo}"

        async def lint(ctx):
            if "lint" not in stages:
                stages["lint"] = await asyncio.to_thread(run_cycle, store, settings, root, now=state["at"])
            ctx.state["improve_cycle"] = stages["lint"]
            return StepResult(True, summary="cycle lint recorded")

        async def registered_experiments(ctx):
            attempted = {r["id"] for r in stages.get("experiments", [])}
            completed = list(stages.get("experiments", []))
            for exp in experiments.list_experiments(root):
                if exp.state not in ("registered", "running") or exp.experiment_id in attempted:
                    continue
                try:
                    result = await asyncio.to_thread(experiments.run, store, settings, root, exp.experiment_id, judge_llm=llm, sandbox=sandbox)
                    completed.append({"id": result.experiment_id, "state": result.state})
                except Exception as exc:
                    completed.append({"id": exp.experiment_id, "error": str(exc)})
            stages["experiments"] = completed
            return StepResult(True, summary="registered experiments recorded")

        async def forensics(ctx):
            if objectives.enabled(settings):
                stages["objective_inputs"] = objectives.prepare(settings, store, workflow=workflow, repo=repo)
                from .ledger import Ledger
                ledger = Ledger(root, store)
                completed_candidates = evolution.candidates(settings)
                attempted_proposals = {c.get("proposal") for c in completed_candidates}
                for completed in completed_candidates:
                    if completed["state"] in evolution.TERMINAL:
                        for proposal in ledger.load(completed["workflow"]).proposals:
                            if proposal.id == completed.get("proposal") and proposal.state in ("open", "experiment-registered", "neutral", "underpowered"):
                                ledger.transition(completed["workflow"], proposal.id, "closed", candidate=completed["id"], outcome=completed["state"])
                for name in stages["objective_inputs"]:
                    if not any(p.state == "open" and p.id not in attempted_proposals for p in ledger.load(name).proposals):
                        records = store.query(workflow=name, limit=3)
                        if records:
                            ledger.open_proposal(name, tier=1, stage="efficiency", claim="Reduce resource use while preserving executable contracts and existing outputs", evidence=[r["id"] for r in records], loss=1)
                atomic_json(path, state)
            if forensic_key not in stages:
                stages[forensic_key] = {"ok": False, "summary": "forensics-interrupted; no paid-call replay"}
                atomic_json(path, state)  # intent survives any exception or process death
                if objectives.enabled(settings):
                    from .runtime import diagnose
                    result = StepResult(True, summary="program-verified hypotheses; no human labels or evaluator model",
                                        outputs=await asyncio.to_thread(diagnose, settings, store, llm))
                else:
                    from ..engine.steps.improve import _forensics
                    result = await _forensics(ctx)
                stages[forensic_key] = {"ok": result.ok, "outputs": result.outputs, "summary": result.summary}
            record = stages[forensic_key]
            return StepResult(True, summary=record.get("summary", "forensics recorded"),
                              outputs={"trace_outputs": {"forensics": json.dumps(record.get("outputs", {}), default=str)}})

        async def evolve(ctx):
            if "evolve" not in stages:
                stages["evolve"] = await asyncio.to_thread(evolution.run, settings, store, workflow=workflow, repo=repo, post=post, llm=llm, sandbox=sandbox, now=now)
            return StepResult(True, summary="evolution recorded")

        errors = []

        def handler(fn):
            async def execute(ctx):
                try:
                    result = await fn(ctx)
                    atomic_json(path, state)
                    return result
                except Exception as exc:
                    errors.append(exc)
                    raise
            return execute

        registry = StepRegistry()
        steps = []
        for name, fn in (("lint", lint), ("experiments", registered_experiments), ("forensics", forensics), ("evolve", evolve)):
            registry.register(StepSpec(f"improve.{name}", "agent" if name == "forensics" else "deterministic", "read", handler(fn), checkpoint=False))
            steps.append(PlaybookStep(forensic_key if name == "forensics" else name, f"improve.{name}", params={"workflow": "" if workflow == "all" else workflow, "repo": repo} if name == "forensics" else {}))
        plan = Playbook("workflow-improve", 1, "locked", [], [], steps)
        bound_settings = settings.model_copy(update={"trace_store_root": str(store.root)})
        if llm is None:
            from ..llm import LLM
            llm = LLM(settings)
        with bind_store(store):
            outcome = await WorkflowExecution(bound_settings, registry).execute(
                plan, run_dir=root / "coordinator-run" / str(state["at"]),
                state={"improve_item": "cycle"}, llm=llm)
        if errors:
            raise errors[0]
        if outcome.status != "done":
            raise RuntimeError(outcome.blocked_reason or "improvement execution failed")
        state.update(state="complete", units=stages["lint"].get("units", 0),
                     proposals_opened=stages["lint"].get("proposals_opened", []))
        atomic_json(path, state)
        return state
