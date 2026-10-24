"""Ephemeral credential files over a local side channel, never process input."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import struct
import tempfile

from .network_relay import unix_service

MAX_BUNDLE_BYTES = 131072
SOCKET_PATH = '/run/model-credentials.sock'


def _relative(value):
    return (isinstance(value, str) and value and not value.startswith('/') and '\\' not in value
            and all(part not in ('', '.', '..') for part in value.split('/'))
            and not any(c in value for c in '\0\r\n'))


def validate_bundle(bundle):
    if not isinstance(bundle, dict) or set(bundle) != {'files', 'homes', 'redactions'}:
        raise ValueError('invalid worker credential bundle')
    files, homes, redactions = (bundle[key] for key in ('files', 'homes', 'redactions'))
    if (not isinstance(files, dict) or not 0 < len(files) <= 8
            or any(not _relative(path) or not isinstance(value, str) for path, value in files.items())
            or not isinstance(homes, dict) or not homes
            or any(not isinstance(name, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', name)
                   or name in {'PATH', 'HOME', 'TMPDIR', 'LANG', 'PYTHONPATH', 'PYTHONHOME'}
                   or name.startswith(('LD_', 'DYLD_')) or not _relative(path)
                   for name, path in homes.items())
            or not isinstance(redactions, list) or not redactions
            or any(not isinstance(value, str) or len(value) < 8 or any(c in value for c in '\0\r\n')
                   for value in redactions)):
        raise ValueError('invalid worker credential fields')
    paths = [PurePosixPath(path) for path in files]
    if any(left != right and left in right.parents for left in paths for right in paths):
        raise ValueError('conflicting worker credential paths')
    payload = json.dumps(bundle, sort_keys=True, ensure_ascii=False).encode()
    if len(payload) > MAX_BUNDLE_BYTES:
        raise ValueError('worker credential bundle is too large')
    return payload


class WorkerCredentials:
    """Opaque, memory-only material; repr and metadata never contain credentials."""
    def __init__(self, bundle, *, source_sha256, identity=None):
        self._payload = validate_bundle(bundle)
        self.sha256 = hashlib.sha256(self._payload).hexdigest()
        self.metadata = {'source_sha256': source_sha256, 'snapshot_sha256': self.sha256}
        self.identity = dict(identity or {'kind': 'unknown', 'expires_at': None})

    @contextmanager
    def serve(self):
        payload = self._payload
        def send(connection):
            connection.sendall(struct.pack('!I', len(payload)) + payload)
        # Run/trace paths may exceed the Unix-socket pathname limit. The private
        # directory is independent; the original execution binds its inode/hash.
        with tempfile.TemporaryDirectory(prefix='imc-credentials-', dir='/tmp') as private:
            path = Path(private) / 'channel.sock'
            with unix_service(path, send):
                # Parent is mode 0700; Docker mounts only the socket into the
                # non-root worker, which must be able to connect to that inode.
                path.chmod(0o666)
                yield path


def receive_bundle(expected_sha256, path=SOCKET_PATH):
    def read_exact(connection, size):
        value = bytearray()
        while len(value) < size:
            block = connection.recv(size - len(value))
            if not block:
                raise ValueError('worker credential channel ended early')
            value.extend(block)
        return bytes(value)
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        length, = struct.unpack('!I', read_exact(connection, 4))
        if not 0 < length <= MAX_BUNDLE_BYTES:
            raise ValueError('invalid worker credential frame')
        payload = read_exact(connection, length)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError('worker credential snapshot differs')
    bundle = json.loads(payload)
    if validate_bundle(bundle) != payload:
        raise ValueError('noncanonical worker credential snapshot')
    return bundle


@contextmanager
def credential_environment(bundle):
    validate_bundle(bundle)
    with tempfile.TemporaryDirectory(prefix='infermatrix-credentials-', dir='/tmp') as private:
        root = Path(private)
        for name, value in bundle['files'].items():
            path = root / name
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(value)
        yield {name: str(root / path) for name, path in bundle['homes'].items()}
