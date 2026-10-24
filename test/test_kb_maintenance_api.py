"""Lifecycle entry points preserve single-writer execution and policy bindings."""
from dataclasses import replace

import pytest

from infermatrix_copilot.kb_service import maintenance, maintenance_policy
from infermatrix_copilot.kb_service.ledger import LeaseError
from infermatrix_copilot.kb_service.maintenance_store import MaintenanceStore

from test_kb_maintenance_flow import configured_runtime, standard_answer


@pytest.fixture
def runtime(tmp_path):
    return configured_runtime(tmp_path, standard_answer)[0]


def test_request_and_status_do_not_execute_or_take_over_lease(runtime):
    rt = runtime
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    owner = rt.ledger.live_lease()
    rt.lease_owner = None
    first = maintenance.request(rt, "manual", ["demo"])
    assert maintenance.request(rt, "manual", ["demo"]) == first
    with pytest.raises(ValueError):
        maintenance.request(rt, "manual", ["demo"], options={"calibrate": True})
    report = maintenance.status(rt, ["demo"])
    assert report["snapshot"] == rt.ledger.active_snapshot()
    assert report["requests"][0]["detail"] == {"kind": "maintenance", "calibrate": False, "drill": False}
    assert report["runs"] == [] and report["findings"] == []
    assert not rt.gateway.calls and not store.runs()
    assert rt.ledger.live_lease() == owner


@pytest.mark.parametrize("repos", [[], ["demo", "demo"], ["missing"]])
def test_request_refuses_ambiguous_or_unknown_scope(runtime, repos):
    with pytest.raises(ValueError):
        maintenance.request(runtime, "invalid", repos)
    assert not MaintenanceStore(runtime.ledger).pending_requests()


@pytest.mark.parametrize("repository_state", ["disabled", "removed"])
def test_owner_disposition_can_be_queued_for_historical_repository(runtime, repository_state):
    rt = runtime
    if repository_state == "disabled":
        rt.registry["demo"] = replace(rt.registry["demo"], enabled=False)
    else:
        del rt.registry["demo"]
    with pytest.raises(ValueError, match="disabled|unknown"):
        maintenance.request(rt, "normal", ["demo"])
    # Verification remains the scheduler's responsibility; enqueueing grants
    # neither an owner identity nor permission to append a resolution.
    queued = maintenance.request(rt, "owner", ["demo"], options={"resolution": {"signed": "request"}})
    assert queued["detail"] == {"resolution": {"signed": "request"}}
    assert not rt.gateway.calls


def test_run_due_advances_corrections_before_review(runtime, monkeypatch):
    order = []
    def corrections(rt):
        order.append("correction")
        return [{"status": "pending"}]
    def review(rt):
        assert order == ["correction"]
        order.append("review")
        return {"status": "complete"}
    monkeypatch.setattr(maintenance, "advance_corrections", corrections)
    monkeypatch.setattr(maintenance, "tick", review)
    assert maintenance.run_due(runtime) == {"corrections": [{"status": "pending"}],
                                           "maintenance": {"status": "complete"}}
    assert order == ["correction", "review"]


def test_review_failure_does_not_hide_completed_correction_events(runtime, monkeypatch):
    recorded = []
    event = {"status": "new_content_restored_pending_ack"}
    monkeypatch.setattr(maintenance, "advance_corrections", lambda rt: [event])
    def fail_review(rt):
        assert recorded == [event]
        raise RuntimeError("source unavailable")
    monkeypatch.setattr(maintenance, "tick", fail_review)
    with pytest.raises(RuntimeError, match="source unavailable"):
        maintenance.run_due(runtime, on_correction=recorded.append)
    assert recorded == [event]


@pytest.mark.parametrize("lease", ["missing", "lost", "expired"])
def test_run_due_refuses_invalid_lease_before_any_progress(runtime, monkeypatch, lease):
    rt = runtime
    if lease == "missing":
        rt.lease_owner = None
    elif lease == "lost":
        rt.ledger.release_lease(rt.lease_owner)
    else:
        now = rt.clock()
        rt.ledger._clock = lambda: now + 901
    monkeypatch.setattr(maintenance, "advance_corrections", lambda rt: pytest.fail("correction ran without lease"))
    monkeypatch.setattr(maintenance, "tick", lambda rt: pytest.fail("audit ran without lease"))
    with pytest.raises((ValueError, LeaseError)):
        maintenance.run_due(rt)


@pytest.mark.parametrize('source', ['maintenance_resolution.py', 'gate_receipts.py', 'ledger.py', 'companion.py'])
def test_extracted_resolution_implementation_invalidates_prior_policy(runtime, monkeypatch, source):
    original_policy = maintenance_policy.policy_digest(runtime)
    from pathlib import Path
    original_read = Path.read_text
    def changed_read(path, *args, **kwargs):
        text = original_read(path, *args, **kwargs)
        return text + "\n# changed workflow implementation\n" if path.name == source else text
    monkeypatch.setattr(Path, "read_text", changed_read)
    assert maintenance_policy.policy_digest(runtime) != original_policy
    assert maintenance_policy.correction_publishable(runtime, {"detail": {"maintenance_policy": original_policy}}) \
        == "maintenance policy changed or disabled"


def test_changed_policy_cancels_old_cycle_without_rewriting_observations(runtime, monkeypatch):
    rt = runtime
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    snapshot = rt.ledger.active_snapshot()
    units = maintenance._units(rt, snapshot)[1]
    run = store.begin_cycle(snapshot=snapshot, policy_sha256="prior-policy", now=rt.clock(), eligible_repos=["demo"])
    store.add_items(run["id"], units, now=rt.clock())
    # Retain a completed observation plus an unfinished item in the old cycle.
    completed = {**units[0], "unit_id": "prior-observation"}
    store.add_items(run["id"], [completed], now=rt.clock())
    store.record_outcome(run["id"], completed["unit_id"], "verified", detail={"reason": "original"}, now=rt.clock())
    before = store.findings()
    result = maintenance.tick(rt)
    assert result["status"] == "policy_changed"
    assert store.run(run["id"])["status"] == "cancelled"
    assert store.findings() == before and not rt.gateway.calls
