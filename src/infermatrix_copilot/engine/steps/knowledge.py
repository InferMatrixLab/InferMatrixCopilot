"""Knowledge-service steps (``knowledge.*``) used by the kb-* playbooks.

They are thin: the logic lives in ``kb_service``; steps resolve the runtime
from the environment, act on ONE repository (``params.repo``) and publish
only plain data through ``state_updates`` so ``--resume`` works. A missing
repository, a disabled one, or an unavailable pinned model blocks the run
rather than guessing.
"""

from __future__ import annotations

from ..step import FailureKind, StepContext, StepResult
from ._common import step

def _runtime(ctx: StepContext):
    if ctx.runtime is not None:
        return ctx.runtime
    from ...kb_service.runtime import KbRuntime
    from ...kb_service.cli import DEFAULT_STATE_DIR
    from pathlib import Path

    return KbRuntime.from_env(ctx.settings, state_dir=Path(ctx.params.get("state_dir") or DEFAULT_STATE_DIR))


def _lifecycle(ctx: StepContext):
    rt = _runtime(ctx)
    repo = str(ctx.params.get("repo") or ctx.state.get("kb_repo") or "")
    lifecycle = rt.registry.get(repo)
    if lifecycle is None or not lifecycle.enabled:
        return rt, None
    return rt, lifecycle


@step("knowledge.collect_events", kind="deterministic", risk="read",
      description="Record new knowledge events (merged PRs, Copilot run lessons) for one repository")
async def collect_events(ctx: StepContext) -> StepResult:
    rt, lifecycle = _lifecycle(ctx)
    if lifecycle is None:
        return StepResult(False, FailureKind.BLOCKED, "repository is unknown or disabled")
    from ...kb_service.runtime import collect_events as collect

    count = collect(rt, lifecycle)
    return StepResult(True, summary=f"{count} new event(s)",
                      outputs={"state_updates": {"kb_repo": lifecycle.repo, "kb_new_events": count}})


@step("knowledge.intake", kind="agent", risk="knowledge",
      description="Draft typed knowledge operations from pending events and run the quality gate")
async def intake(ctx: StepContext) -> StepResult:
    rt, lifecycle = _lifecycle(ctx)
    if lifecycle is None:
        return StepResult(False, FailureKind.BLOCKED, "repository is unknown or disabled")
    from ...kb_service.runtime import run_intake

    changeset_id = run_intake(rt, lifecycle, max_events=int(ctx.params.get("max_events", 10)))
    status = rt.ledger.changeset(changeset_id)["status"] if changeset_id else "none"
    return StepResult(True, summary=f"change set {changeset_id or '-'}: {status}",
                      outputs={"state_updates": {"kb_changeset": changeset_id or "",
                                                 "kb_changeset_status": status}})


@step("knowledge.publish", kind="deterministic", risk="knowledge",
      description="Hand a gated change set to the publisher (auto_merge) or record it (shadow)")
async def publish(ctx: StepContext) -> StepResult:
    rt, lifecycle = _lifecycle(ctx)
    changeset_id = str(ctx.state.get("kb_changeset") or "")
    if lifecycle is None:
        return StepResult(False, FailureKind.BLOCKED, "repository is unknown or disabled")
    if not changeset_id:
        return StepResult(True, summary="nothing to publish")
    from ...kb_service.runtime import publish as publish_changeset

    status = publish_changeset(rt, lifecycle, changeset_id)
    return StepResult(True, summary=f"change set {changeset_id}: {status}",
                      outputs={"state_updates": {"kb_changeset_status": status}})


def _with_lease(rt, work):
    """Run ``work(owner)`` under the single-writer lease (``kb run`` path); the
    scheduler already holds it and passes its own owner."""
    if rt.lease_owner:
        return work(rt.lease_owner)
    with rt.ledger.lease() as owner:
        return work(owner)


@step("knowledge.advance_merges", kind="deterministic", risk="knowledge",
      description="Advance in-flight knowledge PRs: sign verdicts, enqueue, record merges")
async def advance_merges(ctx: StepContext) -> StepResult:
    rt, lifecycle = _lifecycle(ctx)
    if lifecycle is None:
        return StepResult(False, FailureKind.BLOCKED, "repository is unknown or disabled")
    from ...kb_service import merge

    if rt.outbox is not None and rt.publisher_public_key is not None:
        merge.apply_acks(rt, rt.outbox.collect_acks(rt.publisher_public_key))
    events = _with_lease(rt, lambda owner: merge.advance(rt, lifecycle))
    return StepResult(True, summary="; ".join(events) or "nothing to advance",
                      outputs={"state_updates": {"kb_merge_events": events}})


@step("knowledge.sweep", kind="agent", risk="knowledge",
      description="Release sweep: T1 structure, T2/T3 rule re-checks, purge; each page through the gate")
async def sweep(ctx: StepContext) -> StepResult:
    rt, lifecycle = _lifecycle(ctx)
    if lifecycle is None or not lifecycle.full_name:
        return StepResult(False, FailureKind.BLOCKED, "repository is unknown, disabled or has no upstream")
    from ...kb_service.runtime import publish as publish_changeset
    from ...kb_service.sweep import UpstreamRepo, detect_release, run_sweep

    upstream = UpstreamRepo(rt.state_dir / "upstream" / f"{lifecycle.repo}.git", lifecycle.full_name)

    def work(owner):
        found = detect_release(rt, lifecycle, upstream)
        if found is None:
            return None
        report = run_sweep(rt, lifecycle, owner, found, upstream)
        for changeset_id in report["changesets"]:
            publish_changeset(rt, lifecycle, changeset_id)
        return report

    report = _with_lease(rt, work)
    if report is None:
        return StepResult(True, summary="no release to sweep")
    return StepResult(True, summary=f"sweep {report['sweep']['tag']}: {len(report['changesets'])} change set(s)",
                      outputs={"state_updates": {"kb_sweep_changesets": report["changesets"]}})


@step("knowledge.activate", kind="deterministic", risk="knowledge",
      description="Build, verify and activate the knowledge snapshot of the knowledge repository's main")
async def activate_snapshot(ctx: StepContext) -> StepResult:
    rt = _runtime(ctx)
    from ...kb_service.activate import ActivationError, activate

    try:
        sha = rt.knowledge.fetch()
        snapshot = activate(rt, sha)
    except ActivationError as exc:
        return StepResult(False, FailureKind.ESCALATE, f"activation refused: {exc}")
    return StepResult(True, summary=f"active snapshot {sha}",
                      outputs={"state_updates": {"kb_active_snapshot": sha, "kb_snapshot_path": str(snapshot)}})


def _init_runtime(ctx: StepContext):
    """kb init's own runtime: never the service's (that opens kb.db), and it
    serves disabled repositories — bootstrapping one is the point."""
    from pathlib import Path

    from ...kb_service.cli import DEFAULT_STATE_DIR
    from ...kb_service.init_support import InitRuntime

    if ctx.runtime is not None:
        return ctx.runtime
    return InitRuntime.from_env(ctx.settings, state_dir=Path(ctx.params.get("state_dir") or DEFAULT_STATE_DIR))


def init_options(params):
    """Decode the unchanged CLI/playbook parameter surface once."""
    dry_run = str(params.get("dry_run", "true")).lower() not in ("0", "false", "no")
    pin = str(params.get("pin") or "") or None
    count = params.get("pr_count")
    kwargs = {"pr_count": int(count)} if count not in (None, "") else {}
    ceiling = params.get("budget_usd")
    if ceiling not in (None, ""):
        kwargs["budget_usd"] = float(ceiling)
    for option in ("from_existing", "subscription_generator", "retry_unfinished", "unlimited_subscription"):
        if str(params.get(option, "false")).lower() in ("1", "true", "yes"):
            kwargs[option] = True
    if params.get("acceptance_mode"):
        kwargs["acceptance_mode"] = str(params["acceptance_mode"])
    if params.get("foundation_mode"):
        kwargs["foundation_mode"] = str(params["foundation_mode"])
    if params.get("foundation_record"):
        from pathlib import Path
        kwargs["foundation_record_path"] = Path(params["foundation_record"])
    return {"dry_run": dry_run, "pin": pin, **kwargs}


@step("knowledge.init", kind="agent", risk="knowledge",
      checkpoint=False,
      description="kb init: bootstrap one repository's knowledge base, one human-merged stage at a time")
async def init_stage(ctx: StepContext) -> StepResult:
    from ...kb_service.init_stages import run_stage_async
    from ...kb_service.init_support import InitError

    repo = str(ctx.params.get("repo") or ctx.state.get("kb_repo") or "")
    stage = str(ctx.params.get("stage") or "")
    try:
        rt = _init_runtime(ctx)
        lifecycle = rt.registry.get(repo)
        if lifecycle is None:
            return StepResult(False, FailureKind.BLOCKED, f"no adapter declares knowledge repo {repo!r}")
        record = await run_stage_async(rt, lifecycle, stage, **init_options(ctx.params))
    except (InitError, NotImplementedError, ValueError) as exc:
        return StepResult(False, FailureKind.BLOCKED, str(exc))
    updates = {"kb_init_stage": stage, "kb_init_status": record.status, "kb_init_pr": dict(record.pr)}
    if record.status in ("blocked", "partial"):
        return StepResult(False, FailureKind.BLOCKED, "; ".join(record.problems)[:2000] or
                          "semantic depth target unmet; partial preview and checkpoint retained",
                          outputs={"state_updates": updates})
    return StepResult(True, summary=f"kb init {stage} for {repo}: {record.status}",
                      outputs={"state_updates": updates})


from ...kb_service.init_execution import step_specs
from ._common import register_step

for _spec in step_specs():
    register_step(_spec)
