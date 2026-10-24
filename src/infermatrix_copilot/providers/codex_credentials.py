"""Read an existing native login; delegate OAuth refresh to its original host."""
import hashlib
import base64
import json
from .credential_sources import read_private_login


def _access_expiry(token):
    """Read expiry only; native authentication owns token verification/refresh."""
    try:
        encoded = token.split('.')[1]
        value = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))['exp']
        return value if type(value) is int and value > 0 else None
    except (ValueError, IndexError, KeyError, TypeError):
        return None

from .worker_credentials import MAX_BUNDLE_BYTES, WorkerCredentials


def login_snapshot(path, *, owner_uid=None):
    """Use native external-token mode; never export or rotate a refresh token."""
    payload = read_private_login(path, owner_uid=owner_uid)
    try:
        data = json.loads(payload)
        tokens = data['tokens']
        if (data.get('auth_mode') != 'chatgpt' or data.get('OPENAI_API_KEY') not in (None, '')
                or not isinstance(tokens, dict)
                or any(not isinstance(tokens.get(key), str) or not tokens[key]
                       for key in ('access_token', 'id_token', 'account_id'))):
            raise ValueError('unsupported login')
    except (ValueError, KeyError, TypeError):
        # Native cache values must never be interpolated into diagnostics.
        raise ValueError('native Codex login is not a managed ChatGPT account') from None
    external = {'auth_mode': 'chatgptAuthTokens', 'OPENAI_API_KEY': None,
                'tokens': {key: tokens[key] for key in ('access_token', 'id_token', 'account_id')},
                'last_refresh': data.get('last_refresh')}
    external['tokens']['refresh_token'] = ''
    return WorkerCredentials({'files': {'codex/auth.json': json.dumps(external)},
        'homes': {'CODEX_HOME': 'codex'}, 'redactions': [tokens['access_token'], tokens['id_token']]},
        source_sha256=hashlib.sha256(payload).hexdigest(),
        identity={'kind': 'codex-chatgpt', 'expires_at': _access_expiry(tokens['access_token'])})
