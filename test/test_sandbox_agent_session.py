"""Agent entry -> canonical native call -> sandbox -> original typed outcome."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from infermatrix_copilot.providers import run_harness_session, run_harness_step
from infermatrix_copilot.providers.base import AgentSessionRequest
from infermatrix_copilot.providers.codex import CodexTransport
from infermatrix_copilot.providers.container_execution import ContainerExecution
from infermatrix_copilot.providers.container_protocol import receipt_line
from infermatrix_copilot.providers.registry import transport_for_id
from infermatrix_copilot.process_execution import ProcessOutcome
from infermatrix_copilot.sandbox.base import SandboxPending
from infermatrix_copilot.scopes import ToolScope, READ_TOOLS
from infermatrix_copilot.tool_bridge import write_bridge_spec, load_bridge_spec
from infermatrix_copilot.trace_store import TraceStore
from test_completion_profile_configuration import configured  # noqa: F401


@pytest.fixture
def session(configured, tmp_path, monkeypatch):
    settings, calls = configured
    source, run, knowledge = [tmp_path / name for name in ('source', 'run', 'knowledge')]
    for path in (source, run, knowledge / 'general'):
        path.mkdir(parents=True)
    (source / 'file.bin').write_bytes(b'\0\xffsource')
    (run / 'evidence.json').write_text('{"frozen":true}')
    (knowledge / 'general/rules.md').write_text('read-only knowledge')
    settings = settings.model_copy(update={'native_session_profiles': settings.native_completion_profiles,
                                           'knowledge_dir': knowledge})
    scope = ToolScope('review', READ_TOOLS, read_only=True, root=str(source))
    bridge = write_bridge_spec(run_dir=run, step_name='review', scope=scope, repo='demo')
    request = AgentSessionRequest(system='policy', prompt='inspect ' + str(source), model='fixture',
        scope=scope, run_dir=run, step_name='review', bridge_spec_path=bridge, max_iters=4, timeout_s=30,
        idle_timeout_s=2)
    original = ContainerExecution.run
    def receipt(self, **kwargs):
        result = original(self, **kwargs)
        envelope = json.loads(kwargs['input'])
        return ProcessOutcome(result.stdout, result.stderr + receipt_line(envelope,
            {'stdout': hashlib.sha256(result.stdout.encode()), 'stderr': hashlib.sha256(result.stderr.encode())},
            result.returncode, result.reason), result.returncode, result.reason)
    monkeypatch.setattr(ContainerExecution, 'run', receipt)
    monkeypatch.setattr(CodexTransport, 'bridge_probe', lambda self, profile, request:
        ({'schema': 'bridge-readiness/1', 'tools': sorted(request.scope.allowed_tools)}, profile.sandbox._prepared_context()))
    return settings, calls, request


def test_native_session_is_scoped_contained_and_durable(session):
    settings, calls, request = session
    transport = transport_for_id(settings, 'codex')
    outcome = run_harness_session(transport, request)
    assert outcome.text == '{"ok":true}' and outcome.usage['input_tokens'] == 10
    assert len(calls) == 1
    envelope = json.loads(calls[0][1]['input'])
    assert envelope['version'] == 7
    assert envelope['controls'] == {'idle_timeout_s': 2, 'termination_grace_s': 5}
    argv = envelope['argv']
    assert argv[argv.index('-C') + 1] == '/tmp/inputs/repository'
    assert any('infermatrix_copilot.tool_bridge' in arg for arg in argv)
    assert any('safe.directory' in arg and '/tmp/inputs/repository' in arg for arg in argv)
    assert not any('PYTHONPATH' in arg or str(request.run_dir) in arg for arg in argv)
    inputs = envelope['frozen_inputs']['files']
    bridge = json.loads(inputs['/tmp/inputs/bridge.json'])
    assert bridge['scope']['root'] == '/tmp/inputs/repository'
    assert bridge['run_dir'] == '/tmp/work/session' and bridge['input_run_dir'] == '/tmp/inputs/run'
    assert bridge['knowledge_dir'] == '/tmp/inputs/knowledge'
    assert '/tmp/inputs/repository/file.bin' in inputs
    assert '/tmp/inputs/run/evidence.json' in inputs
    root = next((request.run_dir / 'native-sessions').iterdir())
    store = TraceStore(root / 'traces')
    records = [record for record, _ in store.iter_records(kind='model_call')]
    assert len(records) == 1
    store.validate_native_attempt(records[0])
    assert records[0]['result']['session_outcome']['usage'] == outcome.usage
    assert run_harness_session(transport, request) == outcome
    assert len(calls) == 1
    assert not hasattr(transport, 'session_profile')  # no mutable cached profile
    with pytest.raises(SandboxPending, match='differs'):
        run_harness_session(transport, replace(request, prompt='different intent'))
    assert len(calls) == 1


def test_unknown_original_session_never_creates_another_native_attempt(session, monkeypatch):
    settings, calls, request = session
    monkeypatch.setattr(ContainerExecution, 'run', lambda *a, **k: (_ for _ in ()).throw(
        SandboxPending('original worker unavailable', prior_execution=True)))
    transport = transport_for_id(settings, 'codex')
    for _ in range(2):
        with pytest.raises(SandboxPending):
            run_harness_session(transport, request)
    root = next((request.run_dir / 'native-sessions').iterdir())
    assert len(list((root / 'traces/attempts').iterdir())) == 1


@pytest.mark.parametrize('damage', ['write', 'bridge', 'outside', 'link', 'runner'])
def test_unsupported_session_scope_refuses_before_native_dispatch(session, damage):
    settings, calls, request = session
    transport = transport_for_id(settings, 'codex')
    if damage == 'write':
        request = replace(request, scope=replace(request.scope, read_only=False))
    elif damage == 'bridge':
        data = json.loads(request.bridge_spec_path.read_text());data['scope']['root'] = '/outside'
        request.bridge_spec_path.write_text(json.dumps(data))
    elif damage == 'outside':
        request = replace(request, scope=replace(request.scope, read_roots=('/outside',)))
    elif damage == 'link':
        (Path(request.scope.root) / 'outside').symlink_to('/etc/passwd')
    else:
        transport.runner = lambda *a, **k: pytest.fail('host escape')
    with pytest.raises(ValueError):
        run_harness_session(transport, request)
    assert calls == []


def test_shared_snapshot_materializes_linked_git_worktree(session, tmp_path):
    from infermatrix_copilot.providers.session_inputs import freeze_session
    from infermatrix_copilot.sandbox.inputs import stage_inputs
    settings, _, request = session
    source = Path(request.scope.root)
    def git(*args, cwd=source):
        return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()
    git('init', '-q');git('config', 'user.name', 'fixture');git('config', 'user.email', 'fixture@example.test')
    git('add', '.');git('commit', '-qm', 'frozen')
    head = git('rev-parse', 'HEAD')
    worktree = tmp_path / 'linked';git('worktree', 'add', '--detach', str(worktree), head)
    request = replace(request, scope=replace(request.scope, root=str(worktree)), bridge_spec_path=None)
    frozen = freeze_session(request, knowledge_dir=settings.knowledge_dir)
    staging = tmp_path / 'staging'
    stage_inputs(staging, frozen.frozen_inputs)
    # The projection is complete without the host common checkout or its config.
    repository = staging / 'input-files/repository'
    assert git('rev-parse', 'HEAD', cwd=repository) == head
    assert subprocess.check_output(['git', 'show', 'HEAD:file.bin'], cwd=repository) == b'\0\xffsource'
    assert 'user' not in (repository / '.git/config').read_text()


def test_agent_step_entry_uses_the_session_workflow_and_preserves_host_bridge(session):
    from types import SimpleNamespace
    settings, calls, request = session
    context = SimpleNamespace(settings=settings, state={'task_spec': {'repo': 'demo'}},
                              run_dir=request.run_dir, trace=None)
    result = run_harness_step(context, SimpleNamespace(model=request.model), step_name=request.step_name,
        system=request.system, prompt=request.prompt, scope=request.scope, max_iters=request.max_iters)
    assert result.text == '{"ok":true}' and len(calls) == 1
    restored, data = load_bridge_spec(request.bridge_spec_path)
    assert restored == request.scope and data['run_dir'] == str(request.run_dir)


def test_finished_agent_receipt_recovers_after_result_write_failure(session, monkeypatch):
    from infermatrix_copilot.providers import original_execution
    settings, calls, request = session
    transport = transport_for_id(settings, 'codex')
    original = original_execution.immutable_write_bytes
    def fail_result(path, *args, **kwargs):
        if path.name == 'session-result.json':
            raise OSError('result publication interrupted')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(original_execution, 'immutable_write_bytes', fail_result)
        with pytest.raises(OSError, match='publication interrupted'):
            run_harness_session(transport, request)
    assert run_harness_session(transport, request).text == '{"ok":true}'
    assert len(calls) == 1


def test_denied_source_paths_are_absent_from_all_model_inputs(session):
    from infermatrix_copilot.providers.session_inputs import freeze_session
    settings, _, request = session
    hidden = Path(request.scope.root) / 'hidden';hidden.mkdir();(hidden / 'secret').write_text('hidden')
    request = replace(request, bridge_spec_path=None, scope=replace(request.scope,
        deny_prefixes=(str(hidden),), read_roots=(request.scope.root,)))
    frozen = freeze_session(request, knowledge_dir=settings.knowledge_dir)
    assert not any('/hidden/' in name for name in frozen.frozen_inputs['files'])
    assert frozen.scope.deny_prefixes == ('/tmp/inputs/repository/hidden',)


def test_missing_bridge_capability_refuses_before_the_model(session, monkeypatch):
    from infermatrix_copilot.providers.completion_profiles import CompletionPolicyError
    settings, calls, request = session
    def refuse(*args):
        raise ValueError('MCP dependencies unavailable')
    monkeypatch.setattr(CodexTransport, 'bridge_probe', refuse)
    with pytest.raises(CompletionPolicyError) as caught:
        run_harness_session(transport_for_id(settings, 'codex'), request)
    assert not caught.value.native_call_sent and calls == []


def test_status_polling_does_not_change_immutable_session_inputs(session, monkeypatch):
    from infermatrix_copilot import run_status
    from infermatrix_copilot.providers.session_inputs import freeze_session, RUN
    settings, _, request = session
    run = request.run_dir
    run_status.status_path(run).write_text(json.dumps({'state': run_status.DONE}))
    run_status.reconcile_if_dead(run, run.parent)
    read = os.read
    def read_while_polling(fd, size):
        data = read(fd, size)
        # The real reader opens/truncates its lock even for a terminal status.
        run_status.reconcile_if_dead(run, run.parent)
        return data
    monkeypatch.setattr(os, 'read', read_while_polling)
    frozen = freeze_session(request, knowledge_dir=settings.knowledge_dir)
    assert RUN + '/evidence.json' in frozen.frozen_inputs['files']
    assert all(RUN + '/' + name not in frozen.frozen_inputs['files']
               for name in run_status.mutable_status_paths())


def test_session_snapshot_still_refuses_changes_to_real_evidence(session, monkeypatch):
    from infermatrix_copilot.providers.session_inputs import freeze_session
    settings, _, request = session
    evidence = request.run_dir / 'evidence.json'
    read = os.read
    inode = evidence.stat().st_ino
    def read_then_change(fd, size):
        data = read(fd, size)
        if os.fstat(fd).st_ino == inode and data:
            evidence.write_text('{"frozen":false,"changed":true}')
        return data
    monkeypatch.setattr(os, 'read', read_then_change)
    with pytest.raises(ValueError, match='source changed during snapshot'):
        freeze_session(request, knowledge_dir=settings.knowledge_dir)


def test_structured_scoped_session_keeps_bridge_schema_and_original_receipt(session, tmp_path):
    from infermatrix_copilot.providers.json_session import run_readonly_json
    settings, calls, request = session
    options = dict(cwd=Path(request.scope.root), command=['/must-not-run-host-cli'], provider='codex',
        model='fixture', output_schema={'type': 'object', 'required': ['ok'],
            'properties': {'ok': {'type': 'boolean'}}}, idle_timeout_s=2, absolute_timeout_s=30,
        execution_config=settings.native_session_profiles['codex'], execution_root=tmp_path/'structured',
        settings=settings, session_request=request)
    result = run_readonly_json(request.prompt, **options)
    assert result['payload'] == {'ok': True}
    assert len(calls) == 1
    envelope = json.loads(calls[0][1]['input'])
    assert '--output-schema' in envelope['argv']
    assert any('infermatrix-tools' in value for value in envelope['argv'])
    bridge = json.loads(envelope['frozen_inputs']['files']['/tmp/inputs/bridge.json'])
    assert bridge['scope']['read_only'] and bridge['knowledge_dir'] == '/tmp/inputs/knowledge'
    assert '/tmp/inputs/knowledge/general/rules.md' in envelope['frozen_inputs']['files']
    assert run_readonly_json(request.prompt, **options) == result
    assert len(calls) == 1
    changed = {**options, 'output_schema': {'type': 'object', 'required': ['different']}}
    with pytest.raises(SandboxPending, match='differs'):
        run_readonly_json(request.prompt, **changed)
    assert len(calls) == 1


@pytest.mark.parametrize('provider', ['cursor', 'claude-code', 'deepseek'])
def test_structured_agent_session_requires_explicit_provider_capability(session, provider):
    settings, calls, request = session
    transport = transport_for_id(settings, provider)
    with pytest.raises(ValueError, match='structured agent sessions'):
        transport.validate_session(replace(request, bridge_spec_path=None, output_schema={'type': 'object'}))
    assert not calls


def test_scoped_structured_unknown_reuses_original_attempt_without_dispatch(session, tmp_path, monkeypatch):
    from infermatrix_copilot.providers.json_session import run_readonly_json
    settings, _, request = session
    dispatched = []
    def missing(*args, **kwargs):
        dispatched.append(1)
        raise SandboxPending('original structured worker response lost', prior_execution=True)
    monkeypatch.setattr(ContainerExecution, 'run', missing)
    for _ in range(2):
        with pytest.raises(SandboxPending):
            run_readonly_json(request.prompt, cwd=Path(request.scope.root), command=['/must-not-run'],
                provider='codex', model='fixture', output_schema={'type': 'object'},
                idle_timeout_s=2, absolute_timeout_s=30,
                execution_config=settings.native_session_profiles['codex'],
                execution_root=tmp_path/'structured', settings=settings, session_request=request)
    assert dispatched == [1]
    assert len(list((tmp_path/'structured/traces/attempts').iterdir())) == 1


@pytest.mark.parametrize('damage', ['cwd', 'schema', 'write', 'host-mode'])
def test_scoped_structured_binding_refuses_before_model(session, tmp_path, damage):
    from infermatrix_copilot.providers.json_session import run_readonly_json
    settings, calls, request = session
    options = dict(cwd=Path(request.scope.root), command=['/must-not-run'],
        provider='codex', model='fixture', output_schema={'type': 'object'},
        idle_timeout_s=2, absolute_timeout_s=30, execution_config=settings.native_session_profiles['codex'],
        execution_root=tmp_path/'structured', settings=settings, session_request=request)
    if damage == 'cwd': options['cwd'] = tmp_path
    elif damage == 'schema': options['session_request'] = replace(request, output_schema={'type': 'string'})
    elif damage == 'write': options['session_request'] = replace(request, scope=replace(request.scope, read_only=False))
    else: options['execution_config'] = None
    with pytest.raises(ValueError):
        run_readonly_json(request.prompt, **options)
    assert not calls and not (tmp_path/'structured').exists()


def test_scoped_structured_timeout_cannot_accept_valid_json(session, tmp_path, monkeypatch):
    from infermatrix_copilot.providers.json_session import run_readonly_json, JSONSessionError
    from infermatrix_copilot.agent_loop import AgentOutcome
    settings, _, request = session
    calls = []
    def timed_out(self, value):
        calls.append(1)
        return AgentOutcome(text='{"ok":true}', iterations=1, tool_calls=0, truncated=True)
    monkeypatch.setattr(CodexTransport, 'run_session', timed_out)
    options = dict(cwd=Path(request.scope.root), command=['/must-not-run'],
        provider='codex', model='fixture', output_schema={'type': 'object'},
        idle_timeout_s=2, absolute_timeout_s=30, execution_config=settings.native_session_profiles['codex'],
        execution_root=tmp_path/'structured', settings=settings, session_request=request)
    with pytest.raises(JSONSessionError) as failure:
        run_readonly_json(request.prompt, **options)
    assert failure.value.timeout
    with pytest.raises((JSONSessionError, SandboxPending)):
        run_readonly_json(request.prompt, **options)
    assert calls == [1]

