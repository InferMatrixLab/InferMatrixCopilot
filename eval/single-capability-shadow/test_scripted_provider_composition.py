"""Fixture generators may coexist with native judges without replacing dispatch."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from arm_runtime import model_gateway
from ledger import Refused
from legacy_provider_host import LegacyTransport
from test_legacy_provider_host import setup, mixed, sandbox, configured, worker_receipt, settings  # noqa: F401


def configured_generator(mixed):
    calls, shim, path, plan = setup(mixed)
    del plan['shared_execution']['profiles']['zcode']
    del plan['provider_binary_sha256_by_provider']['zcode']
    plan['scripted_providers'] = ['zcode']
    config = json.loads(path.read_bytes());config['plan'] = plan
    path.write_text(json.dumps(config))
    return calls, shim, path, plan


@pytest.mark.parametrize('legacy', [False, True])
def test_fixture_generator_cannot_replace_native_judge(mixed, monkeypatch, legacy):
    from infermatrix_copilot.kb_service.models import ModelRole
    settings, _, _, _ = mixed
    calls, shim, path, plan = configured_generator(mixed)
    monkeypatch.setattr('arm_runtime.supports_shared_execution', lambda: not legacy)
    generator = object()
    gateway = model_gateway(settings, shim=shim, scripted_transports={'zcode': generator})
    assert gateway._transport('zcode') is generator
    if legacy:
        assert isinstance(gateway._transport('codex'), LegacyTransport)
    else:
        reply = gateway.call_json(ModelRole('judge', 'codex', 'fixture', 'high'),
                                  system='rubric', prompt='frozen correction')
        assert reply.data == {'ok': True} and not calls  # no ZCode native dispatch
        import sqlite3
        with sqlite3.connect(shim.parent.parent / 'calls.sqlite') as db:
            assert db.execute("SELECT count(*) FROM calls WHERE status='complete'").fetchone()[0] == 1


@pytest.mark.parametrize('invalid', ['native-profile', 'judge', 'live', 'custom-factory', 'same-provider'])
def test_scripted_injection_refuses_before_host_or_native_dispatch(mixed, monkeypatch, invalid):
    settings, _, _, _ = mixed
    calls, shim, path, plan = configured_generator(mixed)
    scripts = {'zcode': object()};kwargs = {}
    if invalid == 'native-profile':
        plan['shared_execution']['profiles']['zcode'] = plan['shared_execution']['profiles']['codex']
    elif invalid == 'judge': scripts = {'codex': object()}
    elif invalid == 'live': plan['execution_kind'] = 'native'
    elif invalid == 'custom-factory': kwargs['transport_factory'] = lambda _: pytest.fail('untrusted factory entered')
    else: plan['execution_profile']['roles']['judge']['provider'] = 'zcode'
    config = json.loads(path.read_bytes());config['plan'] = plan;path.write_text(json.dumps(config))
    monkeypatch.setattr('legacy_provider_host.host_binding', lambda *_: pytest.fail('invalid fixture reached host'))
    with pytest.raises(Refused):
        model_gateway(settings, shim=shim, scripted_transports=scripts, **kwargs)
    assert not calls


def test_correction_uses_archived_judge_records_and_preserves_accepted_peers(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service.models import ModelGateway
    from infermatrix_copilot.llm import Reply, Block
    from init_native_arm import execute
    import test_kb_init_knowledge as fixture
    monkeypatch.chdir(tmp_path)
    shim = tmp_path / 'codex';shim.write_text('never invoked')
    plan = {'execution_kind': 'fixture', 'scripted_providers': ['zcode'],
        'shared_execution': {'schema': 'shared-sandbox/2'}, 'host_timeout_s': 180,
        'execution_profile': {'model': 'offline-judge', 'reasoning_effort': 'high', 'roles': {
            'generator': {'provider': 'zcode', 'model': 'GLM-5.3', 'effort': ''},
            'judge': {'provider': 'codex', 'model': 'offline-judge', 'effort': 'high'}}}}
    config = tmp_path / 'config.json';config.write_text(json.dumps({'plan':plan}))
    monkeypatch.setenv('SHADOW_ARM_CONFIG',str(config))
    calls = []
    class Judge:
        subscription_billing = True
        def complete(self, *, messages, model, **kwargs):
            prompt = messages[0]['content'];calls.append(prompt)
            data = {'dimensions': {'faithful': 'no' if 'Engine.step parses JSON' in prompt else 'yes',
                'does_not_weaken':'yes','non_contradictory':'yes'},
                'reasons': {'faithful':'The shown function returns a constant.'}}
            return Reply(blocks=[Block(type='text',text=json.dumps(data))],model=model)
    def composed(settings, *, scripted_transports, recorder, **kwargs):
        assert set(scripted_transports)=={'zcode'}
        return ModelGateway(settings,transport_factory=lambda provider:
            scripted_transports[provider] if provider in scripted_transports else Judge(),recorder=recorder)
    monkeypatch.setattr('init_fixture_policies.model_gateway',composed)
    report=execute('foundation',SimpleNamespace(shim=str(shim),model=None,effort=None,capture=False,
        correction=True,simulate_subscription_auth=False,subscription_native=None,
        fixture_path=Path(fixture.__file__).resolve(),validators=Path(fixture.__file__).resolve().parent.parent/'knowledge/tools'),native=False)
    result=json.loads((tmp_path/'foundation.json').read_bytes())
    assert len(calls)==13==len(result['packets'])
    assert result['first_judge_attempts']==12 and result['correction_judge_attempts']==1
    assert report['gateway_attempts']==13 and 'judge gateway attempts' in report['gateway_attempts_scope']
    assert result['retained_tasks']>0 and report['state']=='dry_run'


def test_correction_records_a_judge_refusal_without_dispatching_the_provider(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole, ModelUnavailable
    from infermatrix_copilot.trace_store import TraceStore
    from init_fixture_policies import correction_runtime
    shim = tmp_path / 'codex'; shim.write_text('must not execute')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'plan': {'shared_execution': {'schema': 'shared-sandbox/2'}}}))
    monkeypatch.setenv('SHADOW_ARM_CONFIG', str(config))

    class RefusedJudge:
        def validate_completion(self):
            raise RuntimeError('known refusal before native dispatch')

        def complete(self, **kwargs):
            pytest.fail('preflight refusal dispatched the judge')

    monkeypatch.setattr('init_fixture_policies.model_gateway', lambda settings, *, recorder, **kwargs:
        ModelGateway(None, transport_factory=lambda _: RefusedJudge(), recorder=recorder))
    fixture = {'_runtime': lambda world, gateway=None, **kwargs:
        SimpleNamespace(state_dir=tmp_path / 'state', gateway=gateway)}
    packets = []
    role = ModelRole('judge', 'codex', 'offline-judge', 'high')
    runtime = correction_runtime(SimpleNamespace(shim=str(shim)), fixture, {},
        SimpleNamespace(strict_backend_cli=str(shim)), packets, [], role)
    with pytest.raises(ModelUnavailable, match='known refusal'):
        runtime.gateway.call_json(role, system='rubric', prompt='frozen correction')
    assert packets == [{'system': 'rubric', 'prompt': 'frozen correction'}]
    records = list(TraceStore(tmp_path / 'state/init/traces').iter_records(kind='model_call'))
    assert len(records) == 1
    record, _ = records[0]
    assert record['result']['native_call_sent'] is False
    assert record['result']['native_outcome_pending'] is False


def test_reconstructed_gateway_keeps_model_identity_and_uses_fresh_controls(mixed):
    from infermatrix_copilot.kb_service.models import ModelRole
    settings, _, _, _ = mixed
    calls, shim, _, _ = configured_generator(mixed)
    first = model_gateway(settings, shim=shim, scripted_transports={'zcode':object()})
    role=ModelRole('judge','codex','fixture','high')
    first.call_json(role,system='rubric',prompt='original paid intent')
    second = model_gateway(settings, shim=shim, scripted_transports={'zcode':object()})
    assert first.evaluation_runtime.control_root != second.evaluation_runtime.control_root
    assert first.evaluation_runtime.root == second.evaluation_runtime.root
    assert first.evaluation_runtime.ledger_path == second.evaluation_runtime.ledger_path
    with pytest.raises(Refused,match='original evaluation attempt cannot be replaced'):
        second.call_json(role,system='rubric',prompt='original paid intent')
    import sqlite3
    with sqlite3.connect(shim.parent.parent/'calls.sqlite') as db:
        assert db.execute("SELECT count(*) FROM calls WHERE status='complete'").fetchone()[0]==1
