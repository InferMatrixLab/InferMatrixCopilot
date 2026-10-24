"""Offline original-cost settlement does not run another model or review."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from infermatrix_copilot.kb_service.init_support import InitRecord
from infermatrix_copilot.trace_store import TraceStore

SCRIPT = '''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from infermatrix_copilot.config import Settings
from infermatrix_copilot.kb_service.init_budget import Budget
from infermatrix_copilot.kb_service.init_support import InitRecord, checkpoint_budget
from infermatrix_copilot.kb_service.init_feature_discovery import _discovery_budget
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.llm import Reply, Block
from infermatrix_copilot.trace_store import TraceStore
root, stage, policy, actual = Path(sys.argv[1]), sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
record = InitRecord(stage=stage, repo='demo', spent_usd=.125, discovery={'identity': 'batch'})
record.save(root)
budget = checkpoint_budget(1, record, root)
if stage == 'feature-discovery':
    budget = _discovery_budget(Budget(1, spent_usd=.125), root/'init/demo/discovery-budget.json', 'batch')
store = TraceStore(root/'init/traces', environ={})
def complete(**kwargs):
    with (root/'dispatches').open('a') as handle:
        handle.write('dispatch\\n')
    return Reply(blocks=[Block(type='text', text='{"ok":true}')], stop_reason='end_turn', usage={'cost_usd': actual})
gateway = ModelGateway(Settings(_env_file=None), recorder=trace_recorder(store),
                       transport_factory=lambda _: SimpleNamespace(complete=complete))
with budget.reserve(.5, accounting=policy):
    if stage == 'feature-discovery':
        record.spent_usd = .625
        record.save(root)
    gateway.call_json(ModelRole('generator', 'codex', 'fixture'), system='rubric', prompt='frozen')
    os._exit(74)
'''


def interrupted(root, stage='pr-history', policy='actual', actual=.1, *, rejected=False):
    script = SCRIPT
    if rejected:
        script = script.replace('from infermatrix_copilot.trace_store import TraceStore',
            'from infermatrix_copilot.trace_store import TraceStore, NativeCallArchive\n'
            'NativeCallArchive.finish = lambda *args, **kwargs: os._exit(74)')
        script = script.replace('text=\'{"ok":true}\'', 'text=\'invalid JSON\'')
    proc = subprocess.run([sys.executable, '-c', script, str(root), stage, policy, json.dumps(actual)],
                          env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 74, proc.stderr
    store = TraceStore(root/'init/traces')
    (trace, _), = list(store.iter_records())
    return store, trace


def command(root, stage, trace, confirmed=True, *, outcome=None):
    args = ['--state-dir', str(root), 'reconcile-init-cost', '--repo', 'demo', '--stage', stage,
            '--record', trace['id']]
    if confirmed:
        args.append('--offline-confirmed')
    if outcome is not None:
        args.extend(['--outcome', outcome])
    return subprocess.run([sys.executable, '-c',
        'import sys; from infermatrix_copilot.kb_service.cli import main; sys.exit(main(sys.argv[1:]))', *args],
        env=os.environ.copy(), capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize('stage', ['pr-history', 'feature-discovery'])
def test_auth_control_does_not_duplicate_original_budget_reservation(tmp_path, stage):
    store, trace = interrupted(tmp_path, stage)
    control = store.append('auth_probe', context=trace['context'], result={'model_calls': 0})
    logs = {p: p.read_bytes() for p in (store.root/'records').glob('*.jsonl')}
    for _ in range(2):
        result = command(tmp_path, stage, trace)
        assert result.returncode == 0, result.stderr
    assert InitRecord.load(tmp_path, 'demo', stage).spent_usd == pytest.approx(.225)
    assert (tmp_path/'dispatches').read_text() == 'dispatch\n'
    assert all(p.read_bytes() == value for p, value in logs.items())
    refused = command(tmp_path, stage, control)
    assert refused.returncode == 1 and 'reconciliation refused' in refused.stderr


@pytest.mark.parametrize('stage', ['pr-history', 'feature-discovery'])
@pytest.mark.parametrize('policy,actual,accounted', [('actual', .1, .1), ('actual', 9, 9),
                                                   ('actual', None, .5), ('fixed', 9, .5)])
def test_reconcile_original_cost_after_process_loss_once(tmp_path, stage, policy, actual, accounted):
    store, trace = interrupted(tmp_path, stage, policy, actual)
    original_journal = {str(p): p.read_bytes() for p in (store.root/'records').glob('*.jsonl')}
    result = command(tmp_path, stage, trace)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['accounted_usd'] == accounted
    record = InitRecord.load(tmp_path, 'demo', stage)
    assert record.spent_usd == pytest.approx(.125 + accounted)
    before = {str(p): p.read_bytes() for p in (tmp_path/'init/demo').glob('*.json')}
    repeated = command(tmp_path, stage, trace)
    assert repeated.returncode == 0, repeated.stderr
    assert {str(p): p.read_bytes() for p in (tmp_path/'init/demo').glob('*.json')} == before
    assert {str(p): p.read_bytes() for p in (store.root/'records').glob('*.jsonl')} == original_journal
    assert (tmp_path/'dispatches').read_text() == 'dispatch\n'


@pytest.mark.parametrize('damage', ['unconfirmed', 'legacy_policy', 'amount', 'duplicate', 'inflight', 'cost'])
def test_reconciliation_refuses_unproven_original_without_mutation(tmp_path, damage):
    store, trace = interrupted(tmp_path)
    record = InitRecord.load(tmp_path, 'demo', 'pr-history')
    identity = trace['context']['init_budget']['reservation_id']
    if damage == 'legacy_policy':
        del record.budget_calls[identity]['accounting']
        record.save(tmp_path)
    elif damage == 'amount':
        record.budget_calls[identity]['reserved_usd'] = .6
        record.save(tmp_path)
    elif damage == 'duplicate':
        path, = (store.root/'records').glob('*.jsonl')
        with path.open('a') as handle:
            handle.write(json.dumps(trace)+'\n')
    elif damage in ('inflight', 'cost'):
        path, = (store.root/'attempts').glob('*/attempt.json')
        data = json.loads(path.read_text())
        if damage == 'inflight':
            data['status'] = 'inflight'
        else:
            data['cost_usd'] = 9
        path.write_text(json.dumps(data))
    account = InitRecord.path(tmp_path, 'demo', 'pr-history')
    before = account.read_bytes()
    result = command(tmp_path, 'pr-history', trace, confirmed=damage != 'unconfirmed')
    assert result.returncode == 1, result.stderr
    assert 'reconciliation refused' in result.stderr
    assert account.read_bytes() == before
    assert (tmp_path/'dispatches').read_text() == 'dispatch\n'


@pytest.mark.parametrize('rejected', [False, True])
def test_discovery_recovers_crash_between_account_settlement_and_mirror(tmp_path, monkeypatch, rejected):
    from infermatrix_copilot.kb_service.init_recovery import reconcile_cost
    store, trace = interrupted(tmp_path, 'feature-discovery', rejected=rejected)
    outcome = 'failed' if rejected else 'complete'
    if rejected:
        store.recover_native_attempt(trace['result']['native_attempt_id'], trace['id'],
            offline_confirmed=True, outcome=outcome)
    with monkeypatch.context() as patch:
        def interrupted_save(*args, **kwargs):
            raise OSError('mirror unavailable')
        patch.setattr(InitRecord, 'save', interrupted_save)
        with pytest.raises(OSError, match='mirror unavailable'):
            reconcile_cost(tmp_path, repo='demo', stage='feature-discovery', trace_id=trace['id'],
                           offline_confirmed=True, outcome=outcome)
    assert InitRecord.load(tmp_path, 'demo', 'feature-discovery').spent_usd == .625
    result = command(tmp_path, 'feature-discovery', trace, outcome=outcome)
    assert result.returncode == 0, result.stderr
    assert InitRecord.load(tmp_path, 'demo', 'feature-discovery').spent_usd == pytest.approx(.225)
    assert (tmp_path/'dispatches').read_text() == 'dispatch\n'


@pytest.mark.parametrize('stage', ['pr-history', 'feature-discovery'])
@pytest.mark.parametrize('policy,actual,accounted', [('actual', .1, .1), ('actual', 9, 9),
                                                   ('actual', None, .5), ('fixed', 9, .5)])
def test_rejected_completed_reply_cost_recovers_once_without_approving_it(tmp_path, stage, policy, actual, accounted):
    store, trace = interrupted(tmp_path, stage, policy, actual, rejected=True)
    assert trace['error'] and trace['result']['native_call_completed'] is True
    account_before = {p: p.read_bytes() for p in (tmp_path/'init/demo').glob('*.json')}
    # Even explicit failed cost recovery requires the original archive to be settled first.
    refused = command(tmp_path, stage, trace, outcome='failed')
    assert refused.returncode == 1
    assert all(p.read_bytes() == value for p, value in account_before.items())
    archive = store.recover_native_attempt(trace['result']['native_attempt_id'], trace['id'],
        offline_confirmed=True, outcome='failed')
    assert archive['status'] == 'failed'
    # Existing callers cannot accidentally charge a failure through the successful-result path.
    assert command(tmp_path, stage, trace).returncode == 1
    originals = {p: p.read_bytes() for p in store.root.rglob('*') if p.is_file() and p.suffix != '.lock'}
    result = command(tmp_path, stage, trace, outcome='failed')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['accounted_usd'] == accounted
    record = InitRecord.load(tmp_path, 'demo', stage)
    assert record.spent_usd == pytest.approx(.125 + accounted)
    assert record.coverage == {} and record.history == {}  # no quality checkpoint or approval
    before = {p: p.read_bytes() for p in (tmp_path/'init/demo').glob('*.json')}
    repeated = command(tmp_path, stage, trace, outcome='failed')
    assert repeated.returncode == 0, repeated.stderr
    assert all(p.read_bytes() == value for p, value in before.items())
    assert all(p.read_bytes() == value for p, value in originals.items())
    assert (tmp_path/'dispatches').read_text() == 'dispatch\n'


@pytest.mark.parametrize('damage', ['pending', 'not_completed', 'missing_facts', 'amount',
                                    'duplicate', 'inflight', 'unconfirmed'])
def test_failed_cost_refuses_unproven_original_before_account_write(tmp_path, damage):
    store, trace = interrupted(tmp_path, rejected=True)
    store.recover_native_attempt(trace['result']['native_attempt_id'], trace['id'],
        offline_confirmed=True, outcome='failed')
    archive_path, = (store.root/'attempts').glob('*/attempt.json')
    archive = json.loads(archive_path.read_text())
    if damage in ('pending', 'not_completed', 'missing_facts'):
        key = 'native_call_completed' if damage == 'not_completed' else 'native_outcome_pending'
        if damage == 'missing_facts':
            trace['result'].pop(key)
            archive.pop(key)
        else:
            trace['result'][key] = archive[key] = damage == 'pending'
    elif damage == 'inflight':
        archive.update(status='inflight', trace_id='')
    elif damage == 'amount':
        trace['context']['init_budget']['reserved_usd'] = .6
        archive['context'] = trace['context']
    archive_path.write_text(json.dumps(archive))
    journal, = (store.root/'records').glob('*.jsonl')
    journal.write_text(json.dumps(trace)+'\n')
    if damage == 'duplicate':
        with journal.open('a') as handle:
            handle.write(json.dumps(trace)+'\n')
    originals = {p: p.read_bytes() for p in (tmp_path/'init').rglob('*')
                 if p.is_file() and p.suffix != '.lock'}
    result = command(tmp_path, 'pr-history', trace, confirmed=damage != 'unconfirmed', outcome='failed')
    assert result.returncode == 1, result.stderr
    assert 'reconciliation refused' in result.stderr
    assert all(p.read_bytes() == value for p, value in originals.items())
    assert (tmp_path/'dispatches').read_text() == 'dispatch\n'
