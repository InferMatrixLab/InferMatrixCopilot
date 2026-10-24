"""Partial metadata stays bounded and retries retain the original publication."""
import copy
import hashlib
import json
from types import SimpleNamespace

from infermatrix_copilot.kb_service.init_stages import render_pr_body
from infermatrix_copilot.kb_service.init_support import load_prepared, save_prepared
from test_kb_foundation_explicit_partial import _retained_native_partial, _run_partial
from test_kb_init_modules import _modules_lifecycle
from test_kb_init_skeleton import world  # noqa: F401


def test_large_native_history_has_bounded_truthful_partial_body(world):
    rt, original, calls, traces, page = _retained_native_partial(world)
    record = _run_partial(rt)
    original_jobs = copy.deepcopy(record.coverage['foundation_jobs'])
    # The actual failure arose from serializing accumulated native verdicts,
    # drops and gaps into metadata. Their size must never change body bounds.
    record.verdicts.update({f'facet-{i}': {'verdict':'pass', 'page':page} for i in range(10000)})
    record.dropped.extend([{'rule_id':'unknown', 'why':'retained independent reasons '*200}] * 10000)
    record.unfinished.extend(['retained unknown '*200] * 10000)
    before = copy.deepcopy(record)
    body = render_pr_body(record, _modules_lifecycle())
    assert len(body.encode()) < 10000
    assert 'init_complete=false' in body and '1 项' in body and '0/1' in body
    assert record.coverage['foundation_publication']['receipt']['sha256'] in body
    assert record.coverage['foundation_jobs'] == original_jobs and record == before


def test_strict_formatter_remains_exact_legacy_path():
    from infermatrix_copilot.kb_service.init_support import InitRecord
    record = InitRecord('knowledge','toy',pin='a'*40,kb_base_sha='b'*40)
    body = render_pr_body(record, _modules_lifecycle())
    assert body == '\n'.join([
        '`kb init` stage **knowledge** for `toy` (`o/toy`).', '',
        '- Upstream pin: `'+'a'*40+'`', '- Knowledge base: `'+'b'*40+'`',
        '- Model spend (accounted): $0.00', '',
        'Human-merged. Explanatory knowledge has pinned source references and per-facet advisory verdicts. '
        'Design inferences are labeled; failed sections are removed and missing facets remain listed.', ''])


def test_prepared_body_refresh_preserves_native_jobs_files_and_commit_metadata(world):
    rt, original, calls, traces, page = _retained_native_partial(world, fail_create=1)
    first = _run_partial(rt)
    assert first.status == 'blocked' and first.pr['prepared']
    prepared = load_prepared(first.pr['prepared'])
    # Simulate the authentic older runtime's already prepared oversized body
    # through the same official metadata writer, without rewriting any source.
    oversized = {**prepared, 'body':'Old full review history\n'+'retained failed context '*60000}
    save_prepared(first.pr['prepared'], **oversized)
    # The older runtime predates the executor's immutable publication proof.
    # Keep the fixture faithful to that legacy record, rather than tampering
    # with a newly bound prepared publication.
    first.pr.pop('validation', None)
    first.save(rt.state_dir)
    before_tasks = copy.deepcopy(first.coverage['foundation_jobs'])
    before_proofs = first.coverage['foundation_publication']['receipt'].copy()
    before_calls = len(calls)
    resumed = _run_partial(rt)
    after = load_prepared(resumed.pr['prepared'])
    assert resumed.status == 'published', resumed.problems
    assert after['body'] != oversized['body'] and len(after['body'].encode()) < 10000
    assert {k:v for k,v in after.items() if k!='body'} == {k:v for k,v in oversized.items() if k!='body'}
    assert len(calls) == before_calls
    assert resumed.inputs_digest == original.inputs_digest
    assert resumed.coverage['foundation_jobs'] == before_tasks
    assert resumed.coverage['foundation_publication']['receipt'] == before_proofs
