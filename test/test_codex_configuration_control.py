"""Native preflight is finite control I/O, with one shared process owner."""
import json
import os
from pathlib import Path
import sys

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.process_execution import ProcessOutcome
from infermatrix_copilot.providers.codex import CodexTransport, _configuration_control, _configuration_request
from infermatrix_copilot.sandbox import BoundSandboxProfile, SandboxExecution, SandboxPending


@pytest.fixture
def native(tmp_path):
    script = tmp_path / 'native'
    script.write_text('#!' + sys.executable + '\n' + '''
import json,sys,time
from pathlib import Path
root=Path(__file__).parent
if sys.argv[1:]==['--version']:
 print('codex-cli '+('0.1' if (root/'bad-version').exists() else '0.162.1'))
 raise SystemExit(0)
(root/'started').write_text('started')
assert sys.argv[1:3]==['app-server','--strict-config']
for line in sys.stdin:
 message=json.loads(line)
 with (root/'requests').open('a') as out: out.write(json.dumps(message)+'\\n')
 if message['method']=='initialize':
  if (root/'bad-initialize').exists(): result={'error':{'message':'SECRET_DIAGNOSTIC'}}
  else: result={'result':{'userAgent':'fixture'}}
 elif message['method']=='config/read':
  config={'model':'fixture-model','model_provider':'openai','features':{'apps':False,'network_proxy':None},
          'mcp_servers':{'fixture':{'enabled':True,'command':'SECRET_COMMAND',
                                  'env':{'TOKEN':'SECRET_ENV'}}},
          'unknown_secret':'SECRET_UNKNOWN'}
  if (root/'layers.json').exists():
   layers=json.loads((root/'layers.json').read_text())
  else: layers=[{'config':config}]
  result={'result':{'config':config,'layers':layers}}
 elif message['method']=='configRequirements/read':
  requirements=json.loads((root/'requirements.json').read_text()) if (root/'requirements.json').exists() else None
  result={'result':{'requirements':requirements}}
 else:
  (root/'MODEL_STARTED').write_text(message['method'])
  raise SystemExit(99)
 print(json.dumps({'id':message['id'],**result}),flush=True)
 if (root/'exit-failure').exists() and message['method']=='config/read':
  raise SystemExit(1)
''')
    script.chmod(0o700)
    request = {'native_cli': str(script), 'expected_version': 'codex-cli 0.162.1',
               'configuration_arguments': [], 'timeout_s': 10}
    return tmp_path, request


def control(native):
    root, request = native
    return _configuration_control(request, cwd=root, env={'PATH': os.defpath, 'HOME': str(root)})


def test_finite_handshake_preserves_policy_facts_without_secrets_or_model_start(native):
    root, _ = native
    proof = control(native)
    assert proof['methods_sent'] == ['initialize', 'config/read']
    assert proof['effective']['model'] == 'fixture-model'
    assert proof['effective']['mcp_servers'] == {'fixture': {'enabled': True}}
    assert proof['effective']['features'] == {'apps': False, 'network_proxy': None}
    assert 'SECRET_' not in json.dumps(proof)
    assert proof['native_reason'] == 'absolute' and proof['native_returncode'] < 0
    assert [r['method'] for r in map(json.loads, (root/'requests').read_text().splitlines())] == ['initialize', 'config/read']
    assert not (root/'MODEL_STARTED').exists()


def test_unverified_native_version_never_initializes_or_starts_a_model(native):
    root, _ = native
    (root/'bad-version').touch()
    with pytest.raises(ValueError, match='verified Codex version'):
        control(native)
    assert not (root/'started').exists() and not (root/'MODEL_STARTED').exists()


def test_initialize_failure_cannot_send_config_or_model_requests(native):
    root, _ = native
    (root/'bad-initialize').touch()
    with pytest.raises(ValueError, match='handshake rejected'):
        control(native)
    assert [r['method'] for r in map(json.loads, (root/'requests').read_text().splitlines())] == ['initialize']
    assert not (root/'MODEL_STARTED').exists()


@pytest.mark.parametrize('change', ['command', 'method', 'version', 'flag', 'timeout'])
def test_control_request_cannot_choose_a_model_method_or_alternate_command(native, change):
    root, request = native
    request = dict(request)
    if change == 'command': request['native_cli'] = 'relative-command'
    elif change == 'method': request['method'] = 'thread/start'
    elif change == 'version': request['expected_version'] = 'codex-cli 0.1'
    elif change == 'flag': request['configuration_arguments'] = ['proxy', 'stdio']
    else: request['timeout_s'] = float('nan')
    with pytest.raises(ValueError):
        _configuration_request(request)
    assert not (root/'started').exists()


class ControlBackend(SandboxExecution):
    def __init__(self, native):
        self.native, self.calls, self.original, self.outcome = native, [], None, None

    def preflight(self, *, for_dispatch=True):
        pass

    def execute(self, command, *, cwd, env, input, frozen_inputs=None, **kwargs):
        if self.original is not None:
            raise SandboxPending('already reserved', prior_execution=True)
        self.original = (command, cwd, env, input, frozen_inputs)
        self.execution_deadline = kwargs['deadline']
        self.native_timeout = json.loads(input)['timeout_s']
        self.calls.append(command)
        request = json.loads(input)
        request['native_cli'] = self.native[1]['native_cli']
        proof = _configuration_control(request, cwd=self.native[0], env={'PATH': os.defpath, 'HOME': str(self.native[0])})
        self.outcome = ProcessOutcome(json.dumps(proof), '', 0)
        return self.outcome

    def result(self, command, *, cwd, env, input, frozen_inputs=None, **kwargs):
        if self.original != (command, cwd, env, input, frozen_inputs):
            raise SandboxPending('changed request', prior_execution=True)
        return self.outcome

    def cancel(self):
        raise AssertionError('completed control must not be cancelled')


def test_adapter_uses_profile_and_original_result_without_native_replay(native):
    backend = ControlBackend(native)
    profile = BoundSandboxProfile(backend)
    transport = CodexTransport(Settings(_env_file=None))
    frozen = {'files': {}, 'sealed_dirs': ['/etc/codex']}
    proof = transport.configuration_probe(profile, frozen_inputs=frozen)
    assert transport.get_configuration_result(profile, frozen_inputs=frozen, recover=True) == proof
    assert len(backend.calls) == 1
    assert backend.calls[0][-1] == '--configuration-probe'
    with pytest.raises(SandboxPending):
        transport.get_configuration_result(profile, configuration_arguments=('-c', 'features.apps=false'), frozen_inputs=frozen)
    with pytest.raises(SandboxPending):
        transport.configuration_probe(profile, frozen_inputs=frozen)
    assert len(backend.calls) == 1


def test_control_profile_refuses_injected_host_runner_before_execution(native):
    backend = ControlBackend(native)
    transport = CodexTransport(Settings(_env_file=None))
    transport.runner = lambda *_: pytest.fail('host runner cannot start')
    with pytest.raises(ValueError, match='host runner'):
        transport.configuration_probe(BoundSandboxProfile(backend))
    assert not backend.calls


def test_session_staging_uses_absolute_budget_but_native_rpc_keeps_short_timeout(native):
    import time
    backend = ControlBackend(native)
    profile = BoundSandboxProfile(backend)
    transport = CodexTransport(Settings(_env_file=None))
    deadline = time.monotonic() + 300
    proof = transport.configuration_probe(profile, absolute_deadline=deadline)
    assert backend.execution_deadline == deadline
    assert backend.native_timeout == 13
    assert transport.get_configuration_result(profile, recover=True) == proof
    assert len(backend.calls) == 1


def test_bridge_readiness_can_stage_inputs_within_the_same_session_deadline(native, monkeypatch):
    import time
    from types import SimpleNamespace
    backend = ControlBackend(native)
    observed = {}
    def execute(command, **kwargs):
        observed.update(kwargs)
        return ProcessOutcome(json.dumps({'schema': 'bridge-readiness/1',
            'tools': ['read_file', 'doc_read', 'doc_search']}), '', 0)
    monkeypatch.setattr(backend, 'execute', execute)
    monkeypatch.setattr(backend, 'context', lambda: {'fixture': 'same context'})
    request = SimpleNamespace(absolute_deadline=time.monotonic() + 300,
        frozen_inputs={'files': {}, 'sealed_dirs': []},
        scope=SimpleNamespace(allowed_tools=frozenset({'read_file'})))
    CodexTransport(Settings(_env_file=None)).bridge_probe(BoundSandboxProfile(backend), request)
    assert observed['deadline'] == request.absolute_deadline
    assert observed['frozen_inputs'] == request.frozen_inputs


@pytest.mark.parametrize('deadline', [float('inf'), float('nan'), True, '300'])
def test_configuration_control_rejects_invalid_deadline_before_execution(native, deadline):
    backend = ControlBackend(native)
    with pytest.raises(ValueError, match='finite absolute deadline'):
        CodexTransport(Settings(_env_file=None)).configuration_probe(
            BoundSandboxProfile(backend), absolute_deadline=deadline)
    assert not backend.calls


@pytest.mark.parametrize('high_value,disabled,expected', [(False,False,False),(True,False,True),(None,False,None),(False,True,True)])
def test_hidden_tools_use_active_high_to_low_layers_without_coercing_unknown(native, high_value, disabled, expected):
    root, request = native
    (root/'layers.json').write_text(json.dumps([
        {'config':{'tools':{'update_plan':{'enabled':high_value}}},'disabledReason':'untrusted' if disabled else None},
        {'config':{'tools':{'update_plan':{'enabled':True}}}},
    ]))
    request['include_requirements']=True
    proof=control(native)
    assert proof['effective']['tools']['update_plan'] is expected
    assert proof['methods_sent']==['initialize','config/read','configRequirements/read']
    assert proof['requirements_checked'] and proof['requirements_empty']
    assert not (root/'MODEL_STARTED').exists()


@pytest.mark.parametrize('requirements', [{}, {'features': {'hooks': True}}])
def test_some_requirements_is_not_reported_as_no_requirements(native, requirements):
    root, request = native
    (root/'requirements.json').write_text(json.dumps(requirements))
    request['include_requirements']=True
    proof=control(native)
    assert proof['requirements_checked'] and not proof['requirements_empty']
    assert not (root/'MODEL_STARTED').exists()



def test_managed_requirements_facts_are_sanitized_and_opaque_values_remain_private(native):
    root, request = native
    (root/'requirements.json').write_text(json.dumps({
        'featureRequirements': {'hooks': False}, 'allowRemoteControl': False,
        'allowedWebSearchModes': ['disabled'], 'chatgptBaseUrl': 'SECRET_ACCOUNT_URL',
        'hooks': {'command': 'SECRET_HOOK'}, 'additionalDeveloperInstructions': 'SECRET_INSTRUCTIONS'}))
    request['include_requirements'] = True
    proof = control(native)
    assert proof['requirements']['values'] == {
        'featureRequirements': {'hooks': False}, 'allowRemoteControl': False, 'allowedWebSearchModes': ['disabled']}
    assert 'chatgptBaseUrl' in proof['requirements']['active_fields']
    assert 'SECRET_' not in json.dumps(proof)
    assert not (root/'MODEL_STARTED').exists()
