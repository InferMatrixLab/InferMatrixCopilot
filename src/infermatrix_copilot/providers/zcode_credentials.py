"""Project one selected native coding-plan account, without OAuth refresh state."""
import base64
import hashlib
import re
import json
import os
from pathlib import Path
import sys
import subprocess
from urllib.parse import quote
from types import SimpleNamespace

from .credential_sources import read_private_login
from .worker_credentials import WorkerCredentials

MARKER = '.zcode/v2/infermatrix-native-account.json'


def _cache_decoder(owner_uid):
    """Decode only selected enc:v1 values using the native 0.16.9 protocol."""
    key = None

    def decode(value):
        nonlocal key
        if not isinstance(value, str) or not value.startswith('enc:'):
            return value
        try:
            if not value.startswith('enc:v1:'):
                raise ValueError('unsupported native credential encoding')
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            if key is None:
                secret = os.environ.get('ZCODE_CREDENTIAL_SECRET', '').strip()
                if not secret:
                    import pwd
                    principal = pwd.getpwuid(os.getuid() if owner_uid is None else owner_uid)
                    secret = f'zcode-credential-fallback:{sys.platform}:{principal.pw_dir}:{principal.pw_name}'
                key = hashlib.sha256(secret.encode('utf-8')).digest()
            fields = value[len('enc:v1:'):].split('.')
            if len(fields) != 3 or any(not re.fullmatch(r'[A-Za-z0-9_-]+', part) for part in fields):
                raise ValueError('invalid native ciphertext')
            iv, tag, ciphertext = [base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)) for part in fields]
            if len(iv) != 12 or len(tag) != 16:
                raise ValueError('invalid native ciphertext lengths')
            return AESGCM(key).decrypt(iv, ciphertext + tag, None).decode('utf-8')
        except Exception:
            # Neither native ciphertext, decrypted material nor a key may enter diagnostics.
            raise ValueError('native ZCode credential cannot be decoded') from None

    return decode


def _selected(data, provider_id, *, decode=lambda value: value):
    from .zcode import _DEFAULT_PROVIDER_ID
    if provider_id != _DEFAULT_PROVIDER_ID or not isinstance(data, dict):
        raise ValueError('unsupported native ZCode coding account')
    identity_key = 'account-provider:' + provider_id + ':identity'
    identity = decode(data.get(identity_key))
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError('native ZCode coding account identity is unavailable')
    identity = identity.strip()
    key = 'account-provider:coding-plan:' + provider_id + ':account:' + quote(identity, safe="~()*!.'-_") + ':api-key'
    token = decode(data.get(key))
    if not isinstance(token, str) or len(token.strip()) < 8:
        raise ValueError('native ZCode coding account credential is unavailable')
    return {identity_key: identity, key: token.strip()}, token.strip()


def login_snapshot(path, *, owner_uid=None, provider_id=None):
    payload = read_private_login(path, owner_uid=owner_uid)
    try:
        selected, token = _selected(json.loads(payload), provider_id, decode=_cache_decoder(owner_uid))
    except (ValueError, TypeError):
        raise ValueError('native ZCode login has no selected coding-plan account') from None
    return WorkerCredentials({'files': {'zcode/.zcode/v2/credentials.json': json.dumps(selected),
        'zcode/' + MARKER: json.dumps({'schema': 'zcode-native-account/1', 'provider': provider_id})},
        'homes': {'ZCODE_DATA_BASE_DIR': 'zcode'}, 'redactions': [token]},
        source_sha256=hashlib.sha256(payload).hexdigest(),
        identity={'kind': 'zcode-coding-plan', 'expires_at': None})


def snapshot_home():
    root = os.environ.get('ZCODE_DATA_BASE_DIR')
    return Path(root) if root and (Path(root) / MARKER).is_file() else None


def main():
    try:
        request = json.load(sys.stdin)
        if (not isinstance(request, dict) or set(request) != {'provider_id', 'native_cli'}
                or not isinstance(request['native_cli'], str) or not Path(request['native_cli']).is_absolute()
                or snapshot_home() is None):
            raise ValueError('invalid native account control')
        from .registry import _construct_transport
        native = _construct_transport(SimpleNamespace(strict_backend_cli=request['native_cli']), 'zcode')
        version = native.version_probe(timeout_s=10)
        if version.returncode or not native.credential_runtime_supported(version.stdout):
            raise ValueError('native CLI is unavailable')
        root = snapshot_home()
        marker = json.loads(read_private_login(root / MARKER))
        if marker != {'schema': 'zcode-native-account/1', 'provider': request['provider_id']}:
            raise ValueError('native account marker differs')
        _selected(json.loads(read_private_login(root / '.zcode/v2/credentials.json')), request['provider_id'])
        print(json.dumps({'schema': 'zcode-account-status/1', 'provider': request['provider_id'],
                          'authenticated': True, 'native_version': version.stdout.strip()}))
        return 0
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print('native ZCode account control refused', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
