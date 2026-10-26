"""Trusted policy -> control -> original model dispatch and read-only recovery."""
from copy import deepcopy
import hashlib
import json
import socket
import tomllib

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.providers import container_execution as containers
from infermatrix_copilot.providers.codex_policy import CodexCompletionPolicy, DISABLED_FEATURES, TOOL_METADATA
from infermatrix_copilot.providers.completion import complete_native
from infermatrix_copilot.providers.completion_profiles import (
    CompletionPolicyError, normalize_completion_profiles, restore_completion_transport)
from infermatrix_copilot.providers.container_protocol import receipt_line
from infermatrix_copilot.providers.registry import transport_for_id
from infermatrix_copilot.process_execution import ProcessOutcome
from infermatrix_copilot.trace_store import NativeCompletionRecorder, TraceStore
from test_container_execution import Daemon


def policy_data():
    return {'kind': 'codex-restricted', 'version': 'codex-cli 0.162.1', 'catalog': {
        'etag': 'original-catalog', 'models': [
            {'slug': 'first', 'priority': 3, 'model_messages': {'instructions': 'original first'},
             'default_reasoning_level': 'high', 'context_window': 1000, 'input_modalities': ['text']},
            {'slug': 'second', 'priority': 1, 'model_messages': {'instructions': 'original second'},
             'supported_reasoning_levels': ['low', 'high'], 'context_window': 2000},
        ]}}


def test_complete_catalog_and_default_selection_metadata_are_preserved():
    original = policy_data()
    policy = CodexCompletionPolicy.build(original)
    after = json.loads(policy.catalog_json)
    assert after['etag'] == original['catalog']['etag']
    assert [model['slug'] for model in after['models']] == ['first', 'second']
    for before, result in zip(original['catalog']['models'], after['models']):
        assert {key: value for key, value in result.items() if key not in TOOL_METADATA} == before
        assert {key: result[key] for key in TOOL_METADATA} == TOOL_METADATA
    assert '-c' in policy.arguments() and not any(arg.startswith('model=') for arg in policy.arguments())
    with pytest.raises(ValueError, match='absent'):
        policy.arguments(model='missing')
    assert original == policy_data()  # compilation cannot mutate caller/cache data


def test_optional_policy_does_not_change_old_structured_profile_bytes():
    from infermatrix_copilot.providers.completion_profiles import completion_config_dict, parse_completion_config
    value = {'kind': 'sandbox', 'backend': {'kind': 'docker', 'docker': '/docker',
             'socket': '/daemon', 'image': 'sha256:' + 'a' * 64}, 'location': {'kind': 'local'}}
    old = normalize_completion_profiles({'codex': value})
    assert 'policy' not in old['codex']
    assert 'policy' not in completion_config_dict(parse_completion_config(old['codex']))


@pytest.fixture
def configured(tmp_path, monkeypatch):
    docker = tmp_path/'docker';docker.write_text('#!/bin/sh\nexit 99\n');docker.chmod(0o700)
    channel = socket.socket(socket.AF_UNIX);channel.bind(str(tmp_path/'daemon'))
    config = {'kind': 'sandbox', 'backend': {'kind': 'docker', 'docker': str(docker),
              'socket': str(tmp_path/'daemon'), 'image': 'sha256:'+'a'*64},
              'location': {'kind': 'local'}, 'policy': policy_data()}
    settings = Settings(_env_file=None, native_completion_profiles={'codex': config},
                        trace_store_root=str(tmp_path/'traces'), strict_backend_model='', metrics_enabled=False)
    daemons, calls, damage = {}, [], {}
    def daemon(root):
        return daemons.setdefault(str(root), Daemon())
    monkeypatch.setattr(containers.ContainerExecution, '_control', lambda self, *args: daemon(self.root).control(*args))
    def execute(command, *, cwd, input, cleanup, **kwargs):
        request = json.loads(input);calls.append((str(cwd), request))
        if '--configuration-probe' in request['argv']:
            control = json.loads(request['input'])
            flags = {}
            args = control['configuration_arguments']
            for index in range(0, len(args), 2):
                if args[index] == '-c': flags.update(tomllib.loads(args[index+1]))
            view = {'model': flags.get('model'), 'model_reasoning_effort': flags.get('model_reasoning_effort'),
                    'model_catalog_json': flags['model_catalog_json'], 'web_search': 'disabled',
                    'features': {name: False for name in DISABLED_FEATURES},
                    'tools': {'update_plan': False, 'experimental_request_user_input': False},
                    'configured_startup_keys': [], 'mcp_servers': {
                        name: {'enabled': server.get('enabled', True)}
                        for name, server in flags.get('mcp_servers', {}).items()}}
            if damage.get('feature'): view['features']['hooks'] = True
            if damage.get('tools'): view['tools']['update_plan'] = None
            if damage.get('mcp'): view['mcp_servers']['managed'] = {'enabled': True}
            from infermatrix_copilot.providers.codex_policy import requirements_view
            requirements = damage.get('requirements')
            stdout = json.dumps({'schema': 'codex-configuration/1', 'version': 'codex-cli 0.162.1',
                                'methods_sent': ['initialize', 'config/read', 'configRequirements/read'],
                                'requirements_checked': True, 'requirements_empty': requirements is None,
                                'requirements': requirements_view(requirements),
                                'effective': view, 'layers': []})
        else:
            stdout = '\n'.join(map(json.dumps, [
                {'type':'item.completed','model':'reported-model','item':{'type':'agent_message','text':'{"ok":true}'}},
                {'type':'turn.completed','usage':{'input_tokens':7,'output_tokens':3}}]))
        daemon(cwd).container['State'] = {'Running':False,'Status':'exited','ExitCode':0}
        cleanup()
        return ProcessOutcome(stdout, receipt_line(request, {'stdout':hashlib.sha256(stdout.encode()),
                              'stderr':hashlib.sha256(b'')}, 0, None), 0)
    monkeypatch.setattr(containers, 'run_process', execute)
    try:
        yield settings, calls, damage
    finally:
        channel.close()


def call(configured, *, model='first', effort='high'):
    settings, _, _ = configured
    store = TraceStore(settings.trace_store_root)
    request = {'system':'policy','messages':[{'role':'user','content':'frozen evidence'}], 'model':model,'effort':effort}
    # Message-shaped requests use the canonical buffered recorder wrapper.
    entries = []
    class Recorder:
        def durable(self): return self
        def begin_call(self, value): return store.begin_call(value)
        def __call__(self, value):
            entries.append(value)
            return store.append('model_call',inputs={},outputs={},model={'provider':'codex'},
                                usage=value['usage'], result={'native_attempt_id':value['native_attempt_id']})
    result = complete_native(lambda: transport_for_id(settings,'codex'), request=request,
        identity={'provider':'codex'}, recorder=Recorder(), settings=settings)
    return result, request, store, entries


@pytest.mark.parametrize('model,effort', [('first','high'), ('second','low'), ('','')])
def test_control_precedes_one_model_and_original_policy_survives_read_only_recovery(configured, model, effort):
    settings, calls, _ = configured
    (reply, _, record, _), request, store, entries = call(configured, model=model, effort=effort)
    assert reply.text == '{"ok":true}' and reply.model == 'reported-model'
    assert record['usage']['input_tokens'] == 7
    assert len(calls) == 2 and '--configuration-probe' in calls[0][1]['argv']
    assert calls[1][1]['argv'][1] == 'exec'
    assert calls[0][1]['frozen_inputs'] == calls[1][1]['frozen_inputs']
    assert len([row for row,_ in store.iter_records(kind='model_call')]) == 1
    controls = [row for row,_ in store.iter_records(kind='configuration_probe')]
    assert len(controls) == 1 and controls[0]['result']['model_calls'] == 0
    root = store.root/'attempts'/entries[-1]['native_attempt_id']
    recovered = restore_completion_transport(settings, transport_for_id(settings,'codex'), root, recover=True)
    assert recovered.get_completion_result(**request, recover=True).text == reply.text
    assert len(calls) == 2
    with pytest.raises(CompletionPolicyError): recovered.complete(**request)
    assert len(calls) == 2


@pytest.mark.parametrize('damage', ['feature','tools','mcp'])
def test_failed_effective_controls_cannot_dispatch_or_fallback(configured, damage):
    settings, calls, state = configured
    state[damage] = True
    with pytest.raises(ValueError) as error:
        call(configured)
    assert error.value.native_call_sent is False
    assert error.value.allow_fallback is False
    assert len(calls) == 1 and '--configuration-probe' in calls[0][1]['argv']


def test_changed_original_policy_proof_refuses_recovery_without_redispatch(configured):
    settings, calls, _ = configured
    (_,_,record,_), request, store, entries = call(configured)
    root = store.root/'attempts'/entries[-1]['native_attempt_id']
    proof = root/'native-policy-proof.json'
    proof.write_text('{}')
    with pytest.raises(CompletionPolicyError):
        restore_completion_transport(settings, transport_for_id(settings,'codex'), root, recover=True)
    assert len(calls) == 2


def test_control_journal_failure_cannot_enter_model_dispatch(configured, monkeypatch):
    settings, calls, _ = configured
    original = TraceStore.append
    def append(self, kind, **kwargs):
        if kind == 'configuration_probe':
            raise OSError('control journal unavailable')
        return original(self, kind, **kwargs)
    monkeypatch.setattr(TraceStore, 'append', append)
    with pytest.raises(CompletionPolicyError) as error:
        call(configured)
    assert error.value.native_call_sent is False and error.value.allow_fallback is False
    assert len(calls) == 1 and '--configuration-probe' in calls[0][1]['argv']


def test_durable_run_restores_original_catalog_and_policy(configured, tmp_path):
    from infermatrix_copilot.app.completion_execution import completion_settings_for_run
    from infermatrix_copilot.app.reservation import RunReservation
    from infermatrix_copilot.task_spec import TaskSpec
    settings, _, _ = configured
    settings = settings.model_copy(update={'run_root': tmp_path/'runs'})
    run_id, created = RunReservation(settings).reserve(TaskSpec(kind='pr_review', repo='toy', pr=1),
        owner_server_id='owner', owner_server_pid=1, idempotency_key='original-key')
    assert created
    changed = settings.model_copy(deep=True)
    changed.native_completion_profiles['codex']['policy']['catalog']['models'][0]['model_messages']['instructions'] = 'changed'
    restored = completion_settings_for_run(changed, settings.run_root/run_id, resuming=True)
    assert restored.native_completion_profiles == settings.native_completion_profiles


def test_control_rows_do_not_supply_native_model_outcomes(configured):
    settings, calls, _ = configured
    (_,_,record,_), request, store, entries = call(configured)
    controls = [row for row,_ in store.iter_records(kind='configuration_probe')]
    with pytest.raises(ValueError):
        store.validate_native_attempt(controls[0])
    models = [row for row,_ in store.iter_records(kind='model_call')]
    assert len(models) == 1 and models[0]['usage'] == {'input_tokens':7,'output_tokens':3,'input_tokens_include_cache':True}


@pytest.mark.parametrize('requirements', [
    {'allowedWebSearchModes': ['disabled'], 'allowedLoginMethods': None},
    {'featureRequirements': {'hooks': False, 'shell_tool': False}, 'allowedLoginMethods': ['api', 'chatgpt']},
    {'allowBrowserAndComputerUse': False, 'allowRemoteControl': False, 'allowAppshots': False},
])
def test_compatible_managed_constraints_survive_control_model_and_recovery(configured, requirements):
    settings, calls, state = configured
    state['requirements'] = requirements
    (reply, _, _, _), request, store, entries = call(configured)
    archive = store.root/'attempts'/entries[-1]['native_attempt_id']
    saved = json.loads((archive/'native-policy-request.json').read_text())
    assert saved['schema'] == 'native-policy-request/2'
    assert saved['frozen_inputs']['sealed_dirs'] == []
    assert saved['frozen_inputs']['sealed_files'] == ['/etc/codex/config.toml']
    recovered = restore_completion_transport(settings, transport_for_id(settings,'codex'), archive, recover=True)
    assert recovered.get_completion_result(**request, recover=True).text == reply.text
    assert len(calls) == 2


@pytest.mark.parametrize('requirements', [
    {}, {'featureRequirements': {'hooks': True}}, {'featureRequirements': {'network_proxy': True}},
    {'allowedWebSearchModes': ['live']}, {'allowRemoteControl': True},
    {'modelProvider': 'changed'}, {'models': {'newThread': {'model': 'changed'}}},
    {'allowedLoginMethods': ['api']}, {'hooks': {'SECRET': 'PRIVATE_COMMAND'}},
])
def test_incompatible_or_unverified_managed_constraints_never_dispatch(configured, requirements):
    settings, calls, state = configured
    state['requirements'] = requirements
    with pytest.raises(ValueError) as error:
        call(configured)
    assert error.value.native_call_sent is False and error.value.allow_fallback is False
    assert len(calls) == 1 and '--configuration-probe' in calls[0][1]['argv']


def test_original_directory_projection_remains_a_distinct_compatibility_contract():
    data = policy_data()
    current = CodexCompletionPolicy.build(data)
    legacy = CodexCompletionPolicy.build(data, legacy_inputs=True)
    assert current.identity == legacy.identity
    assert legacy.inputs()['sealed_dirs'] == ['/etc/codex'] and 'sealed_files' not in legacy.inputs()
    assert current.inputs()['sealed_dirs'] == [] and current.inputs()['sealed_files'] == ['/etc/codex/config.toml']
    assert current.arguments() == legacy.arguments()


@pytest.mark.parametrize('foreign_mcp', [False, True])
def test_agent_policy_admits_only_its_mapped_bridge_before_dispatch(configured, tmp_path, foreign_mcp, monkeypatch):
    from infermatrix_copilot.providers import run_harness_session
    from infermatrix_copilot.providers.base import AgentSessionRequest
    from infermatrix_copilot.scopes import ToolScope, READ_TOOLS
    from infermatrix_copilot.tool_bridge import write_bridge_spec
    from infermatrix_copilot.providers.codex import CodexTransport
    monkeypatch.setattr(CodexTransport, 'bridge_probe', lambda self, profile, request:
        ({'schema': 'bridge-readiness/1', 'tools': sorted(request.scope.allowed_tools)}, profile.sandbox._prepared_context()))
    settings, calls, damage = configured
    settings = settings.model_copy(update={'native_session_profiles': settings.native_completion_profiles})
    source, run, knowledge = (tmp_path / name for name in ('source', 'run', 'knowledge'))
    for directory in (source, run, knowledge): directory.mkdir()
    (source / 'file.py').write_text('frozen = True\n')
    settings = settings.model_copy(update={'knowledge_dir': knowledge})
    scope = ToolScope('review', READ_TOOLS, read_only=True, root=str(source))
    bridge = write_bridge_spec(run_dir=run, step_name='review', scope=scope, repo='demo')
    request = AgentSessionRequest(system='policy', prompt='frozen', scope=scope, model='first',
        max_iters=4, timeout_s=30, run_dir=run, step_name='review', bridge_spec_path=bridge)
    damage['mcp'] = foreign_mcp
    if foreign_mcp:
        with pytest.raises(CompletionPolicyError):
            run_harness_session(transport_for_id(settings, 'codex'), request)
        assert len(calls) == 1 and '--configuration-probe' in calls[0][1]['argv']
    else:
        assert run_harness_session(transport_for_id(settings, 'codex'), request).text == '{"ok":true}'
        assert len(calls) == 2
        # Configuration staging and the model use the same session budget.
        # The configuration RPC still has its own short, payload-bound timeout.
        assert calls[0][1]['deadline'] == calls[1][1]['deadline']
        assert json.loads(calls[0][1]['input'])['timeout_s'] == 13
        assert calls[0][1]['frozen_inputs'] == calls[1][1]['frozen_inputs']
        assert any('infermatrix_copilot.tool_bridge' in arg for arg in calls[1][1]['argv'])
        assert run_harness_session(transport_for_id(settings, 'codex'), request).text == '{"ok":true}'
        assert len(calls) == 2
