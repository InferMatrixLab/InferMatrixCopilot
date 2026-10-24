"""Installed init stages: shared composition with native generation and judgment.

Only prerequisite stages use frozen fixture material. The measured stage owns
its validation, acceptance policy, checkpoints and result tree in Copilot.
"""
import argparse
from contextlib import redirect_stdout
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

from arm_runtime import application_history_payloads, execution_profile, host_timeout, model_gateway, model_roles
from init_fixture_policies import correction_runtime, history_world, scripted_gateway


STAGES = {'foundation': 'knowledge', 'screening': 'skeleton', 'history': 'pr-history',
          'modules': 'modules', 'discovery': 'feature-discovery'}
DEPTH_MODES = {'depth-strict': 'strict', 'depth-lightweight': 'lightweight'}
STAGES.update({kind: 'knowledge-deepen' for kind in DEPTH_MODES})


QUALITY_KINDS = {'foundation': 'foundation', 'screening': 'screening', 'history': 'history',
                 'module-card': 'modules', 'discovery': 'discovery',
                 'strict-depth': 'depth-strict', 'lightweight-depth': 'depth-lightweight'}


def stage_options(kind, *, native=True, correction=False):
    """The measured workflow's options are also used by host admission."""
    options = {'dry_run': True, **({'subscription_generator': True} if native else {})}
    if kind == 'discovery' or kind in DEPTH_MODES:
        options['from_existing'] = True
    if kind in DEPTH_MODES:
        options['acceptance_mode'] = DEPTH_MODES[kind]
    if kind == 'modules' or correction:
        options['unlimited_subscription'] = True
    return options


def stage_role_error(case, configured):
    kind = QUALITY_KINDS.get(case)
    if kind is None:
        return None
    from infermatrix_copilot.sdk.v1 import knowledge_init_role_error
    return knowledge_init_role_error(STAGES[kind],
        generator_provider=configured['generator']['provider'],
        generator_model=configured['generator']['model'],
        judge_provider=configured['judge']['provider'],
        judge_model=configured['judge']['model'],
        unlimited_subscription=stage_options(kind).get('unlimited_subscription', False))


def frozen_roles(shim, model=None, effort=None, *, discovery=False):
    return model_roles(shim, model, effort, require_explicit=discovery)


def prepare_world(kind, fixture_path, validators):
    sys.path.insert(0, str(fixture_path.parent))  # Frozen test helpers only.
    fixture = runpy.run_path(str(fixture_path))
    import test_kb_init_skeleton as skeleton
    skeleton.REAL_TOOLS = validators
    fixture.setdefault('_tree', skeleton._tree)
    fixture['world'].__wrapped__.__globals__['REAL_TOOLS'] = validators
    os.environ.update(GIT_AUTHOR_DATE='2000-01-01T00:00:00Z',
                      GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')
    world = fixture['world'].__wrapped__(Path.cwd())
    if kind == 'foundation':
        world['prerequisite_gateway'] = fixture['KnowledgeGateway']()
        fixture['_chain'](world, world['prerequisite_gateway'])
    elif kind == 'modules':
        fixture['_world_with_tools'](world)
        fixture['_skeleton'](world)
        world['prerequisite_gateway'] = fixture['ImplementationGateway']()
    elif kind == 'history':
        gateway, source = history_world(fixture, world)
        world['history_source'], world['prerequisite_gateway'] = source, gateway
    elif kind in DEPTH_MODES:
        policy = fixture['_baseline'](world)
        fixture['_complete_breadth'](world, policy)
    elif kind == 'discovery':
        runtime, lifecycle, _, store = fixture['_setup'](world)
        world.update(discovery_runtime=runtime, discovery_lifecycle=lifecycle,
                     discovery_store=store)
    return fixture, world


def archived_stage_records(store):
    """Read the domain's original archive; never create a second recorder."""
    return [{'system': store.blob(row['inputs']['system']),
             'prompt': store.blob(row['inputs']['prompt']), 'archive': row}
            for row, _ in store.iter_records() if row.get('kind') == 'model_call']


def stage_verdicts(kind, record):
    if kind in DEPTH_MODES:
        return {key: {facet: result['verdict'] for facet, result in value['facets'].items()}
                for key, value in record.verdicts.items()}
    return {key: value['verdict'] for key, value in record.verdicts.items()}


def verify_fixture(kind, record, records, tree, *, native=True, packet=None):
    assert record.status in (('dry_run', 'empty') if kind in DEPTH_MODES else ('dry_run',)), (record.problems, record.unfinished)
    verdicts = stage_verdicts(kind, record)
    if kind == 'foundation':
        assert len(records) == (14 if native else 4), len(records)
        assert verdicts['knowledge:core:architecture'] == 'fail', verdicts
        assert sum(value == 'pass' for value in verdicts.values()) == (11 if native else 3), verdicts
        assert not any('Engine.step parses JSON documents and schedules them on a GPU.' in text for text in tree.values())
    elif kind == 'modules':
        assert len(records) == (4 if native else 2), len(records)
        assert verdicts == {'module:tools/fmt/': 'pass', 'module:tools/lint/': 'fail'}, verdicts
        assert 'knowledge/repos/toy/components/tooling/tools-fmt.md' in tree
        assert 'knowledge/repos/toy/components/tooling/tools-lint.md' not in tree
        assert not any('Linter.run parses JSON and schedules GPU execution.' in text for text in tree.values())
        assert 'tools/lint/' in record.coverage['unrouted']
    elif kind == 'history':
        assert len(records) == (4 if native else 1), len(records)
        assert packet['head_sha'] == record.pr['head_sha']
        assert packet['base_sha'] == record.review['base_sha']
        assert len(packet['commits']) == 2 and packet['baseline_rule_pages'] and packet['owner_routes']
        assert {row['owner'] for row in packet['owner_routes']} >= {'core', 'tooling'}
        assert 'components/tooling/rules.md' in packet['complete_diff']
        assert 'repos/toy/rules.md' in packet['complete_diff']
        assert 'Keep the engine step return contract' in packet['complete_diff']
        assert 'Keep the formatter return contract' in packet['complete_diff']
    elif kind in DEPTH_MODES:
        depth = record.coverage['semantic_depth']
        assert depth['complete_features'] == 0, depth
        expected = 4 if DEPTH_MODES[kind] == 'lightweight' else 0
        assert depth['covered_facets'] == expected, depth
        assert len(records) == (8 if expected else 3), len(records)
        missing = set(depth['features']['step0']['missing_facets'])
        assert missing == ({'configuration', 'tradeoffs', 'validation'} if expected else {
            'api', 'flow', 'configuration', 'dependencies', 'failure_modes', 'tradeoffs', 'validation'}), missing
        assert record.unfinished
    elif kind == 'discovery':
        from infermatrix_copilot.kb_service.init_feature_discovery import SYSTEM_DISCOVER, SYSTEM_REVIEW
        assert record.discovery['done'] is True, record.discovery
        assert record.discovery['catalog_boundary_audit']['done'] is True
        assert record.discovery['catalog_consolidation_audit']['done'] is True
        assert any('engine-step' in text for path, text in tree.items()
                   if path.endswith('knowledge-coverage.yaml'))
        assert {row['system'] for row in records} >= {SYSTEM_DISCOVER, SYSTEM_REVIEW}
    else:
        assert len(records) == (6 if native else 3), len(records)
        assert verdicts == {'TOY-I1': 'pass', 'TOY-I2': 'fail',
                            'TOY-I3': 'unsure', 'TOY-I4': 'unjudged'}, verdicts
        from infermatrix_copilot.knowledge_service.lifecycle import Page
        kept = {rule.rule_id for text in tree.values() if text.startswith('---')
                for rule in Page.parse(text).rules()}
        assert kept == {'TOY-I1', 'TOY-I3', 'TOY-I4'}, kept


def legacy_history_gateway(fixture, world, native):
    """Preserve the old scripted-extraction fixture as an explicit adapter policy."""
    gateway = world['prerequisite_gateway']
    def transform(system, prompt, reply, kwargs):
        if system == fixture['SYSTEM_HISTORY'] and fixture['_payload'](prompt)['pr']['number'] == 102:
            data = {'rules': [{'owner': 'tooling', 'title': 'Keep the formatter return contract',
                'trigger': 'a change edits `tools/fmt/z.py`',
                'must': "retain the formatter's constant return value of 4 when adding instrumentation",
                'forbid': 'do not mutate worker state while computing the result',
                'acceptance': 'a focused test proves fmt() returns 4 across repeated calls',
                'evidence': [{'path': 'tools/fmt/z.py', 'start': 1, 'end': 2}]}], 'not_adopted': []}
            if kwargs.get('validate'):
                kwargs['validate'](data)
            return replace(reply, data=data, text=json.dumps(data))
        return reply
    return scripted_gateway(gateway, native, fixture['SYSTEM_REVIEW'], transform=transform)


def legacy_modules_gateway(fixture, world, native):
    """Keep scripted zcode generation explicit; only judgments are native."""
    from infermatrix_copilot.kb_service.gate import JUDGE_SYSTEM
    gateway = world['prerequisite_gateway']
    def transform(system, prompt, reply, kwargs):
        if system == fixture['SYSTEM_CARD_IMPL']:
            payload = json.loads(prompt.split('\n', 1)[1].rsplit('</untrusted_data>', 1)[0])
            data = json.loads(json.dumps(reply.data))
            data['purpose'] = ('Linter.run parses JSON and schedules GPU execution.'
                if payload['module'] == 'tools/lint/' else 'The shown fmt function returns the constant 3.')
            data['entry_points'] = [{'path': payload['files'][0]['path'], 'what': 'shown entry'}]
            data['docs'] = []
            return replace(reply, data=data, text=json.dumps(data))
        return reply
    return scripted_gateway(gateway, native, JUDGE_SYSTEM, transform=transform)


def legacy_foundation_gateway(fixture, world, native):
    from infermatrix_copilot.kb_service.gate import JUDGE_SYSTEM
    def transform(system, prompt, reply, kwargs):
        if system in (fixture['SYSTEM_KNOWLEDGE'], fixture['SYSTEM_KNOWLEDGE_V4']):
            payload = json.loads(prompt.split('\n', 1)[1].rsplit('</untrusted_data>', 1)[0])
            data = json.loads(json.dumps(reply.data))
            for section in data['sections']:
                if section['facet'] == 'architecture':
                    section['body'] = ('Engine.step parses JSON documents and schedules them on a GPU.'
                        if payload['owner'] == 'core' else 'The shown tools/fmt/z.py::fmt function returns the constant 3.')
                if section['facet'] == 'tradeoffs':
                    section['body'] = 'The shown constant return avoids reading mutable state, but cannot adapt its result to changes in runtime state.'
            return replace(reply, data=data, text=json.dumps(data))
        return reply
    return scripted_gateway(world['prerequisite_gateway'], native, JUDGE_SYSTEM, transform=transform)


def legacy_screening_gateway(fixture, native, *, capture, unavailable):
    from infermatrix_copilot.kb_service.gate import JUDGE_SYSTEM
    from infermatrix_copilot.kb_service.models import ModelUnavailable
    rules = [fixture['DOC_RULE'], {**fixture['JUDGED_BAD_RULE'],
             'title': 'Workers may skip ticks',
             'body': '- 允许：a worker may skip `pkg/core.py` steps when it is busy.'},
             {'title': 'Util tests preserve all runtime cases', 'body':
              '- 触发：a PR edits `pkg/util.py`.\n- 强制：reviewers require tests covering every runtime input case.',
              'evidence': [{'path': 'CONTRIBUTING.md', 'start': 2, 'end': 2}]},
             {'title': 'Worker tick contract', 'body':
              '- 触发：a PR edits `pkg/core.py`.\n- 强制：reviewers check that workers call step exactly once per tick.',
              'evidence': [{'path': 'docs/guide.md', 'start': 2, 'end': 2}]}]
    def before_judge(system, prompt):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('</untrusted_data>', 1)[0])
        if not capture and payload['change']['rule_id'] == 'TOY-I4':
            unavailable.append({'system': system, 'prompt': prompt})
            raise ModelUnavailable('offline injected unavailability before provider dispatch')
    return scripted_gateway(fixture['FakeGateway'](doc_rules=rules, seed_rules=False),
                            native, JUDGE_SYSTEM, before_judge=before_judge)


def execute(kind, args, *, native=True):
    if kind not in STAGES:
        raise ValueError('unsupported measured init stage')
    if kind in DEPTH_MODES and not native:
        from ledger import Refused
        raise Refused('full depth stage requires native generation and judgment')
    correction = getattr(args, 'correction', False)
    if correction and (kind != 'foundation' or native):
        from ledger import Refused
        raise Refused('scripted correction policy requires legacy foundation')
    generator, judge = frozen_roles(args.shim, args.model, args.effort, discovery=kind == 'discovery')
    fixture, world = prepare_world(kind, args.fixture_path, args.validators)
    settings = SimpleNamespace(strict_backend_cli=args.shim, strict_backend_model=execution_profile(args.shim)['model'],
                               strict_backend_timeout_s=host_timeout(args.shim))
    records, packets, generations, unavailable = [], [], [], []
    recorder = records.append
    if kind == 'discovery':
        from infermatrix_copilot.kb_service.runtime import trace_recorder
        recorder = trace_recorder(world['discovery_store'])
    gateway = None if correction else model_gateway(settings, shim=args.shim, recorder=recorder)
    retention = getattr(gateway, 'evaluation_payload_retention', None)
    if kind == 'history' and native and retention is None:
        from ledger import Refused
        raise Refused('native history comparison requires explicit frozen evaluation retention')
    if correction:
        runtime = correction_runtime(args, fixture, world, settings, packets, generations, judge)
    elif kind == 'foundation' and not native:
        runtime = fixture['_runtime'](world, legacy_foundation_gateway(fixture, world, gateway), judge=judge)
    elif kind == 'screening' and not native:
        runtime = fixture['_runtime'](world, legacy_screening_gateway(fixture, gateway,
            capture=args.capture, unavailable=unavailable), judge=judge)
    elif kind == 'modules' and not native:
        from infermatrix_copilot.kb_service.models import ModelRole
        gateway = legacy_modules_gateway(fixture, world, gateway)
        runtime = fixture['_runtime'](world, gateway,
            generator=ModelRole('generator', 'zcode', 'GLM-5.3'), judge=judge)
    elif kind == 'history' and not native:
        gateway = legacy_history_gateway(fixture, world, gateway)
        runtime = fixture['_runtime'](world, gateway, judge=judge, github=world['history_source'])
    elif kind in DEPTH_MODES:
        runtime = fixture['_runtime'](world, gateway, generator=generator, judge=judge,
                                      state_dir=world['tmp'] / 'depth')
    elif kind == 'discovery':
        runtime = replace(world['discovery_runtime'], gateway=gateway,
                          generator=generator, judge=judge, discovery_concurrency=1,
                          environ={**world['discovery_runtime'].environ,
                                   'KB_DISCOVERY_GENERATOR': generator.label(),
                                   'KB_DISCOVERY_JUDGE': judge.label(),
                                   'KB_DISCOVERY_CONCURRENCY': '1'})
    else:
        runtime = fixture['_runtime'](world, gateway, generator=generator, judge=judge,
            **({'github': world['history_source']} if kind == 'history' else {}))
    if kind == 'history':
        from infermatrix_copilot.kb_service.init_history import REVIEWER_ENV
        runtime = replace(runtime, environ={**runtime.environ, REVIEWER_ENV: judge.label()})
        payloads_env = application_history_payloads()
        if native and payloads_env is not None:
            # The frozen host policy was validated above; the application owns
            # the recording decision, batch identity and durable state binding.
            runtime = replace(runtime, environ={**runtime.environ, payloads_env: 'retain-private'})
    lifecycle = (fixture['_lifecycle'](seeds=()) if kind == 'screening'
                 else world['discovery_lifecycle'] if kind == 'discovery'
                 else fixture['_modules_lifecycle']())
    options = stage_options(kind, native=native, correction=correction)
    record = fixture['run_stage'](runtime, lifecycle, STAGES[kind], **options)
    if kind == 'discovery':
        records[:] = archived_stage_records(world['discovery_store'])
    first = len(packets if correction else records)
    first_data = json.loads(json.dumps(asdict(record))) if correction else None
    if correction:
        (Path.cwd() / 'foundation-first.json').write_text(json.dumps(first_data, ensure_ascii=False, indent=2))
    if correction or record.status == 'dry_run' or kind in DEPTH_MODES and record.status == 'empty':
        recovered = fixture['run_stage'](runtime, lifecycle, STAGES[kind], **options)
        if kind == 'discovery':
            records[:] = archived_stage_records(world['discovery_store'])
        if correction:
            old_tasks = first_data['coverage'].get('foundation_jobs', {}).get('tasks', {})
            new_tasks = recovered.coverage.get('foundation_jobs', {}).get('tasks', {})
            assert all(json.loads(json.dumps(new_tasks.get(key))) == task for key, task in old_tasks.items()), 'accepted checkpoint changed'
            record = recovered
        else:
            assert len(records) == first, 'completed stage dispatched on resume'
            if record.status == 'dry_run':
                assert fixture['_tree'](recovered) == fixture['_tree'](record)
            else:
                assert recovered.status == 'empty' and not recovered.pr.get('dry_run_dir')
    if not correction:
        packets = [{'system': row['system'], 'prompt': row['prompt']} for row in records]
    tree = fixture['_tree'](record) if record.status == 'dry_run' else {}
    verdicts = stage_verdicts(kind, record)
    packet = None
    if kind == 'history':
        aggregate = [row for row in records if row['system'] == fixture['SYSTEM_REVIEW']]
        if aggregate:
            # The old SDK may append output instructions after the data fence.
            # Its compatibility host delegates presentation decoding to Copilot.
            try:
                from infermatrix_copilot.structured_output import decode_json_object
            except ImportError:
                from arm_runtime import load_arm_config
                from legacy_provider_host import LegacyTransport
                config_path, _ = load_arm_config(args.shim)
                packet = LegacyTransport(config_path, judge.provider).invoke(
                    'decode_object', {'text': aggregate[0]['prompt']})['object']
            else:
                packet = decode_json_object(aggregate[0]['prompt'])
        else:
            packet = None
    result = {'schema': 'init-stage-artifacts/2', 'packets': packets,
              'tree': tree, 'verdicts': verdicts, 'records': records, 'record': asdict(record),
              'coverage': record.coverage,
              'generation_provenance': ('native provider; no scripted measured-stage gateway' if native
                                        else 'scripted extraction characterization; not model quality')}
    if kind == 'history':
        result.update(packet=packet, review=record.review, payload_retention=retention)
    if kind == 'discovery':
        result['discovery'] = record.discovery
        result['publication_tree'] = {path: text for path, text in tree.items()
                                      if not path.startswith('eval/feature-discovery/')}
    if kind == 'screening' and not native:
        result.update(unavailable=unavailable, source=fixture['UPSTREAM'])
        if not args.capture:
            assert len(unavailable) == 1
    if correction:
        result.update(first=first_data, second=asdict(record), scripted_generations=generations,
            first_judge_attempts=first, correction_judge_attempts=len(packets) - first,
            retained_tasks=len(old_tasks), subscription_auth_simulated=args.simulate_subscription_auth)
        if not args.capture:
            assert first == 12 and len(packets) == 13, result
            assert len(generations) == 3 and generations[-1]['facets'] == ['architecture'], generations
            assert generations[-1].get('prior_review_feedback'), generations[-1]
            assert record.status == 'dry_run' and not record.unfinished, record.problems
    (Path.cwd() / (kind + '.json')).write_text(json.dumps(result, indent=2))
    if not args.capture and not correction:
        verify_fixture(kind, record, records, tree, native=native, packet=packet)
    import infermatrix_copilot
    report = {'schema': 'init-stage-report/2',
            'package_file': infermatrix_copilot.__file__, 'state': record.status,
            'gateway_attempts': len(packets),
            'gateway_attempts_scope': ('recorded judge gateway attempts' if correction else 'recorded generator and judge gateway attempts')
                + ', including pre-dispatch failures; native dispatch totals belong to the call ledger',
            'resumed_without_dispatch': (record.status == 'dry_run' or kind in DEPTH_MODES and record.status == 'empty') and not correction,
            'adjudication': 'pending',
            'scope': (f'full {"unlimited-subscription" if kind == "modules" else "capped"} {kind}: native generation, validation, judgment and tree; no publication'
                      if native else f'{kind} scripted generation characterization; native judgments; no publication'),
            'prerequisite_provenance': ('frozen scripted skeleton/modules; excluded from measured stage'
                                        if kind == 'foundation' else 'frozen scripted prerequisites; excluded' if kind in ('history', 'modules') or kind in DEPTH_MODES else 'no prerequisite stage')}
    if kind in DEPTH_MODES:
        report.update(acceptance_mode=DEPTH_MODES[kind], depth=record.coverage['semantic_depth'])
    if kind == 'history':
        report.update(reviewed_head_sha=record.review.get('head_sha'),
                      payload_retention=retention, recovered_without_dispatch=record.status == 'dry_run')
    if kind == 'modules' or kind == 'foundation' and not native and not correction:
        report.update(accepted=sum(value == 'pass' for value in verdicts.values()),
                      rejected=sum(value == 'fail' for value in verdicts.values()),
                      verdicts=verdicts)
    if kind == 'screening' and not native:
        from infermatrix_copilot.knowledge_service.lifecycle import Page
        report.update(kept=sorted({rule.rule_id for text in tree.values() if text.startswith('---')
            for rule in Page.parse(text).rules()}), verdicts=verdicts)
    if correction:
        report.update(first_judge_attempts=first, correction_judge_attempts=len(packets) - first,
                      retained_tasks=len(old_tasks), recovery='one correction resume; accepted checkpoints unchanged')
    return report


def main(kind, *, native=True):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('shim')
    parser.add_argument('fixture_path', type=Path)
    parser.add_argument('validators', type=Path)
    parser.add_argument('--timeout', type=float)  # Old coordinator syntax; frozen plan is authoritative.
    parser.add_argument('--model')
    parser.add_argument('--effort')
    parser.add_argument('--capture', action='store_true')
    parser.set_defaults(correction=False, simulate_subscription_auth=False, subscription_native=None)
    if kind == 'foundation' and not native:
        parser.add_argument('--correction', action='store_true')
        authentication = parser.add_mutually_exclusive_group()
        authentication.add_argument('--simulate-subscription-auth', action='store_true',
            help='Offline fixture only; explicitly simulate the subscription login proof')
        authentication.add_argument('--subscription-native', type=Path,
            help='Manifest-bound native binary for the original subscription preflight policy')
    args = parser.parse_args()
    with redirect_stdout(sys.stderr):
        report = execute(kind, args, native=native)
    print(json.dumps(report))
