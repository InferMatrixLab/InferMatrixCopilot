"""Both arm identities traverse the real provider/archive/sandbox composition."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

# Helpers come from test code; product modules retain their selected source or
# installed package identity. Installed verification supplies only copied tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'test'))
from test_completion_profile_configuration import configured  # noqa: E402,F401
from test_readonly_sandbox import worker_receipt  # noqa: E402,F401

from codex_guard import dispatch
from infermatrix_copilot.providers.codex import CodexTransport
from infermatrix_copilot.trace_store import TraceStore
from shared_execution import validate_shared_request
from ledger import Refused


@pytest.mark.parametrize('damage', ['null', 'unplanned-case', 'unknown-arm', 'empty-arm',
                                    'missing-repo', 'extra-field', 'newline-repo'])
def test_invalid_scoped_bridge_policy_is_refused_during_case_selection(damage):
    from shared_execution import plan_for_case
    table = {'case': {'baseline': {'repo': 'demo'}}}
    if damage == 'null': table = None
    elif damage == 'unplanned-case': table['other'] = table['case']
    elif damage == 'unknown-arm': table['case']['other'] = {'repo': 'demo'}
    elif damage == 'empty-arm': table['case'] = {}
    elif damage == 'missing-repo': table['case']['baseline'] = {}
    elif damage == 'extra-field': table['case']['baseline']['shell'] = True
    else: table['case']['baseline']['repo'] = 'demo\nother'
    with pytest.raises(Refused, match='scoped bridge'):
        plan_for_case({'native_call_limits': {'case': 1}, 'scoped_bridge_by_case': table}, 'case')


@pytest.fixture
def settings(tmp_path):
    from infermatrix_copilot.config import Settings
    return Settings(_env_file=None, run_root=tmp_path / 'runs', metrics_enabled=False)


@pytest.mark.parametrize('schema', ['shared-sandbox/1', 'shared-sandbox/2', 'case-policy', 'scoped-structured'])
def test_each_arm_enters_one_canonical_native_call_and_never_uses_the_host_cli(configured, tmp_path, monkeypatch, schema):
    settings, calls = configured
    source = tmp_path / 'source';source.mkdir();(source / 'source.py').write_text('frozen = True\n')
    binary = tmp_path / 'native';binary.write_text('frozen native identity');binary.chmod(0o700)
    native_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    monkeypatch.setattr(CodexTransport, 'identity_probe', lambda self, profile: {
        'schema': 'native-identity/1', 'sha256': native_hash, 'context': profile.sandbox._prepared_context()})
    plan = {'native_call_limits': {'case': 1}, 'provider_binary_sha256': native_hash,
        'native_timeout_s': 30, 'execution_kind': 'live', 'supply_profile_defaults': True,
        'execution_profile': {'model': 'fixture', 'reasoning_effort': 'high',
                              'config_overrides': ['model_reasoning_effort="high"']},
        'shared_execution': {'schema': 'shared-sandbox/1', 'profile': settings.native_completion_profiles['codex'],
            'source_roots_by_arm': {arm: [str(source)] for arm in ('baseline', 'candidate')},
            'knowledge_by_arm': {}}}
    if schema in ('shared-sandbox/2', 'case-policy'):
        shared = plan['shared_execution'];profile = shared.pop('profile')
        profile = {'kind': 'sandbox', 'backend': profile, 'location': {'kind': 'local'}}
        shared.update(schema="shared-sandbox/2", profiles={
            'codex': {**profile, 'provider_options': {'model_mismatch_policy': 'fail'}},
            'zcode': {**profile, 'provider_options': {'model_mismatch_policy': 'fail',
                'zcode_provider_id': 'account:fixture', 'zcode_reasoning_level': 'max'}}})
        plan['provider_binary_sha256_by_provider'] = {'codex':plan.pop('provider_binary_sha256'),'zcode':'b'*64}
        plan['execution_profile']['roles'] = {'generator':{'provider':'zcode','model':'GLM-5.3','effort':''},
            'judge':{'provider':'codex','model':'fixture','effort':'high'}}
    if schema == 'case-policy':
        from copy import deepcopy
        selected = deepcopy(plan['shared_execution']['profiles'])
        selected['codex']['backend']['native_cli'] = '/opt/case-policy/codex'
        plan['native_profiles_by_case'] = {'case': selected}
    schema_path = None
    if schema == 'scoped-structured':
        from infermatrix_copilot.providers.container_execution import ContainerExecution
        monkeypatch.setattr(ContainerExecution,'supported_input_protocol',lambda self,image:6)
        knowledge = tmp_path/'knowledge';knowledge.mkdir();(knowledge/'rules.md').write_text('frozen knowledge')
        plan['shared_execution']['knowledge_by_arm'] = {arm:str(knowledge) for arm in ('baseline','candidate')}
        plan['scoped_bridge_by_case'] = {'case':{arm:{'repo':'demo'} for arm in ('baseline','candidate')}}
        schema_path = source/'schema.json';schema_path.write_text('{"type":"object"}')
        monkeypatch.setattr(CodexTransport,'bridge_probe',lambda self,profile,request:
            ({'schema':'bridge-readiness/1','tools':sorted(request.scope.allowed_tools)},profile.sandbox._prepared_context()))
    results = []
    for arm in ('baseline', 'candidate'):
        args = dict(ledger_path=tmp_path / 'calls.sqlite', plan=plan, arm=arm, case_id='case',
            binary=binary, cwd=source, env={'PATH': '/usr/bin:/bin'}, prompt='exact frozen native prompt',
            args=['exec', '--json', '--sandbox', 'read-only', '-'])
        if schema_path is not None:args['args'][-1:-1]=['--output-schema',str(schema_path)]
        result = dispatch(**args)
        assert result['native_complete']
        assert dispatch(**args) == result
        rows = list(TraceStore(result['trace_root']).iter_records(kind='model_call'))
        assert len(rows) == 1
        TraceStore(result['trace_root']).validate_native_attempt(rows[0][0])
        results.append(result)
    assert len(calls) == 2
    if schema == 'case-policy':
        assert all(json.loads(options['input'])['argv'][0] == '/opt/case-policy/codex' for _, options in calls)
    if schema == 'scoped-structured':
        for _, options in calls:
            envelope=json.loads(options['input']);assert '--output-schema' in envelope['argv']
            assert envelope['input'].endswith('exact frozen native prompt')
            bridge=json.loads(envelope['frozen_inputs']['files']['/tmp/inputs/bridge.json'])
            assert bridge['scope']['read_only'] and bridge['scope']['strict_extras']
            assert 'doc_read' in bridge['scope']['allowed_tools'] and 'write_file' not in bridge['scope']['allowed_tools']
            assert '/tmp/inputs/knowledge/rules.md' in envelope['frozen_inputs']['files']
        assert all(result['scoped_bridge_sha256'] for result in results)
    else:
        assert all(json.loads(options['input'])['input'] == 'exact frozen native prompt' for _, options in calls)
    assert results[0]['native_attempt_id'] != results[1]['native_attempt_id']
    assert all('/tmp/inputs/repository/source.py' in json.loads(options['input'])['frozen_inputs']['files']
               for _, options in calls)


def test_external_sdk_schema_permission_is_exact_and_never_allows_external_outputs(configured, tmp_path):
    settings, _ = configured
    source = tmp_path / 'source';source.mkdir()
    schema = tmp_path / 'installed-sdk-schema.json';schema.write_text('{"type":"object"}')
    plan = {'execution_profile': {'reasoning_effort': 'high'},
        'shared_execution': {'schema': 'shared-sandbox/1',
            'profile': settings.native_completion_profiles['codex'],
            'source_roots_by_arm': {'baseline': [str(source)], 'candidate': [str(source)]},
            'knowledge_by_arm': {}, 'schema_files_by_arm': {'baseline': {
                str(schema): hashlib.sha256(schema.read_bytes()).hexdigest()}}}}
    args = ['exec', '-C', str(source)]
    validate_shared_request(plan, 'baseline', args, source, schema, source / 'result.json')
    with pytest.raises(Refused):
        validate_shared_request(plan, 'candidate', args, source, schema)
    with pytest.raises(Refused):
        validate_shared_request(plan, 'baseline', args, source, schema, tmp_path / 'external-result.json')
    schema.write_text('{"type":"array"}')
    with pytest.raises(Refused):
        validate_shared_request(plan, 'baseline', args, source, schema)
