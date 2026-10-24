"""The common executor must never replace maintenance's durable domain state."""

import asyncio
import json

import pytest

from infermatrix_copilot.engine.lifecycle import RunLock
from infermatrix_copilot.kb_service import maintenance
from infermatrix_copilot.kb_service.ledger import LeaseError

from test_kb_maintenance_flow import configured_runtime, standard_answer


@pytest.fixture
def runtime(tmp_path):
    return configured_runtime(tmp_path, standard_answer)[0]


def test_async_maintenance_rechecks_both_phases_on_every_invocation(runtime, monkeypatch):
    visits = []
    monkeypatch.setattr(maintenance, "advance_corrections", lambda rt: visits.append("correction") or [])
    monkeypatch.setattr(maintenance, "tick", lambda rt: visits.append("audit") or {"status": "complete"})

    async def run():
        first = await maintenance.run_due_async(runtime)
        second = await maintenance.run_due_async(runtime)
        return first, second

    first, second = asyncio.run(run())
    assert first == second == {"corrections": [], "maintenance": {"status": "complete"}}
    assert visits == ["correction", "audit", "correction", "audit"]
    run_dirs = list((runtime.state_dir / "runs" / "knowledge-maintenance").iterdir())
    assert len(run_dirs) == 1
    # Neither phase may publish executor checkpoints that could bypass the
    # next SQLite lease, applicability, budget or source check.
    assert not (run_dirs[0] / "progress.json").exists()
    trace = [json.loads(line) for line in (run_dirs[0] / "run_trace.jsonl").read_text().splitlines()]
    results = [event for event in trace if event["kind"] == "step_result"]
    assert [event["spec"] for event in results] == [
        "knowledge.maintain.correction", "knowledge.maintain.nightly_audit"] * 2
    assert all(event["ok"] for event in results)
    assert not runtime.gateway.calls


def test_failed_audit_preserves_original_exception_and_correction_callback(runtime, monkeypatch):
    failure, recorded = RuntimeError("source unavailable"), []
    event = {"status": "awaiting_consumer_ack"}
    monkeypatch.setattr(maintenance, "advance_corrections", lambda rt: [event])

    def fail(rt):
        assert recorded == [event]
        raise failure

    monkeypatch.setattr(maintenance, "tick", fail)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(maintenance.run_due_async(runtime, on_correction=recorded.append))
    assert caught.value is failure and recorded == [event]


def test_lost_lease_between_phases_prevents_audit(runtime, monkeypatch):
    monkeypatch.setattr(maintenance, "advance_corrections", lambda rt: [{"status": "complete"}])
    monkeypatch.setattr(maintenance, "tick", lambda rt: pytest.fail("audit ran after lease loss"))
    with pytest.raises(LeaseError):
        maintenance.run_due(runtime, on_correction=lambda event: runtime.ledger.release_lease(runtime.lease_owner))


def test_common_run_lock_refuses_duplicate_before_domain_work(runtime, monkeypatch):
    monkeypatch.setattr(maintenance, "advance_corrections", lambda rt: [])
    monkeypatch.setattr(maintenance, "tick", lambda rt: {"status": "complete"})
    maintenance.run_due(runtime)
    run_dir, = (runtime.state_dir / "runs" / "knowledge-maintenance").iterdir()
    original = (run_dir / "run_trace.jsonl").read_bytes()
    monkeypatch.setattr(maintenance, "advance_corrections", lambda rt: pytest.fail("duplicate correction"))
    with RunLock(run_dir):
        with pytest.raises(RuntimeError, match="run lock.*held"):
            maintenance.run_due(runtime)
    assert (run_dir / "run_trace.jsonl").read_bytes() == original
