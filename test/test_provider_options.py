"""Trusted profile options survive ambient drift through native dispatch."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from infermatrix_copilot.app.completion_execution import completion_settings_for_run
from infermatrix_copilot.app.run_service import RunService
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
from infermatrix_copilot.providers.completion_profiles import (
    CompletionPolicyError, completion_profile_config, normalize_completion_profiles)
from infermatrix_copilot.providers.provider_options import bind_transport
from infermatrix_copilot.providers.registry import transport_for_id
from infermatrix_copilot.providers.zcode import ZCodeTransport
from infermatrix_copilot.trace_store import TraceStore
from test_zcode_sandbox_completion import sandbox  # noqa: F401
from test_completion_profile_configuration import configured, reserve  # noqa: F401


OPTIONS = {'zcode_provider_id': 'account:frozen-coding-plan', 'zcode_reasoning_level': 'high',
           'model_mismatch_policy': 'fail'}


def with_options(settings, options=OPTIONS):
    backend = settings.native_completion_profiles['zcode']
    return settings.model_copy(update={'zcode_provider_id': 'account:ambient', 'zcode_reasoning_level': 'low',
        'model_mismatch_policy': 'warn', 'native_completion_profiles': normalize_completion_profiles({'zcode': {
            'kind': 'sandbox', 'backend': backend, 'location': {'kind': 'local'}, 'provider_options': options}})})


def test_gateway_worker_and_actual_native_model_config_use_frozen_options(sandbox, monkeypatch):
    settings, calls = sandbox
    settings = with_options(settings)
    seen = []
    original = ZCodeTransport._write_model_config
    def capture(self, session, model):
        path = original(self, session, model)
        seen.append(json.loads(path.read_text())['config']['defaultModelSelection'])
        return path
    monkeypatch.setattr(ZCodeTransport, '_write_model_config', capture)
    gateway = ModelGateway(settings)
    assert gateway.call_json(ModelRole('generator', 'zcode', 'GLM-5.3'),
                             system='frozen policy', prompt='frozen evidence').data['spooled']
    selected, = seen
    assert selected['providerId'] == OPTIONS['zcode_provider_id']
    assert selected['options']['reasoningLevel'] == OPTIONS['zcode_reasoning_level']
    assert settings.zcode_provider_id == 'account:ambient' and settings.zcode_reasoning_level == 'low'
    request = json.loads(calls[0][2]['input'])
    assert all(request['settings'][key] == value for key, value in OPTIONS.items())
    store = TraceStore(settings.trace_store_root)
    row, = [row for row, _ in store.iter_records(kind='model_call')]
    store.validate_native_attempt(row)
    descriptor = json.loads((store.root / 'attempts' / row['result']['native_attempt_id'] / 'execution-profile.json').read_bytes())
    assert descriptor['config']['provider_options'] == OPTIONS


def test_per_attempt_binding_does_not_reuse_or_mutate_cached_host_provider(sandbox):
    settings, _ = sandbox
    settings = with_options(settings)
    original = ZCodeTransport(settings)
    assert original._selected_provider_id == 'account:ambient'
    bound = bind_transport(original, settings, completion_profile_config(settings, 'zcode'))
    assert bound._selected_provider_id == OPTIONS['zcode_provider_id']
    assert original._selected_provider_id == 'account:ambient'
    assert transport_for_id(settings, 'zcode').settings.zcode_reasoning_level == 'high'


def test_original_run_pin_retains_options_without_a_new_state_protocol(sandbox):
    settings, calls = sandbox
    settings = with_options(settings)
    run, _ = reserve(settings)
    raw = (run / 'native-completion.json').read_bytes()
    assert json.loads(raw)['schema'] == 'run-completion/4'
    changed = settings.model_copy(deep=True)
    changed.native_completion_profiles['zcode']['provider_options']['zcode_reasoning_level'] = 'max'
    restored = completion_settings_for_run(changed, run, resuming=True)
    assert restored.native_completion_profiles['zcode']['provider_options'] == OPTIONS
    assert transport_for_id(restored, 'zcode').settings.zcode_reasoning_level == 'high'
    assert (run / 'native-completion.json').read_bytes() == raw and not calls


def test_real_durable_child_uses_original_options_after_environment_and_host_drift(sandbox, monkeypatch):
    import infermatrix_copilot
    settings, calls = sandbox
    settings = with_options(settings)
    run, _ = reserve(settings)
    host = settings.model_copy(deep=True)
    host.native_completion_profiles['zcode']['provider_options']['zcode_reasoning_level'] = 'max'
    monkeypatch.setenv('ZCODE_PROVIDER_ID', 'account:ambient-child')
    monkeypatch.setenv('ZCODE_REASONING_LEVEL', 'low')
    monkeypatch.setenv('MODEL_MISMATCH_POLICY', 'warn')
    service = RunService.__new__(RunService)
    service.worker_prefix = (sys.executable,)
    service.settings, service.run_root, service._reap = host, settings.run_root, lambda: None
    product_root = Path(infermatrix_copilot.__file__).resolve().parent.parent
    prefix = (f'import sys; sys.path.insert(0, {str(product_root)!r})\n'
              if 'site-packages' not in product_root.parts else '')
    program = prefix + '''
import json,os
from pathlib import Path
from infermatrix_copilot.config import Settings
from infermatrix_copilot.providers.registry import transport_for_id
transport=transport_for_id(Settings(_env_file=None),'zcode')
observed={'zcode_provider_id':transport._selected_provider_id,
          'zcode_reasoning_level':transport.settings.zcode_reasoning_level,
          'model_mismatch_policy':transport.settings.model_mismatch_policy}
Path(os.environ['RUN_ROOT']).joinpath(%r,'child-provider-options.json').write_text(json.dumps(observed))
''' % run.name
    original_popen = subprocess.Popen
    def launch_probe(command, **kwargs):
        return original_popen([sys.executable, '-I', '-B', '-c', program], **kwargs)
    monkeypatch.setattr('infermatrix_copilot.app.run_service.subprocess.Popen', launch_probe)
    service._launch(run.name)
    assert json.loads((run / 'child-provider-options.json').read_bytes()) == OPTIONS
    assert not calls


@pytest.mark.parametrize('options', [{'api_key': 'PRIVATE-TEST-SECRET'}, {'zcode_reasoning_level': 'invalid'},
    {'zcode_provider_id': 12}, {'model_mismatch_policy': 'ignore'}])
def test_unsupported_or_invalid_options_refuse_without_dispatch_or_secret_diagnostics(sandbox, options):
    settings, calls = sandbox
    with pytest.raises(CompletionPolicyError) as error:
        with_options(settings, options)
    assert 'PRIVATE-TEST-SECRET' not in str(error.value) and not calls


def test_absent_options_keep_legacy_profile_bytes_and_registry_defaults(sandbox):
    settings, _ = sandbox
    profiles = settings.native_completion_profiles
    before = json.dumps(profiles, sort_keys=True)
    assert json.dumps(normalize_completion_profiles(profiles), sort_keys=True) == before
    assert 'provider_options' not in next(iter(profiles.values()))
    assert transport_for_id(settings, 'zcode').settings is settings
    original = ZCodeTransport(settings.model_copy(update={'strict_backend_timeout_s': 91}))
    assert bind_transport(original, settings, completion_profile_config(settings, 'zcode')).settings is original.settings
