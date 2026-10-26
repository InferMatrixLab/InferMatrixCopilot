"""Complete quality plans fail before stage or native admission on input leaks."""
import copy
from pathlib import Path
import random
import shutil

import pytest

import quality_plan
from ledger import Refused
from run_paired import hashed, run, validate
from test_run_paired import fixture as coordinator_fixture


def frozen(tmp_path):
    worker = tmp_path / "worker"; worker.mkdir()
    manifest = coordinator_fixture(worker)
    manifest['plan']['workflow_timeout_s'] = 60
    manifest['plan']['execution_profile']['role_bindings'] = {'pr-reviewer': 'judge'}
    manifest['plan']['execution_profile']['invocation_roles_by_case'] = {'strict': {'harness': 'judge', 'harness_session': 'judge'}}
    manifest['plan']['execution_profile']['roles'] = {
        'generator': {'provider': 'zcode', 'model': 'GLM-5.3', 'effort': 'high'},
        'judge': {'provider': 'codex', 'model': 'fake', 'effort': 'high'}}
    guard = Path(manifest['guard_script']).parent
    for name in ('role_profiles.py', 'arm_runtime.py', 'shared_execution.py'):
        target = guard / name
        shutil.copyfile(Path(__file__).with_name(name), target)
        manifest['bindings'][str(target)] = hashed(target)
    for roots in manifest["plan"]["shared_execution"]["source_roots_by_arm"].values():
        roots.append(str(tmp_path / "run"))
    source = worker / "source.txt"; source.write_text("independent source input")
    private = tmp_path / "private"; private.mkdir()
    label = private / "labels.json"; label.write_text('{"expected": "host only"}')
    helper = Path(quality_plan.__file__).resolve()
    stage_helper = Path(__file__).with_name('init_native_arm.py').resolve()
    manifest['bindings'][str(stage_helper)] = hashed(stage_helper)
    manifest["bindings"].update({str(path): hashed(path) for path in (source, label, helper)})
    manifest["plan"]["native_call_limits"] = {case: 1 for case in sorted(quality_plan.STRATA)}
    template = manifest["stages"][0]
    order = random.Random(manifest["order_seed"])
    stages = []
    for case in sorted(quality_plan.STRATA):
        arms = ["baseline", "candidate"]; order.shuffle(arms)
        for arm in arms:
            stages.append({**copy.deepcopy(template), "case_id": case, "arm": arm})
    manifest["stages"] = stages
    manifest["quality_evaluation"] = {
        "schema": "native-quality/1", "acceptance": "No correctness regression; unresolved outcomes remain pending.",
        "cases": {case: {"scope": "source-audit-workflow" if case == "maintenance" else "full-native-workflow",
                         "inputs": {str(source): hashed(source)}, "private_labels": {str(label): hashed(label)}}
                  for case in quality_plan.STRATA}}
    return manifest, source, label, helper


def test_domain_role_refusal_precedes_any_quality_stage_or_native_intent(tmp_path):
    manifest, _, _, _ = frozen(tmp_path)
    roles = manifest['plan']['execution_profile']['roles']
    roles['generator'] = dict(roles['judge'])
    output = tmp_path / 'run'
    with pytest.raises(Refused, match='independent model families'):
        run(manifest, output)
    assert not output.exists()
    # Recovery may inspect an original inadmissible profile without new dispatch.
    validate(manifest, for_dispatch=False)


def test_case_specific_knowledge_cannot_mount_private_labels(tmp_path):
    manifest, _, label, _ = frozen(tmp_path)
    plan = manifest['plan']
    plan['native_knowledge_by_case'] = {
        case: dict(plan['shared_execution']['knowledge_by_arm']) for case in plan['native_call_limits']}
    plan['native_knowledge_by_case']['direct']['candidate'] = str(label.parent)
    output = tmp_path / 'must-not-start'
    with pytest.raises(Refused, match='private quality labels are inside a worker'):
        run(manifest, output)
    assert not output.exists()
    assert not list(tmp_path.rglob('dispatch-marker'))


def test_quality_role_pins_cannot_be_omitted_before_dispatch(tmp_path):
    manifest, _, _, _ = frozen(tmp_path)
    manifest['plan']['execution_profile'].pop('roles')
    with pytest.raises(Refused, match='freeze supported generator and judge'):
        validate(manifest)


def test_private_descriptor_is_host_only_and_completed_pair_replays(tmp_path):
    manifest, _, label, _ = frozen(tmp_path)
    output = tmp_path / "run"
    assert run(manifest, output)["stages"] == 20
    configs = list(output.glob("*/guard-config.json")); assert len(configs) == 20
    assert all("quality_evaluation" not in path.read_text() and str(label) not in path.read_text() for path in configs)
    markers = {path: path.stat().st_mtime_ns for path in output.glob("*/dispatch-marker")}
    assert len(markers) == 20
    run(manifest, output)
    assert markers == {path: path.stat().st_mtime_ns for path in markers}


@pytest.mark.parametrize("damage", ["missing-case", "acceptance", "partial-workflow", "unbound-input",
                                    "unbound-label", "label-input", "copied-label", "worker-root",
                                    "knowledge-root", "schema-root", "knowledge-environment",
                                    "output-root", "argument", "helper", "input-drift", "label-drift"])
def test_invalid_quality_plan_cannot_create_stage_or_dispatch(tmp_path, damage):
    manifest, source, label, helper = frozen(tmp_path)
    case = manifest["quality_evaluation"]["cases"]["direct"]
    output = tmp_path / "run"
    if damage == "missing-case":manifest["quality_evaluation"]["cases"].pop("history")
    elif damage == "acceptance":manifest["quality_evaluation"]["acceptance"] = ""
    elif damage == "partial-workflow":case["scope"] = "isolated-judgment"
    elif damage == "unbound-input":manifest["bindings"].pop(str(source))
    elif damage == "unbound-label":manifest["bindings"].pop(str(label))
    elif damage == "label-input":case["inputs"][str(label)] = hashed(label)
    elif damage == "copied-label":
        source.write_bytes(label.read_bytes());manifest["bindings"][str(source)] = hashed(source)
        for row in manifest["quality_evaluation"]["cases"].values():row["inputs"][str(source)] = hashed(source)
    elif damage == "worker-root":manifest["plan"]["shared_execution"]["source_roots_by_arm"]["baseline"].append(str(label.parent))
    elif damage == "knowledge-root":manifest["plan"]["shared_execution"]["knowledge_by_arm"]["candidate"] = str(label.parent)
    elif damage == "schema-root":manifest["plan"]["shared_execution"]["schema_files_by_arm"] = {"baseline": {str(label): hashed(label)}}
    elif damage == "knowledge-environment":manifest["stages"][0]["environment"] = {"KNOWLEDGE_ROOT": str(label.parent)}
    elif damage == "output-root":output = label.parent
    elif damage == "argument":manifest["stages"][0]["command"].append(str(label))
    elif damage == "helper":manifest["bindings"].pop(str(helper))
    elif damage == "input-drift":source.write_text("changed after freeze")
    elif damage == "label-drift":label.write_text("changed expected result")
    with pytest.raises(Refused):run(manifest, output)
    assert not (output / "stages.sqlite").exists()
    assert not list(tmp_path.rglob("dispatch-marker"))


def test_legacy_conformance_plan_keeps_its_original_scope(tmp_path):
    manifest = coordinator_fixture(tmp_path)
    validate(manifest)


def phase_manifest(tmp_path):
    manifest, source, label, helper = frozen(tmp_path)
    evidence = tmp_path / 'private/external.json'; evidence.write_text('{"native_calls":0,"status":"deferred"}')
    manifest['bindings'][str(evidence)] = hashed(evidence)
    value = manifest['quality_evaluation']; value['schema'] = 'native-quality/2'
    value['external_cases'] = {case: {'status': 'deferred', 'reason': 'Separate declared validation phase',
        'evidence': {str(evidence): hashed(evidence)}} for case in quality_plan.STRATA - {'maintenance'}}
    manifest['plan']['native_call_limits'] = {'maintenance': 1}
    manifest['plan']['execution_profile'].pop('invocation_roles_by_case')
    manifest['stages'] = [stage for stage in manifest['stages'] if stage['case_id'] == 'maintenance']
    order = ['baseline', 'candidate']; random.Random(manifest['order_seed']).shuffle(order)
    manifest['stages'].sort(key=lambda stage: order.index(stage['arm']))
    return manifest, evidence


def test_declared_phase_executes_only_active_cases_and_never_claims_full_acceptance(tmp_path):
    manifest, _ = phase_manifest(tmp_path)
    # Non-discovery phases retain the explicit host choice of same-family roles.
    roles = manifest['plan']['execution_profile']['roles']; roles['generator'] = dict(roles['judge'])
    output = tmp_path / 'run'; result = run(manifest, output)
    assert result['stages'] == 2 and result['full_quality_acceptance'] is False
    assert len(result['external_cases']) == 9 and result['adjudication'] == 'pending'
    markers = {p: p.stat().st_mtime_ns for p in output.glob('*/dispatch-marker')}
    assert len(markers) == 2
    run(manifest, output)
    assert markers == {p: p.stat().st_mtime_ns for p in markers}


@pytest.mark.parametrize('damage', ['missing-case', 'missing-external', 'active-external', 'status', 'unbound', 'changed', 'model-input', 'worker-root'])
def test_phase_cannot_silently_drop_cases_or_expose_retained_evidence(tmp_path, damage):
    manifest, evidence = phase_manifest(tmp_path); value = manifest['quality_evaluation']
    if damage == 'missing-case': value['cases'].pop('discovery')
    elif damage == 'missing-external': value['external_cases'].pop('discovery')
    elif damage == 'active-external': value['external_cases']['maintenance'] = value['external_cases']['discovery']
    elif damage == 'status': value['external_cases']['discovery']['status'] = 'passed'
    elif damage == 'unbound': manifest['bindings'].pop(str(evidence))
    elif damage == 'changed': evidence.write_text('changed')
    elif damage == 'model-input': value['cases']['maintenance']['inputs'][str(evidence)] = hashed(evidence)
    else:
        for roots in manifest['plan']['shared_execution']['source_roots_by_arm'].values():roots.append(str(evidence.parent))
    with pytest.raises(Refused): run(manifest, tmp_path / 'run')
    assert not (tmp_path / 'run').exists()


def test_actual_unlimited_module_options_are_admitted_before_any_model(tmp_path):
    from init_native_arm import stage_role_error, stage_options
    configured = {'generator': {'provider': 'codex', 'model': 'gpt-6-sol'},
                  'judge': {'provider': 'codex', 'model': 'gpt-6-sol'}}
    assert stage_options('modules')['unlimited_subscription'] is True
    assert 'Zcode GLM-5.3' in stage_role_error('module-card', configured)
    assert stage_role_error('foundation', configured) is None


def test_history_request_role_must_be_bound_before_any_dispatch(tmp_path):
    manifest, _, _, _ = frozen(tmp_path)
    manifest['plan']['execution_profile'].pop('role_bindings')
    with pytest.raises(Refused, match='unplanned model role'):
        run(manifest, tmp_path / 'run')
    assert not (tmp_path / 'run').exists()


def test_strict_step_invocation_profile_is_required_before_first_model(tmp_path):
    manifest,_,_,_=frozen(tmp_path)
    manifest['plan']['execution_profile'].pop('invocation_roles_by_case')
    with pytest.raises(Refused,match='unplanned model role'):run(manifest,tmp_path/'run')
    assert not (tmp_path/'run').exists()


def test_strict_quality_plan_needs_explicit_whole_workflow_wait_before_dispatch(tmp_path):
    manifest, _, _, _ = frozen(tmp_path)
    manifest['plan'].pop('workflow_timeout_s')
    with pytest.raises(Refused, match='whole-workflow'):
        run(manifest, tmp_path/'run')
    assert not (tmp_path/'run').exists()
    # Original retained plans remain inspectable; this is not a dispatch waiver.
    validate(manifest, for_dispatch=False)


def test_selected_arm_original_proof_is_host_only_and_cannot_be_a_worker_input(tmp_path):
    import stage_selection
    manifest,_,_,_=frozen(tmp_path)
    position=next(i for i,s in enumerate(manifest['stages']) if s['arm']=='baseline')
    proof=tmp_path/'private/original.json';proof.write_text('{"original":"pending"}')
    helper=Path(stage_selection.__file__).resolve()
    manifest['bindings'].update({str(proof):hashed(proof),str(helper):hashed(helper)})
    manifest['stage_selection']={'schema':'selected-stages/1',
        'positions':[i for i in range(len(manifest['stages'])) if i!=position],
        'retained':{str(position):{'status':'pending','reason':'Original paid outcome retained',
                                 'evidence':{str(proof):hashed(proof)}}}}
    validate(manifest)
    manifest['plan']['shared_execution']['source_roots_by_arm']['candidate'].append(str(proof.parent))
    with pytest.raises(Refused,match='inside a worker'):
        run(manifest,tmp_path/'run')
    assert not (tmp_path/'run').exists()


def test_standalone_init_quality_entry_needs_whole_workflow_deadline(tmp_path):
    manifest, _, _, _ = frozen(tmp_path)
    driver = tmp_path / 'worker' / 'standalone_init.py'
    driver.write_text('from init_native_arm import main\n')
    manifest['bindings'][str(driver)] = hashed(driver)
    import init_native_arm
    import init_fixture_policies
    for module in (init_native_arm, init_fixture_policies):
        target = driver.with_name(Path(module.__file__).name)
        shutil.copy2(module.__file__, target)
        manifest['bindings'][str(target)] = hashed(target)
    for stage in manifest['stages']:
        if stage['case_id'] == 'foundation':
            stage.update(command=[stage['command'][0], '-I', str(driver), '{shim}'], timeout_s=70)
    validate(manifest)
    for stage in manifest['stages']:
        if stage['case_id'] == 'foundation': stage['timeout_s'] = 69
    output = tmp_path / 'run'
    with pytest.raises(Refused, match='init stage deadline'):
        run(manifest, output)
    assert not output.exists()
    validate(manifest, for_dispatch=False)


@pytest.mark.parametrize('case', ['direct', 'strict'])
def test_review_phase_must_cover_selected_case_host_deadline(tmp_path, case):
    import json
    import sys
    import arm_entry
    import quality_stage_arm
    import review_case_arm
    import strict_review_case_arm
    manifest, _, _, _ = frozen(tmp_path)
    plan = manifest['plan']
    plan['case_deadlines'] = {name: {'native_timeout_s': 10, 'host_timeout_s': 10,
                                    'workflow_timeout_s': 60} for name in plan['native_call_limits']}
    plan['case_deadlines'][case] = {'native_timeout_s': 40, 'host_timeout_s': 50,
                                  'workflow_timeout_s': 100}
    entry = Path(arm_entry.__file__).resolve()
    driver = Path((review_case_arm if case == 'direct' else strict_review_case_arm).__file__).resolve()
    packet = {'schema': 'quality-stage/1', 'arm_entry': str(entry),
              'files': {str(f): hashed(f) for f in (entry, driver, Path(sys.executable))},
              'phases': [{'name': 'native-workflow', 'python': sys.executable, 'driver': str(driver),
                          'arguments': [], 'timeout_s': 150}]}
    path = tmp_path / 'review-phase.json'
    def freeze():
        path.write_text(json.dumps(packet))
        manifest['bindings'][str(path)] = hashed(path)
        manifest['bindings'][str(Path(quality_stage_arm.__file__).resolve())] = hashed(quality_stage_arm.__file__)
        for stage in manifest['stages']:
            if stage['case_id'] == case:
                stage.update(command=[sys.executable, '-I', str(Path(quality_stage_arm.__file__).resolve()),
                             '--phases', str(path), '--sha256', hashed(path), '--shim', '{shim}'], timeout_s=180)
    freeze()
    validate(manifest)
    packet['phases'][0]['timeout_s'] = 149
    freeze()
    output = tmp_path / 'run'
    with pytest.raises(Refused, match='review phase deadline'):
        run(manifest, output)
    assert not output.exists()
