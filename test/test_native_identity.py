"""Identity control hashes bytes and never invokes the selected native binary."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from infermatrix_copilot.providers import native_identity
from infermatrix_copilot.providers.base import HarnessTransport
from infermatrix_copilot.providers.codex import CodexTransport
from infermatrix_copilot.providers.zcode import ZCodeTransport


def test_generic_and_legacy_identity_commands_read_the_same_bytes_without_execution(tmp_path):
    marker = tmp_path / 'must-not-exist'
    binary = tmp_path / 'native'
    binary.write_text('#!/bin/sh\ntouch ' + str(marker) + '\nexit 71\n')
    binary.chmod(0o700)
    expected = {'schema': 'native-identity/1', 'sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}
    for module, args in [('native_identity', []), ('codex', ['--identity-probe'])]:
        result = subprocess.run([sys.executable, '-B', '-m', 'infermatrix_copilot.providers.' + module, *args],
            input=json.dumps({'native_cli': str(binary)}), text=True, capture_output=True, check=True)
        assert json.loads(result.stdout) == expected
    assert not marker.exists()
    assert CodexTransport.identity_probe is ZCodeTransport.identity_probe is HarnessTransport.identity_probe


@pytest.mark.parametrize('stdout', ['{}', 'damaged', '{"schema":"native-identity/1","sha256":"wrong"}'])
def test_unconfirmed_identity_is_rejected_before_any_native_call(stdout):
    profile = SimpleNamespace(native_cli='/opt/native', execute=lambda *a, **kw:
                              SimpleNamespace(stdout=stdout, returncode=0, reason=None))
    with pytest.raises(ValueError):
        native_identity.probe(profile)
