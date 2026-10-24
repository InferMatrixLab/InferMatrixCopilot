"""Cleanup evidence crosses the same provider-neutral local/SSH profile."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.persistence import immutable_write_bytes
from infermatrix_copilot.process_execution import ProcessCleanupError, ProcessOutcome
from infermatrix_copilot.providers import completion_worker
from infermatrix_copilot.providers.registry import transport_for_id
from infermatrix_copilot.sandbox.base import BoundSandboxProfile
from infermatrix_copilot.sandbox.remote import canonical
from infermatrix_copilot.trace_store import NativeCompletionRecorder, TraceStore
from test_sandbox_execution import Backend, remote  # noqa: F401


WORKER = '''
import sys
from infermatrix_copilot.process_execution import ProcessCleanupError,ProcessOutcome,OwnedProcessCleanupError
from infermatrix_copilot.providers import completion_worker,registry
original=registry._construct_transport
class FailedAdapter:
    def complete(self,**kwargs):
        kwargs['native_event_sink']({'type':'result','response':'partial','usage':{'inputTokens':7,'outputTokens':2}})
        error=ProcessCleanupError(ProcessOutcome('native partial','native diagnostics',-1,'absolute'))
        error.process_cleanup_error=OwnedProcessCleanupError({123456:98765})
        error.process_cleanup_error.scope={'boot_id':'12345678-1234-1234-1234-123456789abc','pid_namespace':987654}
        error.leader_returncode=None
        raise error
registry._construct_transport=lambda settings,provider: original(settings,provider) if settings is None else FailedAdapter()
raise SystemExit(completion_worker.main())
'''


@pytest.mark.parametrize('location', ['local', 'ssh'])
def test_worker_cleanup_survives_location_archive_and_original_reader(tmp_path, monkeypatch, remote, location):
    from infermatrix_copilot.providers.completion import complete_native
    calls = []

    def execute(backend, cmd, *, cwd, env, input=None, on_line=None, **kwargs):
        backend.root.mkdir(parents=True, exist_ok=True)
        immutable_write_bytes(backend.root / 'native-intent', canonical([cmd,cwd,env,input]).encode())
        calls.append(cmd)
        result = subprocess.run([sys.executable, '-c', WORKER], input=input,
            capture_output=True, text=True, timeout=20, env=os.environ.copy())
        assert result.returncode == 1
        outcome = ProcessOutcome(result.stdout, result.stderr, result.returncode)
        immutable_write_bytes(backend.root/'result', json.dumps(asdict(outcome)).encode())
        if on_line:
            for line in result.stdout.splitlines():on_line('stdout', line)
        return outcome

    monkeypatch.setattr(Backend, 'execute', execute)
    executor = Backend(tmp_path/'local', []) if location == 'local' else remote[0]
    profile = BoundSandboxProfile(executor, '/native-zcode')
    native = transport_for_id(Settings(_env_file=None), 'zcode', completion_profile=profile)
    arguments = dict(system='instructions',messages=[],model='fixture',max_tokens=100,role='generator',effort='',max_budget_usd=None)
    proxy = SimpleNamespace(supports_native_events=True,native_snapshot=native.native_snapshot,
        complete=lambda **kw: completion_worker.complete(native,arguments,native_event_sink=kw.get('native_event_sink')))
    store = TraceStore(tmp_path/'traces', environ={})
    with pytest.raises(ProcessCleanupError) as caught:
        complete_native(lambda:proxy,request={'system':'instructions','messages':[], 'model':'fixture'},
            identity={'provider':'zcode'},payload={'system':'instructions','prompt':'frozen'},
            recorder=NativeCompletionRecorder(store,strict=True))
    assert caught.value.native_outcome_pending
    record, _ = next(store.iter_records(kind='model_call'))
    facts = record['result']['process_cleanup']
    assert facts['scope'] == {'boot_id':'12345678-1234-1234-1234-123456789abc','pid_namespace':987654}
    assert facts['survivors'] == [{'pid':123456,'start_time':98765}]
    assert facts['leader_returncode'] is None
    assert store.blob(record['outputs']['native_stdout']) == 'native partial'
    assert store.blob(record['outputs']['native_stderr']) == 'native diagnostics'
    assert record['usage']['input_tokens'] == 7
    assert record['result']['native_outcome_pending']
    TraceStore(store.root,environ={}).validate_native_attempt(record,allow_failed=True)
    if location == 'ssh':
        (executor.root/'remote-result.json').unlink()  # read the bound worker through the SSH protocol
    with pytest.raises(ProcessCleanupError) as original:
        completion_worker.read_result(native,recover=True)
    assert original.value.cleanup_diagnostics == facts
    assert len(calls) == 1


def cleanup_frame():
    return {'schema':completion_worker.SCHEMA,'kind':'cleanup','value':{
        'process_cleanup':{'schema':'owned-process-cleanup/1',
            'scope':{'boot_id':None,'pid_namespace':None},'survivors':[],
            'leader_returncode':None,'error_types':['ProcessCleanupError']}}}


def test_cleanup_frame_cannot_establish_success_even_with_zero_exit():
    outcome={'stdout':json.dumps(cleanup_frame()),'stderr':'','returncode':0,'reason':None}
    receipt=completion_worker.process_receipt(outcome)
    assert not receipt['native_complete'] and receipt['native_outcome_pending']


@pytest.mark.parametrize('damage',['duplicate','result','event_after','bad_identity','extra_payload'])
def test_cleanup_protocol_rejects_conflicting_or_malformed_frames(damage):
    from infermatrix_copilot.llm import Reply,Block
    values=[cleanup_frame()]
    if damage=='duplicate':values.append(cleanup_frame())
    elif damage=='result':values.append({'schema':completion_worker.SCHEMA,'kind':'result','value':asdict(Reply(blocks=[Block(type='text',text='ok')]))})
    elif damage=='event_after':values.append({'schema':completion_worker.SCHEMA,'kind':'event','value':{}})
    elif damage=='bad_identity':values[0]['value']['process_cleanup']['survivors']=[{'pid':True,'start_time':1}]
    else:values[0]['value']['unknown']='must refuse'
    with pytest.raises(ValueError):
        completion_worker.process_receipt({'stdout':'\n'.join(map(json.dumps,values)),'stderr':'','returncode':0,'reason':None})
