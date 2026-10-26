"""Application drivers keep quota authority while using canonical execution."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'test'))
from test_completion_profile_configuration import configured  # noqa: E402,F401
from test_readonly_sandbox import worker_receipt  # noqa: E402,F401

from arm_runtime import model_gateway, native_completion, legacy_settings, host_timeout, sdk_runtime
from ledger import Refused
from infermatrix_copilot.kb_service.models import ModelRole, ModelUnavailable
from infermatrix_copilot.providers.codex import CodexTransport
from infermatrix_copilot.trace_store import TraceStore


@pytest.fixture
def settings(tmp_path):
    from infermatrix_copilot.config import Settings
    return Settings(_env_file=None, run_root=tmp_path / 'runs', metrics_enabled=False)


@pytest.fixture
def arm(configured, tmp_path, monkeypatch):
    settings, calls = configured
    area = tmp_path / 'candidate'; area.mkdir()
    shim = area / 'codex'; shim.write_text('not executed')
    profile = settings.native_completion_profiles['codex']
    plan = {'native_call_limits': {'case': 2}, 'provider_binary_sha256': 'a' * 64,
            'native_timeout_s': 30, 'execution_kind': 'fixture',
            'execution_profile': {'model': 'fixture', 'reasoning_effort': 'high'},
            'shared_execution': {'schema': 'shared-sandbox/1', 'profile': profile,
                'source_roots_by_arm': {name: [str(tmp_path)] for name in ('baseline', 'candidate')},
                'knowledge_by_arm': {}}}
    config = area / 'config.json'
    config.write_text(json.dumps({'ledger_path': str(tmp_path / 'calls.sqlite'), 'plan': plan,
                                 'case_id': 'case', 'arm': 'candidate'}))
    monkeypatch.setenv('SHADOW_ARM_CONFIG', str(config))
    monkeypatch.setattr(CodexTransport, 'identity_probe', lambda self, profile: {
        'schema': 'native-identity/1', 'sha256': 'a' * 64, 'context': profile.sandbox._prepared_context()})
    return settings, calls, shim, plan


def rows(shim):
    with sqlite3.connect(shim.parent.parent / 'calls.sqlite') as db:
        return db.execute('SELECT status,result FROM calls').fetchall()


def test_gateway_defaults_profile_and_durable_recorder_share_one_archive(arm):
    settings, calls, shim, _ = arm
    records = []
    gateway = model_gateway(settings, shim=shim, recorder=records.append)
    role = ModelRole('judge', 'codex', 'fixture')
    result = gateway.call_json(role, system='rubric', prompt='frozen source')
    assert result.data == {'ok': True} and len(calls) == 1 and len(records) == 1
    state, raw = rows(shim)[0]
    receipt = json.loads(raw)
    assert state == 'complete' and receipt['native_complete']
    assert receipt['accounting_usage']['input_tokens'] == 10
    store = TraceStore(receipt['trace_root'])
    captured = list(store.iter_records(kind='model_call'))
    assert len(captured) == 1
    store.validate_native_attempt(captured[0][0])
    assert captured[0][0]['model']['effort'] == 'high'
    assert role.effort == ''  # defaults do not mutate the frozen domain role


def test_completed_request_does_not_dispatch_a_second_paid_attempt(arm):
    settings, calls, shim, _ = arm
    gateway = model_gateway(settings, shim=shim)
    role = ModelRole('judge', 'codex', 'fixture')
    gateway.call_json(role, system='rubric', prompt='frozen source')
    with pytest.raises(Refused, match='cannot be replaced'):
        gateway.call_json(role, system='rubric', prompt='frozen source')
    assert len(calls) == 1 and len(rows(shim)) == 1


def test_domain_schema_rejection_still_settles_the_completed_native_charge(arm):
    settings, calls, shim, _ = arm
    gateway = model_gateway(settings, shim=shim)
    def reject(_):
        raise ValueError('domain criterion failed')
    with pytest.raises(ModelUnavailable, match='domain criterion failed'):
        gateway.call_json(ModelRole('judge', 'codex', 'fixture'), system='rubric', prompt='source', validate=reject)
    assert len(calls) == 1 and rows(shim)[0][0] == 'complete'


def test_unknown_response_keeps_original_reservation_and_blocks_other_calls(arm, monkeypatch):
    from infermatrix_copilot.providers.container_execution import ContainerExecution
    from infermatrix_copilot.sandbox.base import SandboxPending
    settings, calls, shim, _ = arm
    attempts = []
    def missing(*args, **kwargs):
        attempts.append(1)
        raise SandboxPending('original worker response missing', prior_execution=True)
    monkeypatch.setattr(ContainerExecution, 'run', missing)
    gateway = model_gateway(settings, shim=shim)
    for prompt in ('source', 'different source'):
        with pytest.raises((SandboxPending, ModelUnavailable, Refused)):
            gateway.call_json(ModelRole('judge', 'codex', 'fixture'), system='rubric', prompt=prompt)
    assert len(attempts) == 1 and rows(shim)[0] == ('reserved', None)


@pytest.mark.parametrize('change', ['model', 'effort', 'provider'])
def test_profile_drift_refuses_before_native_dispatch(arm, change):
    settings, calls, shim, _ = arm
    role = ModelRole('judge', 'codex', 'fixture', effort='high')
    role = replace(role, **{change: 'wrong'})
    gateway = model_gateway(settings, shim=shim)
    with pytest.raises(Refused):
        gateway.call_json(role, system='rubric', prompt='source')
    assert not calls and not (shim.parent.parent / 'calls.sqlite').exists()


def test_completion_entry_preserves_reply_without_a_second_producer(arm):
    settings, calls, shim, _ = arm
    reply = native_completion(settings, shim=shim, system='rubric', messages=[{'role': 'user', 'content': 'source'}])
    assert ''.join(block.text for block in reply.blocks) == '{"ok":true}'
    assert len(calls) == 1 and rows(shim)[0][0] == 'complete'


def test_known_native_failure_retains_partial_original_for_explicit_reconciliation(arm, monkeypatch):
    from infermatrix_copilot.providers.container_execution import ContainerExecution
    from infermatrix_copilot.providers.container_protocol import receipt_line
    from infermatrix_copilot.process_execution import ProcessOutcome
    settings, calls, shim, _ = arm
    stdout = '{"type":"turn.failed","error":{"message":"native refusal"}}\n'
    def fail(self, **kwargs):
        calls.append((self, kwargs))
        envelope = json.loads(kwargs['input'])
        return ProcessOutcome(stdout, receipt_line(envelope,
            {'stdout': hashlib.sha256(stdout.encode()), 'stderr': hashlib.sha256(b'')}, 7, None), 7)
    monkeypatch.setattr(ContainerExecution, 'run', fail)
    gateway = model_gateway(settings, shim=shim)
    with pytest.raises(ModelUnavailable):
        gateway.call_json(ModelRole('judge', 'codex', 'fixture'), system='rubric', prompt='source')
    partial, = shim.parent.parent.glob('*.partial.json')
    receipt = json.loads(partial.read_bytes())
    assert receipt['terminal_failure'] and not receipt['native_complete']
    assert rows(shim) == [('reserved', None)] and len(calls) == 1


def test_recorder_failure_leaves_the_original_count_reserved(arm):
    settings, calls, shim, _ = arm
    def unavailable(entry):
        raise OSError('original observer unavailable')
    gateway = model_gateway(settings, shim=shim, recorder=unavailable)
    with pytest.raises(OSError, match='original observer unavailable'):
        gateway.call_json(ModelRole('judge', 'codex', 'fixture'), system='rubric', prompt='source')
    assert len(calls) == 1 and rows(shim) == [('reserved', None)]


def test_legacy_package_uses_only_its_original_gateway(arm, monkeypatch):
    settings, calls, shim, _ = arm
    monkeypatch.setattr('arm_runtime.supports_shared_execution', lambda: False)
    monkeypatch.delenv('SHADOW_ARM_CONFIG')
    observer = [].append
    factory = lambda _: object()
    gateway = model_gateway(settings, shim=shim, recorder=observer, transport_factory=factory)
    assert gateway._settings is settings and gateway._recorder is observer
    assert gateway._transport('codex') is not None and not calls


def test_frozen_orchestration_and_native_deadlines_have_separate_owners(arm):
    settings, calls, shim, _ = arm
    path = shim.parent / 'config.json'
    value = json.loads(path.read_bytes());value['plan']['host_timeout_s'] = 120
    path.write_text(json.dumps(value))
    assert host_timeout(shim) == 120
    outer = legacy_settings(settings, shim)
    assert outer.strict_backend_timeout_s == 120 and outer is not settings
    assert model_gateway(settings, shim=shim)._settings.strict_backend_timeout_s == 30
    value['plan']['host_timeout_s'] = 10;path.write_text(json.dumps(value))
    with pytest.raises(Refused, match='deadline'):
        legacy_settings(settings, shim)
    assert not calls


def test_case_deadlines_reach_sdk_legacy_host_and_wait_without_changing_budget(arm):
    from arm_runtime import workflow_timeout
    from ledger import Ledger
    from codex_guard import digest
    settings, calls, shim, plan = arm
    path = case_profiles(shim, plan)
    plan['case_deadlines'] = {
        'case': {'native_timeout_s': 300, 'host_timeout_s': 360, 'workflow_timeout_s': 900},
        'other': {'native_timeout_s': 30, 'host_timeout_s': 45, 'workflow_timeout_s': 100}}
    value = json.loads(path.read_text()); value['plan'] = plan; path.write_text(json.dumps(value))
    assert host_timeout(shim) == 360 and workflow_timeout(shim) == 900
    assert legacy_settings(settings, shim).strict_backend_timeout_s == 360
    with sdk_runtime({'_env_file': None}, shim=shim, for_worker=True) as composed:
        assert composed['strict_backend_timeout_s'] == 300
    gateway = model_gateway(settings, shim=shim)
    assert gateway._settings.strict_backend_timeout_s == 300
    assert gateway.call_json(ModelRole('judge', 'codex', 'fixture'), system='rubric', prompt='source').data == {'ok': True}
    assert len(calls) == 1 and plan['native_timeout_s'] == 30
    ledger = Ledger(shim.parent.parent / 'calls.sqlite', plan)
    try:
        assert ledger.db.execute('SELECT digest FROM plan').fetchone()[0] == digest(plan)
    finally:
        ledger.close()


@pytest.mark.parametrize('damage', ['null-table', 'missing-case', 'extra-case', 'missing-field',
    'extra-field', 'native-cap', 'boolean-native', 'nan-native', 'host-before-native',
    'boolean-host', 'workflow-short', 'workflow-cap', 'invalid-unused-case'])
def test_case_deadline_refusal_precedes_controls_and_reservation(arm, monkeypatch, damage):
    settings, calls, shim, plan = arm
    path = case_profiles(shim, plan)
    table = {case: {'native_timeout_s': 30, 'host_timeout_s': 45, 'workflow_timeout_s': 150}
             for case in plan['native_call_limits']}
    if damage == 'null-table': table = None
    elif damage == 'missing-case': table.pop('other')
    elif damage == 'extra-case': table['unknown'] = table['other']
    elif damage == 'missing-field': table['case'].pop('workflow_timeout_s')
    elif damage == 'extra-field': table['case']['extra'] = 1
    elif damage == 'native-cap': table['case']['native_timeout_s'] = 601
    elif damage == 'boolean-native': table['case']['native_timeout_s'] = True
    elif damage == 'nan-native': table['case']['native_timeout_s'] = float('nan')
    elif damage == 'host-before-native': table['case']['host_timeout_s'] = 20
    elif damage == 'boolean-host': table['case']['host_timeout_s'] = True
    elif damage == 'workflow-short': table['case']['workflow_timeout_s'] = 119
    elif damage == 'workflow-cap': table['case']['workflow_timeout_s'] = 7201
    else: table['other']['workflow_timeout_s'] = 10
    plan['case_deadlines'] = table
    value = json.loads(path.read_text()); value['plan'] = plan; path.write_text(json.dumps(value))
    controls = []
    monkeypatch.setattr(CodexTransport, 'identity_probe', lambda *args: controls.append(1))
    with pytest.raises(Refused, match='deadline'):
        model_gateway(settings, shim=shim)
    assert not controls and not calls and not (shim.parent.parent / 'calls.sqlite').exists()


def test_old_native_deadline_cap_is_not_raised_by_new_selector(arm):
    settings, calls, shim, plan = arm
    path = shim.parent / 'config.json'; value = json.loads(path.read_text())
    value['plan']['native_timeout_s'] = 300; path.write_text(json.dumps(value))
    with pytest.raises(Refused, match='at most 180'):
        model_gateway(settings, shim=shim)
    assert not calls


def test_sdk_composition_accounts_for_auxiliary_calls_in_async_workflow_threads(arm):
    import asyncio
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.providers import llm_for
    settings, calls, shim, _ = arm
    overrides = {'_env_file': None, 'strict_backend': 'codex', 'strict_backend_model': 'fixture',
                 'strict_backend_cli': str(shim), 'run_root': shim.parent / 'runs', 'metrics_enabled': False}
    with sdk_runtime(overrides, shim=shim) as configured:
        selected = Settings(**configured)
        assert selected.strict_backend_effort == 'high'
        assert selected.native_session_profiles == selected.native_completion_profiles
        async def call():
            return await asyncio.to_thread(lambda: llm_for(selected).create(
                system='repair rubric', messages=[{'role': 'user', 'content': 'frozen repair'}]))
        assert asyncio.run(call()).text == '{"ok":true}'
    assert len(calls) == 1 and rows(shim)[0][0] == 'complete'


def test_durable_sdk_parent_cannot_dispatch_an_unaccounted_model(arm):
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.providers import llm_for
    settings, calls, shim, _ = arm
    with sdk_runtime({'_env_file': None, 'strict_backend': 'codex'}, shim=shim, for_worker=True) as configured:
        with pytest.raises(Refused, match='must execute in their worker'):
            llm_for(Settings(**configured)).create(system='rubric', messages=[{'role': 'user', 'content': 'source'}])
    assert not calls and not (shim.parent.parent / 'calls.sqlite').exists()


def test_unfrozen_forge_read_refuses_current_arm_before_archive_or_reservation(arm, monkeypatch):
    settings, calls, shim, _ = arm
    marker = shim.parent / 'unfrozen.json';marker.write_text('{}')
    monkeypatch.setenv('SHADOW_FORGE_REFUSAL', str(marker))
    gateway = model_gateway(settings, shim=shim)
    with pytest.raises(Refused, match='unfrozen forge request'):
        gateway.call_json(ModelRole('judge', 'codex', 'fixture', effort='high'), system='rubric', prompt='source')
    assert not calls and not (shim.parent.parent / 'calls.sqlite').exists()


def test_identical_calls_in_distinct_logical_steps_charge_once_per_original_intent(arm):
    from infermatrix_copilot.trace_store import trace_context
    settings, calls, shim, _ = arm
    config = shim.parent / 'config.json';value = json.loads(config.read_bytes())
    value['plan']['call_identity'] = 'agent-step/1';config.write_text(json.dumps(value))
    gateway = model_gateway(settings, shim=shim)
    role = ModelRole('judge', 'codex', 'fixture', effort='high')
    for step in ('review#logic', 'review#contracts'):
        with trace_context(unit_id='original-review', attempt=1, agent_step=step):
            assert gateway.call_json(role, system='same rubric', prompt='same draft').data == {'ok': True}
    assert len(calls) == 2 and [status for status, _ in rows(shim)] == ['complete', 'complete']
    with trace_context(unit_id='original-review', attempt=1, agent_step='review#logic'):
        with pytest.raises(Refused, match='original evaluation attempt cannot be replaced'):
            gateway.call_json(role, system='same rubric', prompt='same draft')
    assert len(calls) == 2


def test_sdk_composition_uses_the_frozen_knowledge_resource_instead_of_package_default(arm):
    settings, calls, shim, _ = arm
    knowledge = shim.parent / 'frozen-knowledge';knowledge.mkdir()
    path = shim.parent / 'config.json';value = json.loads(path.read_bytes())
    value['plan']['shared_execution']['knowledge_by_arm']['candidate'] = str(knowledge)
    path.write_text(json.dumps(value))
    with sdk_runtime({'_env_file': None, 'strict_backend': 'codex'}, shim=shim) as configured:
        assert configured['knowledge_dir'] == knowledge
    assert not calls


def test_caller_recorder_keeps_admission_and_native_execution_in_its_original_store(arm):
    from infermatrix_copilot.kb_service.runtime import trace_recorder
    from infermatrix_copilot.providers.subscription_admission import _store
    settings, calls, shim, _ = arm
    original = TraceStore(shim.parent / 'state' / 'init' / 'traces')
    gateway = model_gateway(settings, shim=shim, recorder=trace_recorder(original))
    # Auth admission and complete_native independently resolve the recorder.
    # They must select the same original root, even when settings point elsewhere.
    assert _store(gateway._settings, gateway._recorder).root == original.root
    assert Path(gateway._settings.trace_store_root) != original.root
    assert not calls
    gateway.call_json(ModelRole('judge', 'codex', 'fixture'), system='rubric', prompt='source')
    state, raw = rows(shim)[0]
    receipt = json.loads(raw)
    assert state == 'complete' and receipt['trace_root'] == str(original.root)
    record, = [row for row, _ in original.iter_records(kind='model_call')]
    original.validate_native_attempt(record)
    assert len(calls) == 1



def knowledge_plan(arm):
    settings, calls, shim, plan = arm
    path = shim.parent / 'config.json'
    config = json.loads(path.read_bytes())
    config['plan']['call_identity'] = 'knowledge-dispatch/1'
    path.write_text(json.dumps(config))
    return settings, calls, shim


def test_known_domain_attempts_with_identical_input_have_distinct_native_intents(arm):
    from infermatrix_copilot.kb_service.quality_review import _attempt_scope
    settings, calls, shim = knowledge_plan(arm)
    gateway = model_gateway(settings, shim=shim)
    judge = ModelRole('judge', 'codex', 'fixture')
    for attempt in (1, 2):
        with _attempt_scope('a' * 64, attempt):
            assert gateway.call_json(judge, system='rubric', prompt='identical source').data['ok']
    assert len(calls) == 2 and len(rows(shim)) == 2
    assert all(status == 'complete' for status, _ in rows(shim))
    with _attempt_scope('a' * 64, 2):
        with pytest.raises(Refused, match='cannot be replaced'):
            gateway.call_json(judge, system='rubric', prompt='identical source')
    assert len(calls) == 2 and len(rows(shim)) == 2


@pytest.mark.parametrize('scope', [None, {}, {'dispatch_sha256': 'a'*64, 'attempt': 0},
    {'dispatch_sha256': 'a'*64, 'attempt': True}, {'dispatch_sha256': 'wrong', 'attempt': 1}])
def test_knowledge_identity_requires_original_dispatch_metadata_before_model(arm, scope):
    from infermatrix_copilot.trace_store import trace_context
    settings, calls, shim = knowledge_plan(arm)
    gateway = model_gateway(settings, shim=shim)
    with trace_context(knowledge_dispatch=scope):
        with pytest.raises(Refused, match='original knowledge dispatch'):
            gateway.call_json(ModelRole('judge', 'codex', 'fixture'), system='rubric', prompt='source')
    assert not calls and not (shim.parent.parent / 'calls.sqlite').exists()


def test_unknown_knowledge_attempt_blocks_a_different_attempt(arm, monkeypatch):
    from infermatrix_copilot.kb_service.quality_review import _attempt_scope
    from infermatrix_copilot.providers.container_execution import ContainerExecution
    from infermatrix_copilot.sandbox.base import SandboxPending
    settings, calls, shim = knowledge_plan(arm)
    sent = []
    def unknown(*args, **kwargs):
        sent.append(1)
        raise SandboxPending('original worker response missing', prior_execution=True)
    monkeypatch.setattr(ContainerExecution, 'run', unknown)
    gateway = model_gateway(settings, shim=shim)
    for attempt in (1, 2):
        with _attempt_scope('a'*64, attempt):
            with pytest.raises((SandboxPending, ModelUnavailable, Refused)):
                gateway.call_json(ModelRole('judge', 'codex', 'fixture'), system='rubric', prompt='source')
    assert len(sent) == 1 and rows(shim) == [('reserved', None)]


def case_profiles(shim, plan):
    from copy import deepcopy
    shared = plan['shared_execution']
    profile = shared.pop('profile')
    shared.update(schema='shared-sandbox/2', profiles={'codex': profile})
    plan['provider_binary_sha256_by_provider'] = {'codex': plan.pop('provider_binary_sha256')}
    plan['execution_profile']['roles'] = {name:{'provider':'codex','model':'fixture','effort':'high'}
                                        for name in ('generator','judge')}
    plan['native_call_limits']['other'] = 1
    selected = deepcopy(profile);selected['native_cli'] = '/opt/case-policy/codex'
    plan['native_profiles_by_case'] = {'case': {'codex':selected}, 'other': {'codex':deepcopy(profile)}}
    path = shim.parent / 'config.json'
    value = json.loads(path.read_text());value['plan']=plan;path.write_text(json.dumps(value))
    return path


def test_application_case_profile_uses_canonical_provider_and_original_budget_plan(arm):
    from ledger import Ledger
    from codex_guard import digest
    settings,calls,shim,plan = arm
    case_profiles(shim,plan)
    gateway = model_gateway(settings,shim=shim,recorder=None)
    assert gateway.call_json(ModelRole('judge','codex','fixture'),system='rubric',prompt='frozen').data=={'ok':True}
    assert len(calls)==1 and json.loads(calls[0][1]['input'])['argv'][0]=='/opt/case-policy/codex'
    assert plan['shared_execution']['profiles']['codex'].get('native_cli')!='/opt/case-policy/codex'
    ledger = Ledger(shim.parent.parent/'calls.sqlite',plan)
    try:assert ledger.db.execute('SELECT digest FROM plan').fetchone()[0]==digest(plan)
    finally:ledger.close()


def test_case_knowledge_reaches_sdk_without_changing_original_plan_or_budget(arm, tmp_path):
    settings, calls, shim, plan = arm
    path = case_profiles(shim, plan)
    selected = tmp_path / 'full-review-knowledge'; selected.mkdir()
    other = tmp_path / 'init-knowledge'; other.mkdir()
    plan['native_knowledge_by_case'] = {
        'case': {name: str(selected) for name in ('baseline', 'candidate')},
        'other': {name: str(other) for name in ('baseline', 'candidate')}}
    value = json.loads(path.read_text()); value['plan'] = plan; path.write_text(json.dumps(value))
    with sdk_runtime({'_env_file': None}, shim=shim, for_worker=True) as composed:
        assert composed['knowledge_dir'] == selected
    assert plan['shared_execution']['knowledge_by_arm'] == {}
    assert calls == [] and not (shim.parent.parent / 'calls.sqlite').exists()


@pytest.mark.parametrize('damage', ['null-table', 'missing-case', 'extra-case', 'missing-arm',
    'extra-arm', 'relative-path', 'empty-path', 'boolean-path', 'null-path', 'invalid-unused-case'])
def test_case_knowledge_refuses_before_identity_controls_or_reservation(arm, monkeypatch, damage):
    settings, calls, shim, plan = arm
    path = case_profiles(shim, plan)
    table = {case: {name: str(shim.parent) for name in ('baseline', 'candidate')}
             for case in plan['native_call_limits']}
    if damage == 'null-table': table = None
    elif damage == 'missing-case': table.pop('other')
    elif damage == 'extra-case': table['unplanned'] = table['other']
    elif damage == 'missing-arm': table['case'].pop('baseline')
    elif damage == 'extra-arm': table['case']['unplanned'] = str(shim.parent)
    elif damage == 'relative-path': table['case']['candidate'] = 'relative'
    elif damage == 'empty-path': table['case']['candidate'] = ''
    elif damage == 'boolean-path': table['case']['candidate'] = True
    elif damage == 'null-path': table['case']['candidate'] = None
    else: table['other']['baseline'] = 'relative-unused'
    plan['native_knowledge_by_case'] = table
    value = json.loads(path.read_text()); value['plan'] = plan; path.write_text(json.dumps(value))
    controls = []
    monkeypatch.setattr(CodexTransport, 'identity_probe', lambda *args: controls.append(1))
    with pytest.raises(Refused, match='knowledge paths'):
        model_gateway(settings, shim=shim, recorder=None)
    assert not controls and not calls and not (shim.parent.parent / 'calls.sqlite').exists()


@pytest.mark.parametrize('damage',['missing-case','extra-case','invalid-unused-profile'])
def test_case_profile_refusal_precedes_identity_controls_reservation_and_dispatch(arm,damage):
    settings,calls,shim,plan = arm
    path = case_profiles(shim,plan)
    if damage=='missing-case':plan['native_profiles_by_case'].pop('other')
    elif damage=='extra-case':plan['native_profiles_by_case']['unplanned']=plan['native_profiles_by_case']['case']
    else:plan['native_profiles_by_case']['other']['codex']['unplanned-option']=True
    value=json.loads(path.read_text());value['plan']=plan;path.write_text(json.dumps(value))
    with pytest.raises(Refused):model_gateway(settings,shim=shim,recorder=None)
    assert calls==[] and not (shim.parent.parent/'calls.sqlite').exists()


def test_auxiliary_role_binding_preserves_role_identity_and_exact_model_pin(arm):
    settings, calls, shim, plan = arm
    plan['execution_profile']['roles'] = {name:{'provider':'codex','model':'fixture','effort':'high'}
                                         for name in ('generator','judge')}
    plan['execution_profile']['role_bindings'] = {'pr-reviewer':'judge'}
    path = shim.parent / 'config.json'; data=json.loads(path.read_text());data['plan']=plan;path.write_text(json.dumps(data))
    records=[];gateway=model_gateway(settings,shim=shim,recorder=records.append)
    reply=gateway.call_json(ModelRole('pr-reviewer','codex','fixture','high'),system='aggregate',prompt='exact frozen head')
    assert reply.data=={'ok':True} and len(calls)==1 and records[0]['role']=='pr-reviewer'
    with pytest.raises((ModelUnavailable,Refused),match='unplanned model role'):
        gateway.call_json(ModelRole('other-reviewer','codex','fixture','high'),system='aggregate',prompt='other')
    assert len(calls)==1


def test_invocation_profile_keeps_opaque_step_name_in_original_call_identity(arm):
    settings,calls,shim,plan=arm
    plan['execution_profile']['roles']={name:{'provider':'codex','model':'fixture','effort':'high'} for name in ('generator','judge')}
    plan['execution_profile']['invocation_roles_by_case']={'case':{'harness_session':'judge'}}
    path=shim.parent/'config.json';data=json.loads(path.read_text());data['plan']=plan;path.write_text(json.dumps(data))
    from arm_runtime import ArmRuntime,digest
    runtime=ArmRuntime(path,shim=shim)
    request={'provider_id':'codex','model':'fixture','effort':'high','role':'reviewer.delegate-1','kind':'harness_session'}
    first=runtime.acquire(request);assert first.request_digest==digest(request)
    second=runtime.acquire({**request,'role':'reviewer.delegate-2'})
    assert first.call_id!=second.call_id
    with pytest.raises(Refused,match='frozen model/effort'):
        runtime.acquire({**request,'model':'unplanned'})
    with pytest.raises(Refused,match='unplanned model role'):
        runtime.acquire({**request,'kind':'harness'})
    assert not calls
