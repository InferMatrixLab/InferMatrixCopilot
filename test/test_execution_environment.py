"""Credential isolation is one policy at both public and transport boundaries."""
import os
from types import MappingProxyType

from infermatrix_copilot.sdk.v1 import readonly_environment


def test_readonly_policy_is_nonmutating_idempotent_and_preserves_model_auth():
    source = MappingProxyType({
        "HOME": "/model-home", "PATH": "/tools", "OPENAI_API_KEY": "model",
        "ANTHROPIC_API_KEY": "model", "GH_TOKEN": "publisher",
        "GITHUB_APP_PRIVATE_KEY_PATH": "/publisher-key", "SSH_AUTH_SOCK": "/agent",
        "CURSOR_GIT_ASKPASS_MAIN": "/broker", "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "broker",
    })
    before = dict(source)
    actual = readonly_environment(source)
    assert dict(source) == before
    assert readonly_environment(actual) == actual
    for key in ("HOME", "PATH", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert actual[key] == source[key]
    assert not set(actual) & {
        "GH_TOKEN", "GITHUB_APP_PRIVATE_KEY_PATH", "SSH_AUTH_SOCK",
        "CURSOR_GIT_ASKPASS_MAIN", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    }
    assert actual["GH_CONFIG_DIR"] == os.devnull
    assert actual["GIT_CONFIG_GLOBAL"] == actual["GIT_CONFIG_SYSTEM"] == os.devnull
    assert actual["GIT_TERMINAL_PROMPT"] == "0"


def test_default_policy_filters_ambient_environment(monkeypatch):
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "publisher")
    monkeypatch.setenv("OPENAI_API_KEY", "model")
    actual = readonly_environment()
    assert "GH_ENTERPRISE_TOKEN" not in actual
    assert actual["OPENAI_API_KEY"] == "model"
    assert "GH_ENTERPRISE_TOKEN" in os.environ
