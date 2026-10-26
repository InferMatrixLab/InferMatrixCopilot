"""Multi-phase quality execution never retries a partial paid stage implicitly."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

import quality_stage_arm
from ledger import Refused


def frozen(tmp_path, monkeypatch, *, fail=False):
    entry = tmp_path / "entry.py"
    entry.write_text("import runpy,sys\nrunpy.run_path(sys.argv[sys.argv.index('--')+1],run_name='__main__')\n")
    drivers = []
    for number in (1, 2):
        driver = tmp_path / f"driver-{number}.py"
        driver.write_text(f"from pathlib import Path\nPath('marker').write_text('{number}')\nprint('phase {number}')\n"
                          + ("raise SystemExit(7)\n" if fail and number == 2 else ""))
        drivers.append(driver)
    packet = {"schema": "quality-stage/1", "arm_entry": str(entry),
              "files": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in [entry, *drivers, Path(sys.executable)]},
              "phases": [{"name": f"phase-{number}", "python": sys.executable, "driver": str(driver),
                          "arguments": ["{shim}"], "timeout_s": 10} for number, driver in enumerate(drivers, 1)]}
    path = tmp_path / "phases.json"
    def freeze():
        path.write_text(json.dumps(packet));return hashlib.sha256(path.read_bytes()).hexdigest()
    digest = freeze()
    work = tmp_path / "work";work.mkdir();monkeypatch.chdir(work)
    config = tmp_path / "guard-config.json";config.write_text("{}")
    monkeypatch.setenv("SHADOW_ARM_CONFIG", str(config))
    def invoke(selected=None):
        monkeypatch.setattr(sys, "argv", ["phase", "--phases", str(path), "--sha256", selected or digest,
                                         "--shim", str(tmp_path / "codex")])
        quality_stage_arm.main()
    return packet, freeze, work, invoke


def test_phases_delegate_and_original_execution_cannot_be_relaunched(tmp_path, monkeypatch):
    _, _, work, invoke = frozen(tmp_path, monkeypatch)
    invoke()
    markers = {path: path.stat().st_mtime_ns for path in work.glob("*/marker")};assert len(markers) == 2
    assert all(json.loads(path.read_text()) == {"returncode": 0, "reason": None}
               for path in work.glob("*/phase-process.json"))
    assert all((path.parent / 'tmp').is_dir() for path in markers)
    with pytest.raises(Refused, match="reconcile"):invoke()
    assert markers == {path: path.stat().st_mtime_ns for path in markers}


def test_failed_later_phase_preserves_first_and_refuses_restart(tmp_path, monkeypatch):
    _, _, work, invoke = frozen(tmp_path, monkeypatch, fail=True)
    with pytest.raises(Refused, match="phase failed"):invoke()
    assert json.loads((work / "phase-2/phase-process.json").read_text())["returncode"] == 7
    before = (work / "phase-1/marker").stat().st_mtime_ns
    with pytest.raises(Refused, match="reconcile"):invoke()
    assert (work / "phase-1/marker").stat().st_mtime_ns == before


def test_legacy_temporary_outputs_and_traces_stay_in_phase(tmp_path, monkeypatch):
    packet, freeze, work, invoke = frozen(tmp_path, monkeypatch)
    driver = Path(packet['phases'][0]['driver'])
    driver.write_text("from pathlib import Path\nimport json,os,tempfile\n"
        "with tempfile.NamedTemporaryFile(delete=False) as stream: name=stream.name\n"
        "Path('isolation.json').write_text(json.dumps({'temporary':name,'trace':os.environ['TRACE_STORE_ROOT'],"
        "'pythonpath':os.environ.get('PYTHONPATH')}))\n")
    packet['files'][str(driver)] = hashlib.sha256(driver.read_bytes()).hexdigest()
    invoke(freeze())
    scope = json.loads((work / 'phase-1/isolation.json').read_text())
    assert Path(scope['temporary']).is_relative_to(work / 'phase-1/tmp')
    assert scope['trace'] == str(work / 'phase-1/runs/traces') and scope['pythonpath'] is None


@pytest.mark.parametrize("damage", ["manifest", "later-driver", "unbound-driver", "duplicate", "escape", "deadline"])
def test_all_phases_are_checked_before_starting_first(tmp_path, monkeypatch, damage):
    packet, freeze, work, invoke = frozen(tmp_path, monkeypatch)
    if damage == "manifest":selected = "0" * 64
    else:
        if damage == "later-driver":Path(packet["phases"][1]["driver"]).write_text("changed")
        elif damage == "unbound-driver":packet["files"].pop(packet["phases"][1]["driver"])
        elif damage == "duplicate":packet["phases"][1]["name"] = "phase-1"
        elif damage == "escape":packet["phases"][1]["name"] = "../outside"
        elif damage == "deadline":packet["phases"][1]["timeout_s"] = 0
        selected = freeze()
    with pytest.raises(Refused):invoke(selected)
    assert list(work.iterdir()) == []


def test_later_judgment_contract_refuses_before_any_initial_phase(tmp_path,monkeypatch):
    packet,freeze,work,invoke=frozen(tmp_path,monkeypatch)
    source=tmp_path/'foundation-input.json';source.write_text(json.dumps({'op':'edit','page':'repos/toy/rules.md','text':'source fact','evidence':[]}))
    (tmp_path/'manifest.json').write_text(json.dumps({'files':{source.name:{'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}}}))
    driver=tmp_path/'block_arm.py';driver.write_text('raise AssertionError("must not dispatch")')
    packet['files'][str(driver)]=hashlib.sha256(driver.read_bytes()).hexdigest()
    packet['phases'][1].update(driver=str(driver),arguments=['--inputs',str(tmp_path),'--case','foundation'])
    with pytest.raises(Refused,match='actual prose/prose contract'):invoke(freeze())
    assert not list(work.iterdir())


def test_strict_phase_must_cover_workflow_and_preparation_before_any_dispatch(tmp_path):
    from test_run_paired import fixture
    from run_paired import validate, hashed, run
    worker=tmp_path/'worker';worker.mkdir();manifest=fixture(worker)
    manifest['plan'].update(native_call_limits={'strict':13}, host_timeout_s=300,
                            workflow_timeout_s=5100)
    wrapper=Path(quality_stage_arm.__file__).resolve()
    entry=worker/'entry.py';entry.write_text('raise AssertionError("must not run")')
    driver=worker/'strict_review_case_arm.py';driver.write_text('raise AssertionError("must not run")')
    packet={'schema':'quality-stage/1','arm_entry':str(entry),
            'files':{str(path):hashed(path) for path in [entry,driver,Path(sys.executable)]},
            'phases':[{'name':'native-workflow','python':sys.executable,'driver':str(driver),
                       'arguments':['{shim}'],'timeout_s':5400}]}
    path=worker/'strict-phases.json';path.write_text(json.dumps(packet))
    manifest['bindings'].update({str(path):hashed(path),str(wrapper):hashed(wrapper)})
    for stage in manifest['stages']:
        stage.update(case_id='strict',timeout_s=7200,
            command=[sys.executable,'-I',str(wrapper),'--phases',str(path),
                     '--sha256',hashed(path),'--shim','{shim}'])
    validate(manifest)
    packet['phases'][0]['timeout_s']=5399
    path.write_text(json.dumps(packet));manifest['bindings'][str(path)]=hashed(path)
    for stage in manifest['stages']:stage['command'][6]=hashed(path)
    with pytest.raises(Refused,match='workflow wait and host preparation'):
        run(manifest,tmp_path/'output')
    assert not (tmp_path/'output').exists()


@pytest.mark.parametrize('case', ['foundation', 'screening', 'history', 'module-card',
                                  'discovery', 'strict-depth', 'lightweight-depth'])
def test_init_workflow_deadline_refuses_before_stage_or_native_intent(tmp_path, case):
    from test_run_paired import fixture
    from run_paired import validate, hashed, run
    worker = tmp_path / 'worker'; worker.mkdir()
    manifest = fixture(worker)
    manifest['plan'].update(native_call_limits={case: 14}, host_timeout_s=300,
                            workflow_timeout_s=4230)
    wrapper = Path(quality_stage_arm.__file__).resolve()
    entry = worker / 'entry.py'; entry.write_text('raise AssertionError("must not run")')
    driver = worker / 'init_entry.py'; driver.write_text('from init_native_arm import main\n')
    packet = {'schema': 'quality-stage/1', 'arm_entry': str(entry),
              'files': {str(p): hashed(p) for p in [entry, driver, Path(sys.executable)]},
              'phases': [{'name': 'native-workflow', 'python': sys.executable,
                         'driver': str(driver), 'arguments': ['{shim}'], 'timeout_s': 4530}]}
    path = worker / 'phases.json'
    def freeze():
        path.write_text(json.dumps(packet))
        manifest['bindings'].update({str(path): hashed(path), str(wrapper): hashed(wrapper)})
        for stage in manifest['stages']:
            stage.update(case_id=case, timeout_s=4560,
                command=[sys.executable, '-I', str(wrapper), '--phases', str(path),
                         '--sha256', hashed(path), '--shim', '{shim}'])
    freeze(); validate(manifest)
    packet['phases'][0]['timeout_s'] = 4529
    freeze()
    with pytest.raises(Refused, match='init phase deadline'):
        run(manifest, tmp_path / 'run')
    assert not (tmp_path / 'run').exists()
    # Original frozen records remain inspectable without new dispatch.
    validate(manifest, for_dispatch=False)


def test_outer_deadline_cannot_cut_off_serial_quality_phases(tmp_path):
    from test_run_paired import fixture
    from run_paired import validate, hashed, run
    worker = tmp_path / 'worker'; worker.mkdir()
    manifest = fixture(worker)
    wrapper = Path(quality_stage_arm.__file__).resolve()
    entry = worker / 'entry.py'; entry.write_text('raise AssertionError("must not run")')
    driver = worker / 'control.py'; driver.write_text('raise AssertionError("must not run")')
    packet = {'schema': 'quality-stage/1', 'arm_entry': str(entry),
              'files': {str(p): hashed(p) for p in [entry, driver, Path(sys.executable)]},
              'phases': [{'name': name, 'python': sys.executable, 'driver': str(driver),
                         'arguments': ['{shim}'], 'timeout_s': 100} for name in ['first', 'second']]}
    path = worker / 'phases.json'; path.write_text(json.dumps(packet))
    manifest['bindings'].update({str(path): hashed(path), str(wrapper): hashed(wrapper)})
    for stage in manifest['stages']:
        stage.update(timeout_s=230, command=[sys.executable, '-I', str(wrapper),
            '--phases', str(path), '--sha256', hashed(path), '--shim', '{shim}'])
    validate(manifest)
    for stage in manifest['stages']: stage['timeout_s'] = 229
    with pytest.raises(Refused, match='all serial quality phases'):
        run(manifest, tmp_path / 'run')
    assert not (tmp_path / 'run').exists()
