"""Application endpoint for sandbox operations on their original execution host.

SSH supplies authorization. Requests contain trusted configuration and command
input, never login contents. All actual execution delegates to the local backend.
"""
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import tempfile

from .persistence import immutable_write_bytes
from .providers.completion_profiles import local_sandbox_profile, parse_completion_config
from .sandbox.base import AGENT_PROTOCOL_VERSION, CancellationReceipt, FileCancellation, SandboxPending, execution_controls, EXECUTION_PROTOCOL_VERSION
from .sandbox.remote import canonical
from .sandbox.inputs import BINARY_INPUT_PROTOCOL_VERSION, INPUT_PROTOCOL_VERSION, SEALED_FILE_INPUT_PROTOCOL_VERSION, LEGACY_INPUT_PROTOCOL_VERSION, input_protocol, normalize_inputs
from .trace_store import environment_fingerprint


def _state(request, release):
    binding = request['binding']
    if (not isinstance(binding, dict) or binding.get('version') != 1
            or not re.fullmatch(r'[0-9a-f]{32}', str(binding.get('identity', '')))
            or binding.get('release') != release
            or binding.get('agent_protocol', 1) != request['version']
            or binding.get('backend') != request['backend']
            or binding.get('location', {}).get('state_root') != request['state_root']
            or type(binding.get('timeout_s')) not in (int, float)
            or not math.isfinite(binding['timeout_s']) or binding['timeout_s'] <= 0):
        raise SandboxPending('original remote sandbox request or release differs', prior_execution=True)
    root = Path(request['state_root'])
    if not root.is_absolute():
        raise ValueError('remote state root must be absolute')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    details = root.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o022:
        raise ValueError('remote sandbox state must be an owned private directory')
    root = root / binding['identity']
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink():
        raise ValueError('remote execution directory must not be a symlink')
    data = canonical(binding).encode()
    path = root / 'request.json'
    if request['operation'] == 'execute':
        try:
            immutable_write_bytes(path, data)
        except FileExistsError as exc:
            raise SandboxPending('original remote execution already reserved', prior_execution=True) from exc
    elif not path.is_file() or path.is_symlink() or path.read_bytes() != data:
        raise SandboxPending('original remote execution request unavailable', prior_execution=True)
    return root, binding


def perform(request, emit):
    if (not isinstance(request, dict) or set(request) != {'version', 'operation', 'backend', 'state_root', 'binding'}
            or request['version'] not in (1, AGENT_PROTOCOL_VERSION, LEGACY_INPUT_PROTOCOL_VERSION, SEALED_FILE_INPUT_PROTOCOL_VERSION, BINARY_INPUT_PROTOCOL_VERSION, INPUT_PROTOCOL_VERSION)
            or type(request['version']) is not int
            or request['operation'] not in {'preflight', 'execute', 'result', 'cancel', 'context'}):
        raise ValueError('invalid remote sandbox operation')
    config = parse_completion_config(request['backend'])
    release = environment_fingerprint()['runtime_package_sha256']
    if request['operation'] == 'preflight':
        with tempfile.TemporaryDirectory(prefix='im-sandbox-preflight-') as directory:
            profile = local_sandbox_profile(config, Path(directory))
            supported = min(INPUT_PROTOCOL_VERSION, profile.sandbox.supported_input_protocol())
        return {'release': release, 'protocol': AGENT_PROTOCOL_VERSION, 'context_binding': True,
                'frozen_inputs_protocol': supported, 'execution_controls': 1}
    root, binding = _state(request, release)
    command = binding['request']
    fields = {'cmd', 'cwd', 'env', 'input'}
    if request['version'] in (LEGACY_INPUT_PROTOCOL_VERSION, SEALED_FILE_INPUT_PROTOCOL_VERSION, BINARY_INPUT_PROTOCOL_VERSION, INPUT_PROTOCOL_VERSION):
        fields.add('frozen_inputs')
    if request['version'] == EXECUTION_PROTOCOL_VERSION and 'controls' in command:
        fields.add('controls')
    if set(command) != fields:
        raise ValueError('invalid remote sandbox command')
    args = dict(cwd=command['cwd'], env=command['env'], input=command['input'])
    if request['version'] in (LEGACY_INPUT_PROTOCOL_VERSION, SEALED_FILE_INPUT_PROTOCOL_VERSION, BINARY_INPUT_PROTOCOL_VERSION, INPUT_PROTOCOL_VERSION):
        frozen_inputs = normalize_inputs(command['frozen_inputs'])
        if (frozen_inputs != command['frozen_inputs']
                or request['version'] != EXECUTION_PROTOCOL_VERSION and (frozen_inputs is None or input_protocol(frozen_inputs) != request['version'])):
            raise ValueError('invalid remote frozen sandbox inputs')
        args['frozen_inputs'] = frozen_inputs
    if 'controls' in command:
        controls = command['controls']
        if not isinstance(controls, dict) or execution_controls(**controls) != controls:
            raise ValueError('invalid remote execution controls')
        args.update(controls)
    if request['operation'] == 'context':
        profile = local_sandbox_profile(config, root / 'execution', for_dispatch=False)
        return profile.sandbox.context()
    if request['operation'] == 'cancel':
        immutable_write_bytes(root / 'cancel-intent.json', b'{}', exist_ok=True)
        profile = local_sandbox_profile(config, root / 'execution', for_dispatch=False)
        receipt = CancellationReceipt(True, False, 'pending')
        if (root / 'execution/binding.json').exists():
            receipt = profile.sandbox.cancel()
        return (asdict(receipt) if request['version'] in (AGENT_PROTOCOL_VERSION, LEGACY_INPUT_PROTOCOL_VERSION, SEALED_FILE_INPUT_PROTOCOL_VERSION, BINARY_INPUT_PROTOCOL_VERSION, INPUT_PROTOCOL_VERSION)
                else {'cancel_requested': receipt.requested})
    if request['operation'] == 'result':
        profile = local_sandbox_profile(config, root / 'execution', for_dispatch=False)
        # Match the remote request to the worker before its canonical result read.
        outcome = profile.get_result(command['cmd'], **args, recover=True)
    else:
        profile = local_sandbox_profile(config, root / 'execution')
        if binding.get('expected_context') is not None:
            profile = profile.with_context(binding['expected_context'])
        credentials = getattr(profile.sandbox, 'credentials', None)
        immutable_write_bytes(root / 'execution-profile.json', canonical({
            'release': release, 'backend': request['backend'],
            'credentials': credentials.metadata if credentials is not None else None}).encode())
        outcome = profile.execute(command['cmd'], **args,
            deadline=time.monotonic() + binding['timeout_s'],
            cancel=FileCancellation(root / 'cancel-intent.json'),
            on_line=lambda channel, text: emit({'type': 'line', 'channel': channel, 'text': text}))
    # The backend's bound worker result remains authoritative for reconciliation.
    immutable_write_bytes(root / 'response.json', canonical(asdict(outcome)).encode(), exist_ok=True)
    return asdict(outcome)


def main():
    def emit(value):
        print(canonical(value), flush=True)
    try:
        value = perform(json.load(sys.stdin), emit)
        emit({'type': 'result', 'value': value})
    except BaseException as exc:
        # Configuration, caches, native streams and untrusted record values must
        # never be interpolated into control-channel diagnostics.
        emit({'type': 'error', 'code': 'pending' if isinstance(exc, SandboxPending) else 'rejected'})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
