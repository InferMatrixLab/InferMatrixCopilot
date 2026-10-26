"""A sandbox profile never grants permission to retain omitted payloads."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from arm_runtime import model_gateway, validate_payload_retention
from ledger import Refused


@pytest.fixture
def retained(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    area = tmp_path / 'candidate'; area.mkdir(mode=0o700)
    shim = area / 'codex'; shim.write_text('not executed')
    evidence = tmp_path / 'input.txt'; evidence.write_text('frozen evaluation evidence')
    manifest = tmp_path / 'retained-inputs.json'
    manifest.write_text(json.dumps({'schema': 'shadow-retained-inputs/1', 'files': {
        str(evidence): hashlib.sha256(evidence.read_bytes()).hexdigest()}}))
    policy = {'schema': 'shadow-payload-retention/1', 'mode': 'retain-request-result',
        'publication': 'disabled', 'case_ids': ['history'], 'root': str(tmp_path),
        'input_manifest': str(manifest), 'input_manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest()}
    config = {'plan': {'execution_kind': 'fixture', 'native_call_limits': {'history': 4, 'other': 1},
                      'payload_retention': policy}, 'case_id': 'history', 'arm': 'candidate',
              'ledger_path': str(tmp_path / 'calls.sqlite')}
    path = area / 'config.json'; path.write_text(json.dumps(config))
    monkeypatch.setenv('SHADOW_ARM_CONFIG', str(path))
    return config, path, shim, evidence, manifest


@pytest.mark.parametrize('damage', ['schema', 'mode', 'publication', 'case', 'root', 'permissions',
                                    'manifest', 'input', 'symlink', 'unknown_field'])
def test_invalid_retention_cannot_construct_a_model_transport(retained, monkeypatch, damage):
    config, path, shim, evidence, manifest = retained
    policy = config['plan']['payload_retention']
    if damage == 'schema': policy['schema'] = 'unknown'
    elif damage == 'mode': policy['mode'] = 'implicit'
    elif damage == 'publication': policy['publication'] = 'enabled'
    elif damage == 'case': policy['case_ids'] = ['unplanned']
    elif damage == 'root': policy['root'] = str(path.parent)
    elif damage == 'permissions': Path(policy['root']).chmod(0o755)
    elif damage == 'manifest': manifest.write_text('{}')
    elif damage == 'input': evidence.write_text('changed')
    elif damage == 'symlink':
        alias = evidence.with_name('alias.json'); alias.symlink_to(manifest)
        policy['input_manifest'] = str(alias)
    else: policy['silent'] = True
    path.write_text(json.dumps(config))
    monkeypatch.setattr('arm_runtime.supports_shared_execution', lambda: False)
    with pytest.raises(Refused):
        model_gateway(SimpleNamespace(), shim=str(shim),
                      transport_factory=lambda _: pytest.fail('invalid policy constructed transport'))


def test_default_and_unlisted_case_preserve_payload_omission(retained):
    config, path, shim, _, _ = retained
    config['case_id'] = 'other'
    assert validate_payload_retention(config, path) is None
    config['plan'].pop('payload_retention')
    assert validate_payload_retention(config, path) is None


def test_explicit_policy_changes_only_evaluation_recording(retained, monkeypatch):
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
    config, path, shim, evidence, _ = retained
    monkeypatch.setattr('arm_runtime.supports_shared_execution', lambda: False)
    monkeypatch.setattr('arm_runtime.application_history_payloads', lambda: None)
    calls = []
    def call(self, role, **kwargs): calls.append(kwargs); return 'result'
    monkeypatch.setattr(ModelGateway, 'call_json', call)
    gateway = model_gateway(SimpleNamespace(), shim=str(shim))
    request = {'system': 'rubric', 'prompt': 'frozen input', 'record_payload': False}
    assert gateway.call_json(ModelRole('generator', 'codex', 'model'), **request) == 'result'
    assert calls == [{**request, 'record_payload': True}]
    assert request['record_payload'] is False
    evidence.write_text('changed after first call')
    with pytest.raises(Refused): gateway.call_json(ModelRole('generator', 'codex', 'model'), **request)
    assert len(calls) == 1


def test_recording_policy_cannot_be_changed_after_gateway_construction(retained, monkeypatch):
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
    config, path, shim, _, _ = retained
    monkeypatch.setattr('arm_runtime.supports_shared_execution', lambda: False)
    monkeypatch.setattr(ModelGateway, 'call_json', lambda *a, **kw: pytest.fail('changed config dispatched'))
    gateway = model_gateway(SimpleNamespace(), shim=str(shim))
    config['plan']['payload_retention']['case_ids'].append('other')
    path.write_text(json.dumps(config))
    with pytest.raises(Refused, match='configuration changed'):
        gateway.call_json(ModelRole('generator', 'codex', 'model'), record_payload=False)


# Reuse the real canonical adapter/archive fixture; only the native boundary is fake.
from test_arm_runtime import arm, configured, settings, worker_receipt  # noqa: E402,F401


def test_default_configured_gateway_still_refuses_omission_before_dispatch(arm):
    from infermatrix_copilot.kb_service.models import ModelRole
    from infermatrix_copilot.providers.completion_profiles import CompletionPolicyError
    settings, calls, shim, _ = arm
    gateway = model_gateway(settings, shim=shim)
    with pytest.raises(CompletionPolicyError, match='retained request/result evidence'):
        gateway.call_json(ModelRole('generator', 'codex', 'fixture'),
                          system='omitted', prompt='omitted', record_payload=False)
    assert calls == []
    assert not (shim.parent.parent / 'calls.sqlite').exists()


def test_explicit_current_gateway_keeps_one_canonical_native_receipt_and_charge(arm):
    from infermatrix_copilot.kb_service.models import ModelRole
    from infermatrix_copilot.trace_store import TraceStore
    import sqlite3
    settings, calls, shim, _ = arm
    path = shim.parent / 'config.json'; config = json.loads(path.read_bytes())
    root = shim.parent.parent; root.chmod(0o700)
    evidence = root / 'input.txt'; evidence.write_text('frozen synthetic evaluation input')
    manifest = root / 'retained-inputs.json'
    manifest.write_text(json.dumps({'schema': 'shadow-retained-inputs/1', 'files': {
        str(evidence): hashlib.sha256(evidence.read_bytes()).hexdigest()}}))
    config['plan']['payload_retention'] = {'schema': 'shadow-payload-retention/1',
        'mode': 'retain-request-result', 'publication': 'disabled', 'case_ids': ['case'],
        'root': str(root), 'input_manifest': str(manifest),
        'input_manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest()}
    path.write_text(json.dumps(config))
    records = []; gateway = model_gateway(settings, shim=shim, recorder=records.append)
    with pytest.raises(Refused, match='application must select its payload policy'):
        gateway.call_json(ModelRole('generator', 'codex', 'fixture'),
            system='synthetic evaluation', prompt='frozen input', record_payload=False)
    assert not calls and not (root / 'calls.sqlite').exists()
    reply = gateway.call_json(ModelRole('generator', 'codex', 'fixture'),
        system='synthetic evaluation', prompt='frozen input', record_payload=True)
    assert reply.data == {'ok': True} and len(calls) == len(records) == 1
    assert records[0]['prompt'] == 'frozen input'
    with sqlite3.connect(root / 'calls.sqlite') as db:
        rows = db.execute('SELECT status,result FROM calls').fetchall()
    assert len(rows) == 1 and rows[0][0] == 'complete'
    receipt = json.loads(rows[0][1]); store = TraceStore(receipt['trace_root'])
    native = [row for row, _ in store.iter_records(kind='model_call')]
    assert len(native) == 1
    store.validate_native_attempt(native[0])
    assert receipt['accounting_usage']['input_tokens'] == 10


@pytest.mark.parametrize('missing', ['runtime', 'manifest'])
def test_retention_implementation_and_input_manifest_must_be_bound(retained, tmp_path, missing):
    from test_run_paired import fixture
    import run_paired
    from run_paired import hashed, validate
    config, path, _, _, manifest_path = retained
    planned = fixture(tmp_path)
    policy = dict(config['plan']['payload_retention']); policy['case_ids'] = ['case']
    planned['plan']['payload_retention'] = policy
    helper = str(Path(run_paired.__file__).resolve().with_name('arm_runtime.py'))
    planned['bindings'].update({helper: hashed(helper), str(manifest_path): hashed(manifest_path)})
    validate(planned)
    planned['bindings'].pop(helper if missing == 'runtime' else str(manifest_path))
    with pytest.raises(Refused, match='not hash-bound'):
        run_paired.run(planned, tmp_path / 'new-stage-root')
    assert not (tmp_path / 'new-stage-root').exists()


def test_retention_cannot_move_to_a_different_execution_root(retained, tmp_path):
    from test_run_paired import fixture
    import run_paired
    from run_paired import hashed
    config, _, _, _, manifest_path = retained
    planned = fixture(tmp_path)
    policy = dict(config['plan']['payload_retention']); policy['case_ids'] = ['case']
    planned['plan']['payload_retention'] = policy
    helper = str(Path(run_paired.__file__).resolve().with_name('arm_runtime.py'))
    planned['bindings'].update({helper: hashed(helper), str(manifest_path): hashed(manifest_path)})
    with pytest.raises(Refused, match='root differs'):
        run_paired.run(planned, tmp_path / 'different-output')
    assert not (tmp_path / 'different-output').exists()
