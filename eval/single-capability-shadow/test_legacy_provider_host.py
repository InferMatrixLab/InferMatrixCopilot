"""A legacy SDK may opt into a pinned current host, never a substitute provider."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ledger import Refused
from legacy_provider_host import LegacyTransport, host_binding, serve
from test_mixed_provider_runtime import mixed  # noqa: F401
from test_zcode_sandbox_completion import sandbox  # noqa: F401
from test_completion_profile_configuration import configured  # noqa: F401
from test_readonly_sandbox import worker_receipt  # noqa: F401
from test_arm_runtime import settings  # noqa: F401


def binding(package):
    from infermatrix_copilot.source_fingerprint import package_tree_hash
    script = Path(__file__).with_name('legacy_provider_host.py').resolve()
    return {'python': sys.executable, 'python_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        'script': str(script), 'script_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
        'package_sha256': package_tree_hash(package)}


def setup(mixed):
    import infermatrix_copilot
    settings, calls, shim, plan = mixed
    plan['legacy_provider_host'] = binding(Path(infermatrix_copilot.__file__).parent)
    path = shim.parent / 'config.json';config=json.loads(path.read_bytes());config['plan']=plan;path.write_text(json.dumps(config))
    return calls, shim, path, plan


def test_host_delegates_both_roles_to_their_canonical_adapters_and_account(mixed):
    calls, shim, path, plan = setup(mixed)
    for name in ('generator','judge'):
        role=plan['execution_profile']['roles'][name]
        value=serve(path, {'operation':'complete','provider':role['provider'],'context':{},'request':{
            'system':'test','messages':[{'role':'user','content':'frozen '+name}],
            'role':name,'model':role['model'],'effort':role['effort']}})
        assert value['reply']['blocks'][0]['text']
    import sqlite3
    with sqlite3.connect(shim.parent.parent/'calls.sqlite') as db:
        assert db.execute("SELECT count(*) FROM calls WHERE status='complete'").fetchone()[0]==2
    with pytest.raises(Refused, match='cannot be replaced'):
        serve(path, {'operation':'complete','provider':'codex','context':{},'request':{
            'system':'test','messages':[{'role':'user','content':'frozen judge'}],
            'role':'judge','model':'fixture','effort':'high'}})


def test_unbound_or_drifted_host_refuses_before_process(mixed, monkeypatch):
    calls, shim, path, plan = setup(mixed)
    client=LegacyTransport(path,'zcode')
    config=json.loads(path.read_bytes());config['plan']['legacy_provider_host']['package_sha256']='a'*64;path.write_text(json.dumps(config))
    monkeypatch.setattr(subprocess,'run',lambda *a,**kw: pytest.fail('must refuse before host launch'))
    with pytest.raises(Refused, match='configuration changed'):
        client.complete(system='test')
    assert not calls
    with pytest.raises(Refused, match='freeze the canonical'):
        host_binding({})


def test_old_reply_projection_preserves_model_usage_and_never_retries(mixed, monkeypatch):
    _,_,path,_=setup(mixed);client=LegacyTransport(path,'zcode');calls=[]
    def run(*args,**kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args,0,json.dumps({'schema':'legacy-provider-host/1','reply':{
            'blocks':[{'type':'text','text':'{"ok":true}'}],'usage':{'input_tokens':0},'model':'GLM-5.3','stop_reason':'end_turn','request_id':''}}),'')
    monkeypatch.setattr(subprocess,'run',run)
    reply=client.complete(system='test',messages=[])
    assert reply.usage=={'input_tokens':0} and reply.model=='GLM-5.3'
    assert len(calls)==1 and 'PYTHONPATH' not in calls[0]['env']
    monkeypatch.setattr(subprocess,'run',lambda *a,**kw: subprocess.CompletedProcess(a,1,'',''))
    with pytest.raises(Refused, match='reconciliation'):
        client.complete(system='test',messages=[])


def test_host_package_drift_precedes_native_dispatch(mixed):
    calls,_,path,plan=setup(mixed)
    config=json.loads(path.read_bytes());config['plan']['legacy_provider_host']['package_sha256']='a'*64;path.write_text(json.dumps(config))
    with pytest.raises(Refused,match='package changed'):
        serve(path,{'operation':'capabilities','provider':'zcode','request':None,'context':{}})
    assert not calls


def test_legacy_host_process_uses_the_selected_case_orchestration_deadline(mixed, monkeypatch):
    _, _, path, _ = setup(mixed)
    config = json.loads(path.read_text())
    config['plan']['case_deadlines'] = {'case': {
        'native_timeout_s': 300, 'host_timeout_s': 360, 'workflow_timeout_s': 1500}}
    path.write_text(json.dumps(config))
    client = LegacyTransport(path, 'zcode')
    observed = []
    def complete(*args, **kwargs):
        observed.append(kwargs['timeout'])
        return subprocess.CompletedProcess(args, 0, json.dumps({'schema': 'legacy-provider-host/1',
            'reply': {'blocks': [{'type': 'text', 'text': '{"ok":true}'}], 'usage': {},
                      'model': 'GLM-5.3', 'stop_reason': 'end_turn', 'request_id': ''}}), '')
    monkeypatch.setattr(subprocess, 'run', complete)
    assert client.complete(system='test', messages=[]).model == 'GLM-5.3'
    assert observed == [360]


def test_completion_binds_admission_to_its_transport_instead_of_reusing_a_boolean(mixed, monkeypatch):
    from dataclasses import replace
    from infermatrix_copilot.llm import Reply, Block
    calls,_,path,plan=setup(mixed);marker=object();seen=[]
    monkeypatch.setattr('infermatrix_copilot.providers.completion_profiles.configured_credential_source',lambda config: {'configured':True})
    def admit(settings, transport):
        transport.admission_marker=marker
        return transport
    monkeypatch.setattr('infermatrix_copilot.providers.subscription_admission.admit_subscription',admit)
    def complete(factory, **kwargs):
        assert factory().admission_marker is marker
        seen.append(kwargs['identity']['provider'])
        return Reply([Block(type='text',text='{"ok":true}')]),None,{},None
    monkeypatch.setattr('infermatrix_copilot.providers.completion.complete_native',complete)
    packet={'operation':'complete','provider':'zcode','context':{},'request':{
        'system':'test','messages':[],'role':'generator','model':'GLM-5.3','effort':''}}
    assert serve(path,packet)['reply']['blocks'][0]['text']=='{"ok":true}'
    assert seen==['zcode']
    monkeypatch.setattr('infermatrix_copilot.providers.subscription_admission.admit_subscription',lambda *args:None)
    with pytest.raises(Refused,match='not admitted'):
        serve(path,packet)
    assert seen==['zcode'] and not calls


def test_legacy_durable_payload_omission_refuses_before_host_launch(mixed, monkeypatch):
    from arm_runtime import model_gateway
    from infermatrix_copilot.kb_service.models import ModelRole
    settings,calls,shim,_=mixed
    setup(mixed)
    monkeypatch.setattr('arm_runtime.supports_shared_execution',lambda:False)
    monkeypatch.setattr(subprocess,'run',lambda *a,**kw: pytest.fail('no RPC for omitted durable payloads'))
    gateway=model_gateway(settings,shim=shim)
    with pytest.raises(Refused,match='explicit payload retention'):
        gateway.call_json(ModelRole('generator','zcode','GLM-5.3'),system='history',prompt='private',record_payload=False)
    assert not calls


def test_original_plan_is_bound_before_the_first_legacy_transport_exists(mixed, monkeypatch):
    from arm_runtime import model_gateway
    from infermatrix_copilot.kb_service.models import ModelRole
    settings,calls,shim,_=mixed
    _,_,path,_=setup(mixed)
    monkeypatch.setattr('arm_runtime.supports_shared_execution',lambda:False)
    gateway=model_gateway(settings,shim=shim)
    config=json.loads(path.read_bytes());config['plan']['host_timeout_s']=600;path.write_text(json.dumps(config))
    monkeypatch.setattr(subprocess,'run',lambda *a,**kw: pytest.fail('no host launch after plan drift'))
    with pytest.raises(Refused,match='configuration changed'):
        gateway.subscription_billing(ModelRole('generator','zcode','GLM-5.3'))
    assert not calls


def test_inherited_channel_delivers_events_before_reply_in_callers_context(tmp_path):
    import threading
    from legacy_provider_host import run_host
    script=tmp_path/'host.py'
    script.write_text('''import json,socket,sys
packet=json.load(sys.stdin)
with socket.socket(fileno=packet['event_fd']) as sock, sock.makefile('rwb') as stream:
 for i in (1,2):
  stream.write(json.dumps({'event':{'type':'progress','index':i}}).encode()+b'\\n');stream.flush()
  assert json.loads(stream.readline())=={'ok':True}
print(json.dumps({'done':True}))
''')
    events=[];caller=threading.get_ident()
    def observe(event):
        assert threading.get_ident()==caller
        events.append(event)
    result=run_host([sys.executable,'-I','-B',str(script)],{},timeout=5,environment={},observer=observe)
    assert json.loads(result.stdout)=={'done':True}
    assert [v['index'] for v in events]==[1,2]


def test_rejected_event_consumer_cannot_be_reentered_by_late_frames(tmp_path):
    from legacy_provider_host import run_host
    script=tmp_path/'host.py'
    script.write_text('''import json,socket,sys
packet=json.load(sys.stdin)
with socket.socket(fileno=packet['event_fd']) as sock, sock.makefile('rwb') as stream:
 for i in (1,2):
  stream.write(json.dumps({'event':{'index':i}}).encode()+b'\\n');stream.flush()
  assert json.loads(stream.readline())=={'ok':False}
raise SystemExit(1)
''')
    events=[]
    def reject(event):
        events.append(event)
        raise ValueError('stop callback')
    with pytest.raises(ValueError,match='stop callback'):
        run_host([sys.executable,'-I','-B',str(script)],{},timeout=5,environment={},observer=reject)
    assert events==[{'index':1}]


def test_host_streams_canonical_events_and_decodes_partial_usage_once(mixed):
    calls,_,path,plan=setup(mixed);events=[]
    packet={'operation':'complete','provider':'zcode','context':{},'request':{
        'system':'test','messages':[{'role':'user','content':'frozen event source'}],
        'role':'generator','model':'GLM-5.3','effort':''}}
    assert serve(path,packet,observer=events.append)['reply']['blocks']
    assert any(event.get('type')=='result' for event in events)
    partial=[{'type':'session.updated','payload':{'modelId':'GLM-5.3'}},
             {'type':'result','usage':{'inputTokens':0}}]
    before=len(calls)
    value=serve(path,{'operation':'snapshot','provider':'zcode','context':{},'request':{'events':partial}})['snapshot']
    assert value['served_model']=='GLM-5.3' and value['usage']=={'input_tokens':0}
    assert len(calls)==before


def test_consumer_failure_cannot_replace_the_original_native_intent(mixed):
    import sqlite3
    calls,shim,path,_=setup(mixed)
    packet={'operation':'complete','provider':'zcode','context':{},'request':{
        'system':'test','messages':[{'role':'user','content':'consumer rejection'}],
        'role':'generator','model':'GLM-5.3','effort':''}}
    def reject(event):
        raise ValueError('original consumer rejected the event')
    with pytest.raises((ValueError,RuntimeError)):
        serve(path,packet,observer=reject)
    with sqlite3.connect(shim.parent.parent/'calls.sqlite') as db:
        before=db.execute('SELECT id,status,result FROM calls').fetchall()
    assert len(before)==1
    dispatched=len(calls)
    with pytest.raises(Refused):
        serve(path,packet)
    assert len(calls)==dispatched
    with sqlite3.connect(shim.parent.parent/'calls.sqlite') as db:
        assert db.execute('SELECT id,status,result FROM calls').fetchall()==before


def test_presentation_decode_delegates_without_native_or_account_calls(mixed, monkeypatch):
    calls, shim, path, plan = setup(mixed)
    from arm_runtime import ArmRuntime
    monkeypatch.setattr(ArmRuntime, 'transport', lambda *a, **kw: pytest.fail('decode prepared native identity'))
    monkeypatch.setattr(ArmRuntime, 'budget', lambda *a, **kw: pytest.fail('decode entered model budget'))
    result = serve(path, {'operation': 'decode_object', 'provider': 'codex', 'context': {},
                         'request': {'text': '<untrusted_data>\n{"complete_diff":"frozen"}\n</untrusted_data>\nReturn JSON only.'}})
    assert result['object'] == {'complete_diff': 'frozen'}
    assert not calls
