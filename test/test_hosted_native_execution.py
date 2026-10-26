"""Translating hosts share provider, archive, diagnostics and original identity."""
import hashlib
import json
from pathlib import Path
import time

import pytest

from infermatrix_copilot.providers.codex import CodexTransport
from infermatrix_copilot.providers.hosted_execution import execute_native_process
from infermatrix_copilot.providers.registry import transport_for_id
from infermatrix_copilot.sandbox.base import SandboxPending
from infermatrix_copilot.sandbox.inputs import snapshot_directory
from infermatrix_copilot.trace_store import TraceStore
from test_completion_profile_configuration import configured  # noqa: F401
from test_readonly_sandbox import worker_receipt  # noqa: F401


def invoke(settings, root, **changes):
    request = {'system': 'raw native prompt', 'messages': [], 'model': 'fixture', 'effort': 'high',
               'role': 'raw-host', 'readonly': True, 'absolute_deadline': time.monotonic() + 30, **changes}
    return execute_native_process(transport_for_id(settings, 'codex'), root=root,
        identity={'prompt': '', 'profile': settings.native_completion_profiles}, request=request)


def test_raw_host_call_preserves_process_receipt_and_one_original(configured, tmp_path):
    settings, calls = configured
    result = invoke(settings, tmp_path / 'native')
    assert result['native_complete'] and result['output_text'] == '{"ok":true}'
    assert result['usage'] == {'input_tokens': 10, 'output_tokens': 2}
    assert result['accounting_usage']['input_tokens'] == 10
    assert json.loads(calls[0][1]['input'])['input'] == 'raw native prompt'
    assert invoke(settings, tmp_path / 'native') == result and len(calls) == 1
    store = TraceStore(result['trace_root'])
    rows = [row for row, _ in store.iter_records(kind='model_call')]
    assert len(rows) == 1
    store.validate_native_attempt(rows[0])
    assert store.blob(rows[0]['outputs']['native_stdout']) == result['stdout']
    assert rows[0]['result']['native_exit']['stdout_sha256'] == hashlib.sha256(result['stdout'].encode()).hexdigest()


def test_native_output_file_policy_preserves_message_whitespace():
    adapter = CodexTransport(None)
    events = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '  answer\n'}},
              {'type': 'turn.completed'}]
    assert adapter._final_text(events) == 'answer'
    assert adapter.process_receipt({'stdout': '\n'.join(map(json.dumps, events)),
        'stderr': '', 'returncode': 0, 'reason': None})['output_text'] == '  answer\n'


def test_known_failure_keeps_original_native_diagnostics(configured, tmp_path, monkeypatch):
    from infermatrix_copilot.providers.container_execution import ContainerExecution
    from infermatrix_copilot.providers.container_protocol import receipt_line
    from infermatrix_copilot.process_execution import ProcessOutcome
    settings, calls = configured
    stdout = '{"type":"turn.failed","error":{"message":"native refusal"}}\n'
    def fail(self, **kwargs):
        calls.append((self, kwargs))
        envelope = json.loads(kwargs['input'])
        return ProcessOutcome(stdout, receipt_line(envelope,
            {'stdout': hashlib.sha256(stdout.encode()), 'stderr': hashlib.sha256(b'')}, 7, None), 7)
    monkeypatch.setattr(ContainerExecution, 'run', fail)
    root = tmp_path / 'failed'
    result = invoke(settings, root)
    assert not result['native_complete'] and result['returncode'] == 7
    assert result['stdout'] == stdout and result['error']
    assert invoke(settings, root) == result and len(calls) == 1


def test_unknown_exchange_never_creates_a_replacement_attempt(configured, tmp_path, monkeypatch):
    from infermatrix_copilot.providers.container_execution import ContainerExecution
    settings, _ = configured
    monkeypatch.setattr(ContainerExecution, 'run', lambda *a, **k: (_ for _ in ()).throw(
        SandboxPending('original response missing', prior_execution=True)))
    for _ in range(2):
        with pytest.raises(SandboxPending):
            invoke(settings, tmp_path / 'unknown')
    assert len(list((tmp_path / 'unknown/traces/attempts').iterdir())) == 1


def test_raw_result_cache_is_bound_even_when_trace_blobs_redact(configured, tmp_path, monkeypatch):
    from infermatrix_copilot.providers.container_execution import ContainerExecution
    from infermatrix_copilot.providers.container_protocol import receipt_line
    from infermatrix_copilot.process_execution import ProcessOutcome
    settings, calls = configured
    secret = 'fixture-native-private-value-123456'
    monkeypatch.setenv('HOSTED_TEST_SECRET', secret)
    stdout = '\n'.join(map(json.dumps, [
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': secret}},
        {'type': 'turn.completed'}]))
    def result(self, **kwargs):
        calls.append((self, kwargs))
        envelope = json.loads(kwargs['input'])
        return ProcessOutcome(stdout, receipt_line(envelope,
            {'stdout': hashlib.sha256(stdout.encode()), 'stderr': hashlib.sha256(b'')}, 0, None), 0)
    monkeypatch.setattr(ContainerExecution, 'run', result)
    root = tmp_path / 'raw'
    first = invoke(settings, root)
    assert first['output_text'] == secret
    assert secret not in TraceStore(first['trace_root']).blob(next(
        TraceStore(first['trace_root']).iter_records(kind='model_call'))[0]['outputs']['native_stdout'])
    assert invoke(settings, root) == first and len(calls) == 1
    cached = root / 'exchange-result.json'
    value = json.loads(cached.read_text());value['result']['stdout'] = value['result']['stdout'].replace(secret, 'different')
    cached.write_text(json.dumps(value))
    with pytest.raises(SandboxPending):
        invoke(settings, root)
    assert len(calls) == 1


def test_account_settlement_and_observer_errors_are_not_successful_native_receipts(configured, tmp_path):
    from infermatrix_copilot.budgeting import bind_call_budget
    settings, _ = configured
    def failed_settlement(ticket, facts):
        raise RuntimeError('original settlement unavailable')
    with bind_call_budget('fixture', lambda request: 'ticket', failed_settlement):
        with pytest.raises(RuntimeError, match='original settlement unavailable'):
            invoke(settings, tmp_path / 'settlement')
    assert not (tmp_path / 'settlement/exchange-result.json').exists()
    def observer(event):
        raise RuntimeError('original event consumer unavailable')
    with pytest.raises(RuntimeError, match='original event consumer unavailable'):
        execute_native_process(transport_for_id(settings, 'codex'), root=tmp_path / 'observer',
            identity={'prompt': ''}, request={'system': 'policy', 'messages': [], 'model': 'fixture',
                'readonly': True}, native_observer=observer)
    assert not (tmp_path / 'observer/exchange-result.json').exists()
