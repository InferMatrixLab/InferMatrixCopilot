"""Queued work and its child retain the trusted native execution identity."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from infermatrix_copilot.app.completion_execution import completion_settings_for_run
from infermatrix_copilot.app.run_service import RunService
from infermatrix_copilot.providers.completion_profiles import CompletionPolicyError
from test_completion_profile_configuration import configured, reserve  # noqa: F401


@pytest.mark.parametrize('version', [1, 2, 3])
def test_old_pin_bytes_and_uncaptured_backend_semantics_are_preserved(configured, version):
    settings, calls = configured
    run, _ = reserve(settings)
    value = {'schema': f'run-completion/{version}',
             'profiles': settings.native_completion_profiles, 'trace_store_root': settings.trace_store_root}
    if version >= 2:
        value['session_profiles'] = settings.native_completion_profiles
    if version == 3:
        value['strict_backend_effort'] = 'high'
    raw = (json.dumps(value, indent=2) + '\n').encode()
    (run / 'native-completion.json').write_bytes(raw)
    status_path = run / 'run_status.json'
    status = json.loads(status_path.read_bytes())
    status['native_completion_sha256'] = hashlib.sha256(raw).hexdigest()
    status_path.write_text(json.dumps(status))
    host = settings.model_copy(update={'strict_backend_model': 'host-model', 'strict_backend_effort': 'low'})
    restored = completion_settings_for_run(host, run)
    assert restored.strict_backend_model == 'host-model'
    assert restored.strict_backend_effort == ('high' if version == 3 else '')
    assert (run / 'native-completion.json').read_bytes() == raw
    assert not calls


@pytest.mark.parametrize('field,value', [
    ('strict_backend', 'unknown-provider'), ('strict_backend_model', 42),
    ('strict_backend_timeout_s', True), ('strict_backend_timeout_s', float('nan')),
    ('strict_backend_timeout_s', 10 ** 1000), ('unexpected', 'untrusted'),
], ids=['unknown-provider', 'numeric-model', 'boolean-deadline', 'nan-deadline', 'huge-deadline', 'extra-field'])
def test_invalid_bound_native_identity_refuses_without_dispatch(configured, field, value):
    settings, calls = configured
    run, _ = reserve(settings)
    pin_path = run / 'native-completion.json'
    pin = json.loads(pin_path.read_bytes())
    pin['backend'][field] = value
    raw = json.dumps(pin).encode()
    pin_path.write_bytes(raw)
    status_path = run / 'run_status.json'
    status = json.loads(status_path.read_bytes())
    status['native_completion_sha256'] = hashlib.sha256(raw).hexdigest()
    status_path.write_text(json.dumps(status))
    with pytest.raises(CompletionPolicyError, match='cannot restore original completion execution'):
        completion_settings_for_run(settings, run)
    assert not calls


def test_reserved_native_identity_survives_host_configuration_changes(configured):
    settings, calls = configured
    settings = settings.model_copy(update={
        'strict_backend_cli': '/original/codex', 'strict_backend_timeout_s': 45,
        'strict_backend_effort': 'high'})
    run, _ = reserve(settings)
    changed = settings.model_copy(update={
        'strict_backend': 'cursor', 'strict_backend_model': 'another-model',
        'strict_backend_cli': '/replacement/cursor', 'strict_backend_timeout_s': 99,
        'strict_backend_effort': 'low'})
    restored = completion_settings_for_run(changed, run)
    for field in ('strict_backend', 'strict_backend_model', 'strict_backend_cli',
                  'strict_backend_timeout_s', 'strict_backend_effort'):
        assert getattr(restored, field) == getattr(settings, field), field
    assert not calls


def test_reserved_child_receives_effective_backend_model_cli_and_deadline(configured, monkeypatch):
    settings, calls = configured
    settings = settings.model_copy(update={
        'strict_backend_cli': '/original/codex', 'strict_backend_timeout_s': 45})
    run, _ = reserve(settings)
    service = RunService.__new__(RunService)
    service.worker_prefix = (sys.executable,)
    service.settings, service.run_root = settings, settings.run_root
    service._reap = lambda: None
    captured = []

    class Child:
        pid, returncode = 424243, 0

        def wait(self):
            return 0

    def popen(command, **kwargs):
        captured.append(kwargs['env'])
        return Child()

    monkeypatch.setattr('infermatrix_copilot.app.run_service.subprocess.Popen', popen)
    service._launch(run.name)
    env, = captured
    assert env['STRICT_BACKEND_MODEL'] == 'fixture'
    assert env['STRICT_BACKEND_CLI'] == '/original/codex'
    assert env['STRICT_BACKEND_TIMEOUT_S'] == '45'
    assert json.loads(env['NATIVE_COMPLETION_PROFILES']) == settings.native_completion_profiles
    assert not calls


def test_real_child_reads_original_backend_settings_after_host_drift(configured, monkeypatch):
    import infermatrix_copilot

    settings, calls = configured
    settings = settings.model_copy(update={
        'strict_backend_cli': '/original/codex', 'strict_backend_timeout_s': 45,
        'strict_backend_effort': 'high'})
    run, _ = reserve(settings)
    service = RunService.__new__(RunService)
    service.worker_prefix = (sys.executable,)
    service.settings = settings.model_copy(update={
        'strict_backend': 'cursor', 'strict_backend_model': 'host-model',
        'strict_backend_cli': '/replacement/cursor', 'strict_backend_timeout_s': 99,
        'strict_backend_effort': 'low'})
    service.run_root, service._reap = settings.run_root, lambda: None
    output = run / 'child-settings.json'
    product_root = Path(infermatrix_copilot.__file__).resolve().parent.parent
    # Source tests explicitly import the tested tree; installed checks use -I
    # and no product-path injection. The child executes only a settings probe.
    prefix = (f'import sys; sys.path.insert(0, {str(product_root)!r})\n'
              if 'site-packages' not in product_root.parts else '')
    program = prefix + '''
import json,os
from pathlib import Path
from infermatrix_copilot.config import Settings
from infermatrix_copilot.providers import llm_for
settings=Settings(_env_file=None)
client=llm_for(settings)
observed={key:getattr(settings,key) for key in (
    'strict_backend','strict_backend_model','strict_backend_cli',
    'strict_backend_timeout_s','strict_backend_effort')}
observed['served_model']=client._harness_model
Path(os.environ['RUN_ROOT']).joinpath(%r,'child-settings.json').write_text(json.dumps(observed))
''' % run.name
    original_popen = subprocess.Popen

    def launch_probe(command, **kwargs):
        assert command[:3] == [sys.executable, '-m', 'infermatrix_copilot']
        return original_popen([sys.executable, '-I', '-B', '-c', program], **kwargs)

    monkeypatch.setattr('infermatrix_copilot.app.run_service.subprocess.Popen', launch_probe)
    service._launch(run.name)
    observed = json.loads(output.read_bytes())
    assert observed == {'strict_backend': 'codex', 'strict_backend_model': 'fixture',
                        'strict_backend_cli': '/original/codex', 'strict_backend_timeout_s': 45,
                        'strict_backend_effort': 'high', 'served_model': 'fixture'}
    assert not calls
