"""Freeze host completion policy with a run; restore it before model dispatch."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from ..persistence import atomic_write_bytes, immutable_write_bytes
from ..providers.completion_profiles import CompletionPolicyError, normalize_completion_profiles
from .. import run_status

PIN_FILE = 'native-completion.json'
PIN_FIELD = 'native_completion_sha256'
_BACKEND_FIELDS = ('strict_backend', 'strict_backend_model', 'strict_backend_cli', 'strict_backend_timeout_s')


def _backend_settings(value):
    """Decode the non-secret native identity, using the canonical provider rule."""
    from ..config import Settings
    if (not isinstance(value, dict) or set(value) != set(_BACKEND_FIELDS)
            or any(not isinstance(value[key], str) for key in _BACKEND_FIELDS[:-1])):
        raise ValueError('invalid original native backend identity')
    Settings._validate_strict_backend(value['strict_backend'])
    timeout = value['strict_backend_timeout_s']
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('invalid original native deadline')
    return value


def backend_environment(settings):
    """The worker receives the same effective native settings as its host."""
    return {key.upper(): str(getattr(settings, key))
            for key in (*_BACKEND_FIELDS, 'strict_backend_effort')}


def create_completion_pin(settings, run_dir):
    profiles = normalize_completion_profiles(getattr(settings, 'native_completion_profiles', {}))
    sessions = normalize_completion_profiles(getattr(settings, 'native_session_profiles', {}))
    effort = getattr(settings, 'strict_backend_effort', '')
    if not profiles and not sessions and not effort:
        return ''
    value = {'schema': 'run-completion/4', 'profiles': profiles,
             'session_profiles': sessions, 'strict_backend_effort': effort,
             'backend': _backend_settings({key: getattr(settings, key) for key in _BACKEND_FIELDS}),
             'trace_store_root': getattr(settings, 'trace_store_root', '')}
    payload = (json.dumps(value, sort_keys=True, ensure_ascii=False) + '\n').encode()
    immutable_write_bytes(Path(run_dir) / PIN_FILE, payload)
    return hashlib.sha256(payload).hexdigest()


def completion_settings_for_run(settings, run_dir, *, resuming=False, allow_create=False):
    run_dir = Path(run_dir)
    path, task_path = run_dir / PIN_FILE, run_dir / 'task.json'
    try:
        observed = run_status.read_status(run_dir)
        if observed is None and (run_dir / 'run_status.json').exists():
            raise ValueError('unreadable original run status')
        status = observed or {}
        task = json.loads(task_path.read_text()) if task_path.exists() else {}
        if not isinstance(task, dict):
            raise ValueError('invalid task record')
        markers = [row[PIN_FIELD] for row in (status, task) if PIN_FIELD in row]
        if markers:
            if (any(marker != markers[0] for marker in markers) or path.is_symlink()
                    or not isinstance(markers[0], str)):
                raise ValueError('changed completion pin authority')
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != markers[0]:
                raise ValueError('completion pin differs from original binding')
            value = json.loads(payload)
            version = value.get('schema')
            fields = {'schema', 'profiles', 'trace_store_root'}
            if version in ('run-completion/2', 'run-completion/3', 'run-completion/4'):
                fields.add('session_profiles')
            if version in ('run-completion/3', 'run-completion/4'):
                fields.add('strict_backend_effort')
            if version == 'run-completion/4':
                fields.add('backend')
            if set(value) != fields or version not in ('run-completion/1', 'run-completion/2', 'run-completion/3', 'run-completion/4'):
                raise ValueError('invalid completion pin')
            backend = _backend_settings(value['backend']) if version == 'run-completion/4' else {}
            profiles = normalize_completion_profiles(value['profiles'])
            sessions = normalize_completion_profiles(value.get('session_profiles', {}))
            trace_root = value['trace_store_root']
            effort = value.get('strict_backend_effort', '')
            if not isinstance(trace_root, str) or not isinstance(effort, str):
                raise ValueError('invalid original trace root')
        else:
            if path.exists() or path.is_symlink():
                raise ValueError('completion pin has no original authority')
            if (allow_create and not resuming and not status and
                    (getattr(settings, 'native_completion_profiles', {}) or getattr(settings, 'native_session_profiles', {})
                     or getattr(settings, 'strict_backend_effort', ''))):
                if not task_path.is_file():
                    raise ValueError('profile-enabled workflows require a persisted task')
                marker = create_completion_pin(settings, run_dir)
                task[PIN_FIELD] = marker
                atomic_write_bytes(task_path, (json.dumps(task, ensure_ascii=False) + '\n').encode())
                return settings
            # A legacy run never opted into a configured container. New host
            # configuration cannot reinterpret its original execution policy.
            profiles, sessions, trace_root = {}, {}, getattr(settings, 'trace_store_root', '')
            effort = ''
            backend = {}
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        raise CompletionPolicyError('cannot restore original completion execution: ' + str(exc)) from exc
    if (profiles == getattr(settings, 'native_completion_profiles', {})
            and sessions == getattr(settings, 'native_session_profiles', {})
            and effort == getattr(settings, 'strict_backend_effort', '')
            and all(getattr(settings, key) == value for key, value in backend.items())
            and trace_root == getattr(settings, 'trace_store_root', '')):
        return settings
    return settings.model_copy(deep=True, update={**backend, 'native_completion_profiles': profiles,
                                                 'native_session_profiles': sessions,
                                                 'strict_backend_effort': effort,
                                                 'trace_store_root': trace_root})
