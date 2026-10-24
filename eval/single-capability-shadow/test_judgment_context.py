"""Judgment entries must bind the frozen role, not a fixture model or timeout."""
from pathlib import Path
import hashlib
import json
import sys

import pytest
import block_arm
import maintenance_arm
import discovery_arm
import strict_depth_arm


JUDGMENT_DRIVERS = {
    'block_arm': block_arm, 'maintenance_arm': maintenance_arm,
    'discovery_arm': discovery_arm, 'strict_depth_arm': strict_depth_arm,
}


def frozen(tmp_path, monkeypatch, *, explicit=True):
    shim = tmp_path / 'codex'; shim.write_text('not a native executable')
    profile = {'model': 'transport-default', 'reasoning_effort': 'high'}
    if explicit:
        profile['roles'] = {'generator': {'provider': 'zcode', 'model': 'GLM-5.3', 'effort': ''},
                            'judge': {'provider': 'codex', 'model': 'live-judge', 'effort': 'high'}}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'plan': {'execution_profile': profile,
        'native_timeout_s': 90, 'host_timeout_s': 300}}))
    monkeypatch.setenv('SHADOW_ARM_CONFIG', str(path))
    return shim


@pytest.mark.parametrize('mode', ['strict', 'lightweight'])
def test_depth_entry_uses_frozen_judge_and_orchestration_deadline(tmp_path, monkeypatch, mode):
    import depth_arm
    shim = frozen(tmp_path, monkeypatch)
    folder = tmp_path / 'inputs'; folder.mkdir()
    payload = {'sections': {'flow': 'shown call', 'configuration': 'shown default'},
               'feature': 'step', 'pin': 'a' * 40, 'existing_knowledge': {}, 'evidence': []}
    data = ('<untrusted_data>\n' + json.dumps(payload) + '\n</untrusted_data>').encode()
    (folder / 'depth-seven-prompt.txt').write_bytes(data)
    (folder / 'manifest.json').write_text(json.dumps({'files': {
        'depth-seven-prompt.txt': {'sha256': hashlib.sha256(data).hexdigest()}}}))
    def gateway(settings, **kwargs):
        assert settings.strict_backend_model == 'transport-default'
        assert settings.strict_backend_timeout_s == 300
        return object()
    def review(rt, *args, **kwargs):
        assert (rt.judge.provider, rt.judge.model, rt.judge.effort) == ('codex', 'live-judge', 'high')
        return {'facets': {'flow': {'verdict': 'pass' if mode == 'lightweight' else 'unjudged'},
                           'configuration': {'verdict': 'unjudged'}}}
    monkeypatch.setattr(depth_arm, 'model_gateway', gateway)
    monkeypatch.setattr(depth_arm, 'review_facets', review)
    monkeypatch.setattr(sys, 'argv', ['depth', '--inputs', str(folder), '--shim', str(shim),
        '--mode', mode, '--output', str(tmp_path / 'output.json')])
    depth_arm.main()


@pytest.mark.parametrize('module', ['block_arm', 'maintenance_arm', 'discovery_arm', 'strict_depth_arm'])
def test_judgment_override_refuses_before_inputs_or_provider(tmp_path, monkeypatch, module):
    from ledger import Refused
    selected = JUDGMENT_DRIVERS[module]
    shim = frozen(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    if module in ('block_arm', 'maintenance_arm'):
        command = ['--inputs', str(tmp_path / 'missing'), '--shim', str(shim),
                   '--model', 'wrong-model', '--output', str(tmp_path / 'output.json')]
        if module == 'block_arm': command += ['--case', 'screening']
    else:
        command = ([str(shim)] if module == 'discovery_arm' else ['--shim', str(shim)])
        command += ['--model', 'wrong-model']
    monkeypatch.setattr(selected, 'model_gateway', lambda *a, **k: pytest.fail('role drift constructed a gateway'))
    monkeypatch.setattr(sys, 'argv', [module, *command])
    with pytest.raises(Refused, match='frozen judge'):
        selected.main()
    assert not (tmp_path / 'source').exists() and not (tmp_path / 'src').exists()


def test_legacy_default_roles_keep_the_frozen_model_and_effort(tmp_path, monkeypatch):
    from arm_runtime import judgment_context, model_roles
    shim = frozen(tmp_path, monkeypatch, explicit=False)
    settings, judge = judgment_context(str(shim), 'transport-default')
    generator, stage_judge = model_roles(str(shim))
    assert settings.strict_backend_model == judge.model == stage_judge.model == generator.model == 'transport-default'
    assert judge.effort == stage_judge.effort == generator.effort == 'high'
    assert judge.provider == stage_judge.provider == generator.provider == 'codex'


@pytest.mark.parametrize('entry', ['stage', 'judgment'])
def test_entry_preserves_case_context_for_invocation_role_validation(tmp_path, monkeypatch, entry):
    from arm_runtime import judgment_context, model_roles
    from ledger import Refused
    shim = frozen(tmp_path, monkeypatch)
    config = tmp_path / 'config.json'
    data = json.loads(config.read_text())
    data['plan']['native_call_limits'] = {'strict': 13, 'lightweight-depth': 10}
    data['plan']['execution_profile']['invocation_roles_by_case'] = {
        'strict': {'harness': 'judge', 'harness_session': 'judge'}}
    config.write_text(json.dumps(data))
    load = (lambda: model_roles(str(shim))) if entry == 'stage' else (lambda: judgment_context(str(shim)))
    _, judge = load()
    assert (judge.provider, judge.model, judge.effort) == ('codex', 'live-judge', 'high')
    del data['plan']['native_call_limits']['strict']
    config.write_text(json.dumps(data))
    with pytest.raises(Refused, match='declared cases'):
        load()
