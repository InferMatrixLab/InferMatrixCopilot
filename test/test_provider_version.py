"""Provider diagnostics share process supervision and never start model sessions."""
import subprocess
import sys

import pytest

from infermatrix_copilot.sdk.v1 import provider_version
from infermatrix_copilot.contracts.models import InvalidRequestError


@pytest.mark.parametrize('provider', ['codex', 'cursor', 'claude-code', 'zcode'])
def test_version_uses_the_configured_prefix_and_filters_publisher_credentials(provider, monkeypatch):
    monkeypatch.setenv('GH_TOKEN', 'publisher-secret')
    script = "import os,sys; assert 'GH_TOKEN' not in os.environ; assert sys.argv[1:] == ['--version']; print('native fixture 1.0')"
    assert provider_version(provider, [sys.executable, '-c', script]) == 'native fixture 1.0'


@pytest.mark.parametrize('code,stdout,stderr,expected', [
    (0, 'out', 'err', 'out'), (0, '', 'err', 'err'),
    (2, 'out', 'err', 'err'), (2, 'out', '', 'out'),
])
def test_version_preserves_doctor_output_and_error_precedence(monkeypatch, code, stdout, stderr, expected):
    def run(argv, **kwargs):
        assert argv[-1] == '--version' and kwargs['timeout_s'] == 30
        return stdout, stderr, code, False
    monkeypatch.setattr('infermatrix_copilot.providers.base.run_cli', run)
    if code:
        with pytest.raises(RuntimeError, match=expected):
            provider_version('codex', [sys.executable])
    else:
        assert provider_version('codex', [sys.executable]) == expected


def test_version_deadline_is_enforced_by_shared_supervisor():
    with pytest.raises(subprocess.TimeoutExpired):
        provider_version('codex', [sys.executable, '-c', 'import time; time.sleep(10)'], timeout_s=.05)


@pytest.mark.parametrize('provider,command', [('api', None), ('deepseek', None), ('unknown', None),
                                              ('codex', 'codex'), ('codex', [1]), ('codex', [''])])
def test_invalid_version_requests_fail_before_dispatch(monkeypatch, provider, command):
    monkeypatch.setattr('infermatrix_copilot.providers.base.run_cli', lambda *a, **k: pytest.fail('dispatched'))
    with pytest.raises(InvalidRequestError):
        provider_version(provider, command)
