"""Mixed native roles share admission, canonical receipts and original bindings."""
import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'test'))
from test_zcode_sandbox_completion import sandbox  # noqa: F401
from test_completion_profile_configuration import configured  # noqa: F401
from test_readonly_sandbox import worker_receipt  # noqa: F401
from test_arm_runtime import settings  # noqa: F401

from arm_runtime import model_gateway
from ledger import Refused
from role_profiles import role
from shared_execution import shared_profiles
from infermatrix_copilot.kb_service.models import ModelRole
from infermatrix_copilot.kb_service.quality_review import QualityPolicy, review_call
from infermatrix_copilot.providers.base import HarnessTransport
from infermatrix_copilot.providers.container_execution import ContainerExecution
from infermatrix_copilot.trace_store import TraceStore

_CANONICAL_COMMAND = ContainerExecution.run_command


@pytest.fixture
def mixed(sandbox, monkeypatch, tmp_path):
    settings, calls = sandbox
    profile = settings.native_completion_profiles['zcode']
    profiles = {'codex': {**profile, 'native_cli': '/usr/local/bin/codex'}, 'zcode': profile}
    area = tmp_path / 'mixed';area.mkdir();shim = area / 'codex';shim.write_text('not invoked')
    plan = {'native_call_limits': {'case': 4}, 'execution_kind': 'fixture', 'native_timeout_s': 30,
        'provider_binary_sha256_by_provider': {'codex': 'a' * 64, 'zcode': 'b' * 64},
        'execution_profile': {'model': 'fixture', 'reasoning_effort': 'high', 'roles': {
            'generator': {'provider': 'zcode', 'model': 'GLM-5.3', 'effort': ''},
            'judge': {'provider': 'codex', 'model': 'fixture', 'effort': 'high'}}},
        'shared_execution': {'schema': 'shared-sandbox/2', 'profiles': profiles,
            'source_roots_by_arm': {arm: [str(tmp_path)] for arm in ('baseline', 'candidate')},
            'knowledge_by_arm': {}}}
    path = area / 'config.json';path.write_text(json.dumps({'arm': 'candidate', 'case_id': 'case',
        'ledger_path': str(tmp_path / 'calls.sqlite'), 'plan': plan}))
    monkeypatch.setenv('SHADOW_ARM_CONFIG', str(path))
    zcode_worker = ContainerExecution.run_command
    def execute(execution, command, **kwargs):
        if command[-1] == 'infermatrix_copilot.providers.completion_worker':
            return zcode_worker(execution, command, **kwargs)
        return _CANONICAL_COMMAND(execution, command, **kwargs)
    monkeypatch.setattr(ContainerExecution, 'run_command', execute)
    monkeypatch.setattr(HarnessTransport, 'identity_probe', lambda self, profile:
        {'schema': 'native-identity/1', 'sha256': plan['provider_binary_sha256_by_provider'][self.spec.id],
         'context': profile.sandbox._prepared_context()})
    return settings.model_copy(update={'strict_backend_model': ''}), calls, shim, plan


def test_generation_and_shared_quality_review_settle_their_own_canonical_provider(mixed):
    settings, calls, shim, plan = mixed
    records = []
    gateway = model_gateway(settings, shim=shim, recorder=records.append)
    generator = ModelRole('generator', **role(plan, 'generator'))
    judge = ModelRole('judge', **role(plan, 'judge'))
    generated = gateway.call_json(generator, system='generate', prompt='frozen evidence')
    judged = review_call(gateway, judge, policy=QualityPolicy('mixed-conformance', archive='verified'),
        system='judge', prompt=generated.text, validate=lambda data: data['ok'])
    assert generated.data['spooled'] and judged.data['ok']
    assert judged.quality_receipt['outcome'] == 'judged'
    with sqlite3.connect(shim.parent.parent / 'calls.sqlite') as db:
        rows = db.execute('SELECT status,result FROM calls').fetchall()
    assert len(rows) == 2 and all(state == 'complete' for state, _ in rows)
    assert {json.loads(raw)['accounting_usage']['input_tokens'] for _, raw in rows} == {10, 34}
    assert len(records) == 2
    for record in records:
        receipt = json.loads(next(raw for _, raw in rows if
            json.loads(raw)['native_attempt_id'] == record['native_attempt_id']))
        store = TraceStore(receipt['trace_root'])
        row, = [v for v, _ in store.iter_records(kind='model_call') if v['id'] == receipt['trace_id']]
        store.validate_native_attempt(row)
        assert row['model']['provider'] == record['provider']
    assert set(gateway.evaluation_runtime.proofs) == {'codex', 'zcode'}
    assert not gateway.subscription_billing(generator)
    before = len(calls)
    with pytest.raises(Refused, match='cannot be replaced'):
        gateway.call_json(generator, system='generate', prompt='frozen evidence')
    assert len(calls) == before


@pytest.mark.parametrize('change', ['provider', 'binary', 'profile', 'effort'])
def test_mixed_plan_or_role_drift_refuses_before_native_dispatch(mixed, change):
    settings, calls, shim, plan = mixed
    if change == 'binary':
        del plan['provider_binary_sha256_by_provider']['zcode']
    elif change == 'profile':
        del plan['shared_execution']['profiles']['zcode']
    else:
        gateway = model_gateway(settings, shim=shim)
        selected = role(plan, 'generator')
        selected[change] = 'codex' if change == 'provider' else 'high'
        with pytest.raises(Refused):
            gateway.call_json(ModelRole('generator', **selected), system='generate', prompt='evidence')
        assert not calls
        return
    with pytest.raises(Refused):
        shared_profiles(plan)
    assert not calls


def test_old_package_refuses_mixed_profiles_instead_of_substituting_codex(mixed, monkeypatch):
    settings, calls, shim, _ = mixed
    monkeypatch.setattr('arm_runtime.supports_shared_execution', lambda: False)
    with pytest.raises(Refused, match='mixed native providers'):
        model_gateway(settings, shim=shim)
    assert not calls


def test_live_mixed_configuration_requires_complete_frozen_provider_options(mixed):
    settings, calls, shim, plan = mixed
    plan['execution_kind'] = 'live'
    with pytest.raises(Refused, match='frozen provider options'):
        shared_profiles(plan)
    profiles = plan['shared_execution']['profiles']
    profiles['codex'] = {'kind': 'sandbox', 'backend': profiles['codex'], 'location': {'kind': 'local'},
                         'provider_options': {'model_mismatch_policy': 'fail'}}
    profiles['zcode'] = {'kind': 'sandbox', 'backend': profiles['zcode'], 'location': {'kind': 'local'},
        'provider_options': {'zcode_provider_id': 'account:fixture', 'zcode_reasoning_level': 'high',
                             'model_mismatch_policy': 'fail'}}
    assert set(shared_profiles(plan)) == {'codex', 'zcode'}
    profiles['zcode']['provider_options']['zcode_provider_id'] = ''
    with pytest.raises(Refused, match='frozen provider options'):
        shared_profiles(plan)
    assert not calls
