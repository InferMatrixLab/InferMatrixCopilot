"""Credential side channels preserve the native login and original outcomes."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest

from infermatrix_copilot import container_worker
from infermatrix_copilot.providers.codex_credentials import login_snapshot
from infermatrix_copilot.providers.container_execution import ContainerCompletionProfile, ContainerExecution
from infermatrix_copilot.providers.container_protocol import decode_receipt, receipt_line
from infermatrix_copilot.providers.process import ProcessOutcome
from infermatrix_copilot.providers.worker_credentials import receive_bundle, validate_bundle
from infermatrix_copilot.redaction import CredentialStream, redact
from execution_helpers import contained_codex  # noqa: F401


ACCESS = 'synthetic-access-token-ONLY'
ID_TOKEN = 'synthetic-id-token-ONLY'
REFRESH = 'synthetic-refresh-token-MUST-STAY-HOST'


def test_nested_native_json_cannot_hide_escaped_credentials_across_chunks():
    token = 'synthetic-quote-"-backslash-\\-é-secret'
    payload = json.dumps({'kind': 'event', 'value': {'response': json.dumps({'token': token})}}).encode() + b'\n'
    output = []
    stream = CredentialStream([token], output.append)
    for offset in range(0, len(payload), 3):stream.feed(payload[offset:offset + 3])
    stream.finish()
    nested = json.loads(json.loads(b''.join(output))['value']['response'])
    assert nested['token'] == '[REDACTED]'


@pytest.fixture
def native_login(tmp_path):
    path = tmp_path / 'auth.json'
    path.write_text(json.dumps({'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None,
        'tokens': {'access_token': ACCESS, 'id_token': ID_TOKEN, 'refresh_token': REFRESH,
                   'account_id': 'fixture-account'}, 'last_refresh': '2026-10-13T00:00:00Z'}))
    path.chmod(0o600)
    return path


def test_snapshot_uses_native_external_tokens_and_private_ephemeral_files(native_login):
    from infermatrix_copilot.providers.worker_credentials import credential_environment
    original = native_login.read_bytes()
    credentials = login_snapshot(native_login)
    assert ACCESS not in repr(credentials) and REFRESH not in repr(credentials.metadata)
    with credentials.serve() as channel:
        bundle = receive_bundle(credentials.sha256, channel)
        assert REFRESH not in json.dumps(bundle)
        assert credentials.metadata['source_sha256'] == hashlib.sha256(original).hexdigest()
        with credential_environment(bundle) as env:
            home = Path(env['CODEX_HOME'])
            auth = home / 'auth.json'
            assert auth.stat().st_mode & 0o777 == 0o600
            external = json.loads(auth.read_text())
            assert external['auth_mode'] == 'chatgptAuthTokens'
            assert external['tokens']['access_token'] == ACCESS
            assert external['tokens']['refresh_token'] == ''
        assert not home.exists()
    assert not channel.exists() and native_login.read_bytes() == original


@pytest.mark.parametrize('damage', ['public', 'symlink', 'api', 'invalid'])
def test_unsupported_login_rejected_without_cache_changes(native_login, damage):
    path = native_login
    if damage == 'public':
        path.chmod(0o644)
    elif damage == 'symlink':
        path = native_login.with_name('link.json')
        path.symlink_to(native_login)
    elif damage == 'api':
        path.write_text(json.dumps({'auth_mode': 'apikey', 'OPENAI_API_KEY': ACCESS}))
    else:
        path.write_text('invalid private cache '+ACCESS)
    before = native_login.read_bytes()
    with pytest.raises((OSError, ValueError)) as caught:
        login_snapshot(path)
    assert ACCESS not in str(caught.value) and native_login.read_bytes() == before


def test_service_credential_owner_is_explicit_and_bound(native_login, monkeypatch):
    owner = native_login.stat().st_uid
    monkeypatch.setattr(os, 'getuid', lambda: owner+1)
    with pytest.raises(ValueError, match='private owned regular file'):
        login_snapshot(native_login)
    assert login_snapshot(native_login, owner_uid=owner).sha256
    with pytest.raises(ValueError, match='private owned regular file'):
        login_snapshot(native_login, owner_uid=owner+1)
    for invalid in (True,-1):
        with pytest.raises(ValueError, match='credential owner'):
            login_snapshot(native_login, owner_uid=invalid)


@pytest.mark.parametrize('damage', ['path', 'environment', 'multiline_secret', 'file_parent'])
def test_bad_worker_credential_bundle_fails_before_file_creation(damage):
    bundle = {'files': {'codex/auth.json': '{}'}, 'homes': {'CODEX_HOME': 'codex'}, 'redactions': [ACCESS]}
    if damage == 'path':
        bundle['files'] = {'../outside': 'secret'}
    elif damage == 'environment':
        bundle['homes'] = {'LD_PRELOAD': 'codex'}
    elif damage == 'multiline_secret':
        bundle['redactions'] = ['multiline\nsecret']
    else:
        bundle['files']['codex'] = 'conflicting parent'
    with pytest.raises(ValueError):
        validate_bundle(bundle)


@pytest.mark.parametrize('version', [4, 5])
def test_worker_filters_chunked_credentials_before_streams_and_receipt(native_login, monkeypatch, capsysbinary, version):
    from infermatrix_copilot.providers import worker_credentials
    credentials = login_snapshot(native_login)
    original = native_login.read_bytes()
    code = '''import json,os,sys,time
from pathlib import Path
p=Path(os.environ['CODEX_HOME'])/'auth.json'
v=json.loads(p.read_text())
assert v['auth_mode']=='chatgptAuthTokens' and not v['tokens']['refresh_token']
token=v['tokens']['access_token']
sys.stdout.write(token[:9]);sys.stdout.flush();time.sleep(.02)
sys.stdout.write(token[9:]+'\\n');sys.stdout.flush()
sys.stderr.write(v['tokens']['id_token']);sys.stderr.flush()
'''
    request = {'version': version, 'argv': [sys.executable, '-c', code], 'input': None, 'proxy': False,
               'deadline': time.monotonic()+10, 'credentials_sha256': credentials.sha256}
    if version == 5:
        request['frozen_inputs'] = {'files': {}, 'sealed_dirs': ['/etc/codex']}
        monkeypatch.setattr(container_worker, 'verify_inputs', lambda value: None)
    with credentials.serve() as channel:
        real_receive = worker_credentials.receive_bundle
        monkeypatch.setattr(worker_credentials, 'receive_bundle', lambda digest: real_receive(digest, channel))
        assert container_worker.execute(request) == 0
    captured = capsysbinary.readouterr()
    assert ACCESS.encode() not in captured.out+captured.err
    assert ID_TOKEN.encode() not in captured.out+captured.err and REFRESH.encode() not in captured.out+captured.err
    outcome = ProcessOutcome(captured.out.decode(), captured.err.decode(), 0)
    result = decode_receipt(outcome, hashlib.sha256(json.dumps(request).encode()).hexdigest(), required=True)
    assert result.stdout == '[REDACTED]\n' and result.stderr == '[REDACTED]'
    assert native_login.read_bytes() == original


@pytest.mark.parametrize('version', [4, 5])
def test_credential_hash_mismatch_cannot_start_native_process(native_login, monkeypatch, tmp_path, version):
    from infermatrix_copilot.providers import worker_credentials
    credentials = login_snapshot(native_login)
    marker = tmp_path / 'dispatched'
    request = {'version': version, 'argv': [sys.executable, '-c', f'open({str(marker)!r},"w").write("paid")'],
               'input': None, 'proxy': False, 'deadline': time.monotonic()+10, 'credentials_sha256': 'f'*64}
    if version == 5:
        request['frozen_inputs'] = {'files': {}, 'sealed_dirs': ['/etc/codex']}
        monkeypatch.setattr(container_worker, 'verify_inputs', lambda value: None)
    with credentials.serve() as channel:
        real_receive = worker_credentials.receive_bundle
        monkeypatch.setattr(worker_credentials, 'receive_bundle', lambda digest: real_receive(digest, channel))
        with pytest.raises(ValueError, match='snapshot differs'):
            container_worker.execute(request)
    assert not marker.exists()


def test_original_result_observation_does_not_need_live_credentials(native_login, tmp_path, monkeypatch):
    from dataclasses import asdict
    credentials = login_snapshot(native_login)
    root = tmp_path / 'attempt'
    runtime = ContainerExecution(root, docker='/docker', socket='/daemon')
    profile = ContainerCompletionProfile(runtime, 'sha256:'+'a'*64, credentials=credentials)
    seen = []
    def run(execution, **kwargs):
        seen.append(kwargs)
        bundle = receive_bundle(credentials.sha256, execution.credential_socket)
        assert REFRESH not in json.dumps(bundle)
        public = json.loads(kwargs['input'])
        assert public['credentials_sha256'] == credentials.sha256 and ACCESS not in kwargs['input']
        stdout = '{"ok":true}\n'
        outcome = ProcessOutcome(stdout, receipt_line(public,
            {'stdout':hashlib.sha256(stdout.encode()), 'stderr':hashlib.sha256(b'')}, 0, None), 0)
        root.mkdir(exist_ok=True)
        binding = {'version':1,'name':'imc-'+'a'*32,'control':execution.command,'daemon':'daemon',
            'image':kwargs['image'],'argv':kwargs['argv'],'proxy':None,
            'input_sha256':hashlib.sha256(kwargs['input'].encode()).hexdigest(),
            'worker_protocol':4,'worker_deadline':kwargs['worker_deadline'],
            'worker_credentials_sha256':credentials.sha256}
        identity = {'name':binding['name'],'daemon':'daemon','id':'a'*64}
        (root/'binding.json').write_text(json.dumps(binding))
        (root/'container.json').write_text(json.dumps(identity))
        (root/'result.json').write_text(json.dumps({'name':binding['name'],'id':identity['id'],'outcome':asdict(outcome)}))
        return outcome
    monkeypatch.setattr(ContainerExecution, 'run', run)
    cmd = ['/usr/local/bin/codex','exec','-']
    result = profile.execute(cmd, cwd='/tmp/work', env={}, input='frozen', deadline=time.monotonic()+10)
    assert result.stdout == '{"ok":true}\n'
    native_login.unlink()
    observer = ContainerCompletionProfile(ContainerExecution(root,docker='/docker',socket='/daemon'),profile.image)
    assert observer.get_result(cmd,cwd='/tmp/work',env={},input='frozen') == result
    assert len(seen)==1
    persisted = ''.join(path.read_text() for path in root.glob('*.json'))
    assert ACCESS not in persisted and REFRESH not in persisted


def test_trace_and_worker_use_one_secret_filter():
    from infermatrix_copilot.trace_store import redact as legacy_redact
    assert legacy_redact is redact
    parts=[]
    stream=CredentialStream([ACCESS], parts.append)
    for byte in (ACCESS+'\n'+ID_TOKEN).encode():
        stream.feed(bytes([byte]))
    stream.finish()
    assert b''.join(parts)==('[REDACTED]\n'+ID_TOKEN).encode()
    # Explicit token values are not environment paths, even with this prefix.
    explicit='/opaque-native-token'
    parts=[]
    stream=CredentialStream([explicit],parts.append)
    stream.feed(explicit.encode())
    stream.finish()
    assert b''.join(parts)==b'[REDACTED]'


def test_configured_login_reaches_canonical_worker_and_sanitized_receipt(contained_codex):
    from types import SimpleNamespace
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.providers import llm_for
    from infermatrix_copilot.trace_store import TraceStore
    def native(argv, **kwargs):
        auth=json.loads((Path(kwargs['env']['CODEX_HOME'])/'auth.json').read_text())
        assert auth['auth_mode']=='chatgptAuthTokens' and not auth['tokens']['refresh_token']
        text=json.dumps({'ok':True,'leaked':auth['tokens']['access_token']})
        events=[{'type':'item.completed','item':{'type':'agent_message','text':text}},
                {'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}]
        return SimpleNamespace(stdout='\n'.join(map(json.dumps,events)),stderr='',returncode=0)
    contained_codex.runner=native
    settings=Settings(_env_file=None,strict_backend='codex',strict_backend_model='fixture-model')
    assert json.loads(llm_for(settings).create(system='policy',messages=[{'role':'user','content':'frozen'}]).text)=={
        'ok':True,'leaked':'[REDACTED]'}
    store=TraceStore(settings.trace_store_root)
    row,=[row for row,_ in store.iter_records()]
    store.validate_native_attempt(row)
    descriptor=json.loads((store.root/'attempts'/row['result']['native_attempt_id']/'execution-profile.json').read_text())
    assert set(descriptor['credentials'])=={'source_sha256','snapshot_sha256'}
    for reference in [*row['inputs'].values(),*row['outputs'].values()]:
        text=store.blob(reference)
        assert 'fixture-access-token' not in text and 'host-only-refresh-token' not in text
    assert len(contained_codex.calls)==1


def test_bad_configured_login_is_rejected_at_workflow_preflight(contained_codex):
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.providers import llm_for
    from infermatrix_copilot.providers.completion_profiles import CompletionPolicyError
    settings=Settings(_env_file=None,strict_backend='codex')
    path=Path(settings.native_completion_profiles['codex']['codex_auth_file'])
    path.write_text(json.dumps({'auth_mode':'apikey','OPENAI_API_KEY':ACCESS}))
    with pytest.raises(CompletionPolicyError,match='login is unavailable'):
        llm_for(settings).validate_completion()
    assert contained_codex.calls==[]
