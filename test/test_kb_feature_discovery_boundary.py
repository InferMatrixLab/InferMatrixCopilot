"""Catalog admission requires an independent audit against all formal IDs."""
from copy import deepcopy
import json

import pytest

from infermatrix_copilot.kb_service.init_budget import BudgetExhausted
from infermatrix_copilot.kb_service.feature_discovery_index import build_discovery_index
from infermatrix_copilot.kb_service.init_feature_discovery import (
    DiscoveryEngine, SYSTEM_REVIEW, _hash, _boundary_ready,
)
from infermatrix_copilot.kb_service.models import ModelUnavailable
from infermatrix_copilot.llm import Block, Reply

from test_kb_feature_discovery import PIN, candidate, index, reply
from test_kb_feature_discovery_stage import NativeScript, _setup, _run, _report, _policy, _save_legacy_record
from test_kb_init_skeleton import world  # noqa: F401


def approved(row, relation='new', related_id=''):
    return {'supported': 'yes', 'relation': relation, 'related_id': related_id,
            'reason': 'Pinned implementation supports the independently classified contract.',
            'candidate_sha256': _hash(row), 'judge_receipt': {'trace_id': 'primary'}}


def engine(tmp_path, rows, call, seeds=(), concurrency=13):
    idx = index(tmp_path)
    for row in rows:
        row['generator_receipts'] = [{'trace_id': 'generator'}]
    state = {'candidates': {r['id']: r for r in rows},
             'reviews': {r['id']: approved(r) for r in rows},
             'tasks': {'source:preserved': {'status': 'complete', 'receipt': 'preserved'}},
             'identity': 'original-pinned-scan'}
    return DiscoveryEngine(idx, seeds=list(seeds), owners=['runtime'], state=state, call=call,
                           save=lambda: None, concurrency=concurrency, repository='o/library', pin=PIN)


def decisions(prompt, classify=None):
    payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0])
    results = {}
    for row in payload['candidates']:
        relation, target = classify(row) if classify else ('new', '')
        results[row['id']] = {'supported': 'yes', 'relation': relation, 'related_id': target,
                             'reason': 'Independent boundary classification with the complete formal catalog.'}
    return payload, {'decisions': results}


def test_all_formal_features_remain_visible_despite_large_proposal_competition(tmp_path):
    seeds = [{'id': f'formal-{n}', 'title': 'Formal ' + str(n) + ' ' + 'behavior ' * 80,
              'owner': 'runtime', 'source_globs': ['src/session.py']} for n in range(79)]
    captures = []
    def call(role, system, prompt, validate):
        assert role == 'judge' and system == SYSTEM_REVIEW
        payload, data = decisions(prompt, lambda r: ('subcapability', 'formal-78'))
        captures.append(payload)
        assert [r['id'] for r in payload['baseline'] if r['catalog_status'] == 'existing_feature'] == [s['id'] for s in seeds]
        return reply(role, data)
    e = engine(tmp_path, [candidate(key='microfeature')], call, seeds)
    original = deepcopy(e.state['reviews'])
    assert e.audit_catalog_boundaries()
    features, outcomes = e.catalog('repos/library', require_boundary=True)
    assert len(features) == 79
    assert outcomes[0]['status'] == 'accepted' and outcomes[0]['relation'] == 'subcapability'
    assert outcomes[0]['related_id'] == 'formal-78'
    assert e.state['reviews'] == original
    assert outcomes[0]['primary_judge_receipt'] == original['microfeature']['judge_receipt']
    assert len(captures) == 1


def test_missing_audit_cannot_promote_a_primary_yes(tmp_path):
    e = engine(tmp_path, [candidate()], None)
    assert not e.catalog('repos/library', require_boundary=True)[0]
    assert e.catalog('repos/library', require_boundary=True)[1][0]['status'] == 'unknown'


def test_test_and_document_only_candidates_are_not_boundary_repaired(tmp_path):
    calls = []
    e = engine(tmp_path, [candidate(path='docs/session.md')], lambda *args: calls.append(args))
    assert e.audit_catalog_boundaries()
    assert e.state['catalog_boundary_audit']['candidate_ids'] == []
    assert not calls and not e.catalog('repos/library', require_boundary=True)[0]


@pytest.mark.parametrize('failure', ['rejected', 'unavailable'])
def test_boundary_failure_is_unknown_without_generator_repairs_or_resampling(tmp_path, failure):
    calls = []
    def call(role, system, prompt, validate):
        calls.append(role)
        if failure == 'unavailable':
            raise ModelUnavailable('native channel unavailable')
        _, data = decisions(prompt)
        for row in data['decisions'].values():
            row.update(supported='no', relation='unknown', reason='Boundary overlaps an existing capability.')
        return reply(role, data)
    e = engine(tmp_path, [candidate()], call)
    original = deepcopy(e.state['reviews'])
    assert e.audit_catalog_boundaries() and e.review() and e.audit_catalog_boundaries()
    assert calls == ['judge']
    assert e.state['reviews'] == original
    assert not e.catalog('repos/library', require_boundary=True)[0]


def test_budget_resume_reuses_scan_primary_and_successful_boundary_batches(tmp_path):
    calls = []
    def call(role, system, prompt, validate):
        payload, data = decisions(prompt)
        calls.append([r['id'] for r in payload['candidates']])
        if len(calls) == 2:
            raise BudgetExhausted('boundary interrupted')
        return reply(role, data)
    e = engine(tmp_path, [candidate(key=f'feature-{n:02d}', title=f'Capability {n}') for n in range(13)], call, concurrency=1)
    original_tasks, original_reviews = deepcopy(e.state['tasks']), deepcopy(e.state['reviews'])
    assert not e.audit_catalog_boundaries()
    assert len(e.state['boundary_reviews']) == 12
    assert e.audit_catalog_boundaries()
    assert len(calls) == 3 and calls[1] == calls[2]
    assert len(e.catalog('repos/library', require_boundary=True)[0]) == 13
    assert e.state['tasks'] == original_tasks and e.state['reviews'] == original_reviews


def test_changed_candidate_set_invalidates_only_boundary_audit(tmp_path):
    calls = []
    def call(role, system, prompt, validate):
        calls.append(role); _, data = decisions(prompt)
        return reply(role, data)
    e = engine(tmp_path, [candidate()], call)
    assert e.audit_catalog_boundaries()
    previous = deepcopy(e.state['catalog_boundary_audit'])
    e.state['candidates']['session']['description'] += ' With a newly reviewed explicit close contract.'
    e.state['reviews']['session'] = approved(e.state['candidates']['session'])
    assert not _boundary_ready(e.seeds, e.state, repository=e.repository, pin=e.pin)
    assert e.audit_catalog_boundaries()
    assert calls == ['judge', 'judge']
    assert e.state['boundary_history'][0]['audit'] == previous
    assert e.state['tasks']['source:preserved']['status'] == 'complete'


def test_unresolved_same_title_new_candidates_stay_unknown(tmp_path):
    def call(role, system, prompt, validate):
        _, data = decisions(prompt); return reply(role, data)
    e = engine(tmp_path, [candidate(key='first'), candidate(key='second')], call)
    assert e.audit_catalog_boundaries()
    features, outcomes = e.catalog('repos/library', require_boundary=True)
    assert not features
    assert all(r['status'] == 'unknown' and 'duplicate' in r['reason'] for r in outcomes)


@pytest.mark.parametrize('relation', ['implementation_supplement', 'alias', 'subcapability', 'shared_component'])
def test_aliases_can_resolve_to_an_independently_accepted_new_parent(tmp_path, relation):
    def call(role, system, prompt, validate):
        mapping = {'parent': ('new', ''), 'alias-one': (relation, 'parent'), 'alias-two': ('alias', 'alias-one')}
        _, data = decisions(prompt, lambda r: mapping[r['id']]); return reply(role, data)
    e = engine(tmp_path, [candidate(key=k, title=k) for k in ('parent', 'alias-one', 'alias-two')], call)
    for key in ('alias-one', 'alias-two'):
        path = f'src/{key}.py'
        (tmp_path / path).write_text('def helper(identity):\n    return identity\n')
        row = e.state['candidates'][key]
        row['evidence'] = [{'path': path, 'start': 1, 'end': 2},
                           {'path': 'docs/session.md', 'start': 1, 'end': 2}]
        e.state['reviews'][key] = approved(row)
    e.index = build_discovery_index(tmp_path, pin=PIN, scope={'roots': ['src/'], 'exclude': []},
                                    doc_globs=['docs/*.md'])
    assert e.audit_catalog_boundaries()
    features, outcomes = e.catalog('repos/library', require_boundary=True)
    assert [f['id'] for f in features] == ['parent']
    assert features[0]['entry_points'] == ['src/session.py']
    assert set(features[0]['source_globs']) == {'src/session.py', 'src/alias-one.py', 'src/alias-two.py'}
    assert features[0]['docs'] == ['docs/session.md']
    assert all(r['status'] == 'accepted' for r in outcomes)


def test_alias_cycle_cannot_create_a_formal_feature(tmp_path):
    def call(role, system, prompt, validate):
        _, data = decisions(prompt, lambda r: ('alias', 'second' if r['id'] == 'first' else 'first'))
        return reply(role, data)
    e = engine(tmp_path, [candidate(key='first', title='First'), candidate(key='second', title='Second')], call)
    assert e.audit_catalog_boundaries()
    features, outcomes = e.catalog('repos/library', require_boundary=True)
    assert not features and all(r['status'] == 'unknown' for r in outcomes)


class BoundaryNative(NativeScript):
    def __init__(self, supported=True):
        super().__init__(); self.boundary_supported = supported

    def complete(self, **kwargs):
        response = super().complete(**kwargs)
        payload = self.calls[-1]['payload']
        if 'catalog_boundary_audit' not in payload:
            return response
        data = {'decisions': {r['id']: {'supported': 'yes' if self.boundary_supported else 'no',
            'relation': 'new' if self.boundary_supported else 'unknown', 'related_id': '',
            'reason': 'Native supplemental boundary decision.'} for r in payload['candidates']}}
        return Reply(blocks=[Block(type='text', text=json.dumps(data))], stop_reason=response.stop_reason,
                     usage=response.usage, model=response.model)


def test_legacy_unpublished_preview_reuses_all_scan_and_primary_records(world):
    from pathlib import Path
    import hashlib
    from infermatrix_copilot.kb_service.init_feature_discovery import write_full_discovery_report, compact_discovery_report
    from infermatrix_copilot.kb_service.init_support import InitRecord
    rt, lifecycle, script, _ = _setup(world, script=BoundaryNative())
    previous = _run(rt, lifecycle)
    assert previous.status == 'dry_run', previous.problems
    original_tasks = deepcopy(previous.discovery['tasks'])
    original_reviews = deepcopy(previous.discovery['reviews'])
    generator_calls = sum(c['role'] == 'generator' for c in script.calls)
    judge_calls = sum(c['role'] == 'judge' for c in script.calls)
    previous.discovery.pop('catalog_boundary_audit')
    previous.discovery.pop('boundary_reviews')
    previous.discovery.pop('catalog_consolidation_audit')
    previous.discovery.pop('consolidation_reviews')
    # Simulate the actual older renderer, which had no consolidation namespace.
    full = json.loads(Path(previous.discovery['full_artifact']['path']).read_text())
    full.pop('catalog_consolidation_audit')
    artifact = write_full_discovery_report(rt.state_dir, full)
    text = json.dumps(compact_discovery_report(full, artifact), ensure_ascii=False, indent=2) + '\n'
    path = Path(previous.pr['dry_run_dir']) / 'tree' / previous.discovery['report_path']
    path.write_text(text)
    previous.discovery.update(full_artifact=artifact, report_sha256=hashlib.sha256(text.encode()).hexdigest())
    _save_legacy_record(rt, previous)
    current = _run(rt, lifecycle)
    assert current.status == 'dry_run', current.problems
    assert current.discovery['tasks'] == original_tasks and current.discovery['reviews'] == original_reviews
    assert sum(c['role'] == 'generator' for c in script.calls) == generator_calls
    assert sum(c['role'] == 'judge' for c in script.calls) == judge_calls + 2
    assert _report(current)[1]['catalog_boundary_audit']['done']


def test_forged_empty_audit_subset_is_rejected_by_cached_production_inventory(world):
    from infermatrix_copilot.kb_service.init_feature_discovery import _FeatureDiscovery
    rt, lifecycle, _, _ = _setup(world, script=BoundaryNative())
    record = _run(rt, lifecycle)
    assert record.status == 'dry_run', record.problems
    record.discovery['catalog_boundary_audit']['candidate_ids'] = []
    record.discovery['boundary_reviews'] = {}
    stage = _FeatureDiscovery(rt, lifecycle, dry_run=True, pin=None)
    assert not stage._boundary_record_ready(record)


def test_explicit_retry_does_not_restart_exhausted_content_repairs(world):
    from infermatrix_copilot.kb_service.init_stages import run_stage
    rt, lifecycle, script, _ = _setup(world, policy=_policy(), script=NativeScript(supported=False))
    first = _run(rt, lifecycle)
    assert first.status == 'dry_run', first.problems
    assert first.discovery['reviews']['engine-step']['attempts'] == 4
    original = deepcopy(first.discovery['reviews'])
    calls = len(script.calls)
    second = run_stage(rt, lifecycle, 'feature-discovery', dry_run=True, from_existing=True,
                       unlimited_subscription=True, retry_unfinished=True)
    assert second.status == 'dry_run', second.problems
    assert second.discovery['reviews'] == original
    assert len(script.calls) == calls


def test_prepared_catalog_without_boundary_proof_cannot_publish(world):
    from infermatrix_copilot.kb_service.init_stages import run_stage
    from test_kb_init_skeleton import FakeGh
    rt, lifecycle, script, _ = _setup(world, script=BoundaryNative())
    rt.environ.update(ALLOW_PUSH='1', ALLOW_POST='1', KB_INIT_GIT_AUTHOR='t <t@example.com>')
    gh = FakeGh(fail_create=1); rt.gh_run = gh
    first = run_stage(rt, lifecycle, 'feature-discovery', dry_run=False, from_existing=True,
                      unlimited_subscription=True)
    assert first.status == 'blocked' and first.pr.get('prepared'), first.problems
    first.discovery.pop('catalog_boundary_audit')
    first.save(rt.state_dir)
    calls, pushes = len(script.calls), list(gh.pushed)
    second = run_stage(rt, lifecycle, 'feature-discovery', dry_run=False, from_existing=True,
                       unlimited_subscription=True)
    assert second.status == 'blocked' and any('boundary audit' in p for p in second.problems)
    assert len(script.calls) == calls and gh.pushed == pushes


@pytest.mark.parametrize('review', ['primary', 'boundary', 'consolidation'])
@pytest.mark.parametrize('damage', ['missing', 'tampered'])
def test_prepared_catalog_replays_native_archives_without_rewriting_proofs(world, review, damage):
    from pathlib import Path
    from infermatrix_copilot.kb_service.init_stages import run_stage
    from test_kb_init_skeleton import FakeGh

    rt, lifecycle, script, store = _setup(world, script=BoundaryNative())
    rt.environ.update(ALLOW_PUSH='1', ALLOW_POST='1', KB_INIT_GIT_AUTHOR='t <t@example.com>')
    gh = FakeGh(fail_create=1); rt.gh_run = gh
    first = run_stage(rt, lifecycle, 'feature-discovery', dry_run=False, from_existing=True,
                      unlimited_subscription=True)
    assert first.status == 'blocked' and first.pr.get('prepared'), first.problems
    original_discovery = deepcopy(first.discovery)
    prepared = Path(first.pr['prepared'])
    prepared_bytes = prepared.read_bytes()
    namespace = {'primary': 'reviews', 'boundary': 'boundary_reviews', 'consolidation': 'consolidation_reviews'}[review]
    checked = next(iter(first.discovery[namespace].values()))
    native = store.get(checked['judge_receipt']['trace_id'])
    reply_hash = native['outputs']['reply'].removeprefix('sha256:')
    blob = store.root / 'blobs' / reply_hash[:2] / f'{reply_hash}.gz'
    original_blob = blob.read_bytes()
    if damage == 'missing':
        blob.unlink()
    else:
        blob.write_bytes(b'tampered native archive')
    calls, pushes = len(script.calls), list(gh.pushed)
    rejected = run_stage(rt, lifecycle, 'feature-discovery', dry_run=False, from_existing=True,
                         unlimited_subscription=True)
    assert rejected.status == 'blocked'
    assert any('native archives' in p or 'boundary audit' in p for p in rejected.problems)
    assert rejected.discovery == original_discovery
    assert prepared.read_bytes() == prepared_bytes
    assert len(script.calls) == calls and gh.pushed == pushes
    # Restoring the genuine archive resumes the same prepared publication,
    # preserving both review records instead of synthesizing replacements.
    blob.write_bytes(original_blob)
    resumed = run_stage(rt, lifecycle, 'feature-discovery', dry_run=False, from_existing=True,
                        unlimited_subscription=True)
    assert resumed.status == 'published', resumed.problems
    assert resumed.discovery == original_discovery
    assert prepared.read_bytes() == prepared_bytes
    assert len(script.calls) == calls


@pytest.mark.parametrize('tamper', ['verdict', 'formal_catalog', 'candidate'])
def test_supplemental_native_approval_binds_verdict_full_catalog_and_candidate(world, tamper):
    from infermatrix_copilot.kb_service.init_feature_discovery import _FeatureDiscovery
    from infermatrix_copilot.kb_service.models import ModelRole
    from dataclasses import replace
    rt, lifecycle, _, _ = _setup(world, policy=_policy(), script=BoundaryNative(supported=tamper != 'verdict'))
    record = _run(rt, lifecycle)
    assert record.status == 'dry_run', record.problems
    state = record.discovery; checked = state['boundary_reviews']['engine-step']
    row = state['candidates']['engine-step']
    if tamper == 'verdict':
        checked.update(supported='yes', relation='new')
    elif tamper == 'formal_catalog':
        state['catalog_boundary_audit']['identity']['formal_catalog_sha256'] = '0' * 64
    else:
        row['description'] = 'Claim that the supplemental native judge never read.'
        checked['candidate_sha256'] = _hash(row)
    stage = _FeatureDiscovery(replace(rt, generator=ModelRole.parse('generator', row['generator_receipts'][0]['requested']),
        judge=ModelRole.parse('judge', checked['judge_receipt']['requested'])), lifecycle, dry_run=True, pin=None)
    stage._validate_saved_archives(state)
    assert state['boundary_reviews']['engine-step']['supported'] == 'unsure'
    assert state['catalog_boundary_audit']['done'] is False
    assert 'binding' in state['boundary_reviews']['engine-step']['reason']
