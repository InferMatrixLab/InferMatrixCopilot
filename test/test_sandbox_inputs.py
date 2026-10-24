"""Frozen inputs share local/remote identity and never rearm an original run."""
from copy import deepcopy
import hashlib
import json
import os
import time

import pytest

from infermatrix_copilot import container_worker
from infermatrix_copilot.providers import container_execution as containers
from infermatrix_copilot.providers.container_protocol import receipt_line
from infermatrix_copilot.process_execution import ProcessOutcome
from infermatrix_copilot.sandbox.inputs import normalize_inputs, stage_inputs, verify_inputs
from test_container_execution import execution, IMAGE  # noqa: F401
from test_sandbox_execution import remote  # noqa: F401

INPUTS = {'files': {'/tmp/inputs/model.json': '{"models":[]}'}, 'sealed_dirs': ['/etc/codex']}


@pytest.mark.parametrize('path', ['/tmp/inputs/../escape', '/tmp/inputs/a//b', '/tmp/inputs/',
                                 '/tmp/inputs/a\nmount', '/etc/codex/config', '/tmp/inputs/a,b'])
def test_invalid_input_paths_fail_before_publication(tmp_path, path):
    with pytest.raises(ValueError):
        stage_inputs(tmp_path, {'files': {path: 'data'}, 'sealed_dirs': []})
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('change', ['overlap', 'capacity', 'sealed_parent', 'sealed_repeat', 'surrogate'])
def test_input_limits_and_scope_are_canonical(change):
    value = deepcopy(INPUTS)
    if change == 'overlap': value['files']['/tmp/inputs/model.json/child'] = 'data'
    if change == 'capacity': value['files']['/tmp/inputs/model.json'] = 'x' * (4 * 1024 * 1024 + 1)
    if change == 'sealed_parent': value['sealed_dirs'] = ['/etc']
    if change == 'sealed_repeat': value['sealed_dirs'] *= 2
    if change == 'surrogate': value['files']['/tmp/inputs/model.json'] = '\udfff'
    with pytest.raises(ValueError):
        normalize_inputs(value)


def test_staging_keeps_original_bytes_and_handles_private_umask(tmp_path):
    previous = os.umask(0o077)
    try:
        mounts = stage_inputs(tmp_path, INPUTS)
    finally:
        os.umask(previous)
    assert [(p.name, target) for p, target in mounts] == [('input-files', '/tmp/inputs'), ('empty-config', '/etc/codex')]
    assert (tmp_path / 'input-files/model.json').read_text() == INPUTS['files']['/tmp/inputs/model.json']
    assert (tmp_path / 'input-files').stat().st_mode & 0o777 == 0o755
    assert (tmp_path / 'input-files/model.json').stat().st_mode & 0o777 == 0o444
    assert stage_inputs(tmp_path, deepcopy(INPUTS)) == mounts
    changed = deepcopy(INPUTS)
    changed['files']['/tmp/inputs/model.json'] = 'changed'
    with pytest.raises(FileExistsError):
        stage_inputs(tmp_path, changed)
    assert (tmp_path / 'input-files/model.json').read_text() == INPUTS['files']['/tmp/inputs/model.json']


def test_staging_refuses_symlink_or_nonempty_sealed_source(tmp_path):
    elsewhere = tmp_path / 'outside'
    elsewhere.mkdir()
    (tmp_path / 'input-files').symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError):
        stage_inputs(tmp_path, INPUTS)
    assert not list(elsewhere.iterdir())


def test_worker_verifies_frozen_bytes_and_sealed_dirs_before_native_dispatch(tmp_path, monkeypatch):
    from infermatrix_copilot.sandbox import inputs
    files, sealed = tmp_path / 'inputs', tmp_path / 'etc/codex'
    files.mkdir(); sealed.mkdir(parents=True)
    (files / 'model.json').write_text(INPUTS['files']['/tmp/inputs/model.json'])
    def projected(name):
        return files / 'model.json' if name.startswith('/tmp/inputs/') else sealed
    monkeypatch.setattr(inputs, 'Path', projected)
    verify_inputs(INPUTS)
    (files / 'model.json').write_text('changed')
    with pytest.raises(ValueError, match='differs'):
        verify_inputs(INPUTS)
    (files / 'model.json').write_text(INPUTS['files']['/tmp/inputs/model.json'])
    (sealed / 'managed_config.toml').write_text('enabled=true')
    with pytest.raises(ValueError, match='not sealed'):
        verify_inputs(INPUTS)


def test_worker_invalid_projection_cannot_launch_native_command(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(container_worker, 'Path', lambda name: tmp_path)
    monkeypatch.setattr(container_worker, 'run_process', lambda *a, **k: calls.append(True))
    monkeypatch.setattr(container_worker, 'verify_inputs', lambda value: (_ for _ in ()).throw(ValueError('changed projection')))
    request = {'version': 5, 'argv': ['/native'], 'input': 'frozen prompt', 'proxy': False,
               'deadline': time.monotonic() + 10, 'credentials_sha256': None, 'frozen_inputs': INPUTS}
    with pytest.raises(ValueError, match='changed projection'):
        container_worker.execute(request)
    assert not calls


def test_local_mounts_and_recovery_bind_exact_frozen_input_bytes(execution, monkeypatch):
    runtime, daemon = execution
    controls, requests = [], []
    control = daemon.control
    def observe(*args):
        controls.append(args)
        return control(*args)
    monkeypatch.setattr(runtime, '_control', observe)
    def start(command, *, input, cleanup, **kwargs):
        request = json.loads(input)
        requests.append(request)
        assert request['version'] == 5 and request['credentials_sha256'] is None
        assert request['frozen_inputs'] == INPUTS
        daemon.container['State'] = {'Running': False, 'Status': 'exited', 'ExitCode': 0}
        cleanup()
        stdout = 'native result\n'
        return ProcessOutcome(stdout, receipt_line(request, {'stdout': hashlib.sha256(stdout.encode()),
                                                         'stderr': hashlib.sha256(b'')}, 0, None), 0)
    monkeypatch.setattr(containers, 'run_process', start)
    result = runtime.run_command(['/native'], image=IMAGE, cwd='/tmp/work', env={}, input='prompt',
                                 deadline=time.monotonic() + 10, frozen_inputs=INPUTS)
    assert result == ProcessOutcome('native result\n', '', 0)
    created = next(value for value in controls if value[0] == 'create')
    mounted = [created[index + 1] for index, arg in enumerate(created) if arg == '--mount']
    assert any('dst=/tmp/inputs,readonly' in value for value in mounted)
    assert any('dst=/etc/codex,readonly' in value for value in mounted)
    monkeypatch.setattr(runtime, '_control', lambda *_: pytest.fail('terminal recovery must not access daemon'))
    assert runtime.get_command_result(['/native'], image=IMAGE, cwd='/tmp/work', env={}, input='prompt', frozen_inputs=INPUTS) == result
    changed = deepcopy(INPUTS)
    changed['files']['/tmp/inputs/model.json'] = 'changed'
    for value in (changed, None):
        with pytest.raises(containers.ContainerPending):
            runtime.get_command_result(['/native'], image=IMAGE, cwd='/tmp/work', env={}, input='prompt', frozen_inputs=value)
    assert len(requests) == 1


def test_remote_input_binding_rejects_changed_inputs_without_second_dispatch(remote, monkeypatch):
    from test_sandbox_execution import Backend
    runtime, calls, controls, state = remote
    received = []
    execute = Backend.execute
    def observe(self, *args, **kwargs):
        received.append(deepcopy(kwargs.get('frozen_inputs')))
        return execute(self, *args, **kwargs)
    monkeypatch.setattr(Backend, 'execute', observe)
    result = runtime.execute(['/native'], cwd='/tmp/work', env={}, input='prompt',
                             deadline=time.monotonic() + 5, frozen_inputs=INPUTS)
    binding = json.loads((runtime.root / 'remote-binding.json').read_text())
    assert binding['agent_protocol'] == 3 and binding['request']['frozen_inputs'] == INPUTS
    assert runtime.result(['/native'], cwd='/tmp/work', env={}, input='prompt', frozen_inputs=INPUTS) == result
    changed = deepcopy(INPUTS)
    changed['sealed_dirs'] = []
    with pytest.raises(containers.ContainerPending):
        runtime.result(['/native'], cwd='/tmp/work', env={}, input='prompt', frozen_inputs=changed)
    assert len([value for value in calls if value[0] == 'execute']) == 1
    assert controls == ['preflight', 'execute']
    assert received == [INPUTS]


def test_legacy_remote_preflight_refuses_new_inputs_before_reservation(remote, monkeypatch):
    runtime, calls, controls, state = remote
    monkeypatch.setattr(runtime, '_invoke', lambda operation, **kwargs: {'release': 'a' * 64, 'protocol': 2})
    with pytest.raises(ValueError, match='does not support frozen'):
        runtime.execute(['/native'], cwd='/tmp/work', env={}, deadline=time.monotonic() + 5, frozen_inputs=INPUTS)
    assert not (runtime.root / 'remote-binding.json').exists() and calls == []


FILE_INPUTS = {'files': {'/tmp/inputs/model.json': '{}'}, 'sealed_dirs': [],
               'sealed_files': ['/etc/example/config.toml']}


def test_file_seal_leaves_neighboring_managed_requirements_visible(tmp_path, monkeypatch):
    from infermatrix_copilot.sandbox import inputs
    mounts = stage_inputs(tmp_path, FILE_INPUTS)
    assert [(path.name, target) for path, target in mounts] == [
        ('input-files', '/tmp/inputs'), ('empty-config-file', '/etc/example/config.toml')]
    blank = tmp_path/'empty-config-file'
    assert blank.read_bytes() == b'' and blank.stat().st_mode & 0o777 == 0o444
    requirements = tmp_path/'requirements.toml'
    requirements.write_text('allow_remote_control = false')
    assert stage_inputs(tmp_path, FILE_INPUTS) == mounts
    monkeypatch.setattr(inputs, 'Path', lambda name: (
        blank if name == '/etc/example/config.toml' else tmp_path/'input-files/model.json'))
    verify_inputs(FILE_INPUTS)
    assert requirements.read_text() == 'allow_remote_control = false'
    blank.chmod(0o600);blank.write_text('changed')
    with pytest.raises(ValueError, match='not sealed'):
        verify_inputs(FILE_INPUTS)


@pytest.mark.parametrize('files,dirs', [
    (['/etc/config.toml'], []), (['/etc/example/a/b'], []),
    (['/etc/example/../escape'], []), (['/etc/example/config,evil'], []),
    (['/etc/example/config.toml']*2, []), (['/etc/example/config.toml'], ['/etc/example'])])
def test_invalid_file_seals_fail_before_staging(tmp_path, files, dirs):
    with pytest.raises(ValueError):
        stage_inputs(tmp_path, {'files': {}, 'sealed_dirs': dirs, 'sealed_files': files})
    assert not (tmp_path/'frozen-inputs.json').exists()


def test_file_seal_refuses_symlink_in_host_staging(tmp_path):
    (tmp_path/'empty-config-file').symlink_to(tmp_path/'elsewhere')
    with pytest.raises(ValueError, match='symlink'):
        stage_inputs(tmp_path, FILE_INPUTS)


def test_old_worker_protocol_cannot_interpret_new_file_projection():
    from infermatrix_copilot.container_worker import validate_request
    request = {'version': 5, 'argv': ['/native'], 'input': None, 'proxy': False,
               'deadline': time.monotonic()+10, 'credentials_sha256': None, 'frozen_inputs': FILE_INPUTS}
    with pytest.raises(ValueError, match='protocol'):
        validate_request(request)
    assert validate_request({**request, 'version': 6})['version'] == 6


def test_file_projection_requires_a_valid_terminal_worker_receipt():
    outcome = ProcessOutcome('native result', '', 0)
    binding = {'worker_protocol': 6, 'input_sha256': 'a'*64}
    with pytest.raises(containers.ContainerPending, match='reconciliation'):
        containers.ContainerExecution._decode_worker_result(binding, outcome)


def test_old_remote_agent_refuses_file_projection_before_reservation(remote, monkeypatch):
    runtime, calls, controls, state = remote
    monkeypatch.setattr(runtime, '_invoke', lambda operation, **kwargs: {
        'release': 'a'*64, 'protocol': 2, 'frozen_inputs_protocol': 3})
    with pytest.raises(ValueError, match='does not support frozen'):
        runtime.execute(['/native'], cwd='/tmp/work', env={}, deadline=time.monotonic()+5,
                        frozen_inputs=FILE_INPUTS)
    assert not (runtime.root/'remote-binding.json').exists() and calls == []


def test_remote_file_projection_uses_same_inputs_and_read_only_recovery(remote, monkeypatch):
    from test_sandbox_execution import Backend
    runtime, calls, controls, state = remote
    observed = []
    execute = Backend.execute
    def run(self, *args, **kwargs):
        observed.append(deepcopy(kwargs['frozen_inputs']))
        return execute(self, *args, **kwargs)
    monkeypatch.setattr(Backend, 'execute', run)
    result = runtime.execute(['/native'], cwd='/tmp/work', env={}, input='prompt',
        deadline=time.monotonic()+5, frozen_inputs=FILE_INPUTS)
    binding = json.loads((runtime.root/'remote-binding.json').read_text())
    assert binding['agent_protocol'] == 4
    assert runtime.result(['/native'], cwd='/tmp/work', env={}, input='prompt', frozen_inputs=FILE_INPUTS) == result
    changed = {**FILE_INPUTS, 'sealed_files': ['/etc/example/another.toml']}
    with pytest.raises(containers.ContainerPending):
        runtime.result(['/native'], cwd='/tmp/work', env={}, input='prompt', frozen_inputs=changed)
    assert observed == [FILE_INPUTS] and controls == ['preflight', 'execute']
