"""One command/result contract for local and remote sandbox implementations."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import math

AGENT_PROTOCOL_VERSION = 2
EXECUTION_PROTOCOL_VERSION = 5


def execution_controls(idle_timeout_s=None, termination_grace_s=0):
    if idle_timeout_s is not None and (type(idle_timeout_s) not in (int, float)
            or not math.isfinite(idle_timeout_s) or idle_timeout_s <= 0):
        raise ValueError('idle deadline must be positive and finite')
    if (type(termination_grace_s) not in (int, float) or not math.isfinite(termination_grace_s)
            or not 0 <= termination_grace_s <= 300):
        raise ValueError('termination grace must be finite and bounded')
    return ({'idle_timeout_s': idle_timeout_s, 'termination_grace_s': termination_grace_s}
            if idle_timeout_s is not None or termination_grace_s else None)


@dataclass(frozen=True)
class CancellationReceipt:
    requested: bool
    terminated: bool
    state: str

    @classmethod
    def from_dict(cls, value):
        if (not isinstance(value, dict) or set(value) != {'requested', 'terminated', 'state'}
                or type(value['requested']) is not bool or type(value['terminated']) is not bool
                or value['state'] not in ('terminal', 'pending', 'unknown')
                or value['terminated'] != (value['state'] == 'terminal')
                or not value['requested']):
            raise ValueError('invalid sandbox cancellation receipt')
        return cls(**value)


@dataclass(frozen=True)
class FileCancellation:
    path: Path

    def is_set(self):
        return self.path.exists() or self.path.is_symlink()


@dataclass(frozen=True)
class CombinedCancellation:
    signals: tuple

    def is_set(self):
        return any(signal is not None and signal.is_set() for signal in self.signals)


class SandboxPending(RuntimeError):
    """The original execution must be reconciled, never replaced implicitly."""
    allow_fallback = False

    def __init__(self, message, *, prior_execution=False):
        super().__init__(message)
        self.native_outcome_pending = prior_execution


def refuse_reserved(path, *, observed=False):
    """Original authority wins at preflight, dispatch and reservation races."""
    if observed or path.exists() or path.is_symlink():
        raise SandboxPending('execution already reserved; reconcile original binding', prior_execution=True)


class SandboxExecution(ABC):
    """A host-provisioned sandbox; a plain host subprocess is not an adapter."""
    supports_credentials = False

    def with_credentials(self, credentials):
        raise ValueError('sandbox cannot bind native credentials')

    @abstractmethod
    def preflight(self, *, for_dispatch=True): ...

    def supported_input_protocol(self):
        from .inputs import BINARY_INPUT_PROTOCOL_VERSION
        return BINARY_INPUT_PROTOCOL_VERSION

    def require_inputs(self, value):
        from .inputs import input_protocol
        if value and input_protocol(value) > self.supported_input_protocol():
            raise ValueError('sandbox does not support frozen sandbox inputs')

    @abstractmethod
    def execute(self, cmd, *, cwd, env, deadline, input=None, on_line=None, cancel=None, frozen_inputs=None): ...

    @abstractmethod
    def result(self, cmd, *, cwd, env, input=None, recover=False, frozen_inputs=None): ...

    def read_result(self, *, recover=False):
        """Read this instance's original execution without reconstructing input.

        Implementations must retain their original location and execution
        binding. Missing evidence stays pending; this operation cannot dispatch.
        """
        raise ValueError('sandbox cannot read an original bound result')

    @abstractmethod
    def cancel(self) -> CancellationReceipt: ...

    def context(self):
        raise ValueError('sandbox cannot attest its execution context')

    def with_context(self, expected):
        raise ValueError('sandbox cannot bind an admitted execution context')


class SandboxCompletionProfile:
    """Shared completion binding; providers know neither Docker nor SSH."""
    sandbox: SandboxExecution
    native_cli: str

    def validate(self, *, for_dispatch=True):
        if not isinstance(self.sandbox, SandboxExecution):
            raise ValueError('completion requires a registered sandbox executor')
        self.sandbox.preflight(for_dispatch=for_dispatch)
        if not isinstance(self.native_cli, str) or not self.native_cli.startswith('/') or '\0' in self.native_cli:
            raise ValueError('native CLI must be an absolute sandbox path')

    def execute(self, cmd, **kwargs):
        return self.sandbox.execute(cmd, **kwargs)

    def get_result(self, cmd, **kwargs):
        self.validate(for_dispatch=False)
        return self.sandbox.result(cmd, **kwargs)

    def read_result(self, *, recover=False):
        self.validate(for_dispatch=False)
        return self.sandbox.read_result(recover=recover)

    def with_context(self, expected):
        return BoundSandboxProfile(self.sandbox.with_context(expected), self.native_cli)

    def with_credentials(self, credentials):
        return BoundSandboxProfile(self.sandbox.with_credentials(credentials), self.native_cli)


@dataclass(frozen=True)
class BoundSandboxProfile(SandboxCompletionProfile):
    sandbox: SandboxExecution
    native_cli: str = '/usr/local/bin/codex'
