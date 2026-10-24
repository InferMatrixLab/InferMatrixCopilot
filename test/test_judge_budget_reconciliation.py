"""Original judge count recovery shares native evidence, never model dispatch."""
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from infermatrix_copilot.improve.budget import BudgetRefused, Governor, judge_governed
from infermatrix_copilot.llm import Block, Reply
from infermatrix_copilot.providers.completion import complete_native
from infermatrix_copilot.trace_store import NativeCallArchive, TraceStore, MigrationError, bind_store


def evidence_bytes(store):
    return {str(p): p.read_bytes() for p in store.root.rglob('*')
            if p.is_file() and p.name not in ('index.migrate.lock',)}


CRASH = '''
import os,sys
from pathlib import Path
from types import SimpleNamespace
from infermatrix_copilot.improve.budget import Governor,judge_governed
from infermatrix_copilot.providers.completion import complete_native
from infermatrix_copilot.llm import Reply,Block
from infermatrix_copilot.trace_store import NativeCallArchive,TraceStore
root=Path(sys.argv[1]);mode=sys.argv[2]
gov=Governor(root/'ledger',usd_week=0,judge_calls_week=3)
if mode in ('settlement','rejected'):gov.settle_judge_call=lambda *args,**kwargs:os._exit(70)
if mode=='archive':NativeCallArchive.finish=lambda *args,**kwargs:os._exit(71)
if mode=='reservation':
 original=gov.reserve_judge_call
 def reserve(*args,**kwargs):
  original(*args,**kwargs)
  os._exit(72)
 gov.reserve_judge_call=reserve
if mode=='record':
 original_append=TraceStore.append
 def append(self,kind,*args,**kwargs):
  if kind=='model_call':os._exit(73)
  return original_append(self,kind,*args,**kwargs)
 TraceStore.append=append
def complete(**kwargs):
 with (root/'dispatches').open('a') as handle:handle.write('dispatch\\n')
 return Reply(blocks=[Block(type='text',text='{"ok":true}')],usage={'cost_usd':9},model='model')
def validate(reply):
 if mode=='rejected':raise ValueError('fixture judgment rejected')
transport=SimpleNamespace(complete=complete)
with judge_governed(gov):complete_native(lambda:transport,request={'model':'model','system':'frozen','messages':[]},
 identity={'provider':'test'},validate=validate,capture={'provider':'judge-fixture',
 'extra_result':{'usd':0,'subscription':True,'accounting_convention':'judge-calls/v1'}})
'''


def interrupted(root, mode='settlement'):
    result = subprocess.run([sys.executable, '-c', CRASH, str(root), mode], env=os.environ.copy(),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == {'settlement': 70, 'rejected': 70, 'archive': 71,
                                'reservation': 72, 'record': 73}[mode], result.stderr
    gov = Governor(root / 'ledger', usd_week=0, judge_calls_week=3)
    path, = gov.dir.glob('20*-W*.json')
    state = json.loads(path.read_text())
    token, = state['judge_tokens']
    store = TraceStore(gov.dir / 'native-traces')
    return gov, token, store, path


@pytest.mark.parametrize('mode', ['settlement', 'rejected'])
def test_original_terminal_attempt_is_counted_once_without_dollar_or_trace_mutation(tmp_path, mode):
    gov, token, store, path = interrupted(tmp_path, mode)
    before_trace = evidence_bytes(store)
    first = gov.reconcile_judge(token, offline_confirmed=True)
    assert first['accounted_judge_calls'] == 1
    before = path.read_bytes()
    assert gov.reconcile_judge(token, offline_confirmed=True) == first
    assert path.read_bytes() == before
    assert evidence_bytes(store) == before_trace
    assert gov.remaining()['judge_calls_used'] == 1 and gov.remaining()['judge_calls_reserved'] == 0
    assert gov.remaining()['usd_settled'] == 0
    assert (tmp_path / 'dispatches').read_text() == 'dispatch\n'


@pytest.mark.parametrize('mode', ['reservation', 'record', 'archive'])
def test_incomplete_original_attempt_stays_reserved_without_replacement(tmp_path, mode):
    gov, token, store, path = interrupted(tmp_path, mode)
    before = path.read_bytes()
    with pytest.raises((BudgetRefused, ValueError)):
        gov.reconcile_judge(token, offline_confirmed=True)
    assert path.read_bytes() == before
    assert gov.remaining()['judge_calls_used'] == 0 and gov.remaining()['judge_calls_reserved'] == 1
    assert (tmp_path / 'dispatches').exists() is (mode != 'reservation')


def test_original_archive_repair_then_count_recovery_keeps_the_original_record(tmp_path):
    gov, token, store, path = interrupted(tmp_path, 'archive')
    record, = [r for r, _ in store.iter_records(kind='model_call')]
    store.recover_native_attempt(record['result']['native_attempt_id'], record['id'], offline_confirmed=True)
    assert gov.reconcile_judge(token, offline_confirmed=True)['trace_id'] == record['id']
    assert (tmp_path / 'dispatches').read_text() == 'dispatch\n'


@pytest.mark.parametrize('damage', ['binding', 'schema', 'duplicate', 'blob', 'pending', 'pause'])
def test_changed_missing_pending_or_unpaused_evidence_cannot_settle_the_count(tmp_path, damage):
    gov, token, store, path = interrupted(tmp_path)
    state = json.loads(path.read_text())
    record, = [r for r, _ in store.iter_records(kind='model_call')]
    if damage == 'binding':
        state['judge_bindings'][token]['model']['model'] = 'other'
        path.write_text(json.dumps(state))
    elif damage == 'schema':
        state['judge_bindings'][token]['schema'] = 'foreign-binding/9'
        path.write_text(json.dumps(state))
    elif damage == 'duplicate':
        store.append('model_call', result={'native_attempt_id': record['result']['native_attempt_id']})
    elif damage == 'blob':
        digest = record['outputs']['reply'].split(':', 1)[1]
        (store.root / 'blobs' / digest[:2] / (digest + '.gz')).unlink()
    elif damage == 'pending':
        archive = store.root / 'attempts' / record['result']['native_attempt_id'] / 'attempt.json'
        meta = json.loads(archive.read_text());meta['native_outcome_pending'] = True
        archive.write_text(json.dumps(meta))
    before = path.read_bytes()
    with pytest.raises((BudgetRefused, ValueError, OSError, MigrationError)):
        gov.reconcile_judge(token, offline_confirmed=damage != 'pause')
    assert path.read_bytes() == before and (tmp_path / 'dispatches').read_text() == 'dispatch\n'


def test_pending_worker_and_recorder_failure_keep_original_count_reservations(tmp_path):
    for failure in ('worker', 'recorder'):
        gov = Governor(tmp_path / failure, usd_week=0, judge_calls_week=1)
        calls = []
        def dispatch(**kwargs):
            calls.append('original')
            if failure == 'worker':
                error = RuntimeError('original worker still pending')
                error.native_outcome_pending = True
                raise error
            return Reply(blocks=[Block(type='text', text='original')], usage={}, model='model')
        def recorder(entry):
            raise OSError('original receipt unavailable')
        with judge_governed(gov), pytest.raises((RuntimeError, OSError)):
            complete_native(lambda: SimpleNamespace(complete=dispatch),
                            request={'model': 'model', 'system': 'frozen', 'messages': []},
                            identity={'provider': 'test'}, recorder=recorder if failure == 'recorder' else None)
        path, = gov.dir.glob('20*-W*.json')
        token, = json.loads(path.read_text())['judge_tokens']
        before = path.read_bytes()
        with pytest.raises((BudgetRefused, ValueError)):
            gov.reconcile_judge(token, offline_confirmed=True)
        assert path.read_bytes() == before and calls == ['original']
        assert gov.remaining()['judge_calls_reserved'] == 1 and gov.remaining()['judge_calls_used'] == 0


def test_legacy_unbound_rows_and_conflicting_bindings_are_not_reinterpreted(tmp_path):
    gov = Governor(tmp_path, usd_week=0, judge_calls_week=3)
    token = gov.reserve_judge_call()
    before = gov._path(gov.week()).read_bytes()
    with pytest.raises(BudgetRefused, match='no native binding'):
        gov.reconcile_judge(token, offline_confirmed=True)
    assert gov._path(gov.week()).read_bytes() == before
    gov.settle_judge_call(token)
    assert gov.remaining()['judge_calls_used'] == 1
    other = gov.reserve_judge_call()
    binding = {'schema': 'native-budget-binding/1', 'attempt_id': 'original', 'trace_root': '/original'}
    gov.bind_judge_call(other, binding)
    with pytest.raises(BudgetRefused, match='different binding'):
        gov.bind_judge_call(other, {**binding, 'attempt_id': 'replacement'})
    with pytest.raises(BudgetRefused, match='changed during recovery'):
        gov.settle_judge_call(other, native_binding={**binding, 'attempt_id': 'replacement'})
    assert gov.remaining()['judge_calls_reserved'] == 1


def test_recovery_is_concurrent_idempotent_and_pinned_to_original_week(tmp_path):
    gov, token, store, path = interrupted(tmp_path)
    future = Governor(tmp_path / 'ledger', usd_week=0, judge_calls_week=3, clock=lambda: 1900000000)
    def reconcile(_):
        try:
            return future.reconcile_judge(token, offline_confirmed=True)
        except MigrationError:
            return None  # the canonical nonblocking recovery lock refuses concurrent ownership
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(reconcile, range(4)))
    original = future.reconcile_judge(token, offline_confirmed=True)
    assert any(result is not None for result in results)
    assert all(result is None or result == original for result in results)
    state = json.loads(path.read_text())
    assert state['judge_calls'] == 1 and state['judge_reserved'] == 0
    assert (tmp_path / 'dispatches').read_text() == 'dispatch\n'


def test_real_judge_entry_point_preserves_legacy_aliases_and_has_one_bound_archive(tmp_path, monkeypatch):
    from infermatrix_copilot.improve.judges import JudgeSpec, run_judge
    from infermatrix_copilot.providers import registry
    gov = Governor(tmp_path / 'ledger', usd_week=0, judge_calls_week=1)
    calls = []
    def dispatch(**kwargs):
        assert gov.remaining()['judge_calls_reserved'] == 1
        calls.append(kwargs)
        return Reply(blocks=[Block(type='text', text='{"status":"hit","quote":"q"}')],
                     usage={'cost_usd': 9, 'input_tokens': 5, 'output_tokens': 2}, model='model')
    monkeypatch.setattr(registry, 'transport_for_id', lambda settings, pid: SimpleNamespace(complete=dispatch))
    store = TraceStore(tmp_path / 'trace', environ={})
    with bind_store(store):
        assert run_judge(JudgeSpec('cli', 'model', provider='zcode'), system='frozen',
                         prompt='frozen', governor=gov)['status'] == 'hit'
    record, = [r for r, _ in store.iter_records(kind='model_call')]
    assert record['result']['usd'] == 0 and record['result']['subscription'] is True
    assert record['result']['cost_usd'] == 9 and record['result']['accounting_convention'] == 'judge-calls/v1'
    assert store.validate_native_attempt(record)['id'] == record['result']['native_attempt_id']
    assert len(calls) == 1 and gov.remaining()['usd_settled'] == 0
    token, = json.loads(gov._path(gov.week()).read_text())['judge_tokens']
    assert gov.reconcile_judge(token, offline_confirmed=True)['accounted_judge_calls'] == 1


def test_operator_command_uses_original_count_and_refuses_unconfirmed_writers(tmp_path):
    from infermatrix_copilot.improve.cli import main
    gov, token, store, path = interrupted(tmp_path)
    before = path.read_bytes()
    args = ['budget', '--ledger-dir', str(tmp_path / 'ledger'), '--reconcile-judge', token]
    assert main(args) == 2 and path.read_bytes() == before
    assert main([*args, '--offline-confirmed']) == 0
    assert gov.remaining()['judge_calls_used'] == 1
    assert (tmp_path / 'dispatches').read_text() == 'dispatch\n'
