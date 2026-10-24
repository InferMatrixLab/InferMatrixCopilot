"""Actual standard SDK worker, with only native/image boundaries scripted."""
from pathlib import Path
from contextlib import nullcontext
import hashlib,json,os,runpy,sqlite3,sys,time
from unittest.mock import patch
import pytest
P=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(P/'test'))
sys.path.insert(0,str(P/'eval/single-capability-shadow'))
from test_strict_budget_stop import settings,configured,arm
from test_arm_runtime import rows
from test_e2e_strict_mock import _pr_checkout,_review_reply,FINDING
from arm_runtime import sdk_runtime
from durable_worker import runtime_options,wait_result
from strict_host import forge_cli
from ledger import Refused
from infermatrix_copilot.sdk.v1 import StrictRuntime,StrictReviewRequest,RepositoryRef
import infermatrix_copilot
pytestmark=pytest.mark.skipif('site-packages' not in infermatrix_copilot.__file__,reason='Requires the installed candidate: durable_worker intentionally refuses source-path product imports.')


def install_scripted_native(config_path):
    from infermatrix_copilot.providers.codex import CodexTransport
    from infermatrix_copilot.providers.container_execution import ContainerExecution
    from infermatrix_copilot.providers.container_protocol import receipt_line
    from infermatrix_copilot.process_execution import ProcessOutcome
    from infermatrix_copilot.engine import worktrees
    root=Path(config_path).parent.parent
    os.environ['MEMORY_DB']=str(root/'worker-memory.sqlite')
    os.environ['METRICS_ENABLED']='false'
    from infermatrix_copilot.config import Settings
    effective=Settings(_env_file=None)
    policy={key:getattr(effective,key) for key in ['review_deep_engine','review_second_round','review_verify_comments','ensemble_zero_yield_retry']}
    with sqlite3.connect(root/'scripted-processes.sqlite') as db:
        db.execute('CREATE TABLE IF NOT EXISTS launches (pid INTEGER, policy TEXT)')
        db.execute('INSERT INTO launches VALUES (?,?)',(os.getpid(),json.dumps(policy)))
    output=_review_reply(review_comments=[dict(FINDING)],verdict='confirmed').text
    def native(execution,**kwargs):
        with sqlite3.connect(root/'scripted-processes.sqlite') as db:
            db.execute('CREATE TABLE IF NOT EXISTS calls (pid INTEGER)')
            db.execute('INSERT INTO calls VALUES (?)',(os.getpid(),))
            count=db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]
        fixture=json.loads(Path(config_path).read_text()).get('fixture_preclaim_gate')
        if fixture=='simultaneous' and count==1:
            wait_fixture(root/'release-native')
        request=json.loads(kwargs['input'])
        stdout='\n'.join(map(json.dumps,[{'type':'item.completed','item':{'type':'agent_message','text':output}},
            {'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}]))
        return ProcessOutcome(stdout,receipt_line(request,{'stdout':hashlib.sha256(stdout.encode()),'stderr':hashlib.sha256(b'')},0,None),0)
    CodexTransport.identity_probe=lambda self,profile:{'schema':'native-identity/1','sha256':'a'*64,'context':profile.sandbox._prepared_context()}
    CodexTransport.bridge_probe=lambda self,profile,request:({'schema':'bridge-readiness/1','tools':sorted(request.scope.allowed_tools)},profile.sandbox._prepared_context())
    ContainerExecution.run=native
    ContainerExecution.supported_input_protocol=lambda self,image:8
    worktrees.Path.home=staticmethod(lambda:root)


def worker_fixture():
    # This test bootstrap installs boundary fixtures, then delegates verbatim to
    # the maintained policy wrapper and actual product -m worker entry point.
    config=Path(sys.argv[2]);install_scripted_native(config)
    root=config.parent.parent
    gated=json.loads(config.read_text()).get('fixture_preclaim_gate')
    import infermatrix_copilot.persistence as persistence
    original_write=persistence.immutable_write_bytes
    def observe_worker_entry(path,*args,**kwargs):
        if Path(path).name!='evaluation-worker.json':
            return original_write(path,*args,**kwargs)
        # Observe a startup without bypassing either wrapper or product code.
        try:
            return original_write(path,*args,**kwargs)
        finally:
            (root/f'worker-ready-{os.getpid()}').write_text('paused before product entry')
            release=root/f'release-worker-{os.getpid()}'
            wait_fixture(release)
            if release.read_text()=='crash':
                os._exit(91)  # fixture exits before the product run claim
    sys.argv=sys.argv[3:]
    code=None
    try:
        with patch.object(persistence,'immutable_write_bytes',observe_worker_entry) if gated else nullcontext():
            runpy.run_path(sys.argv[0],run_name='__main__')
    except SystemExit as exc:
        code=exc.code
        raise
    finally:
        if gated:
            (root/f'worker-exit-{os.getpid()}.json').write_text(json.dumps({'pid':os.getpid(),'exitcode':code}))


def wait_fixture(path):
    deadline=time.monotonic()+45
    while not path.exists():
        if time.monotonic()>deadline:
            raise AssertionError(f'Fixture barrier not released: {path.name}')
        time.sleep(.02)


@pytest.mark.parametrize('call_limit,expected_state',[(4,'blocked'),(5,'done')])
def test_worker_parent_reopen_keeps_budget_and_original_run(arm,tmp_path,monkeypatch,call_limit,expected_state,record_property):
    settings,unused_parent_calls,shim,_=arm
    work,head=_pr_checkout(tmp_path)
    config=shim.parent/'config.json';value=json.loads(config.read_text())
    value.update(case_id='strict')
    value['plan'].update(native_call_limits={'strict':call_limit},call_identity='agent-step/1',worker_execution='durable',workflow_timeout_s=call_limit*30+30)
    config.write_text(json.dumps(value))
    overrides=settings.model_dump();overrides.update(_env_file=None,repo_paths={'widget':str(work)},repo_full_names={'widget':'acme/widget'},mcp_repo_allowlist=['widget'],default_repo='widget',review_depth='standard',strict_backend_cli=str(shim))
    request=StrictReviewRequest(RepositoryRef('widget','acme/widget'),7,head,'','durable-budget-control',review_depth='standard')
    fixture=P/'test/test_e2e_strict_mock.py'
    descriptor={'kind':'fixture','fixture':str(fixture),'sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),'head':head}
    with forge_cli(tmp_path,descriptor),sdk_runtime(overrides,shim=shim,for_worker=True) as selected:
        options=runtime_options(shim)
        original=options['worker_prefix']
        options['worker_prefix']=(sys.executable,'-I','-B',str(Path(__file__).resolve()),'--worker-fixture',str(config),*original[3:])
        with StrictRuntime(settings_overrides=selected,**options) as runtime:
            first=runtime.start_review(request)
            root=Path(selected['run_root'])/first.run_id
            deadline=time.monotonic()+20
            while not (root/'evaluation-worker.json').exists():
                if time.monotonic()>deadline or runtime.get_result(first.run_id).terminal:
                    raise AssertionError((root/'console.log').read_text() if (root/'console.log').exists() else 'Worker did not write identity')
                time.sleep(.05)
            worker=json.loads((root/'evaluation-worker.json').read_text())
            assert worker['pid']!=os.getpid()
            # A bootstrap marker precedes the product's run claim. Observe a
            # real first dispatch before testing recovery of in-flight work.
            claim_deadline=time.monotonic()+45
            while True:
                count=0
                try:
                    with sqlite3.connect(tmp_path/'scripted-processes.sqlite') as db:
                        count=db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]
                except sqlite3.OperationalError:
                    pass
                if count:
                    break
                if time.monotonic()>claim_deadline or runtime.get_result(first.run_id).terminal:
                    raise AssertionError((root/'console.log').read_text())
                time.sleep(.05)
            os.kill(worker['pid'],0)
            with pytest.raises(Refused,match='original durable worker remains unresolved'):
                wait_result(runtime,first.run_id,0)
            launch=(root/'worker-launch.json').read_bytes()
        # Closing the SDK parent is not evidence that its worker stopped.
        os.kill(worker['pid'],0)
        with StrictRuntime(settings_overrides=selected,**options) as recovered:
            assert recovered.find_review(request.idempotency_key,expected_head_sha=head).run_id==first.run_id
            assert recovered.start_review(request).run_id==first.run_id
            result=wait_result(recovered,first.run_id,call_limit*30+30)
            assert result.state==expected_state
            assert (root/'worker-launch.json').read_bytes()==launch
            ledger=rows(shim)
            assert len(ledger)==call_limit and all(state=='complete' for state,_ in ledger)
            with sqlite3.connect(tmp_path/'scripted-processes.sqlite') as db:
                dispatched=db.execute('SELECT pid FROM calls').fetchall()
                launched=db.execute('SELECT pid,policy FROM launches').fetchall()
            assert len(launched)==1 and launched[0][0]==worker['pid']
            assert all(json.loads(launched[0][1]).values())
            assert len(dispatched)==call_limit and {row[0] for row in dispatched}=={worker['pid']}
            again=recovered.start_review(request)
            assert again.run_id==first.run_id and rows(shim)==ledger
            assert not unused_parent_calls
            record_property('durable_strict_budget_evidence',json.dumps({'call_limit':call_limit,'state':result.state,'worker_pid':worker['pid'],'parent_pid':os.getpid(),'parent_closed_and_reopened':True,'observation_timeout_did_not_restart':True,'same_run_and_launch':True,'scripted_native_processes':len(dispatched),'native_model_dispatches':0,'all_scripted_native_receipt_rows_complete':True,'one_original_worker_started':True,'worker_options':json.loads(launched[0][1])}))


@pytest.mark.parametrize('delivery',['simultaneous','late_after_terminal','first_dies_before_claim'])
def test_preclaim_competing_workers_execute_one_original_review(
    arm,tmp_path,delivery,record_property,
):
    settings,parent_calls,shim,_=arm
    work,head=_pr_checkout(tmp_path)
    config=shim.parent/'config.json';value=json.loads(config.read_text())
    value.update(case_id='strict',fixture_preclaim_gate=delivery)
    value['plan'].update(native_call_limits={'strict':5},call_identity='agent-step/1',worker_execution='durable',workflow_timeout_s=180)
    config.write_text(json.dumps(value))
    overrides=settings.model_dump();overrides.update(_env_file=None,repo_paths={'widget':str(work)},repo_full_names={'widget':'acme/widget'},mcp_repo_allowlist=['widget'],default_repo='widget',review_depth='standard',strict_backend_cli=str(shim))
    request=StrictReviewRequest(RepositoryRef('widget','acme/widget'),7,head,'','preclaim-control',review_depth='standard')
    fixture=P/'test/test_e2e_strict_mock.py'
    descriptor={'kind':'fixture','fixture':str(fixture),'sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),'head':head}
    def launched():
        with sqlite3.connect(tmp_path/'scripted-processes.sqlite') as db:
            return [pid for pid, in db.execute('SELECT pid FROM launches')]
    with forge_cli(tmp_path,descriptor),sdk_runtime(overrides,shim=shim,for_worker=True) as selected:
        original=runtime_options(shim)['worker_prefix']
        options={'worker_prefix':(sys.executable,'-I','-B',str(Path(__file__).resolve()),'--worker-fixture',str(config),*original[3:])}
        with StrictRuntime(settings_overrides=selected,**options) as first_parent:
            first=first_parent.start_review(request)
            root=Path(selected['run_root'])/first.run_id
            wait_fixture(root/'evaluation-worker.json')
            first_pid=json.loads((root/'evaluation-worker.json').read_text())['pid']
            wait_fixture(tmp_path/f'worker-ready-{first_pid}')
            os.kill(first_pid,0)
            assert first_parent.get_result(first.run_id).state=='queued'
            launch=(root/'worker-launch.json').read_bytes()
            if delivery=='first_dies_before_claim':
                (tmp_path/f'release-worker-{first_pid}').write_text('crash')
                wait_result(first_parent,first.run_id,20)
                status=json.loads((root/'run_status.json').read_text())
                assert status['child_pid'] is None
                assert not (tmp_path/'calls.sqlite').exists()
        with StrictRuntime(settings_overrides=selected,**options) as recovered:
            try:
                assert recovered.find_review(request.idempotency_key,expected_head_sha=head).run_id==first.run_id
                assert recovered.start_review(request).run_id==first.run_id
                deadline=time.monotonic()+20
                while len(launched())<2:
                    if time.monotonic()>deadline:raise AssertionError('Queued adoption did not start a second fixture worker')
                    time.sleep(.05)
                pids=launched();assert len(pids)==2 and first_pid in pids
                second_pid=next(pid for pid in pids if pid!=first_pid)
                wait_fixture(tmp_path/f'worker-ready-{second_pid}')
                assert not list(tmp_path.glob('worker-exit-*.json'))
                if delivery=='first_dies_before_claim':
                    (tmp_path/f'release-worker-{second_pid}').write_text('go')
                    winner_pid=second_pid
                else:
                    (tmp_path/f'release-worker-{first_pid}').write_text('go')
                if delivery=='simultaneous':
                    (tmp_path/f'release-worker-{second_pid}').write_text('go')
                    deadline=time.monotonic()+30
                    while not list(tmp_path.glob('worker-exit-*.json')):
                        if time.monotonic()>deadline:raise AssertionError('Lock loser did not exit while winner was paused')
                        time.sleep(.05)
                    loser_exit=json.loads(next(tmp_path.glob('worker-exit-*.json')).read_text())
                    assert loser_exit['exitcode']==3
                    winner_pid=next(pid for pid in pids if pid!=loser_exit['pid'])
                    assert not recovered.get_result(first.run_id).terminal
                    assert json.loads((root/'run_status.json').read_text())['child_pid']==winner_pid
                    (tmp_path/'release-native').write_text('go')
                result=wait_result(recovered,first.run_id,180)
                assert result.state=='done'
                if delivery=='late_after_terminal':
                    winner_pid=first_pid
                    assert not (tmp_path/f'worker-exit-{second_pid}.json').exists()
                    (tmp_path/f'release-worker-{second_pid}').write_text('go')
                waited=[pid for pid in pids if not (delivery=='first_dies_before_claim' and pid==first_pid)]
                for pid in waited:wait_fixture(tmp_path/f'worker-exit-{pid}.json')
                exits={pid:json.loads((tmp_path/f'worker-exit-{pid}.json').read_text())['exitcode'] for pid in waited}
                if delivery=='first_dies_before_claim':exits[first_pid]='fixture exit before product claim'
                assert exits[winner_pid]==0
                if delivery=='late_after_terminal':assert exits[second_pid]==0
                assert recovered.get_result(first.run_id).state=='done'
                assert json.loads((root/'run_status.json').read_text())['child_pid']==winner_pid
                with sqlite3.connect(tmp_path/'scripted-processes.sqlite') as db:
                    calls=db.execute('SELECT pid FROM calls').fetchall()
                ledger=rows(shim)
                assert len(calls)==len(ledger)==5 and {pid for pid, in calls}=={winner_pid}
                assert all(state=='complete' for state,_ in ledger)
                assert not parent_calls and launched()==pids
                assert (root/'worker-launch.json').read_bytes()==launch
                entries=[json.loads(path.read_text()) for path in (root/'evaluation-workers').glob('*.json')]
                assert len(entries)==2 and {entry['pid'] for entry in entries}==set(pids)
                assert len({entry['startup_id'] for entry in entries})==2
                assert all(entry['config_sha256']==hashlib.sha256(config.read_bytes()).hexdigest() for entry in entries)
                assert json.loads((root/'evaluation-worker.json').read_text())['pid']==first_pid
                record_property('preclaim_execution_evidence',json.dumps({'delivery':delivery,'startup_pids':pids,'winner_pid':winner_pid,'worker_exits':exits,'same_run_and_launch':True,'final_state':result.state,'scripted_process_calls':len(calls),'complete_native_receipts':len(ledger),'evaluation_identity_guard_bypassed':False,'product_lock_and_claim_unchanged':True,'all_startup_proofs_retained':True,'legacy_first_proof_preserved':True,'native_model_dispatches':0}))
            finally:
                (tmp_path/'release-native').write_text('cleanup')
                for pid in launched():(tmp_path/f'release-worker-{pid}').write_text('cleanup')


if __name__=='__main__' and sys.argv[1:2]==['--worker-fixture']:
    worker_fixture()
