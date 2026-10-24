"""Reply, trace and domain accounts consume the same attested native usage."""
from types import SimpleNamespace
import pytest
from infermatrix_copilot.agent_loop import AgentOutcome
from infermatrix_copilot.budgeting import bind_call_budget
from infermatrix_copilot.providers.codex import CodexTransport
from infermatrix_copilot.providers.completion import complete_native
from infermatrix_copilot.trace_store import TraceStore,bind_store

KNOWN={'input_tokens':10,'output_tokens':2,'input_tokens_include_cache':True}
EVENT={'type':'native.codex.event','payload':{'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}}

@pytest.mark.parametrize('declared,events,expected',[
    (None,[],{}), ({},[],{}),
    (None,[{'type':'native.codex.event','payload':{'type':'turn.completed'}}],{}),
    (None,[EVENT],KNOWN), ({},[EVENT],KNOWN),
    ({'input_tokens':0,'output_tokens':0},[EVENT],{'input_tokens':0,'output_tokens':0}),
    ({'input_tokens':7},[],{'input_tokens':7}),
],ids=['legacy-unattested','explicit-unknown','missing-native-counters','recovered-native','recovered-empty','reported-zero','partial-known'])
def test_session_usage_has_one_projection(tmp_path,declared,events,expected):
    outcome=AgentOutcome(text='answer',iterations=1,tool_calls=0,truncated=False,refusals=[],
                         input_tokens=100,output_tokens=20,usage=declared)
    def run(request):
        for event in events:request.native_event_sink(event)
        return outcome
    transport=SimpleNamespace(run_session=run,native_snapshot=CodexTransport.native_snapshot)
    request=SimpleNamespace(system='s',prompt='p',model='fixture',step_name='test')
    store=TraceStore(tmp_path/'traces',environ={})
    accounts=[]
    with bind_store(store),bind_call_budget('test',lambda request:None,
            lambda ticket,facts:accounts.append(facts['usage'])):
        reply,value,entry,_=complete_native(lambda:transport,request={'system':'s','messages':[],'model':'fixture'},
            identity={'provider':'codex'},session_request=request,capture=True)
    trace,=store.query(kind='model_call')
    assert reply.usage==entry['usage']==trace['usage']==(accounts[0] or {})==expected
    assert value is outcome and outcome.usage==declared
    assert (outcome.input_tokens,outcome.output_tokens)==(100,20)
    assert not hasattr(request,'native_event_sink')
    if declared is None and not events:assert accounts==[None]
