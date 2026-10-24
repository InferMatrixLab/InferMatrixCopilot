"""SDK readonly calls use canonical native/archive and generic sandbox paths."""
import json
import hashlib
import time

import pytest

from infermatrix_copilot.sdk.v1 import ReviewRuntime
from infermatrix_copilot.sandbox.base import SandboxPending
from infermatrix_copilot.sandbox.inputs import snapshot_directory
from infermatrix_copilot.trace_store import TraceStore
from test_completion_profile_configuration import configured  # noqa: F401


@pytest.fixture(autouse=True)
def worker_receipt(configured, monkeypatch):
    from infermatrix_copilot.providers.container_execution import ContainerExecution
    from infermatrix_copilot.providers.container_protocol import receipt_line
    from infermatrix_copilot.process_execution import ProcessOutcome
    original = ContainerExecution.run
    def run(self, **kwargs):
        result = original(self, **kwargs)
        request = json.loads(kwargs['input'])
        return ProcessOutcome(result.stdout, result.stderr + receipt_line(request,
            {'stdout': hashlib.sha256(result.stdout.encode()), 'stderr': hashlib.sha256(result.stderr.encode())},
            result.returncode, result.reason), result.returncode, result.reason)
    monkeypatch.setattr(ContainerExecution, 'run', run)


def invoke(settings, root, **kwargs):
    return ReviewRuntime.run_session('frozen evidence', cwd=root, command=['/host/must-not-run'],
        provider='codex', model='fixture', output_schema={'type': 'object'}, idle_timeout_s=2,
        absolute_timeout_s=20, execution_config=settings.native_completion_profiles['codex'],
        execution_root=root / 'readonly', settings=settings, **kwargs)


def test_sdk_readonly_reuses_provider_worker_and_original_receipt(configured, tmp_path):
    settings, calls = configured
    source = tmp_path / 'source';source.mkdir()
    (source / 'binary').write_bytes(b'\0\xffgit object')
    result = invoke(settings, tmp_path, frozen_inputs=snapshot_directory(source))
    assert result['payload'] == {'ok': True}
    assert result['accounting_usage']['input_tokens'] == 10
    assert result['usage']['input_tokens'] == 10
    assert len(calls) == 1
    envelope = json.loads(calls[0][1]['input'])
    assert envelope['version'] == 7
    assert envelope['controls'] == {'idle_timeout_s': 2, 'termination_grace_s': 5}
    assert '--output-schema' in envelope['argv']
    assert envelope['argv'][envelope['argv'].index('-C') + 1] == '/tmp/inputs/repository'
    binary = envelope['frozen_inputs']['files']['/tmp/inputs/repository/binary']
    assert 'sha256' in binary and 'base64' not in binary
    store = TraceStore(tmp_path / 'readonly/traces')
    records = [record for record, _ in store.iter_records(kind='model_call')]
    assert len(records) == 1
    store.validate_native_attempt(records[0])
    assert invoke(settings, tmp_path, frozen_inputs=snapshot_directory(source)) == result
    assert len(calls) == 1
    (source / 'binary').write_bytes(b'drift')
    with pytest.raises(SandboxPending, match='differs'):
        invoke(settings, tmp_path, frozen_inputs=snapshot_directory(source))
    assert len(calls) == 1


def test_readonly_unknown_call_is_not_dispatched_with_a_new_attempt(configured, tmp_path, monkeypatch):
    settings, calls = configured
    from infermatrix_copilot.providers.container_execution import ContainerExecution
    monkeypatch.setattr(ContainerExecution, 'run', lambda *a, **k: (_ for _ in ()).throw(
        SandboxPending('lost original response', prior_execution=True)))
    for _ in range(2):
        with pytest.raises(SandboxPending):
            invoke(settings, tmp_path)
    marker = json.loads((tmp_path / 'readonly/readonly-attempt.json').read_text())
    store = TraceStore(tmp_path / 'readonly/traces')
    assert len(list((store.root / 'attempts').iterdir())) == 1
    assert marker['native_attempt_id'] == next((store.root / 'attempts').iterdir()).name


def test_readonly_finished_trace_recovers_after_result_publication_crash(configured, tmp_path, monkeypatch):
    settings, calls = configured
    from infermatrix_copilot.providers import original_execution
    original = original_execution.immutable_write_bytes
    def write(path, *args, **kwargs):
        if path.name == 'readonly-result.json':
            raise OSError('result publication interrupted')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(original_execution, 'immutable_write_bytes', write)
        with pytest.raises(OSError):
            invoke(settings, tmp_path)
    assert invoke(settings, tmp_path)['payload'] == {'ok': True}
    assert len(calls) == 1


def test_direct_public_sdk_application_routes_its_trusted_profile_and_owned_source(configured, tmp_path, monkeypatch):
    import ast
    from dataclasses import replace
    from pathlib import Path
    from test_e2e_direct_runtime import setup
    from infermatrix_copilot.engine.steps.review import direct_run
    from infermatrix_copilot.providers.container_execution import ContainerExecution
    from infermatrix_copilot.providers.container_protocol import receipt_line
    from infermatrix_copilot.process_execution import ProcessOutcome
    settings, calls = configured
    config, request = setup(tmp_path, monkeypatch, 'codex', 0)
    code = ast.parse(Path(request.profile['command'][1]).read_text())
    payload = next(ast.literal_eval(node.value) for node in code.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == 'payload' for target in node.targets))
    profile = {**request.profile, 'execution_config': settings.native_completion_profiles['codex']}
    request = replace(request, profile=profile)
    config = replace(config, direct_profiles={'codex': profile})
    seen = []
    from infermatrix_copilot.providers.codex import CodexTransport
    monkeypatch.setattr(CodexTransport, 'bridge_probe', lambda self, profile, request:
        ({'schema': 'bridge-readiness/1', 'tools': sorted(request.scope.allowed_tools)},
         profile.sandbox._prepared_context()))
    def run(self, **kwargs):
        envelope = json.loads(kwargs['input']);seen.append(envelope)
        assert '/tmp/inputs/repository/source.py' in envelope['frozen_inputs']['files']
        assert '--output-schema' in envelope['argv']
        assert any('infermatrix-tools' in value for value in envelope['argv'])
        bridge = json.loads(envelope['frozen_inputs']['files']['/tmp/inputs/bridge.json'])
        assert bridge['scope']['read_only'] and bridge['scope']['strict_extras']
        assert 'doc_read' in bridge['scope']['allowed_tools']
        assert 'write_file' not in bridge['scope']['allowed_tools']
        assert bridge['knowledge_dir'] == '/tmp/inputs/knowledge'
        stdout = '\n'.join(map(json.dumps, [
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(payload)}},
            {'type': 'turn.completed'}]))
        return ProcessOutcome(stdout, receipt_line(envelope,
            {'stdout': hashlib.sha256(stdout.encode()), 'stderr': hashlib.sha256(b'')}, 0, None), 0)
    monkeypatch.setattr(ContainerExecution, 'run', run)
    monkeypatch.setattr(ContainerExecution, 'supported_input_protocol', lambda self, image: 6)
    with ReviewRuntime(config=config) as runtime:
        monkeypatch.setattr(runtime._core._q, 'put', lambda *args: None)
        handle = runtime.reserve_review(request)
        run_dir = Path(config.run_root) / handle.run_id
        monkeypatch.setattr(direct_run, 'Settings', lambda: runtime._core.settings)
        monkeypatch.setenv('COPILOT_DIRECT_PROFILE', json.dumps(profile))
        monkeypatch.setenv('COPILOT_DIRECT_REQUEST_SHA256', hashlib.sha256((run_dir / 'request.json').read_bytes()).hexdigest())
        assert direct_run.main([handle.run_id]) == 0
        assert runtime.get_result(handle.run_id).state == 'done'
        assert runtime.get_result(handle.run_id).review.direct_result['reviewed_head_sha'] == request.review.expected_head_sha
        assert runtime.find_review(request.idempotency_key, expected_head_sha=request.review.expected_head_sha).run_id == handle.run_id
    assert len(seen) == 1


def test_wire_schema_encoding_keeps_original_intent_and_replays_once(configured, worker_receipt, tmp_path):
    from copy import deepcopy
    from infermatrix_copilot.providers.original_execution import canonical
    from infermatrix_copilot.sandbox.inputs import merge_inputs
    settings, calls = configured
    schema={'type':'object','properties':{'optional':{'type':'string','pattern':'^(?:$|x)$'}}}
    before=deepcopy(schema)
    config=settings.native_completion_profiles['codex']
    options=dict(cwd=tmp_path,command=['/host/must-not-run'],provider='codex',model='fixture',
        output_schema=schema,idle_timeout_s=5,absolute_timeout_s=30,
        execution_config=config,execution_root=tmp_path/'schema-session')
    first=ReviewRuntime.run_session('evidence',**options)
    assert ReviewRuntime.run_session('evidence',**options)==first
    assert schema==before and len(calls)==1
    envelope=json.loads(calls[0][1]['input'])
    wire=json.loads(envelope['frozen_inputs']['files']['/tmp/inputs/readonly-schema.json'])
    assert wire['properties']['optional']['pattern']=='^(?:x)?$'
    original_inputs=merge_inputs(None,{'files':{'/tmp/inputs/readonly-schema.json':json.dumps(schema)},'sealed_dirs':[]})
    intent=json.loads((tmp_path/'schema-session/readonly-intent.json').read_text())
    assert intent['schema']==schema
    assert intent['inputs_sha256']==hashlib.sha256(canonical(original_inputs)).hexdigest()
