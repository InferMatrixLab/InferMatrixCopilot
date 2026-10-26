import json

import pytest

from infermatrix_copilot.budgeting import bind_call_budget
from infermatrix_copilot.config import Settings
from infermatrix_copilot.improve.judges import JudgeSpec, run_judge
from infermatrix_copilot.providers.completion import complete_native
from infermatrix_copilot.providers.container_execution import (
    ContainerCompletionProfile, ContainerExecution, ContainerPending,
)
from infermatrix_copilot.providers.process import ProcessOutcome
from infermatrix_copilot.providers.registry import transport_for_id


@pytest.fixture
def contained(tmp_path, monkeypatch):
    runtime = ContainerExecution(tmp_path / 'attempt', docker='/docker', socket='/daemon')
    profile = ContainerCompletionProfile(runtime, 'sha256:' + 'a' * 64)
    events = [
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"ok":true}'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2}},
    ]
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return ProcessOutcome('\n'.join(map(json.dumps, events)), '', 0)

    monkeypatch.setattr(runtime, 'run', run)
    return profile, events, calls


def test_judge_selects_contained_profile_through_canonical_transport(contained):
    profile, _, calls = contained
    verdict = run_judge(JudgeSpec('cli', 'fixture-model', 'codex'),
        system='judge policy', prompt='frozen evidence', completion_profile=profile)
    assert verdict == {'ok': True}
    assert len(calls) == 1
    envelope = json.loads(calls[0]['input'])
    argv = envelope['argv']
    assert argv[0] == '/usr/local/bin/codex'
    assert '--strict-config' in argv and '--ignore-user-config' in argv
    assert '--dangerously-bypass-approvals-and-sandbox' in argv
    assert argv[argv.index('-m') + 1] == 'fixture-model'
    assert 'frozen evidence' in envelope['input'] and 'judge policy' in envelope['input']
    assert calls[0]['argv'] == ['/usr/local/bin/python', '-m', 'infermatrix_copilot.container_worker']


def test_containment_does_not_change_no_tools_capability():
    transport = transport_for_id(Settings(_env_file=None), 'codex')
    with pytest.raises(ValueError, match='builtin_tools_off'):
        transport.validate_completion()
    assert 'builtin_tools_off' not in transport.spec.capabilities


def test_container_billing_cannot_be_attested_by_host_login(contained, monkeypatch):
    profile, _, calls = contained
    transport = transport_for_id(Settings(_env_file=None), 'codex', completion_profile=profile)
    monkeypatch.setattr(transport, 'cli_path', lambda: pytest.fail('must not probe a different host login'))
    assert transport.subscription_billing is False
    assert not calls


@pytest.mark.parametrize('ending', ['success', 'tool', 'error', 'missing_terminal'])
def test_contained_completion_accounting_and_rejections(contained, ending):
    profile, events, calls = contained
    if ending == 'tool':
        events.insert(0, {'type': 'item.completed', 'item': {'type': 'command_execution'}})
    elif ending == 'error':
        events.append({'type': 'turn.failed', 'error': {'message': 'failed'}})
    elif ending == 'missing_terminal':
        events[-1]['type'] = 'usage.update'
    transport = transport_for_id(Settings(_env_file=None), 'codex', completion_profile=profile)
    settled = []
    with bind_call_budget('test', lambda _: 'ticket', lambda _, facts: settled.append(dict(facts))):
        if ending == 'success':
            reply, _, _, _ = complete_native(lambda: transport, request={'system': 's', 'messages': []})
            assert reply.text == '{"ok":true}'
        else:
            with pytest.raises(RuntimeError):
                complete_native(lambda: transport, request={'system': 's', 'messages': []})
    assert len(calls) == len(settled) == 1
    assert settled[0]['usage']['input_tokens'] == 10
    assert settled[0]['usage']['output_tokens'] == 2
    assert settled[0]['outcome'] == ('completed' if ending == 'success' else 'failed')


def test_reserved_attempt_rejected_in_completion_preflight(contained):
    profile, _, calls = contained
    transport = transport_for_id(Settings(_env_file=None), 'codex', completion_profile=profile)
    profile.execution.root.mkdir()
    (profile.execution.root / 'binding.json').write_text('{}')
    with pytest.raises(ContainerPending) as caught:
        complete_native(lambda: transport, request={'system': 's', 'messages': []})
    assert caught.value.native_call_sent is False
    assert not calls


@pytest.mark.parametrize('binding', ['{}', 'unreadable original binding'])
@pytest.mark.parametrize('failure_at', [None, 'record', 'archive_finish', 'settlement'])
@pytest.mark.parametrize('pending_stage', ['primary', 'fallback'])
def test_reserved_container_keeps_generator_dispatch_pending_without_fallback(contained, binding, failure_at, pending_stage):
    from types import SimpleNamespace

    from infermatrix_copilot.budgeting import native_outcome_unknown
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole, ModelUnavailable
    from infermatrix_copilot.kb_service.quality_review import DispatchJournal
    from infermatrix_copilot.llm import Block, Reply

    profile, _, calls = contained
    transport = transport_for_id(Settings(_env_file=None), 'codex', completion_profile=profile)
    profile.execution.root.mkdir()
    retained = profile.execution.root / 'binding.json'
    retained.write_text(binding)
    selected, settled, replacements = [], [], []
    infrastructure_failure = ModelUnavailable('infrastructure unavailable')

    class Recorder:
        def begin_call(self, _):
            def finish(*args, **kwargs):
                if failure_at == 'archive_finish':
                    raise infrastructure_failure
            return SimpleNamespace(payload=lambda: {}, finish=finish)

        def __call__(self, entry):
            if failure_at == 'record':
                raise infrastructure_failure
            return {}

    def settle(_, facts):
        settled.append(dict(facts))
        if failure_at == 'settlement':
            raise infrastructure_failure

    def replace(**kwargs):
        replacements.append(kwargs)
        return Reply(blocks=[Block(type='text', text='{"replacement":true}')])

    def factory(provider):
        selected.append(provider)
        if provider == 'missing':
            raise ModelUnavailable('known unavailable before dispatch')
        return transport if provider == 'codex' else SimpleNamespace(complete=replace)

    gateway = ModelGateway(None, transport_factory=factory, recorder=Recorder())
    role = ModelRole('generator', 'codex', 'original',
                     fallback=ModelRole('generator', 'claude-code', 'replacement'))
    if pending_stage == 'fallback':
        role = ModelRole('generator', 'missing', 'unavailable',
                         fallback=ModelRole('generator', 'codex', 'original'))
    pending = {}
    task = {'unit': 'original-task', 'attempt': 1}
    journal = DispatchJournal(pending, lambda row: row, lambda update: update())
    scope = journal.begin(task)
    with bind_call_budget('test', lambda _: 'ticket', settle):
        with pytest.raises(ModelUnavailable, match='infrastructure unavailable' if failure_at else 'already reserved') as caught:
            gateway.call_json(role, system='policy', prompt='original evidence')
    error = caught.value
    if failure_at and pending_stage == 'primary':
        assert error is infrastructure_failure
    assert not error.allow_fallback and native_outcome_unknown(error)
    assert error.native_call_sent is False  # no new dispatch or charge in this invocation
    assert error.native_call_completed is False
    assert selected == (['codex'] if pending_stage == 'primary' else ['missing', 'codex'])
    assert not replacements and not calls
    assert len(settled) == len(selected) and all(facts['sent'] is False for facts in settled)
    journal.settle(task, None, error, lambda *_: [], scope=scope)
    assert scope[0] in pending
    with pytest.raises(ModelUnavailable, match='outcome unknown'):
        journal.begin(task)
    assert retained.read_text() == binding


def test_unsupported_provider_profile_fails_before_dispatch(contained):
    profile, _, calls = contained
    with pytest.raises(ValueError, match='unsupported provider'):
        transport_for_id(Settings(_env_file=None), 'claude-code', completion_profile=profile)
    assert not calls


@pytest.mark.parametrize('reason,code,error', [(None, 7, RuntimeError), ('absolute', -9, TimeoutError)])
def test_partial_verdict_cannot_hide_failed_execution(contained, monkeypatch, reason, code, error):
    profile, events, _ = contained
    monkeypatch.setattr(profile.execution, 'run', lambda **_: ProcessOutcome(
        '\n'.join(map(json.dumps, events)), '', code, reason))
    transport = transport_for_id(Settings(_env_file=None), 'codex', completion_profile=profile)
    settled = []
    with bind_call_budget('test', lambda _: 'ticket', lambda _, facts: settled.append(dict(facts))):
        with pytest.raises(error) as caught:
            complete_native(lambda: transport, request={'system': 's', 'messages': []})
    assert caught.value.native_call_sent and not caught.value.native_call_completed
    assert settled[0]['usage']['input_tokens'] == 10
    assert settled[0]['outcome'] == 'failed'
