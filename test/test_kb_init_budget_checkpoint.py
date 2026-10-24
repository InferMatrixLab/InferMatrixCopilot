"""Crash-safe checkpoints share Budget's accounting lock, not model execution."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service.init_budget import Budget
from infermatrix_copilot.kb_service.init_feature_discovery import _discovery_budget, _FeatureDiscovery
from infermatrix_copilot.kb_service.init_history import _checkpoint_budget
from infermatrix_copilot.kb_service.init_support import InitRecord


def test_checkpoint_precedes_dispatch_and_records_settlement():
    saved = []
    budget = Budget(2, spent_usd=.125, checkpoint=lambda spent, reserved: saved.append((spent, reserved)))
    with budget.reserve(.5) as reservation:
        assert saved == [(.125, .5)]
        assert budget.remaining_usd == 1.375
        reservation.charge(.25)
    assert saved == [(.125, .5), (.375, 0)]
    assert budget.spent_usd == .375


def test_parallel_calls_share_checkpoints_without_holding_model_execution_lock():
    saved = []
    both_models_running = Barrier(2, timeout=5)
    budget = Budget(1, checkpoint=lambda spent, reserved: saved.append((spent, reserved)))

    def model(cost):
        with budget.reserve(.5) as reservation:
            # This barrier would time out if a model body held the budget lock.
            both_models_running.wait()
            reservation.charge(cost)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(model, cost) for cost in (.125, .25)]
        for future in futures:
            future.result(timeout=10)
    assert saved[:2] == [(0, .5), (0, 1)]
    assert saved[-1] == (.375, 0)
    assert len(saved) == 4
    assert budget.remaining_usd == .625


@pytest.mark.parametrize("limit", [1, None])
@pytest.mark.parametrize("result", ["unknown", "error", "overrun"])
def test_unknown_failure_and_actual_overrun_keep_existing_accounting(limit, result):
    saved = []
    budget = Budget(limit, checkpoint=lambda spent, reserved: saved.append((spent, reserved)))

    def model():
        with budget.reserve(.5) as reservation:
            if result == "error":
                raise RuntimeError("model unavailable")
            reservation.charge(None if result == "unknown" else .75)

    if result == "error":
        with pytest.raises(RuntimeError, match="model unavailable"):
            model()
    else:
        model()
    charged = .75 if result == "overrun" else .5
    assert saved == [(0, .5), (charged, 0)]
    assert budget.spent_usd == charged
    assert budget.remaining_usd == (None if limit is None else limit - charged)


@pytest.mark.parametrize("write_before_failure", [False, True])
def test_failed_predispatch_checkpoint_never_calls_model_or_frees_allowance(tmp_path, write_before_failure):
    record = InitRecord(stage="pr-history", repo="demo")
    original_save = record.save
    attempts = []

    def failing_save(state_dir):
        if write_before_failure:
            original_save(state_dir)
        raise OSError("checkpoint unavailable")

    record.save = failing_save
    budget = _checkpoint_budget(1, record, tmp_path)
    with pytest.raises(OSError, match="checkpoint unavailable"):
        with budget.reserve(.5):
            attempts.append("model")
    assert attempts == []
    assert budget.spent_usd == .5
    assert budget.remaining_usd == .5
    # The enclosing stage's final save must not cancel a possibly written hold.
    del record.save
    record.spent_usd = budget.spent_usd
    record.save(tmp_path)
    assert InitRecord.load(tmp_path, "demo", "pr-history").spent_usd == .5
    with pytest.raises(RuntimeError, match="recover durable state"):
        with budget.reserve(.125):
            attempts.append("unaccounted retry")
    assert attempts == []


@pytest.mark.parametrize("write_before_failure", [False, True])
@pytest.mark.parametrize("actual", [.125, .75])
def test_failed_settlement_keeps_full_reservation_or_actual_overrun(tmp_path, write_before_failure, actual):
    record = InitRecord(stage="knowledge-deepen", repo="demo")
    original_save = record.save
    saves = []
    model_calls = []

    def failing_settlement(state_dir):
        saves.append(record.spent_usd)
        if len(saves) == 1 or write_before_failure:
            original_save(state_dir)
        if len(saves) == 2:
            raise OSError("settlement unavailable")

    record.save = failing_settlement
    budget = _checkpoint_budget(1, record, tmp_path)
    with pytest.raises(OSError, match="settlement unavailable"):
        with budget.reserve(.5) as reservation:
            model_calls.append("model")
            reservation.charge(actual)
    assert model_calls == ["model"]
    assert budget.spent_usd == max(.5, actual)
    with pytest.raises(RuntimeError, match="recover durable state"):
        with budget.reserve(.125):
            model_calls.append("unaccounted retry")
    assert model_calls == ["model"]
    if not write_before_failure:
        # The original durable reservation survives a failed settlement write.
        assert InitRecord.load(tmp_path, "demo", "knowledge-deepen").spent_usd == .5
    del record.save
    record.spent_usd = budget.spent_usd
    record.save(tmp_path)
    recovered = InitRecord.load(tmp_path, "demo", "knowledge-deepen")
    assert recovered.spent_usd == max(.5, actual)
    assert _checkpoint_budget(1, recovered, tmp_path).remaining_usd == 1 - max(.5, actual)


def test_init_record_format_charges_inflight_reservation_on_recovery(tmp_path):
    record = InitRecord(stage="pr-history", repo="demo", spent_usd=.125)
    budget = _checkpoint_budget(1, record, tmp_path)
    with budget.reserve(.5) as reservation:
        saved = InitRecord.load(tmp_path, "demo", "pr-history")
        assert saved.spent_usd == .625
        assert _checkpoint_budget(1, saved, tmp_path).remaining_usd == .375
        reservation.charge(.25)
    assert InitRecord.load(tmp_path, "demo", "pr-history").spent_usd == .375


def test_discovery_journal_preserves_accounting_fields_and_adds_call_identity(tmp_path):
    journal = tmp_path / "discovery-budget.json"
    budget = _discovery_budget(Budget(None, spent_usd=.125), journal, "frozen-batch")
    with budget.reserve(.5) as reservation:
        assert json.loads(journal.read_text()) == {
            "schema": "discovery-budget/2", "identity": "frozen-batch", "spent_usd": .125, "reserved_usd": .5,
            "calls": {reservation.identity: {"status": "reserved", "reserved_usd": .5, "accounted_usd": .5, "accounting": "actual"}},
        }
        reservation.charge(.25)
    assert json.loads(journal.read_text()) == {
        "schema": "discovery-budget/2", "identity": "frozen-batch", "spent_usd": .375, "reserved_usd": 0,
        "calls": {reservation.identity: {"status": "settled", "reserved_usd": .5, "accounted_usd": .25, "accounting": "actual"}},
    }


@pytest.mark.parametrize("record_spend, expected", [(.125, .625), (.75, .75)])
def test_discovery_resume_takes_maximum_of_record_and_journal_with_reservations(tmp_path, record_spend, expected):
    journal = tmp_path / "init" / "demo" / "discovery-budget.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"identity": "batch", "spent_usd": .125, "reserved_usd": .5}))
    previous = InitRecord(stage="feature-discovery", repo="demo", inputs_digest="inputs", spent_usd=record_spend,
                          discovery={"identity": "batch"})
    stage = SimpleNamespace(
        record=InitRecord(stage="feature-discovery", repo="demo", inputs_digest="inputs"), budget=Budget(1),
        rt=SimpleNamespace(state_dir=tmp_path), lifecycle=SimpleNamespace(repo="demo"),
    )
    assert _FeatureDiscovery._restore_progress(stage, previous) == []
    assert stage.budget.spent_usd == expected


def test_discovery_resume_rejects_another_batch_journal(tmp_path):
    journal = tmp_path / "init" / "demo" / "discovery-budget.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"identity": "another-batch", "spent_usd": .125, "reserved_usd": .5}))
    previous = InitRecord(stage="feature-discovery", repo="demo", inputs_digest="inputs",
                          discovery={"identity": "batch"})
    stage = SimpleNamespace(
        record=InitRecord(stage="feature-discovery", repo="demo", inputs_digest="inputs"), budget=Budget(1),
        rt=SimpleNamespace(state_dir=tmp_path), lifecycle=SimpleNamespace(repo="demo"),
    )
    assert _FeatureDiscovery._restore_progress(stage, previous) == ["discovery reservation journal identity differs"]


def test_discovery_failed_settlement_preserves_durable_outstanding_amount(tmp_path, monkeypatch):
    journal = tmp_path / "discovery-budget.json"
    budget = _discovery_budget(Budget(1), journal, "batch")
    from infermatrix_copilot.kb_service import init_feature_discovery
    original_write = init_feature_discovery.atomic_write_bytes
    writes = []

    def fail_second_write(path, data):
        writes.append(path)
        if len(writes) == 2:
            raise OSError("journal unavailable")
        return original_write(path, data)

    monkeypatch.setattr(init_feature_discovery, "atomic_write_bytes", fail_second_write)
    with pytest.raises(OSError, match="journal unavailable"):
        with budget.reserve(.5) as reservation:
            reservation.charge(.125)
    assert json.loads(journal.read_text()) == {"schema": "discovery-budget/2", "identity": "batch", "spent_usd": 0, "reserved_usd": .5,
        "calls": {reservation.identity: {"status": "reserved", "reserved_usd": .5, "accounted_usd": .5, "accounting": "actual"}}}
    assert budget.spent_usd == .5


def test_previously_active_call_can_settle_without_erasing_a_failed_reservation():
    saved = []

    def checkpoint(spent, reserved):
        saved.append((spent, reserved))
        if len(saved) == 2:
            raise OSError("second call reservation unavailable")

    budget = Budget(2, checkpoint=checkpoint)
    with budget.reserve(.5) as active:
        with pytest.raises(OSError, match="reservation unavailable"):
            with budget.reserve(.5):
                pytest.fail("failed checkpoint dispatched the second model")
        assert budget.spent_usd == .5
        active.charge(.125)
    # Successful settlement of the first call keeps the unknown second hold.
    assert saved[-1] == (.625, 0)
    assert budget.spent_usd == .625
    with pytest.raises(RuntimeError, match="recover durable state"):
        with budget.reserve(.125):
            pytest.fail("later successful checkpoint cleared the failure latch")
