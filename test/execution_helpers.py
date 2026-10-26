"""Exercise application policies through the real shared execution entry."""
from types import SimpleNamespace
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import socket

import pytest

from infermatrix_copilot.app.workflow_execution import WorkflowExecution


def application_executor(registry, settings, *, run_dir, **resources):
    execution = WorkflowExecution(settings, registry)

    async def run(playbook, state):
        return await execution.execute(playbook, run_dir=run_dir, state=state, **resources)

    return SimpleNamespace(run=run)


@pytest.fixture
def contained_codex(tmp_path, monkeypatch):
    """Replace only Docker/native processes; keep profile, worker and receipts."""
    from infermatrix_copilot import container_worker
    from infermatrix_copilot.providers import worker_credentials
    from infermatrix_copilot.providers.container_execution import ContainerExecution
    from infermatrix_copilot.providers.process import ProcessOutcome

    docker = tmp_path / 'docker'
    docker.write_text('#!/bin/sh\nexit 99\n')
    docker.chmod(0o700)
    daemon = socket.socket(socket.AF_UNIX)
    daemon.bind(str(tmp_path / 'daemon.sock'))
    proxy = socket.socket(socket.AF_UNIX)
    proxy.bind(str(tmp_path / 'model.sock'))
    auth = tmp_path / 'auth.json'
    auth.write_text(json.dumps({'auth_mode':'chatgpt','OPENAI_API_KEY':None,
        'tokens':{'access_token':'fixture-access-token','id_token':'fixture-id-token',
                  'account_id':'fixture-account','refresh_token':'host-only-refresh-token'}}))
    auth.chmod(0o600)
    monkeypatch.setenv('NATIVE_COMPLETION_PROFILES', json.dumps({'codex':{
        'kind':'docker','docker':str(docker),'socket':str(tmp_path/'daemon.sock'),
        'image':'sha256:'+'a'*64,'codex_auth_file':str(auth),'model_proxy_socket':str(tmp_path/'model.sock')}}))
    monkeypatch.setenv('TRACE_STORE_ROOT', str(tmp_path / 'native-traces'))
    selected = SimpleNamespace(runner=None, calls=[])

    def native(argv, **kwargs):
        selected.calls.append(argv)
        result = selected.runner(argv, **kwargs)
        kwargs['on_chunk']('stdout', result.stdout.encode())
        kwargs['on_chunk']('stderr', result.stderr.encode())
        return ProcessOutcome(result.stdout, result.stderr, result.returncode)

    real_receive = worker_credentials.receive_bundle
    def run(execution, **kwargs):
        request = json.loads(kwargs['input'])
        assert kwargs['worker_protocol'] == request['version']
        with monkeypatch.context() as patch:
            if request.get('frozen_inputs') is not None:
                # Simulate Docker's bind mount, then run the real worker verifier.
                from pathlib import Path
                from infermatrix_copilot.sandbox import inputs
                mounts = inputs.stage_inputs(execution.root / 'fixture-inputs', kwargs['frozen_inputs'])
                def verify(value):
                    def mounted_path(name):
                        path = Path(name)
                        for host, target in mounts:
                            if path == Path(target) or Path(target) in path.parents:
                                return host / path.relative_to(target)
                        return path
                    with monkeypatch.context() as mapping:
                        mapping.setattr(inputs, 'Path', mounted_path)
                        inputs.verify_inputs(value)
                patch.setattr(container_worker, 'verify_inputs', verify)
            patch.setattr(worker_credentials, 'receive_bundle',
                          lambda digest: real_receive(digest, execution.credential_socket))
            patch.setattr(container_worker, 'run_process', native)
            stdout, stderr = io.TextIOWrapper(io.BytesIO()), io.TextIOWrapper(io.BytesIO())
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = container_worker.execute(request)
                result = ProcessOutcome(stdout.buffer.getvalue().decode(), stderr.buffer.getvalue().decode(), code)
        return result

    monkeypatch.setattr(ContainerExecution, 'run', run)
    monkeypatch.setattr(ContainerExecution, 'supported_input_protocol', lambda self, image: 6)
    try:
        yield selected
    finally:
        daemon.close()
        proxy.close()
