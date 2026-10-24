"""Durable evidence must survive its producer without rearming unknown work."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from infermatrix_copilot.process_execution import ProcessCleanupError, ProcessOutcome, OwnedProcessCleanupError
from infermatrix_copilot.providers.completion import complete_native
from infermatrix_copilot.trace_store import NativeCompletionRecorder, TraceStore


@pytest.mark.parametrize('observer', [False, True])
def test_cleanup_identity_and_partial_output_are_bound_to_original_attempt(tmp_path, observer):
    store = TraceStore(tmp_path / 'traces', environ={})
    outcome = ProcessOutcome('partial native stdout', 'partial stderr', -1, 'absolute')
    local = OwnedProcessCleanupError({123456: 98765, 123457: None})
    if observer:
        # A receiver must retain the producer's scope rather than recapture it.
        local.scope = {'boot_id': '12345678-1234-1234-1234-123456789abc', 'pid_namespace': 987654}
    error = ProcessCleanupError(outcome)
    error.process_cleanup_error = local
    error.leader_returncode = None
    if observer:
        error = ValueError('observer unavailable')
        error.process_cleanup_error = local
        error.process_outcome = outcome
        error.leader_returncode = None
    transport = SimpleNamespace(complete=lambda **_: (_ for _ in ()).throw(error))
    with pytest.raises(type(error)):
        complete_native(lambda: transport, request={'system':'instructions','messages':[], 'model':'fixture'},
            identity={'provider':'fixture'}, payload={'system':'instructions','prompt':'frozen evidence'},
            recorder=NativeCompletionRecorder(store, strict=True))
    record, _ = next(store.iter_records(kind='model_call'))
    facts = record['result']['process_cleanup']
    assert facts['schema'] == 'owned-process-cleanup/1'
    assert facts['leader_returncode'] is None
    assert facts['scope'] == local.scope
    assert facts['survivors'] == [{'pid':123456,'start_time':98765},{'pid':123457,'start_time':None}]
    assert record['result']['native_outcome_pending'] is True
    assert store.blob(record['outputs']['native_stdout']) == outcome.stdout
    assert store.blob(record['outputs']['native_stderr']) == outcome.stderr
    archive = store.root / 'attempts' / record['result']['native_attempt_id'] / 'attempt.json'
    original = archive.read_bytes()
    meta = json.loads(original)
    assert meta['process_cleanup'] == facts
    TraceStore(store.root, environ={}).validate_native_attempt(record, allow_failed=True)
    meta['process_cleanup']['survivors'][0]['start_time'] += 1
    archive.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match='cleanup'):
        TraceStore(store.root, environ={}).validate_native_attempt(record, allow_failed=True)
    archive.write_bytes(original)


CHILD = '''
import json,sys
from pathlib import Path
from types import SimpleNamespace
from infermatrix_copilot.process_execution import ProcessCleanupError,ProcessOutcome,OwnedProcessCleanupError
from infermatrix_copilot.providers.completion import complete_native
from infermatrix_copilot.providers.original_execution import run_original_native
from infermatrix_copilot.sandbox.base import SandboxPending
root=Path(sys.argv[1]); mode=sys.argv[2]
def dispatch(recorder,store):
    with (root/'dispatches').open('a') as f:f.write('one\\n')
    error=ProcessCleanupError(ProcessOutcome('partial','',0))
    error.process_cleanup_error=OwnedProcessCleanupError({123456:98765})
    return complete_native(lambda:SimpleNamespace(complete=lambda **_:(_ for _ in ()).throw(error)),
        request={'system':'instructions','messages':[],'model':'fixture'},identity={'provider':'fixture'},
        payload={'system':'instructions','prompt':'frozen'},recorder=recorder)
def project(*args):
    with (root/'projections').open('a') as f:f.write('unexpected\\n')
    return {'invalid_terminal_result':True}
try:
    run_original_native(root=root,name='original',identity={'profile':'frozen','location':'local','key':'one'},dispatch=dispatch,project=project)
except ProcessCleanupError:
    assert mode=='first'
except SandboxPending:
    assert mode=='restart'
else:
    raise AssertionError('unknown original work was published as terminal')
assert (root/'dispatches').read_text()=='one\\n'
assert not (root/'projections').exists()
'''


def test_fresh_interpreter_keeps_original_pending_without_dispatch_or_projection(tmp_path):
    for mode in ('first', 'restart'):
        child = subprocess.run([sys.executable, '-c', CHILD, str(tmp_path), mode],
            env=os.environ.copy(), capture_output=True, text=True, timeout=20)
        assert child.returncode == 0, child.stderr
    assert not (tmp_path/'original-result.json').exists()
    store = TraceStore(tmp_path/'traces', environ={})
    records = list(store.iter_records(kind='model_call'))
    assert len(records) == 1
    assert records[0][0]['result']['native_outcome_pending'] is True
