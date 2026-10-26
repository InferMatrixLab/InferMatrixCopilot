"""Host proxy variables stay excluded; provisioned sandbox proxies survive nesting."""
import pytest

from infermatrix_copilot.providers.base import sanitized_env
from infermatrix_copilot.providers.worker_environment import PROXY_MARKER, bind_proxy, proxy_environment


def test_host_credentials_and_unmarked_proxy_are_not_inherited(monkeypatch):
    monkeypatch.delenv(PROXY_MARKER, raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'private-host-key')
    monkeypatch.setenv('HTTP_PROXY', 'http://untrusted.example')
    monkeypatch.setenv('HTTPS_PROXY', 'http://untrusted.example')
    env = sanitized_env()
    assert not {'OPENAI_API_KEY', 'HTTP_PROXY', 'HTTPS_PROXY', PROXY_MARKER} & env.keys()


def test_only_provisioned_loopback_proxy_crosses_the_provider_environment_boundary(monkeypatch):
    provisioned = {}
    bind_proxy(provisioned, '127.0.0.1', 12345)
    for name, value in provisioned.items():monkeypatch.setenv(name, value)
    monkeypatch.setenv('GH_TOKEN', 'private-host-key')
    env = sanitized_env()
    assert proxy_environment(env) == provisioned
    assert 'GH_TOKEN' not in env


@pytest.mark.parametrize('proxy', ['http://user:PRIVATE@127.0.0.1:1234', 'http://outside:1234',
    'https://127.0.0.1:1234', 'http://127.0.0.1:65536', 'http://127.0.0.1:0'])
def test_invalid_proxy_marker_cannot_forward_host_configuration_or_secret_diagnostics(proxy):
    with pytest.raises(ValueError) as error:
        proxy_environment({PROXY_MARKER: '1', 'HTTP_PROXY': proxy, 'HTTPS_PROXY': proxy})
    assert 'PRIVATE' not in str(error.value)
