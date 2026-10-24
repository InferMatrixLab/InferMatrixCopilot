"""Original init reservation identities survive lost processes and checkpoints."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from infermatrix_copilot.kb_service.init_support import InitRecord, checkpoint_budget
from infermatrix_copilot.trace_store import current_context


def test_original_budget_identity_is_checkpointed_and_trace_context_restored(tmp_path):
    record = InitRecord(stage='pr-history', repo='demo', spent_usd=.125)
    budget = checkpoint_budget(2, record, tmp_path)
    with budget.reserve(.5) as reservation:
        loaded = InitRecord.load(tmp_path, 'demo', 'pr-history')
        assert loaded.spent_usd == .625
        assert loaded.budget_calls[reservation.identity] == {
            'status': 'reserved', 'reserved_usd': .5, 'accounted_usd': .5, 'accounting': 'actual'}
        assert current_context()['init_budget']['reservation_id'] == reservation.identity
        reservation.charge(.25)
    assert 'init_budget' not in current_context()
    loaded = InitRecord.load(tmp_path, 'demo', 'pr-history')
    assert loaded.spent_usd == .375
    assert loaded.budget_calls[reservation.identity] == {
        'status': 'settled', 'reserved_usd': .5, 'accounted_usd': .25, 'accounting': 'actual'}
    assert checkpoint_budget(2, loaded, tmp_path).spent_usd == .375


def test_legacy_aggregate_keeps_spend_without_fabricated_call_ids(tmp_path):
    record = InitRecord(stage='pr-history', repo='demo', spent_usd=.7)
    path = record.save(tmp_path)
    data = json.loads(path.read_text())
    del data['budget_calls']
    path.write_text(json.dumps(data))
    loaded = InitRecord.load(tmp_path, 'demo', 'pr-history')
    budget = checkpoint_budget(1, loaded, tmp_path)
    assert budget.spent_usd == .7 and loaded.budget_calls == {}


@pytest.mark.parametrize('calls', [None, [], {'bad': {}}, {'a' * 32: {'status': 'settled',
    'reserved_usd': .5, 'accounted_usd': -1}}])
def test_corrupt_original_call_journal_is_retained_without_dispatch(tmp_path, calls):
    record = InitRecord(stage='pr-history', repo='demo', budget_calls=calls)
    path = record.save(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='invalid init budget call journal'):
        checkpoint_budget(1, record, tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('account', ['record', 'discovery'])
def test_process_exit_preserves_same_reservation_in_record_and_native_archive(tmp_path, account):
    script = '''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from infermatrix_copilot.config import Settings
from infermatrix_copilot.kb_service.init_support import InitRecord, checkpoint_budget
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
from infermatrix_copilot.kb_service.quality_review import QualityPolicy, review_call
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.llm import Reply, Block
from infermatrix_copilot.trace_store import TraceStore, NativeCallArchive
root = Path(sys.argv[1])
record = InitRecord(stage='pr-history', repo='demo', spent_usd=.125)
budget = checkpoint_budget(1, record, root)
if sys.argv[2] == 'discovery':
    from infermatrix_copilot.kb_service.init_budget import Budget
    from infermatrix_copilot.kb_service.init_feature_discovery import _discovery_budget
    budget = _discovery_budget(Budget(1, spent_usd=.125), root / 'discovery-budget.json', 'batch')
store = TraceStore(root / 'traces', environ={})
NativeCallArchive.finish = lambda *args, **kwargs: os._exit(73)
gateway = ModelGateway(Settings(_env_file=None), recorder=trace_recorder(store),
    transport_factory=lambda _: SimpleNamespace(complete=lambda **kwargs:
        Reply(blocks=[Block(type='text', text='{"ok":true}')], stop_reason='end_turn', usage={})))
review_call(gateway, ModelRole('judge', 'codex', 'fixture'), policy=QualityPolicy('facet'),
            system='rubric', prompt='frozen', validate=lambda _: None, budget=budget, charge_usd=.5)
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path), account],
                            env=os.environ.copy(), text=True, capture_output=True, timeout=30)
    assert result.returncode == 73, result.stderr
    if account == 'record':
        record = InitRecord.load(tmp_path, 'demo', 'pr-history')
        calls = record.budget_calls
        path = InitRecord.path(tmp_path, 'demo', 'pr-history')
        def restore():
            return checkpoint_budget(1, record, tmp_path)
        assert record.spent_usd == .625
    else:
        from infermatrix_copilot.kb_service.init_budget import Budget
        from infermatrix_copilot.kb_service.init_feature_discovery import _discovery_budget
        path = tmp_path / 'discovery-budget.json'
        data = json.loads(path.read_text())
        calls = data['calls']
        def restore():
            return _discovery_budget(Budget(1), path, 'batch')
        assert data['spent_usd'] + data['reserved_usd'] == .625
    identity, = calls
    assert calls[identity]['status'] == 'reserved'
    archive, = (tmp_path / 'traces/attempts').glob('*/attempt.json')
    native = json.loads(archive.read_text())
    assert native['context']['init_budget'] == {'reservation_id': identity, 'reserved_usd': .5, 'accounting': 'fixed'}
    from infermatrix_copilot.trace_store import TraceStore
    store = TraceStore(tmp_path / 'traces')
    (call, _), = list(store.iter_records())
    assert call['context']['init_budget'] == native['context']['init_budget']
    before = path.read_bytes()
    resumed = restore()
    assert resumed.spent_usd == .625
    assert path.read_bytes() == before


@pytest.mark.parametrize('spent,accounted,error', [(.1, .5, 'total is below'),
                                                  (1, .1, 'lost its reservation')])
def test_retained_calls_cannot_reduce_conservative_budget(tmp_path, spent, accounted, error):
    record = InitRecord(stage='pr-history', repo='demo', spent_usd=spent,
        budget_calls={'a' * 32: {'status': 'reserved', 'reserved_usd': .5, 'accounted_usd': accounted}})
    path = record.save(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match=error):
        checkpoint_budget(1, record, tmp_path)
    assert path.read_bytes() == before


def test_parallel_reservations_keep_distinct_trace_and_account_identities(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    record = InitRecord(stage='knowledge', repo='demo')
    budget = checkpoint_budget(5, record, tmp_path)
    barrier = Barrier(4)
    def work(_):
        with budget.reserve(.5) as reservation:
            barrier.wait(timeout=10)
            assert current_context()['init_budget']['reservation_id'] == reservation.identity
            reservation.charge(.2)
            return reservation.identity
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(work, range(4)))
    loaded = InitRecord.load(tmp_path, 'demo', 'knowledge')
    assert len(set(ids)) == 4 and set(loaded.budget_calls) == set(ids)
    assert loaded.spent_usd == pytest.approx(.8)
    assert all(entry['status'] == 'settled' for entry in loaded.budget_calls.values())
    assert 'init_budget' not in current_context()


def test_discovery_resume_keeps_original_ids_and_conservative_total(tmp_path):
    from infermatrix_copilot.kb_service.init_budget import Budget
    from infermatrix_copilot.kb_service.init_feature_discovery import _discovery_budget
    journal = tmp_path / 'discovery-budget.json'
    budget = _discovery_budget(Budget(2, spent_usd=.125), journal, 'batch')
    with budget.reserve(.5) as first:
        # Capture the durable interrupted state before this process settles.
        interrupted = journal.read_bytes()
        assert current_context()['init_budget']['reservation_id'] == first.identity
        first.charge(.25)
    journal.write_bytes(interrupted)
    resumed = _discovery_budget(Budget(2, spent_usd=.2), journal, 'batch')
    assert resumed.spent_usd == .625
    assert resumed.calls[first.identity]['status'] == 'reserved'
    assert journal.read_bytes() == interrupted
    with resumed.reserve(.25) as second:
        assert second.identity != first.identity
        saved = json.loads(journal.read_text())
        assert set(saved['calls']) == {first.identity, second.identity}
        assert saved['spent_usd'] == .625 and saved['reserved_usd'] == .25
        second.charge(.1)
    assert resumed.calls[first.identity]['accounted_usd'] == .5


@pytest.mark.parametrize('damage', ['null_calls', 'negative_total', 'lost_total', 'batch', 'bad_entry'])
def test_discovery_corrupt_journal_cannot_be_overwritten_or_rearmed(tmp_path, damage):
    from infermatrix_copilot.kb_service.init_budget import Budget
    from infermatrix_copilot.kb_service.init_feature_discovery import _discovery_budget
    journal = tmp_path / 'discovery-budget.json'
    data = {'identity': 'batch', 'spent_usd': .1, 'reserved_usd': .5,
            'calls': {'a' * 32: {'status': 'reserved', 'reserved_usd': .5, 'accounted_usd': .5, 'accounting': 'actual'}}}
    if damage == 'null_calls':
        data['calls'] = None
    elif damage == 'negative_total':
        data['reserved_usd'] = -.1
    elif damage == 'lost_total':
        data['reserved_usd'] = 0
    elif damage == 'batch':
        data['identity'] = 'another-batch'
    else:
        data['calls']['a' * 32]['status'] = 'invalid'
    journal.write_text(json.dumps(data))
    before = journal.read_bytes()
    with pytest.raises(ValueError):
        _discovery_budget(Budget(2), journal, 'batch')
    assert journal.read_bytes() == before


@pytest.mark.parametrize('accounting,actual,expected', [('fixed', .1, .5), ('fixed', 9, .5),
    ('fixed', None, .5), ('actual', .1, .1), ('actual', 9, 9), ('actual', None, .5)])
def test_budget_preserves_explicit_original_accounting_policy(tmp_path, accounting, actual, expected):
    record = InitRecord(stage='pr-history', repo='demo')
    budget = checkpoint_budget(1, record, tmp_path)
    with budget.reserve(.5, accounting=accounting) as reservation:
        saved = InitRecord.load(tmp_path, 'demo', 'pr-history')
        assert saved.budget_calls[reservation.identity]['accounting'] == accounting
        assert current_context()['init_budget']['accounting'] == accounting
        reservation.charge(actual)
    saved = InitRecord.load(tmp_path, 'demo', 'pr-history')
    assert saved.budget_calls[reservation.identity]['accounting'] == accounting
    assert saved.spent_usd == expected


def test_legacy_call_entry_does_not_invent_an_accounting_policy(tmp_path):
    entry = {'status': 'reserved', 'reserved_usd': .5, 'accounted_usd': .5}
    record = InitRecord(stage='pr-history', repo='demo', spent_usd=.5, budget_calls={'a' * 32: entry})
    budget = checkpoint_budget(1, record, tmp_path)
    assert budget.calls['a' * 32] == entry and 'accounting' not in entry
