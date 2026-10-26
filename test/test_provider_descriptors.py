"""Public readiness and native transports share vendor authentication meaning."""
import sys
import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.providers.descriptors import provider_readiness
from infermatrix_copilot.providers.registry import PROVIDERS, transport_for_id


@pytest.mark.parametrize("provider,code,message,ready", [
    ("cursor", 0, "Logged in as reviewer", True),
    ("cursor", 0, "Not logged in", False),
    ("codex", 0, "Logged in using ChatGPT", True),
    ("codex", 1, "Not logged in", False),
    ("claude-code", 0, json.dumps({"loggedIn":True,"authMethod":"claude.ai"}), True),
    ("claude-code", 0, json.dumps({"loggedIn":True,"authMethod":"api_key_helper"}), True),
    ("claude-code", 1, json.dumps({"loggedIn":False,"authMethod":"none"}), False),
    ("claude-code", 0, json.dumps({"loggedIn":False}), False),
    ("claude-code", 0, json.dumps({"loggedIn":"true"}), False),
    ("claude-code", 0, "invalid status", False),
])
def test_readiness_matches_native_authentication_without_publisher_credentials(
    monkeypatch, provider, code, message, ready,
):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return message, "", code, False

    monkeypatch.setenv("GH_TOKEN", "test-publisher-secret")
    monkeypatch.setattr("infermatrix_copilot.providers.base.run_cli", run)
    public = provider_readiness(provider, [sys.executable])
    assert calls[0][0] == [sys.executable, *PROVIDERS[provider].readiness_args]
    assert "GH_TOKEN" not in calls[0][1]["env"]
    adapter = transport_for_id(SimpleNamespace(strict_backend_cli=sys.executable), provider)
    native = adapter.auth_gap()
    assert public == ((native,) if native else ())
    assert bool(public) is not ready


@pytest.mark.parametrize('ending', ['timeout','launch_error'])
def test_claude_readiness_and_gateway_stop_before_model_dispatch(monkeypatch,ending):
    import subprocess
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.kb_service.models import ModelGateway,ModelRole,ModelUnavailable
    calls=[]
    def probe(command,**kwargs):
        calls.append(command)
        assert command[1:]==['auth','status']
        if ending=='timeout': raise subprocess.TimeoutExpired(command,15)
        raise OSError('SECRET_DIAGNOSTIC')
    monkeypatch.setattr('infermatrix_copilot.providers.base.run_cli',probe)
    monkeypatch.setattr('infermatrix_copilot.providers.claude_code.ClaudeCodeTransport.complete',
                        lambda *_a,**_k: pytest.fail('authentication failure dispatched a model'))
    issue=provider_readiness('claude-code',[sys.executable])
    assert len(issue)==1 and 'SECRET_' not in issue[0]
    settings=Settings(_env_file=None,strict_backend_cli=sys.executable,metrics_enabled=False)
    with pytest.raises(ModelUnavailable,match='authentication status is unavailable'):
        ModelGateway(settings).call_json(ModelRole('generator','claude-code','model'),system='rules',prompt='evidence')
    assert len(calls)==2



def test_shared_auth_interpretation_retains_each_callers_environment_policy(monkeypatch):
    from infermatrix_copilot.providers.claude_code import ClaudeCodeTransport
    monkeypatch.setenv('ANTHROPIC_API_KEY','test-only-model-key')
    monkeypatch.setenv('GH_TOKEN','test-publisher-secret')
    observed=[]
    def probe(command,**kwargs):
        observed.append(kwargs['env'])
        assert 'GH_TOKEN' not in kwargs['env']
        present='ANTHROPIC_API_KEY' in kwargs['env']
        return json.dumps({'loggedIn':present,'authMethod':'api_key' if present else 'none'}),'',0 if present else 1,False
    monkeypatch.setattr('infermatrix_copilot.providers.base.run_cli',probe)
    assert provider_readiness('claude-code',[sys.executable])==()
    assert ClaudeCodeTransport(SimpleNamespace(strict_backend_cli=sys.executable)).auth_gap() is not None
    assert 'ANTHROPIC_API_KEY' in observed[0] and 'ANTHROPIC_API_KEY' not in observed[1]
