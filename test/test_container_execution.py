import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from infermatrix_copilot.providers import container_execution as containers
from infermatrix_copilot.providers.process import ProcessOutcome

IMAGE = 'fixture@sha256:' + 'a' * 64


class Daemon:
    def __init__(self):
        self.id = 'daemon-one'
        self.calls = []
        self.container = None
        self.fail_create_reply = False
        self.fail_start_reply = False
        self.disconnect = False

    def control(self, *args):
        self.calls.append(args[0])
        if args[:2] == ('image', 'inspect'):
            return json.dumps({'io.infermatrix.sandbox.inputs': '6'})
        if args[0] == 'info':
            return self.id
        if args[0] == 'create':
            name = args[args.index('--name') + 1]
            self.container = {'Id': 'c' * 64, 'Name': '/' + name,
                'Config': {'Labels': {'infermatrix.execution': name}},
                'State': {'Running': False, 'Status': 'created'}}
            if self.fail_create_reply:
                raise containers.ContainerPending('lost create response')
            return self.container['Id']
        if self.container is None:
            raise containers.ContainerPending('container absent')
        if args[0] == 'inspect':
            return json.dumps([self.container])
        if args[0] == 'kill':
            self.container['State'] = {'Running': False, 'Status': 'exited', 'ExitCode': 137}
            return ''
        if args[0] == 'rm':
            self.container = None
            return ''
        raise AssertionError(args)

    def start(self, command, *, cleanup, **kwargs):
        assert command[-4:-1] == ['start', '--attach', '--interactive']
        self.calls.append('start')
        self.container['State'] = {'Running': True, 'Status': 'running'}
        if self.fail_start_reply:
            raise OSError('lost attach response')
        if not self.disconnect:
            self.container['State'] = {'Running': False, 'Status': 'exited', 'ExitCode': 0}
        cleanup()
        return ProcessOutcome('native receipt', '', 0)


@pytest.fixture
def execution(tmp_path, monkeypatch):
    daemon = Daemon()
    runtime = containers.ContainerExecution(tmp_path, docker='/docker', socket='/daemon.sock')
    monkeypatch.setattr(runtime, '_control', daemon.control)
    monkeypatch.setattr(containers, 'run_process', daemon.start)
    return runtime, daemon


def run(runtime):
    return runtime.run(image=IMAGE, argv=['model'], input='frozen evidence',
                       deadline=time.monotonic() + 10)


@pytest.mark.parametrize('change', [None, 'prompt', 'model', 'image', 'missing_result'])
def test_completion_recovery_uses_original_receipt_without_dispatch_or_accounting(execution, monkeypatch, change):
    from dataclasses import replace
    from infermatrix_copilot.budgeting import bind_call_budget
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.providers.completion import complete_native
    from infermatrix_copilot.providers.registry import transport_for_id

    runtime, daemon = execution
    events = [
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"ok":true}'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2}},
    ]

    def native_start(*args, **kwargs):
        request_envelope = json.loads(kwargs['input'])
        assert 'features.apps=false' in request_envelope['argv']
        daemon.start(*args, **kwargs)
        return ProcessOutcome('\n'.join(map(json.dumps, events)), '', 0)

    monkeypatch.setattr(containers, 'run_process', native_start)
    profile = containers.ContainerCompletionProfile(runtime, IMAGE)
    settings = Settings(_env_file=None)
    request = {'system': 'policy', 'messages': [{'role': 'user', 'content': 'evidence'}], 'model': 'pinned'}
    transport = transport_for_id(settings, 'codex', completion_profile=profile)
    original, _, _, _ = complete_native(lambda: transport, request=request)
    runtime.remove()
    before = list(daemon.calls)

    if change == 'prompt':
        request['messages'] = [{'role': 'user', 'content': 'changed evidence'}]
    elif change == 'model':
        request['model'] = 'different'
    elif change == 'image':
        profile = replace(profile, image='fixture@sha256:' + 'b' * 64)
    elif change == 'missing_result':
        (runtime.root / 'result.json').unlink()

    def forbidden(*args, **kwargs):
        pytest.fail('recovery must not dispatch, contact Docker, or reserve/settle accounting')

    monkeypatch.setattr(runtime, '_control', forbidden)
    monkeypatch.setattr(runtime, 'run', forbidden)
    recovered = transport_for_id(settings, 'codex', completion_profile=profile)
    with bind_call_budget('test', forbidden, forbidden):
        if change is None:
            reply = recovered.get_completion_result(**request)
            assert reply.text == original.text and reply.usage == original.usage
        else:
            with pytest.raises(containers.ContainerPending):
                recovered.get_completion_result(**request)
    assert daemon.calls == before and daemon.calls.count('start') == 1


@pytest.mark.parametrize('damage', ['stdout', 'stderr', 'receipt'])
def test_damaged_live_receipt_retains_original_container(execution, monkeypatch, damage):
    import hashlib
    from infermatrix_copilot.providers.container_protocol import receipt_line

    runtime, daemon = execution

    def process(command, **kwargs):
        daemon.start(command, **kwargs)
        digests = {'stdout': hashlib.sha256(b'reply'), 'stderr': hashlib.sha256(b'warning')}
        receipt = receipt_line(json.loads(kwargs['input']), digests, 0, None)
        return ProcessOutcome('reply', 'warning' + receipt, 0)

    monkeypatch.setattr(containers, 'run_process', process)
    runtime.run_command(['/native'], image=IMAGE, cwd='/tmp/work', env={},
                        deadline=time.monotonic() + 10, input='frozen evidence')
    path = runtime.root / 'result.json'
    saved = json.loads(path.read_text())
    if damage == 'receipt':
        saved['outcome']['stderr'] = saved['outcome']['stderr'].replace('request_sha256', 'wrong_key')
    else:
        saved['outcome'][damage] = 'changed' + saved['outcome'][damage]
    path.write_text(json.dumps(saved))
    before = list(daemon.calls)
    for operation in (runtime.get_result, runtime.recover_result, runtime.remove):
        with pytest.raises(containers.ContainerPending):
            operation()
    assert daemon.calls == before
    assert daemon.container is not None
    assert not (runtime.root / 'recovered-result.json').exists()


def test_completed_and_removed_execution_cannot_dispatch_again(execution):
    runtime, daemon = execution
    result = run(runtime)
    assert runtime.get_result() == result
    runtime.remove()
    assert daemon.container is None
    with pytest.raises(containers.ContainerPending, match='already reserved'):
        run(runtime)
    assert daemon.calls.count('create') == daemon.calls.count('start') == 1
    assert runtime.get_result() == result


@pytest.mark.parametrize('legacy', [False, True])
def test_command_recovery_retains_original_deadline_and_legacy_envelope(execution, legacy):
    runtime, _ = execution
    cmd = ['/bin/true']
    args = dict(image=IMAGE, cwd='/tmp/work', env={}, input=None)
    if legacy:
        argv, payload = runtime._command_envelope(cmd, cwd='/tmp/work', env={}, input=None)
        result = runtime.run(image=IMAGE, argv=argv, input=payload,
                             deadline=time.monotonic() + 10)
    else:
        result = runtime.run_command(cmd, deadline=time.monotonic() + 10, **args)
    assert runtime.get_command_result(cmd, **args) == result
    if not legacy:
        binding = json.loads((runtime.root / 'binding.json').read_text())
        binding['worker_deadline'] += 1
        (runtime.root / 'binding.json').write_text(json.dumps(binding))
        with pytest.raises(containers.ContainerPending, match='differs'):
            runtime.get_command_result(cmd, **args)


@pytest.mark.parametrize('damage', [None, 'stdout', 'stderr', 'request', 'exit', 'missing', 'running'])
def test_lost_result_recovery_requires_complete_original_worker_receipt(execution, monkeypatch, damage):
    import hashlib
    from infermatrix_copilot.providers.container_protocol import receipt_line

    runtime, daemon = execution
    cmd = ['/bin/true']
    args = dict(image=IMAGE, cwd='/tmp/work', env={}, input='evidence')
    deadline = time.monotonic() + 10
    runtime.run_command(cmd, deadline=deadline, **args)
    original = (runtime.root / 'binding.json').read_bytes()
    (runtime.root / 'result.json').unlink()
    _, payload = runtime._command_envelope(cmd, cwd=args['cwd'], env={}, input='evidence',
                                           deadline=deadline, protocol=3)
    request = json.loads(payload)
    if damage == 'request':
        request['input'] = 'changed'
    stdout, stderr = 'complete output', 'native diagnostics'
    digests = {name: hashlib.sha256(value.encode())
               for name, value in [('stdout', stdout), ('stderr', stderr)]}
    receipt = receipt_line(request, digests, 9 if damage == 'exit' else 0, None)
    if damage == 'stdout':
        stdout = stdout[1:]
    if damage == 'stderr':
        stderr = stderr[1:]
    if damage == 'missing':
        receipt = ''
    if damage == 'running':
        daemon.container['State'] = {'Running': True, 'Status': 'running'}
    logs = []

    def read_logs(command, **kwargs):
        assert command[-2:] == ['logs', 'c' * 64]
        logs.append(command)
        return ProcessOutcome(stdout, stderr + receipt, 0)

    monkeypatch.setattr(containers, 'run_process', read_logs)
    if damage is None:
        expected = ProcessOutcome(stdout, stderr, 0)
        assert runtime.get_original_command_result(image=IMAGE, recover=True) == expected
        assert runtime.get_command_result(cmd, **args) == expected
        assert runtime.get_original_command_result(image=IMAGE) == expected
        runtime.recover_result()
        assert len(logs) == 1
        runtime.remove()
        assert runtime.get_command_result(cmd, **args) == expected
    else:
        with pytest.raises(containers.ContainerPending):
            runtime.get_original_command_result(image=IMAGE, recover=True)
        assert not (runtime.root / 'recovered-result.json').exists()
    assert (runtime.root / 'binding.json').read_bytes() == original
    assert daemon.calls.count('start') == 1


@pytest.mark.parametrize('change', [None, 'image', 'control', 'missing', 'corrupt'])
def test_original_profile_result_needs_no_caller_payload(execution, monkeypatch, change):
    from dataclasses import replace
    runtime, daemon = execution
    profile = containers.ContainerCompletionProfile(runtime, IMAGE)
    result = runtime.run_command(['/bin/true'], image=IMAGE, cwd='/tmp/work', env={},
                                 input='original evidence', deadline=time.monotonic() + 10)
    if change == 'image':
        profile = replace(profile, image='fixture@sha256:' + 'b' * 64)
    elif change == 'control':
        runtime.command = ['/different-docker', '-H', 'unix:///daemon.sock']
    elif change == 'missing':
        (runtime.root / 'result.json').unlink()
    elif change == 'corrupt':
        (runtime.root / 'result.json').write_text('broken')
    before = list(daemon.calls)
    monkeypatch.setattr(runtime, '_command_envelope', lambda *a, **k: pytest.fail('rebuilt caller input'))
    monkeypatch.setattr(containers, 'run_process', lambda *a, **k: pytest.fail('dispatched a process'))
    if change is None:
        assert profile.read_result() == result
    else:
        with pytest.raises(containers.ContainerPending):
            profile.read_result()
    assert daemon.calls == before


def test_recovery_cannot_replace_corrupt_saved_result(execution, monkeypatch):
    runtime, _ = execution
    runtime.run_command(['/bin/true'], image=IMAGE, cwd='/tmp/work', env={},
                        deadline=time.monotonic() + 10)
    (runtime.root / 'result.json').write_text('{broken')
    monkeypatch.setattr(containers, 'run_process', lambda *a, **k: pytest.fail('read logs'))
    with pytest.raises(containers.ContainerPending):
        runtime.recover_result()
    assert (runtime.root / 'result.json').read_text() == '{broken'


def test_old_worker_without_receipt_protocol_stays_pending(execution):
    runtime, daemon = execution
    run(runtime)
    (runtime.root / 'result.json').unlink()
    with pytest.raises(containers.ContainerPending, match='no recoverable'):
        runtime.recover_result()
    assert daemon.calls.count('start') == 1


@pytest.mark.parametrize('change', [None, 'request', 'saved_receipt'])
def test_provider_recovers_original_reply_without_dispatch_or_settlement(execution, monkeypatch, change):
    import hashlib
    from infermatrix_copilot.budgeting import bind_call_budget
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.providers.container_protocol import receipt_line
    from infermatrix_copilot.providers.registry import transport_for_id

    runtime, daemon = execution
    events = [
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"ok":true}'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2}},
    ]
    logs, reads = [], []

    def process(command, **kwargs):
        if command[-2:] == ['logs', 'c' * 64]:
            reads.append(command)
            return logs[0]
        daemon.start(command, **kwargs)
        stdout = '\n'.join(map(json.dumps, events))
        digests = {'stdout': hashlib.sha256(stdout.encode()), 'stderr': hashlib.sha256(b'')}
        receipt = receipt_line(json.loads(kwargs['input']), digests, 0, None)
        result = ProcessOutcome(stdout, receipt, 0)
        logs.append(result)
        return result

    monkeypatch.setattr(containers, 'run_process', process)
    profile = containers.ContainerCompletionProfile(runtime, IMAGE)
    transport = transport_for_id(Settings(_env_file=None), 'codex', completion_profile=profile)
    request = {'system': 'policy', 'messages': [{'role': 'user', 'content': 'frozen'}], 'model': 'pinned'}
    original = transport.complete(**request)
    (runtime.root / 'result.json').unlink()

    def forbidden(*args, **kwargs):
        pytest.fail('recovery dispatched or settled accounting')

    monkeypatch.setattr(runtime, 'run', forbidden)
    with bind_call_budget('recovery', forbidden, forbidden):
        with pytest.raises(containers.ContainerPending):
            transport.get_completion_result(**request)
        assert not reads
        if change == 'request':
            request['messages'] = [{'role': 'user', 'content': 'changed'}]
            with pytest.raises(containers.ContainerPending, match='differs'):
                transport.get_completion_result(**request, recover=True)
            assert not reads
        else:
            reply = transport.get_completion_result(**request, recover=True)
            assert reply.text == original.text and reply.usage == original.usage
            assert reply.usage['input_tokens'] == 10
            if change == 'saved_receipt':
                path = runtime.root / 'recovered-result.json'
                saved = json.loads(path.read_text())
                saved['outcome']['stderr'] = ''
                path.write_text(json.dumps(saved))
                with pytest.raises(containers.ContainerPending):
                    transport.get_completion_result(**request, recover=True)
                assert len(reads) == 1
            else:
                monkeypatch.setattr(runtime, '_control', forbidden)
                assert transport.get_completion_result(**request, recover=True).text == reply.text
                assert len(reads) == 1
    assert daemon.calls.count('start') == 1


def test_local_content_addressed_image_is_accepted(execution):
    runtime, daemon = execution
    result = runtime.run(image='sha256:' + 'b' * 64, argv=['model'],
                         deadline=time.monotonic() + 10)
    assert result.returncode == 0
    assert daemon.calls.count('start') == 1


def test_explicit_command_overrides_image_entrypoint(execution, monkeypatch):
    runtime, daemon = execution
    create = []

    def control(*args):
        if args[0] == 'create':
            create.append(args)
        return daemon.control(*args)

    monkeypatch.setattr(runtime, '_control', control)
    argv = ['/usr/local/bin/python', '-c', 'print("chosen command")']
    runtime.run(image=IMAGE, argv=argv, deadline=time.monotonic() + 10)
    command, = create
    # Docker otherwise prepends the image's ENTRYPOINT to these arguments.
    assert command[command.index('--entrypoint') + 1] == argv[0]
    assert command[command.index(IMAGE) + 1:] == tuple(argv[1:])
    assert json.loads((runtime.root / 'binding.json').read_text())['argv'] == argv


@pytest.mark.parametrize('failure', ['create', 'start', 'result'])
def test_unknown_outcomes_retain_identity_without_replacement(execution, monkeypatch, failure):
    runtime, daemon = execution
    if failure == 'create':
        daemon.fail_create_reply = True
    elif failure == 'start':
        daemon.fail_start_reply = True
    else:
        def broken(*args, **kwargs):
            raise OSError('disk unavailable')
        monkeypatch.setattr(containers, 'atomic_write_bytes', broken)
    with pytest.raises((containers.ContainerPending, OSError)):
        run(runtime)
    original = (runtime.root / 'binding.json').read_bytes()
    with pytest.raises(containers.ContainerPending):
        run(runtime)
    assert (runtime.root / 'binding.json').read_bytes() == original
    assert daemon.calls.count('create') == 1
    assert daemon.calls.count('start') == (0 if failure == 'create' else 1)
    assert runtime.reconcile()['state']['Running'] is False
    with pytest.raises(containers.ContainerPending):
        runtime.remove()  # no durable result means keep reconciliation evidence


@pytest.mark.parametrize('contents', ['{broken', '{}', 'null'])
def test_corrupt_binding_never_dispatches(execution, contents):
    runtime, daemon = execution
    (runtime.root / 'binding.json').write_text(contents)
    with pytest.raises(containers.ContainerPending):
        run(runtime)
    assert daemon.calls == []


def test_changed_daemon_cannot_stop_or_remove_original(execution):
    runtime, daemon = execution
    run(runtime)
    daemon.id = 'replacement-daemon'
    with pytest.raises(containers.ContainerPending, match='original container daemon'):
        runtime.remove()
    assert daemon.container is not None
    assert 'rm' not in daemon.calls


def test_successful_client_exit_cannot_hide_a_disconnected_live_container(execution):
    runtime, daemon = execution
    daemon.disconnect = True
    with pytest.raises(containers.ContainerPending, match='does not establish'):
        run(runtime)
    assert not daemon.container['State']['Running']
    assert not (runtime.root / 'result.json').exists()
    with pytest.raises(containers.ContainerPending):
        run(runtime)
    assert daemon.calls.count('start') == 1


def test_missing_container_does_not_release_original_binding(execution):
    runtime, daemon = execution
    run(runtime)
    daemon.container = None
    with pytest.raises(containers.ContainerPending):
        runtime.reconcile()
    with pytest.raises(containers.ContainerPending):
        run(runtime)
    assert daemon.calls.count('start') == 1


def test_concurrent_attempts_publish_one_binding(execution):
    runtime, daemon = execution
    def invoke(_):
        try:
            return run(runtime)
        except containers.ContainerPending:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(invoke, range(8)))
    assert sum(result is not None for result in outcomes) == 1
    assert daemon.calls.count('create') == daemon.calls.count('start') == 1


def test_expired_request_and_unpinned_image_cannot_reserve(execution):
    runtime, daemon = execution
    with pytest.raises(ValueError, match='digest'):
        runtime.run(image='fixture:latest', argv=['model'], deadline=time.monotonic() + 5)
    with pytest.raises(ValueError, match='cancelled before reservation'):
        runtime.run(image=IMAGE, argv=['model'], deadline=time.monotonic() - 1)
    assert daemon.calls == []
    assert not (runtime.root / 'binding.json').exists()


def test_cancellation_during_creation_cannot_start_model(execution, monkeypatch):
    import threading
    runtime, daemon = execution
    cancel = threading.Event()
    def control(*args):
        result = daemon.control(*args)
        if args[0] == 'create':
            cancel.set()
        return result
    monkeypatch.setattr(runtime, '_control', control)
    with pytest.raises(containers.ContainerPending, match='before container start'):
        runtime.run(image=IMAGE, argv=['model'], deadline=time.monotonic() + 10,
                    cancel=cancel)
    assert 'start' not in daemon.calls
    assert runtime.reconcile()['state']['Status'] == 'created'
    with pytest.raises(containers.ContainerPending):
        run(runtime)


@pytest.mark.parametrize('operation', ['reconcile', 'remove'])
def test_same_name_replacement_cannot_impersonate_original(execution, operation):
    runtime, daemon = execution
    run(runtime)
    daemon.container['Id'] = 'd' * 64
    with pytest.raises(containers.ContainerPending):
        getattr(runtime, operation)()
    assert 'kill' not in daemon.calls and 'rm' not in daemon.calls


def test_missing_native_identity_cannot_authorize_cleanup(execution):
    runtime, daemon = execution
    run(runtime)
    (runtime.root / 'container.json').unlink(missing_ok=True)
    daemon.container['State'] = {'Running': True, 'Status': 'running'}
    with pytest.raises(containers.ContainerPending):
        runtime.remove()
    assert 'kill' not in daemon.calls and 'rm' not in daemon.calls


def test_mutations_use_native_id_after_the_name_is_reused(execution, monkeypatch):
    runtime, daemon = execution
    targets = []
    control = daemon.control
    def capture(*args):
        if args[0] in ('inspect', 'kill', 'rm'):
            targets.append((args[0], args[1]))
        return control(*args)
    monkeypatch.setattr(runtime, '_control', capture)
    run(runtime)
    runtime.remove()
    assert targets and all(target == 'c' * 64 for _, target in targets)


def test_model_proxy_cannot_mount_docker_socket(tmp_path):
    with pytest.raises(ValueError, match='dedicated Unix socket'):
        containers.ContainerExecution(tmp_path, docker='/docker', socket='/daemon.sock',
                                      model_proxy_socket='/daemon.sock')


def test_proxy_must_exist_before_container_is_created(execution):
    runtime, daemon = execution
    runtime.model_proxy_socket = str(runtime.root / 'missing.sock')
    with pytest.raises(containers.ContainerPending, match='socket is unavailable'):
        run(runtime)
    assert 'create' not in daemon.calls and 'start' not in daemon.calls


def test_proxy_replacement_during_creation_cannot_dispatch(execution, monkeypatch):
    import socket
    runtime, daemon = execution
    path = runtime.root / 'proxy.sock'
    runtime.model_proxy_socket = str(path)
    with socket.socket(socket.AF_UNIX) as original, socket.socket(socket.AF_UNIX) as replacement:
        original.bind(str(path))
        def control(*args):
            result = daemon.control(*args)
            if args[0] == 'create':
                path.unlink()
                replacement.bind(str(path))
            return result
        monkeypatch.setattr(runtime, '_control', control)
        with pytest.raises(containers.ContainerPending, match='proxy changed'):
            run(runtime)
        assert 'start' not in daemon.calls
