"""A completed foundation worker must retain other workers' durable costs."""
import threading
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service import init_knowledge_parallel as parallel
from infermatrix_copilot.kb_service.init_budget import Budget
from infermatrix_copilot.kb_service.init_coverage import Owner
from infermatrix_copilot.kb_service.init_support import InitRecord, checkpoint_budget


def test_completed_worker_checkpoint_retains_another_workers_reservation(tmp_path, monkeypatch):
    record = InitRecord("knowledge", "toy", inputs_digest="same")
    budget = checkpoint_budget(None, record, tmp_path)
    second_reserved, coordinator_saved = threading.Event(), threading.Event()
    observed = []
    save = InitRecord.save

    def observe(current, state_dir):
        path = save(current, state_dir)
        if (threading.current_thread() is threading.main_thread()
                and second_reserved.is_set() and not coordinator_saved.is_set()):
            durable = InitRecord.load(state_dir, "toy", "knowledge")
            observed.append((durable.spent_usd, len(durable.coverage["foundation_jobs"]["tasks"])))
            coordinator_saved.set()
        return path

    def worker(stage, job):
        with budget.reserve(10.0) as reservation:
            if job["number"] == 0:
                assert second_reserved.wait(5)
            else:
                second_reserved.set()
                assert coordinator_saved.wait(5)
            reservation.charge(10.0)
        return {"input_sha256": str(job["number"]), "artifacts": [], "error": ""}

    monkeypatch.setattr(InitRecord, "save", observe)
    monkeypatch.setattr(parallel, "_worker", worker)
    monkeypatch.setattr(parallel, "_validate_fresh", lambda *_: None)
    monkeypatch.setattr(parallel, "_apply", lambda *_: [])
    stage = SimpleNamespace(record=record, budget=budget, rt=SimpleNamespace(
        unlimited_subscription=True, environ={"KB_KNOWLEDGE_CONCURRENCY": "2"},
        gateway=None, state_dir=tmp_path))
    jobs = [{"number": number, "owner": Owner(str(number), str(number), ()), "payload": {}, "offered": {}}
            for number in range(2)]
    list(parallel.run_jobs(stage, jobs))

    assert observed == [(20.0, 1)]  # $10 settled plus $10 unknown if interrupted here.
    assert InitRecord.load(tmp_path, "toy", "knowledge").spent_usd == 20.0


def test_failed_domain_checkpoint_requires_recovery_before_another_call():
    def unavailable(*_):
        raise OSError("durable checkpoint unavailable")

    budget = Budget(20.0, checkpoint=unavailable)
    with pytest.raises(OSError, match="durable checkpoint unavailable"):
        budget.checkpoint_now()
    with pytest.raises(RuntimeError, match="recover durable state"):
        with budget.reserve(10.0):
            pytest.fail("dispatch must be refused after a failed checkpoint")
