"""Standard Strict workflow with real accounting and a scripted native edge."""
from pathlib import Path
import hashlib,json,sys
import pytest
P=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(P/'test'))
sys.path.insert(0,str(P/'eval/single-capability-shadow'))
from test_completion_profile_configuration import configured
from test_arm_runtime import arm,rows
from test_e2e_strict_mock import _pr_checkout,_fake_gh
from infermatrix_copilot.config import Settings
from infermatrix_copilot.providers.codex import CodexTransport
from infermatrix_copilot.providers.container_execution import ContainerExecution
from infermatrix_copilot.providers.container_protocol import receipt_line
from infermatrix_copilot.process_execution import ProcessOutcome
from infermatrix_copilot.engine.agent_runtime import BASE_OUTPUT_SCHEMA
from infermatrix_copilot.engine.steps import _common as common_mod
from infermatrix_copilot.engine.steps.pr import fetch as fetch_mod
from infermatrix_copilot.engine import worktrees as wt_mod
from infermatrix_copilot.sdk.v1 import ReviewRuntime,RepositoryRef,StrictReviewRequest
from arm_runtime import sdk_runtime

@pytest.fixture
def settings(tmp_path,monkeypatch):
    monkeypatch.setenv('KB_RUN_ROOTS',str(tmp_path/'no-runs'))
    return Settings(_env_file=None,run_root=tmp_path/'runs',metrics_enabled=False,
        playbooks_dir=P/'playbooks',repo_paths={},allow_push=False,
        memory_db=tmp_path/'memory.sqlite',ensemble_parallel=False,
        review_verify_concurrency=1,knowledge_dir=P/'knowledge')


@pytest.mark.parametrize('call_limit,expected_state',[(1,'blocked'),(3,'blocked'),(4,'blocked'),(5,'done')])
def test_complete_standard_workflow_respects_original_paid_call_ceiling(arm,tmp_path,monkeypatch,call_limit,expected_state,record_property):
    settings,calls,shim,_=arm
    work,head=_pr_checkout(tmp_path)
    config=shim.parent/'config.json';value=json.loads(config.read_text())
    value['plan']['native_call_limits']={'strict':call_limit}
    value['plan']['call_identity']='agent-step/1'
    value['case_id']='strict';config.write_text(json.dumps(value))
    base={k:([] if 'list' in v else 'x') for k,v in BASE_OUTPUT_SCHEMA.items()}
    base.update(status='success',summary='Scripted edge, not quality evidence',confidence='high',next_action='none',failure_kind=None,
        review_comments=[{'file':'feature.py','line':2,'severity':'major','comment':'Division by zero for x=0.','evidence':'return 1 / x'}],verdict='confirmed')
    def native(execution,**kwargs):
        calls.append((execution,kwargs));envelope=json.loads(kwargs['input'])
        events=[{'type':'item.completed','item':{'type':'agent_message','text':json.dumps(base)}},
                {'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}]
        stdout='\n'.join(map(json.dumps,events))
        return ProcessOutcome(stdout,receipt_line(envelope,{'stdout':hashlib.sha256(stdout.encode()),'stderr':hashlib.sha256(b'')},0,None),0)
    monkeypatch.setattr(ContainerExecution,'run',native)
    monkeypatch.setattr(ContainerExecution,'supported_input_protocol',lambda self,image:8)
    monkeypatch.setattr(CodexTransport,'bridge_probe',lambda self,profile,request:({'schema':'bridge-readiness/1','tools':sorted(request.scope.allowed_tools)},profile.sandbox._prepared_context()))
    monkeypatch.setattr(fetch_mod,'_gh',_fake_gh(head));monkeypatch.setattr(common_mod,'gh',_fake_gh(head))
    monkeypatch.setattr(wt_mod.Path,'home',staticmethod(lambda:tmp_path))
    overrides=settings.model_dump();overrides.update(_env_file=None,repo_paths={'widget':str(work)},repo_full_names={'widget':'acme/widget'},mcp_repo_allowlist=['widget'],default_repo='widget',review_depth='standard',strict_backend_cli=str(shim))
    from infermatrix_copilot.mcp_server import CopilotMCP
    with sdk_runtime(overrides,shim=shim) as selected:
        core=CopilotMCP(Settings(**selected))
        monkeypatch.setattr(core._q,'put',lambda *_:None)
        runtime=object.__new__(ReviewRuntime);runtime._core=core
        try:
            for flag in ['review_deep_engine','review_second_round','review_verify_comments','ensemble_zero_yield_retry']:
                assert getattr(core.settings,flag) is True,flag
            request=StrictReviewRequest(RepositoryRef('widget','acme/widget'),7,head,'','strict-budget-control',review_depth='standard')
            reserved=runtime.reserve_review(request)
            code=core.copilot.execute_strict_reserved(reserved.run_id)
            result=runtime.get_result(reserved.run_id);ledger=rows(shim)
            print(json.dumps({'exit':code,'state':result.state,'calls':len(calls),'ledger':[(s,json.loads(raw)['native_complete'] if raw else None) for s,raw in ledger]}))
            assert len(calls)==call_limit and len(ledger)==call_limit
            assert all(status=='complete' for status,_ in ledger)
            assert result.terminal and result.state==expected_state
            facts=[]
            run_dir=Path(core.settings.run_root)/reserved.run_id
            for path in run_dir.glob('native-sessions/*/traces/records/*.jsonl'):
                intent=json.loads((path.parents[2]/'session-intent.json').read_text())
                for line in path.read_text().splitlines():
                    record=json.loads(line)
                    if record.get('kind')=='model_call':
                        facts.append({'step':intent['step'],**{key:record['result'][key] for key in ['native_call_sent','native_call_completed','native_outcome_pending']}})
            assert sum(row['native_call_sent'] for row in facts)==call_limit
            assert not any(row['native_outcome_pending'] for row in facts)
            refused=[row for row in facts if not row['native_call_sent']]
            assert len(refused)==(1 if expected_state=='blocked' else 0)
            assert all(not row['native_call_completed'] for row in refused)
            record_property('strict_budget_evidence',json.dumps({'call_limit':call_limit,'state':result.state,'scripted_container_invocations':len(calls),'native_model_dispatches':0,'facts':facts,'all_workflow_options_enabled':True,'original_reservation_preserved':True}))
            assert runtime.find_review(request.idempotency_key,expected_head_sha=head).run_id==reserved.run_id
            assert core.copilot.execute_strict_reserved(reserved.run_id)==0
            assert runtime.get_result(reserved.run_id).state==expected_state
            assert rows(shim)==ledger
            assert len(calls)==call_limit
        finally:core.close()
