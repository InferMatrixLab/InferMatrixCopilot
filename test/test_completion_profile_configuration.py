"""Trusted configuration reaches canonical callers and keeps original identities."""
import json
import socket
import sys

import pytest

from infermatrix_copilot.app.completion_execution import completion_settings_for_run
from infermatrix_copilot.app.reservation import RunReservation
from infermatrix_copilot.config import Settings
from infermatrix_copilot.providers import llm_for
from infermatrix_copilot.providers.codex import CodexTransport
from infermatrix_copilot.providers.completion import complete_native
from infermatrix_copilot.providers.completion_profiles import CompletionPolicyError
from infermatrix_copilot.providers.completion_profiles import normalize_completion_profiles
from infermatrix_copilot.providers.container_execution import ContainerExecution
from infermatrix_copilot.providers.process import ProcessOutcome
from infermatrix_copilot.providers.registry import transport_for_id
from infermatrix_copilot.task_spec import TaskSpec
from infermatrix_copilot.trace_store import NativeCompletionRecorder, TraceStore


@pytest.fixture
def configured(settings, tmp_path, monkeypatch):
    docker = tmp_path / 'docker'
    docker.write_text('#!/bin/sh\nexit 99\n')
    docker.chmod(0o700)
    daemon = socket.socket(socket.AF_UNIX)
    daemon.bind(str(tmp_path / 'daemon.sock'))
    profile = dict(kind='docker', docker=str(docker), socket=str(tmp_path / 'daemon.sock'),
                   image='sha256:' + 'a' * 64)
    settings = settings.model_copy(update={'strict_backend': 'codex', 'strict_backend_model': 'fixture',
        'native_completion_profiles': normalize_completion_profiles({'codex': profile}),
        'trace_store_root': str(tmp_path / 'traces'),
        'metrics_enabled': False})
    calls = []

    def run(execution, **kwargs):
        calls.append((execution, kwargs))
        events = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"ok":true}'}},
                  {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2}}]
        return ProcessOutcome('\n'.join(map(json.dumps, events)), '', 0)

    monkeypatch.setattr(ContainerExecution, 'run', run)
    monkeypatch.setattr(CodexTransport, 'cli_path', lambda _: pytest.fail('host CLI is not this profile'))
    monkeypatch.setattr(CodexTransport, 'auth_gap', lambda _: pytest.fail('host login is not this profile'))
    try:
        yield settings, calls
    finally:
        daemon.close()


def records(settings):
    store = TraceStore(settings.trace_store_root)
    rows = [row for row, _ in store.iter_records() if row['kind'] == 'model_call']
    for row in rows:
        store.validate_native_attempt(row)
    return store, rows


def test_cached_harness_uses_distinct_durable_profiles_and_one_receipt(configured):
    settings, calls = configured
    llm = llm_for(settings)
    assert llm.available
    for index in range(2):
        assert llm.create(system='policy', messages=[{'role': 'user', 'content': f'evidence {index}'}],
                          role='triage').text == '{"ok":true}'
    assert len(calls) == 2 and calls[0][0].root != calls[1][0].root
    assert getattr(llm._transport, 'completion_profile', None) is None
    store, rows = records(settings)
    assert len(rows) == 2
    for index, (row, (execution, _)) in enumerate(zip(rows, calls)):
        assert execution.root == store.root / 'attempts' / row['result']['native_attempt_id'] / 'execution'
        assert store.blob(row['inputs']['messages']) == json.dumps([{'role': 'user', 'content': f'evidence {index}'}])
        assert row['model']['role'] == 'triage' and row['model']['provider'] == 'harness:codex'
        assert row['usage']['input_tokens'] == 10
        assert row['result']['usd'] is None and row['result']['subscription'] is None
        descriptor = json.loads((execution.root.parent / 'execution-profile.json').read_text())
        assert descriptor['config']['image'] == settings.native_completion_profiles['codex']['image']


def test_knowledge_and_judge_use_same_profile_boundary(configured, monkeypatch):
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
    from infermatrix_copilot.kb_service.runtime import trace_recorder
    from infermatrix_copilot.improve.judges import JudgeSpec, run_judge

    settings, calls = configured
    gateway = ModelGateway(settings, recorder=trace_recorder(TraceStore(settings.trace_store_root)))
    role = ModelRole('generator', 'codex', 'fixture', 'high')
    assert gateway.subscription_billing(role) is False
    assert gateway.call_json(role, system='policy', prompt='frozen knowledge').data == {'ok': True}
    # The public judge entry constructs Settings; only its trusted host settings
    # are supplied here. Provider, decoding, archives and accounting remain real.
    monkeypatch.setattr('infermatrix_copilot.config.Settings', lambda **_: settings)
    assert run_judge(JudgeSpec('cli', 'fixture', 'codex'), system='policy', prompt='frozen judge') == {'ok': True}
    store, rows = records(settings)
    assert len(rows) == len(calls) == 2
    assert rows[0]['model']['effort'] == 'high'
    assert store.blob(rows[0]['inputs']['prompt']) == 'frozen knowledge'
    assert rows[1]['model']['role'] == 'judge'
    assert rows[1]['result']['subscription'] is None


@pytest.mark.parametrize('damage', ['image', 'provider', 'extra_field', 'socket', 'trace', 'runner', 'payload'])
def test_bad_configuration_fails_without_dispatch(configured, damage):
    settings, calls = configured
    values = json.loads(json.dumps(settings.native_completion_profiles))
    if damage in ('image', 'extra_field', 'provider'):
        if damage == 'image':
            values['codex']['image'] = 'mutable:latest'
        elif damage == 'extra_field':
            values['codex']['unknown'] = True
        else:
            values['cursor'] = values.pop('codex')
        with pytest.raises(ValueError):
            Settings(_env_file=None, native_completion_profiles=values)
    else:
        if damage == 'socket':
            values['codex']['socket'] += '.missing'
        settings = settings.model_copy(update={'native_completion_profiles': values,
            **({'trace_store_root': ''} if damage == 'trace' else {})})
        transport = transport_for_id(settings, 'codex')
        if damage == 'runner':
            transport.runner = lambda **_: pytest.fail('host runner must not dispatch')
        with pytest.raises(CompletionPolicyError) as caught:
            complete_native(lambda: transport, request={'system': 's', 'messages': []},
                identity={'provider': 'codex'}, settings=settings, record_payload=damage != 'payload')
        assert caught.value.native_call_sent is False and caught.value.allow_fallback is False
    assert not calls


def test_recording_failure_retains_unknown_outcome_without_fallback(configured, monkeypatch):
    from infermatrix_copilot.budgeting import native_outcome_unknown
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole, ModelUnavailable

    settings, calls = configured
    store = TraceStore(settings.trace_store_root)
    error = ModelUnavailable('receipt unavailable')
    monkeypatch.setattr(store, 'append', lambda *_, **__: (_ for _ in ()).throw(error))
    gateway = ModelGateway(settings, recorder=NativeCompletionRecorder(store))
    role = ModelRole('generator', 'codex', 'fixture', fallback=ModelRole('generator', 'missing', 'replacement'))
    with pytest.raises(ModelUnavailable) as caught:
        gateway.call_json(role, system='policy', prompt='evidence')
    assert len(calls) == 1 and caught.value is error
    assert error.native_call_sent and error.native_call_completed and native_outcome_unknown(error)
    assert error.allow_fallback is False
    archive = next((store.root / 'attempts').glob('*/attempt.json'))
    assert json.loads(archive.read_text())['status'] == 'inflight'


def reserve(settings, key='business-key'):
    reservation = RunReservation(settings)
    run_id, created = reservation.reserve(TaskSpec(kind='pr_review', repo='toy', pr=1),
        owner_server_id='owner', owner_server_pid=1, idempotency_key=key)
    return settings.run_root / run_id, created


def test_reservation_and_retry_restore_original_policy(configured):
    settings, _ = configured
    run_dir, created = reserve(settings)
    assert created
    changed = settings.model_copy(deep=True)
    changed.native_completion_profiles['codex']['image'] = 'sha256:' + 'b' * 64
    changed.trace_store_root += '-new'
    restored = completion_settings_for_run(changed, run_dir, resuming=True)
    assert restored.native_completion_profiles == settings.native_completion_profiles
    assert restored.trace_store_root == settings.trace_store_root
    same_dir, _ = reserve(changed)
    assert same_dir == run_dir
    disabled = changed.model_copy(update={'native_completion_profiles': {}})
    assert completion_settings_for_run(disabled, run_dir).native_completion_profiles == settings.native_completion_profiles


@pytest.mark.parametrize('damage', ['missing', 'modified', 'unbound'])
def test_original_profile_damage_refuses_reinterpretation(configured, damage):
    settings, calls = configured
    run_dir, _ = reserve(settings)
    pin = run_dir / 'native-completion.json'
    if damage == 'missing':
        pin.unlink()
    elif damage == 'modified':
        pin.write_text('{}')
    else:
        status = json.loads((run_dir / 'run_status.json').read_text())
        status.pop('native_completion_sha256')
        (run_dir / 'run_status.json').write_text(json.dumps(status))
    with pytest.raises(CompletionPolicyError):
        completion_settings_for_run(settings, run_dir, resuming=True)
    assert not calls


def test_legacy_run_never_inherits_new_container_configuration(configured):
    settings, _ = configured
    run_dir, _ = reserve(settings.model_copy(update={'native_completion_profiles': {}}))
    assert not (run_dir / 'native-completion.json').exists()
    assert completion_settings_for_run(settings, run_dir).native_completion_profiles == {}


def test_workflow_composition_restores_original_profile_into_context_and_llm(configured):
    from infermatrix_copilot.app.repository_context import RepositoryContextResolver
    from infermatrix_copilot.app.workflow_execution import WorkflowExecution
    from infermatrix_copilot.engine import StepRegistry, StepResult, StepSpec
    from infermatrix_copilot.playbooks.store import Playbook, PlaybookStep

    settings, calls = configured
    run_dir = settings.run_root / 'cli-run'
    run_dir.mkdir(parents=True)
    spec = TaskSpec(kind='pr_review', repo='toy', pr=1)
    (run_dir / 'task.json').write_text(json.dumps({'spec': spec.model_dump(), 'playbook': {}}))
    registry, images = StepRegistry(), []

    async def inspect(ctx):
        images.append(ctx.settings.native_completion_profiles['codex']['image'])
        assert ctx.llm.create(system='policy', messages=[{'role': 'user', 'content': 'evidence'}]).text == '{"ok":true}'
        return StepResult(True, checkpoint=False)

    registry.register(StepSpec('inspect', 'agent', 'read', inspect))
    plan = Playbook(name='profile', version=1, status='active', task_kinds=['pr_review'], repos=[],
                    steps=[PlaybookStep('inspect', 'inspect')])
    resolver = RepositoryContextResolver(settings)
    assert WorkflowExecution(settings, registry, resolver).run(plan, spec, run_dir, llm=llm_for(settings)).exit_code == 0
    changed = settings.model_copy(deep=True)
    changed.native_completion_profiles['codex']['image'] = 'sha256:' + 'b' * 64
    assert WorkflowExecution(changed, registry, resolver).run(plan, spec, run_dir,
        llm=llm_for(changed), resuming=True).exit_code == 0
    assert images == [settings.native_completion_profiles['codex']['image']] * 2
    assert len(calls) == 2
    records(settings)


@pytest.mark.parametrize('effort', ['', 'high'])
def test_reserved_child_receives_original_profile_environment(configured, monkeypatch, effort):
    from infermatrix_copilot.app.run_service import RunService

    settings, calls = configured
    settings = settings.model_copy(update={'strict_backend_effort': effort})
    run_dir, _ = reserve(settings)
    changed = settings.model_copy(deep=True)
    changed.native_completion_profiles['codex']['image'] = 'sha256:' + 'b' * 64
    changed.trace_store_root += '-new'
    changed.strict_backend_effort = 'low'
    # Exercise the real launch path without starting its background worker or a
    # model child. Only Popen is replaced; reservation and environment are real.
    service = RunService.__new__(RunService)
    service.worker_prefix = (sys.executable,)
    service.settings, service.run_root = changed, settings.run_root
    service._reap = lambda: None
    captured = []

    class Child:
        pid, returncode = 424243, 0
        def wait(self):
            return 0

    def popen(*args, **kwargs):
        captured.append(kwargs['env'])
        return Child()

    monkeypatch.setattr('infermatrix_copilot.app.run_service.subprocess.Popen', popen)
    service._launch(run_dir.name)
    assert json.loads(captured[0]['NATIVE_COMPLETION_PROFILES']) == settings.native_completion_profiles
    assert captured[0]['TRACE_STORE_ROOT'] == settings.trace_store_root
    assert captured[0]['STRICT_BACKEND_EFFORT'] == effort
    assert not calls


def test_session_profile_is_frozen_with_the_run_without_changing_legacy_pins(configured):
    settings, _ = configured
    sessions = settings.native_completion_profiles
    settings = settings.model_copy(update={'native_session_profiles': sessions})
    run_dir, _ = reserve(settings)
    pin = json.loads((run_dir / 'native-completion.json').read_text())
    assert pin['schema'] == 'run-completion/4' and pin['session_profiles'] == sessions
    changed = settings.model_copy(deep=True)
    changed.native_session_profiles['codex']['image'] = 'sha256:' + 'b' * 64
    assert completion_settings_for_run(changed, run_dir, resuming=True).native_session_profiles == sessions
    legacy_dir, _ = reserve(settings.model_copy(update={'native_session_profiles': {}}), key='legacy')
    assert json.loads((legacy_dir / 'native-completion.json').read_text())['schema'] == 'run-completion/4'
    assert completion_settings_for_run(settings, legacy_dir).native_session_profiles == {}
