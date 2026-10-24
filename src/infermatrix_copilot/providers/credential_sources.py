"""Trusted native login references; backends receive only opaque snapshots."""
import os
from pathlib import Path
import stat

from .worker_credentials import MAX_BUNDLE_BYTES


def normalize_source(value):
    from .registry import PROVIDERS
    if (not isinstance(value, dict) or not {'provider', 'path'} <= set(value)
            or set(value) - {'provider', 'path', 'owner_uid'}
            or not isinstance(value['provider'], str) or value['provider'] not in PROVIDERS
            or 'native_credentials' not in PROVIDERS[value['provider']].capabilities
            or not isinstance(value['path'], str) or not Path(value['path']).is_absolute()
            or any(c in value['path'] for c in '\0\r\n')):
        raise ValueError('invalid native credential source')
    owner = value.get('owner_uid')
    if owner is not None and (type(owner) is not int or owner < 0):
        raise ValueError('invalid native credential owner')
    return {key: item for key, item in value.items() if item is not None}


def read_private_login(path, *, owner_uid=None):
    expected = os.getuid() if owner_uid is None else owner_uid
    if type(expected) is not int or expected < 0:
        raise ValueError('invalid native credential owner')
    fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        details = os.fstat(handle.fileno())
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != expected
                or details.st_mode & 0o077 or details.st_size > MAX_BUNDLE_BYTES):
            raise ValueError('native login must be a private owned regular file')
        payload = handle.read(MAX_BUNDLE_BYTES + 1)
    if len(payload) > MAX_BUNDLE_BYTES:
        raise ValueError('native login exceeds the snapshot limit')
    return payload


def snapshot_source(source, *, options=None):
    from .provider_options import normalize_options
    from .registry import _construct_transport
    source = normalize_source(source)
    provider = source['provider']
    return _construct_transport(None, provider).credential_snapshot(source['path'],
        owner_uid=source.get('owner_uid'), options=normalize_options(provider, options or {}))
