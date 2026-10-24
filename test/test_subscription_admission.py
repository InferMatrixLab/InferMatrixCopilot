"""Native profile admission binds the exact context to the next model dispatch."""
from dataclasses import asdict
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time
import uuid

import pytest

from execution_helpers import contained_codex  # noqa: F401
from infermatrix_copilot.config import Settings
from infermatrix_copilot.kb_service.models import ModelGateway,ModelRole,ModelUnavailable
from infermatrix_copilot.providers.codex import CodexTransport
from infermatrix_copilot.providers.container_execution import ContainerExecution
from infermatrix_copilot.providers.process import ProcessOutcome
from infermatrix_copilot.sandbox.remote import canonical
from infermatrix_copilot.trace_store import NativeCompletionRecorder,TraceStore


def jwt(exp):
    encoded=base64.urlsafe_b64encode(json.dumps({'exp':exp}).encode()).decode().rstrip('=')
    return 'header.'+encoded+'.synthetic-signature'


@pytest.fixture(params=['local','ssh'])
def admitted_setup(contained_codex,tmp_path,monkeypatch,request):
    settings=Settings(_env_file=None)
    auth=Path(settings.native_completion_profiles['codex']['codex_auth_file'])
    data=json.loads(auth.read_text());data['tokens']['access_token']=jwt(int(time.time())+3600)
    auth.write_text(json.dumps(data))
    old_run=ContainerExecution.run
    def run(execution,**kwargs):
        name='imc-'+uuid.uuid4().hex
        binding={'version':1,'name':name,'control':execution.command,'daemon':'fixture',
            'image':kwargs['image'],'argv':kwargs['argv'],'proxy':execution._proxy_identity(),
            'input_sha256':hashlib.sha256(kwargs['input'].encode()).hexdigest(),
            'worker_protocol':kwargs['worker_protocol'],'worker_deadline':kwargs['worker_deadline'],
            'worker_credentials_sha256':execution.credentials_sha256}
        identity={'name':name,'daemon':'fixture','id':'a'*64}
        (execution.root/'binding.json').write_text(json.dumps(binding))
        (execution.root/'container.json').write_text(json.dumps(identity))
        outcome=old_run(execution,**kwargs)
        (execution.root/'result.json').write_text(json.dumps({'name':name,'id':'a'*64,'outcome':asdict(outcome)}))
        return outcome
    monkeypatch.setattr(ContainerExecution,'run',run)
    controls=[]
    if request.param=='ssh':
        legacy=settings.native_completion_profiles['codex']
        config={'kind':'sandbox','backend':legacy,'location':{'kind':'ssh','host':'fixture',
                'python':'/remote/python','state_root':str(tmp_path/'remote'),'ssh':'/bin/true'}}
        monkeypatch.setenv('NATIVE_COMPLETION_PROFILES',json.dumps({'codex':config}))
        settings=Settings(_env_file=None,trace_store_root=str(tmp_path/'traces'))
        from infermatrix_copilot import sandbox_agent
        def ssh(command,*,input,on_line,cleanup=None,**kwargs):
            packet=json.loads(input);controls.append(packet['operation']);frames=[]
            try:
                value=sandbox_agent.perform(packet,frames.append)
                frames.append({'type':'result','value':value});code=0
            except Exception:
                frames.append({'type':'error','code':'rejected'});code=1
            try:
                for frame in frames:on_line('stdout',canonical(frame))
            finally:
                if cleanup:cleanup()
            return ProcessOutcome('','',code)
        monkeypatch.setattr('infermatrix_copilot.sandbox.remote.run_process',ssh)
    else:
        settings=settings.model_copy(update={'trace_store_root':str(tmp_path/'traces')})
    state={'status':'Logged in using ChatGPT','model_calls':0,'auth_calls':0}
    def native(argv,**kwargs):
        if argv[-2:]==['login','status']:
            state['auth_calls']+=1
            return SimpleNamespace(stdout=state['status']+'\n',stderr='',returncode=0)
        state['model_calls']+=1
        events=[{'type':'item.completed','item':{'type':'agent_message','text':'{"ok":true}'}},
                {'type':'turn.completed','usage':{'input_tokens':3,'output_tokens':2}}]
        return SimpleNamespace(stdout='\n'.join(map(json.dumps,events))+'\n',stderr='',returncode=0)
    contained_codex.runner=native
    monkeypatch.setattr(CodexTransport,'cli_path',lambda _:pytest.fail('profile must not inspect host CLI/login'))
    store=TraceStore(settings.trace_store_root)
    gateway=ModelGateway(settings,recorder=NativeCompletionRecorder(store))
    role=ModelRole('generator','codex','fixture')
    return gateway,settings,store,role,auth,state,controls


def test_admission_native_probe_is_not_a_model_and_next_dispatch_is_bound(admitted_setup):
    gateway,settings,store,role,auth,state,controls=admitted_setup
    assert gateway.subscription_billing(role) is True
    assert state['auth_calls']==1 and state['model_calls']==0
    admitted=gateway._subscription_transports[role.label()]
    assert getattr(admitted,'completion_profile',None) is None
    proof=admitted.configured_subscription_admission
    assert proof['context']['credentials']['source_sha256']==hashlib.sha256(auth.read_bytes()).hexdigest()
    assert proof['context']['credentials']['kind']=='codex-chatgpt'
    reply=gateway.call_json(role,system='policy',prompt='evidence')
    assert reply.data=={'ok':True} and reply.cost_usd is None
    assert state['model_calls']==1 and role.label() not in gateway._subscription_transports
    rows=[row for row,_ in store.iter_records()]
    assert [row['kind'] for row in rows].count('auth_probe')==1
    model=next(row for row in rows if row['kind']=='model_call')
    assert model['result']['subscription'] is True and model['result']['cost_usd'] is None
    store.validate_native_attempt(model)


def test_admitted_task_replays_original_model_without_another_auth_or_model(admitted_setup):
    from copy import deepcopy
    from infermatrix_copilot.kb_service.init_budget import Budget
    from infermatrix_copilot.kb_service.init_replay import OriginalTaskReplay, original_task_records
    from infermatrix_copilot.kb_service.quality_review import _attempt_scope, _dispatch_key
    gateway,settings,store,role,auth,state,controls=admitted_setup
    binding={'owner':'original','input_sha256':'a'*64}
    scope=_dispatch_key(binding),1
    budget=Budget(1,calls={})
    with _attempt_scope(*scope):
        with budget.reserve(.5,accounting='fixed') as reservation:
            assert gateway.subscription_billing(role)
            reply=gateway.call_json(role,system='policy',prompt='evidence')
            reservation.charged=.5
    assert state['auth_calls']==1 and state['model_calls']==1
    assert {row['kind'] for row,_ in store.iter_records()}=={'auth_probe','model_call'}
    calls=original_task_records(store,binding=binding,attempt=1)
    assert len(calls)==1 and calls[0]['kind']=='model_call'
    account=deepcopy(budget.calls)
    journals={p:p.read_bytes() for p in (store.root/'records').glob('*.jsonl')}
    replay=OriginalTaskReplay(store,calls,binding=binding,attempt=1,subscription_roles=[role.label()])
    with _attempt_scope(*scope):
        with replay.reserve(.5,accounting='fixed') as reservation:
            assert replay.subscription_billing(role)
            recovered=replay.call_json(role,system='policy',prompt='evidence')
            reservation.charged=.5
    assert recovered==reply and replay.consumed_records()==calls
    assert state['auth_calls']==1 and state['model_calls']==1 and budget.calls==account
    assert all(p.read_bytes()==value for p,value in journals.items())


@pytest.mark.parametrize('change',['credentials','config','expiry','proof','release','store'])
def test_changed_admission_never_reaches_model_or_fallback(admitted_setup,change):
    gateway,settings,store,role,auth,state,controls=admitted_setup
    assert gateway.subscription_billing(role)
    admitted=gateway._subscription_transports[role.label()]
    if change=='credentials':
        data=json.loads(auth.read_text());data['tokens']['access_token']=jwt(int(time.time())+7200)
        auth.write_text(json.dumps(data))
    elif change=='config':
        config=settings.native_completion_profiles['codex']
        (config['backend'] if 'backend' in config else config)['image']='sha256:'+'b'*64
    elif change=='expiry':
        # Retained proof changed on disk and in memory; expiry still fails.
        proof=admitted.configured_subscription_admission
        proof['context']['credentials']['expires_at']=int(time.time())-1
        (Path(proof['root'])/'admission.json').write_text(json.dumps(proof,sort_keys=True))
    elif change=='release':
        proof=admitted.configured_subscription_admission
        proof['context']['release']='0'*64
        (Path(proof['root'])/'admission.json').write_text(json.dumps(proof,sort_keys=True))
    elif change=='store':
        gateway._recorder=NativeCompletionRecorder(TraceStore(store.root.parent/'different-store'))
    else:
        (Path(admitted.configured_subscription_admission['root'])/'admission.json').unlink()
    with pytest.raises(ModelUnavailable) as error:
        gateway.call_json(role,system='policy',prompt='evidence')
    assert error.value.allow_fallback is False and state['model_calls']==0


def test_failed_control_journal_cannot_admit_subscription(admitted_setup,monkeypatch):
    gateway,settings,store,role,auth,state,controls=admitted_setup
    def failed_append(*args,**kwargs):
        raise OSError('control journal unavailable')
    monkeypatch.setattr(store,'append',failed_append)
    with pytest.raises(OSError,match='control journal unavailable'):
        gateway.subscription_billing(role)
    assert state['auth_calls']==1 and state['model_calls']==0
    assert role.label() not in gateway._subscription_transports


@pytest.mark.parametrize('bad',['api_status','expired','unknown_expiry'])
def test_unknown_or_expired_native_context_is_never_admitted(admitted_setup,bad):
    gateway,settings,store,role,auth,state,controls=admitted_setup
    if bad=='api_status':state['status']='Logged in using an API key'
    else:
        data=json.loads(auth.read_text())
        data['tokens']['access_token']=jwt(int(time.time())-1) if bad=='expired' else 'opaque-unknown-access-token'
        auth.write_text(json.dumps(data))
    assert gateway.subscription_billing(role) is False
    assert not gateway._subscription_transports and state['model_calls']==0
