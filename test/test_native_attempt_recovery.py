"""Recovery must reuse the durable call and never invoke another provider."""
import json
import os
import subprocess
import sys

import pytest


CRASH = '''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from infermatrix_copilot.config import Settings
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
from infermatrix_copilot.kb_service.quality_review import QualityPolicy, review_call
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.llm import Reply, Block
from infermatrix_copilot.trace_store import TraceStore, NativeCallArchive, trace_context
root = Path(sys.argv[1])
store = TraceStore(root / 'traces', environ={})
def complete(**kwargs):
    with (root / 'dispatches').open('a') as handle:
        handle.write('dispatch\\n')
    return Reply(blocks=[Block(type='text', text='{"ok":true}')],
                 stop_reason='end_turn', usage={'input_tokens': 10, 'output_tokens': 2})
NativeCallArchive.finish = lambda *args, **kwargs: os._exit(72)
gateway = ModelGateway(Settings(_env_file=None), recorder=trace_recorder(store),
                       transport_factory=lambda _: SimpleNamespace(complete=complete))
with trace_context(run_id='original', unit_id='unit'):
    review_call(gateway, ModelRole('judge', 'codex', 'fixture'),
                policy=QualityPolicy('facet'), system='rubric', prompt='frozen', validate=lambda _: None)
'''


@pytest.fixture
def interrupted(tmp_path):
    from infermatrix_copilot.trace_store import TraceStore
    result = subprocess.run([sys.executable, '-c', CRASH, str(tmp_path)],
                            capture_output=True, text=True, timeout=30, env=os.environ.copy())
    assert result.returncode == 72, result.stderr
    store = TraceStore(tmp_path / 'traces', environ={})
    (record, _), = list(store.iter_records())
    path, = (store.root / 'attempts').glob('*/attempt.json')
    assert json.loads(path.read_text())['status'] == 'inflight'
    return tmp_path, store, record, path


def cli(root, record, *, confirmed=True, outcome=None):
    args = ['--state-dir', str(root), 'recover-attempt', '--attempt',
            record['result']['native_attempt_id'], '--record', record['id']]
    if confirmed:
        args.append('--offline-confirmed')
    if outcome is not None:
        args.extend(['--outcome', outcome])
    # A separate interpreter has no original transport or gateway instance.
    return subprocess.run([sys.executable, '-c',
        'import sys; from infermatrix_copilot.kb_service.cli import main; sys.exit(main(sys.argv[1:]))',
        *args], capture_output=True, text=True, timeout=30, env=os.environ.copy())


def journal_bytes(store):
    return {str(path): path.read_bytes() for path in (store.root / 'records').glob('*.jsonl')}


@pytest.mark.parametrize('missing_index', [False, True])
def test_recover_original_after_process_exit_and_repeat_without_dispatch(interrupted, missing_index):
    from infermatrix_copilot.kb_service.models import ModelRole
    from infermatrix_copilot.kb_service.quality_review import verify_archived_receipt
    root, store, record, path = interrupted
    journal = journal_bytes(store)
    if missing_index:
        store.index_path.unlink()
    result = cli(root, record)
    assert result.returncode == 0, result.stderr
    recovered = json.loads(path.read_text())
    assert recovered['trace_id'] == record['id'] and recovered['status'] == 'complete'
    assert recovered['usage'] == record['usage']
    assert recovered['cost_usd'] == record['result']['cost_usd']
    assert recovered['context'] == record['context']
    assert store.validate_native_attempt(record) == recovered
    if missing_index:
        assert not store.index_path.exists()  # index repair is separate
    else:
        verify_archived_receipt(store, {'trace_id': record['id'], 'requested': 'codex:fixture',
            'reply_sha256': record['outputs']['reply'].split(':')[1]},
            ModelRole('judge', 'codex', 'fixture'), text='{"ok":true}')
    before = path.read_bytes()
    again = cli(root, record)
    assert again.returncode == 0, again.stderr
    assert path.read_bytes() == before
    assert journal_bytes(store) == journal
    assert (root / 'dispatches').read_text() == 'dispatch\n'


@pytest.mark.parametrize('damage', ['unconfirmed', 'inputs', 'context', 'model', 'env',
                                    'events', 'sessions', 'failed', 'duplicate', 'wrong_record'])
def test_recovery_refuses_unsafe_or_ambiguous_original_without_mutation(interrupted, damage):
    root, store, record, path = interrupted
    attempt = json.loads(path.read_text())
    if damage in ('inputs', 'context', 'model', 'env'):
        attempt[damage]['unexpected'] = 'different'
        if damage == 'model':
            attempt['model']['model'] = 'another-model'
        path.write_text(json.dumps(attempt))
    elif damage == 'sessions':
        attempt['native_session_ids'] = ['another-session']
        path.write_text(json.dumps(attempt))
    elif damage == 'events':
        (path.parent / 'native-events.jsonl').write_text('unexpected event\n')
    elif damage in ('failed', 'duplicate'):
        journal, = (store.root / 'records').glob('*.jsonl')
        if damage == 'failed':
            record['error'] = 'failed call'
            journal.write_text(json.dumps(record) + '\n')
        else:
            with journal.open('a') as handle:
                handle.write(json.dumps(record) + '\n')
    elif damage == 'wrong_record':
        record['id'] = 'nonexistent'
    before, journal = path.read_bytes(), journal_bytes(store)
    result = cli(root, record, confirmed=damage != 'unconfirmed')
    assert result.returncode == 1, result.stdout
    assert 'recovery refused' in result.stderr
    assert path.read_bytes() == before and journal_bytes(store) == journal
    assert (root / 'dispatches').read_text() == 'dispatch\n'


@pytest.fixture
def completed_failure(tmp_path):
    from infermatrix_copilot.trace_store import TraceStore
    script = CRASH.replace('validate=lambda _: None',
        'validate=lambda _: (_ for _ in ()).throw(ValueError("original validation failed"))')
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            capture_output=True, text=True, timeout=30, env=os.environ.copy())
    assert result.returncode == 72, result.stderr
    store = TraceStore(tmp_path / 'traces', environ={})
    (record, _), = list(store.iter_records())
    path, = (store.root / 'attempts').glob('*/attempt.json')
    assert 'original validation failed' in record['error']
    assert record['result']['native_call_completed'] is True
    assert json.loads(path.read_text())['status'] == 'inflight'
    return tmp_path, store, record, path


@pytest.mark.parametrize('missing_index', [False, True])
def test_recover_completed_failure_preserves_failure_without_dispatch(completed_failure, missing_index):
    root, store, record, path = completed_failure
    before, journals = path.read_bytes(), journal_bytes(store)
    if missing_index:
        store.index_path.unlink()
    # Default recovery cannot reinterpret a failed call as successful.
    refused = cli(root, record)
    assert refused.returncode == 1
    assert path.read_bytes() == before
    recovered = cli(root, record, outcome='failed')
    assert recovered.returncode == 0, recovered.stderr
    attempt = json.loads(path.read_text())
    assert attempt['status'] == 'failed'
    assert attempt['error'] == record['error']
    assert attempt['trace_id'] == record['id']
    assert attempt['outputs'] == record['outputs']
    assert attempt['usage'] == record['usage']
    assert attempt['cost_usd'] == record['result']['cost_usd']
    assert store.validate_native_attempt(record, allow_failed=True) == attempt
    with pytest.raises(ValueError):
        store.validate_native_attempt(record)  # failure cannot become an approved reply
    before = path.read_bytes()
    again = cli(root, record, outcome='failed')
    assert again.returncode == 0, again.stderr
    assert path.read_bytes() == before and journal_bytes(store) == journals
    assert (root / 'dispatches').read_text() == 'dispatch\n'
    if missing_index:
        assert not store.index_path.exists()


@pytest.mark.parametrize('damage', ['pending', 'not_completed', 'not_sent', 'missing_facts',
                                    'missing_error', 'events', 'terminal_success', 'unconfirmed',
                                    'attempt_pending'])
def test_failed_recovery_refuses_unproven_or_conflicting_outcome(completed_failure, damage):
    root, store, record, path = completed_failure
    if damage == 'pending':
        record['result']['native_outcome_pending'] = True
    elif damage == 'not_completed':
        record['result']['native_call_completed'] = False
    elif damage == 'not_sent':
        record['result']['native_call_sent'] = False
    elif damage == 'missing_facts':
        record['result'].pop('native_outcome_pending')
    elif damage == 'missing_error':
        record['error'] = ''
    elif damage == 'events':
        (path.parent / 'native-events.jsonl').write_text('different\n')
    elif damage == 'terminal_success':
        attempt = json.loads(path.read_text())
        attempt.update(status='complete', trace_id=record['id'])
        path.write_text(json.dumps(attempt))
    elif damage == 'attempt_pending':
        attempt = json.loads(path.read_text())
        attempt['native_outcome_pending'] = True
        path.write_text(json.dumps(attempt))
    journal, = (store.root / 'records').glob('*.jsonl')
    journal.write_text(json.dumps(record) + '\n')
    before, journals = path.read_bytes(), journal_bytes(store)
    refused = cli(root, record, outcome='failed', confirmed=damage != 'unconfirmed')
    assert refused.returncode == 1, refused.stderr
    assert 'recovery refused' in refused.stderr
    assert path.read_bytes() == before and journal_bytes(store) == journals
    assert (root / 'dispatches').read_text() == 'dispatch\n'


@pytest.mark.parametrize('field,value', [
    ('native_outcome_pending', True),
    ('native_outcome_pending', 'false'),
    ('native_call_sent', 1),
    ('native_call_completed', None),
    ('process_cleanup', {'schema':'owned-process-cleanup/1',
        'scope':{'boot_id':None,'pid_namespace':None},'survivors':[],
        'leader_returncode':None,'error_types':['ProcessCleanupError']}),
])
def test_recovery_refuses_pending_or_corrupt_facts_before_archive_mutation(interrupted, field, value):
    root, store, record, path = interrupted
    record['result'][field] = value
    journal, = (store.root/'records').glob('*.jsonl')
    journal.write_text(json.dumps(record)+'\n')
    before, journals = path.read_bytes(), journal_bytes(store)
    result = cli(root, record)
    assert result.returncode == 1, result.stdout
    assert 'recovery refused' in result.stderr
    assert path.read_bytes() == before
    assert journal_bytes(store) == journals
    assert (root/'dispatches').read_text() == 'dispatch\n'


def test_incomplete_archive_receipt_verification_cannot_authorize_fallback(interrupted):
    from infermatrix_copilot.budgeting import native_outcome_unknown
    from infermatrix_copilot.kb_service.models import ModelRole, ModelUnavailable
    from infermatrix_copilot.kb_service.quality_review import verify_archived_receipt
    _, store, record, path = interrupted
    before, journal = path.read_bytes(), journal_bytes(store)
    with pytest.raises(ModelUnavailable) as caught:
        verify_archived_receipt(store, {'trace_id':record['id'],'requested':'codex:fixture',
            'reply_sha256':record['outputs']['reply'].split(':')[1]},
            ModelRole('judge','codex','fixture'))
    assert caught.value.allow_fallback is False
    assert native_outcome_unknown(caught.value)
    assert path.read_bytes() == before and journal_bytes(store) == journal
