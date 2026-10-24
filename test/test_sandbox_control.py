"""Native/control stream separation and cancellation against the original identity."""
from dataclasses import asdict
import json
from pathlib import Path
import threading
import time

import pytest

from infermatrix_copilot.providers.container_execution import ContainerExecution
from infermatrix_copilot.providers.process import ProcessOutcome, run_process
from infermatrix_copilot.sandbox import CancellationReceipt, SandboxPending
from infermatrix_copilot.sandbox.base import AGENT_PROTOCOL_VERSION
from test_container_execution import execution, run  # noqa: F401
from test_sandbox_execution import remote, invoke  # noqa: F401


@pytest.mark.parametrize('protocol', [1, 3, 4, 5, 6, 7, 8])
@pytest.mark.parametrize('stderr', ['', '\nINFERMATRIX_WORKER_RECEIPT_V1 not-json\n'])
def test_unproven_original_worker_receipt_has_explicit_pending_fact(protocol, stderr):
    original = ProcessOutcome('partial native output\n', stderr, -9, 'absolute')
    with pytest.raises(SandboxPending) as caught:
        ContainerExecution._decode_worker_result(
            {'worker_protocol': protocol, 'input_sha256': 'a' * 64}, original, required=True)
    assert caught.value.native_outcome_pending is True
    assert caught.value.allow_fallback is False


def test_legacy_live_process_exit_does_not_require_new_worker_receipts():
    original = ProcessOutcome('legacy output\n', '', 0)
    assert ContainerExecution._decode_worker_result({'worker_protocol': 1}, original) == original
    assert ContainerExecution._decode_worker_result(
        {'worker_protocol': 3, 'input_sha256': 'a' * 64}, original) == original


@pytest.mark.parametrize('stderr', ['', 'warning', 'warning\n', '\n', 'a\n\nb\n',
                                    '\nINFERMATRIX_WORKER_RECEIPT_V1 native text\nlast'])
def test_command_callbacks_contain_only_validated_native_lines(tmp_path, monkeypatch, stderr):
    import os
    import sys
    runtime=ContainerExecution(tmp_path/'execution',docker='/docker',socket='/daemon')
    def worker(**kwargs):
        return run_process([sys.executable,'-m','infermatrix_copilot.container_worker'],
            cwd=tmp_path,env=os.environ.copy(),deadline=kwargs['deadline'],input=kwargs['input'],
            on_line=kwargs['on_line'],cancel=kwargs['cancel'])
    monkeypatch.setattr(runtime,'run',worker)
    lines=[]
    cmd=[sys.executable,'-c','import sys; print("native"); sys.stderr.write('+repr(stderr)+')']
    outcome=runtime.run_command(cmd,image='sha256:'+'a'*64,cwd='/tmp/work',env={},
                               deadline=time.monotonic()+10,on_line=lambda *row:lines.append(row))
    expected=stderr.split('\n')
    if expected[-1]=='':expected.pop()
    assert outcome==ProcessOutcome('native\n',stderr,0)
    assert [line for channel,line in lines if channel=='stdout']==['native']
    assert [line for channel,line in lines if channel=='stderr']==expected


def test_malformed_control_record_is_retained_as_diagnostics(tmp_path, monkeypatch):
    runtime=ContainerExecution(tmp_path,docker='/docker',socket='/daemon')
    stderr='warning\n\nINFERMATRIX_WORKER_RECEIPT_V1 not-json\n'
    def worker(**kwargs):
        for line in stderr.split('\n')[:-1]:kwargs['on_line']('stderr',line)
        return ProcessOutcome('',stderr,0)
    monkeypatch.setattr(runtime,'run',worker)
    observed=[]
    with pytest.raises(SandboxPending):
        runtime.run_command(['/native'],image='sha256:'+'a'*64,cwd='/tmp/work',env={},
            deadline=time.monotonic()+10,on_line=lambda *row:observed.append(row))
    assert observed==[('stderr',line) for line in stderr.split('\n')[:-1]]


@pytest.mark.parametrize('state', ['running','created','exited','unknown'])
def test_cancellation_distinguishes_request_from_terminal_evidence(execution, monkeypatch, state):
    runtime,daemon=execution
    run(runtime)
    if state=='running':daemon.container['State']={'Running':True,'Status':'running'}
    elif state=='created':daemon.container['State']={'Running':False,'Status':'created','ExitCode':0}
    elif state=='unknown':monkeypatch.setattr(runtime,'_inspect',lambda *_:(_ for _ in ()).throw(SandboxPending('unavailable')))
    receipt=runtime.cancel()
    assert isinstance(receipt,CancellationReceipt) and receipt.requested
    assert receipt.terminated==(state in ('running','exited'))
    assert receipt.state==('terminal' if receipt.terminated else 'unknown' if state=='unknown' else 'pending')
    assert (runtime.root/'cancel-intent.json').exists()
    with pytest.raises(SandboxPending) as error:
        run(runtime)
    assert error.value.native_outcome_pending
    assert daemon.calls.count('start')==1


def test_cancellation_before_start_is_durable_and_cannot_replay(execution, monkeypatch):
    runtime,daemon=execution
    control=daemon.control
    def cancel_during_create(*args):
        value=control(*args)
        if args[0]=='create':
            (runtime.root/'cancel-intent.json').write_text('{}')
        return value
    monkeypatch.setattr(runtime,'_control',cancel_during_create)
    with pytest.raises(SandboxPending,match='cancelled before container start'):
        run(runtime)
    assert daemon.calls.count('create')==1 and 'start' not in daemon.calls
    with pytest.raises(SandboxPending) as error:
        run(runtime)
    assert error.value.native_outcome_pending and 'start' not in daemon.calls


def test_existing_cancelled_binding_precedes_invalid_replacement_arguments(execution):
    runtime,daemon=execution
    run(runtime); runtime.cancel()
    stopped=threading.Event();stopped.set()
    with pytest.raises(SandboxPending) as error:
        runtime.run(image='invalid',argv=[],deadline=0,cancel=stopped)
    assert error.value.native_outcome_pending and daemon.calls.count('start')==1


def test_remote_cancel_returns_the_same_receipt_and_retains_pending(remote):
    executor,calls,controls,_=remote
    invoke(executor)
    receipt=executor.cancel()
    assert receipt==CancellationReceipt(True,False,'pending')
    assert len([c for c in calls if c[0]=='execute'])==1


def test_legacy_remote_rows_observe_original_protocol_without_dispatch(remote, monkeypatch):
    executor,calls,controls,_=remote
    invoke(executor)
    path=executor.root/'remote-binding.json'
    binding=json.loads(path.read_text());binding.pop('agent_protocol')
    path.write_text(json.dumps(binding))
    (executor.root/'remote-result.json').unlink()
    original=ProcessOutcome('old native result','',0)
    versions=[]
    def legacy_server(command,*,input,on_line,**kwargs):
        request=json.loads(input);versions.append(request['version'])
        if request['operation']=='result':value=asdict(original)
        elif request['operation']=='cancel':value={'cancel_requested':True}
        else:pytest.fail('legacy observation cannot execute or preflight')
        on_line('stdout',json.dumps({'type':'result','value':value}))
        return ProcessOutcome('','',0)
    monkeypatch.setattr('infermatrix_copilot.sandbox.remote.run_process',legacy_server)
    assert executor.result(['/native'],cwd='/tmp/work',env={},input='frozen',recover=True)==original
    assert executor.cancel()==CancellationReceipt(True,False,'unknown')
    assert versions==[1,1] and len([c for c in calls if c[0]=='execute'])==1


def test_old_agent_capability_fails_before_reservation(remote, monkeypatch):
    executor,calls,controls,_=remote
    def old_agent(*args,**kwargs):
        kwargs['on_line']('stdout',json.dumps({'type':'result','value':{'release':'a'*64}}))
        return ProcessOutcome('','',0)
    monkeypatch.setattr('infermatrix_copilot.sandbox.remote.run_process',old_agent)
    with pytest.raises(ValueError,match='attest'):
        invoke(executor)
    assert not (executor.root/'remote-binding.json').exists() and not calls


@pytest.mark.parametrize('value', [{}, {'requested':True,'terminated':True,'state':'unknown'},
                                  {'requested':1,'terminated':False,'state':'pending'},
                                  {'requested':True,'terminated':False,'state':'terminal'}])
def test_invalid_cancel_receipts_cannot_establish_termination(value):
    with pytest.raises(ValueError):CancellationReceipt.from_dict(value)


def test_corrupt_cancellation_intent_stays_pending_and_cannot_replay(execution):
    runtime,daemon=execution
    run(runtime)
    (runtime.root/'cancel-intent.json').write_text('{"name":"wrong-original"}')
    with pytest.raises(SandboxPending) as error:runtime.cancel()
    assert error.value.native_outcome_pending
    with pytest.raises(SandboxPending):run(runtime)
    assert daemon.calls.count('start')==1


def test_remote_prior_authority_precedes_cancelled_or_invalid_replacement(remote):
    executor,calls,controls,_=remote
    invoke(executor)
    stopped=threading.Event();stopped.set()
    with pytest.raises(SandboxPending) as error:
        executor.execute([],cwd='',env={},deadline=0,cancel=stopped)
    assert error.value.native_outcome_pending and len([c for c in calls if c[0]=='execute'])==1
    assert controls==['preflight','execute']


@pytest.mark.parametrize('entry', ['command','profile','configured'])
def test_all_local_entries_retain_original_authority_before_validation(execution, entry):
    from infermatrix_copilot.providers.container_execution import ContainerCompletionProfile
    from infermatrix_copilot.providers.completion_profiles import DockerCompletionConfig,local_sandbox_profile
    runtime,daemon=execution
    run(runtime)
    with pytest.raises(SandboxPending) as error:
        if entry=='command':
            runtime.run_command([],image='invalid',cwd='',env={},deadline=0)
        elif entry=='profile':
            ContainerCompletionProfile(runtime,'invalid',credentials=object()).execute([],cwd='',env={},deadline=0)
        else:
            config=DockerCompletionConfig.parse({'kind':'docker','docker':'/missing','socket':'/missing',
                'image':'sha256:'+'a'*64,'codex_auth_file':'/missing'})
            local_sandbox_profile(config,runtime.root)
    assert error.value.native_outcome_pending and daemon.calls.count('start')==1
