"""Native stage composition must refuse role drift before preparation or calls."""
from pathlib import Path
from types import SimpleNamespace
import json
import pytest

from init_native_arm import execute


@pytest.mark.parametrize('kind,native', [('foundation', True), ('screening', True),
                                       ('history', True), ('discovery', True), ('depth-strict', True), ('depth-lightweight', True), ('modules', True), ('modules', False),
                                       ('foundation', False), ('screening', False)])
@pytest.mark.parametrize('drift', ['model', 'effort'])
def test_stage_overrides_cannot_replace_frozen_native_identity(tmp_path, monkeypatch, kind, native, drift):
    shim = tmp_path / 'codex'
    shim.write_text('unused')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'plan': {'execution_profile': {
        'model': 'frozen-model', 'reasoning_effort': 'high'}}}))
    monkeypatch.setenv('SHADOW_ARM_CONFIG', str(config))
    monkeypatch.setattr('init_native_arm.prepare_world', lambda *a: pytest.fail('drift prepared repositories'))
    monkeypatch.setattr('init_native_arm.model_gateway', lambda *a, **kw: pytest.fail('drift constructed a model gateway'))
    args = SimpleNamespace(shim=str(shim), model='changed' if drift == 'model' else None,
                           effort='low' if drift == 'effort' else None)
    with pytest.raises(ValueError, match='frozen model/effort'):
        execute(kind, args, native=native)
    assert set(p.name for p in tmp_path.iterdir()) == {'codex', 'config.json'}


def test_unknown_measured_stage_cannot_enter_workflow_composition(monkeypatch):
    monkeypatch.setattr('init_native_arm.frozen_roles', lambda *a: pytest.fail('unknown stage selected model roles'))
    with pytest.raises(ValueError, match='unsupported measured init stage'):
        execute('not-a-stage', SimpleNamespace())


def test_full_discovery_stage_uses_original_domain_archive_and_resumes(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service.models import ModelGateway
    import test_kb_feature_discovery_stage as fixture
    fixture_path = Path(fixture.__file__).resolve()
    monkeypatch.chdir(tmp_path)
    shim = tmp_path / 'codex'
    shim.write_text('not dispatched')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'plan': {'execution_profile': {
        'model': 'offline-model', 'reasoning_effort': 'high', 'roles': {
            'generator': {'provider': 'codex', 'model': 'fixture-generator', 'effort': 'high'},
            'judge': {'provider': 'codex', 'model': 'offline-model', 'effort': 'high'}}}, 'host_timeout_s': 180}}))
    monkeypatch.setenv('SHADOW_ARM_CONFIG', str(config))
    script = fixture.NativeScript()
    monkeypatch.setattr('init_native_arm.model_gateway', lambda settings, **kwargs:
        ModelGateway(settings, transport_factory=lambda _: script, recorder=kwargs['recorder']))
    report = execute('discovery', SimpleNamespace(shim=str(shim), model=None, effort=None,
        capture=False, fixture_path=fixture_path,
        validators=fixture_path.parent.parent / 'knowledge/tools'))
    data = json.loads((tmp_path / 'discovery.json').read_bytes())
    assert report['state'] == 'dry_run' and report['resumed_without_dispatch']
    assert len(data['records']) == len(script.calls) > 2
    assert {row['model'] for row in script.calls if row['role'] == 'generator'} == {'fixture-generator'}
    assert {row['model'] for row in script.calls if row['role'] == 'judge'} == {'offline-model'}
    assert data['discovery']['done'] is True
    assert all(row['archive']['kind'] == 'model_call' for row in data['records'])
    assert any(path.endswith('knowledge-coverage.yaml') for path in data['publication_tree'])


def test_native_modules_delegates_subscription_policy_to_the_original_domain(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service.models import ModelGateway
    from infermatrix_copilot.kb_service.init_support import InitError
    import test_kb_module_source_evidence as fixture
    fixture_path = Path(fixture.__file__).resolve()
    monkeypatch.chdir(tmp_path)
    shim = tmp_path / 'codex';shim.write_text('not dispatched')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'plan': {'execution_profile': {
        'model': 'frozen-model', 'reasoning_effort': 'high'}, 'host_timeout_s': 180}}))
    monkeypatch.setenv('SHADOW_ARM_CONFIG', str(config))
    monkeypatch.setattr('init_native_arm.model_gateway', lambda *a, **kw:
        ModelGateway(None, transport_factory=lambda _: pytest.fail('invalid subscription policy dispatched a model')))
    with pytest.raises(InitError, match='Zcode GLM-5.3 extraction and an independent Codex judge'):
        execute('modules', SimpleNamespace(shim=str(shim), model=None, effort=None,
            capture=False, fixture_path=fixture_path,
            validators=fixture_path.parent.parent / 'knowledge/tools'))


@pytest.mark.parametrize('kind,native', [('foundation', True), ('screening', False), ('history', True)])
def test_scripted_correction_cannot_change_another_execution_policy(monkeypatch, kind, native):
    from ledger import Refused
    monkeypatch.setattr('init_native_arm.frozen_roles', lambda *a: pytest.fail('invalid correction selected roles'))
    with pytest.raises(Refused, match='legacy foundation'):
        execute(kind, SimpleNamespace(correction=True), native=native)


def test_scripted_routing_keeps_native_calls_and_pre_dispatch_failure_separate():
    from init_fixture_policies import scripted_gateway
    scripted, native = [], []
    fixture = SimpleNamespace(call_json=lambda role, **kwargs: scripted.append((role, kwargs)) or {'scripted': True})
    provider = SimpleNamespace(call_json=lambda role, **kwargs: native.append((role, kwargs)) or {'native': True})
    unavailable = []
    def before(system, prompt):
        if prompt == 'unavailable':
            unavailable.append(prompt)
            raise RuntimeError('injected before dispatch')
    adapter = scripted_gateway(fixture, provider, 'judge', before_judge=before,
        transform=lambda system, prompt, reply, kwargs: {**reply, 'transformed': True})
    assert adapter.call_json('generator', system='generate', prompt='input') == {'scripted': True, 'transformed': True}
    assert adapter.call_json('judge', system='judge', prompt='input', record_payload=False) == {'native': True}
    with pytest.raises(RuntimeError, match='before dispatch'):
        adapter.call_json('judge', system='judge', prompt='unavailable')
    assert len(scripted) == len(native) == len(unavailable) == 1
    assert native[0][1]['record_payload'] is False



@pytest.mark.parametrize('kind', ['depth-strict', 'depth-lightweight'])
def test_full_depth_requires_native_generation_before_preparing_world(monkeypatch, kind):
    from ledger import Refused
    monkeypatch.setattr('init_native_arm.prepare_world', lambda *a: pytest.fail('non-native depth prepared repositories'))
    with pytest.raises(Refused, match='requires native generation'):
        execute(kind, SimpleNamespace(), native=False)


@pytest.mark.parametrize('mode', ['strict', 'lightweight'])
def test_full_depth_stage_preserves_acceptance_and_resumes_without_dispatch(tmp_path, monkeypatch, mode):
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
    from infermatrix_copilot.llm import Block, Reply
    import test_kb_knowledge_depth as fixture
    fixture_path = Path(fixture.__file__).resolve()
    monkeypatch.chdir(tmp_path)
    shim = tmp_path / 'codex'; shim.write_text('not a host native executable')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'plan': {'execution_profile': {
        'model': 'offline-judge', 'reasoning_effort': 'high', 'roles': {
            'generator': {'provider': 'zcode', 'model': 'GLM-5.3', 'effort': ''},
            'judge': {'provider': 'codex', 'model': 'offline-judge', 'effort': 'high'}}}, 'host_timeout_s': 180}}))
    monkeypatch.setenv('SHADOW_ARM_CONFIG', str(config))
    calls = []
    responses = fixture.DepthGateway()
    from infermatrix_copilot.kb_service.depth_inputs import SYSTEM_DEPTH_LIGHTWEIGHT
    class Native:
        subscription_billing = True
        supports_native_events = True
        def complete(self, *, system, messages, model, role, effort='', **kwargs):
            calls.append((role, system))
            response = responses.call_json(ModelRole(role, 'zcode' if role == 'generator' else 'codex', model, effort),
                                          system=fixture.SYSTEM_DEPTH if system == SYSTEM_DEPTH_LIGHTWEIGHT else system,
                                          prompt=messages[0]['content'])
            data = response.data
            if system == SYSTEM_DEPTH_LIGHTWEIGHT:
                for section in data['sections']:
                    if section['facet'] == 'validation': section['validation_kind'] = 'automated_runtime'
            if system == fixture.SYSTEM_DEPTH_REVIEW:
                data['facets'].pop('validation', None)
                for facet, verdict in data['facets'].items():
                    if facet in ('configuration', 'tradeoffs'):
                        verdict['dimensions']['faithful'] = 'no' if facet == 'configuration' else 'unsure'
            return Reply(blocks=[Block(type='text', text=json.dumps(data))], stop_reason='end_turn',
                         usage={'input_tokens': 3, 'output_tokens': 2}, model=model)
    monkeypatch.setattr('init_native_arm.model_gateway', lambda settings, **kwargs:
        ModelGateway(None, transport_factory=lambda provider: Native(), recorder=kwargs['recorder']))
    report = execute('depth-' + mode, SimpleNamespace(shim=str(shim), model=None, effort=None, capture=False,
        fixture_path=fixture_path, validators=fixture_path.parent.parent / 'knowledge/tools'))
    assert report['state'] == ('dry_run' if mode == 'lightweight' else 'empty') and report['resumed_without_dispatch']
    assert report['depth']['covered_facets'] == (4 if mode == 'lightweight' else 0)
    assert len(calls) == report['gateway_attempts']
    assert {role for role, _ in calls} == {'generator', 'judge'}


def test_stage_report_counts_a_refused_gateway_attempt_without_claiming_a_native_dispatch(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
    from infermatrix_copilot.llm import Block, Reply
    import test_kb_init_skeleton as fixture
    fixture_path = Path(fixture.__file__).resolve()
    monkeypatch.chdir(tmp_path)
    shim = tmp_path / 'codex'; shim.write_text('must not execute')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'plan': {'execution_profile': {
        'model': 'offline-model', 'reasoning_effort': 'high'}, 'host_timeout_s': 180}}))
    monkeypatch.setenv('SHADOW_ARM_CONFIG', str(config))
    responses = fixture.FakeGateway(doc_rules=[fixture.DOC_RULE, fixture.DOC_RULE2], seed_rules=False)
    sent = []

    class Native:
        subscription_billing = True
        supports_native_events = True

        def validate_completion(self):
            if len(sent) == 3:
                raise RuntimeError('known refusal before the next native dispatch')

        def complete(self, *, system, messages, model, role, effort='', **kwargs):
            sent.append(role)
            reply = responses.call_json(ModelRole(role, 'codex', model, effort),
                                        system=system, prompt=messages[0]['content'])
            return Reply(blocks=[Block(type='text', text=reply.text)],
                         stop_reason='end_turn', model=model,
                         usage={'input_tokens': 3, 'output_tokens': 2})

    monkeypatch.setattr('init_native_arm.model_gateway', lambda settings, **kwargs:
        ModelGateway(None, transport_factory=lambda _: Native(), recorder=kwargs['recorder']))
    report = execute('screening', SimpleNamespace(shim=str(shim), model=None, effort=None,
        capture=True, fixture_path=fixture_path,
        validators=fixture_path.parent.parent / 'knowledge/tools'))
    data = json.loads((tmp_path / 'screening.json').read_text())
    assert report['state'] == 'dry_run' and report['resumed_without_dispatch']
    assert sent == ['generator', 'generator', 'judge']
    assert report['schema'] == 'init-stage-report/2' and report['gateway_attempts'] == 4
    assert 'pre-dispatch failures' in report['gateway_attempts_scope']
    assert 'native_calls' not in report
    assert len(data['records']) == 4 and data['records'][-1]['error']
    assert sorted(data['verdicts'].values()) == ['pass', 'unjudged']
