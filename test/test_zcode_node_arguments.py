"""Large credentialed input crosses argv without granting a native Read tool."""
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from infermatrix_copilot.providers.base import flatten_messages
from infermatrix_copilot.providers.credential_sources import snapshot_source
from infermatrix_copilot.providers.worker_credentials import credential_environment, receive_bundle
from infermatrix_copilot.providers.zcode import ZCodeTransport, _DEFAULT_PROVIDER_ID
from test_native_credential_sources import login, OPTIONS  # noqa: F401


def transport(cli):
    return ZCodeTransport(SimpleNamespace(strict_backend_cli=str(cli), strict_backend_model='GLM-5.3',
        strict_backend_timeout_s=15, zcode_provider_id=_DEFAULT_PROVIDER_ID,
        zcode_reasoning_level='max', model_mismatch_policy='fail'))


def test_node_input_is_exact_above_linux_single_argument_limit_without_read_or_pipe_loss(login, tmp_path, monkeypatch):
    node = shutil.which('node')
    if not node:
        pytest.skip('the optional native Node CLI runtime is unavailable')
    cli = tmp_path / 'native-node'
    cli.write_text('#!' + node + '''
const fs=require('node:fs'),crypto=require('node:crypto');
if (process.argv.includes('--version')) { console.log('0.16.9');process.exit(0); }
if (!fs.fstatSync(1).isFile()) throw new Error('native output lost regular-file spooling');
const prompt=process.argv.find(v=>v.startsWith('--prompt=')).slice('--prompt='.length);
if (Buffer.byteLength(prompt,'utf8')<=131072) throw new Error('large input was clipped');
const deny=process.argv.find(v=>v.startsWith('--disallowed-tools=')).split('=')[1].split(',');
for (const name of ['Read','Grep','Glob','Bash','Edit','ReadSessionContext'])
  if (!deny.includes(name)) throw new Error('native credential read capability was exposed');
console.log(JSON.stringify({type:'session.updated',payload:{modelId:'GLM-5.3',providerId:'account:bigmodel-individual-coding-plan'}}));
console.log(JSON.stringify({type:'result',response:JSON.stringify({sha256:crypto.createHash('sha256').update(prompt).digest('hex')}),usage:{inputTokens:7,outputTokens:2}}));
''');cli.chmod(0o700)
    credentials = snapshot_source({'provider': 'zcode', 'path': str(login)}, options=OPTIONS)
    with credentials.serve() as channel, credential_environment(receive_bundle(credentials.sha256, channel)) as env:
        for name, value in env.items():monkeypatch.setenv(name, value)
        system = 'literal bytes; never evaluate $(printf unsafe) or `echo unsafe`'
        messages = [{'role': 'user', 'content': '你好 α x\n' * 30000}]
        events = []
        reply = transport(cli).complete(system=system, messages=messages, model='GLM-5.3', native_event_sink=events.append)
        expected = hashlib.sha256(flatten_messages(system, messages).encode()).hexdigest()
        assert json.loads(reply.text) == {'sha256': expected}
        assert reply.usage['input_tokens'] == 7
        binding, = [event for event in events if event.get('type') == 'native.argument.binding']
        assert binding['payload']['sha256'] == expected


@pytest.mark.parametrize('version', ['0.16.9', '0.17.0'])
def test_unsupported_cli_is_refused_by_a_zero_model_version_probe_before_invocation(login, tmp_path, monkeypatch, version):
    cli = tmp_path / 'not-node'
    invoked = tmp_path / 'must-not-exist'
    cli.write_text('#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n'
                   + f"if '--version' in sys.argv: print({version!r});raise SystemExit(0)\n"
                   + f'Path({str(invoked)!r}).write_text("model invoked")\n')
    cli.chmod(0o700)
    credentials = snapshot_source({'provider': 'zcode', 'path': str(login)}, options=OPTIONS)
    with credentials.serve() as channel, credential_environment(receive_bundle(credentials.sha256, channel)) as env:
        for name, value in env.items():monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match='does not support'):
            transport(cli).complete(system='x' * 200000, messages=[], model='GLM-5.3')
    assert not invoked.exists()
