import asyncio

import pytest

from infermatrix_copilot.engine import Executor, FailureKind, StepRegistry, StepResult, StepSpec
from infermatrix_copilot.notify import Notifier
from infermatrix_copilot.playbooks.store import Playbook, PlaybookStep


def make_step(name, fn, risk="read"):
    async def handler(ctx):
        return fn(ctx)
    return StepSpec(name, "deterministic", risk, handler)


def playbook(steps, name="test-pb"):
    return Playbook(name=name, version=1, status="active", task_kinds=["pr_review"],
                    repos=[], steps=steps)


@pytest.fixture()
def env(settings, trace, tmp_path):
    run_dir = tmp_path / "run"
    registry = StepRegistry()
    notifier = Notifier(settings, run_dir, trace, "run-test")
    executor = Executor(registry, settings, run_dir=run_dir, trace=trace,
                        notifier=notifier)
    return registry, executor, notifier


def test_sequential_execution_and_outputs(env):
    registry, executor, _ = env
    calls = []
    registry.register(make_step("s.one", lambda ctx: (calls.append("one"),
                       StepResult(True, summary="one done", outputs={"v": 1}))[1]))
    registry.register(make_step("s.two", lambda ctx: (calls.append("two"),
                       StepResult(True, summary=f"got {ctx.state['outputs']['a']['v']}"))[1]))
    pb = playbook([PlaybookStep("a", "s.one"), PlaybookStep("b", "s.two")])
    outcome = asyncio.run(executor.run(pb, {}))
    assert outcome.status == "done"
    assert calls == ["one", "two"]
    assert outcome.step_results["b"].summary == "got 1"


def test_mutating_foreach_owns_shared_checkout_one_item_at_a_time(env):
    registry, executor, _ = env
    active = 0
    peak = 0
    seen = []

    async def edit(ctx):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        seen.append(ctx.item)
        active -= 1
        return StepResult(True, summary="edited")

    registry.register(StepSpec("s.edit", "agent", "write_workspace", edit))
    result = asyncio.run(executor.run(
        playbook([PlaybookStep("edit", "s.edit", foreach="groups")]),
        {"groups": ["first", "second"]},
    ))
    assert result.status == "done"
    assert seen == ["first", "second"] and peak == 1


def test_task_params_reach_steps(env):
    """`--task-param K=V` lands on the TaskSpec; it must also reach a step's
    `ctx.params`, which previously only ever saw the playbook's step params."""
    registry, executor, _ = env
    seen = {}
    registry.register(make_step("s.one", lambda ctx: (seen.update(ctx.params),
                       StepResult(True, summary="ok"))[1]))
    pb = playbook([PlaybookStep("a", "s.one")])
    outcome = asyncio.run(executor.run(
        pb, {"task_spec": {"params": {"limit": 5}}}))
    assert outcome.status == "done"
    assert seen["limit"] == 5


def test_playbook_step_params_override_task_params(env):
    """Step params are authored invariants — several are safety-bearing
    (`force_push`, `pre_push`) — so a global task param must not flip them."""
    registry, executor, _ = env
    seen = {}
    registry.register(make_step("s.one", lambda ctx: (seen.update(ctx.params),
                       StepResult(True, summary="ok"))[1]))
    pb = playbook([PlaybookStep("a", "s.one", params={"force_push": False})])
    outcome = asyncio.run(executor.run(
        pb, {"task_spec": {"params": {"force_push": True, "limit": 5}}}))
    assert outcome.status == "done"
    assert seen["force_push"] is False   # step wins
    assert seen["limit"] == 5            # non-conflicting task param still flows


def test_missing_task_spec_params_is_not_fatal(env):
    registry, executor, _ = env
    seen = {}
    registry.register(make_step("s.one", lambda ctx: (seen.update({"n": len(ctx.params)}),
                       StepResult(True, summary="ok"))[1]))
    pb = playbook([PlaybookStep("a", "s.one")])
    assert asyncio.run(executor.run(pb, {})).status == "done"
    assert seen["n"] == 0


def test_checkpoint_resume_skips_completed(env):
    registry, executor, _ = env
    counter = {"n": 0}

    def flaky(ctx):
        counter["n"] += 1
        if counter["n"] == 1:
            return StepResult(False, FailureKind.TEST_FAILURE, "boom")
        return StepResult(True, summary="ok")

    registry.register(make_step("s.stable", lambda ctx: StepResult(True, summary="stable")))
    registry.register(make_step("s.flaky", flaky))
    pb = playbook([PlaybookStep("a", "s.stable"), PlaybookStep("b", "s.flaky")])

    first = asyncio.run(executor.run(pb, {}))
    assert first.status == "failed"
    second = asyncio.run(executor.run(pb, {}))
    assert second.status == "done"
    assert second.step_results["a"].summary == "stable"  # cached summary, not re-run
    assert counter["n"] == 2  # flaky ran twice, stable once


def test_rerun_from_reexecutes_that_step_and_later_ones(env):
    registry, executor, _ = env
    calls = []
    for name in ("one", "two", "three"):
        registry.register(make_step(f"s.{name}", lambda ctx, n=name: (
            calls.append(n), StepResult(True, summary=n))[1]))
    pb = playbook([PlaybookStep("a", "s.one"), PlaybookStep("b", "s.two"),
                   PlaybookStep("c", "s.three")])
    assert asyncio.run(executor.run(pb, {})).status == "done"
    calls.clear()

    outcome = asyncio.run(executor.run(pb, {"rerun_from": "b"}))
    assert outcome.status == "done"
    assert calls == ["two", "three"]
    calls.clear()
    assert asyncio.run(executor.run(pb, {})).status == "done"
    assert calls == []


def test_rerun_from_unknown_step_blocks_without_touching_checkpoint(env):
    registry, executor, _ = env
    registry.register(make_step("s.one", lambda ctx: StepResult(True, summary="one")))
    pb = playbook([PlaybookStep("a", "s.one")])
    assert asyncio.run(executor.run(pb, {})).status == "done"
    before = executor.progress_file.read_text(encoding="utf-8")

    outcome = asyncio.run(executor.run(pb, {"rerun_from": "missing"}))
    assert outcome.status == "blocked"
    assert "missing" in outcome.blocked_reason
    assert executor.progress_file.read_text(encoding="utf-8") == before


def test_retryable_is_retried(env):
    registry, executor, _ = env
    attempts = {"n": 0}

    def transient(ctx):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return StepResult(False, FailureKind.RETRYABLE, "transient")
        return StepResult(True, summary="recovered")

    registry.register(make_step("s.transient", transient))
    outcome = asyncio.run(executor.run(playbook([PlaybookStep("a", "s.transient")]), {}))
    assert outcome.status == "done" and attempts["n"] == 2


def test_blocked_escalates(env, tmp_path):
    registry, executor, notifier = env
    registry.register(make_step(
        "s.blocked", lambda ctx: StepResult(False, FailureKind.BLOCKED, "no gpu")))
    outcome = asyncio.run(executor.run(playbook([PlaybookStep("a", "s.blocked")]), {}))
    assert outcome.status == "blocked"
    assert len(notifier.sent) == 1 and "no gpu" in notifier.sent[0].reason
    assert (tmp_path / "run" / "ESCALATION.md").exists()


def test_resolved_escalation_moves_to_history(env, tmp_path):
    registry, executor, notifier = env
    registry.register(make_step(
        "s.blocked", lambda ctx: StepResult(
            False, FailureKind.BLOCKED, "temporary operator action")))
    asyncio.run(executor.run(playbook([PlaybookStep("a", "s.blocked")]), {}))

    assert notifier.resolve("resumed workflow completed") is True
    assert not (tmp_path / "run" / "ESCALATION.md").exists()
    history = (tmp_path / "run" / "ESCALATION_HISTORY.md").read_text()
    assert "temporary operator action" in history
    assert "resumed workflow completed" in history
    assert notifier.resolve() is False


def test_escalation_summary_and_artifacts_forwarded(env):
    registry, executor, notifier = env
    registry.register(make_step(
        "s.rich_fail", lambda ctx: StepResult(
            False, FailureKind.ESCALATE, "module failed",
            outputs={"escalation_summary": {"module": "scheduler", "exit_code": 1},
                     "artifacts": ["/logs/module-scheduler.log"]})))
    outcome = asyncio.run(executor.run(playbook([PlaybookStep("a", "s.rich_fail")]), {}))
    assert outcome.status == "blocked"
    esc = notifier.sent[0]
    assert esc.state_summary["module"] == "scheduler"
    assert "/logs/module-scheduler.log" in esc.artifacts


def test_foreach_fanout(env):
    registry, executor, _ = env
    seen = []
    registry.register(make_step(
        "s.item", lambda ctx: (seen.append(ctx.item),
                               StepResult(True, summary=str(ctx.item)))[1]))
    pb = playbook([PlaybookStep("a", "s.item", foreach="modules")])
    outcome = asyncio.run(executor.run(pb, {"modules": ["m1", "m2", "m3"]}))
    assert outcome.status == "done"
    assert sorted(seen) == ["m1", "m2", "m3"]
    assert "all 3 items ok" in outcome.step_results["a"].summary


def test_handler_exception_becomes_blocked(env):
    registry, executor, _ = env

    def boom(ctx):
        raise RuntimeError("bug in handler")

    registry.register(make_step("s.boom", boom))
    outcome = asyncio.run(executor.run(playbook([PlaybookStep("a", "s.boom")]), {}))
    assert outcome.status == "blocked"
    assert "bug in handler" in outcome.blocked_reason


def test_step_span_records_step_id_and_foreach_item(env, tmp_path):
    """A step span must say which playbook step and which foreach item it was:
    repo-rebase-v3 runs its module-rebase spec for BOTH waves and fans each
    out, so `step` alone cannot tell those spans apart."""
    import infermatrix_copilot.tracing as tracing
    registry, executor, _ = env
    path = tmp_path / "trace.jsonl"
    tracing.init("run-fan", path)
    try:
        registry.register(make_step("s.mod", lambda ctx: StepResult(True, summary="ok")))
        pb = playbook([PlaybookStep("wave1", "s.mod", foreach="modules")])
        outcome = asyncio.run(executor.run(
            pb, {"modules": ["model_executor", "scheduler"]}))
        assert outcome.status == "done"
        steps = [s for s in tracing.load_spans(path) if s["name"] == "step"]
        assert len(steps) == 2
        assert {s["attr"]["step_id"] for s in steps} == {"wave1"}
        assert {s["attr"]["item"] for s in steps} == {"model_executor", "scheduler"}
    finally:
        tracing.init("noop", tmp_path / "discard.jsonl")


def test_step_span_omits_item_when_not_foreach(env, tmp_path):
    import infermatrix_copilot.tracing as tracing
    registry, executor, _ = env
    path = tmp_path / "trace.jsonl"
    tracing.init("run-plain", path)
    try:
        registry.register(make_step("s.one", lambda ctx: StepResult(True, summary="ok")))
        asyncio.run(executor.run(playbook([PlaybookStep("fetch", "s.one")]), {}))
        step = [s for s in tracing.load_spans(path) if s["name"] == "step"][0]
        assert step["attr"]["step_id"] == "fetch"
        assert "item" not in step["attr"]
    finally:
        tracing.init("noop", tmp_path / "discard.jsonl")


def test_item_key_shortens_dicts_and_strings():
    from infermatrix_copilot.engine.executor import _item_key
    assert _item_key("model_executor") == "model_executor"
    assert _item_key({"name": "grp-3", "detail": "x" * 999}) == "grp-3"
    assert _item_key({"id": 7}) == "7"
    assert len(_item_key("z" * 500)) == 80
    assert len(_item_key({"unknown": "y" * 500})) == 80   # falls back to str()
