"""Public provider facts and bounded readiness probes, derived from the registry."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import shutil
import subprocess
from types import SimpleNamespace

from ..contracts.models import InvalidRequestError
from .registry import PROVIDERS


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    kind: str
    display: str
    execution_modes: tuple[str, ...]
    cli_candidates: tuple[str, ...]
    capabilities: tuple[str, ...]
    default_model: str
    supports_model_override: bool

    def to_dict(self):
        return asdict(self)


def provider_descriptor(provider_id: str) -> ProviderDescriptor:
    """Describe implementation support; callers still authorize deployment use."""
    spec = PROVIDERS.get(provider_id)
    if spec is None:
        raise InvalidRequestError(f"unknown provider {provider_id!r}")
    return ProviderDescriptor(spec.id, spec.kind, spec.display, spec.execution_modes,
        spec.cli_names, tuple(sorted(spec.capabilities)), spec.default_model, True)


def provider_descriptors() -> tuple[ProviderDescriptor, ...]:
    return tuple(provider_descriptor(name) for name in PROVIDERS)


def _provider_argv(descriptor, command):
    if command is not None and not isinstance(command, (list, tuple)):
        raise InvalidRequestError("provider command must be a nonempty string array")
    argv = list(command) if command else [next(
        (name for name in descriptor.cli_candidates if shutil.which(name)), descriptor.cli_candidates[0])]
    if not argv or any(not isinstance(arg, str) or not arg for arg in argv):
        raise InvalidRequestError("provider command must be a nonempty string array")
    return argv


def provider_version(provider_id: str, command=None, *, timeout_s=30) -> str:
    """Return native CLI version text without starting a model session."""
    descriptor = provider_descriptor(provider_id)
    if not descriptor.cli_candidates:
        raise InvalidRequestError(f"{provider_id} has no native CLI version probe")
    argv = _provider_argv(descriptor, command)
    argv[0] = shutil.which(argv[0]) or argv[0]
    from .json_session import readonly_environment
    from .registry import transport_for_id

    adapter = transport_for_id(SimpleNamespace(), provider_id)
    result = adapter.version_probe(argv, env=readonly_environment(), timeout_s=timeout_s)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return (result.stdout or result.stderr).strip()


def provider_readiness(provider_id: str, command=None, *, timeout_s=15) -> tuple[str, ...]:
    """Probe CLI availability/authentication without starting a model session."""
    descriptor = provider_descriptor(provider_id)
    if descriptor.kind == "api" or not descriptor.cli_candidates:
        return ()  # API credentials and embedded SDKs are checked by runtime readiness.
    argv = _provider_argv(descriptor, command)
    executable = shutil.which(argv[0])
    if executable is None:
        return (f"{provider_id} CLI not found: {argv[0]}",)
    probe = PROVIDERS[provider_id].readiness_args
    if not probe:
        return ()
    from .json_session import readonly_environment

    from .registry import transport_for_id

    adapter = transport_for_id(SimpleNamespace(), provider_id)
    try:
        result = adapter.auth_probe([executable, *argv[1:]], env=readonly_environment(), timeout_s=timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        return (f"{provider_id} readiness probe failed: {type(exc).__name__}",)
    issue = adapter.auth_status_issue(result.returncode, result.stdout + result.stderr)
    return (issue,) if issue else ()


def provider_worker_limit() -> int:
    from ..config import MAX_STRICT_WORKERS_LIMIT

    return MAX_STRICT_WORKERS_LIMIT
