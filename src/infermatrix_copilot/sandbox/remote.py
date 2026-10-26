"""SSH location adapter. The remote agent uses the same local sandbox backend."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import time
import uuid
from copy import copy

from ..persistence import immutable_write_bytes
from ..process_execution import ProcessCleanupError, ProcessOutcome, run_process
from .base import AGENT_PROTOCOL_VERSION, CancellationReceipt, SandboxExecution, SandboxPending, refuse_reserved, execution_controls, EXECUTION_PROTOCOL_VERSION
from .context import ContextChanged, validate_context, verify_context
from .inputs import BINARY_INPUT_PROTOCOL_VERSION, INPUT_PROTOCOL_VERSION, SEALED_FILE_INPUT_PROTOCOL_VERSION, LEGACY_INPUT_PROTOCOL_VERSION, input_protocol, normalize_inputs


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class SSHLocation:
    kind: str
    host: str
    python: str
    state_root: str
    ssh: str = '/usr/bin/ssh'
    sudo: bool = False
    control_timeout_s: float = 15.0

    @classmethod
    def parse(cls, value):
        result = cls(**value)
        if (result.kind != 'ssh' or not isinstance(result.host, str)
                or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:\[\]-]*', result.host)
                or type(result.sudo) is not bool
                or type(result.control_timeout_s) not in (int, float)
                or not 0 < result.control_timeout_s < 300):
            raise ValueError('invalid SSH sandbox location')
        for path in (result.python, result.state_root, result.ssh):
            if not isinstance(path, str) or not Path(path).is_absolute() or any(c in path for c in '\0\r\n'):
                raise ValueError('sandbox location requires absolute paths')
        return result

    def command(self):
        remote = (['/usr/bin/sudo', '-n'] if self.sudo else []) + [self.python, '-m', 'infermatrix_copilot.sandbox_agent']
        return [self.ssh, '-o', 'BatchMode=yes', '--', self.host, shlex.join(remote)]


class RemoteSandboxExecution(SandboxExecution):
    def __init__(self, root, location, backend):
        self.root, self.location = Path(root), location
        self.backend = json.loads(canonical(backend))
        self._release = None
        self._input_protocol = None
        self._execution_controls = None
        self.expected_context = None

    def with_context(self, expected):
        selected = copy(self)
        selected.expected_context = validate_context(expected)
        return selected

    def context(self):
        return validate_context(self._invoke('context', self._read_binding()))

    def _read_binding(self):
        try:
            value = json.loads((self.root / 'remote-binding.json').read_text())
            if (value['version'] != 1 or value['location'] != asdict(self.location)
                    or value['backend'] != self.backend
                    or value.get('agent_protocol', 1) not in (1, AGENT_PROTOCOL_VERSION, LEGACY_INPUT_PROTOCOL_VERSION, SEALED_FILE_INPUT_PROTOCOL_VERSION, BINARY_INPUT_PROTOCOL_VERSION, INPUT_PROTOCOL_VERSION)
                    or not re.fullmatch(r'[0-9a-f]{32}', value['identity'])
                    or not re.fullmatch(r'[0-9a-f]{64}', value['release'])):
                raise ValueError('changed binding')
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SandboxPending('invalid original remote sandbox binding', prior_execution=True) from exc

    @staticmethod
    def _request(cmd, cwd, env, input, frozen_inputs=None, controls=None):
        if (not isinstance(cmd, (tuple, list)) or not cmd
                or any(not isinstance(arg, str) or '\0' in arg for arg in cmd)
                or not isinstance(cwd, str) or not isinstance(env, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items())
                or input is not None and not isinstance(input, str)):
            raise ValueError('invalid sandbox command')
        request = {'cmd': list(cmd), 'cwd': cwd, 'env': dict(env), 'input': input}
        frozen_inputs = normalize_inputs(frozen_inputs)
        if frozen_inputs is not None:
            request['frozen_inputs'] = frozen_inputs
        if controls is not None:
            request['controls'] = controls
            request.setdefault('frozen_inputs', None)
        return request

    def _invoke(self, operation, binding=None, *, deadline=None, on_line=None, cancel=None):
        protocol = binding.get('agent_protocol', 1) if binding is not None else AGENT_PROTOCOL_VERSION
        request = {'version': protocol, 'operation': operation, 'backend': self.backend,
                   'state_root': self.location.state_root, 'binding': binding}
        terminals = []
        def event(channel, line):
            if channel != 'stdout':
                return
            frame = json.loads(line)
            if terminals:
                raise ValueError('remote sandbox emitted data after its terminal response')
            if frame.get('type') == 'line' and frame.get('channel') in ('stdout', 'stderr'):
                if not isinstance(frame.get('text'), str):
                    raise ValueError('invalid sandbox stream')
                if on_line is not None:
                    on_line(frame['channel'], frame['text'])
            elif frame.get('type') in ('result', 'error'):
                terminals.append(frame)
            else:
                raise ValueError('invalid remote sandbox frame')

        def cleanup():
            if operation == 'execute' and (not terminals or terminals[-1]['type'] != 'result'):
                receipt = self.cancel()
                if not receipt.terminated:
                    raise SandboxPending('original remote termination is unconfirmed', prior_execution=True)

        try:
            outcome = run_process(self.location.command(), cwd=self.root if self.root.is_dir() else tempfile.gettempdir(),
                env=os.environ.copy(), input=canonical(request), on_line=event,
                deadline=deadline or time.monotonic() + self.location.control_timeout_s,
                cancel=cancel, cleanup=cleanup if operation == 'execute' else None)
            if outcome.reason or outcome.returncode or len(terminals) != 1:
                raise ValueError('remote response is incomplete')
            frame = terminals[0]
            if frame['type'] == 'error':
                raise ValueError('remote sandbox refused the operation')
            return frame['value']
        except (OSError, ValueError, KeyError, TypeError, ProcessCleanupError) as exc:
            raise SandboxPending('remote sandbox response unknown; reconcile original execution',
                                 prior_execution=operation != 'preflight') from exc

    def preflight(self, *, for_dispatch=True, deadline=None):
        if not for_dispatch:
            return
        refuse_reserved(self.root / 'remote-binding.json')
        if not Path(self.location.ssh).is_file() or not os.access(self.location.ssh, os.X_OK):
            raise ValueError('SSH executable is unavailable')
        value = self._invoke('preflight', deadline=min(deadline, time.monotonic() + self.location.control_timeout_s)
                             if deadline is not None else None)
        self._execution_controls = value.get('execution_controls')
        release = value.get('release')
        if (not isinstance(release, str) or not re.fullmatch(r'[0-9a-f]{64}', release)
                or value.get('protocol') != AGENT_PROTOCOL_VERSION):
            raise ValueError('remote agent did not attest its release')
        self._release = release
        self._input_protocol = value.get('frozen_inputs_protocol')
        if self.expected_context is not None:
            if value.get('context_binding') is not True:
                raise ContextChanged('remote agent cannot bind admitted credentials')
            verify_context(self.expected_context, self.expected_context)
            if release != self.expected_context['release']:
                raise ContextChanged('admitted remote release changed')

    def supported_input_protocol(self):
        value = getattr(self, '_input_protocol', None)
        return value if type(value) is int else 0

    def _outcome(self, value):
        try:
            return ProcessOutcome.from_dict(value)
        except ValueError as exc:
            raise SandboxPending('invalid remote sandbox outcome', prior_execution=True) from exc

    def _save_result(self, binding, value):
        result = self._outcome(value)
        immutable_write_bytes(self.root / 'remote-result.json', canonical({
            'binding_sha256': hashlib.sha256(canonical(binding).encode()).hexdigest(),
            'outcome_sha256': hashlib.sha256(canonical(asdict(result)).encode()).hexdigest(),
            'outcome': asdict(result)}).encode(), exist_ok=True)
        return result

    def execute(self, cmd, *, cwd, env, deadline, input=None, on_line=None, cancel=None, frozen_inputs=None, idle_timeout_s=None, termination_grace_s=0):
        refuse_reserved(self.root / 'remote-binding.json')
        controls = execution_controls(idle_timeout_s, termination_grace_s)
        request = self._request(cmd, cwd, env, input, frozen_inputs, controls)
        if deadline <= time.monotonic() or cancel is not None and cancel.is_set():
            raise ValueError('sandbox execution cancelled before reservation')
        self.preflight(deadline=deadline)
        if controls is not None and self._execution_controls != 1:
            raise ValueError('remote agent does not support execution controls')
        if request.get('frozen_inputs') is not None and (type(self._input_protocol) is not int
                or not input_protocol(request['frozen_inputs']) <= self._input_protocol <= INPUT_PROTOCOL_VERSION):
            raise ValueError('remote agent does not support frozen sandbox inputs')
        if deadline <= time.monotonic() or cancel is not None and cancel.is_set():
            raise ValueError('sandbox execution cancelled before reservation')
        self.root.mkdir(parents=True, exist_ok=True)
        binding = {'version': 1, 'identity': uuid.uuid4().hex, 'location': asdict(self.location),
                   'agent_protocol': EXECUTION_PROTOCOL_VERSION if controls is not None else input_protocol(request['frozen_inputs']) if 'frozen_inputs' in request else AGENT_PROTOCOL_VERSION,
                   'backend': self.backend, 'request': request, 'release': self._release,
                   'timeout_s': deadline - time.monotonic()}
        if self.expected_context is not None:
            binding['expected_context'] = self.expected_context
        try:
            immutable_write_bytes(self.root / 'remote-binding.json', canonical(binding).encode())
        except FileExistsError as exc:
            refuse_reserved(self.root / 'remote-binding.json', observed=True)
        return self._save_result(binding, self._invoke('execute', binding, deadline=deadline,
                                                     on_line=on_line, cancel=cancel))

    def result(self, cmd, *, cwd, env, input=None, recover=False, frozen_inputs=None, idle_timeout_s=None, termination_grace_s=0):
        binding = self._read_binding()
        if binding['request'] != self._request(cmd, cwd, env, input, frozen_inputs, execution_controls(idle_timeout_s, termination_grace_s)):
            raise SandboxPending('request differs from original remote execution', prior_execution=True)
        return self._result(binding, recover=recover)

    def read_result(self, *, recover=False):
        return self._result(self._read_binding(), recover=recover)

    def _result(self, binding, *, recover):
        path = self.root / 'remote-result.json'
        if path.exists() or path.is_symlink():
            try:
                value = json.loads(path.read_text())
                if value['binding_sha256'] != hashlib.sha256(canonical(binding).encode()).hexdigest():
                    raise ValueError('changed result')
                if value['outcome_sha256'] != hashlib.sha256(canonical(value['outcome']).encode()).hexdigest():
                    raise ValueError('changed process output')
                return self._outcome(value['outcome'])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise SandboxPending('invalid original remote result', prior_execution=True) from exc
        if not recover:
            raise SandboxPending('remote result requires original-worker reconciliation', prior_execution=True)
        return self._save_result(binding, self._invoke('result', binding))

    def cancel(self):
        binding = self._read_binding()
        value = self._invoke('cancel', binding)
        if binding.get('agent_protocol', 1) == 1:
            # Old replies acknowledged a request, never proved termination.
            if value != {'cancel_requested': True}:
                raise SandboxPending('invalid legacy cancellation acknowledgment', prior_execution=True)
            return CancellationReceipt(True, False, 'unknown')
        try:
            return CancellationReceipt.from_dict(value)
        except ValueError as exc:
            raise SandboxPending('invalid remote cancellation receipt', prior_execution=True) from exc
