"""Abrupt SDK parent death while the standard worker owns a reserved call.

The provider/container edge is scripted; the SDK, process boundary, durable
worker, workflow, reservation and accounting owners are the real installed code.
"""
from pathlib import Path
import hashlib
import json
import os
import runpy
import sqlite3
import subprocess
import sys
import time

import pytest

P = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(P / 'test'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_strict_budget_stop import settings, configured, arm  # noqa: E402,F401
from test_strict_budget_worker import install_scripted_native  # noqa: E402
from test_e2e_strict_mock import _pr_checkout  # noqa: E402
from test_arm_runtime import rows  # noqa: E402
from arm_runtime import sdk_runtime  # noqa: E402
from durable_worker import runtime_options, wait_result  # noqa: E402
from strict_host import forge_cli  # noqa: E402
from infermatrix_copilot.sdk.v1 import StrictRuntime, StrictReviewRequest, RepositoryRef  # noqa: E402
import infermatrix_copilot  # noqa: E402

pytestmark = pytest.mark.skipif('site-packages' not in infermatrix_copilot.__file__,
    reason='Standard durable worker requires an installed product.')


def options(shim):
    original = runtime_options(shim)['worker_prefix']
    return {'worker_prefix': (sys.executable, '-I', '-B', str(Path(__file__).resolve()),
        '--worker', str(Path(shim).parent / 'config.json'), *original[3:])}


def request(head):
    return StrictReviewRequest(RepositoryRef('widget', 'acme/widget'), 7, head, '',
        'abrupt-sdk-parent-crash', review_depth='standard')


def bootstrap():
    role, config = sys.argv[1], Path(sys.argv[2])
    install_scripted_native(config)
    from infermatrix_copilot.providers.codex import CodexTransport
    CodexTransport.auth_gap = lambda self: None
    root = config.parent.parent
    if role == '--worker':
        from infermatrix_copilot.providers.container_execution import ContainerExecution
        native = ContainerExecution.run
        def paused(execution, **kwargs):
            ready = root / 'native-boundary-ready.json'
            if not ready.exists():
                ready.write_text(json.dumps({'worker_pid': os.getpid()}))
                deadline = time.monotonic() + 60
                while not (root / 'release-native').exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError('Scripted native boundary was not released')
                    time.sleep(.02)
            return native(execution, **kwargs)
        ContainerExecution.run = paused
        sys.argv = sys.argv[3:]
        runpy.run_path(sys.argv[0], run_name='__main__')
        return
    assert role == '--parent'
    payload = json.loads((root / 'parent-input.json').read_text())
    shim = config.parent / 'codex'
    with sdk_runtime(payload['overrides'], shim=shim, for_worker=True) as selected:
        with StrictRuntime(settings_overrides=selected, **options(shim)) as runtime:
            first = runtime.start_review(request(payload['head']))
            run = Path(selected['run_root']) / first.run_id
            deadline = time.monotonic() + 60
            while not (root / 'native-boundary-ready.json').exists():
                result = runtime.get_result(first.run_id)
                if result.terminal or time.monotonic() > deadline:
                    raise AssertionError((run / 'console.log').read_text())
                time.sleep(.05)
            ready = json.loads((root / 'native-boundary-ready.json').read_text())
            (root / 'parent-ready.json').write_text(json.dumps({**ready,
                'run_id': first.run_id, 'parent_pid': os.getpid(),
                'worker_launch_sha256': hashlib.sha256((run / 'worker-launch.json').read_bytes()).hexdigest()}))
            os._exit(91)  # no SDK/context cleanup: actual abrupt process death


def test_abrupt_parent_crash_keeps_original_worker_and_reserved_call(
    arm, tmp_path, record_property,
):
    settings, parent_calls, shim, _ = arm
    work, head = _pr_checkout(tmp_path)
    config = shim.parent / 'config.json'
    value = json.loads(config.read_text())
    value.update(case_id='strict')
    value['plan'].update(native_call_limits={'strict': 5}, call_identity='agent-step/1',
        worker_execution='durable', workflow_timeout_s=180)
    config.write_text(json.dumps(value))
    overrides = settings.model_dump(mode='json')
    overrides.update(_env_file=None, repo_paths={'widget': str(work)},
        repo_full_names={'widget': 'acme/widget'}, mcp_repo_allowlist=['widget'],
        default_repo='widget', review_depth='standard', strict_backend_cli=str(shim))
    (tmp_path / 'parent-input.json').write_text(json.dumps({'overrides': overrides, 'head': head}))
    fixture = P / 'test/test_e2e_strict_mock.py'
    descriptor = {'kind': 'fixture', 'fixture': str(fixture),
        'sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(), 'head': head}
    with forge_cli(tmp_path, descriptor):
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        process = subprocess.Popen([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
            '--parent', str(config)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = process.communicate(timeout=80)
            assert process.returncode == 91, stdout + stderr
            ready = json.loads((tmp_path / 'parent-ready.json').read_text())
            assert ready['parent_pid'] == process.pid
            os.kill(ready['worker_pid'], 0)
            reserved = rows(shim)
            assert len(reserved) == 1 and reserved[0][0] == 'reserved'
            with sdk_runtime(overrides, shim=shim, for_worker=True) as selected:
                with StrictRuntime(settings_overrides=selected, **options(shim)) as recovered:
                    run = Path(selected['run_root']) / ready['run_id']
                    before = (run / 'worker-launch.json').read_bytes()
                    assert hashlib.sha256(before).hexdigest() == ready['worker_launch_sha256']
                    found = recovered.find_review('abrupt-sdk-parent-crash', expected_head_sha=head)
                    assert found.run_id == ready['run_id']
                    assert recovered.start_review(request(head)).run_id == ready['run_id']
                    assert recovered.get_result(ready['run_id']).state == 'running'
                    assert rows(shim) == reserved
                    assert (run / 'worker-launch.json').read_bytes() == before
                    (tmp_path / 'release-native').write_text('resume original scripted response')
                    result = wait_result(recovered, ready['run_id'], 120)
                    assert result.state == 'done'
                    complete = rows(shim)
                    assert len(complete) == 5 and all(row[0] == 'complete' for row in complete)
                    assert recovered.start_review(request(head)).run_id == ready['run_id']
                    assert rows(shim) == complete
                    assert (run / 'worker-launch.json').read_bytes() == before
                    assert len(list((run / 'evaluation-workers').glob('*.json'))) == 1
                    with sqlite3.connect(tmp_path / 'scripted-processes.sqlite') as db:
                        calls = db.execute('SELECT pid FROM calls').fetchall()
                    assert len(calls) == 5 and {pid for pid, in calls} == {ready['worker_pid']}
                    assert not parent_calls
                    record_property('abrupt_parent_evidence', json.dumps({
                        **ready, 'parent_exit_code': process.returncode, 'same_run_and_worker': True,
                        'one_reserved_call_before_response': True, 'final_state': result.state,
                        'scripted_native_calls': len(calls), 'actual_model_calls': 0,
                        'second_startup_or_dispatch': False}))
        finally:
            (tmp_path / 'release-native').touch()
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


if __name__ == '__main__':
    bootstrap()
