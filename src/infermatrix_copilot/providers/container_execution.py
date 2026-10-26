"""Daemon-owned execution identity; provider protocols and model policy stay outside.

The profile has no network and accepts only digest-pinned images. Optional
private channels supply a model proxy and ephemeral credentials. It is not a
tool-free capability. A retained
binding is never permission to dispatch again, even if the container is gone.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from ..persistence import atomic_write_bytes, immutable_write_bytes
from ..sandbox.base import (CancellationReceipt, CombinedCancellation, FileCancellation,
                            SandboxCompletionProfile, SandboxPending, refuse_reserved, execution_controls)
from .process import ProcessOutcome, run_process
from ..sandbox.inputs import normalize_inputs, stage_inputs, project_inputs, input_protocol, BINARY_INPUT_PROTOCOL_VERSION


def validate_container_image(image):
    if not isinstance(image, str) or not re.fullmatch(r'(?:[^\s]+@)?sha256:[0-9a-f]{64}', image):
        raise ValueError('container image must be pinned by sha256 digest')


ContainerPending = SandboxPending  # compatibility name; one uncertainty contract


@dataclass(frozen=True)
class ContainerCompletionProfile(SandboxCompletionProfile):
    """Host-selected containment for one completion, not a no-tools promise.

    The host supplies a fresh authorized attempt directory and a trusted pinned
    image containing the worker and native CLI. Reusing this profile after any
    reservation requires reconciliation, never a replacement model dispatch.
    """

    execution: ContainerExecution
    image: str
    native_cli: str = '/usr/local/bin/codex'
    credentials: object = field(default=None, repr=False)

    @property
    def sandbox(self):
        from ..sandbox.docker import DockerSandboxExecution
        return DockerSandboxExecution(self.execution, self.image, self.credentials)


class ContainerExecution:
    """One durable execution, controlled locally on the Docker daemon's host.

    The record directory belongs to the caller's original attempt. New attempts
    require domain authorization outside this class. Keep the record until that
    domain settles its result; deleting it can destroy the idempotency boundary.
    """

    def __init__(self, record_dir, *, docker, socket, control_timeout=15, model_proxy_socket=None):
        self.root = Path(record_dir)
        self.command = [str(docker), '-H', 'unix://' + str(socket)]
        self.daemon_socket = str(Path(socket).absolute())
        self.control_timeout = control_timeout
        self.model_proxy_socket = (str(Path(model_proxy_socket).absolute())
                                   if model_proxy_socket is not None else None)
        if self.model_proxy_socket is not None and (
                Path(self.model_proxy_socket).resolve() == Path(socket).resolve()
                or any(c in self.model_proxy_socket for c in (',', '\n', '\r'))):
            raise ValueError('model proxy must be a dedicated Unix socket')

    def _proxy_identity(self):
        return self._socket_identity(self.model_proxy_socket, 'model proxy')

    @staticmethod
    def _socket_identity(path, label):
        if path is None:
            return None
        try:
            details = Path(path).lstat()
            if not stat.S_ISSOCK(details.st_mode):
                raise ValueError('not a socket')
            return {'path': str(path), 'device': details.st_dev, 'inode': details.st_ino}
        except (OSError, ValueError) as exc:
            raise ContainerPending(f'{label} socket is unavailable') from exc

    def _credential_identity(self):
        path = getattr(self, 'credential_socket', None)
        if path is None:
            return None
        if any(Path(path).resolve() == Path(other).resolve()
               for other in (self.daemon_socket, self.model_proxy_socket) if other is not None):
            raise ValueError('credentials require a dedicated socket')
        return self._socket_identity(path, 'credential')

    def _control(self, *args):
        result = run_process(self.command + list(args), cwd=self.root,
            env=os.environ.copy(), deadline=time.monotonic() + self.control_timeout)
        if result.reason or result.returncode:
            raise ContainerPending('container control failed; retain original binding')
        return result.stdout

    def supported_input_protocol(self, image):
        """An immutable image advertises worker input support, independently of the host."""
        validate_container_image(image)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        labels = json.loads(self._control('image', 'inspect', '--format', '{{json .Config.Labels}}', image))
        if labels is not None and not isinstance(labels, dict):
            raise ValueError('invalid sandbox image labels')
        value = (labels or {}).get('io.infermatrix.sandbox.inputs', str(BINARY_INPUT_PROTOCOL_VERSION))
        if not isinstance(value, str) or value not in ('3', '4', '5', '6'):
            raise ValueError('invalid sandbox image input capability')
        return int(value)

    def require_inputs(self, image, value):
        # Legacy inputs preserve their original worker contracts and fingerprints.
        if value and value.get('directories') and input_protocol(value) > self.supported_input_protocol(image):
            raise ValueError('sandbox image does not support frozen directory inputs')

    def _read(self, filename):
        try:
            value = json.loads((self.root / filename).read_text())
            if not isinstance(value, dict):
                raise ValueError('expected object')
            return value
        except (OSError, ValueError) as exc:
            raise ContainerPending('unreadable container record; retain original binding') from exc

    def _binding(self):
        value = self._read('binding.json')
        if value.get('proxy') is not None and not isinstance(value['proxy'], dict):
            raise ContainerPending('invalid model proxy binding')
        if (value.get('version') != 1 or value.get('control') != self.command
                or (value.get('proxy') or {}).get('path') != self.model_proxy_socket
                or not re.fullmatch(r'imc-[0-9a-f]{32}', str(value.get('name', '')))
                or not isinstance(value.get('daemon'), str) or not value['daemon']):
            raise ContainerPending('invalid or changed container execution binding')
        return value

    def _check_daemon(self, binding):
        if self._control('info', '--format', '{{.ID}}').strip() != binding['daemon']:
            raise ContainerPending('original container daemon is unavailable')

    def _identity(self, binding, *, required=True):
        path = self.root / 'container.json'
        if not required and not path.exists() and not path.is_symlink():
            return None
        value = self._read('container.json')
        if (value.get('name') != binding['name'] or value.get('daemon') != binding['daemon']
                or not re.fullmatch(r'[0-9a-f]{64}', str(value.get('id', '')))):
            raise ContainerPending('invalid native container identity')
        return value['id']

    def _inspect(self, binding):
        self._check_daemon(binding)
        identity = self._identity(binding, required=False)
        try:
            values = json.loads(self._control('inspect', identity or binding['name']))
            value = values[0]
            if (len(values) != 1 or value['Name'] != '/' + binding['name']
                    or not re.fullmatch(r'[0-9a-f]{64}', value['Id'])
                    or (identity is not None and value['Id'] != identity)
                    or not isinstance(value['State']['Running'], bool)
                    or value['Config']['Labels'].get('infermatrix.execution') != binding['name']):
                raise ValueError('identity mismatch')
            return value
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise ContainerPending('invalid container inspection; retain original binding') from exc

    def reconcile(self):
        """Observe the original execution only; never create, start or replay it."""
        binding = self._binding()
        value = self._inspect(binding)
        state = value.get('State')
        if not isinstance(state, dict) or not isinstance(state.get('Running'), bool):
            raise ContainerPending('invalid container state')
        return {'name': binding['name'], 'id': value['Id'], 'state': state,
                'identity_confirmed': self._identity(binding, required=False) is not None}

    def _stop(self, binding):
        identity = self._identity(binding)
        value = self._inspect(binding)
        was_running = value['State']['Running']
        if was_running:
            self._control('kill', identity)
            value = self._inspect(binding)
        if value['State']['Running']:
            raise ContainerPending('container termination is unconfirmed')
        return was_running

    def cancel(self):
        """Persist cancellation before observing/stopping the original container."""
        try:
            binding = self._binding()
            immutable_write_bytes(self.root / 'cancel-intent.json',
                                  json.dumps({'name': binding['name']}).encode(), exist_ok=True)
        except (OSError, ContainerPending) as exc:
            raise ContainerPending('original cancellation intent is not durable', prior_execution=True) from exc
        try:
            self._stop(binding)
            state = self._inspect(binding)['State']
        except ContainerPending:
            return CancellationReceipt(True, False, 'unknown')
        terminal = state.get('Status') == 'exited' and not state['Running']
        return CancellationReceipt(True, terminal, 'terminal' if terminal else 'pending')

    def get_result(self):
        """Read a durably returned outcome, without dispatch or accounting."""
        binding = self._binding()
        path = self.root / 'result.json'
        recovered = not (path.exists() or path.is_symlink())
        value = self._read('recovered-result.json' if recovered else 'result.json')
        if value.get('name') != binding['name'] or value.get('id') != self._identity(binding):
            raise ContainerPending('result belongs to another execution')
        try:
            result = ProcessOutcome.from_dict(value['outcome'])
            # Validate any available worker proof before callers can remove the
            # original container. Only recovered results require a receipt;
            # legacy/live-controller outcomes can legitimately lack one.
            self._decode_worker_result(binding, result, required=recovered)
            return result
        except (KeyError, TypeError, ValueError) as exc:
            raise ContainerPending('invalid recorded container outcome') from exc

    def recover_result(self):
        """Recover a stopped v3 worker from complete logs, never start it again.

        This records process evidence only. Provider parsing and domain receipt
        settlement still belong to their original callers.
        """
        if any((self.root / name).exists() or (self.root / name).is_symlink()
               for name in ('result.json', 'recovered-result.json')):
            return self.get_result()
        binding = self._binding()
        if binding.get('worker_protocol') not in (3, 4, 5, 6, 7, 8):
            raise ContainerPending('original worker has no recoverable terminal protocol')
        identity = self._identity(binding)
        inspected = self._inspect(binding)
        state = inspected['State']
        if (state['Running'] or state.get('Status') != 'exited'
                or type(state.get('ExitCode')) is not int):
            raise ContainerPending('original worker has not exited')
        logs = run_process(self.command + ['logs', identity], cwd=self.root,
                           env=os.environ.copy(), deadline=time.monotonic() + self.control_timeout)
        if logs.reason or logs.returncode:
            raise ContainerPending('original worker logs unavailable')
        outcome = ProcessOutcome(logs.stdout, logs.stderr, state['ExitCode'])
        self._decode_worker_result(binding, outcome, required=True)
        if self._inspect(binding)['State'] != state:
            raise ContainerPending('worker state changed during recovery')
        data = json.dumps({'name': binding['name'], 'id': identity,
                           'outcome': asdict(outcome)}).encode()
        try:
            immutable_write_bytes(self.root / 'recovered-result.json', data, exist_ok=True)
        except FileExistsError as exc:
            raise ContainerPending('conflicting recovered worker result') from exc
        return self.get_result()

    @staticmethod
    def _decode_worker_result(binding, outcome, *, required=False):
        if binding.get('worker_protocol') not in (3, 4, 5, 6, 7, 8):
            if required:
                raise ContainerPending('original worker has no recoverable terminal protocol', prior_execution=True)
            return outcome
        from .container_protocol import decode_receipt
        try:
            return decode_receipt(outcome, binding['input_sha256'],
                                  required=required or binding.get('worker_protocol') in (4, 5, 6, 7, 8))
        except (KeyError, ValueError) as exc:
            raise ContainerPending('worker result requires reconciliation', prior_execution=True) from exc

    def remove(self):
        """Remove only a stopped container with a durably recorded outcome."""
        self.get_result()
        binding = self._binding()
        self._stop(binding)
        self._control('rm', self._identity(binding))
        # Keep binding/result after removal: absence cannot authorize dispatch.

    def _command_envelope(self, cmd, *, cwd, env, input, deadline=None, protocol=None,
                          credentials_sha256=None, frozen_inputs=None, controls=None):
        from ..container_worker import validate_request

        if str(cwd) != '/tmp/work' or env or not isinstance(cmd, (list, tuple)):
            raise ValueError('container commands require /tmp/work and an empty host environment')
        request = validate_request({'version': protocol or (1 if deadline is None else 2),
                                    'argv': list(cmd), 'input': input,
                                    'proxy': self.model_proxy_socket is not None,
                                    **({'deadline': deadline} if deadline is not None else {}),
                                    **({'credentials_sha256': credentials_sha256} if protocol in (4, 5, 6, 7, 8) else {}),
                                    **({'frozen_inputs': project_inputs(frozen_inputs) if protocol in (7, 8) else normalize_inputs(frozen_inputs)} if protocol in (5, 6, 7, 8) else {}),
                                    **({'controls': controls} if protocol in (7, 8) else {})})
        return ['/usr/local/bin/python', '-m', 'infermatrix_copilot.container_worker'], json.dumps(request)

    def get_command_result(self, cmd, *, image, cwd, env, input=None, recover=False, frozen_inputs=None, idle_timeout_s=None, termination_grace_s=0):
        """Read only the result bound to this exact image, command and evidence.

        No daemon access or live proxy is needed after a durable result exists.
        Explicit recovery may observe the original worker's terminal logs.
        Missing/incomplete evidence remains pending; this never runs again.
        """
        binding = self._binding()
        controls = execution_controls(idle_timeout_s, termination_grace_s)
        if controls != binding.get('worker_controls'):
            raise ContainerPending('execution controls differ from original binding')
        try:
            frozen_inputs = normalize_inputs(frozen_inputs)
            if binding.get('worker_protocol') not in (7, 8) and bool(frozen_inputs) != (binding.get('worker_protocol') in (5, 6)):
                raise ValueError('changed frozen input contract')
            argv, payload = self._command_envelope(cmd, cwd=cwd, env=env, input=input,
                deadline=binding.get('worker_deadline'), protocol=binding.get('worker_protocol'),
                credentials_sha256=binding.get('worker_credentials_sha256'), frozen_inputs=frozen_inputs, controls=controls)
        except ValueError as exc:
            raise ContainerPending('invalid original command binding') from exc
        if (binding.get('argv') != argv
                or binding.get('input_sha256') != hashlib.sha256(payload.encode()).hexdigest()):
            raise ContainerPending('completion request differs from original execution binding')
        return self.get_original_command_result(image=image, recover=recover)

    def get_original_command_result(self, *, image, recover=False):
        """Read the original worker receipt using its retained request digest."""
        binding = self._binding()
        if (binding.get('image') != image or binding.get('argv') !=
                ['/usr/local/bin/python', '-m', 'infermatrix_copilot.container_worker']):
            raise ContainerPending('completion worker differs from original execution binding')
        outcome = self.recover_result() if recover else self.get_result()
        return self._decode_worker_result(binding, outcome)

    def run_command(self, cmd, *, image, cwd, env, deadline, input=None,
                    on_line=None, cancel=None, frozen_inputs=None, idle_timeout_s=None, termination_grace_s=0):
        """Execute a provider command through the packaged container worker.

        Commands use image paths and the disposable /tmp/work directory. Host
        paths/environment are deliberately not translated or silently ignored.
        Host-selected credentials use a private side channel; the command input
        contains their digest only. The worker receives no host environment.
        """
        refuse_reserved(self.root / 'binding.json')
        frozen_inputs = normalize_inputs(frozen_inputs)
        credentials_sha256 = getattr(self, 'credentials_sha256', None)
        controls = execution_controls(idle_timeout_s, termination_grace_s)
        protocol = (6 if frozen_inputs.get('sealed_files') else 5) if frozen_inputs is not None else 4 if credentials_sha256 is not None else 3
        if controls is not None or frozen_inputs and input_protocol(frozen_inputs) >= BINARY_INPUT_PROTOCOL_VERSION:
            protocol = 7
        if frozen_inputs and frozen_inputs.get('directories'):
            self.require_inputs(image, frozen_inputs)
            protocol = 8
        argv, payload = self._command_envelope(cmd, cwd=cwd, env=env, input=input,
            deadline=deadline, protocol=protocol, credentials_sha256=credentials_sha256, frozen_inputs=frozen_inputs, controls=controls)
        from .container_protocol import NativeOutputLines
        lines = NativeOutputLines(on_line) if on_line is not None else None
        try:
            outcome = self.run(image=image, argv=argv, deadline=deadline, input=payload,
                              on_line=lines, cancel=cancel, worker_deadline=deadline,
                              worker_protocol=protocol,
                              **({'worker_controls': controls} if protocol in (7, 8) else {}),
                              **({'frozen_inputs': frozen_inputs} if frozen_inputs is not None else {}),
                              **({'worker_credentials_sha256': credentials_sha256} if credentials_sha256 is not None else {}))
            decoded = self._decode_worker_result({'worker_protocol': protocol,
                'input_sha256': hashlib.sha256(payload.encode()).hexdigest()}, outcome)
        except BaseException:
            if lines is not None:
                try:
                    lines.finish()
                except BaseException:
                    pass  # retain the original execution/observer failure
            raise
        if lines is not None:
            lines.finish(decoded)
        return decoded

    def run(self, *, image, argv, deadline, input=None, on_line=None, cancel=None,
            worker_deadline=None, worker_protocol=None, worker_credentials_sha256=None, frozen_inputs=None, worker_controls=None):
        # Existing authority takes precedence even over cancellation/invalid
        # arguments: a new invocation did not settle the original execution.
        refuse_reserved(self.root / 'binding.json')
        cancel = CombinedCancellation((cancel, FileCancellation(self.root / 'cancel-intent.json')))
        validate_container_image(image)
        if not argv or not all(isinstance(arg, str) for arg in argv):
            raise ValueError('container command must be a nonempty string sequence')
        if deadline <= time.monotonic() or (cancel is not None and cancel.is_set()):
            raise ValueError('container execution cancelled before reservation')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        frozen_inputs = normalize_inputs(frozen_inputs)
        if frozen_inputs is not None and (worker_protocol not in (5, 6, 7, 8) or not isinstance(input, str)
                or json.loads(input).get('frozen_inputs') != (project_inputs(frozen_inputs) if worker_protocol in (7, 8) else frozen_inputs)):
            raise ValueError('frozen sandbox mounts require a bound worker request')
        self.require_inputs(image, frozen_inputs)
        mounts = stage_inputs(self.root, frozen_inputs)
        for source, _ in mounts:
            if any(c in str(source) for c in '\0\r\n,'):
                raise ValueError('invalid frozen sandbox staging path')
        # The exclusive immutable publication arbitrates concurrent callers.
        # Any existing record, even corrupt, is a refusal to create/start again.
        name = 'imc-' + uuid.uuid4().hex
        binding = {'version': 1, 'name': name, 'control': self.command,
                   'daemon': self._control('info', '--format', '{{.ID}}').strip(),
                   'image': image, 'argv': argv, 'proxy': self._proxy_identity(),
                   'input_sha256': hashlib.sha256(
                       input.encode() if isinstance(input, str) else (input or b'')).hexdigest()}
        if worker_deadline is not None:
            binding['worker_deadline'] = worker_deadline
        if worker_protocol is not None:
            binding['worker_protocol'] = worker_protocol
        if worker_protocol in (7, 8):
            binding['worker_controls'] = worker_controls
        if worker_credentials_sha256 is not None:
            credentials = self._credential_identity()
            if credentials is None or worker_credentials_sha256 != getattr(self, 'credentials_sha256', None):
                raise ValueError('credentialed worker requires its original side channel')
            binding['credentials'] = credentials
            binding['worker_credentials_sha256'] = worker_credentials_sha256
        if not binding['daemon']:
            raise ContainerPending('daemon did not provide an identity')
        try:
            immutable_write_bytes(self.root / 'binding.json', json.dumps(binding).encode())
        except FileExistsError as exc:
            refuse_reserved(self.root / 'binding.json', observed=True)
        proxy_args = (['--mount', 'type=bind,src=' + self.model_proxy_socket +
                       ',dst=/run/model-proxy.sock,readonly'] if self.model_proxy_socket else [])
        if worker_credentials_sha256 is not None:
            proxy_args += ['--mount', 'type=bind,src=' + binding['credentials']['path'] +
                           ',dst=/run/model-credentials.sock,readonly']
        for source, target in mounts:
            proxy_args += ['--mount', f'type=bind,src={source},dst={target},readonly']
        identity = self._control('create', '--name', name, '--label', 'infermatrix.execution=' + name,
            '--network=none', '--read-only', '--user=65534:65534', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--pids-limit=64', '--memory=512m',
            '--cpus=1', '--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m',
            '--interactive', *proxy_args, '--entrypoint', argv[0], image, *argv[1:]).strip()
        if not re.fullmatch(r'[0-9a-f]{64}', identity):
            raise ContainerPending('unconfirmed created container identity; retain binding')
        immutable_write_bytes(self.root / 'container.json', json.dumps({
            'name': name, 'daemon': binding['daemon'], 'id': identity}).encode())
        if deadline <= time.monotonic() or (cancel is not None and cancel.is_set()):
            raise ContainerPending('execution cancelled before container start; retain binding')
        if self._proxy_identity() != binding['proxy']:
            raise ContainerPending('model proxy changed before container start')
        if worker_credentials_sha256 is not None and self._credential_identity() != binding['credentials']:
            raise ContainerPending('credential channel changed before container start')
        if self._inspect(binding)['State'].get('Status') != 'created':
            raise ContainerPending('container is not in its original unstarted state')
        # A crash before/after this immutable intent cannot authorize a restart.
        immutable_write_bytes(self.root / 'start-intent.json',
                              json.dumps({'name': name, 'id': identity}).encode())
        stopped_live = []
        try:
            outcome = run_process(self.command + ['start', '--attach', '--interactive', identity],
                cwd=self.root, env=os.environ.copy(), deadline=deadline, input=input,
                on_line=on_line, cancel=cancel,
                cleanup=lambda: stopped_live.append(self._stop(binding)))
        except BaseException as exc:
            # Popen may fail before the supervisor acquires ownership.
            if not hasattr(exc, 'process_cleanup_error'):
                try:
                    self._stop(binding)
                except BaseException as cleanup_error:
                    exc.process_cleanup_error = cleanup_error
            raise
        state = self._inspect(binding)['State']
        if outcome.reason is None and (
                any(stopped_live) or state.get('Status') != 'exited'
                or type(state.get('ExitCode')) is not int
                or state['ExitCode'] != outcome.returncode):
            raise ContainerPending('client completion does not establish container completion')
        atomic_write_bytes(self.root / 'result.json',
            json.dumps({'name': name, 'id': identity, 'outcome': asdict(outcome)}).encode(), mode=0o600)
        return outcome
