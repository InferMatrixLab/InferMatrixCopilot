"""One private-file/side-channel path for configured native provider accounts."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from infermatrix_copilot.providers.credential_sources import normalize_source, read_private_login, snapshot_source
from infermatrix_copilot.providers.completion_profiles import (
    CompletionPolicyError, normalize_completion_profiles, parse_completion_config, sandbox_profile_for_config,
    prepare_completion_transport)
from infermatrix_copilot.providers.worker_credentials import credential_environment, receive_bundle
from infermatrix_copilot.providers.zcode import ZCodeTransport, _DEFAULT_PROVIDER_ID
from infermatrix_copilot.providers import zcode_credentials
from infermatrix_copilot.sandbox import SandboxExecution
from test_completion_profile_configuration import configured  # noqa: F401
from test_subscription_admission import admitted_setup  # noqa: F401
from execution_helpers import contained_codex  # noqa: F401


TOKEN = 'synthetic-coding-token-ONLY'
OTHER = 'other-account-token-MUST-STAY-HOST'
REFRESH = 'oauth-refresh-MUST-STAY-HOST'
OPTIONS = {'zcode_provider_id': _DEFAULT_PROVIDER_ID, 'zcode_reasoning_level': 'max',
           'model_mismatch_policy': 'fail'}


@pytest.fixture
def login(tmp_path):
    path = tmp_path / 'credentials.json'
    path.write_text(json.dumps({'account-provider:' + _DEFAULT_PROVIDER_ID + ':identity': 'fixture-account',
        'account-provider:coding-plan:' + _DEFAULT_PROVIDER_ID + ':account:fixture-account:api-key': TOKEN,
        'oauth:bigmodel:refresh_token': REFRESH, 'unrelated-account:api-key': OTHER}))
    path.chmod(0o600)
    return path


def version_cli(directory):
    path = directory / 'native-version'
    path.write_text('#!/usr/bin/env python3\nimport sys\nassert sys.argv[1:]==["--version"]\nprint("0.16.9")\n')
    path.chmod(0o700)
    return path


def test_selected_account_snapshot_and_zero_model_control_share_the_original_private_channel(login, monkeypatch):
    original = login.read_bytes()
    credentials = snapshot_source({'provider': 'zcode', 'path': str(login)}, options=OPTIONS)
    assert credentials.identity == {'kind': 'zcode-coding-plan', 'expires_at': None}
    assert TOKEN not in repr(credentials) and REFRESH not in repr(credentials.metadata)
    with credentials.serve() as channel:
        bundle = receive_bundle(credentials.sha256, channel)
        serialized = json.dumps(bundle)
        assert TOKEN in serialized and OTHER not in serialized and REFRESH not in serialized
        with credential_environment(bundle) as env:
            for name, value in env.items():monkeypatch.setenv(name, value)
            marker = Path(env['ZCODE_DATA_BASE_DIR']) / zcode_credentials.MARKER
            assert marker.stat().st_mode & 0o777 == 0o600
            monkeypatch.setattr(sys, 'stdin', StringIO(json.dumps({'provider_id': _DEFAULT_PROVIDER_ID,
                                                'native_cli': str(version_cli(login.parent))})))
            stdout = StringIO()
            with redirect_stdout(stdout):assert zcode_credentials.main() == 0
            assert ZCodeTransport.subscription_auth_status(0, stdout.getvalue())
            assert not ZCodeTransport.subscription_auth_status(0, stdout.getvalue() + TOKEN)
        assert not marker.exists()
    assert login.read_bytes() == original


@pytest.mark.parametrize('damage', ['public', 'symlink', 'fifo', 'invalid', 'missing', 'wrong-provider'])
def test_invalid_native_sources_refuse_without_exposing_or_changing_material(login, damage):
    path = login
    options = dict(OPTIONS)
    if damage == 'public':login.chmod(0o644)
    elif damage == 'symlink':
        path = login.with_name('link');path.symlink_to(login)
    elif damage == 'fifo':
        path = login.with_name('pipe');os.mkfifo(path, 0o600)
    elif damage == 'invalid':login.write_text('invalid ' + TOKEN)
    elif damage == 'missing':login.write_text('{}')
    else:options['zcode_provider_id'] = 'custom-api-provider'
    before = login.read_bytes()
    with pytest.raises((ValueError, OSError)) as error:
        snapshot_source({'provider': 'zcode', 'path': str(path)}, options=options)
    assert TOKEN not in str(error.value) and login.read_bytes() == before


def test_credential_source_is_independent_of_backend_and_location(configured, login, tmp_path):
    settings, _ = configured
    backend = dict(settings.native_completion_profiles['codex'], native_cli=str(version_cli(tmp_path)))
    source = {'provider': 'zcode', 'path': str(login)}
    value = {'kind': 'sandbox', 'backend': backend, 'location': {'kind': 'local'},
             'credentials_source': source, 'provider_options': OPTIONS}
    config = parse_completion_config(normalize_completion_profiles({'zcode': value})['zcode'])
    profile = sandbox_profile_for_config(config, tmp_path / 'local')
    assert profile.sandbox.credentials.identity['kind'] == 'zcode-coding-plan'
    value['location'] = {'kind': 'ssh', 'host': 'worker', 'python': '/opt/env/bin/python', 'state_root': '/state'}
    remote = parse_completion_config(normalize_completion_profiles({'zcode': value})['zcode']).profile(tmp_path / 'remote')
    assert remote.sandbox.backend['credentials_source'] == source
    assert remote.sandbox.backend['location'] == {'kind': 'local'}
    assert TOKEN not in json.dumps(remote.sandbox.backend)
    assert SandboxExecution.supports_credentials is False


def test_source_provider_and_single_authority_are_checked_before_dispatch(configured, login):
    settings, calls = configured
    backend = dict(settings.native_completion_profiles['codex'], native_cli='/opt/native-zcode')
    value = {'kind': 'sandbox', 'backend': backend, 'location': {'kind': 'local'},
             'credentials_source': {'provider': 'codex', 'path': str(login)}}
    with pytest.raises(CompletionPolicyError, match='selected provider'):
        normalize_completion_profiles({'zcode': value})
    backend['codex_auth_file'] = '/old/auth.json'
    with pytest.raises(CompletionPolicyError):normalize_completion_profiles({'codex': value})
    assert not calls


def test_credentialed_zcode_checks_served_provider(login, monkeypatch):
    credentials = snapshot_source({'provider': 'zcode', 'path': str(login)}, options=OPTIONS)
    with credentials.serve() as channel:
        bundle = receive_bundle(credentials.sha256, channel)
        with credential_environment(bundle) as env:
            for name, value in env.items():monkeypatch.setenv(name, value)
            transport = ZCodeTransport(SimpleNamespace(zcode_provider_id=_DEFAULT_PROVIDER_ID))
            transport._check_credential_provider([{'type': 'session.updated', 'payload': {'providerId': _DEFAULT_PROVIDER_ID}},
                                                  {'type': 'session.updated', 'payload': {'title': 'partial update'}}])
            with pytest.raises(Exception, match='selected native account provider'):
                transport._check_credential_provider([{'type': 'session.updated', 'payload': {'providerId': 'other'}}])
            with pytest.raises(Exception, match='selected native account provider'):
                transport._check_credential_provider([])


def test_generic_codex_source_uses_the_same_local_and_remote_admission_and_model_path(admitted_setup):
    from infermatrix_copilot.kb_service.models import ModelGateway
    from infermatrix_copilot.trace_store import NativeCompletionRecorder
    _, settings, store, role, auth, state, _ = admitted_setup
    old = settings.native_completion_profiles['codex']
    backend = dict(old['backend'] if old['kind'] == 'sandbox' else old)
    backend.pop('codex_auth_file', None);backend.pop('codex_auth_owner_uid', None)
    profile = {'kind': 'sandbox', 'backend': backend, 'location': old.get('location', {'kind': 'local'}),
               'credentials_source': {'provider': 'codex', 'path': str(auth)}}
    settings = settings.model_copy(update={'native_completion_profiles': normalize_completion_profiles({'codex': profile})})
    gateway = ModelGateway(settings, recorder=NativeCompletionRecorder(store))
    assert gateway.subscription_billing(role)
    assert state['auth_calls'] == 1 and state['model_calls'] == 0
    assert gateway.call_json(role, system='policy', prompt='source evidence').data == {'ok': True}
    assert state['model_calls'] == 1
    row, = [r for r, _ in store.iter_records(kind='model_call')]
    store.validate_native_attempt(row)
    assert row['result']['subscription'] is True and row['result']['cost_usd'] is None


def test_zcode_admission_uses_actual_account_control_and_binds_source_before_a_model(configured, login, tmp_path, monkeypatch):
    from infermatrix_copilot.providers.container_execution import ContainerExecution
    from infermatrix_copilot.providers.process import ProcessOutcome
    from infermatrix_copilot.providers.registry import transport_for_id
    from infermatrix_copilot.providers.subscription_admission import admit_subscription
    from infermatrix_copilot.sandbox.docker import DockerSandboxExecution
    from infermatrix_copilot.trace_store import TraceStore
    settings, calls = configured
    backend = dict(settings.native_completion_profiles['codex'], native_cli=str(version_cli(tmp_path)))
    profile = {'kind': 'sandbox', 'backend': backend, 'location': {'kind': 'local'},
               'credentials_source': {'provider': 'zcode', 'path': str(login)}, 'provider_options': OPTIONS}
    settings = settings.model_copy(update={'native_completion_profiles': normalize_completion_profiles({'zcode': profile})})
    controls = []
    def execute(execution, command, **kwargs):
        assert command[-1] == 'infermatrix_copilot.providers.zcode_credentials'
        controls.append(command)
        bundle = receive_bundle(execution.credentials_sha256, execution.credential_socket)
        with credential_environment(bundle) as env, monkeypatch.context() as patch:
            for key, value in env.items():patch.setenv(key, value)
            patch.setattr(sys, 'stdin', StringIO(kwargs['input']))
            output = StringIO()
            with redirect_stdout(output):code = zcode_credentials.main()
        return ProcessOutcome(output.getvalue(), '', code)
    monkeypatch.setattr(ContainerExecution, 'run_command', execute)
    monkeypatch.setattr(DockerSandboxExecution, 'context', lambda self: self._prepared_context())
    original = transport_for_id(settings, 'zcode')
    admitted = admit_subscription(settings, original)
    assert admitted is not None and len(controls) == 1 and not calls
    assert not getattr(original, 'configured_subscription_admission', None)
    proof = admitted.configured_subscription_admission
    assert proof['context']['credentials']['kind'] == 'zcode-coding-plan'
    assert proof['context']['credentials']['expires_at'] is None
    store = TraceStore(settings.trace_store_root)
    archive = store.begin_call({'provider': 'zcode', 'model': 'GLM-5.3', 'system': 'policy', 'prompt': 'evidence'})
    prepared = prepare_completion_transport(settings, admitted, archive)
    assert prepared.subscription_billing and prepared.completion_profile.sandbox.expected_context == proof['context']
    login.write_text(login.read_text().replace(TOKEN, OTHER))
    changed = store.begin_call({'provider': 'zcode', 'system': 'policy', 'prompt': 'another evidence'})
    with pytest.raises(ValueError):prepare_completion_transport(settings, admitted, changed)
    assert len(controls) == 1 and not calls
    assert not list(store.iter_records(kind='model_call'))
    assert all(TOKEN.encode() not in path.read_bytes() for path in store.root.rglob('*') if path.is_file())


def encrypted_cache_value(value, secret):
    """Independent writer for the native AES-GCM wire format; test material only."""
    import base64, hashlib
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv=bytes(range(12));encrypted=AESGCM(hashlib.sha256(secret.encode()).digest()).encrypt(iv,value.encode(),None)
    encode=lambda part:base64.urlsafe_b64encode(part).decode().rstrip('=')
    return 'enc:v1:'+'.'.join(map(encode,[iv,encrypted[-16:],encrypted[:-16]]))


def test_encrypted_selected_account_projects_plain_native_compatible_values_only(login, monkeypatch):
    secret='fixture-native-secret';monkeypatch.setenv('ZCODE_CREDENTIAL_SECRET',secret)
    data=json.loads(login.read_bytes());identity_key='account-provider:'+_DEFAULT_PROVIDER_ID+':identity'
    key='account-provider:coding-plan:'+_DEFAULT_PROVIDER_ID+':account:fixture-account:api-key'
    data[identity_key]=encrypted_cache_value('fixture-account',secret);data[key]=encrypted_cache_value(TOKEN,secret)
    data['unrelated-account:api-key']='enc:v1:corrupt.unused.value'
    login.write_text(json.dumps(data));original=login.read_bytes()
    credentials=snapshot_source({'provider':'zcode','path':str(login)},options=OPTIONS)
    with credentials.serve() as channel:
        bundle=receive_bundle(credentials.sha256,channel)
        projected=json.loads(bundle['files']['zcode/.zcode/v2/credentials.json'])
        assert projected=={identity_key:'fixture-account',key:TOKEN}
        assert secret not in json.dumps(bundle) and REFRESH not in json.dumps(bundle)
        assert TOKEN in bundle['redactions'] and TOKEN not in repr(credentials)
        with credential_environment(bundle) as env:
            for name,value in env.items():monkeypatch.setenv(name,value)
            monkeypatch.setattr(sys,'stdin',StringIO(json.dumps({'provider_id':_DEFAULT_PROVIDER_ID,'native_cli':str(version_cli(login.parent))})))
            with redirect_stdout(StringIO()):assert zcode_credentials.main()==0
    assert login.read_bytes()==original


def test_encrypted_cache_fallback_uses_credential_owner_instead_of_service_uid(login,monkeypatch):
    import pwd
    owner=os.getuid();monkeypatch.delenv('ZCODE_CREDENTIAL_SECRET',raising=False)
    looked_up=[]
    def principal(uid):
        looked_up.append(uid);return SimpleNamespace(pw_dir='/native-home',pw_name='native-user')
    monkeypatch.setattr(pwd,'getpwuid',principal);monkeypatch.setattr(os,'getuid',lambda:0)
    secret=f'zcode-credential-fallback:{sys.platform}:/native-home:native-user'
    data=json.loads(login.read_bytes())
    for key in list(data):
        if key.endswith(':identity') or key.endswith(':account:fixture-account:api-key'):
            data[key]=encrypted_cache_value(data[key],secret)
    login.write_text(json.dumps(data))
    result=snapshot_source({'provider':'zcode','path':str(login),'owner_uid':owner},options=OPTIONS)
    assert result.identity['kind']=='zcode-coding-plan' and looked_up==[owner]


@pytest.mark.parametrize('damage',['wrong-key','tag','format','version'])
def test_corrupt_encrypted_selected_identity_refuses_without_cache_changes_or_secret_diagnostics(login,monkeypatch,damage):
    secret='fixture-native-secret';monkeypatch.setenv('ZCODE_CREDENTIAL_SECRET',secret)
    encoded=encrypted_cache_value('fixture-account',secret)
    if damage=='wrong-key':monkeypatch.setenv('ZCODE_CREDENTIAL_SECRET','different-fixture-secret')
    elif damage=='tag':
        parts=encoded.split('.');parts[1]='A'*22;encoded='.'.join(parts)
    elif damage=='format':encoded='enc:v1:invalid'
    else:encoded=encoded.replace('enc:v1:','enc:v2:')
    data=json.loads(login.read_bytes());data['account-provider:'+_DEFAULT_PROVIDER_ID+':identity']=encoded
    login.write_text(json.dumps(data));before=login.read_bytes()
    with pytest.raises(ValueError) as raised:snapshot_source({'provider':'zcode','path':str(login)},options=OPTIONS)
    assert secret not in str(raised.value) and encoded not in str(raised.value) and TOKEN not in str(raised.value)
    assert login.read_bytes()==before


def test_plain_cache_needs_no_crypto_extra_and_missing_crypto_refuses_encrypted_cache(login,monkeypatch):
    import builtins
    encrypted=encrypted_cache_value('fixture-account','fixture-native-secret')
    original_import=builtins.__import__
    def without_crypto(name,*args,**kwargs):
        if name.startswith('cryptography'):raise ImportError('fixture missing optional crypto')
        return original_import(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',without_crypto)
    assert snapshot_source({'provider':'zcode','path':str(login)},options=OPTIONS).identity['kind']=='zcode-coding-plan'
    data=json.loads(login.read_bytes());data['account-provider:'+_DEFAULT_PROVIDER_ID+':identity']=encrypted
    login.write_text(json.dumps(data))
    with pytest.raises(ValueError):snapshot_source({'provider':'zcode','path':str(login)},options=OPTIONS)
