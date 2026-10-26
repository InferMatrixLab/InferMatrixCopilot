"""Workflow policy is part of generation authority, including legacy refusal."""
from copy import deepcopy
import hashlib
import json

import pytest

from infermatrix_copilot.kb_service import init_execution
from infermatrix_copilot.kb_service.init_stages import run_stage
from infermatrix_copilot.kb_service.init_support import InitError
from test_history_extraction_checkpoint import runtime, _modules_lifecycle
from test_kb_init_skeleton import FakeGateway, _runtime, _lifecycle, world  # noqa: F401


def remove_policy(record):
    for state in record.coverage['generation_execution'].values():
        if 'generated' in state:
            state['generated'].pop('execution_policy', None)
        proof = state.get('generation_dispatched_binding')
        if proof:
            proof['inputs'].pop('execution_policy', None)
            proof['sha256'] = hashlib.sha256(json.dumps(proof['inputs'], sort_keys=True,
                                                       ensure_ascii=False).encode()).hexdigest()


def test_unknown_call_cannot_escape_by_changing_workflow_policy(world, monkeypatch):
    rt, calls, store = runtime(world)
    monkeypatch.setattr(init_execution, 'policy_digest', lambda: 'policy-A')
    first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    states = deepcopy(first.coverage['generation_execution'])
    assert states['history:101']['generation_dispatched_binding']['inputs']['execution_policy'] == 'policy-A'
    monkeypatch.setattr(init_execution, 'policy_digest', lambda: 'policy-B')
    again = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert again.status == 'blocked' and calls == [101]
    assert again.coverage['generation_execution'] == states
    assert len(list(store.iter_records(kind='model_call'))) == 1


def test_unbound_legacy_unknown_refuses_before_checkpoint_mutation(world):
    rt, calls, _ = runtime(world)
    first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    remove_policy(first)
    path = first.save(rt.state_dir); before = path.read_bytes()
    with pytest.raises(InitError, match='legacy generation has no workflow policy binding'):
        run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert calls == [101] and path.read_bytes() == before


def test_unbound_legacy_completed_generation_does_not_repay_after_interruption(world, monkeypatch):
    gateway = FakeGateway()
    rt = _runtime(world, gateway)
    monkeypatch.setattr(init_execution, 'validate', lambda *_: (_ for _ in ()).throw(RuntimeError('interrupted')))
    first = run_stage(rt, _lifecycle(), 'skeleton', dry_run=True)
    assert first.status == 'blocked' and first.evidence
    remove_policy(first)
    path = first.save(rt.state_dir); before = path.read_bytes(); count = len(gateway.calls)
    with pytest.raises(InitError, match='legacy generation has no workflow policy binding'):
        run_stage(rt, _lifecycle(), 'skeleton', dry_run=True)
    assert len(gateway.calls) == count and path.read_bytes() == before


def test_legacy_known_predispatch_refusal_can_bind_current_policy(world):
    rt, calls, _ = runtime(world, unknown=False)
    first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert first.status == 'blocked' and calls == []
    remove_policy(first); first.save(rt.state_dir)
    again = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert again.status == 'empty' and calls == [101, 102, 103]
    assert all(row['generated']['execution_policy'] == init_execution.policy_digest()
               for row in again.coverage['generation_execution'].values())
