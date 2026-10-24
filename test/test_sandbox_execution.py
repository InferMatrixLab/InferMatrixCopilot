"""Provider-neutral profiles and location parity, with native boundaries mocked."""
from dataclasses import asdict
import json
from pathlib import Path
import threading
import time

import pytest

from test_completion_profile_configuration import configured  # noqa: F401

from infermatrix_copilot import sandbox_agent
from infermatrix_copilot.config import Settings
from infermatrix_copilot.persistence import immutable_write_bytes
from infermatrix_copilot.providers.completion_profiles import normalize_completion_profiles
from infermatrix_copilot.providers.process import ProcessOutcome
from infermatrix_copilot.providers.registry import transport_for_id
from infermatrix_copilot.sandbox import BoundSandboxProfile, SandboxExecution, SandboxPending
from infermatrix_copilot.sandbox.remote import RemoteSandboxExecution, SSHLocation, canonical


class Backend(SandboxExecution):
    def __init__(self, root, calls):
        self.root, self.calls = root, calls

    def preflight(self, *, for_dispatch=True):
        pass

    def execute(self, cmd, *, cwd, env, input=None, on_line=None, **kwargs):
        self.root.mkdir(parents=True, exist_ok=True)
        immutable_write_bytes(self.root / 'native-intent', canonical([cmd, cwd, env, input]).encode())
        self.calls.append(('execute', str(self.root)))
        stdout = '\n'.join(map(json.dumps, [
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"ok":true}'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 3, 'output_tokens': 2}}])) + '\n'
        if on_line:
            for line in stdout.splitlines():
                on_line('stdout', line)
        outcome = ProcessOutcome(stdout, '', 0)
        immutable_write_bytes(self.root / 'result', canonical(asdict(outcome)).encode())
        return outcome

    def result(self, cmd, *, cwd, env, input=None, **kwargs):
        assert (self.root / 'native-intent').read_text() == canonical([cmd, cwd, env, input])
        return self.read_result(**kwargs)

    def read_result(self, **kwargs):
        self.calls.append(('result', str(self.root)))
        return ProcessOutcome(**json.loads((self.root / 'result').read_text()))

    def cancel(self):
        self.calls.append(('cancel', str(self.root)))


@pytest.fixture
def remote(tmp_path, monkeypatch):
    calls, controls = [], []
    location = SSHLocation('ssh', 'fixture-host', '/remote/python', str(tmp_path/'remote'), ssh='/bin/true')
    config = {'kind':'docker', 'docker':'/remote/docker', 'socket':'/remote/daemon.sock',
              'image':'sha256:'+'a'*64}
    executor = RemoteSandboxExecution(tmp_path/'controller', location, config)
    state = {'drop':False, 'malformed':False, 'after_terminal':False}

    def local_profile(config, root, *, for_dispatch=True):
        return BoundSandboxProfile(Backend(root, calls))

    def ssh(command, *, input, on_line, cleanup=None, **kwargs):
        assert command == location.command()
        request = json.loads(input)
        controls.append(request['operation'])
        frames = []
        try:
            value = sandbox_agent.perform(request, frames.append)
            frames.append({'type':'result','value':value})
            returncode = 0
        except Exception:
            frames.append({'type':'error','code':'pending'})
            returncode = 1
        try:
            for frame in frames:
                if state['drop'] and request['operation']=='execute' and frame['type']=='result':
                    raise OSError('lost SSH reply')
                on_line('stdout', 'malformed' if state['malformed'] and request['operation']=='execute' else canonical(frame))
            if state['after_terminal'] and request['operation']=='execute':
                on_line('stdout', canonical({'type':'line','channel':'stdout','text':'late'}))
        finally:
            if cleanup:
                cleanup()
        return ProcessOutcome('', '', returncode)

    monkeypatch.setattr(sandbox_agent, 'local_sandbox_profile', local_profile)
    monkeypatch.setattr('infermatrix_copilot.sandbox.remote.run_process', ssh)
    return executor, calls, controls, state


def invoke(executor, **kwargs):
    return executor.execute(['/native'], cwd='/tmp/work', env={}, input='frozen',
                            deadline=time.monotonic()+30, **kwargs)


def test_provider_accepts_a_sandbox_interface_without_a_docker_type(tmp_path):
    profile = BoundSandboxProfile(Backend(tmp_path, []), '/sandbox/native')
    transport = transport_for_id(Settings(_env_file=None), 'codex', completion_profile=profile)
    reply = transport.complete(system='policy', messages=[{'role':'user','content':'evidence'}])
    assert reply.text == '{"ok":true}' and reply.usage['input_tokens'] == 3
    assert 'container_execution' not in Path(__import__(
        'infermatrix_copilot.providers.codex', fromlist=['__file__']).__file__).read_text()


def test_remote_streams_and_terminal_result_use_the_same_backend(remote):
    executor, calls, controls, _ = remote
    lines = []
    result = invoke(executor, on_line=lambda *line: lines.append(line))
    assert lines == [('stdout', line) for line in result.stdout.splitlines()]
    assert len([c for c in calls if c[0]=='execute']) == 1
    assert executor.result(['/native'], cwd='/tmp/work', env={}, input='frozen') == result
    assert controls == ['preflight','execute']  # durable local result needs no SSH
    with pytest.raises(SandboxPending):
        invoke(executor)
    assert controls == ['preflight','execute']


def test_lost_remote_reply_reconciles_original_execution_without_replay(remote):
    executor, calls, controls, state = remote
    state['drop'] = True
    with pytest.raises(SandboxPending) as error:
        invoke(executor)
    assert error.value.native_outcome_pending
    assert controls == ['preflight','execute','cancel']
    with pytest.raises(SandboxPending):
        invoke(executor)
    state['drop'] = False
    result = executor.result(['/native'], cwd='/tmp/work', env={}, input='frozen', recover=True)
    assert json.loads(result.stdout.splitlines()[0])['item']['text']=='{"ok":true}'
    assert len([c for c in calls if c[0]=='execute']) == 1
    assert controls == ['preflight','execute','cancel','result']


def test_original_remote_result_uses_bound_worker_without_rebuilding_input(remote, monkeypatch):
    executor, calls, controls, state = remote
    state['drop'] = True
    with pytest.raises(SandboxPending):
        invoke(executor)
    state['drop'] = False
    monkeypatch.setattr(executor, '_request', lambda *a, **k: pytest.fail('rebuilt caller input'))
    result = BoundSandboxProfile(executor).read_result(recover=True)
    assert executor.read_result() == result
    assert controls == ['preflight', 'execute', 'cancel', 'result']
    assert [kind for kind, _ in calls].count('execute') == 1


@pytest.mark.parametrize('change', ['host', 'backend', 'release', 'corrupt', 'result'])
def test_original_remote_result_rejects_changed_authority(remote, change):
    executor, calls, controls, _ = remote
    invoke(executor)
    if change == 'host':
        from dataclasses import replace
        executor = RemoteSandboxExecution(executor.root, replace(executor.location, host='other'), executor.backend)
    elif change == 'backend':
        executor.backend['image'] = 'sha256:' + 'b' * 64
    elif change == 'release':
        path = executor.root / 'remote-binding.json'
        binding = json.loads(path.read_text())
        binding['release'] = 'b' * 64
        path.write_text(canonical(binding))
        (executor.root / 'remote-result.json').unlink()
    else:
        path = executor.root / ('remote-result.json' if change == 'result' else 'remote-binding.json')
        path.write_text('broken')
    with pytest.raises(SandboxPending):
        executor.read_result(recover=True)
    assert [kind for kind, _ in calls].count('execute') == 1
    assert controls.count('execute') == 1


def test_unknown_original_remote_result_stays_pending_without_another_dispatch(remote):
    executor, calls, controls, _ = remote
    invoke(executor)
    (executor.root / 'remote-result.json').unlink()
    root = Path(next(root for kind, root in calls if kind == 'execute'))
    (root / 'result').unlink()
    with pytest.raises(SandboxPending):
        executor.read_result(recover=True)
    with pytest.raises(SandboxPending):
        invoke(executor)
    assert controls == ['preflight', 'execute', 'result']
    assert [kind for kind, _ in calls].count('execute') == 1


def test_original_remote_result_keeps_backend_request_matching(remote):
    executor, calls, controls, _ = remote
    invoke(executor)
    (executor.root / 'remote-result.json').unlink()
    root = Path(next(root for kind, root in calls if kind == 'execute'))
    (root / 'native-intent').write_text(canonical([['/native'], '/tmp/work', {}, 'other evidence']))
    with pytest.raises(SandboxPending):
        executor.read_result(recover=True)
    assert controls.count('execute') == 1


@pytest.mark.parametrize('change', ['host','backend','prompt','release','corrupt'])
def test_remote_recovery_refuses_changed_identity(remote, change):
    executor, calls, controls, _ = remote
    invoke(executor)
    args = dict(cwd='/tmp/work', env={}, input='frozen')
    if change=='host':
        from dataclasses import replace
        executor = RemoteSandboxExecution(executor.root, replace(executor.location, host='other'), executor.backend)
    elif change=='backend':
        executor.backend['image']='sha256:'+'b'*64
    elif change=='prompt':
        args['input']='changed'
    else:
        path = executor.root/'remote-binding.json'
        value=json.loads(path.read_text())
        if change=='release':
            value['release']='b'*64
            path.write_text(canonical(value))
            (executor.root/'remote-result.json').unlink()
        else:
            path.write_text('broken')
    before=len(controls)
    with pytest.raises(SandboxPending):
        executor.result(['/native'], **args, recover=True)
    assert len([c for c in calls if c[0]=='execute'])==1
    assert len(controls)<=before+1


@pytest.mark.parametrize('damage', ['malformed','after_terminal'])
def test_malformed_control_response_never_authorizes_replacement(remote, damage):
    executor, calls, controls, state=remote
    executor.preflight()
    state[damage]=True
    with pytest.raises(SandboxPending):
        invoke(executor)
    assert len([c for c in calls if c[0]=='execute'])==1
    with pytest.raises(SandboxPending):
        invoke(executor)


def test_remote_agent_reservation_arbitrates_duplicate_dispatch(remote):
    executor, calls, controls, _=remote
    invoke(executor)
    binding=executor._read_binding()
    with pytest.raises(SandboxPending):
        executor._invoke('execute', binding)
    assert len([c for c in calls if c[0]=='execute'])==1


def test_cancelled_before_reservation_never_contacts_remote(remote):
    executor, calls, controls, _=remote
    stopped=threading.Event(); stopped.set()
    with pytest.raises(ValueError):
        invoke(executor, cancel=stopped)
    assert not controls and not calls and not (executor.root/'remote-binding.json').exists()


def test_new_configuration_separates_backend_and_location_and_retains_legacy_bytes():
    legacy={'kind':'docker','docker':'/docker','socket':'/daemon','image':'sha256:'+'a'*64}
    baseline=normalize_completion_profiles({'codex':legacy})
    new={'kind':'sandbox','backend':legacy,'location':{'kind':'ssh','host':'fixture-host',
         'python':'/remote/python','state_root':'/remote/attempts'}}
    normalized=normalize_completion_profiles({'codex':new})
    assert normalized['codex']['backend']['image']==legacy['image']
    assert normalized['codex']['location']['kind']=='ssh'
    assert normalize_completion_profiles(baseline)==baseline
    assert normalize_completion_profiles(normalized)==normalized


@pytest.mark.parametrize('host', ['-oProxyCommand=bad','host with spaces','host\ncommand'])
def test_remote_location_rejects_option_and_shell_injection(host):
    with pytest.raises(ValueError):
        SSHLocation.parse({'kind':'ssh','host':host,'python':'/python','state_root':'/state'})


def test_configured_remote_completion_uses_gateway_archive_and_typed_result(remote, tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
    from infermatrix_copilot.trace_store import NativeCompletionRecorder, TraceStore
    from infermatrix_copilot.providers.codex import CodexTransport
    executor, calls, controls, _=remote
    config={'kind':'sandbox','backend':executor.backend,'location':asdict(executor.location)}
    settings=Settings(_env_file=None, native_completion_profiles={'codex':config},
                      trace_store_root=str(tmp_path/'traces'))
    monkeypatch.setattr(CodexTransport, 'cli_path', lambda _:pytest.fail('remote completion must not run the host CLI'))
    monkeypatch.setattr(CodexTransport, 'auth_gap', lambda _:pytest.fail('remote login must not be read on the controller'))
    store=TraceStore(settings.trace_store_root)
    gateway=ModelGateway(settings,recorder=NativeCompletionRecorder(store))
    reply=gateway.call_json(ModelRole('judge','codex','fixture'),system='policy',prompt='frozen evidence')
    assert reply.data=={'ok':True}
    rows=[row for row,_ in store.iter_records() if row['kind']=='model_call']
    assert len(rows)==1 and len([c for c in calls if c[0]=='execute'])==1
    store.validate_native_attempt(rows[0])
    attempt=rows[0]['result']['native_attempt_id']
    binding=json.loads((store.root/'attempts'/attempt/'execution/remote-binding.json').read_text())
    assert binding['location']['host']=='fixture-host' and binding['backend']['image']==executor.backend['image']


def test_configured_local_sandbox_keeps_the_existing_execution_path(configured):
    from infermatrix_copilot.providers import llm_for
    settings,calls=configured
    legacy=settings.native_completion_profiles['codex']
    settings=settings.model_copy(update={'native_completion_profiles':normalize_completion_profiles(
        {'codex':{'kind':'sandbox','backend':legacy,'location':{'kind':'local'}}})})
    assert llm_for(settings).create(system='policy',messages=[{'role':'user','content':'frozen'}]).text=='{"ok":true}'
    assert len(calls)==1


def test_provider_profiles_do_not_import_sandbox_backends_or_locations():
    import ast
    from infermatrix_copilot.providers import codex, registry
    for module in [codex, registry]:
        tree=ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not set((node.module or '').split('.')) & {'container_execution','docker','remote'}


def test_cached_remote_output_corruption_cannot_be_used_or_redispatched(remote):
    executor,calls,controls,_=remote
    invoke(executor)
    path=executor.root/'remote-result.json'
    value=json.loads(path.read_text())
    value['outcome']['stdout']='different but valid output'
    path.write_text(canonical(value))
    for recover in [False,True]:
        with pytest.raises(SandboxPending):
            executor.result(['/native'],cwd='/tmp/work',env={},input='frozen',recover=recover)
    with pytest.raises(SandboxPending):
        invoke(executor)
    assert len([c for c in calls if c[0]=='execute'])==1 and controls==['preflight','execute']


def test_registered_backend_needs_no_provider_or_location_special_case(tmp_path, monkeypatch):
    from dataclasses import dataclass
    from infermatrix_copilot.providers import llm_for
    from infermatrix_copilot.sandbox import backends
    monkeypatch.setattr(backends,'_CONFIGS',dict(backends._CONFIGS))

    @dataclass(frozen=True)
    class TestConfig(backends.SandboxBackendConfig):
        kind: str
        native_cli: str='/sandbox/native'

        @classmethod
        def parse(cls, value):
            return cls(**value)

        def profile(self, root, *, credentials=None):
            return BoundSandboxProfile(Backend(Path(root),[]),self.native_cli)

        def prepare(self, root, *, for_dispatch=True):
            profile=self.profile(root)
            profile.validate(for_dispatch=for_dispatch)
            return profile

    backends.register_backend('fixture',TestConfig)
    settings=Settings(_env_file=None,strict_backend='codex',trace_store_root=str(tmp_path/'traces'),
        native_completion_profiles={'codex':{'kind':'sandbox','backend':{'kind':'fixture'},'location':{'kind':'local'}}})
    assert llm_for(settings).create(system='policy',messages=[{'role':'user','content':'frozen'}]).text=='{"ok":true}'
    with pytest.raises(ValueError,match='duplicate'):
        backends.register_backend('fixture',TestConfig)
