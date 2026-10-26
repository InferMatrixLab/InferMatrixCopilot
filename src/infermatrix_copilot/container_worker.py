"""Container entry point for an already authorized provider command.

The host owns deadlines, durable identity and accounting. This worker preserves
the native stdout/stderr protocol and uses the shared process supervisor. Its
request is host configuration, never a model-generated tool call.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import re

from .providers.network_relay import loopback_proxy
from .process_execution import run_process
from .providers.container_protocol import receipt_line
from .sandbox.inputs import normalize_inputs, verify_inputs
from .sandbox.base import execution_controls


def validate_request(request):
    if not isinstance(request, dict):
        raise ValueError('invalid container command envelope')
    fields = {'version', 'argv', 'input', 'proxy'}
    if request.get('version') in (2, 3, 4, 5, 6, 7, 8):
        fields.add('deadline')
    if request.get('version') in (4, 5, 6, 7, 8):
        fields.add('credentials_sha256')
    if request.get('version') in (5, 6, 7, 8):
        fields.add('frozen_inputs')
    if request.get('version') in (7, 8):
        fields.add('controls')
    if set(request) != fields:
        raise ValueError('invalid container command envelope')
    argv = request['argv']
    if (type(request['version']) is not int or request['version'] not in (1, 2, 3, 4, 5, 6, 7, 8)
            or not isinstance(argv, list) or not argv
            or not all(isinstance(arg, str) and '\0' not in arg for arg in argv)
            or not argv[0].startswith('/')
            or request['input'] is not None and not isinstance(request['input'], str)
            or type(request['proxy']) is not bool):
        raise ValueError('invalid container command fields')
    if request['version'] in (2, 3, 4, 5, 6, 7, 8) and (
            type(request['deadline']) not in (int, float)
            or not math.isfinite(request['deadline']) or request['deadline'] <= 0):
        raise ValueError('invalid container command deadline')
    credentials = request.get('credentials_sha256')
    if request['version'] in (4, 5, 6, 7, 8) and not (request['version'] in (5, 6, 7, 8) and credentials is None) and (not isinstance(credentials, str)
            or not re.fullmatch(r'[0-9a-f]{64}', request['credentials_sha256'])):
        raise ValueError('invalid worker credential identity')
    if request['version'] in (5, 6) and (normalize_inputs(request['frozen_inputs']) is None
            or normalize_inputs(request['frozen_inputs']) != request['frozen_inputs']):
        raise ValueError('invalid frozen worker inputs')
    if request['version'] in (5, 6) and bool(request['frozen_inputs'].get('sealed_files')) != (request['version'] == 6):
        raise ValueError('frozen worker input protocol differs from its projection')
    if request['version'] in (7, 8):
        if normalize_inputs(request['frozen_inputs'], projection=True) != request['frozen_inputs']:
            raise ValueError('invalid frozen worker projection')
        controls = request['controls']
        if controls is not None and (not isinstance(controls, dict)
                or set(controls) != {'idle_timeout_s', 'termination_grace_s'}
                or execution_controls(**controls) != controls):
            raise ValueError('invalid worker execution controls')
    if request['version'] != 8 and (request.get('frozen_inputs') or {}).get('directories'):
        raise ValueError('directory inputs require worker protocol 8')
    return request


def execute(request):
    request = validate_request(request)
    digests = {name: hashlib.sha256() for name in ('stdout', 'stderr')}

    def finish(code, reason=None):
        if request['version'] in (3, 4, 5, 6, 7, 8):
            sys.stderr.write(receipt_line(request, digests, code, reason))
            sys.stderr.flush()
        return code
    # The controller is on the daemon's host: its monotonic clock is shared
    # with the worker. Keep this guard alive even if the controller disappears.
    deadline = request.get('deadline', float('inf'))
    if time.monotonic() >= deadline:
        return finish(124, 'absolute')
    workspace = Path('/tmp/work')
    workspace.mkdir(parents=True, exist_ok=True)
    if request['version'] in (5, 6, 7, 8):
        verify_inputs(request['frozen_inputs'])
    # Do not inherit image or controller credentials/configuration variables.
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp',
           'TMPDIR': '/tmp', 'LANG': 'C.UTF-8'}
    with ExitStack() as resources:
        secrets = None
        if request.get('credentials_sha256') is not None:
            from .providers.worker_credentials import receive_bundle, credential_environment
            bundle = receive_bundle(request['credentials_sha256'])
            env.update(resources.enter_context(credential_environment(bundle)))
            secrets = bundle['redactions']
        if request['proxy']:
            host, port = resources.enter_context(loopback_proxy('/run/model-proxy.sock'))
            from .providers.worker_environment import bind_proxy
            bind_proxy(env, host, port)

        def forward(channel, value):
            digests[channel].update(value)
            stream = sys.stdout.buffer if channel == 'stdout' else sys.stderr.buffer
            stream.write(value)
            stream.flush()

        if secrets is None:
            on_chunk = forward
        else:
            from .redaction import CredentialStream
            streams = {channel: CredentialStream(secrets, lambda data, channel=channel: forward(channel, data))
                       for channel in digests}
            on_chunk = lambda channel, value: streams[channel].feed(value)
        try:
            result = run_process(request['argv'], cwd=workspace, env=env,
                                 deadline=deadline, input=request['input'], on_chunk=on_chunk,
                                 **(request.get('controls') or {}))
        finally:
            if secrets is not None:
                for stream in streams.values():
                    stream.finish()
    if result.reason is not None:
        return finish(124, result.reason)
    return finish(result.returncode if result.returncode >= 0 else 128 - result.returncode)


def main():
    # The entire envelope is consumed before the native process receives stdin.
    return execute(json.load(sys.stdin))


if __name__ == '__main__':
    raise SystemExit(main())
