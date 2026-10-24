"""Broker policy through real supervised I/O; namespace boundary is scripted.

The separate native-namespace test retains its confinement assertions. These
workers only emit frozen test messages and never contact a model provider.
"""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from infermatrix_copilot.improve import artifacts, isolation
from infermatrix_copilot.improve.budget import current_governor
from infermatrix_copilot.llm import Block, Reply
from infermatrix_copilot.trace_store import current_context


@pytest.fixture
def broker(tmp_path,monkeypatch):
    checkout=tmp_path/'checkout';checkout.mkdir()
    (checkout/'src').mkdir();(checkout/'src/candidate.py').write_text('# frozen\n')
    for command in (['init','-q'],['add','.'],['-c','user.name=test','-c','user.email=test@invalid','commit','-qm','fixture']):
        subprocess.run(['git','-C',str(checkout),*command],check=True,capture_output=True)
    source=tmp_path/'source';metadata=artifacts.baseline(checkout,source)
    sandbox=isolation.Sandbox()
    monkeypatch.setattr(sandbox,'check',lambda:{'ready':True,'reason':''})
    settings=SimpleNamespace(llm_max_tokens=20,tier_target=lambda _:SimpleNamespace(model='declared'))
    governor=object();calls=[]
    def create(**request):
        assert current_governor() is governor and current_context()['item']=='original'
        calls.append(request)
        return Reply(blocks=[Block(type='text',text='original reply')],stop_reason='end_turn',usage={})
    llm=SimpleNamespace(create=create)
    def run(script,*,payload=None,timeout=3):
        monkeypatch.setattr(isolation,'argv',lambda *_:[sys.executable,'-u','-c',script])
        return sandbox.run(source,payload or {'trace_context':{'item':'original'}},tmp_path/'work',
                           llm=llm,settings=settings,governor=governor,timeout=timeout)
    result={'source_sha':metadata['tree_sha'],'package_path':'/candidate/src/worker.py','ok':True}
    request={'type':'model_call','request':{'model':'declared','max_tokens':10,'messages':[]}}
    return run,calls,result,request,source


def test_broker_roundtrip_preserves_policy_source_and_stderr_report(broker):
    run,calls,result,request,source=broker
    script=f"""
import json,sys
print({json.dumps(json.dumps(request))},flush=True)
sys.stderr.write('diagnostic\\n'*10000);sys.stderr.flush()
reply=json.loads(sys.stdin.readline());assert reply['blocks'][0]['text']=='original reply'
print({json.dumps(json.dumps({'type':'result','result':result}))},flush=True)
"""
    actual=run(script,payload={'mode':'tests','trace_context':{'item':'original'}})
    assert len(calls)==1 and actual=={**result,'_broker_calls':1,'report':'diagnostic\n'*100}
    assert artifacts.verify(source)['tree_sha']==result['source_sha']


@pytest.mark.parametrize('damage',['model','tokens','fields','overlap','source','missing','partial'])
def test_invalid_broker_output_never_becomes_a_success_or_model_dispatch(broker,damage):
    run,calls,result,request,source=broker
    if damage=='model':request['request']['model']='undeclared'
    elif damage=='tokens':request['request']['max_tokens']=21
    elif damage=='fields':request['request']['provider']='untrusted'
    if damage in {'model','tokens','fields'}:wire=json.dumps(request)+'\n'
    elif damage=='overlap':wire=json.dumps(request)+'\n'+json.dumps(request)+'\n'
    elif damage=='source':wire=json.dumps({'type':'result','result':{**result,'source_sha':'wrong'}})+'\n'
    elif damage=='missing':wire=''
    else:wire=json.dumps(request) # no terminal delimiter
    with pytest.raises(isolation.SandboxUnavailable):
        run('import sys;sys.stdout.write('+repr(wire)+');sys.stdout.flush()')
    assert calls==[] and artifacts.verify(source)['tree_sha']==result['source_sha']


def test_broker_worker_timeout_is_owned_by_shared_supervisor(broker):
    run,calls,result,request,source=broker
    with pytest.raises(isolation.SandboxUnavailable,match='candidate timed out'):
        run('import time;time.sleep(30)',timeout=.2)
    assert calls==[]


@pytest.mark.parametrize('stop', ['rejection', 'terminated'])
def test_stopped_or_rejected_broker_cannot_dispatch_queued_requests(broker,monkeypatch,stop):
    import threading
    run,calls,result,request,source=broker
    def supervised(*args,**kwargs):
        channel=kwargs['input_channel'];stopped=threading.Event();channel._claim(stopped)
        monkeypatch.setattr(channel,'send',lambda _:None)
        receive=kwargs['on_chunk']
        if stop=='terminated':
            stopped.set()
            receive('stdout',(json.dumps(request)+'\n').encode())
        else:
            rejected={**request,'request':{**request['request'],'model':'undeclared'}}
            try:
                receive('stdout',(json.dumps(rejected)+'\n').encode())
            except isolation.SandboxUnavailable as original:
                # Reproduce the supervisor's best-effort drain after callback
                # rejection, even while the original stop signal is delayed.
                try:receive('stdout',(json.dumps(request)+'\n').encode())
                except isolation.SandboxUnavailable:pass
                raise original
    monkeypatch.setattr(isolation,'run_process',supervised)
    with pytest.raises(isolation.SandboxUnavailable):
        run('no worker dispatch')
    assert calls==[]
