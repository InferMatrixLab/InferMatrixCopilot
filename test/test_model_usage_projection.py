"""Per-request usage projection, independent of review outcome or legacy totals."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.engine.step import StepContext
from infermatrix_copilot.execution_artifacts import native_session_dir
from infermatrix_copilot.llm import LLM
from infermatrix_copilot.metrics import collect_run_metrics, cost_from_records
from infermatrix_copilot.model_usage import bind_model_usage
from infermatrix_copilot.run_trace import RunTrace
from infermatrix_copilot.trace_store import TraceStore, bind_store, trace_context


def append_call(store, run, *, tokens=100, output=20, attempt='session-1', **result):
    return store.append('model_call', context={'run_id':run.name},
        model={'model':'fixture-model','provider':'codex','role':'investigator'},
        usage={'input_tokens':tokens,'output_tokens':output},
        result={'native_attempt_id':attempt, **result})


@pytest.mark.parametrize('response,mismatch', [('valid',False), ('malformed',False), ('valid',True)])
@pytest.mark.parametrize('retain_payload', [False, True])
def test_real_auxiliary_client_counts_usage_even_when_output_is_rejected(tmp_path, monkeypatch, response, mismatch, retain_payload):
    from infermatrix_copilot.engine.steps.review.refinement import _promote_uncovered
    run=tmp_path/'run-usage';run.mkdir()
    settings=Settings(_env_file=None, llm_provider='openai',openai_api_key='fixture',
                      agent_model='fixture-model',trace_store_root=str(tmp_path/'global-traces') if retain_payload else '')
    calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model='different-model' if mismatch else kwargs['model'],id='fixture-request',
            choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(
                content='{"additions":[]}' if response=='valid' else 'no valid JSON',tool_calls=[]))],
            usage=SimpleNamespace(prompt_tokens=13538,completion_tokens=52,
                prompt_tokens_details=SimpleNamespace(cached_tokens=9088),
                completion_tokens_details=SimpleNamespace(reasoning_tokens=41)))
    monkeypatch.setattr('infermatrix_copilot.llm._build_client',lambda *args:SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    client=LLM(settings)
    trace=RunTrace(run/'run_trace.jsonl')
    trace.record('agent_output',input_tokens=100,output_tokens=20,tool_calls=2)
    store=TraceStore(tmp_path/'global-traces')
    local=TraceStore(native_session_dir(run,'investigator')/'traces')
    append_call(local,run)
    append_call(store,run)  # mirrored native attempt: not a second charge
    append_call(store,tmp_path/'foreign-run',tokens=999999,output=999999,attempt='foreign')
    # Span and event totals are old compatibility views, never additional calls.
    (run/'trace.jsonl').write_text(json.dumps({'name':'llm','attr':{
        'model':'claude-sonnet','prompt_tokens':777777,'completion_tokens':777777}})+'\n')
    ctx=StepContext(settings,{}, {},run,trace,client)
    original={'findings':['A current supported requirement lacks a test.'],'review_comments':[]}
    with bind_model_usage(trace,run.name),bind_store(store if retain_payload else None),trace_context(run_id=run.name):
        result=asyncio.run(_promote_uncovered(ctx,original,{}))
    assert result==original and len(calls)==1
    metrics=collect_run_metrics(run,settings,'done')['cost']
    assert metrics['source']=='model_calls'
    assert (metrics['input_tokens'],metrics['output_tokens'],metrics['llm_calls'])==(13638,72,2)
    assert metrics['tool_calls']==2
    assert metrics['actual_usd'] is None and metrics['cost_partial'] is True
    if not retain_payload:
        assert len(list(store.iter_records(kind='model_call')))==2  # setup only; no API payload stored
    assert 'current supported requirement' not in str(list(trace.events('model_call_usage')))


def test_unknown_usage_and_corrupt_journal_are_explicit_partials(tmp_path):
    run=tmp_path/'run';run.mkdir();root=tmp_path/'traces';store=TraceStore(root)
    append_call(store,run,tokens=None,output=7)
    settings=Settings(_env_file=None,trace_store_root=str(root))
    projected=cost_from_records(run,settings)
    assert projected['usage_partial'] and projected['cost_partial'] and projected['actual_usd'] is None
    assert projected['input_tokens']==0 and projected['output_tokens']==7
    journal=next((root/'records').glob('*.jsonl'))
    with journal.open('a') as f:f.write('{corrupt secret-like data\n')
    projected=cost_from_records(run,settings)
    assert projected['cost_partial'] and projected['record_read_errors'][0]['error_type']=='JSONDecodeError'
    assert 'secret-like' not in str(projected)


def test_conflicting_mirrors_are_not_silently_selected(tmp_path):
    run=tmp_path/'run';run.mkdir();root=tmp_path/'traces';store=TraceStore(root)
    append_call(store,run,tokens=100)
    append_call(store,run,tokens=200)
    projected=cost_from_records(run,Settings(_env_file=None,trace_store_root=str(root)))
    assert projected['llm_calls']==1 and projected['conflicting_calls']==1
    assert projected['usage_partial'] and projected['cost_partial'] and projected['actual_usd'] is None


def test_reported_zero_cost_and_predispatch_refusal_are_not_estimated(tmp_path):
    run=tmp_path/'run';run.mkdir();store=TraceStore(tmp_path/'traces')
    append_call(store,run,cost_usd=0)
    append_call(store,run,attempt='refused',tokens=None,output=None,native_call_sent=False)
    projected=cost_from_records(run,Settings(_env_file=None,trace_store_root=str(tmp_path/'traces')))
    assert projected['actual_usd']==0 and projected['usd']==0
    assert projected['llm_calls']==1 and projected['not_sent_calls']==1
    assert not projected['usd_is_estimate'] and not projected['cost_partial']


def test_unowned_old_records_do_not_replace_legacy_run_totals(tmp_path):
    run=tmp_path/'run';run.mkdir();store=TraceStore(tmp_path/'traces')
    store.append('model_call',usage={'input_tokens':999999,'output_tokens':999999})
    trace=RunTrace(run/'run_trace.jsonl');trace.record('agent_output',input_tokens=12,output_tokens=3)
    settings=Settings(_env_file=None,trace_store_root=str(tmp_path/'traces'))
    assert cost_from_records(run,settings) is None
    metrics=collect_run_metrics(run,settings,'done')['cost']
    assert metrics['source']=='events' and metrics['input_tokens']==12 and metrics['output_tokens']==3


def test_inconsistent_legacy_totals_do_not_fabricate_a_complete_bill(tmp_path):
    run=tmp_path/'run';run.mkdir();store=TraceStore(tmp_path/'traces')
    append_call(store,run,cost_usd=1)
    trace=RunTrace(run/'run_trace.jsonl');trace.record('agent_output',input_tokens=200,output_tokens=40)
    m=collect_run_metrics(run,Settings(_env_file=None,trace_store_root=str(tmp_path/'traces')),'done')['cost']
    assert m['source']=='model_calls' and m['input_tokens']==100 and m['output_tokens']==20
    assert m['usage_partial'] and m['cost_partial'] and m['legacy_usage_exceeds_records']
    assert m['actual_usd'] is None and m['cost_index'] is None


def test_sdk_diagnostics_preserve_partial_flags_and_old_cost_shape(tmp_path):
    from infermatrix_copilot.contract import build_review_result
    run=tmp_path/'run';run.mkdir()
    old={'usd':0.25,'minutes':1,'input_tokens':12,'output_tokens':3}
    (run/'metrics.json').write_text(json.dumps({'cost':old}))
    assert build_review_result(run)['diagnostics']['cost']==old
    store=TraceStore(tmp_path/'traces');append_call(store,run,tokens=None,output=7)
    collect_run_metrics(run,Settings(_env_file=None,trace_store_root=str(tmp_path/'traces')),'done')
    cost=build_review_result(run)['diagnostics']['cost']
    assert cost['source']=='model_calls' and cost['actual_usd'] is None
    assert cost['cost_partial'] and cost['usage_partial'] and cost['llm_calls']==1


@pytest.mark.parametrize('failure', ['usage_projection', 'start_and_usage', 'payload_capture'])
def test_diagnostic_failure_does_not_repeat_a_completed_model_call(tmp_path, monkeypatch, failure):
    from infermatrix_copilot.llm import Block, Reply, capture_model_call
    run=tmp_path/'run';run.mkdir();trace=RunTrace(run/'run_trace.jsonl');store=TraceStore(tmp_path/'traces')
    original=trace.record
    def record(kind, **fields):
        if kind=='model_call_usage' and failure in ('usage_projection','start_and_usage'):raise OSError('fixture diagnostic unavailable')
        if kind=='model_usage_capture' and fields.get('phase')=='start' and failure=='start_and_usage':raise OSError('fixture start marker unavailable')
        return original(kind,**fields)
    monkeypatch.setattr(trace,'record',record)
    if failure=='payload_capture':
        def denied(*args,**kwargs):raise OSError('fixture payload storage unavailable')
        monkeypatch.setattr(store,'put_blob',denied)
    reply=Reply([Block(type='text',text='private reply body')],usage={'input_tokens':31,'output_tokens':9})
    with bind_model_usage(trace,run.name),bind_store(store if failure=='payload_capture' else None),trace_context(run_id=run.name):
        capture_model_call({'model':'fixture','system':'private request body'},'reducer','api',reply,.01)
    m=collect_run_metrics(run,Settings(_env_file=None),'done')['cost']
    assert m['actual_usd'] is None and m['cost_partial']
    if failure=='payload_capture':
        assert m['llm_calls']==1 and m['input_tokens']==31 and m['output_tokens']==9
        assert not m['usage_partial']
    else:
        assert m['usage_capture_incomplete'] and m['usage_partial'] and m['cost_index'] is None
    assert 'private reply body' not in trace.path.read_text() and 'private request body' not in trace.path.read_text()


def test_interrupted_capture_cannot_be_reported_as_complete_zero_cost(tmp_path):
    run=tmp_path/'run';run.mkdir();trace=RunTrace(run/'run_trace.jsonl')
    trace.record('model_usage_capture',capture_id='interrupted',phase='start',observed=0,saved=0,errors=0)
    m=collect_run_metrics(run,Settings(_env_file=None),'interrupted')['cost']
    assert m['usage_capture_incomplete'] and m['cost_partial'] and m['actual_usd'] is None
    assert m['cost_index'] is None


def test_model_invoice_is_not_misrepresented_as_actual_run_cost_with_ci(tmp_path):
    run=tmp_path/'run';run.mkdir();store=TraceStore(tmp_path/'traces')
    append_call(store,run,cost_usd=1)
    trace=RunTrace(run/'run_trace.jsonl');trace.record('ci_build',minutes=2)
    cost=collect_run_metrics(run,Settings(_env_file=None,trace_store_root=str(tmp_path/'traces'),ci_rate_usd_per_min=.5),'done')['cost']
    assert cost['usd']==2 and cost['actual_model_usd']==1
    assert cost['actual_usd'] is None and cost['usd_is_estimate']


def test_executor_injects_threaded_metadata_capture_and_resume_does_not_duplicate(tmp_path):
    from infermatrix_copilot.engine import Executor, StepRegistry, StepResult, StepSpec
    from infermatrix_copilot.llm import Block, Reply, capture_model_call
    from infermatrix_copilot.playbooks.store import Playbook, PlaybookStep
    run=tmp_path/'run';trace=RunTrace(run/'run_trace.jsonl');settings=Settings(_env_file=None,trace_store_root='')
    dispatched=[]
    async def handler(ctx):
        dispatched.append(True)
        await asyncio.to_thread(capture_model_call,{'model':'fixture','system':'private request'},
            'fixture','api',Reply([Block(type='text',text='private reply')],usage={'input_tokens':31,'output_tokens':9}),.01)
        return StepResult(True)
    registry=StepRegistry();registry.register(StepSpec('fixture.model','agent','read',handler))
    pb=Playbook(name='usage',version=1,status='active',task_kinds=['pr_review'],repos=[],
                steps=[PlaybookStep('call','fixture.model')])
    executor=Executor(registry,settings,run_dir=run,trace=trace)
    assert asyncio.run(executor.run(pb,{})).status=='done'
    assert asyncio.run(executor.run(pb,{})).status=='done'
    cost=collect_run_metrics(run,settings,'done')['cost']
    assert dispatched==[True] and cost['llm_calls']==1
    assert (cost['input_tokens'],cost['output_tokens'])==(31,9)
    assert not cost['usage_capture_incomplete']
    assert 'private request' not in trace.path.read_text() and 'private reply' not in trace.path.read_text()
