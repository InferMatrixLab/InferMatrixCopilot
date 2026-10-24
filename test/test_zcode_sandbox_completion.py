"""Configured completion reuses the installed adapter and original native facts."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import sys

import pytest

from infermatrix_copilot.providers import completion_worker
from infermatrix_copilot.providers.completion_profiles import normalize_completion_profiles, CompletionPolicyError
from infermatrix_copilot.providers.container_execution import ContainerExecution
from infermatrix_copilot.providers.process import ProcessOutcome
from infermatrix_copilot.providers.zcode import ZCodeTransport
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.trace_store import TraceStore
from test_completion_profile_configuration import configured  # noqa: F401


@pytest.fixture
def sandbox(configured, tmp_path, monkeypatch):
    settings, _ = configured
    binary = tmp_path / 'native-zcode'
    binary.write_text('''#!/usr/bin/env python3
import json,os,stat,sys
assert stat.S_ISREG(os.fstat(1).st_mode), 'native ZCode must retain file spooling'
assert 'OPENAI_API_KEY' not in os.environ and 'GH_TOKEN' not in os.environ
personal=json.load(open(os.environ['ZCODE_PERSONAL_PROVIDER_CONFIG_FILE']))
model=personal['config']['defaultModelSelection']['modelId']
assert model=='GLM-5.3'
def emit(value):print(json.dumps(value))
emit({'type':'session.updated','payload':{'modelId':model}})
if 'rogue' in next(arg for arg in sys.argv if arg.startswith('--prompt=')):
 emit({'type':'tool.updated','payload':{'kind':'scheduled','toolName':'Read','input':{'file_path':'/etc/passwd'}}})
emit({'type':'result','response':json.dumps({'ok':True,'spooled':True}),
      'usage':{'inputTokens':34,'outputTokens':5}})
''')
    binary.chmod(0o700)
    value = dict(settings.native_completion_profiles['codex'], native_cli=str(binary))
    settings = settings.model_copy(update={'strict_backend': 'zcode', 'strict_backend_model': 'GLM-5.3',
        'native_completion_profiles': normalize_completion_profiles({'zcode': value})})
    calls = []

    def execute(execution, command, **kwargs):
        calls.append((execution, command, kwargs))
        output, errors = StringIO(), StringIO()
        with monkeypatch.context() as patch:
            patch.setenv('HOME', str(tmp_path / 'worker-home'))
            patch.delenv('ZCODE_DATA_BASE_DIR', raising=False)
            patch.delenv('ZCODE_PERSONAL_PROVIDER_CONFIG_FILE', raising=False)
            patch.setattr(sys, 'stdin', StringIO(kwargs['input']))
            with redirect_stdout(output), redirect_stderr(errors):
                try:
                    completion_worker.main()
                    code = 0
                except Exception as exc:
                    errors.write(str(exc))
                    code = 1
        for line in output.getvalue().splitlines():
            if kwargs.get('on_line'):
                kwargs['on_line']('stdout', line)
        execution.test_original_result = ProcessOutcome(output.getvalue(), errors.getvalue(), code)
        return execution.test_original_result

    monkeypatch.setattr(ContainerExecution, 'run_command', execute)
    monkeypatch.setattr(ZCodeTransport, 'auth_gap', lambda _: pytest.fail('host login cannot attest worker'))
    yield settings, calls


def test_full_gateway_uses_one_canonical_adapter_and_native_archive(sandbox):
    settings, calls = sandbox
    store = TraceStore(settings.trace_store_root)
    gateway = ModelGateway(settings, recorder=trace_recorder(store))
    role = ModelRole('generator', 'zcode', 'GLM-5.3')
    assert gateway.subscription_billing(role) is False
    for prompt in ('first', 'second'):
        reply = gateway.call_json(role, system='policy', prompt=prompt)
        assert reply.data == {'ok': True, 'spooled': True}
        assert reply.served_model == 'GLM-5.3'
    assert len(calls) == 2 and calls[0][0].root != calls[1][0].root
    rows = [row for row, _ in store.iter_records(kind='model_call')]
    assert len(rows) == 2
    for row in rows:
        store.validate_native_attempt(row)
        assert row['usage']['input_tokens'] == 34 and row['usage']['output_tokens'] == 5
        assert row['model']['provider'] == 'zcode'
        assert row['result'].get('subscription') is not True
    assert all(command[-1] == 'infermatrix_copilot.providers.completion_worker' for _, command, _ in calls)


def test_worker_policy_failure_keeps_reported_usage_and_original_process(sandbox):
    settings, calls = sandbox
    store = TraceStore(settings.trace_store_root)
    gateway = ModelGateway(settings, recorder=trace_recorder(store))
    with pytest.raises(Exception, match='complete original result'):
        gateway.call_json(ModelRole('generator', 'zcode', 'GLM-5.3'), system='policy', prompt='rogue')
    assert len(calls) == 1
    row, = [row for row, _ in store.iter_records(kind='model_call')]
    store.validate_native_attempt(row, allow_failed=True)
    assert row['error'] and row['usage']['input_tokens'] == 34
    assert row['result']['native_exit']['returncode'] == 1


def test_original_result_reader_cannot_dispatch_another_model(sandbox, monkeypatch):
    from infermatrix_copilot.providers.completion_profiles import restore_completion_transport
    from infermatrix_copilot.providers.registry import transport_for_id
    settings, calls = sandbox
    store = TraceStore(settings.trace_store_root)
    gateway = ModelGateway(settings, recorder=trace_recorder(store))
    original = gateway.call_json(ModelRole('generator', 'zcode', 'GLM-5.3'), system='policy', prompt='original')
    execution = calls[0][0]
    row, = [row for row, _ in store.iter_records(kind='model_call')]
    archive = store.root / 'attempts' / row['result']['native_attempt_id']

    def read(bound, **kwargs):
        assert bound.root == execution.root
        return execution.test_original_result

    monkeypatch.setattr(ContainerExecution, 'get_original_command_result', read)
    restored = restore_completion_transport(settings, transport_for_id(settings, 'zcode'), archive)
    reply = restored.get_completion_result(recover=True)
    assert reply.text == original.text and reply.model == 'GLM-5.3'
    with pytest.raises(CompletionPolicyError, match='cannot dispatch'):
        restored.complete(system='policy', messages=[], model='GLM-5.3')
    assert len(calls) == 1


def test_partial_worker_reply_cannot_establish_success():
    from dataclasses import asdict
    from infermatrix_copilot.llm import Block, Reply
    partial = Reply(blocks=[Block(type='text', text='partial')], stop_reason='max_tokens')
    stdout = json.dumps({'schema': completion_worker.SCHEMA, 'kind': 'result', 'value': asdict(partial)})
    assert not completion_worker.process_receipt({'stdout': stdout, 'stderr': '', 'returncode': 0,
                                                  'reason': None})['native_complete']


@pytest.mark.parametrize('stdout', ['', 'damaged worker response'])
def test_unconfirmed_worker_result_stays_pending_through_gateway(sandbox, monkeypatch, stdout):
    settings, calls = sandbox
    def lost(execution, command, **kwargs):
        calls.append((execution, command, kwargs))
        return ProcessOutcome(stdout, '', 0)
    monkeypatch.setattr(ContainerExecution, 'run_command', lost)
    gateway = ModelGateway(settings, recorder=trace_recorder(TraceStore(settings.trace_store_root)))
    with pytest.raises(Exception) as error:
        gateway.call_json(ModelRole('generator', 'zcode', 'GLM-5.3'), system='policy', prompt='original')
    assert getattr(error.value, 'native_outcome_pending', False)
    assert not getattr(error.value, 'allow_fallback', True)
    assert len(calls) == 1


@pytest.mark.parametrize('options', [{'readonly': True}, {'output_schema': {}}, {'max_budget_usd': .1}, {'idle_timeout_s': .1}])
def test_unsupported_worker_capabilities_refuse_before_dispatch(sandbox, options):
    from infermatrix_copilot.providers.completion import complete_native
    from infermatrix_copilot.providers.registry import transport_for_id
    settings, calls = sandbox
    with pytest.raises(ValueError):
        complete_native(lambda: transport_for_id(settings, 'zcode'),
            request={'system': 'policy', 'messages': [], 'model': 'GLM-5.3', **options},
            identity={'provider': 'zcode'}, settings=settings)
    assert not calls


def test_worker_profile_cannot_inherit_codex_credentials_or_policy(sandbox):
    settings, calls = sandbox
    value = dict(settings.native_completion_profiles['zcode'], codex_auth_file='/private/auth.json')
    with pytest.raises(CompletionPolicyError):
        normalize_completion_profiles({'zcode': value})
    assert not calls


@pytest.mark.parametrize('stdout', ['broken', '{}', '{"schema":"wrong","kind":"result","value":{}}'])
def test_malformed_completion_worker_frames_cannot_establish_success(stdout):
    with pytest.raises(ValueError):
        completion_worker.process_receipt({'stdout': stdout, 'stderr': '', 'returncode': 0, 'reason': None})
