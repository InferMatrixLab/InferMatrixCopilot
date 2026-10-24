"""Canonical secret filtering for stored text and credentialed worker streams."""
import os
import re
import json

REDACTED = '[REDACTED]'
_SECRET_SHAPES = re.compile(
    r'gh[pousr]_[A-Za-z0-9]{20,}'
    r'|github_pat_[A-Za-z0-9_]{20,}'
    r'|sk-ant-[A-Za-z0-9_-]{16,}'
    r'|sk-(?:proj-)?[A-Za-z0-9_-]{20,}'
    r'|(?:AKIA|ASIA)[A-Z0-9]{16}'
    r'|xox[abpr]-[A-Za-z0-9-]{10,}'
    r'|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}'
    r'|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----')
_SECRET_NAME = re.compile(r'(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|_KEY|_PAT|CREDENTIALS?)$', re.I)


def _secret_values(environ):
    values = {v for k, v in environ.items() if _SECRET_NAME.search(k) and len(v) >= 8
              and not v.startswith(('/', '~', '.'))}
    return sorted(values, key=len, reverse=True)


def redact(text, environ=None, *, secrets=()):
    values = set(_secret_values(os.environ if environ is None else environ)) | set(secrets)
    # Native responses can contain a JSON string inside the worker's JSONL
    # frame. Filter the decoded secret and both standard escaping layers.
    for _ in range(2):
        values |= {json.dumps(value, ensure_ascii=ascii_only)[1:-1]
                   for value in values for ascii_only in (False, True)}
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return _SECRET_SHAPES.sub(REDACTED, text)


class CredentialStream:
    """Filter complete UTF-8 lines and final partial output before forwarding.

    The native JSONL protocol can split credentials across arbitrary chunks.
    Buffered partial lines flush on exit/cancellation, then the worker receipt
    hashes the filtered bytes actually observed by its controller.
    """
    def __init__(self, secrets, write):
        self.secrets = tuple(secrets)
        self.pending = bytearray()
        self.write = write

    def feed(self, value):
        self.pending.extend(value)
        while b'\n' in self.pending:
            end = self.pending.index(b'\n') + 1
            self._emit(bytes(self.pending[:end]))
            del self.pending[:end]

    def _emit(self, value):
        self.write(redact(value.decode('utf-8', errors='replace'), {}, secrets=self.secrets).encode())

    def finish(self):
        if self.pending:
            self._emit(bytes(self.pending))
            self.pending.clear()
