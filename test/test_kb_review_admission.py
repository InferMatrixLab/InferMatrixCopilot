"""Pending gate intents block replacement work before any model or parent update."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service.models import ModelUnavailable
from infermatrix_copilot.kb_service.runtime import require_settled_reviews


def runtime(kind, detail):
    row = {'id': 'original-attempt', 'kind': kind, 'detail': detail, 'status': 'review_pending'}

    def changesets(repo, statuses):
        assert repo == 'demo' and statuses == ('review_pending',)
        return [row]

    return SimpleNamespace(lease_owner='lease', ledger=SimpleNamespace(changesets=changesets)), row


@pytest.mark.parametrize('kind', ['sweep', 'purge', 'refine', 'rebuild', 'correction', 'maintenance_calibration'])
@pytest.mark.parametrize('legacy', [False, True])
def test_workflows_refuse_pending_review_before_drafting_or_parent_mutation(kind, legacy):
    detail = {'refine_of': 'parent', 'rebuild_of': 'parent', 'correction_unit': 'unit'}
    rt, row = runtime(kind, {} if legacy else detail)
    lifecycle = SimpleNamespace(repo='demo')
    parent = {'id': 'parent', 'kind': 'intake', 'detail': {}}
    before = deepcopy((row, parent))

    with pytest.raises(ModelUnavailable, match='original-attempt.*reconcile original dispatch') as caught:
        if kind in ('sweep', 'purge'):
            from infermatrix_copilot.kb_service.sweep import run_sweep
            run_sweep(rt, lifecycle, 'lease', {}, None)
        elif kind == 'refine':
            from infermatrix_copilot.kb_service.refine import refine
            refine(rt, lifecycle, parent)
        elif kind == 'rebuild':
            from infermatrix_copilot.kb_service.merge import rebuild
            rebuild(rt, lifecycle, parent, 'base changed')
        else:
            from infermatrix_copilot.kb_service.maintenance_correction import propose_correction
            propose_correction(rt, lifecycle, None, None, run_id='run', unit={'unit_id': 'unit'},
                finding={}, base={}, base_sha='sha', publish_result=kind == 'correction')
    assert caught.value.allow_fallback is False
    assert (row, parent) == before


def test_pending_review_for_another_bound_parent_does_not_block_independent_work():
    rt, _ = runtime('refine', {'refine_of': 'other-parent'})
    require_settled_reviews(rt, 'demo', kinds=('refine',), binding={'refine_of': 'parent'})


def test_pending_review_of_other_workflow_does_not_block():
    rt, _ = runtime('sweep', {})
    require_settled_reviews(rt, 'demo', kinds=('refine',), binding={'refine_of': 'parent'})


@pytest.mark.parametrize('kind', ['refine', 'rebuild'])
def test_lost_child_review_is_bound_to_parent_and_resume_makes_no_calls(tmp_path, kind):
    from test_kb_refine import _runtime
    from infermatrix_copilot.kb_service.runtime import collect_events, run_intake
    from infermatrix_copilot.kb_service.refine import refine
    from infermatrix_copilot.kb_service.merge import rebuild

    rt, lifecycle, judges = _runtime(tmp_path, ['no'])
    collect_events(rt, lifecycle)
    parent_id = run_intake(rt, lifecycle)
    parent = rt.ledger.changeset(parent_id)

    def lost(role, prompt):
        if role.name == 'generator':
            return judges(role, prompt)
        error = ModelUnavailable('native response lost', allow_fallback=False)
        error.native_call_sent = True
        error.native_call_completed = False
        raise error

    rt.gateway.answer = lost

    def invoke():
        return (refine(rt, lifecycle, parent) if kind == 'refine'
                else rebuild(rt, lifecycle, parent, 'base changed'))

    with pytest.raises(ModelUnavailable, match='native response lost'):
        invoke()
    child, = rt.ledger.changesets('demo', ('review_pending',))
    assert child['kind'] == kind and child['detail'][kind + '_of'] == parent_id
    assert rt.changeset_path(child['id']).exists()
    calls = list(rt.gateway.calls)
    with pytest.raises(ModelUnavailable, match='reconcile original dispatch'):
        invoke()
    assert rt.gateway.calls == calls
    assert rt.ledger.changeset(parent_id) == parent
