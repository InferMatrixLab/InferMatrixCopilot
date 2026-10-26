"""Offline crash/replay contracts across initialization's shared execution plan."""
import asyncio
import json
from pathlib import Path

import pytest

from infermatrix_copilot.kb_service import init_execution
from infermatrix_copilot.kb_service.init_stages import _Stage, run_stage, run_stage_async
from infermatrix_copilot.kb_service.init_support import InitError, InitRecord
from test_kb_init_skeleton import FakeGateway, _lifecycle, _runtime, world  # noqa: F401


def test_changed_completion_profile_keeps_original_batch_without_regeneration(world):
    gateway = FakeGateway()
    gateway.native_completion_profiles = {'codex': {'image': 'original'}}
    rt = _runtime(world, gateway)
    first = run_stage(rt, _lifecycle(), 'skeleton', dry_run=True)
    assert first.status == 'dry_run', first.problems
    path = InitRecord.path(rt.state_dir, 'toy', 'skeleton')
    original, calls = path.read_bytes(), len(gateway.calls)
    gateway.native_completion_profiles = {'codex': {'image': 'changed'}}
    with pytest.raises(InitError, match='profile differs from the original batch'):
        run_stage(rt, _lifecycle(), 'skeleton', dry_run=True)
    assert path.read_bytes() == original and len(gateway.calls) == calls


def progress(rt):
    paths = list((rt.state_dir / "init/toy/executions/skeleton").glob("*/progress.json"))
    assert len(paths) == 1
    return paths[0], json.loads(paths[0].read_text())


@pytest.mark.parametrize("boundary", ["validate", "prepare_publication"])
def test_completed_artifacts_resume_without_new_model_calls(world, monkeypatch, boundary):
    gateway = FakeGateway()
    rt = _runtime(world, gateway)
    owner, name = (init_execution, "validate") if boundary == "validate" else (_Stage, "_prepare_publication")
    original = getattr(owner, name)
    monkeypatch.setattr(owner, name, lambda *_: (_ for _ in ()).throw(RuntimeError("crash boundary")))
    first = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    assert first.status == "blocked" and "crash boundary" in first.problems[0]
    calls, spend = len(gateway.calls), first.spent_usd
    _, checkpoint = progress(rt)
    assert "prepare" not in checkpoint["completed"] and "publish" not in checkpoint["completed"]
    assert "draft" in checkpoint["completed"]
    monkeypatch.setattr(owner, name, original)
    final = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    assert final.status == "dry_run", final.problems
    assert len(gateway.calls) == calls and final.spent_usd == spend


def test_unbound_prepared_publication_resumes_validation_instead_of_publish(world, monkeypatch):
    gateway = FakeGateway()
    rt = _runtime(world, gateway)
    original = _Stage._prepare_publication
    def interrupted(work, files):
        original(work, files)
        raise RuntimeError("crashed before validation proof")
    monkeypatch.setattr(_Stage, "_prepare_publication", interrupted)
    first = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    assert first.pr["validation_pending"] is True
    calls = len(gateway.calls)
    monkeypatch.setattr(_Stage, "_prepare_publication", original)
    final = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    assert final.status == "dry_run", final.problems
    assert len(gateway.calls) == calls


@pytest.mark.parametrize("damage", ["missing", "content", "outside"])
def test_bad_draft_artifact_blocks_without_regeneration_or_losing_business_facts(world, monkeypatch, damage):
    gateway = FakeGateway()
    rt = _runtime(world, gateway)
    original = init_execution.validate
    monkeypatch.setattr(init_execution, "validate", lambda *_: (_ for _ in ()).throw(RuntimeError("validation interrupted")))
    first = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    calls = len(gateway.calls)
    path, checkpoint = progress(rt)
    ref = checkpoint["completed"]["draft"]["outputs"]["artifact"]
    artifact = path.parent / ref["path"]
    if damage == "missing":
        artifact.unlink()
    elif damage == "content":
        artifact.write_text("{}")
    else:
        ref["path"] = "../" + ref["path"]
        path.write_text(json.dumps(checkpoint))
    monkeypatch.setattr(init_execution, "validate", original)
    final = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    assert final.status == "blocked" and "cached result refused" in final.problems[0]
    assert len(gateway.calls) == calls
    assert final.evidence == first.evidence and final.verdicts == first.verdicts
    assert final.spent_usd == first.spent_usd


def test_batch_lock_is_shared_across_execution_controls(world):
    rt = _runtime(world)
    from infermatrix_copilot.kb_service.init_stages import _make_stage
    work = _make_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    with init_execution.batch_lock(work):
        with pytest.raises(InitError, match="already running"):
            run_stage(rt, _lifecycle(), "skeleton", dry_run=True, pin=world["pin"])
    assert not InitRecord.path(rt.state_dir, "toy", "skeleton").exists()


def test_async_entry_uses_executor_without_nested_event_loop(world):
    async def run():
        return await run_stage_async(_runtime(world), _lifecycle(), "skeleton", dry_run=True)
    record = asyncio.run(run())
    assert record.status == "dry_run", record.problems


def test_final_record_persistence_failure_propagates(world, monkeypatch):
    rt = _runtime(world)
    original = InitRecord.save
    broken = []
    def save(record, state_dir):
        if record.status == "dry_run":
            broken.append(True)
        if broken:
            raise OSError("durable record unavailable")
        return original(record, state_dir)
    monkeypatch.setattr(InitRecord, "save", save)
    with pytest.raises(OSError, match="durable record unavailable"):
        run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
