"""Portable byte snapshots and execution-end idle bounds share original identity."""
import base64
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
import time

import pytest

from infermatrix_copilot.sandbox.inputs import (input_protocol, normalize_inputs, project_inputs,
    snapshot_directory, stage_inputs, verify_inputs)
from infermatrix_copilot.container_worker import validate_request
from infermatrix_copilot.providers.container_protocol import decode_receipt
from infermatrix_copilot.process_execution import ProcessOutcome
from test_sandbox_execution import remote  # noqa: F401
from test_container_execution import execution, IMAGE  # noqa: F401


def test_binary_source_git_bytes_executable_and_relative_links_are_exact(tmp_path, monkeypatch):
    source = tmp_path / 'source';source.mkdir()
    (source / '.git').mkdir();(source / '.git/object').write_bytes(b'\0\xff\x01git')
    (source / 'exec').write_bytes(b'#!/bin/sh\n');(source / 'exec').chmod(0o755)
    (source / 'alias').symlink_to('exec')
    value = snapshot_directory(source)
    assert input_protocol(value) == 5
    mounts = stage_inputs(tmp_path / 'execution', value)
    staged, _ = mounts[0]
    assert (staged / 'repository/.git/object').read_bytes() == b'\0\xff\x01git'
    assert (staged / 'repository/exec').stat().st_mode & 0o777 == 0o555
    assert (staged / 'repository/alias').readlink() == Path('exec')
    proof = project_inputs(value)
    assert 'base64' not in json.dumps(proof)
    from infermatrix_copilot.sandbox import inputs
    monkeypatch.setattr(inputs, 'Path', lambda name: staged / Path(name).relative_to('/tmp/inputs'))
    verify_inputs(proof)
    file = staged / 'repository/.git/object';file.chmod(0o644);file.write_bytes(b'drift')
    with pytest.raises(ValueError, match='differs'):
        verify_inputs(proof)


@pytest.mark.parametrize('target', ['/outside', '../../outside'])
def test_snapshot_containment_rejects_external_links_before_staging(tmp_path, target):
    source = tmp_path / 'source';source.mkdir();(source / 'link').symlink_to(target)
    with pytest.raises(ValueError):
        snapshot_directory(source)
    assert not (tmp_path / 'execution').exists()


def test_worker_enforces_idle_without_controller_and_retains_its_receipt():
    request = {'version': 7, 'argv': [sys.executable, '-c',
        'import time; print("original",flush=True); time.sleep(30)'], 'input': None, 'proxy': False,
        'deadline': time.monotonic() + 20, 'credentials_sha256': None, 'frozen_inputs': None,
        'controls': {'idle_timeout_s': .3, 'termination_grace_s': 0}}
    result = subprocess.run([sys.executable, '-m', 'infermatrix_copilot.container_worker'],
        input=json.dumps(request), capture_output=True, text=True, env=os.environ.copy(), timeout=8)
    outcome = decode_receipt(ProcessOutcome(result.stdout, result.stderr, result.returncode),
                             hashlib.sha256(json.dumps(request).encode()).hexdigest(), required=True)
    assert outcome.stdout == 'original\n' and outcome.reason == 'idle' and outcome.returncode == 124


@pytest.mark.parametrize('controls', [True, {}, {'idle_timeout_s': False, 'termination_grace_s': 0},
                                    {'idle_timeout_s': 1, 'termination_grace_s': -1}])
def test_invalid_worker_controls_cannot_dispatch(controls):
    with pytest.raises(ValueError):
        validate_request({'version': 7, 'argv': ['/native'], 'input': None, 'proxy': False,
            'deadline': time.monotonic() + 20, 'credentials_sha256': None, 'frozen_inputs': None,
            'controls': controls})


def test_remote_controls_and_byte_inputs_use_same_backend_and_recovery(remote):
    executor, calls, operations, _ = remote
    frozen = {'files': {'/tmp/inputs/repository/object': {'base64': base64.b64encode(b'\xffgit').decode(),
                                                       'executable': False}}, 'sealed_dirs': []}
    options = dict(cwd='/tmp/work', env={}, input='frozen', frozen_inputs=frozen,
                   idle_timeout_s=2, termination_grace_s=5)
    outcome = executor.execute(['/native'], deadline=time.monotonic() + 30, **options)
    assert executor.result(['/native'], **options) == outcome
    binding = json.loads((executor.root / 'remote-binding.json').read_text())
    assert binding['agent_protocol'] == 5 and binding['request']['controls']['idle_timeout_s'] == 2
    assert len([call for call in calls if call[0] == 'execute']) == 1
    with pytest.raises(Exception, match='differs'):
        executor.result(['/native'], **{**options, 'idle_timeout_s': 3})


def test_empty_directory_snapshot_projection_and_worker_verification(tmp_path, monkeypatch):
    from infermatrix_copilot.sandbox import inputs
    source = tmp_path / 'source'
    (source / 'empty/nested').mkdir(parents=True)
    (source / 'excluded').mkdir()
    value = snapshot_directory(source, exclude=('excluded',))
    assert value == {'files': {}, 'sealed_dirs': [],
                     'directories': ['/tmp/inputs/repository/empty/nested']}
    assert input_protocol(value) == 6
    staged = stage_inputs(tmp_path / 'execution', value)[0][0]
    assert (staged / 'repository/empty/nested').is_dir()
    assert not (staged / 'repository/excluded').exists()
    monkeypatch.setattr(inputs, 'Path', lambda name: staged / Path(name).relative_to('/tmp/inputs'))
    verify_inputs(project_inputs(value))
    (staged / 'repository/empty/nested').rmdir()
    with pytest.raises(ValueError, match='directory differs'):
        verify_inputs(project_inputs(value))


def test_empty_source_root_survives_snapshot_and_staging(tmp_path):
    source = tmp_path / 'source';source.mkdir()
    value = snapshot_directory(source)
    assert value['directories'] == ['/tmp/inputs/repository']
    staged = stage_inputs(tmp_path / 'execution', value)[0][0]
    assert (staged / 'repository').is_dir()


@pytest.mark.parametrize('name', ['/tmp/inputs/../escape', '/tmp/inputs/a//b',
    '/tmp/inputs/a,b', '/tmp/inputs/', '/tmp/inputs/file', '/tmp/inputs/file/child'])
def test_directory_paths_cannot_escape_or_shadow_files(tmp_path, name):
    with pytest.raises(ValueError):
        stage_inputs(tmp_path, {'files': {'/tmp/inputs/file': 'data'}, 'sealed_dirs': [],
                                'directories': [name]})
    assert not (tmp_path / 'frozen-inputs.json').exists()


def test_packed_git_refs_survive_canonical_source_snapshot(tmp_path):
    from infermatrix_copilot.providers.session_inputs import snapshot_source
    source = tmp_path / 'source';source.mkdir()
    def git(*args, cwd=source):
        return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()
    git('init', '-q');git('config', 'user.name', 'fixture')
    git('config', 'user.email', 'fixture@example.test')
    (source / 'file').write_text('frozen source\n')
    git('add', '.');git('commit', '-qm', 'fixture');git('pack-refs', '--all', '--prune')
    assert not any((source / '.git/refs/heads').iterdir())
    head = git('rev-parse', 'HEAD')
    value = snapshot_source(source)
    assert input_protocol(value) == 6
    staged = stage_inputs(tmp_path / 'execution', value)[0][0] / 'repository'
    assert git('rev-parse', 'HEAD', cwd=staged) == head
    assert git('show', 'HEAD:file', cwd=staged) == 'frozen source'
    assert not (staged / '.git/hooks').exists()
    assert 'user' not in (staged / '.git/config').read_text()


def test_directory_inputs_require_new_worker_and_bound_receipt():
    request = {'version': 8, 'argv': ['/native'], 'input': None, 'proxy': False,
        'deadline': time.monotonic() + 20, 'credentials_sha256': None,
        'frozen_inputs': {'files': {}, 'sealed_dirs': [], 'directories': ['/tmp/inputs/repository']},
        'controls': None}
    assert validate_request(request) == request
    with pytest.raises(ValueError, match='protocol 8'):
        validate_request({**request, 'version': 7})
    from infermatrix_copilot.providers.container_execution import ContainerExecution, ContainerPending
    with pytest.raises(ContainerPending, match='reconciliation'):
        ContainerExecution._decode_worker_result({'worker_protocol': 8, 'input_sha256': 'a'*64},
                                                ProcessOutcome('partial', '', 0))


def test_old_image_refuses_directories_before_reservation_or_native_dispatch(execution, monkeypatch):
    runtime, daemon = execution
    control = daemon.control
    monkeypatch.setattr(runtime, '_control', lambda *args: '{}' if args[:2] == ('image', 'inspect') else control(*args))
    with pytest.raises(ValueError, match='does not support frozen directory'):
        runtime.run_command(['/native'], image=IMAGE, cwd='/tmp/work', env={},
            deadline=time.monotonic()+10, frozen_inputs={'files': {}, 'sealed_dirs': [],
                                                        'directories': ['/tmp/inputs/repository']})
    assert not (runtime.root / 'binding.json').exists()
    assert daemon.container is None


def test_remote_old_image_refuses_directories_before_reservation(remote):
    runtime, calls, operations, _ = remote
    with pytest.raises(ValueError, match='does not support frozen'):
        runtime.execute(['/native'], cwd='/tmp/work', env={}, deadline=time.monotonic()+10,
            frozen_inputs={'files': {}, 'sealed_dirs': [], 'directories': ['/tmp/inputs/repository']})
    assert operations == ['preflight'] and calls == []
    assert not (runtime.root / 'remote-binding.json').exists()


def test_remote_directory_inputs_and_controls_recover_without_redispatch(remote, monkeypatch):
    from test_sandbox_execution import Backend
    monkeypatch.setattr(Backend, 'supported_input_protocol', lambda self: 6)
    runtime, calls, operations, _ = remote
    options = dict(cwd='/tmp/work', env={}, input='frozen', idle_timeout_s=5,
        frozen_inputs={'files': {}, 'sealed_dirs': [], 'directories': ['/tmp/inputs/repository']})
    result = runtime.execute(['/native'], deadline=time.monotonic()+10, **options)
    # Version 5 execution-control records remain valid alongside new version 6 inputs.
    assert json.loads((runtime.root / 'remote-binding.json').read_text())['agent_protocol'] == 5
    assert runtime.result(['/native'], **options) == result
    assert len([call for call in calls if call[0] == 'execute']) == 1


def test_local_directory_worker_projection_and_original_recovery(execution, monkeypatch):
    from infermatrix_copilot.providers import container_execution as containers
    from infermatrix_copilot.providers.container_protocol import receipt_line
    runtime, daemon = execution
    original = daemon.control
    def control(*args):
        if args[:2] == ('image', 'inspect'):
            return json.dumps({'io.infermatrix.sandbox.inputs': '6'})
        return original(*args)
    monkeypatch.setattr(runtime, '_control', control)
    requests = []
    def start(command, *, input, cleanup, **kwargs):
        request = json.loads(input);requests.append(request)
        assert request['version'] == 8
        assert request['frozen_inputs']['directories'] == ['/tmp/inputs/repository/empty']
        assert (runtime.root / 'input-files/repository/empty').is_dir()
        daemon.container['State'] = {'Running': False, 'Status': 'exited', 'ExitCode': 0}
        cleanup()
        return ProcessOutcome('', receipt_line(request,
            {'stdout': hashlib.sha256(b''), 'stderr': hashlib.sha256(b'')}, 0, None), 0)
    monkeypatch.setattr(containers, 'run_process', start)
    options = dict(image=IMAGE, cwd='/tmp/work', env={}, input='prompt',
        frozen_inputs={'files': {}, 'sealed_dirs': [], 'directories': ['/tmp/inputs/repository/empty']})
    result = runtime.run_command(['/native'], deadline=time.monotonic()+10, **options)
    monkeypatch.setattr(runtime, '_control', lambda *args: pytest.fail('terminal result must not inspect image or daemon'))
    assert runtime.get_command_result(['/native'], **options) == result
    assert len(requests) == 1


def test_original_binary_protocol_fingerprint_does_not_become_directory_protocol(execution):
    runtime, _ = execution
    value = {'files': {'/tmp/inputs/object': {'base64': 'AA==', 'executable': False}}, 'sealed_dirs': []}
    assert input_protocol(value) == 5
    _, before = runtime._command_envelope(['/native'], cwd='/tmp/work', env={}, input='original',
        deadline=1234, protocol=7, frozen_inputs=value)
    assert json.loads(before) == {'version': 7, 'argv': ['/native'], 'input': 'original',
        'proxy': False, 'deadline': 1234, 'credentials_sha256': None,
        'frozen_inputs': {'files': {'/tmp/inputs/object': {'sha256': hashlib.sha256(b'\0').hexdigest(),
            'size': 1, 'executable': False}}, 'sealed_dirs': []}, 'controls': None}
    assert 'directories' not in before


def test_directory_capability_refusal_precedes_model_account_preparation(tmp_path):
    from types import SimpleNamespace
    from infermatrix_copilot.budgeting import bind_call_budget
    from infermatrix_copilot.providers.completion import complete_native
    from infermatrix_copilot.sandbox import BoundSandboxProfile
    from test_sandbox_execution import Backend
    calls, accounting = [], []
    backend = Backend(tmp_path, calls)
    transport = SimpleNamespace(spec=SimpleNamespace(id='fixture'),
        completion_profile=BoundSandboxProfile(backend),
        complete=lambda **kwargs: pytest.fail('unsupported image must not dispatch'))
    with bind_call_budget('fixture', lambda request: None, lambda ticket, facts: accounting.append(facts),
                          prepare=lambda *args: pytest.fail('unsupported image must not prepare a paid call')):
        with pytest.raises(ValueError, match='does not support frozen') as caught:
            complete_native(lambda: transport, request={'model': 'fixture',
                'frozen_inputs': {'files': {}, 'sealed_dirs': [], 'directories': ['/tmp/inputs/repository']}})
    assert caught.value.native_call_sent is False
    assert calls == []


def test_snapshot_accepts_stale_preopen_timestamp_for_unchanged_file(tmp_path, monkeypatch):
    from types import SimpleNamespace
    source = tmp_path / 'source'
    source.mkdir()
    evidence = source / 'knowledge-context.sqlite'
    evidence.write_bytes(b'committed ledger bytes')
    original = Path.lstat
    observed = []
    def stale_once(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == evidence and not observed:
            observed.append(True)
            fields = {key: getattr(result, key) for key in dir(result) if key.startswith('st_')}
            fields.update(st_mtime_ns=result.st_mtime_ns - 1000000,
                          st_ctime_ns=result.st_ctime_ns - 1000000)
            return SimpleNamespace(**fields)
        return result
    monkeypatch.setattr(Path, 'lstat', stale_once)
    frozen = snapshot_directory(source)
    import base64
    assert base64.b64decode(frozen['files']['/tmp/inputs/repository/knowledge-context.sqlite']['base64']) == b'committed ledger bytes'
    assert observed


@pytest.mark.parametrize('replacement', ['regular', 'symlink', 'fifo'])
def test_snapshot_refuses_path_replacement_at_open(tmp_path, monkeypatch, replacement):
    source = tmp_path / 'source'
    source.mkdir()
    evidence = source / 'evidence.txt'
    evidence.write_bytes(b'original evidence')
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'private bytes')
    original = os.open
    changed = []
    def swap(path, flags, *args, **kwargs):
        if Path(path) == evidence and not changed:
            changed.append(True)
            evidence.rename(tmp_path / 'original.txt')
            if replacement == 'regular':
                evidence.write_bytes(b'replacement evidence')
            elif replacement == 'symlink':
                evidence.symlink_to(outside)
            else:
                os.mkfifo(evidence)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', swap)
    with pytest.raises((ValueError, OSError)):
        snapshot_directory(source)
    assert changed


def test_snapshot_refuses_replacement_after_read(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    evidence = source / 'evidence.txt'
    evidence.write_bytes(b'original evidence')
    inode = evidence.stat().st_ino
    original = os.read
    changed = []
    def swap_after_read(fd, size):
        data = original(fd, size)
        if os.fstat(fd).st_ino == inode and data and not changed:
            changed.append(True)
            evidence.rename(tmp_path / 'original.txt')
            evidence.write_bytes(b'original evidence')
        return data
    monkeypatch.setattr(os, 'read', swap_after_read)
    with pytest.raises(ValueError, match='source changed during snapshot'):
        snapshot_directory(source)
    assert changed
