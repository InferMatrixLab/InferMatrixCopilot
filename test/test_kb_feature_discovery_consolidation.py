"""Joint provisional-catalog review preserves original proofs and source joins."""
from copy import deepcopy
from dataclasses import replace
import json
import hashlib
from pathlib import Path
import threading
import time

import pytest

from infermatrix_copilot.kb_service.feature_discovery_index import build_discovery_index
from infermatrix_copilot.kb_service.init_feature_discovery import (
    SYSTEM_CONSOLIDATE, _consolidation_ready, _FeatureDiscovery, _hash,
    compact_discovery_report, write_full_discovery_report,
    _mutual_canonical_conflicts, CATALOG_RENDERER_VERSION,
)
from infermatrix_copilot.kb_service.init_budget import BudgetExhausted
from infermatrix_copilot.kb_service.models import ModelUnavailable, ModelRole
from test_kb_feature_discovery import candidate, reply
from test_kb_feature_discovery_boundary import engine, decisions
from test_kb_feature_discovery_stage import _setup, _run, _report, _policy, _save_legacy_record
from test_kb_init_skeleton import world  # noqa: F401


def audited(tmp_path, rows, seeds=(), concurrency=13):
    def primary(role, system, prompt, validate):
        _, data = decisions(prompt)
        return reply(role, data)
    e = engine(tmp_path, rows, primary, seeds, concurrency)
    assert e.audit_catalog_boundaries()
    return e


def test_cross_layer_capability_with_different_aliases_is_one_canonical_feature(tmp_path):
    rows = [candidate(key='backend-export', title='Share image jobs'),
            candidate(key='web-export', path='src/web.ts', title='Web share export action')]
    rows[0]['aliases'] = ['createShareJob']
    rows[1]['aliases'] = ['ShareExportAction']
    e = audited(tmp_path, rows)
    (tmp_path / 'src/web.ts').write_text('export function ShareExportAction() {\n  return createShareJob();\n}\n')
    (tmp_path / 'src/consumer.ts').write_text('import { ShareExportAction } from "./web";\nShareExportAction();\n')
    e.index = build_discovery_index(tmp_path, pin=e.pin, scope={'roots': ['src/'], 'exclude': []}, doc_globs=[])
    # Boundary approvals reference the exact same candidates; only a missing
    # test fixture file prevented provisional admission before rebuilding index.
    assert e.audit_catalog_boundaries()
    provisional, _ = e.catalog('repos/library', require_boundary=True)
    snapshots = deepcopy({k: e.state[k] for k in ('tasks', 'reviews', 'boundary_reviews')})
    captures = []
    def consolidate(role, system, prompt, validate):
        assert role == 'judge' and system == SYSTEM_CONSOLIDATE
        payload, data = decisions(prompt, lambda row: ('new', '') if row['id'] == 'backend-export'
                                  else ('implementation_supplement', 'backend-export'))
        captures.append(payload)
        assert {r['id'] for r in payload['provisional_catalog']} == {'backend-export', 'web-export'}
        assert any('createShareJob' in ex['text'] for ex in payload['evidence'])
        assert any(ex['path'] == 'src/consumer.ts' for ex in payload['consumer_evidence'])
        return reply(role, data)
    e.call = consolidate
    assert e.audit_catalog_consolidation(provisional)
    policies = [context['policy'] for context in e.state['dispatch_contexts'].values()]
    assert any(policy['kind'] == 'boundary' and 'baseline' in policy for policy in policies)
    assert any(policy['kind'] == 'consolidation' and policy['features'] == provisional for policy in policies)
    assert all(request['binding']['context_sha256'] == request['request']['context_sha256']
               for request in e.state['dispatch_requests'].values())
    features, outcomes = e.catalog('repos/library', require_boundary=True, consolidation_features=provisional)
    assert [f['id'] for f in features] == ['backend-export']
    assert set(features[0]['source_globs']) == {'src/session.py', 'src/web.ts'}
    assert features[0]['entry_points'] == ['src/session.py']
    assert all(r['status'] == 'accepted' for r in outcomes)
    assert {k: e.state[k] for k in snapshots} == snapshots
    identity = deepcopy(e.state['catalog_consolidation_audit'])
    # Always derive the ORIGINAL provisional set, never the consolidated result.
    restored, _ = e.catalog('repos/library', require_boundary=True)
    assert restored == provisional
    assert e.audit_catalog_consolidation(restored)
    assert len(captures) == 1 and e.state['catalog_consolidation_audit'] == identity


@pytest.mark.parametrize('failure', ['no', 'missing', 'channel'])
def test_unknown_is_terminal_and_original_79_ids_are_protected(tmp_path, failure):
    seeds = [{'id': f'formal-{n}', 'title': f'Formal {n}', 'owner': 'runtime',
              'source_globs': ['src/session.py'], 'docs': [], 'entry_points': ['src/session.py']} for n in range(79)]
    e = audited(tmp_path, [candidate(key='extra')], seeds)
    provisional, _ = e.catalog('repos/library', require_boundary=True)
    calls = []
    def call(role, system, prompt, validate):
        calls.append(role)
        if failure == 'channel':
            raise ModelUnavailable('native unavailable')
        payload, data = decisions(prompt)
        assert len(payload['baseline']) == 79
        if failure == 'no':
            data['decisions']['extra'].update(supported='no', relation='unknown')
        else:
            data = {'decisions': {}}
        return reply(role, data)
    e.call = call
    assert e.audit_catalog_consolidation(provisional)
    assert e.audit_catalog_consolidation(provisional) and e.review()
    features, outcomes = e.catalog('repos/library', require_boundary=True, consolidation_features=provisional)
    assert features == provisional[:79] and outcomes[0]['status'] == 'unknown'
    assert calls == ['judge']


def test_interrupted_consolidation_reuses_successful_batches_and_all_prior_records(tmp_path):
    e = audited(tmp_path, [candidate(key=f'capability-{n:02}', title=f'Capability {n}') for n in range(13)], concurrency=1)
    provisional, _ = e.catalog('repos/library', require_boundary=True)
    original = deepcopy({k: e.state[k] for k in ('tasks', 'reviews', 'boundary_reviews')})
    calls = []
    def call(role, system, prompt, validate):
        payload, data = decisions(prompt)
        calls.append([r['id'] for r in payload['candidates']])
        if len(calls) == 2:
            raise BudgetExhausted('interrupted')
        return reply(role, data)
    e.call = call
    assert not e.audit_catalog_consolidation(provisional)
    assert len(e.state['consolidation_reviews']) == 12
    assert e.audit_catalog_consolidation(provisional)
    assert calls[1] == calls[2] and len(calls) == 3
    assert {k: e.state[k] for k in original} == original
    e.state['catalog_consolidation_audit']['candidate_ids'] = []
    assert not _consolidation_ready(e.seeds, e.state, provisional, repository=e.repository, pin=e.pin)
    assert not e.catalog('repos/library', require_boundary=True, consolidation_features=provisional)[0]


def test_native_consolidation_candidate_boundary_and_global_inputs_are_bound(world):
    rt, lifecycle, script, store = _setup(world)
    record = _run(rt, lifecycle)
    assert record.status == 'dry_run', record.problems
    state = record.discovery
    checked = state['consolidation_reviews']['engine-step']
    stage = _FeatureDiscovery(replace(rt,
        generator=ModelRole.parse('generator', state['candidates']['engine-step']['generator_receipts'][0]['requested']),
        judge=ModelRole.parse('judge', checked['judge_receipt']['requested'])), lifecycle, dry_run=True, pin=None)
    stage._verify_consolidation_approval('engine-step', state['candidates']['engine-step'], checked, state)
    original = deepcopy(state)
    for field in ('proposal_summaries_sha256', 'provisional_catalog_sha256', 'bindings_sha256'):
        changed = deepcopy(original)
        changed['catalog_consolidation_audit']['identity'][field] = '0' * 64
        stage._validate_saved_archives(changed)
        assert changed['consolidation_reviews']['engine-step']['supported'] == 'unsure'
        assert not changed['catalog_consolidation_audit']['done']
    changed = deepcopy(original)
    changed['boundary_reviews']['engine-step']['reason'] += ' changed'
    stage._validate_saved_archives(changed)
    assert changed['consolidation_reviews']['engine-step']['supported'] == 'unsure'


def test_archived_system_prompt_must_be_the_actual_consolidation_instruction(world, monkeypatch):
    from infermatrix_copilot.trace_store import TraceStore
    from infermatrix_copilot.kb_service.init_feature_discovery import SYSTEM_REVIEW
    rt, lifecycle, _, store = _setup(world)
    record = _run(rt, lifecycle)
    assert record.status == 'dry_run', record.problems
    state = record.discovery
    row = state['candidates']['engine-step']
    checked = state['consolidation_reviews']['engine-step']
    stage = _FeatureDiscovery(rt, lifecycle, dry_run=True, pin=None)
    original_get = TraceStore.get
    wrong_system = store.put_blob(SYSTEM_REVIEW)
    def substituted(self, trace_id):
        native = original_get(self, trace_id)
        if trace_id == checked['judge_receipt']['trace_id']:
            native = deepcopy(native)
            native['inputs']['system'] = wrong_system
        return native
    monkeypatch.setattr(TraceStore, 'get', substituted)
    with pytest.raises(ModelUnavailable, match='system prompt differs'):
        stage._verify_consolidation_approval('engine-step', row, checked, state)


def test_consolidation_shared_worker_ceiling_is_13(tmp_path):
    e = audited(tmp_path, [candidate(key=f'capability-{n:03}', title=f'Capability {n}') for n in range(157)], concurrency=13)
    provisional, _ = e.catalog('repos/library', require_boundary=True)
    lock = threading.Lock()
    active = maximum = 0
    def call(role, system, prompt, validate):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        _, data = decisions(prompt)
        with lock:
            active -= 1
        return reply(role, data)
    e.call = call
    assert e.audit_catalog_consolidation(provisional)
    assert 1 < maximum <= 13 and active == 0


def test_middle_consumer_join_in_an_already_cited_file_is_offered(tmp_path):
    rows = [candidate(key='widget', title='Submit widget'), candidate(key='controller', title='Action controller')]
    rows[0]['aliases'] = ['SubmitWidget']
    rows[1]['aliases'] = ['useSubmitAction']
    rows[1]['evidence'] = [{'path': 'src/session.py', 'start': 100, 'end': 101}]
    e = audited(tmp_path, rows)
    lines = ['value = 1'] * 101
    lines[0] = 'def SubmitWidget(): pass'
    lines[49] = 'SubmitWidget(onSubmit=useSubmitAction())'
    lines[99] = 'def useSubmitAction(): pass'
    (tmp_path / 'src/session.py').write_text('\n'.join(lines) + '\n')
    e.index = build_discovery_index(tmp_path, pin=e.pin, scope={'roots': ['src/'], 'exclude': []}, doc_globs=[])
    assert e.audit_catalog_boundaries()
    provisional, _ = e.catalog('repos/library', require_boundary=True)
    context = e._consolidation_context([e.state['candidates']['widget']], provisional)
    assert any(ex['start'] <= 50 <= ex['end'] and 'onSubmit=useSubmitAction()' in ex['text']
               for ex in context['consumer_evidence'])


@pytest.mark.parametrize('published', [False, True])
def test_pre_consolidation_preview_reuses_scan_primary_boundary_without_retry(world, published):
    rt, lifecycle, script, _ = _setup(world)
    first = _run(rt, lifecycle)
    assert first.status == 'dry_run', first.problems
    retained = deepcopy({k: first.discovery[k] for k in ('tasks', 'reviews', 'boundary_reviews')})
    first.discovery.pop('catalog_consolidation_audit')
    first.discovery.pop('consolidation_reviews')
    full = json.loads(Path(first.discovery['full_artifact']['path']).read_text())
    full.pop('catalog_consolidation_audit')
    artifact = write_full_discovery_report(rt.state_dir, full)
    text = json.dumps(compact_discovery_report(full, artifact), ensure_ascii=False, indent=2) + '\n'
    (Path(first.pr['dry_run_dir']) / 'tree' / first.discovery['report_path']).write_text(text)
    first.discovery.update(full_artifact=artifact, report_sha256=hashlib.sha256(text.encode()).hexdigest())
    if published:
        first.status = 'published'
    _save_legacy_record(rt, first)
    count = len(script.calls)
    current = _run(rt, lifecycle)
    assert current.status == ('published' if published else 'dry_run'), current.problems
    assert {k: current.discovery[k] for k in retained} == retained
    assert len(script.calls) == count + (0 if published else 1)
    if not published:
        assert script.calls[-1]['system'] == SYSTEM_CONSOLIDATE
        compact = _report(current)[1]
        assert compact['catalog_consolidation_audit']['done']
    calls = len(script.calls)
    assert _run(rt, lifecycle).discovery == current.discovery
    assert len(script.calls) == calls


@pytest.mark.parametrize('damage', ['missing', 'tampered'])
def test_cached_consolidation_receipt_damage_preserves_checkpoint_without_resampling(world, damage):
    rt, lifecycle, script, store = _setup(world)
    first = _run(rt, lifecycle)
    assert first.status == 'dry_run', first.problems
    from infermatrix_copilot.kb_service.init_support import InitRecord, InitError
    checkpoint = InitRecord.path(rt.state_dir, lifecycle.repo, 'feature-discovery')
    original = checkpoint.read_bytes()
    proof = first.discovery['consolidation_reviews']['engine-step']['judge_receipt']
    trace = store.get(proof['trace_id'])
    digest = trace['inputs']['prompt'].removeprefix('sha256:')
    blob = store.root / 'blobs' / digest[:2] / f'{digest}.gz'
    saved = blob.read_bytes()
    if damage == 'missing':
        blob.unlink()
    else:
        blob.write_bytes(b'invalid supplemental input')
    count = len(script.calls)
    with pytest.raises(InitError, match='native archives'):
        _run(rt, lifecycle)
    assert checkpoint.read_bytes() == original and len(script.calls) == count
    blob.write_bytes(saved)
    assert _run(rt, lifecycle).discovery == first.discovery
    assert len(script.calls) == count


def test_mutual_canonical_claims_are_derived_unknown_without_changing_native_records(tmp_path):
    e = audited(tmp_path, [candidate(key='backend', title='Structured ask tool'),
                           candidate(key='web', title='Structured ask presentation')])
    provisional, _ = e.catalog('repos/library', require_boundary=True)
    reasons = {'backend': 'Retain this ID as the canonical structured capability encompassing web presentation.',
               'web': 'Connect it to backend as one capability; this ID is canonical.'}
    def call(role, system, prompt, validate):
        _, data = decisions(prompt)
        for key, row in data['decisions'].items():
            row['reason'] = reasons[key]
        return reply(role, data)
    e.call = call
    assert e.audit_catalog_consolidation(provisional)
    saved = deepcopy(e.state)
    # The original pre-consolidation input set remains stable for receipt reuse.
    assert e.catalog('repos/library', require_boundary=True)[0] == provisional
    features, outcomes = e.catalog('repos/library', require_boundary=True, consolidation_features=provisional)
    assert features == []
    assert all(r['status'] == 'unknown' and 'mutual provisional canonical' in r['reason'] for r in outcomes)
    assert e.state == saved
    assert all(r['consolidation_review']['supported'] == 'yes' for r in outcomes)


@pytest.mark.parametrize('other_relation,other_reason', [
    ('new', 'Distinct capability; backend is a related implementation.'),
    ('new', 'This ID is canonical for its own capability.'),
    ('new', 'Uses canonical JSON formatting mentioning backend.'),
    ('new', 'This ID is canonical. An unrelated backend is mentioned separately.'),
    ('new', 'Connect it to backend-extra as one capability; this ID is canonical.'),
    ('new', 'This ID is not canonical relative to backend.'),
    ('implementation_supplement', 'backend is the canonical capability.'),
])
def test_one_way_unrelated_inexact_and_supplement_mentions_are_not_conflicts(other_relation, other_reason):
    rows = {'backend': {'supported': 'yes', 'relation': 'new',
                        'reason': 'Retain this ID as the canonical capability encompassing web.'},
            'web': {'supported': 'yes', 'relation': other_relation, 'reason': other_reason}}
    assert _mutual_canonical_conflicts(rows) == {}


def test_canonical_conflicts_cannot_remove_original_formal_ids(tmp_path):
    seed = {'id': 'backend', 'title': 'Original backend', 'owner': 'runtime',
            'source_globs': ['src/session.py'], 'docs': [], 'entry_points': ['src/session.py']}
    e = audited(tmp_path, [candidate(key='web', title='New web capability')], [seed])
    provisional, _ = e.catalog('repos/library', require_boundary=True)
    def call(role, system, prompt, validate):
        _, data = decisions(prompt)
        data['decisions']['web']['reason'] = 'Connect it to backend as one capability; this ID is canonical.'
        return reply(role, data)
    e.call = call
    assert e.audit_catalog_consolidation(provisional)
    e.state['consolidation_reviews']['backend'] = {'supported': 'yes', 'relation': 'new',
        'reason': 'Retain this ID as the canonical capability encompassing web.'}
    features, _ = e.catalog('repos/library', require_boundary=True, consolidation_features=provisional)
    assert features[0] == seed
    assert {f['id'] for f in features} == {'backend', 'web'}


@pytest.mark.parametrize('published', [False, True])
def test_earlier_renderer_rerenders_unpublished_without_any_native_calls(world, published):
    rt, lifecycle, script, _ = _setup(world)
    first = _run(rt, lifecycle)
    assert first.status == 'dry_run', first.problems
    saved = deepcopy({k: first.discovery[k] for k in ('tasks', 'reviews', 'boundary_reviews',
                        'consolidation_reviews', 'catalog_consolidation_audit')})
    full = json.loads(Path(first.discovery['full_artifact']['path']).read_text())
    full.pop('catalog_renderer_version')
    artifact = write_full_discovery_report(rt.state_dir, full)
    text = json.dumps(compact_discovery_report(full, artifact), ensure_ascii=False, indent=2) + '\n'
    (Path(first.pr['dry_run_dir']) / 'tree' / first.discovery['report_path']).write_text(text)
    first.discovery.pop('catalog_renderer_version')
    first.discovery.update(full_artifact=artifact, report_sha256=hashlib.sha256(text.encode()).hexdigest())
    if published:
        first.status = 'published'
    _save_legacy_record(rt, first)
    calls = len(script.calls)
    current = _run(rt, lifecycle)
    assert current.status == ('published' if published else 'dry_run'), current.problems
    assert {k: current.discovery[k] for k in saved} == saved
    assert len(script.calls) == calls
    if published:
        assert 'catalog_renderer_version' not in current.discovery
    else:
        assert current.discovery['catalog_renderer_version'] == CATALOG_RENDERER_VERSION
        assert _report(current)[1]['catalog_renderer_version'] == CATALOG_RENDERER_VERSION
        assert _run(rt, lifecycle).discovery == current.discovery
        assert len(script.calls) == calls
