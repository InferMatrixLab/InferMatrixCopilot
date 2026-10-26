"""Legacy login projection cannot waive the canonical identity/auth boundary."""
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
from test_shared_execution import configured, settings
from test_subscription_admission import admitted_setup
from execution_helpers import contained_codex
from infermatrix_copilot.providers.codex import CodexTransport
from infermatrix_copilot.providers import subscription_admission
from shared_execution import subscription_control
from ledger import Refused

@pytest.fixture
def control(configured,tmp_path,monkeypatch):
    settings,calls=configured
    source=tmp_path/'arm';source.mkdir()
    proof={}
    def identity(self,profile):
        proof.update(schema='native-identity/1',sha256='a'*64,context=profile.sandbox._prepared_context())
        return dict(proof)
    monkeypatch.setattr(CodexTransport,'identity_probe',identity)
    plan={'provider_binary_sha256':'a'*64,'native_timeout_s':30,
          'execution_profile':{'model':'fixture','reasoning_effort':'high'},
          'shared_execution':{'schema':'shared-sandbox/1','profile':settings.native_completion_profiles['codex'],
              'source_roots_by_arm':{'baseline':[str(source)]},'knowledge_by_arm':{}}}
    return plan,source,proof,calls

def test_missing_canonical_auth_refuses_without_model_dispatch(control):
    plan,source,proof,calls=control
    assert subscription_control(plan=plan,arm='baseline',root=source/'control') is False
    assert not calls

@pytest.mark.parametrize('drift',['none','context','binary','outside'])
def test_control_binds_exact_identity_and_auth_context(control,monkeypatch,tmp_path,drift):
    plan,source,proof,calls=control
    admitted=[]
    def admit(settings,transport):
        admitted.append(settings.trace_store_root)
        context=dict(proof['context'])
        if drift=='context':
            context['image']='sha256:'+'b'*64
        return SimpleNamespace(configured_subscription_admission={'context':context})
    monkeypatch.setattr(subscription_admission,'admit_subscription',admit)
    root=source/'control'
    if drift=='binary':plan['provider_binary_sha256']='b'*64
    if drift=='outside':root=tmp_path/'outside'
    if drift=='none':
        assert subscription_control(plan=plan,arm='baseline',root=root)
        assert len(admitted)==1
        assert all(Path(p).parent.name=='call' for p in admitted)
    else:
        with pytest.raises((ValueError,Refused)):
            subscription_control(plan=plan,arm='baseline',root=root)
        assert len(admitted)==(1 if drift=='context' else 0)
    assert not calls

@pytest.mark.parametrize('allowed',[True,False])
def test_login_projection_uses_no_stdin_or_model_ledger(tmp_path,monkeypatch,capsys,allowed):
    import codex_guard
    config=tmp_path/'config.json';config.write_text(json.dumps({'plan':{},'arm':'baseline'}))
    monkeypatch.setattr(sys,'argv',['guard','--config',str(config),'--','login','status'])
    monkeypatch.setattr(sys,'stdin',SimpleNamespace(read=lambda:pytest.fail('control read model input')))
    monkeypatch.setattr(codex_guard,'dispatch',lambda **kwargs:pytest.fail('control entered model ledger'))
    seen=[]
    def probe(**kwargs):seen.append(kwargs);return allowed
    monkeypatch.setattr(codex_guard,'subscription_control',probe)
    assert codex_guard.main()==(0 if allowed else 1)
    assert seen==[{'plan':{},'arm':'baseline','root':tmp_path/'subscription-control'}]
    assert capsys.readouterr().out==('Logged in using ChatGPT\n' if allowed else 'Not logged in\n')

@pytest.mark.parametrize('damage',['none','binary','proof'])
def test_legacy_status_revalidates_original_canonical_proof_without_another_probe(admitted_setup,tmp_path,monkeypatch,damage):
    gateway,settings,store,role,auth,state,controls=admitted_setup
    source=tmp_path/'arm';source.mkdir()
    def identity(self,profile):
        sandbox=profile.sandbox
        if hasattr(sandbox,'backend'):
            from infermatrix_copilot.sandbox.backends import parse_backend_config
            sandbox=parse_backend_config(sandbox.backend).prepare(tmp_path/'identity-context').sandbox
        return {'schema':'native-identity/1','sha256':'a'*64,'context':sandbox._prepared_context()}
    monkeypatch.setattr(CodexTransport,'identity_probe',identity)
    plan={'provider_binary_sha256':'a'*64,'native_timeout_s':30,
          'execution_profile':{'model':'fixture','reasoning_effort':'high'},
          'shared_execution':{'schema':'shared-sandbox/1','profile':settings.native_completion_profiles['codex'],
              'source_roots_by_arm':{'baseline':[str(source)]},'knowledge_by_arm':{}}}
    root=source/'control'
    assert subscription_control(plan=plan,arm='baseline',root=root)
    assert state['auth_calls']==1 and state['model_calls']==0
    if damage=='binary':plan['provider_binary_sha256']='b'*64
    if damage=='proof':
        saved=json.loads((root/'completed-control.json').read_text())
        (Path(saved['admission']['root'])/'admission.json').write_text('{}')
    if damage=='none':assert subscription_control(plan=plan,arm='baseline',root=root)
    else:
        with pytest.raises((ValueError,Refused)):
            subscription_control(plan=plan,arm='baseline',root=root)
    assert state['auth_calls']==1 and state['model_calls']==0

@pytest.mark.parametrize('source_kind', ['legacy', 'generic'])
@pytest.mark.parametrize('allowed', [True, False])
def test_legacy_completion_dispatches_only_with_canonical_account_admission(admitted_setup, tmp_path, monkeypatch, source_kind, allowed):
    from copy import deepcopy
    from shared_execution import execute_shared
    from infermatrix_copilot.trace_store import TraceStore
    _, settings, _, _, auth, state, _ = admitted_setup
    profile = deepcopy(settings.native_completion_profiles['codex'])
    if source_kind == 'generic':
        if profile['kind'] == 'sandbox':
            backend = profile['backend']
        else:
            backend = profile
            profile = {'kind': 'sandbox', 'backend': backend, 'location': {'kind': 'local'}}
        backend.pop('codex_auth_file')
        backend.pop('codex_auth_owner_uid', None)
        profile['credentials_source'] = {'provider': 'codex', 'path': str(auth)}
    source = tmp_path / 'source'; source.mkdir()
    def identity(self, execution):
        # Use the configured provider's actual prepared context for both
        # local and simulated SSH; never synthesize subscription admission.
        from infermatrix_copilot.providers.completion_profiles import parse_completion_config, sandbox_profile_for_config
        sandbox = execution.sandbox
        if hasattr(sandbox, 'backend'):
            sandbox = sandbox_profile_for_config(parse_completion_config(sandbox.backend), tmp_path / 'identity-context').sandbox
        return {'schema': 'native-identity/1', 'sha256': 'a' * 64,
                'context': sandbox._prepared_context()}
    monkeypatch.setattr(CodexTransport, 'identity_probe', identity)
    plan = {'provider_binary_sha256': 'a' * 64, 'native_timeout_s': 30,
            'execution_profile': {'model': 'fixture', 'reasoning_effort': 'high'},
            'shared_execution': {'schema': 'shared-sandbox/1', 'profile': profile,
                'source_roots_by_arm': {'baseline': [str(source)]}, 'knowledge_by_arm': {}}}
    def dispatch():
        return execute_shared(plan=plan, arm='baseline', args=['exec'], cwd=source,
            prompt='original generation', bridge=None, schema=None, root=source / 'native')
    if not allowed:
        state['status'] = 'Not logged in'
        with pytest.raises(Refused, match='not an authenticated subscription'):
            dispatch()
        assert state['auth_calls'] == 1 and state['model_calls'] == 0
        return
    receipt = dispatch()
    assert receipt['native_complete']
    assert state['auth_calls'] == state['model_calls'] == 1
    store = TraceStore(receipt['trace_root'])
    records = [row for row, _ in store.iter_records(kind='model_call')]
    assert len(records) == 1 and records[0]['result']['subscription'] is True
    store.validate_native_attempt(records[0])
