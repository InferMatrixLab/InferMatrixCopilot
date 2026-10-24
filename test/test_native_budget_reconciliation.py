"""Reconcile original process outcomes and original journals, without dispatch."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.improve.budget import BudgetBreach, BudgetRefused, Governor
from infermatrix_copilot.trace_store import TraceStore, MigrationError


CRASH = '''
import os,sys
from pathlib import Path
from types import SimpleNamespace
from infermatrix_copilot.config import Settings
from infermatrix_copilot.improve.budget import Governor,governed
from infermatrix_copilot.providers.completion import complete_native
from infermatrix_copilot.llm import Reply,Block
from infermatrix_copilot.trace_store import NativeCallArchive,TraceStore
root=Path(sys.argv[1]);mode=sys.argv[2]
cost=float(sys.argv[3]) if len(sys.argv)>3 else .2
settings=Settings(_env_file=None,improve_native_call_limits={'test:model':{'stop_usd':.5,'overshoot_usd':.25}})
gov=Governor(root/'ledger',usd_week=5,judge_calls_week=5,settings=settings)
if mode=='settlement':gov._finish_call=lambda *args,**kwargs:os._exit(70)
if mode=='archive':NativeCallArchive.finish=lambda *args,**kwargs:os._exit(71)
if mode=='reservation':
 original=gov.reserve_amount
 def reserve(*args,**kwargs):
  original(*args,**kwargs)
  os._exit(72)
 gov.reserve_amount=reserve
if mode=='record':
 original_append=TraceStore.append
 def append(self,kind,*args,**kwargs):
  if kind=='model_call':os._exit(73)
  return original_append(self,kind,*args,**kwargs)
 TraceStore.append=append
def complete(**kwargs):
 with (root/'dispatches').open('a') as handle:handle.write('dispatch\\n')
 if kwargs.get('native_event_sink'):
  kwargs['native_event_sink']({'type':'result','usage':{'cost_usd':cost},'session_id':'original'})
 return Reply(blocks=[Block(type='text',text='{"ok":true}')],usage={'cost_usd':cost},model='model')
transport=SimpleNamespace(spec=SimpleNamespace(id='test'),stops_at_spend=True,supports_native_events=True,complete=complete)
with governed(gov):complete_native(lambda:transport,request={'model':'model','system':'frozen','messages':[]},identity={'provider':'test'})
'''


def interrupted(tmp_path, mode='settlement', cost=.2):
    result = subprocess.run([sys.executable, '-c', CRASH, str(tmp_path), mode, str(cost)],
                            env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert result.returncode == {'settlement': 70, 'archive': 71, 'reservation': 72, 'record': 73}[mode], result.stderr
    gov = Governor(tmp_path / 'ledger', usd_week=5, judge_calls_week=5)
    path, = gov.dir.glob('20*-W*.json')
    state = json.loads(path.read_text())
    rid, = state['reservations']
    binding = state['reservations'][rid]['native_binding']
    return gov, rid, TraceStore(Path(binding['trace_root'])), path


def journal_bytes(store):
    return {str(p): p.read_bytes() for p in store.root.rglob('*.jsonl')}


def test_crash_after_original_receipt_settles_once_without_model_or_trace_mutation(tmp_path):
    gov, rid, store, path = interrupted(tmp_path)
    original = journal_bytes(store)
    first = gov.reconcile_native(rid, offline_confirmed=True)
    assert first['accounted_usd'] == .2 and first['native_cost_usd'] == .2
    before = path.read_bytes()
    assert gov.reconcile_native(rid, offline_confirmed=True) == first
    assert path.read_bytes() == before and journal_bytes(store) == original
    assert (tmp_path / 'dispatches').read_text() == 'dispatch\n'


@pytest.mark.parametrize('mode', ['reservation', 'record', 'archive'])
def test_missing_or_inflight_original_outcome_keeps_original_reservation(tmp_path, mode):
    gov, rid, store, path = interrupted(tmp_path, mode)
    before, journal = path.read_bytes(), journal_bytes(store)
    with pytest.raises((BudgetRefused, ValueError)):
        gov.reconcile_native(rid, offline_confirmed=True)
    assert path.read_bytes() == before and journal_bytes(store) == journal
    assert rid in json.loads(path.read_text())['reservations']
    assert (tmp_path / 'dispatches').exists() is (mode != 'reservation')


def test_archive_recovery_then_account_recovery_reuses_the_same_receipt(tmp_path):
    gov, rid, store, path = interrupted(tmp_path, 'archive')
    record, = [r for r, _ in store.iter_records(kind='model_call')]
    store.recover_native_attempt(record['result']['native_attempt_id'], record['id'], offline_confirmed=True)
    result = gov.reconcile_native(rid, offline_confirmed=True)
    assert result['trace_id'] == record['id'] and result['accounted_usd'] == .2
    assert (tmp_path / 'dispatches').read_text() == 'dispatch\n'


@pytest.mark.parametrize('damage', ['binding', 'archive', 'duplicate', 'missing_blob', 'unconfirmed'])
def test_recovery_refuses_changed_or_ambiguous_authority_without_mutating_it(tmp_path, damage):
    gov, rid, store, path = interrupted(tmp_path)
    record, = [r for r, _ in store.iter_records(kind='model_call')]
    state = json.loads(path.read_text())
    if damage == 'binding':
        state['reservations'][rid]['native_binding']['model']['model'] = 'other'
        path.write_text(json.dumps(state))
    elif damage == 'archive':
        file = store.root / 'attempts' / record['result']['native_attempt_id'] / 'attempt.json'
        data = json.loads(file.read_text());data['cost_usd'] = 99;file.write_text(json.dumps(data))
    elif damage == 'duplicate':
        store.append('model_call', inputs={'system': 'different'}, model=record['model'],
                     result={'native_attempt_id': record['result']['native_attempt_id']})
    elif damage == 'missing_blob':
        ref = record['outputs']['reply'].split(':', 1)[1]
        file = store.root / 'blobs' / ref[:2] / (ref + '.gz')
        file.unlink()
    before, journal = path.read_bytes(), journal_bytes(store)
    with pytest.raises((BudgetRefused, ValueError, OSError, MigrationError)):
        gov.reconcile_native(rid, offline_confirmed=damage != 'unconfirmed')
    assert path.read_bytes() == before and journal_bytes(store) == journal
    assert (tmp_path / 'dispatches').read_text() == 'dispatch\n'


def test_legacy_unbound_reservation_cannot_be_guessed_from_a_new_trace(tmp_path):
    gov = Governor(tmp_path, usd_week=5, judge_calls_week=5)
    rid = gov.reserve_amount('model', .75)
    path = gov._path(gov._week_of(rid));before = path.read_bytes()
    with pytest.raises(BudgetRefused, match='no native binding'):
        gov.reconcile_native(rid, offline_confirmed=True)
    assert path.read_bytes() == before


def test_schema_rejected_paid_reply_remains_an_original_account_fact(tmp_path):
    from types import SimpleNamespace
    from infermatrix_copilot.improve.budget import governed
    from infermatrix_copilot.providers.completion import complete_native
    from infermatrix_copilot.llm import Reply, Block
    settings = Settings(_env_file=None, improve_native_call_limits={'test:model': {'stop_usd': .5, 'overshoot_usd': .25}})
    gov = Governor(tmp_path, usd_week=5, judge_calls_week=5, settings=settings)
    original = gov._finish_call
    def lose_settlement(*args, **kwargs):
        raise OSError('fixture lost settlement write')
    gov._finish_call = lose_settlement
    transport = SimpleNamespace(spec=SimpleNamespace(id='test'), stops_at_spend=True,
        complete=lambda **kwargs: Reply(blocks=[Block(type='text', text='invalid')], usage={'cost_usd': .3}, model='model'))
    def validate(reply):
        raise ValueError('schema refused')
    with governed(gov), pytest.raises(OSError, match='lost settlement'):
        complete_native(lambda: transport, request={'model': 'model', 'messages': []}, validate=validate)
    gov._finish_call = original
    state = gov._load(gov.week());rid, = state['reservations']
    result = gov.reconcile_native(rid, offline_confirmed=True)
    assert result['accounted_usd'] == .3


def test_unknown_worker_outcome_stays_pending_without_another_dispatch(tmp_path):
    from types import SimpleNamespace
    from infermatrix_copilot.improve.budget import governed
    from infermatrix_copilot.providers.completion import complete_native
    settings = Settings(_env_file=None, improve_native_call_limits={'test:model': {'stop_usd': .5, 'overshoot_usd': .25}})
    gov = Governor(tmp_path, usd_week=5, judge_calls_week=5, settings=settings)
    calls = []
    def unknown(**kwargs):
        calls.append('original')
        error = RuntimeError('unknown worker response');error.native_outcome_pending = True;raise error
    transport = SimpleNamespace(spec=SimpleNamespace(id='test'), stops_at_spend=True, complete=unknown)
    with governed(gov), pytest.raises(RuntimeError, match='unknown worker'):
        complete_native(lambda: transport, request={'model': 'model', 'messages': []})
    state = gov._load(gov.week());rid, = state['reservations']
    before = gov._path(gov.week()).read_bytes()
    with pytest.raises(BudgetRefused, match='pending'):
        gov.reconcile_native(rid, offline_confirmed=True)
    assert gov._path(gov.week()).read_bytes() == before and calls == ['original']


def test_recovered_actual_overspend_is_recorded_and_retry_keeps_one_charge(tmp_path):
    gov, rid, store, path = interrupted(tmp_path, cost=1.2)
    for _ in range(2):
        with pytest.raises(BudgetBreach):
            gov.reconcile_native(rid, offline_confirmed=True)
        state = json.loads(path.read_text())
        assert state['usd_settled'] == 1.2 and len(state['breaches']) == 1
    assert (tmp_path / 'dispatches').read_text() == 'dispatch\n'


def test_operator_command_reconciles_the_original_account_and_requires_pause_confirmation(tmp_path):
    from infermatrix_copilot.improve.cli import main
    gov, rid, store, path = interrupted(tmp_path)
    before = path.read_bytes()
    args = ['budget', '--ledger-dir', str(tmp_path / 'ledger'), '--reconcile-native', rid]
    assert main(args) == 2 and path.read_bytes() == before
    assert main([*args, '--offline-confirmed']) == 0
    assert json.loads(path.read_text())['usd_settled'] == .2
    assert (tmp_path / 'dispatches').read_text() == 'dispatch\n'


@pytest.mark.parametrize('identity', ['../../foreign|1234567890', '2026-W42|other', '', None])
def test_invalid_recovery_identity_refuses_before_opening_a_journal(tmp_path, identity):
    gov = Governor(tmp_path, usd_week=5, judge_calls_week=5)
    with pytest.raises(BudgetRefused, match='invalid original'):
        gov.reconcile_native(identity, offline_confirmed=True)
    assert list(gov.dir.iterdir()) == []  # constructor creates the directory, not a journal or lock
