from functools import partial
import json
import os
import subprocess
import sys
import time

import pytest

from infermatrix_copilot.container_worker import validate_request
from infermatrix_copilot.providers.base import run_cli
from infermatrix_copilot.providers.container_execution import ContainerExecution
from infermatrix_copilot.providers.process import run_process


def envelope(**changes):
    return {'version': 1, 'argv': [sys.executable, '-c', 'pass'],
            'input': None, 'proxy': False, **changes}


@pytest.mark.parametrize('changes', [
    {'argv': []}, {'argv': 'python'}, {'argv': ['relative']},
    {'argv': ['/bin/x', '\0']}, {'version': True}, {'input': {}},
    {'proxy': 'yes'}, {'env': {'TOKEN': 'secret'}},
])
def test_invalid_envelope_rejected(changes):
    with pytest.raises(ValueError):
        validate_request(envelope(**changes))


def test_worker_preserves_native_streams_exit_and_isolates_environment():
    request = envelope(argv=[sys.executable, '-c',
        'import os,sys; assert "IMC_TEST_SECRET" not in os.environ; '
        'assert os.getcwd()=="/tmp/work"; '
        'sys.stdout.write(sys.stdin.read()); sys.stderr.write("native error"); '
        'sys.exit(7)'], input='frozen evidence\n最後')
    result = subprocess.run([sys.executable, '-m', 'infermatrix_copilot.container_worker'],
        input=json.dumps(request), text=True, capture_output=True,
        env={**os.environ, 'IMC_TEST_SECRET': 'must-not-reach-child'}, timeout=15)
    assert (result.stdout, result.stderr, result.returncode) == (
        request['input'], 'native error', 7)


@pytest.mark.parametrize('deadline', [True, None, float('inf'), float('nan'), -1, '100'])
def test_invalid_worker_deadline_rejected(deadline):
    with pytest.raises(ValueError, match='deadline'):
        validate_request(envelope(version=2, deadline=deadline))


def test_worker_enforces_deadline_without_a_controller():
    request = envelope(version=2, deadline=time.monotonic() + 1,
        argv=[sys.executable, '-c',
              'import time; print("started", flush=True); time.sleep(60)'])
    result = subprocess.run([sys.executable, '-m', 'infermatrix_copilot.container_worker'],
        input=json.dumps(request), text=True, capture_output=True, timeout=5)
    assert result.returncode == 124
    assert result.stdout == 'started\n'


def test_expired_worker_request_cannot_launch_native_command():
    request = envelope(version=2, deadline=time.monotonic() - 1,
                       argv=[sys.executable, '-c', 'print("dispatched")'])
    result = subprocess.run([sys.executable, '-m', 'infermatrix_copilot.container_worker'],
        input=json.dumps(request), text=True, capture_output=True, timeout=5)
    assert result.returncode == 124 and result.stdout == ''


def test_common_cli_boundary_uses_container_worker(tmp_path, monkeypatch):
    runtime = ContainerExecution(tmp_path, docker='/docker', socket='/daemon')
    calls = []

    def local_worker(*, image, argv, deadline, input, on_line, cancel, worker_deadline,
                     worker_protocol):
        # Only the Docker boundary is replaced. Exercise the packaged worker
        # and real native subprocess; containment is verified separately.
        calls.append((image, argv, json.loads(input)))
        assert worker_deadline == deadline == json.loads(input)['deadline']
        assert worker_protocol == 3
        return run_process([sys.executable, *argv[1:]], cwd=tmp_path,
            env=os.environ.copy(), deadline=deadline, input=input,
            on_line=on_line, cancel=cancel)

    monkeypatch.setattr(runtime, 'run', local_worker)
    result = run_cli([sys.executable, '-c', 'import sys; print(sys.stdin.read())'],
        cwd='/tmp/work', env={}, timeout_s=10, input='evidence',
        executor=partial(runtime.run_command, image='sha256:' + 'a' * 64))
    assert result == ('evidence\n', '', 0, False)
    assert len(calls) == 1
    assert calls[0][1][1:] == ['-m', 'infermatrix_copilot.container_worker']
    assert calls[0][2]['input'] == 'evidence'


@pytest.mark.parametrize('cwd,env', [('/host/repo', {}), ('/tmp/work', {'TOKEN': 'x'})])
def test_host_context_rejected_before_reservation(tmp_path, cwd, env):
    runtime = ContainerExecution(tmp_path / 'record', docker='/docker', socket='/daemon')
    with pytest.raises(ValueError):
        runtime.run_command(['/bin/true'], image='sha256:' + 'a' * 64,
                            cwd=cwd, env=env, deadline=100)
    assert not runtime.root.exists()


def test_conflicting_execution_seams_rejected():
    with pytest.raises(ValueError):
        run_cli(['/bin/true'], cwd='/tmp/work', env={}, timeout_s=10,
                executor=lambda *a, **k: pytest.fail('dispatch'), runner=object())
