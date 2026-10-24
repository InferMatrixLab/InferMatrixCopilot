"""Shared execution must preserve resources and refuse unproven resumptions."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.app.workflow_execution import WorkflowExecution
from infermatrix_copilot.engine import StepRegistry, StepResult, StepSpec
from infermatrix_copilot.engine.lifecycle import RunLock, RunLockHeld, register_finalizer
from infermatrix_copilot.playbooks.store import Playbook, PlaybookStep


def workflow(*steps):
    return Playbook(name="bound", version=1, status="active", task_kinds=["pr_review"],
                    repos=[], steps=[PlaybookStep(name, name) for name in steps])


def test_embedded_execution_uses_default_retry_bound_without_full_app_settings(tmp_path):
    registry, visits = StepRegistry(), []

    async def handler(ctx):
        visits.append(True)
        return StepResult(True)

    registry.register(StepSpec("inspect", "deterministic", "read", handler))
    result = asyncio.run(WorkflowExecution(SimpleNamespace(), registry).execute(
        workflow("inspect"), run_dir=tmp_path, state={}))
    assert result.status == "done" and len(visits) == 1


def test_pending_projection_is_local_to_each_concurrent_workflow(settings, tmp_path):
    registry, calls = StepRegistry(), []

    async def handler(ctx):
        await asyncio.sleep(0)
        calls.append(ctx.state["pending"])
        error = RuntimeError("injected transport error")
        error.native_outcome_pending = ctx.state["pending"]
        raise error

    registry.register(StepSpec("inspect", "agent", "read", handler))
    execution = WorkflowExecution(settings, registry)

    async def run_both():
        return await asyncio.gather(*(execution.execute(
            workflow("inspect"), run_dir=tmp_path / str(pending),
            state={"pending": pending}) for pending in (True, False)))

    outcomes = asyncio.run(run_both())
    assert [result.status for result in outcomes] == ["interrupted", "blocked"]
    assert sorted(calls) == [False, True]


def test_current_authority_is_checked_before_cached_success(settings, tmp_path):
    registry, visits = StepRegistry(), []

    async def handler(ctx):
        visits.append(True)
        return StepResult(True)

    registry.register(StepSpec("publish", "script", "push", handler))
    plan = workflow("publish")
    first = asyncio.run(WorkflowExecution(settings, registry).execute(plan, run_dir=tmp_path, state={}))
    assert first.status == "done" and len(visits) == 1
    state = {}
    shadow = settings.model_copy(update={"improve_shadow": True})
    resumed = asyncio.run(WorkflowExecution(shadow, registry).execute(plan, run_dir=tmp_path, state=state))
    assert resumed.status == "blocked" and state["shadow_violation"]
    assert len(visits) == 1


def test_runtime_is_per_execution_and_never_checkpointed(settings, tmp_path):
    registry = StepRegistry()
    seen = []

    async def handler(ctx):
        await asyncio.sleep(0)
        seen.append(ctx.runtime)
        return StepResult(True, outputs={"value": ctx.runtime["value"]})

    registry.register(StepSpec("inspect", "deterministic", "read", handler))
    execution = WorkflowExecution(settings, registry)
    runtimes = [{"value": "one", "secret": object()}, {"value": "two", "secret": object()}]

    async def run():
        return await asyncio.gather(*[
            execution.execute(workflow("inspect"), run_dir=tmp_path / str(i), state={}, runtime=runtime)
            for i, runtime in enumerate(runtimes)
        ])

    assert all(outcome.status == "done" for outcome in asyncio.run(run()))
    assert seen == runtimes
    assert "secret" not in (tmp_path / "0" / "progress.json").read_text()


def test_preflight_always_runs_before_cached_replay(settings, tmp_path):
    registry, events = StepRegistry(), []

    async def preflight(ctx):
        events.append("preflight")
        return StepResult(True)

    async def build(ctx):
        events.append("build")
        return StepResult(True, outputs={"state_updates": {"accepted": True}})

    registry.register(StepSpec("prepare", "validation", "read", preflight, checkpoint=False))
    registry.register(StepSpec("build", "agent", "read", build))
    execution = WorkflowExecution(settings, registry)
    pb = workflow("prepare", "build")
    assert asyncio.run(execution.execute(pb, run_dir=tmp_path, state={}, fingerprint="batch")).status == "done"
    events.clear()

    def validate(step, outputs):
        events.append("validate")
        assert step == "build" and outputs["state_updates"]["accepted"] is True

    state = {}
    result = asyncio.run(execution.execute(pb, run_dir=tmp_path, state=state,
                                          fingerprint="batch", validate_cached=validate))
    assert result.status == "done" and events == ["preflight", "validate"]
    assert state["accepted"] is True
    assert set(json.loads((tmp_path / "progress.json").read_text())["completed"]) == {"build"}


@pytest.mark.parametrize("stored", ["other", None])
def test_fingerprint_mismatch_preserves_checkpoint_and_never_dispatches(settings, tmp_path, stored):
    progress = {"completed": {"build": {"outputs": {"state_updates": {"accepted": True}}}}}
    if stored:
        progress["fingerprint"] = stored
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress))
    original = path.read_bytes()
    registry = StepRegistry()

    async def forbidden(ctx):
        pytest.fail("unbound checkpoint must not dispatch")

    registry.register(StepSpec("build", "agent", "read", forbidden))
    state = {}
    result = asyncio.run(WorkflowExecution(settings, registry).execute(
        workflow("build"), run_dir=tmp_path, state=state, fingerprint="batch"))
    assert result.status == "blocked" and "fingerprint" in result.blocked_reason
    assert "accepted" not in state and path.read_bytes() == original


def test_cached_validation_refuses_before_replaying_state(settings, tmp_path):
    path = tmp_path / "progress.json"
    path.write_text(json.dumps({"completed": {"build": {"outputs": {"state_updates": {"accepted": True}}}}}))
    registry = StepRegistry()

    async def handler(ctx):
        pytest.fail("corrupt cached artifact must not be regenerated")

    registry.register(StepSpec("build", "agent", "read", handler))

    def validate(step, outputs):
        raise ValueError("artifact missing")

    state = {}
    result = asyncio.run(WorkflowExecution(settings, registry).execute(
        workflow("build"), run_dir=tmp_path, state=state, validate_cached=validate))
    assert result.status == "blocked" and "artifact missing" in result.blocked_reason
    assert "accepted" not in state


@pytest.mark.parametrize("cached", [42, {"outputs": []}, {"outputs": {"state_updates": []}}])
def test_malformed_cached_result_blocks_without_dispatch(settings, tmp_path, cached):
    (tmp_path / "progress.json").write_text(json.dumps({"completed": {"build": cached}}))
    registry = StepRegistry()

    async def handler(ctx):
        pytest.fail("malformed checkpoints cannot authorize dispatch")

    registry.register(StepSpec("build", "agent", "read", handler))
    result = asyncio.run(WorkflowExecution(settings, registry).execute(
        workflow("build"), run_dir=tmp_path, state={}))
    assert result.status == "blocked" and "invalid" in result.blocked_reason


def test_supplied_lock_stays_owned_and_finalizers_run(settings, tmp_path):
    registry, finalized = StepRegistry(), []

    async def fail(ctx):
        async def finish(outcome):
            finalized.append(outcome.status)
        register_finalizer(ctx.run_dir, finish)
        raise ValueError("failed")

    registry.register(StepSpec("fail", "deterministic", "read", fail))
    with RunLock(tmp_path) as held:
        result = asyncio.run(WorkflowExecution(settings, registry).execute(
            workflow("fail"), run_dir=tmp_path, state={}, held_lock=held))
        assert result.status == "blocked" and finalized == ["blocked"]
        with pytest.raises(RunLockHeld):
            RunLock(tmp_path).acquire()
    RunLock(tmp_path).acquire().release()


def test_partial_output_continues_without_a_completed_checkpoint(settings, tmp_path):
    registry, visits = StepRegistry(), []

    async def build(ctx):
        visits.append(ctx.item)
        return StepResult(True, outputs={"state_updates": {"preview": True}},
                          checkpoint=ctx.item != "unfinished")

    async def inspect(ctx):
        assert ctx.state["preview"] is True
        return StepResult(True, checkpoint=False)

    registry.register(StepSpec("build", "agent", "read", build))
    registry.register(StepSpec("inspect", "validation", "read", inspect))
    pb = workflow("build", "inspect")
    pb.steps[0].foreach = "items"
    execution = WorkflowExecution(settings, registry)
    for _ in range(2):
        result = asyncio.run(execution.execute(pb, run_dir=tmp_path,
                                               state={"items": ["accepted", "unfinished"]}))
        assert result.status == "done"
    assert visits == ["accepted", "unfinished", "accepted", "unfinished"]
    assert not (tmp_path / "progress.json").exists()


def test_concurrent_shadow_and_regular_runs_keep_agent_policy_local(settings, tmp_path):
    from infermatrix_copilot.scopes import read_only_scope

    registry = StepRegistry()
    seen = {}
    async def handler(ctx):
        await asyncio.sleep(0)
        policy = ctx.agent_policy
        seen[ctx.state["label"]] = policy
        if policy.transform_scope:
            scope = policy.transform_scope(read_only_scope(), ctx, "inspect#lens")
            assert scope.name.startswith("shadow:")
        return StepResult(True)
    registry.register(StepSpec("inspect", "agent", "read", handler))
    async def run():
        return await asyncio.gather(*[
            WorkflowExecution(settings.model_copy(update={"improve_shadow":shadow}), registry).execute(
                workflow("inspect"), run_dir=tmp_path / label, state={"label":label})
            for label, shadow in [("shadow", True), ("regular", False)]
        ])
    assert all(r.status == "done" for r in asyncio.run(run()))
    assert seen["shadow"].record_skill_usage is False
    assert seen["shadow"].repo_map_cache is not None
    assert seen["regular"].record_skill_usage is True
    assert seen["regular"].transform_scope is None
    assert seen["regular"].repo_map_cache is None


def test_rebase_guard_context_preserves_injected_policies(settings, trace, tmp_path, monkeypatch):
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3, workspace
    from infermatrix_copilot.improve.execution import shadow_agent_policy

    ctx = StepContext(settings, {}, {"custom":True}, tmp_path, trace,
                      runtime=object(), agent_policy=shadow_agent_policy())
    monkeypatch.setattr(rebase_v3, "_adapter_manifest", lambda _: {"rebase":{"guard":{}}})
    monkeypatch.setattr(rebase_v3, "_ensure_checkout_locks", lambda *a: None)
    async def guard(inner):
        assert inner is not ctx
        assert inner.agent_policy is ctx.agent_policy
        assert inner.runtime is ctx.runtime
        assert inner.params["custom"] is True
        assert inner.params["abort_stale_state"] is True
        return StepResult(True)
    monkeypatch.setattr(workspace, "_guard_clean_rebase", guard)
    assert asyncio.run(rebase_v3._v3_guard(ctx)).ok


def test_shadow_review_worker_injects_policy_without_workflow_wrapper(settings, tmp_path, monkeypatch):
    from pathlib import Path
    from infermatrix_copilot.improve import drivers
    from infermatrix_copilot.engine.steps.review import steps

    def local_path(value):
        p = Path(value)
        if p.is_absolute():
            assert p.parts[1] in {"tmp", "candidate"}
            return tmp_path.joinpath(*p.parts[1:])
        return p
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(drivers, "Path", local_path)
    seen = []
    async def review(ctx):
        seen.append(ctx.agent_policy)
        assert not ctx.agent_policy.record_skill_usage
        assert ctx.agent_policy.transform_scope is not None
        assert ctx.agent_policy.repo_map_cache(ctx) == ctx.run_dir / "repo_map"
        return StepResult(True)
    monkeypatch.setattr(steps, "_review_diff", review)
    result = drivers.worker_run({"driver":"pr-review", "input":{
        "base_files":{}, "head_files":{}, "diff":"", "repo":"demo", "pr":1}}, settings, None)
    assert result["review_comments"] == []
    assert len(seen) == 1
