import copy
import json
from pathlib import Path
import random
import sqlite3
import sys

import pytest

import arm_entry
import arm_runtime
import durable_worker
import ledger
import shared_execution
import strict_host
from ledger import Refused
from shadow_test_support import shared_plan, bootstrap_guard
from run_paired import hashed, run, reconcile_failed_stage, terminal_failure


def fixture(tmp_path, *, fail=False):
    binary = tmp_path/'fake-codex'
    binary.write_text(f'#!{sys.executable}\n' + 'print(\'{"type":"turn.completed"}\')\n')
    binary.chmod(0o700)
    driver=tmp_path/'driver.py'
    driver.write_text('import subprocess,sys\nfrom pathlib import Path\n'
        'Path("dispatch-marker").write_text("once")\n'
        'p=subprocess.run([sys.argv[1],"exec","--json","--sandbox","read-only","-"],input="frozen",text=True)\n'
        f'raise SystemExit({7 if fail else "p.returncode"})\n')
    guard=bootstrap_guard(tmp_path / 'launcher').resolve()
    paths=[str(binary),str(driver),sys.executable,str(guard),str(guard.with_name('ledger.py'))]
    arms=['baseline','candidate']; random.Random(7).shuffle(arms)
    return {'order_seed':7,'publication':'disabled','binary':str(binary),'guard_python':sys.executable,
        'guard_script':str(guard),'bindings':{p:hashed(p) for p in paths},
        'plan':{'shared_execution':shared_plan(tmp_path, binary),'execution_kind':'live','supply_profile_defaults':True,'native_call_limits':{'case':1},
                'native_timeout_s':10,'provider_binary_sha256':hashed(binary),
                'execution_profile':{'model':'fake','reasoning_effort':'high',
                                     'config_overrides':['model_reasoning_effort="high"']}},
        'stages':[{'case_id':'case','arm':arm,'command':[sys.executable,'-I',str(driver),'{shim}'],
                   'timeout_s':20} for arm in arms]}


def test_completed_pair_replays_without_relaunch(tmp_path):
    manifest=fixture(tmp_path); output=tmp_path/'run'
    assert run(manifest,output)['stages']==2
    markers=list(output.glob('*/dispatch-marker'))
    before=[p.stat().st_mtime_ns for p in markers]
    assert len(before)==2
    assert run(manifest,output)['adjudication']=='pending'
    assert [p.stat().st_mtime_ns for p in markers]==before
    with sqlite3.connect(output/'calls.sqlite') as db:
        assert db.execute('SELECT count(*) FROM calls').fetchone()==(2,)


def test_retained_allowance_overflow_refuses_before_stage_or_ledger_creation(tmp_path):
    manifest = fixture(tmp_path)
    manifest['plan']['retained_prior_call_allowances'] = 36
    output = tmp_path / 'must-not-start'
    with pytest.raises(Refused, match='retained prior allowances plus paired'):
        run(manifest, output)
    assert not output.exists()
    assert not list(tmp_path.rglob('dispatch-marker'))


def test_composed_entry_and_its_helpers_must_be_frozen_before_dispatch(tmp_path):
    from run_paired import validate
    manifest = fixture(tmp_path)
    entry = Path(arm_entry.__file__).resolve()
    manifest['arm_entry_script'] = str(entry)
    with pytest.raises(Refused, match='not hash-bound'):
        validate(manifest)
    for module in (arm_entry, arm_runtime, shared_execution, ledger):
        path = str(Path(module.__file__).resolve())
        manifest['bindings'][path] = hashed(path)
    validate(manifest)


def test_durable_worker_composition_and_account_helpers_must_be_frozen(tmp_path):
    from run_paired import validate
    manifest = fixture(tmp_path)
    manifest['plan']['worker_execution'] = 'durable'
    manifest['plan']['call_identity'] = 'agent-step/1'
    with pytest.raises(Refused, match='not hash-bound'):
        validate(manifest)
    for module in (durable_worker, strict_host, arm_runtime, shared_execution, ledger):
        path = str(Path(module.__file__).resolve())
        manifest['bindings'][path] = hashed(path)
    validate(manifest)


def test_failed_stage_does_not_restart_or_advance(tmp_path):
    manifest=fixture(tmp_path,fail=True); output=tmp_path/'run'
    with pytest.raises(Refused,match='stage failed'):
        run(manifest,output)
    with pytest.raises(Refused,match='reconciliation'):
        run(manifest,output)
    assert len(list(output.glob('*/dispatch-marker')))==1
    with sqlite3.connect(output/'calls.sqlite') as db:
        assert db.execute('SELECT count(*) FROM calls').fetchone()==(1,)


@pytest.mark.parametrize('change',['source','budget','order','missing_arm','environment','publication'])
def test_preflight_rejects_drift_without_dispatch(tmp_path,change):
    manifest=fixture(tmp_path)
    if change=='source':
        Path(manifest['stages'][0]['command'][2]).write_text('changed')
    elif change=='budget':
        manifest['plan']['native_call_limits']['case']=19
    elif change=='order':
        manifest['stages'].reverse()
    elif change=='missing_arm':
        manifest['stages'].pop()
    elif change=='environment':
        manifest['stages'][0]['environment']={'PYTHONPATH':'injected'}
    else:
        manifest['publication']='enabled'
    with pytest.raises(Refused):
        run(manifest,tmp_path/'run')
    assert not (tmp_path/'run').exists()


def test_revised_plan_uses_same_ceiling_in_coordinator_and_native_ledger(tmp_path):
    manifest = fixture(tmp_path)
    manifest['plan'].update(native_call_ceiling=116, budget_revision='full-workflow-20261012')
    manifest['plan']['native_call_limits']['case'] = 58
    output = tmp_path / 'run'
    assert run(manifest, output)['status'] == 'executed'
    with sqlite3.connect(output / 'calls.sqlite') as db:
        assert db.execute('SELECT call_limit FROM plan').fetchone() == (116,)
    manifest['plan']['native_call_limits']['case'] = 59
    with pytest.raises(Refused, match='116-native-call'):
        run(manifest, tmp_path / 'overflow')


def test_new_generation_revision_shares_coordinator_and_native_accounting(tmp_path):
    manifest = fixture(tmp_path)
    manifest['plan'].update(native_call_ceiling=206, budget_revision='native-generation-20261016')
    manifest['plan']['native_call_limits']['case'] = 103
    output = tmp_path / 'run'
    assert run(manifest, output)['status'] == 'executed'
    with sqlite3.connect(output / 'calls.sqlite') as db:
        assert db.execute('SELECT call_limit FROM plan').fetchone() == (206,)
        assert db.execute('SELECT count(*) FROM calls').fetchone() == (2,)
    manifest['plan']['native_call_limits']['case'] = 104
    with pytest.raises(Refused, match='206-native-call'):
        run(manifest, tmp_path / 'overflow')
    assert not (tmp_path / 'overflow').exists()


def test_explicit_terminal_failure_reconciliation_preserves_failed_samples_without_redispatch(tmp_path):
    manifest = fixture(tmp_path)
    binary = Path(manifest['binary'])
    binary.write_text(f'#!{sys.executable}\n' + 'import sys\nprint(\'{"type":"turn.failed","error":{"message":"backend limit"}}\')\nsys.exit(1)\n')
    manifest['bindings'][str(binary)] = hashed(binary)
    manifest['plan']['provider_binary_sha256'] = hashed(binary)
    output = tmp_path / 'run'
    for position in range(2):
        with pytest.raises(Refused, match='stage failed'):
            run(manifest, output)
        reconcile_failed_stage(manifest, output, position)
    markers = {str(path):path.stat().st_mtime_ns for path in output.glob('*/dispatch-marker')}
    result = run(manifest, output)
    assert result['status'] == 'executed_with_failures' and result['failed_stages'] == 2
    assert markers == {str(path):path.stat().st_mtime_ns for path in output.glob('*/dispatch-marker')}
    with sqlite3.connect(output / 'calls.sqlite') as db:
        assert db.execute('SELECT count(*) FROM calls').fetchone() == (2,)
        assert all(not json.loads(row[0])['native_complete'] for row in db.execute('SELECT result FROM calls'))
    evidence = output / 'reconciled-failure-0.json'
    data = json.loads(evidence.read_text()); data['receipts'] = {}
    evidence.write_text(json.dumps(data))
    with pytest.raises(Refused, match='receipt set changed'):
        run(manifest, output)


@pytest.mark.parametrize('change', ['timeout','signal','missing_event','completed_event','zero_exit'])
def test_failed_process_alone_cannot_reconcile_unknown_provider_outcome(change):
    receipt = {'native_complete':False,'returncode':1,'reason':None,
               'stdout':json.dumps({'type':'turn.failed','error':{'message':'known failure'}})}
    assert terminal_failure(receipt)
    if change == 'timeout': receipt['reason'] = 'absolute_timeout'
    elif change == 'signal': receipt['returncode'] = -9
    elif change == 'missing_event': receipt['stdout'] = ''
    elif change == 'completed_event': receipt['stdout'] = '{"type":"turn.completed"}\n' + receipt['stdout']
    else: receipt['returncode'] = 0
    assert not terminal_failure(receipt)


def test_manifest_and_completed_output_are_immutable(tmp_path):
    manifest=fixture(tmp_path); output=tmp_path/'run'
    run(manifest,output)
    changed=copy.deepcopy(manifest); changed['note']='changed'
    with pytest.raises(Refused,match='plan changed'):
        run(changed,output)
    (next(output.glob('*/stdout.txt'))).write_text('tampered')
    with pytest.raises(Refused,match='output changed'):
        run(manifest,output)


def test_native_receipt_tampering_blocks_completed_stage_replay(tmp_path):
    manifest=fixture(tmp_path); output=tmp_path/'run'
    run(manifest,output)
    with sqlite3.connect(output/'calls.sqlite') as db:
        db.execute("UPDATE calls SET result='{}'")
    with pytest.raises(Refused,match='native receipt changed'):
        run(manifest,output)


def test_interrupted_intent_is_never_automatically_replayed(tmp_path):
    from run_paired import digest
    manifest=fixture(tmp_path); output=tmp_path/'run'; output.mkdir()
    with sqlite3.connect(output/'stages.sqlite') as db:
        db.executescript('''CREATE TABLE plan (id INTEGER PRIMARY KEY, digest TEXT);
            CREATE TABLE stages (position INTEGER PRIMARY KEY,case_id TEXT,arm TEXT,
            status TEXT,returncode INTEGER,stdout_sha256 TEXT,stderr_sha256 TEXT);''')
        db.execute('INSERT INTO plan VALUES(1,?)',(digest(manifest),))
        db.execute("INSERT INTO stages VALUES(0,'case',?,'running',NULL,NULL,NULL)",
                   (manifest['stages'][0]['arm'],))
    with pytest.raises(Refused,match='reconciliation'):
        run(manifest,output)
    assert not list(output.glob('*/dispatch-marker'))


@pytest.mark.parametrize('statement', ['from init_native_arm import main', 'import init_native_arm'])
def test_shared_init_implementation_is_bound_before_stage_dispatch(tmp_path, statement):
    from run_paired import validate
    manifest = fixture(tmp_path)
    driver = Path(manifest['stages'][0]['command'][2])
    driver.write_text(statement + '\n')
    manifest['bindings'][str(driver)] = hashed(driver)
    helper = driver.with_name('init_native_arm.py')
    helper.write_text('def main(stage): pass\n')
    with pytest.raises(Refused, match='not hash-bound'):
        run(manifest, tmp_path / 'run')
    assert not (tmp_path / 'run').exists()
    manifest['bindings'][str(helper)] = hashed(helper)
    validate(manifest)
    helper.write_text('def main(stage): raise RuntimeError("changed")\n')
    with pytest.raises(Refused, match='frozen file changed'):
        run(manifest, tmp_path / 'run')
    assert not (tmp_path / 'run').exists()


def test_legacy_driver_does_not_inherit_a_new_shared_implementation_requirement(tmp_path):
    from run_paired import validate
    manifest = fixture(tmp_path)
    driver = Path(manifest['stages'][0]['command'][2])
    driver.write_text('"""from init_native_arm import main is only a comment here."""\n')
    manifest['bindings'][str(driver)] = hashed(driver)
    validate(manifest)


def test_role_selection_and_shared_implementations_are_bound_before_dispatch(tmp_path):
    from run_paired import validate
    manifest = fixture(tmp_path)
    manifest['plan']['execution_profile']['roles'] = {
        'generator': {'provider': 'codex', 'model': 'extract-model', 'effort': 'high'},
        'judge': {'provider': 'codex', 'model': 'judge-model', 'effort': 'high'}}
    guard = Path(manifest['guard_script'])
    paths = [guard.with_name(name) for name in ('role_profiles.py', 'arm_runtime.py', 'shared_execution.py')]
    for path in paths:
        path.write_text('# frozen evaluation implementation\n')
    with pytest.raises(Refused, match='not hash-bound'):
        run(manifest, tmp_path / 'run')
    assert not (tmp_path / 'run').exists()
    manifest['bindings'].update({str(path): hashed(path) for path in paths})
    validate(manifest)
    paths[0].write_text('# changed role selection\n')
    with pytest.raises(Refused, match='frozen file changed'):
        run(manifest, tmp_path / 'run')
    assert not (tmp_path / 'run').exists()


def test_shared_init_fixture_policy_is_bound_before_stage_dispatch(tmp_path):
    from run_paired import validate
    manifest = fixture(tmp_path)
    driver = Path(manifest['stages'][0]['command'][2])
    driver.write_text('from init_native_arm import main\n')
    helper = driver.with_name('init_native_arm.py')
    helper.write_text('from init_fixture_policies import scripted_gateway\n')
    policy = driver.with_name('init_fixture_policies.py')
    policy.write_text('def scripted_gateway(): pass\n')
    manifest['bindings'].update({str(driver): hashed(driver), str(helper): hashed(helper)})
    with pytest.raises(Refused, match='not hash-bound'):
        run(manifest, tmp_path / 'run')
    assert not (tmp_path / 'run').exists()
    manifest['bindings'][str(policy)] = hashed(policy)
    validate(manifest)
    policy.write_text('def scripted_gateway(): raise RuntimeError("changed")\n')
    with pytest.raises(Refused, match='frozen file changed'):
        run(manifest, tmp_path / 'run')
    assert not (tmp_path / 'run').exists()


@pytest.mark.parametrize('damage', ['missing', 'syntax', 'argument_type'])
def test_invalid_driver_refuses_before_any_stage_state_is_created(tmp_path, damage):
    manifest = fixture(tmp_path)
    driver = Path(manifest['stages'][0]['command'][2])
    if damage == 'missing':
        driver.unlink()
    elif damage == 'syntax':
        driver.write_text('def broken(')
        manifest['bindings'][str(driver)] = hashed(driver)
    else:
        manifest['stages'][0]['command'][2] = 42
    with pytest.raises(Refused):
        run(manifest, tmp_path / 'run')
    assert not (tmp_path / 'run').exists()


def test_mixed_provider_manifest_binds_each_executable_without_a_legacy_single_hash(tmp_path):
    from run_paired import validate
    manifest=fixture(tmp_path)
    plan=manifest['plan'];old=plan['shared_execution']
    profile=old['profile']
    plan['shared_execution']={**{k:v for k,v in old.items() if k not in ('profile','schema')},
        'schema':'shared-sandbox/2','profiles':{'codex':profile,'zcode':profile}}
    plan['execution_profile']['roles']={
        'generator':{'provider':'zcode','model':'GLM-5.3','effort':''},
        'judge':{'provider':'codex','model':'offline-model','effort':'high'}}
    plan['provider_binary_sha256_by_provider']={'codex':plan.pop('provider_binary_sha256'),'zcode':'a'*64}
    # Profile option policy is covered separately; this case checks only immutable
    # manifest executable binding and keeps shared profile preflight out of scope.
    import unittest.mock
    with unittest.mock.patch('shared_execution.shared_profiles', return_value={'codex':profile,'zcode':profile}):
        with pytest.raises(Refused,match='provider_binaries'):
            validate(manifest,for_dispatch=False)
        other=tmp_path/'zcode';other.write_text('frozen second provider')
        plan['provider_binary_sha256_by_provider']['zcode']=hashed(other)
        manifest['provider_binaries']={'codex':manifest['binary'],'zcode':str(other)}
        for name in ('role_profiles.py','arm_runtime.py','shared_execution.py','ledger.py'):
            path=Path(manifest['guard_script']).with_name(name)
            if not path.exists():
                path.write_bytes(Path(__file__).with_name(name).read_bytes())
            manifest['bindings'][str(path)]=hashed(path)
        with pytest.raises(Refused,match='not hash-bound'):
            validate(manifest,for_dispatch=False)
        manifest['bindings'][str(other)]=hashed(other)
        validate(manifest,for_dispatch=False)
        plan['provider_binary_sha256_by_provider']['zcode']='a'*64
        with pytest.raises(Refused,match='provider hash differs'):
            validate(manifest,for_dispatch=False)
        manifest['provider_binaries']['zcode']=manifest['binary']
        with pytest.raises(Refused,match='provider hash differs'):
            validate(manifest,for_dispatch=False)


def test_workflow_failure_after_successful_native_call_is_preserved_without_retry(tmp_path):
    manifest = fixture(tmp_path, fail=True); output = tmp_path / 'run'
    for position in range(2):
        with pytest.raises(Refused, match='stage failed'):
            run(manifest, output)
        reconcile_failed_stage(manifest, output, position)
    markers = {str(p): p.stat().st_mtime_ns for p in output.glob('*/dispatch-marker')}
    result = run(manifest, output)
    assert result['status'] == 'executed_with_failures' and result['failed_stages'] == 2
    assert markers == {str(p): p.stat().st_mtime_ns for p in output.glob('*/dispatch-marker')}
    with sqlite3.connect(output / 'calls.sqlite') as db:
        rows = db.execute('SELECT result FROM calls').fetchall()
        assert len(rows) == 2 and all(json.loads(raw)['native_complete'] for (raw,) in rows)
    evidence = json.loads((output / 'reconciled-failure-0.json').read_text())
    assert evidence['outcome'] == 'workflow_failure'
    process = output / f'00-{manifest["stages"][0]["arm"]}' / 'stage-process.json'
    process.write_text('{"returncode":0,"reason":null}')
    with pytest.raises(Refused, match='terminal process'):
        run(manifest, output)


@pytest.mark.parametrize('change', ['signal', 'timeout', 'exit_mismatch', 'missing_process', 'unknown_call'])
def test_workflow_reconciliation_refuses_uncertain_outcomes(tmp_path, change):
    manifest = fixture(tmp_path, fail=True); output = tmp_path / 'run'
    with pytest.raises(Refused, match='stage failed'):
        run(manifest, output)
    process = output / f'00-{manifest["stages"][0]["arm"]}' / 'stage-process.json'
    if change == 'missing_process': process.unlink()
    elif change == 'unknown_call':
        with sqlite3.connect(output / 'calls.sqlite') as db:
            db.execute("UPDATE calls SET status='reserved',result=NULL,result_digest=NULL")
    else:
        process.write_text(json.dumps({'returncode': -9 if change == 'signal' else 8 if change == 'exit_mismatch' else 7,
                                      'reason': 'absolute_timeout' if change == 'timeout' else None}))
    with pytest.raises((Refused, FileNotFoundError)):
        reconcile_failed_stage(manifest, output, 0)
    with sqlite3.connect(output / 'stages.sqlite') as db:
        assert db.execute('SELECT status FROM stages').fetchone() == ('needs_reconciliation',)


def test_recovery_coordinator_binds_original_loaded_helpers_not_its_new_location(tmp_path):
    import importlib.util
    import run_paired
    from test_quality_plan import frozen
    manifest, _, _, _ = frozen(tmp_path)
    recovery = tmp_path / 'recovery'; recovery.mkdir()
    controller = recovery / 'run_paired.py'
    controller.write_bytes(Path(run_paired.__file__).read_bytes())
    spec = importlib.util.spec_from_file_location('recovery_coordinator_test', controller)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.validate(manifest)
    manifest['plan'].update(worker_execution='durable', call_identity='agent-step/1')
    for helper in (durable_worker, strict_host, arm_runtime, shared_execution, ledger):
        path = str(Path(helper.__file__).resolve()); manifest['bindings'][path] = hashed(path)
    module.validate(manifest)
    manifest['bindings'].pop(str(Path(durable_worker.__file__).resolve()))
    with pytest.raises(Refused, match='not hash-bound'):
        module.validate(manifest)


def selected_fixture(tmp_path):
    import stage_selection
    worker=tmp_path/'worker';worker.mkdir();manifest=fixture(worker)
    chosen=next(i for i,s in enumerate(manifest['stages']) if s['arm']=='candidate')
    omitted=1-chosen
    proof=tmp_path/'original-pending.json';proof.write_text('{"run_id":"original","state":"pending"}')
    helper=Path(stage_selection.__file__).resolve()
    manifest['bindings'].update({str(proof):hashed(proof),str(helper):hashed(helper)})
    manifest['stage_selection']={'schema':'selected-stages/1','positions':[chosen],
        'retained':{str(omitted):{'status':'pending','reason':'Keep original unknown paid result; never replay',
                                 'evidence':{str(proof):hashed(proof)}}}}
    return manifest,worker/'run',chosen,omitted,proof


def test_selected_first_time_stage_retains_other_arm_without_dispatch_or_quality_pass(tmp_path):
    manifest,output,chosen,omitted,proof=selected_fixture(tmp_path)
    original=proof.read_bytes();result=run(manifest,output)
    assert result['status']=='executed_partial' and result['stages']==1
    assert result['planned_stages']==2 and result['selected_stage_positions']==[chosen]
    assert result['retained_stages'][str(omitted)]['status']=='pending'
    assert result['full_quality_acceptance'] is False
    assert not (output/f'{omitted:02d}-baseline').exists()
    marker=output/f'{chosen:02d}-candidate/dispatch-marker';before=marker.stat().st_mtime_ns
    run(manifest,output)
    assert marker.stat().st_mtime_ns==before and proof.read_bytes()==original
    with sqlite3.connect(output/'stages.sqlite') as db:
        assert db.execute('SELECT position,status FROM stages').fetchall()==[(chosen,'complete')]


@pytest.mark.parametrize('damage',['empty','duplicate','outside','full','missing-original','unbound-original','invalid-status','helper'])
def test_invalid_stage_selection_refuses_before_any_intent(tmp_path,damage):
    import stage_selection
    manifest,output,chosen,omitted,proof=selected_fixture(tmp_path);selection=manifest['stage_selection']
    if damage=='empty':selection['positions']=[]
    elif damage=='duplicate':selection['positions']=[chosen,chosen]
    elif damage=='outside':selection['positions']=[2]
    elif damage=='full':selection['positions']=[0,1]
    elif damage=='missing-original':selection['retained']={}
    elif damage=='unbound-original':manifest['bindings'].pop(str(proof))
    elif damage=='invalid-status':selection['retained'][str(omitted)]['status']='complete'
    else:manifest['bindings'].pop(str(Path(stage_selection.__file__).resolve()))
    with pytest.raises(Refused):run(manifest,output)
    assert not output.exists()


def test_stage_selection_cannot_be_changed_to_replay_retained_arm(tmp_path):
    manifest,output,chosen,omitted,proof=selected_fixture(tmp_path)
    run(manifest,output)
    manifest['stage_selection'].update(positions=[omitted],retained={str(chosen):{
        'status':'retained','reason':'Already completed','evidence':{str(proof):hashed(proof)}}})
    with pytest.raises(Refused,match='plan changed'):run(manifest,output)
    assert not (output/f'{omitted:02d}-baseline').exists()


@pytest.mark.parametrize('receipt', ['missing', 'unresolved', 'corrupt'])
def test_exited_stage_with_unverified_receipt_stays_pending_without_redispatch(tmp_path, receipt):
    manifest = fixture(tmp_path)
    driver = Path(manifest['stages'][0]['command'][2])
    if receipt == 'missing':
        driver.write_text('from pathlib import Path\nPath("dispatch-marker").write_text("once")\n')
    else:
        sql = ("UPDATE calls SET status='reserved',result=NULL,result_digest=NULL"
               if receipt == 'unresolved' else "UPDATE calls SET result='{}'")
        driver.write_text(driver.read_text().replace('raise SystemExit(p.returncode)', '') +
            'import os,json,sqlite3\n'
            'config=json.loads(Path(os.environ["SHADOW_ARM_CONFIG"]).read_text())\n'
            'with sqlite3.connect(config["ledger_path"]) as db:\n' +
            f' db.execute({sql!r})\n')
    manifest['bindings'][str(driver)] = hashed(driver)
    output = tmp_path / 'run'
    with pytest.raises(Refused, match='native (receipt|call)'):
        run(manifest, output)
    with sqlite3.connect(output / 'stages.sqlite') as db:
        assert db.execute('SELECT status,returncode FROM stages').fetchall() == [('needs_reconciliation', 0)]
    directory = output / f'00-{manifest["stages"][0]["arm"]}'
    assert json.loads((directory / 'stage-process.json').read_text()) == {'returncode': 0, 'reason': None}
    ledger_path = output / 'calls.sqlite'

    def original_calls():
        if not ledger_path.exists():
            return None
        with sqlite3.connect('file:' + str(ledger_path) + '?mode=ro', uri=True) as db:
            return db.execute('SELECT id,request_digest,status,result,result_digest FROM calls ORDER BY id').fetchall()

    calls = original_calls()
    marker_time = (directory / 'dispatch-marker').stat().st_mtime_ns
    with pytest.raises(Refused, match='reconciliation'):
        run(manifest, output)
    with pytest.raises(Refused, match='only an exited failed stage'):
        reconcile_failed_stage(manifest, output, 0)
    assert original_calls() == calls
    assert (directory / 'dispatch-marker').stat().st_mtime_ns == marker_time
    assert len(list(output.glob('*/dispatch-marker'))) == 1


def test_crash_in_receipt_verification_preserves_exited_stage_as_pending(tmp_path, monkeypatch):
    import run_paired
    manifest = fixture(tmp_path)
    output = tmp_path / 'run'

    def crash(*args):
        raise RuntimeError('receipt verification crashed')

    monkeypatch.setattr(run_paired, 'verify_native', crash)
    with pytest.raises(RuntimeError, match='receipt verification crashed'):
        run(manifest, output)
    with sqlite3.connect(output / 'stages.sqlite') as db:
        assert db.execute('SELECT status,returncode FROM stages').fetchall() == [('needs_reconciliation', 0)]
    with pytest.raises(Refused, match='reconciliation'):
        run(manifest, output)
    assert len(list(output.glob('*/dispatch-marker'))) == 1


def captured_failure_fixture(tmp_path):
    manifest=fixture(tmp_path)
    binary=Path(manifest['binary'])
    binary.write_text(f'#!{sys.executable}\n' + 'import sys\nprint(\'{"type":"turn.failed","error":{"message":"known native limit"}}\')\nsys.exit(1)\n')
    manifest['bindings'][str(binary)]=hashed(binary)
    manifest['plan']['provider_binary_sha256']=hashed(binary)
    driver=Path(manifest['stages'][0]['command'][2])
    driver.write_text(driver.read_text().replace('raise SystemExit(p.returncode)',''))
    manifest['bindings'][str(driver)]=hashed(driver)
    return manifest


def test_captured_terminal_native_failure_reconciles_without_redispatch(tmp_path):
    manifest=captured_failure_fixture(tmp_path)
    output=tmp_path/'run'
    for position in range(2):
        with pytest.raises(Refused,match='unresolved native call'):
            run(manifest,output)
        reconcile_failed_stage(manifest,output,position)
    markers={str(p):p.stat().st_mtime_ns for p in output.glob('*/dispatch-marker')}
    result=run(manifest,output)
    assert result['status']=='executed_with_failures' and result['failed_stages']==2
    assert markers=={str(p):p.stat().st_mtime_ns for p in output.glob('*/dispatch-marker')}
    evidence=output/'reconciled-failure-0.json'
    saved=json.loads(evidence.read_text())
    assert saved['outcome']=='terminal_failure' and saved['captured_stage_proof']
    saved.pop('captured_stage_proof');evidence.write_text(json.dumps(saved))
    with pytest.raises(Refused,match='captured stage'):
        run(manifest,output)


@pytest.mark.parametrize('damage',['unknown_native','missing_process','missing_reason','timeout','signal','exit_mismatch'])
def test_captured_failure_refuses_uncertain_original_proof(tmp_path,damage):
    manifest=captured_failure_fixture(tmp_path)
    if damage=='unknown_native':
        binary=Path(manifest['binary'])
        binary.write_text(f'#!{sys.executable}\nimport sys\nsys.exit(1)\n')
        manifest['bindings'][str(binary)]=hashed(binary)
        manifest['plan']['provider_binary_sha256']=hashed(binary)
    output=tmp_path/'run'
    with pytest.raises(Refused,match='unresolved native call'):
        run(manifest,output)
    process=output/f'00-{manifest["stages"][0]["arm"]}'/'stage-process.json'
    if damage=='missing_process':process.unlink()
    elif damage!='unknown_native':
        value=json.loads(process.read_text())
        if damage=='missing_reason':value.pop('reason')
        elif damage=='timeout':value['reason']='absolute_timeout'
        else:value['returncode']=-9 if damage=='signal' else 7
        process.write_text(json.dumps(value))
    def calls():
        with sqlite3.connect(output/'calls.sqlite') as db:
            return db.execute('SELECT id,status,result,result_digest FROM calls').fetchall()
    before=calls()
    with pytest.raises(Refused):reconcile_failed_stage(manifest,output,0)
    assert calls()==before
    with sqlite3.connect(output/'stages.sqlite') as db:
        assert db.execute('SELECT status FROM stages').fetchone()==('needs_reconciliation',)
