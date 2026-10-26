"""Trusted host composition retains standard process ownership and run binding."""
import json
import sys

import pytest

from infermatrix_copilot.app.run_service import RunService
from infermatrix_copilot.app.request_policy import PolicyError
from infermatrix_copilot.sdk.v1 import ReviewRuntime
from test_reserved_execution_identity import reserve
from test_completion_profile_configuration import configured  # noqa: F401


@pytest.mark.parametrize('prefix', [[], 'python', ['python'], ['/missing/python'], [sys.executable, '\0']])
def test_invalid_worker_prefix_refuses_before_service_start(prefix):
    with pytest.raises(ValueError, match='worker prefix'):
        RunService(worker_prefix=prefix)


def test_sdk_injects_host_composition_without_changing_default_worker(configured):
    settings, calls = configured
    with ReviewRuntime(settings_overrides={'_env_file': None, 'run_root': settings.run_root},
                       worker_prefix=(sys.executable, '-I')) as runtime:
        assert runtime._core.worker_prefix == (sys.executable, '-I')
    assert not calls


def test_worker_prefix_keeps_standard_entry_and_original_launch_binding(configured, monkeypatch):
    settings, calls = configured
    run, _ = reserve(settings)
    service = RunService.__new__(RunService)
    service.settings, service.run_root = settings, settings.run_root
    service.worker_prefix = (sys.executable, '-I', '/trusted/composition.py', '--')
    service._reap = lambda: None
    launched = []

    class Child:
        pid, returncode = 424243, 0
        def wait(self):
            return 0

    monkeypatch.setattr('infermatrix_copilot.app.run_service.subprocess.Popen',
                        lambda command, **kwargs: launched.append(command) or Child())
    service._launch(run.name)
    command, = launched
    assert command == [*service.worker_prefix, '-m', 'infermatrix_copilot', '--execute-reserved', run.name]
    binding = (run / 'worker-launch.json').read_bytes()
    assert json.loads(binding)['command'] == command
    service.worker_prefix = (sys.executable, '-I', '/other/composition.py', '--')
    with pytest.raises(PolicyError, match='original binding'):
        service._launch(run.name)
    assert len(launched) == 1 and (run / 'worker-launch.json').read_bytes() == binding
    assert not calls
