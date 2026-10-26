"""Discovery account/result/intent settlement reuses the original task only."""
from copy import deepcopy
from functools import partial
import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.kb_service.config import InitConfig
from infermatrix_copilot.kb_service.init_budget import Budget
from infermatrix_copilot.kb_service.init_feature_discovery import DiscoveryEngine, _discovery_budget, call_discovery_model
from infermatrix_copilot.kb_service import init_recovery
from infermatrix_copilot.kb_service.init_support import InitRecord, execution_snapshot
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole, ModelUnavailable
from infermatrix_copilot.kb_service.quality_review import native_receipt, verify_archived_receipt
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.llm import Block, Reply
from infermatrix_copilot.trace_store import MigrationError, TraceStore
from test_kb_feature_discovery import PIN, approve, candidate, index


def original(root, phase='candidate'):
    source = root / 'source'
    source.mkdir()
    idx = index(source, docs=False)
    cache = root / 'feature-discovery-index'
    cache.mkdir()
    (cache / 'original.json').write_text(json.dumps({'data': idx.data, 'sha256': idx.sha256}))
    store, calls = TraceStore(root / 'init/traces', environ={}), []

    def complete(**kwargs):
        calls.append(kwargs['role'])
        prompt = kwargs['messages'][0]['content']
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0])
        if kwargs['role'] == 'generator':
            data = {'candidates': [candidate()]}
        else:
            reject = phase.startswith('repair') and calls.count('judge') == 1
            data = approve(payload['candidates'], supported='no' if reject else 'yes')
        return Reply(blocks=[Block(type='text', text=json.dumps(data))], model=kwargs['model'],
                     usage={'cost_usd': .1}, stop_reason='end_turn')

    gateway = ModelGateway(Settings(_env_file=None), recorder=trace_recorder(store),
        transport_factory=lambda _: SimpleNamespace(complete=complete, subscription_billing=True))
    rt = SimpleNamespace(gateway=gateway, generator=ModelRole('generator', 'codex', 'fixture'),
                         judge=ModelRole('judge', 'codex', 'fixture'), subscription_generator=True,
                         unlimited_subscription=False, prices={})
    init = InitConfig(judge_call_usd=.25)
    record = InitRecord(stage='feature-discovery', repo='demo', pin=PIN,
                        discovery={'identity': 'original-batch', 'index_sha256': idx.sha256})
    budget = _discovery_budget(Budget(10), root / 'init/demo/discovery-budget.json', 'original-batch')
    def save():
        record.spent_usd = budget.spent_usd
        record.save(root)
    def verify(reply):
        verify_archived_receipt(store, native_receipt(reply), reply.role, reply.text)
    engine = DiscoveryEngine(idx, seeds=[], owners=['runtime'], state=record.discovery,
        call=partial(call_discovery_model, rt, budget, init, verify_reply=verify), save=save,
        concurrency=1, repository='owner/repo', pin=PIN, execution=execution_snapshot(rt, init))
    assert engine.scan() and engine.review()
    assert engine.audit_catalog_boundaries()
    features, _ = engine.catalog('repos/demo', require_boundary=True)
    assert engine.audit_catalog_consolidation(features)
    selected = []
    for key, envelope in record.discovery['dispatch_requests'].items():
        request = envelope['request']
        context = record.discovery['dispatch_contexts'][request['context_sha256']]
        kind = context['policy']['kind']
        if phase == kind or (phase.startswith('repair') and kind == 'repair'
                             and (request['task'][-1] is None) == (phase == 'repair_generate')):
            selected.append((key, envelope, context))
    key, envelope, context = selected[0]
    scope = key, envelope['binding']['attempt']
    requests = deepcopy(record.discovery['dispatch_requests'])
    contexts = deepcopy(record.discovery['dispatch_contexts'])
    record.discovery = {**deepcopy(context['state']), 'dispatch_intents': {key: deepcopy(envelope['binding'])},
                        'dispatch_requests': requests, 'dispatch_contexts': contexts}
    record.discovery['candidates']['independent'] = candidate('independent')
    save()
    offline = DiscoveryEngine(idx, seeds=[], owners=[], state=deepcopy(record.discovery), call=None,
                               save=lambda: None, repository='owner/repo', pin=PIN)
    proposed, _, digest = offline.prepare_original_result(store, scope=scope)
    # Prove recovery reads the authoritative journals and archives, not SQLite.
    store.index_path.unlink()
    return store, calls, scope, proposed, digest


@pytest.mark.parametrize('control', [False, True])
@pytest.mark.parametrize('phase', ['extract', 'candidate', 'boundary', 'consolidation', 'repair_generate', 'repair_judge'])
def test_original_discovery_task_settles_all_three_authorities_once(tmp_path, phase, control):
    store, calls, scope, proposed, digest = original(tmp_path, phase)
    if control:
        for trace, _ in list(store.iter_records()):
            store.append('auth_probe', context=trace['context'], result={'model_calls': 0})
        # Appending is a writer operation; begin offline recovery without its
        # derived index, just as original() does for the model-only fixture.
        store.index_path.unlink()
    before_calls = list(calls)
    logs = {p: p.read_bytes() for p in (store.root / 'records').glob('*.jsonl')}
    result = init_recovery.reconcile_discovery(tmp_path, repo='demo', scope=scope, offline_confirmed=True)
    assert result == {'status': 'reconciled', 'dispatch': scope[0], 'result_sha256': digest,
                      'settlements': result['settlements']}
    assert bool(result['settlements']) == (phase not in {'extract', 'repair_generate'})
    record = InitRecord.load(tmp_path, 'demo', 'feature-discovery')
    proposed['dispatch_intents'].pop(scope[0])
    assert record.discovery == proposed
    assert record.discovery['candidates']['independent']['id'] == 'independent'
    path = InitRecord.path(tmp_path, 'demo', 'feature-discovery')
    account = tmp_path / 'init/demo/discovery-budget.json'
    state_bytes, account_bytes = path.read_bytes(), account.read_bytes()
    assert init_recovery.reconcile_discovery(tmp_path, repo='demo', scope=scope, offline_confirmed=True) == result
    assert path.read_bytes() == state_bytes and account.read_bytes() == account_bytes
    assert calls == before_calls and not store.index_path.exists()
    assert all(p.read_bytes() == value for p, value in logs.items())


@pytest.mark.parametrize('crash', ['after_account', 'before_result_save', 'after_result_save'])
def test_partial_discovery_settlement_resumes_without_rebilling_or_new_work(tmp_path, monkeypatch, crash):
    store, calls, scope, proposed, digest = original(tmp_path)
    before_calls = list(calls)
    account = tmp_path / 'init/demo/discovery-budget.json'
    original_account = account.read_bytes()
    real_save, real_settle = InitRecord.save, init_recovery._settle_cost
    failed = []
    def save(record, root):
        if not failed and scope[0] in record.discovery.get('reconciled_dispatches', {}):
            failed.append(True)
            if crash == 'after_result_save':
                real_save(record, root)
            raise OSError('interrupted checkpoint')
        return real_save(record, root)
    def settle(*args):
        result = real_settle(*args)
        if not failed:
            failed.append(True)
            raise OSError('interrupted checkpoint')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(init_recovery, '_settle_cost', settle) if crash == 'after_account' else patch.setattr(InitRecord, 'save', save)
        with pytest.raises(OSError, match='interrupted checkpoint'):
            init_recovery.reconcile_discovery(tmp_path, repo='demo', scope=scope, offline_confirmed=True)
    record = InitRecord.load(tmp_path, 'demo', 'feature-discovery')
    assert (scope[0] in record.discovery['dispatch_intents']) == (crash != 'after_result_save')
    result = init_recovery.reconcile_discovery(tmp_path, repo='demo', scope=scope, offline_confirmed=True)
    assert result['result_sha256'] == digest and account.read_bytes() == original_account
    assert scope[0] not in InitRecord.load(tmp_path, 'demo', 'feature-discovery').discovery['dispatch_intents']
    assert calls == before_calls and not store.index_path.exists()


def test_discovery_task_reconciles_retained_reservation_before_clearing_intent(tmp_path):
    store, calls, scope, _, _ = original(tmp_path)
    before_calls = list(calls)
    trace, = [row for row, _ in store.iter_records()
              if row['context'].get('knowledge_dispatch') == {'dispatch_sha256': scope[0], 'attempt': scope[1]}]
    binding = trace['context']['init_budget']
    path = tmp_path / 'init/demo/discovery-budget.json'
    data = json.loads(path.read_text())
    entry = data['calls'][binding['reservation_id']]
    entry.update(status='reserved', accounted_usd=entry['reserved_usd'])
    entry.pop('receipt', None)
    data['spent_usd'] -= entry['reserved_usd']
    data['reserved_usd'] += entry['reserved_usd']
    path.write_text(json.dumps(data))
    result = init_recovery.reconcile_discovery(tmp_path, repo='demo', scope=scope, offline_confirmed=True)
    settled = json.loads(path.read_text())
    assert settled['calls'].keys() == data['calls'].keys()
    assert settled['calls'][binding['reservation_id']]['receipt']['trace_id'] == trace['id']
    assert settled['calls'][binding['reservation_id']]['status'] == 'settled'
    assert result['settlements'][0]['accounted_usd'] == .25
    assert settled['reserved_usd'] == 0 and settled['spent_usd'] == data['spent_usd'] + data['reserved_usd']
    assert calls == before_calls


@pytest.mark.parametrize('damage', ['unconfirmed', 'index', 'context', 'account', 'unresolved', 'authority'])
def test_unproven_discovery_recovery_retains_accounts_result_and_intent(tmp_path, damage):
    store, calls, scope, _, _ = original(tmp_path)
    record = InitRecord.load(tmp_path, 'demo', 'feature-discovery')
    if damage == 'index':
        (tmp_path / 'feature-discovery-index/original.json').unlink()
    elif damage == 'context':
        request = record.discovery['dispatch_requests'][scope[0]]['request']
        record.discovery['dispatch_contexts'][request['context_sha256']]['owners'].append('changed')
        record.save(tmp_path)
    elif damage == 'authority':
        record.discovery['candidates']['session']['title'] = 'changed after dispatch'
        record.save(tmp_path)
    elif damage == 'account':
        account = tmp_path / 'init/demo/discovery-budget.json'
        data = json.loads(account.read_text())
        data['identity'] = 'other-batch'
        account.write_text(json.dumps(data))
    elif damage == 'unresolved':
        from infermatrix_copilot.kb_service.quality_review import _attempt_scope
        with _attempt_scope(*scope):
            store.begin_call({'role': 'judge', 'provider': 'codex', 'model': 'fixture',
                              'system': 'interrupted', 'prompt': 'interrupted'})
    snapshots = {p: p.read_bytes() for p in (tmp_path / 'init/demo').glob('*.json')}
    before_calls = list(calls)
    with pytest.raises((ModelUnavailable, ValueError, MigrationError)):
        init_recovery.reconcile_discovery(tmp_path, repo='demo', scope=scope, offline_confirmed=damage != 'unconfirmed')
    assert all(p.read_bytes() == data for p, data in snapshots.items())
    assert calls == before_calls and not store.index_path.exists()


def test_discovery_reconciliation_cli_uses_shared_offline_guard(tmp_path, capsys, monkeypatch):
    from infermatrix_copilot.kb_service.cli import main
    _, calls, scope, _, digest = original(tmp_path)
    before_calls = list(calls)
    # Offline reconciliation must not construct a production runtime/gateway.
    monkeypatch.setattr('infermatrix_copilot.kb_service.runtime.KbRuntime.from_env', lambda *_, **__: pytest.fail('offline path created runtime'))
    monkeypatch.setattr('infermatrix_copilot.kb_service.init_support.InitRuntime.from_env', lambda *_, **__: pytest.fail('offline path created runtime'))
    args = ['--state-dir', str(tmp_path), 'reconcile-discovery', '--repo', 'demo',
            '--dispatch', scope[0], '--attempt', str(scope[1])]
    assert main(args) == 1
    assert 'reconciliation refused' in capsys.readouterr().err
    assert main([*args, '--offline-confirmed']) == 0
    assert json.loads(capsys.readouterr().out)['result_sha256'] == digest
    assert calls == before_calls
