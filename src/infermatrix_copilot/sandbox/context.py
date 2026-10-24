"""Nonsecret execution/credential identity shared by preparation and observation."""
import json
import re
import time


class ContextChanged(ValueError):
    allow_fallback = False


def validate_context(value):
    if (not isinstance(value, dict) or set(value) != {'release', 'artifact', 'credentials'}
            or not isinstance(value['release'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['release'])
            or not isinstance(value['artifact'], str) or not value['artifact']
            or any(c in value['artifact'] for c in '\0\r\n')):
        raise ValueError('invalid sandbox execution context')
    credentials = value['credentials']
    if credentials is not None and (
            not isinstance(credentials, dict)
            or set(credentials) != {'source_sha256', 'snapshot_sha256', 'kind', 'expires_at'}
            or any(not isinstance(credentials[k], str) or not re.fullmatch(r'[0-9a-f]{64}', credentials[k])
                   for k in ('source_sha256', 'snapshot_sha256'))
            or not isinstance(credentials['kind'], str)
            or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', credentials['kind'])
            or credentials['expires_at'] is not None and type(credentials['expires_at']) is not int):
        raise ValueError('invalid sandbox credential identity')
    return json.loads(json.dumps(value))


def verify_context(current, expected):
    current, expected = validate_context(current), validate_context(expected)
    if current != expected:
        raise ContextChanged('sandbox execution context changed after admission')
    expiry = (expected['credentials'] or {}).get('expires_at')
    if expiry is not None and expiry <= time.time():
        raise ContextChanged('admitted sandbox credentials expired')
    return expected
