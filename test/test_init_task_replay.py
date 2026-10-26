"""Archived calls run through ordinary generation/judgment without dispatch."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.kb_service.gate import judge_block
from infermatrix_copilot.kb_service.init_budget import Budget
from infermatrix_copilot.kb_service.init_replay import OriginalTaskReplay, original_task_records
from infermatrix_copilot.kb_service.init_support import generate
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole, ModelUnavailable
from infermatrix_copilot.kb_service.quality_review import DispatchJournal, QualityPolicy, _attempt_scope
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.knowledge_service.l1 import Block as KnowledgeBlock
from infermatrix_copilot.llm import Block, Reply
from infermatrix_copilot.trace_store import TraceStore

GENERATOR = ModelRole('generator', 'codex', 'generator-fixture')
JUDGE = ModelRole('judge', 'codex', 'judge-fixture')
BINDING = {'owner': 'owner', 'attempt': 1, 'input_sha256': 'a'*64}


def test_original_execution_settings_restore_fallback_and_ignore_current_admission():
    from infermatrix_copilot.kb_service.init_support import execution_snapshot, restore_execution
    from infermatrix_copilot.kb_service.init_budget import Price
    from infermatrix_copilot.kb_service.config import InitConfig
    generator = ModelRole('generator', 'claude', 'original', fallback=GENERATOR)
    rt = SimpleNamespace(generator=generator, judge=JUDGE, unlimited_subscription=False,
                         subscription_generator=False, prices={'original': Price(1, 2, 100)},
                         gateway=SimpleNamespace(zcode_reasoning_level='max'), credential='not-a-snapshot-field')
    snapshot = execution_snapshot(rt, InitConfig(judge_call_usd=.25))
    rt.generator = GENERATOR
    rt.unlimited_subscription = True
    rt.prices.clear()
    restored, init = restore_execution(rt, json.loads(json.dumps(snapshot, sort_keys=True)))
    assert restored.generator == generator and restored.generator.fallback.label() == GENERATOR.label()
    assert restored.unlimited_subscription is False and rt.unlimited_subscription is True
    assert restored.prices['original'] == Price(1, 2, 100) and init.judge_call_usd == .25
    assert 'credential' not in snapshot
    snapshot['prices']['original']['max_output_tokens'] = 200
    assert restored.prices['original'].max_output_tokens == 100


def workflow(gateway, budget, *, charge=.5):
    rt = SimpleNamespace(gateway=gateway, generator=GENERATOR, subscription_generator=True,
                         unlimited_subscription=False)
    generated = generate(rt, budget, None, system='generate', prompt='frozen evidence')
    verdict = judge_block(KnowledgeBlock('prose', 'page.md', '', 'prose', 'a'*64),
        base={}, head={'page.md': generated.data['text']}, evidence=[], gateway=gateway, judge=JUDGE,
        budget=budget, charge_usd=charge, quality_policy=QualityPolicy('init-block'))
    return generated, verdict


def original(root, answer='yes', cost=9):
    store = TraceStore(root/'traces', environ={})
    calls = []
    def complete(**kwargs):
        calls.append(kwargs['role'])
        data = {'text': 'original knowledge'} if kwargs['role'] == 'generator' else {
            'dimensions': {'faithful': answer, 'does_not_weaken': 'yes', 'non_contradictory': 'yes'},
            'reasons': {'faithful': 'original judgment'}}
        return Reply(blocks=[Block(type='text', text=json.dumps(data))], stop_reason='end_turn',
                     usage={} if cost is None else {'cost_usd': cost})
    gateway = ModelGateway(Settings(_env_file=None), recorder=trace_recorder(store),
                           transport_factory=lambda _: SimpleNamespace(complete=complete, subscription_billing=True))
    budget = Budget(2, calls={})
    journal = DispatchJournal({}, lambda _: BINDING, lambda update: update())
    scope = journal.begin(None)
    with _attempt_scope(*scope):
        results = workflow(gateway, budget)
    records = [record for record, _ in store.iter_records()]
    return store, records, budget, calls, scope, results


@pytest.mark.parametrize('answer,verdict,cost', [('yes', 'pass', 9), ('no', 'fail', .1), ('unsure', 'human', None)])
def test_replay_preserves_acceptance_receipts_usage_and_original_account(tmp_path, answer, verdict, cost):
    store, records, budget, calls, scope, expected = original(tmp_path, answer, cost)
    account = deepcopy(budget.calls)
    journals = {p: p.read_bytes() for p in (store.root/'records').glob('*.jsonl')}
    store.index_path.unlink()
    replay = OriginalTaskReplay(store, records, binding=BINDING, attempt=1,
                                subscription_roles=[GENERATOR.label()])
    with _attempt_scope(*scope):
        generated, judged = workflow(replay, replay)
    assert generated == expected[0] and judged.to_dict() == expected[1].to_dict()
    assert judged.verdict == verdict
    assert generated.cost_usd == cost
    consumed = replay.consumed_records()
    assert [r['model']['role'] for r in consumed] == ['generator', 'judge']
    assert {r['id'] for r in consumed} == {r['id'] for r in records}
    assert budget.calls == account and budget.spent_usd == .5 and calls == ['generator', 'judge']
    assert not store.index_path.exists()
    assert all(p.read_bytes() == value for p, value in journals.items())
    consumed.clear()
    assert len(replay.consumed_records()) == 2


@pytest.mark.parametrize('damage', ['missing', 'duplicate_id', 'duplicate_prompt', 'runtime', 'budget', 'scope'])
def test_unproven_task_replay_refuses_without_dispatch_or_account_mutation(tmp_path, damage):
    store, records, budget, calls, scope, _ = original(tmp_path)
    account = deepcopy(budget.calls)
    if damage == 'missing':
        records = [r for r in records if r['model']['role'] == 'generator']
    elif damage == 'duplicate_id':
        records.append(deepcopy(records[0]))
    elif damage == 'duplicate_prompt':
        copy = deepcopy(next(r for r in records if r['model']['role'] == 'generator'))
        copy['id'] = 'another-id'; records.append(copy)
    elif damage == 'runtime':
        for record in records:
            record['env']['runtime_package_sha256'] = '0'*64
    with pytest.raises(ModelUnavailable):
        replay = OriginalTaskReplay(store, records, binding=BINDING, attempt=1,
                                    subscription_roles=[GENERATOR.label()])
        with _attempt_scope(scope[0], 2 if damage == 'scope' else scope[1]):
            workflow(replay, replay, charge=.6 if damage == 'budget' else .5)
        # A domain may retain an unavailable judgment as human/unjudged. That
        # does not make a missing original native call a completed replay.
        replay.consumed_records()
    if damage != 'duplicate_id':
        with pytest.raises(ModelUnavailable, match='incomplete'):
            replay.consumed_records()
    assert budget.calls == account and budget.spent_usd == .5 and calls == ['generator', 'judge']


def test_unconsumed_calls_cannot_be_reported_as_a_complete_replay(tmp_path):
    store, records, _, calls, scope, _ = original(tmp_path)
    replay = OriginalTaskReplay(store, records, binding=BINDING, attempt=1)
    with _attempt_scope(*scope):
        replay.call_json(GENERATOR, system='generate', prompt='frozen evidence')
    with pytest.raises(ModelUnavailable, match='incomplete'):
        replay.consumed_records()
    assert calls == ['generator', 'judge']


def test_task_replay_keeps_auth_control_separate_from_native_outcomes(tmp_path):
    store, records, budget, calls, scope, expected = original(tmp_path)
    for record in records:
        store.append('auth_probe', context=record['context'],
                     result={'model_calls': 0, 'subscription': True})
    journals = {p: p.read_bytes() for p in (store.root/'records').glob('*.jsonl')}
    account = deepcopy(budget.calls)
    store.index_path.unlink()
    native = original_task_records(store, binding=BINDING, attempt=scope[1])
    assert native == records
    replay = OriginalTaskReplay(store, native, binding=BINDING, attempt=scope[1],
                                subscription_roles=[GENERATOR.label()])
    with _attempt_scope(*scope):
        generated, judged = workflow(replay, replay)
    assert generated == expected[0] and judged.to_dict() == expected[1].to_dict()
    assert replay.consumed_records() == native
    assert calls == ['generator', 'judge'] and budget.calls == account
    assert not store.index_path.exists()
    assert all(p.read_bytes() == value for p, value in journals.items())


def test_control_record_cannot_be_supplied_as_a_native_replay_outcome(tmp_path):
    store, records, budget, calls, scope, _ = original(tmp_path)
    control = store.append('auth_probe', context=records[0]['context'], result={'model_calls': 0})
    with pytest.raises(ModelUnavailable, match='missing or ambiguous'):
        OriginalTaskReplay(store, [*records, control], binding=BINDING, attempt=scope[1])
    assert calls == ['generator', 'judge']


@pytest.mark.parametrize('damage', ['none', 'unfinished', 'missing', 'corrupt', 'symlink'])
def test_task_inventory_includes_attempts_without_a_receipt(tmp_path, damage):
    store, records, budget, calls, scope, _ = original(tmp_path)
    account = deepcopy(budget.calls)
    if damage == 'unfinished':
        with _attempt_scope(*scope):
            store.begin_call({'role': 'generator', 'provider': 'codex',
                                     'model': 'generator-fixture', 'system': 'extra', 'prompt': 'extra'})
    elif damage != 'none':
        path = store.root/'attempts'/records[0]['result']['native_attempt_id']/'attempt.json'
        if damage == 'missing':
            path.unlink()
        elif damage == 'corrupt':
            path.write_text('{')
        else:
            target = tmp_path/'copied-attempt.json'
            target.write_bytes(path.read_bytes()); path.unlink(); path.symlink_to(target)
    if damage == 'none':
        assert original_task_records(store, binding=BINDING, attempt=1) == records
    else:
        with pytest.raises(ModelUnavailable):
            original_task_records(store, binding=BINDING, attempt=1)
    assert budget.calls == account and calls == ['generator', 'judge']


from test_kb_init_skeleton import world  # noqa: E402,F401


@pytest.mark.parametrize('crash', ['none', 'before_save', 'after_save', 'after_account'])
def test_original_foundation_worker_replays_its_archived_calls(world, monkeypatch, crash, capsys):
    import copy
    from infermatrix_copilot.kb_service import init_knowledge_parallel as parallel
    from test_kb_foundation_parallel import _complete_native
    captured = []
    real_worker = parallel._worker
    def capture(stage, job):
        captured.append((copy.copy(stage), deepcopy(job)))
        return real_worker(stage, job)
    monkeypatch.setattr(parallel, '_worker', capture)
    rt, record, offline, store = _complete_native(world)
    stage, job = captured[0]
    stage.record = record
    binding = parallel._dispatch_binding(stage, owner=job['owner'].owner, payload=job['payload'],
                                         offered=job['offered'], page=job['page'], requested=job['requested'])
    key = parallel._hash(binding); scope = (key, binding['attempt'])
    parallel.original_job_request(stage, scope=scope)
    original_calls, original_account = len(offline.calls), deepcopy(record.budget_calls)
    result, consumed = parallel.replay_original_job(stage, store, scope=scope)
    assert len(consumed) > 1
    saved = record.coverage['foundation_jobs']['tasks'][job['owner'].owner + ':' + result['input_sha256']]
    assert result['artifacts'] == saved['artifacts'] and result['verdicts'] == saved['verdicts']
    assert record.budget_calls == original_account and len(offline.calls) == original_calls
    from infermatrix_copilot.kb_service import init_recovery
    from infermatrix_copilot.kb_service.init_support import InitRecord
    tasks = record.coverage['foundation_jobs']
    task_key = job['owner'].owner + ':' + result['input_sha256']
    tasks['tasks'].pop(task_key)
    tasks['inflight'][key] = binding
    record.save(rt.state_dir)
    spent = record.spent_usd
    original_save, original_settle = InitRecord.save, init_recovery._settle_cost
    failures = []
    def save(current, state_dir):
        if not failures and task_key in current.coverage['foundation_jobs']['tasks']:
            failures.append(1)
            if crash == 'after_save':
                original_save(current, state_dir)
            raise OSError('interrupted settlement')
        return original_save(current, state_dir)
    def settle(*args):
        value = original_settle(*args)
        if not failures:
            failures.append(1)
            raise OSError('interrupted settlement')
        return value
    if crash != 'none':
        if crash == 'after_account':
            monkeypatch.setattr(init_recovery, '_settle_cost', settle)
        else:
            monkeypatch.setattr(InitRecord, 'save', save)
        with pytest.raises(OSError, match='interrupted settlement'):
            init_recovery.reconcile_foundation(rt.state_dir, repo=record.repo, scope=scope, offline_confirmed=True)
        persisted = InitRecord.load(rt.state_dir, record.repo, 'knowledge')
        assert (key in persisted.coverage['foundation_jobs']['inflight']) == (crash != 'after_save')
    settled = init_recovery.reconcile_foundation(rt.state_dir, repo=record.repo, scope=scope, offline_confirmed=True)
    assert settled['status'] == 'reconciled' and settled['settlements']
    persisted = InitRecord.load(rt.state_dir, record.repo, 'knowledge')
    assert key not in persisted.coverage['foundation_jobs']['inflight']
    assert persisted.coverage['foundation_jobs']['tasks'][task_key] == result
    assert persisted.spent_usd == spent
    assert len(offline.calls) == original_calls
    before = InitRecord.path(rt.state_dir, record.repo, 'knowledge').read_bytes()
    assert init_recovery.reconcile_foundation(rt.state_dir, repo=record.repo, scope=scope, offline_confirmed=True) == settled
    assert InitRecord.path(rt.state_dir, record.repo, 'knowledge').read_bytes() == before
    if crash == 'none':
        from infermatrix_copilot.kb_service.cli import main
        command = ['--state-dir', str(rt.state_dir), 'reconcile-foundation', '--repo', record.repo,
                   '--dispatch', key, '--attempt', str(scope[1])]
        assert main(command) == 1
        assert 'writer-pause' in capsys.readouterr().err
        assert InitRecord.path(rt.state_dir, record.repo, 'knowledge').read_bytes() == before
        def no_dispatch(*args, **kwargs):
            raise AssertionError('offline recovery must not dispatch')
        monkeypatch.setattr(ModelGateway, 'call_json', no_dispatch)
        monkeypatch.setattr(ModelGateway, '__init__', no_dispatch)
        assert main(command + ['--offline-confirmed']) == 0
        assert json.loads(capsys.readouterr().out) == settled
        assert InitRecord.path(rt.state_dir, record.repo, 'knowledge').read_bytes() == before
