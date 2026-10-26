"""One native profile-auth proof, bound to the next configured model dispatch.

This is a control operation, not a model call. Quota authorization and journals
remain with their domains. No credential material is stored in the admission.
"""
from copy import copy
import hashlib
import json
from pathlib import Path
import time
import uuid

from ..persistence import immutable_write_bytes
from ..sandbox.context import validate_context, verify_context
from .completion_profiles import (CompletionPolicyError, SandboxCompletionConfig,
                                  completion_config_dict, completion_profile_config, sandbox_profile_for_config,
                                  validate_completion_policy, configured_credential_source)


def _config_hash(config):
    return hashlib.sha256(json.dumps(completion_config_dict(config), sort_keys=True).encode()).hexdigest()


def _store(settings, recorder):
    from ..trace_store import TraceStore, current_store
    store = getattr(recorder, 'store', None) or current_store()
    if isinstance(store, TraceStore):
        return store
    path = getattr(settings, 'trace_store_root', '')
    return TraceStore(Path(path)) if path and Path(path).is_absolute() else None


def admit_subscription(settings, transport, *, recorder=None):
    """Return a fresh admitted transport, or None; never dispatch a model."""
    config = completion_profile_config(settings, transport.spec.id)
    store = _store(settings, recorder)
    if config is None or store is None:
        return None
    if configured_credential_source(config) is None:
        return None  # an image/host login is not an explicit admitted source
    started, probe_id = time.time(), uuid.uuid4().hex
    root = store.root / 'auth-probes' / probe_id
    proof, error, outcome = None, '', None
    try:
        validate_completion_policy(settings, transport)
        root.mkdir(parents=True, exist_ok=False)
        immutable_write_bytes(root / 'request.json', json.dumps({
            'schema': 'native-auth-probe/1', 'provider': transport.spec.id,
            'config': completion_config_dict(config), 'command': transport.authentication_command(config.native_cli),
            'model_calls': 0}, sort_keys=True).encode())
        profile = sandbox_profile_for_config(config, root / 'execution')
        outcome = transport.auth_probe(env={}, execution_profile=profile)
        if transport.subscription_auth_status(outcome.returncode, outcome.stdout + outcome.stderr):
            context = validate_context(profile.sandbox.context())
            credentials = context['credentials'] or {}
            if transport.subscription_credential_identity(credentials):
                verify_context(context, context)
                proof = {'schema': 'native-subscription-admission/1', 'provider': transport.spec.id,
                         'config_sha256': _config_hash(config), 'context': context,
                         'probe_id': probe_id, 'root': str(root.resolve()), 'store_root': str(store.root.resolve())}
                immutable_write_bytes(root / 'admission.json', json.dumps(proof, sort_keys=True).encode())
    except (OSError, ValueError, RuntimeError) as exc:
        error = type(exc).__name__  # caches/control data must not enter diagnostics
    store.append('auth_probe', inputs={'config_sha256': _config_hash(config)},
        outputs={}, model={'provider': transport.spec.id}, seconds=time.time() - started,
        result={'probe_id': probe_id, 'model_calls': 0, 'subscription': proof is not None,
                'returncode': getattr(outcome, 'returncode', None),
                'stdout_sha256': hashlib.sha256(getattr(outcome, 'stdout', '').encode()).hexdigest(),
                'stderr_sha256': hashlib.sha256(getattr(outcome, 'stderr', '').encode()).hexdigest()}, error=error)
    if proof is None:
        return None
    selected = copy(transport)
    selected.configured_subscription_admission = proof
    return selected


def admitted_context(settings, transport, config, *, store_root):
    """Revalidate original proof, configuration and expiry before paid dispatch."""
    proof = getattr(transport, 'configured_subscription_admission', None)
    if proof is None:
        return None
    try:
        root = Path(proof['root'])
        store_root = Path(store_root).resolve()
        # The admission can also use the caller's durable recorder. Its original
        # auth-probe root remains authoritative; no new proof is synthesized.
        path = root / 'admission.json'
        if (proof['store_root'] != str(store_root) or root.parent != store_root / 'auth-probes'
                or root.name != proof['probe_id']
                or root.is_symlink() or root.parent.is_symlink() or path.is_symlink()
                or path.read_text() != json.dumps(proof, sort_keys=True)
                or proof['schema'] != 'native-subscription-admission/1'
                or proof['provider'] != transport.spec.id or proof['config_sha256'] != _config_hash(config)):
            raise ValueError('changed admission')
        context = validate_context(proof['context'])
        credentials = context['credentials'] or {}
        if not transport.subscription_credential_identity(credentials):
            raise ValueError('unknown admitted account')
        verify_context(context, context)
        return context
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CompletionPolicyError('original subscription admission is unavailable or changed') from exc
