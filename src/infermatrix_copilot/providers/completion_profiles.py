"""Trusted host configuration for per-attempt completion execution.

Configuration never comes from model requests. A configured profile changes
containment, not the registry's no-tools or subscription billing capabilities.
Each actual profile belongs to one durable native archive; no cached transport
is mutated and an original profile descriptor is never overwritten.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path

from ..persistence import immutable_write_bytes
from .container_execution import ContainerExecution, validate_container_image
from ..sandbox import BoundSandboxProfile
from ..sandbox.base import refuse_reserved
from ..sandbox.backends import SandboxBackendConfig, parse_backend_config, register_backend
from ..sandbox.docker import DockerSandboxExecution
from ..sandbox.remote import RemoteSandboxExecution, SSHLocation


class CompletionPolicyError(ValueError):
    allow_fallback = False


@dataclass(frozen=True)
class DockerCompletionConfig(SandboxBackendConfig):
    kind: str
    docker: str
    socket: str
    image: str
    native_cli: str = '/usr/local/bin/codex'
    model_proxy_socket: str | None = None
    control_timeout_s: float = 15.0
    codex_auth_file: str | None = None
    codex_auth_owner_uid: int | None = None

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict):
            raise CompletionPolicyError('completion profile must be an object')
        try:
            result = cls(**value)
        except TypeError as exc:
            raise CompletionPolicyError('invalid completion profile fields') from exc
        if result.kind != 'docker':
            raise CompletionPolicyError('unsupported completion profile kind')
        for name in ('docker', 'socket', 'native_cli', 'model_proxy_socket', 'codex_auth_file'):
            path = getattr(result, name)
            if name in ('model_proxy_socket', 'codex_auth_file') and path is None:
                continue
            if not isinstance(path, str) or not Path(path).is_absolute() or any(c in path for c in '\0\r\n,'):
                raise CompletionPolicyError(f'{name} must be an absolute local path')
        if result.model_proxy_socket == result.socket:
            raise CompletionPolicyError('model proxy must not be the Docker daemon socket')
        if result.codex_auth_owner_uid is not None and (type(result.codex_auth_owner_uid) is not int
                or result.codex_auth_owner_uid < 0 or result.codex_auth_file is None):
            raise CompletionPolicyError('credential owner requires an auth file and a nonnegative UID')
        if (type(result.control_timeout_s) not in (int, float)
                or not math.isfinite(result.control_timeout_s) or result.control_timeout_s <= 0):
            raise CompletionPolicyError('container control timeout must be positive and finite')
        try:
            validate_container_image(result.image)
        except ValueError as exc:
            raise CompletionPolicyError(str(exc)) from exc
        return result

    def profile(self, root, *, credentials=None):
        return BoundSandboxProfile(DockerSandboxExecution(ContainerExecution(root, docker=self.docker,
            socket=self.socket, control_timeout=self.control_timeout_s,
            model_proxy_socket=self.model_proxy_socket), self.image, credentials), self.native_cli)

    def prepare(self, root, *, for_dispatch=True):
        credentials = None
        if for_dispatch:
            refuse_reserved(Path(root) / 'binding.json')
            if not Path(self.docker).is_file() or not os.access(self.docker, os.X_OK):
                raise CompletionPolicyError('configured Docker executable is unavailable')
            if not Path(self.socket).is_socket():
                raise CompletionPolicyError('configured Docker daemon socket is unavailable')
            if self.codex_auth_file is not None:
                if self.model_proxy_socket is None:
                    raise CompletionPolicyError('credentialed Codex completion requires a model proxy')
                from .credential_sources import snapshot_source
                try:
                    credentials = snapshot_source({'provider': 'codex', 'path': self.codex_auth_file,
                                                   'owner_uid': self.codex_auth_owner_uid})
                except (OSError, ValueError) as exc:
                    raise CompletionPolicyError('configured native Codex login is unavailable') from exc
        profile = self.profile(root, credentials=credentials)
        profile.validate(for_dispatch=for_dispatch)
        return profile


register_backend('docker', DockerCompletionConfig)


@dataclass(frozen=True)
class SandboxCompletionConfig:
    """Backend and execution location are independent trusted host choices."""
    backend: SandboxBackendConfig
    location: dict
    kind: str = 'sandbox'
    policy: dict | None = None
    provider_options: dict | None = None
    credentials_source: dict | None = None

    @classmethod
    def parse(cls, value):
        if set(value) - {'kind', 'backend', 'location', 'policy', 'provider_options', 'credentials_source'} or value.get('kind') != 'sandbox':
            raise CompletionPolicyError('invalid sandbox completion fields')
        try:
            backend = parse_backend_config(value.get('backend'))
        except ValueError as exc:
            raise CompletionPolicyError('invalid sandbox backend') from exc
        location = value.get('location', {'kind': 'local'})
        options = value.get('provider_options')
        if options is not None and not isinstance(options, dict):
            raise CompletionPolicyError('provider options must be an object')
        source = value.get('credentials_source')
        if source is not None:
            from .credential_sources import normalize_source
            try:
                source = normalize_source(source)
            except ValueError as exc:
                raise CompletionPolicyError(str(exc)) from exc
            if getattr(backend, 'codex_auth_file', None) is not None:
                raise CompletionPolicyError('choose one native credential source')
        if not isinstance(location, dict):
            raise CompletionPolicyError('sandbox location must be an object')
        from .codex_policy import normalize_policy
        try:
            policy = normalize_policy(value.get('policy'))
        except ValueError as exc:
            raise CompletionPolicyError('invalid native completion policy') from exc
        if location == {'kind': 'local'}:
            return cls(backend, dict(location), policy=policy, provider_options=options, credentials_source=source)
        try:
            location = asdict(SSHLocation.parse(location))
        except (TypeError, ValueError) as exc:
            raise CompletionPolicyError('invalid sandbox location') from exc
        return cls(backend, location, policy=policy, provider_options=options, credentials_source=source)

    @property
    def native_cli(self):
        return self.backend.native_cli

    @property
    def remote(self):
        return self.location['kind'] == 'ssh'

    def profile(self, root, *, credentials=None):
        if not self.remote:
            return self.backend.profile(root, credentials=credentials)
        if credentials is not None:
            raise CompletionPolicyError('remote credentials must be prepared on the execution host')
        backend = asdict(self.backend)
        if self.credentials_source is not None:
            backend = {'kind': 'sandbox', 'backend': backend, 'location': {'kind': 'local'},
                       'credentials_source': self.credentials_source}
            if self.provider_options is not None:
                backend['provider_options'] = self.provider_options
        return BoundSandboxProfile(RemoteSandboxExecution(root, SSHLocation.parse(self.location),
                                   backend), self.native_cli)


def parse_completion_config(value):
    if isinstance(value, dict) and value.get('kind') == 'sandbox':
        return SandboxCompletionConfig.parse(value)
    try:
        return parse_backend_config(value)
    except ValueError as exc:
        raise CompletionPolicyError('invalid completion backend') from exc


def completion_config_dict(config):
    """Preserve old serialization when optional provider policy is absent."""
    value = asdict(config)
    if isinstance(config, SandboxCompletionConfig) and value.get('policy') is None:
        value.pop('policy')
    if isinstance(config, SandboxCompletionConfig) and value.get('provider_options') is None:
        value.pop('provider_options')
    if isinstance(config, SandboxCompletionConfig) and value.get('credentials_source') is None:
        value.pop('credentials_source')
    return value


def normalize_completion_profiles(value):
    from .registry import PROVIDERS
    if not isinstance(value, dict) or any(provider not in PROVIDERS
            or 'sandbox_completion' not in PROVIDERS[provider].capabilities for provider in value):
        raise CompletionPolicyError('provider does not support configured completion profiles')
    result = {}
    for provider, config in value.items():
        if provider != 'codex':
            backend = config.get('backend', config) if isinstance(config, dict) else {}
            if not isinstance(backend, dict) or 'native_cli' not in backend:
                raise CompletionPolicyError('configured provider requires an explicit native_cli')
            if backend.get('codex_auth_file') is not None or config.get('policy') is not None:
                raise CompletionPolicyError('provider does not support the selected credential or native policy')
        normalized = completion_config_dict(parse_completion_config(config))
        if 'credentials_source' in normalized and normalized['credentials_source']['provider'] != provider:
            raise CompletionPolicyError('credential source differs from the selected provider')
        if 'provider_options' in normalized:
            from .provider_options import normalize_options
            try:
                normalized['provider_options'] = normalize_options(provider, normalized['provider_options'])
            except ValueError as exc:
                raise CompletionPolicyError(str(exc)) from exc
        if 'codex_auth_file' in normalized and normalized['codex_auth_file'] is None:
            normalized.pop('codex_auth_file')  # preserve legacy profile and init digests
        if 'codex_auth_owner_uid' in normalized and normalized['codex_auth_owner_uid'] is None:
            normalized.pop('codex_auth_owner_uid')
        result[provider] = normalized
    return result


def completion_profile_config(settings, provider, *, session=False):
    values = getattr(settings, 'native_session_profiles' if session else 'native_completion_profiles', {}) or {}
    normalized = normalize_completion_profiles(values)
    return parse_completion_config(normalized[provider]) if provider in normalized else None


def local_sandbox_profile(config, root, *, for_dispatch=True):
    """The same backend/credential composition runs on either execution host."""
    source = getattr(config, 'credentials_source', None)
    options = getattr(config, 'provider_options', None)
    if isinstance(config, SandboxCompletionConfig):
        if config.remote:
            raise CompletionPolicyError('nested remote sandbox execution is unsupported')
        config = config.backend
    profile = config.prepare(root, for_dispatch=for_dispatch)
    if source is not None and for_dispatch:
        if not profile.sandbox.supports_credentials:
            raise CompletionPolicyError('sandbox backend cannot receive native credentials')
        from .credential_sources import snapshot_source
        profile = profile.with_credentials(snapshot_source(source, options=options))
    return profile


def configured_credential_source(config):
    source = getattr(config, 'credentials_source', None)
    if source is not None:
        return source
    backend = config.backend if isinstance(config, SandboxCompletionConfig) else config
    path = getattr(backend, 'codex_auth_file', None)
    if path is not None:
        from .credential_sources import normalize_source
        return normalize_source({'provider': 'codex', 'path': path,
                                 'owner_uid': getattr(backend, 'codex_auth_owner_uid', None)})
    return None


def sandbox_profile_for_config(config, root, *, for_dispatch=True):
    """One host composition for model, auth, configuration and recovery."""
    return (config.profile(root) if isinstance(config, SandboxCompletionConfig) and config.remote
            else local_sandbox_profile(config, root, for_dispatch=for_dispatch))


def validate_completion_policy(settings, transport, *, require_trace=False, session=False):
    """Preflight the selected mechanism without reserving or starting a worker."""
    provider = getattr(getattr(transport, 'spec', None), 'id', '')
    config = completion_profile_config(settings, provider, session=session)
    if config is None:
        transport.validate_completion()
        return None
    from .registry import _construct_transport
    if (not isinstance(transport, type(_construct_transport(settings, provider)))
            or getattr(transport, 'runner', None) is not None):
        raise CompletionPolicyError('configured containment requires the canonical native transport')
    if session and 'sandbox_session' not in transport.spec.capabilities:
        raise CompletionPolicyError('provider does not support configured session execution')
    if getattr(transport, 'completion_profile', None) is not None:
        raise CompletionPolicyError('choose an explicit or configured completion profile')
    if getattr(transport, 'session_profile', None) is not None:
        raise CompletionPolicyError('a prepared session profile cannot be rebound')
    if require_trace:
        from ..trace_store import current_store
        root = getattr(settings, 'trace_store_root', '')
        if current_store() is None and (not root or not Path(root).expanduser().is_absolute()):
            raise CompletionPolicyError('configured completion requires an absolute TRACE_STORE_ROOT')
    try:
        if isinstance(config, SandboxCompletionConfig) and config.remote:
            config.profile(Path('/unused-completion-preflight')).validate()
        else:
            local_sandbox_profile(config, Path('/unused-completion-preflight'))
    except CompletionPolicyError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise CompletionPolicyError('configured sandbox preflight failed') from exc
    return config


def prepare_completion_transport(settings, transport, archive, *, request=None, session_request=None):
    session = session_request is not None
    config = validate_completion_policy(settings, transport, session=session)
    if config is None:
        return transport
    from copy import copy
    from ..trace_store import NativeCallArchive
    if (not isinstance(archive, NativeCallArchive) or archive.path.is_symlink()
            or archive.path.parent.is_symlink()
            or not archive.path.resolve().is_relative_to(archive.store.root.resolve() / 'attempts')):
        raise CompletionPolicyError('configured completion requires a durable native attempt archive')
    descriptor = {'schema': 'completion-execution/1', 'provider': transport.spec.id,
                  'native_attempt_id': archive.id, 'execution_root': str(archive.path.resolve() / 'execution'),
                  'config': completion_config_dict(config)}
    if session:
        descriptor['invocation_mode'] = 'session'
    root = archive.path.resolve() / 'execution'
    profile = sandbox_profile_for_config(config, root)
    from .subscription_admission import admitted_context
    # A completion admission cannot attest the account of an agent session.
    expected = None if session else admitted_context(settings, transport, config, store_root=archive.store.root)
    required = getattr(transport, 'required_execution_context', None)
    if required is not None:
        from ..sandbox.context import verify_context
        if expected is not None:
            verify_context(expected, required)
        else:
            expected = required
        descriptor['required_execution_context'] = required
    if expected is not None:
        try:
            profile = profile.with_context(expected)
        except ValueError as exc:
            raise CompletionPolicyError('admitted execution context changed before dispatch') from exc
        if getattr(transport, 'configured_subscription_admission', None) is not None:
            descriptor['subscription_admission'] = transport.configured_subscription_admission
    credentials = getattr(profile.sandbox, 'credentials', None)
    if credentials is not None:
        descriptor['credentials'] = credentials.metadata
    # The host selects this optional provider policy. It is part of the same
    # frozen profile and run/init pin, never a model-supplied sandbox setting.
    from .provider_options import bind_transport
    selected = bind_transport(transport, settings, config)
    if isinstance(config, SandboxCompletionConfig) and config.policy is not None:
        from .codex_policy import CodexCompletionPolicy
        from ..sandbox.context import validate_context
        policy = CodexCompletionPolicy.build(config.policy)
        model = (request or {}).get('model') or settings.strict_backend_model
        effort = (request or {}).get('effort') or ''
        arguments = policy.arguments(model=model, effort=effort)
        allowed_mcp = []
        policy_inputs = policy.inputs()
        if session:
            from ..sandbox.inputs import merge_inputs
            from .codex import _BRIDGE_SERVER
            policy_inputs = merge_inputs(policy_inputs, session_request.frozen_inputs)
            if session_request.bridge_spec_path is not None:
                arguments += transport._mcp_overrides(session_request.bridge_spec_path, contained=True)
                allowed_mcp = [_BRIDGE_SERVER]
        control_root = archive.path.resolve() / 'configuration'
        policy_request = {'schema': 'native-policy-request/2', 'provider': transport.spec.id,
            'native_attempt_id': archive.id, 'config': completion_config_dict(config),
            'identity': policy.identity, 'model': model, 'effort': effort,
            'control_root': str(control_root), 'expected_context': expected, 'frozen_inputs': policy.inputs()}
        if session:
            policy_request.update(schema='native-policy-request/3', frozen_inputs=policy_inputs,
                allowed_mcp=allowed_mcp,
                session_inputs_sha256=hashlib.sha256(json.dumps(session_request.frozen_inputs, sort_keys=True).encode()).hexdigest())
        request_bytes = (json.dumps(policy_request, sort_keys=True, ensure_ascii=False) + '\n').encode()
        immutable_write_bytes(archive.path / 'native-policy-request.json', request_bytes)
        control_profile = sandbox_profile_for_config(config, control_root)
        if expected is not None:
            control_profile = control_profile.with_context(expected)
        proof = transport.configuration_probe(control_profile, configuration_arguments=arguments,
                                               frozen_inputs=policy_inputs, include_requirements=True,
                                               **({'absolute_deadline': session_request.absolute_deadline} if session else {}))
        policy.validate_configuration(proof, model=model, effort=effort, allowed_mcp=allowed_mcp)
        context = validate_context(control_profile.sandbox.context())
        profile = profile.with_context(context)
        proof_record = {'schema': 'native-policy-proof/1',
            'request_sha256': hashlib.sha256(request_bytes).hexdigest(), 'proof': proof, 'context': context}
        proof_bytes = (json.dumps(proof_record, sort_keys=True, ensure_ascii=False) + '\n').encode()
        immutable_write_bytes(archive.path / 'native-policy-proof.json', proof_bytes)
        binding = {'request_sha256': hashlib.sha256(request_bytes).hexdigest(),
                   'proof_sha256': hashlib.sha256(proof_bytes).hexdigest()}
        archive.store.append('configuration_probe', inputs={'request_sha256': binding['request_sha256']},
            outputs={}, model={'provider': transport.spec.id},
            result={'native_attempt_id': archive.id, 'model_calls': 0,
                    'proof_sha256': binding['proof_sha256']})
        descriptor['native_policy'] = binding
        selected.native_completion_policy = policy
        selected.native_policy_binding = {'root': str(archive.path.resolve()), **binding}
    if session and session_request.bridge_spec_path is not None:
        control = sandbox_profile_for_config(config, archive.path.resolve() / 'bridge-control')
        expected_context = getattr(profile.sandbox, 'expected_context', None)
        if expected_context is not None:
            control = control.with_context(expected_context)
        proof, context = transport.bridge_probe(control, session_request)
        profile = profile.with_context(context)
        proof_bytes = (json.dumps({'schema': 'session-bridge-proof/1', 'proof': proof, 'context': context,
            'inputs_sha256': hashlib.sha256(json.dumps(session_request.frozen_inputs, sort_keys=True).encode()).hexdigest()},
            sort_keys=True) + '\n').encode()
        immutable_write_bytes(archive.path / 'bridge-proof.json', proof_bytes)
        descriptor['bridge_proof_sha256'] = hashlib.sha256(proof_bytes).hexdigest()
        archive.store.append('configuration_probe', inputs={}, outputs={}, model={'provider': transport.spec.id},
            result={'native_attempt_id': archive.id, 'model_calls': 0,
                    'bridge_proof_sha256': descriptor['bridge_proof_sha256']})
    payload = (json.dumps(descriptor, sort_keys=True, ensure_ascii=False) + '\n').encode()
    immutable_write_bytes(archive.path / 'execution-profile.json', payload)
    profile.validate()
    archive.event({'type': 'native.execution.profile', 'payload': {
        'native_attempt_id': archive.id, 'profile_sha256': hashlib.sha256(payload).hexdigest()}})
    # HarnessLLM and subscription admission may cache the host transport. The
    # one-shot execution profile must never be attached to that shared object.
    if session:
        selected.session_profile = profile
    else:
        selected.completion_profile = profile
    return selected


def validate_bound_native_policy(transport):
    """Revalidate immutable input/proof authority without rerunning a control."""
    policy = getattr(transport, 'native_completion_policy', None)
    binding = getattr(transport, 'native_policy_binding', None)
    if policy is None and binding is None:
        return None
    try:
        if policy is None or not isinstance(binding, dict):
            raise ValueError('missing native policy binding')
        root = Path(binding['root'])
        paths = [root / name for name in ('native-policy-request.json', 'native-policy-proof.json')]
        if root.is_symlink() or any(path.is_symlink() for path in paths):
            raise ValueError('changed native policy files')
        data = [path.read_bytes() for path in paths]
        if any(hashlib.sha256(payload).hexdigest() != binding[key]
               for payload, key in zip(data, ('request_sha256', 'proof_sha256'))):
            raise ValueError('changed native policy authority')
        request, proof = map(json.loads, data)
        if (request['schema'] not in ('native-policy-request/1', 'native-policy-request/2', 'native-policy-request/3')
                or request['identity'] != policy.identity
                or request['native_attempt_id'] != root.name
                or proof['schema'] != 'native-policy-proof/1'
                or proof['request_sha256'] != binding['request_sha256']):
            raise ValueError('invalid native policy proof')
        if (request['schema'] == 'native-policy-request/2' and request['frozen_inputs'] != policy.inputs()
                or request['schema'] == 'native-policy-request/1' and not policy.legacy_inputs):
            raise ValueError('changed original native policy projection')
        if request['schema'] == 'native-policy-request/3':
            from ..sandbox.inputs import merge_inputs
            from .codex import _BRIDGE_SERVER
            if (request.get('allowed_mcp') not in ([], [_BRIDGE_SERVER])
                    or merge_inputs(request['frozen_inputs'], policy.inputs()) != request['frozen_inputs']):
                raise ValueError('changed original native session policy')
        policy.validate_configuration(proof['proof'], model=request['model'], effort=request['effort'],
                                      allowed_mcp=request.get('allowed_mcp', ()))
        return request, proof
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CompletionPolicyError('original native completion policy is unavailable') from exc


def restore_completion_transport(settings, transport, archive_path, *, recover=False):
    """Bind a read-only transport to the original policy and worker records."""
    from copy import copy
    from .codex_policy import CodexCompletionPolicy
    original_root = Path(archive_path)
    root = original_root.resolve()
    try:
        if original_root.is_symlink() or (root / 'execution-profile.json').is_symlink():
            raise ValueError('changed original profile path')
        descriptor = json.loads((root / 'execution-profile.json').read_text())
        if (descriptor['schema'] != 'completion-execution/1' or descriptor['provider'] != transport.spec.id
                or descriptor['native_attempt_id'] != root.name or descriptor['execution_root'] != str(root / 'execution')):
            raise ValueError('changed original execution profile')
        config = parse_completion_config(descriptor['config'])
        selected = copy(transport)
        for name in ('native_completion_policy', 'native_policy_binding'):
            if hasattr(selected, name):
                delattr(selected, name)
        selected.completion_profile = sandbox_profile_for_config(config, root / 'execution', for_dispatch=False)
        selected.native_recovery_only = True
        if 'native_policy' in descriptor:
            if not isinstance(config, SandboxCompletionConfig) or config.policy is None:
                raise ValueError('missing original policy')
            original_request = json.loads((root / 'native-policy-request.json').read_text())
            selected.native_completion_policy = CodexCompletionPolicy.build(config.policy,
                legacy_inputs=original_request.get('schema') == 'native-policy-request/1')
            selected.native_policy_binding = {'root': str(root), **descriptor['native_policy']}
            request, proof = validate_bound_native_policy(selected)
            if request['control_root'] != str(root / 'configuration') or request['config'] != descriptor['config']:
                raise ValueError('changed original configuration control')
            profile = sandbox_profile_for_config(config, root / 'configuration', for_dispatch=False)
            observed = transport.get_configuration_result(profile,
                configuration_arguments=selected.native_completion_policy.arguments(model=request['model'], effort=request['effort']),
                frozen_inputs=selected.native_completion_policy.inputs(), recover=recover, include_requirements=True)
            if observed != proof['proof'] or profile.sandbox.context() != proof['context']:
                raise ValueError('original native control differs from its proof')
        return selected
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CompletionPolicyError('cannot restore original completion transport') from exc
