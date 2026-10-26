import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest


def test_process_loss_preserves_quality_identity_before_dispatch_and_refuses_retry(tmp_path):
    script = '''
import json, os, sys
from pathlib import Path
from infermatrix_copilot.config import Settings
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole, ModelUnavailable
from infermatrix_copilot.kb_service.quality_review import QualityPolicy, review_call, checkpoint_review
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.trace_store import TraceStore, trace_context
root = Path(sys.argv[1])
class Transport:
    def complete(self, **kwargs):
        os._exit(71)
store = TraceStore(root / 'traces', environ={})
gateway = ModelGateway(Settings(_env_file=None), transport_factory=lambda _: Transport(),
                       recorder=trace_recorder(store))
path = root / 'state.json'
state = json.loads(path.read_text()) if path.exists() else {}
def save():
    path.write_text(json.dumps(state))
def run():
    return review_call(gateway, ModelRole('judge', 'codex', 'fixture'),
        policy=QualityPolicy('facet', version=3), system='rubric', prompt='frozen evidence',
        validate=lambda _: None)
try:
    with trace_context(run_id='original-run', unit_id='original-unit'):
        checkpoint_review(state, run, save, binding={'head': 'frozen'})
except ModelUnavailable as exc:
    assert not exc.allow_fallback
    print('original dispatch remains pending')
else:
    raise AssertionError('unexpected completed result')
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 71, result.stderr
    state = json.loads((tmp_path / 'state.json').read_text())
    assert state['review_dispatched'] and state['review_attempts'] == 1
    archives = list((tmp_path / 'traces/attempts').glob('*/attempt.json'))
    assert len(archives) == 1
    before = archives[0].read_bytes()
    archive = json.loads(before)
    assert archive['status'] == 'inflight' and not archive.get('trace_id')
    context = archive['context']
    assert context['run_id'] == 'original-run' and context['unit_id'] == 'original-unit'
    identity = {'policy': 'facet', 'version': 3, 'requested': 'codex:fixture',
                'system': 'rubric', 'prompt': 'frozen evidence'}
    assert context['quality_review'] == {
        'policy': 'facet', 'version': 3, 'attempt': 1, 'call_index': 1,
        'dispatch_sha256': hashlib.sha256(json.dumps({'head': 'frozen'}, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        'input_sha256': hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    assert state['review_dispatched_binding'] == {
        'inputs': {'head': 'frozen'}, 'attempt': 1,
        'sha256': context['quality_review']['dispatch_sha256']}
    resumed = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                             env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert resumed.returncode == 0, resumed.stderr
    assert 'original dispatch remains pending' in resumed.stdout
    assert archives[0].read_bytes() == before
    assert list((tmp_path / 'traces/attempts').glob('*/attempt.json')) == archives


@pytest.mark.parametrize('damage', [None, 'id', 'inputs', 'outputs', 'model', 'quality_binding', 'events', 'error'])
def test_completed_receipt_and_native_archive_keep_same_dispatch_binding(tmp_path, damage):
    from types import SimpleNamespace
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole, ModelUnavailable
    from infermatrix_copilot.kb_service.quality_review import QualityPolicy, review_call, verify_archived_receipt
    from infermatrix_copilot.kb_service.runtime import trace_recorder
    from infermatrix_copilot.llm import Reply, Block
    from infermatrix_copilot.trace_store import TraceStore, trace_context, current_context

    store = TraceStore(tmp_path / 'traces', environ={})
    gateway = ModelGateway(Settings(_env_file=None), recorder=trace_recorder(store),
        transport_factory=lambda _: SimpleNamespace(complete=lambda **kwargs:
            Reply(blocks=[Block(type='text', text='{"ok":true}')], stop_reason='end_turn',
                  usage={'input_tokens': 10, 'output_tokens': 2})))
    with trace_context(run_id='run', quality_review={'outer': True}):
        reply = review_call(gateway, ModelRole('judge', 'codex', 'fixture'),
            policy=QualityPolicy('facet', archive='verified'), system='rubric',
            prompt='frozen', validate=lambda _: None)
        assert current_context()['quality_review'] == {'outer': True}
    assert 'quality_review' not in current_context()
    archive, = (tmp_path / 'traces/attempts').glob('*/attempt.json')
    native = json.loads(archive.read_text())
    record = store.get(reply.trace_id)
    expected = {key: reply.quality_receipt[key] for key in
                ('policy', 'version', 'input_sha256', 'dispatch_sha256', 'attempt', 'call_index')}
    assert record['context']['quality_review'] == native['context']['quality_review'] == expected
    assert native['status'] == 'complete' and native['trace_id'] == reply.trace_id
    proof = reply.quality_receipt['native']
    verify_archived_receipt(store, proof, reply.role, text=reply.text)
    if damage is None:
        return
    if damage == 'id':
        native['id'] = 'a' * 32
    elif damage in ('inputs', 'outputs'):
        key = 'prompt' if damage == 'inputs' else 'reply'
        native[damage][key] = store.put_blob('another valid blob')
    elif damage == 'model':
        native['model']['model'] = 'another-model'
    elif damage == 'quality_binding':
        native['context']['quality_review']['attempt'] += 1
    elif damage == 'events':
        native['native_events_sha256'] = store.put_blob('another valid event stream')
    elif damage == 'error':
        native['error'] = 'native archive failed'
    archive.write_text(json.dumps(native))
    before = archive.read_bytes()
    with pytest.raises(ModelUnavailable, match='archive could not be replayed'):
        verify_archived_receipt(store, proof, reply.role, text=reply.text)
    assert archive.read_bytes() == before


def test_process_loss_in_parallel_generator_retains_original_task_binding(tmp_path):
    script = '''
import json, os, sys
from pathlib import Path
from infermatrix_copilot.config import Settings
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole, ModelUnavailable
from infermatrix_copilot.kb_service.quality_review import DispatchJournal, drain_review_tasks
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.trace_store import TraceStore
root = Path(sys.argv[1])
class Transport:
    def complete(self, **kwargs):
        os._exit(72)
store = TraceStore(root/'traces', environ={})
gateway = ModelGateway(Settings(_env_file=None), transport_factory=lambda _: Transport(), recorder=trace_recorder(store))
path = root/'state.json'
state = json.loads(path.read_text()) if path.exists() else {}
def checkpoint(update):
    update()
    path.write_text(json.dumps(state))
journal = DispatchJournal(state, lambda task: {'owner': task, 'binding': 'frozen-batch', 'attempt': 2}, checkpoint)
def work(task):
    return gateway.call_json(ModelRole('generator', 'codex', 'fixture'), system='generate', prompt=task)
try:
    drain_review_tasks(['owner-a'], work, lambda *args: None, concurrency=1, journal=journal)
except ModelUnavailable:
    print('original generation remains pending')
else:
    raise AssertionError('unexpected completion')
'''
    proc = subprocess.run([sys.executable, '-c', script, str(tmp_path)], env=os.environ.copy(),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 72, proc.stderr
    state = json.loads((tmp_path/'state.json').read_text())
    identity, = state
    assert state[identity] == {'owner': 'owner-a', 'binding': 'frozen-batch', 'attempt': 2}
    archive, = (tmp_path/'traces/attempts').glob('*/attempt.json')
    before = archive.read_bytes()
    native = json.loads(before)
    assert native['status'] == 'inflight' and native['model']['role'] == 'generator'
    assert native['context']['knowledge_dispatch'] == {'dispatch_sha256': identity, 'attempt': 2}
    assert 'quality_review' not in native['context']
    again = subprocess.run([sys.executable, '-c', script, str(tmp_path)], env=os.environ.copy(),
                           capture_output=True, text=True, timeout=30)
    assert again.returncode == 0 and 'original generation remains pending' in again.stdout, again.stderr
    assert archive.read_bytes() == before
    assert list((tmp_path/'traces/attempts').glob('*/attempt.json')) == [archive]


def test_parallel_task_native_calls_share_only_their_original_dispatch(tmp_path):
    from types import SimpleNamespace
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
    from infermatrix_copilot.kb_service.quality_review import DispatchJournal, drain_review_tasks, QualityPolicy, review_call
    from infermatrix_copilot.kb_service.runtime import trace_recorder
    from infermatrix_copilot.llm import Reply, Block
    from infermatrix_copilot.trace_store import TraceStore, current_context
    store = TraceStore(tmp_path/'traces', environ={})
    gateway = ModelGateway(Settings(_env_file=None), recorder=trace_recorder(store),
        transport_factory=lambda _: SimpleNamespace(complete=lambda **kwargs:
            Reply(blocks=[Block(type='text', text='{"ok":true}')], stop_reason='end_turn')))
    state, originals, results = {}, {}, {}
    def checkpoint(update):
        update()
        originals.update(state)
    journal = DispatchJournal(state, lambda task: {'owner': task, 'attempt': 1}, checkpoint)
    def work(task):
        generator = gateway.call_json(ModelRole('generator', 'codex', 'fixture'), system='generate', prompt=task)
        judged = review_call(gateway, ModelRole('judge', 'codex', 'fixture'), policy=QualityPolicy('init-block'),
                             system='review', prompt=task, validate=lambda _: None)
        return generator.trace_id, judged.trace_id
    drain_review_tasks(['owner-a', 'owner-b'], work,
                       lambda task, value, error: results.update({task: value}) if error is None else (_ for _ in ()).throw(error),
                       concurrency=2, journal=journal)
    assert not state and 'knowledge_dispatch' not in current_context()
    for task, trace_ids in results.items():
        identity, = [key for key, binding in originals.items() if binding['owner'] == task]
        for index, trace_id in enumerate(trace_ids):
            record = store.get(trace_id)
            store.validate_native_attempt(record)
            assert record['context']['knowledge_dispatch'] == {'dispatch_sha256': identity, 'attempt': 1}
            if index:
                assert record['context']['quality_review']['dispatch_sha256'] == identity
                assert record['context']['quality_review']['call_index'] == 1
            else:
                assert 'quality_review' not in record['context']
