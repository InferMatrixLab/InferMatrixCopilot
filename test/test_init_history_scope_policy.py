"""New scope guidance must never rebind a previously paid history batch."""
from copy import deepcopy

import pytest

from infermatrix_copilot.kb_service import init_history
from infermatrix_copilot.kb_service.init_stages import run_stage
from infermatrix_copilot.kb_service.init_support import InitError
from test_kb_init_history import _setup, _runtime, _modules_lifecycle, world  # noqa: F401
from test_history_extraction_checkpoint import runtime


def test_new_history_batch_binds_scope_policy_and_dispatches_its_prompt(world):
    gateway, source = _setup(world)
    observed = []
    original = gateway.call_json
    def observe(role, *, system, **request):
        observed.append(system)
        return original(role, system=system, **request)
    gateway.call_json = observe
    rt = _runtime(world, gateway, github=source)
    result = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert result.status == 'dry_run', result.problems
    assert result.history['scope_policy_version'] == init_history.HISTORY_SCOPE_POLICY_VERSION
    assert observed.count(init_history.SYSTEM_HISTORY) == 3
    assert init_history.SYSTEM_HISTORY_LEGACY not in observed
    assert init_history.SYSTEM_HISTORY != init_history.SYSTEM_HISTORY_LEGACY


def test_completed_legacy_batch_reuses_original_identity_and_results(world, monkeypatch):
    gateway, source = _setup(world)
    rt = _runtime(world, gateway, github=source)
    with monkeypatch.context() as patch:
        patch.setattr(init_history, 'HISTORY_SCOPE_POLICY_VERSION', 0)
        first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert first.status == 'dry_run', first.problems
    assert 'scope_policy_version' not in first.history
    calls = deepcopy(gateway.extract_calls)
    second = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert second.pr == first.pr and second.inputs_digest == first.inputs_digest
    assert gateway.extract_calls == calls and len(gateway.review_calls) == 1
    assert 'scope_policy_version' not in second.history


def test_legacy_unknown_keeps_exact_prompt_binding_without_replacement(world, monkeypatch):
    rt, calls, _ = runtime(world)
    with monkeypatch.context() as patch:
        patch.setattr(init_history, 'HISTORY_SCOPE_POLICY_VERSION', 0)
        first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    state = deepcopy(first.coverage['generation_execution'])
    original_sha = state['history:101']['generation_dispatched_binding']['inputs']['system_sha256']
    assert original_sha == '64ae0ce04c64fc68a843904bb853406c22c5c682a425059c144373d898836c8d'
    second = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert second.status == 'blocked' and calls == [101]
    assert second.inputs_digest == first.inputs_digest
    assert second.coverage['generation_execution'] == state


def test_scope_policy_change_cannot_replace_an_unknown_call(world, monkeypatch):
    rt, calls, _ = runtime(world)
    with monkeypatch.context() as patch:
        patch.setattr(init_history, 'HISTORY_SCOPE_POLICY_VERSION', 0)
        first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    original = deepcopy(first.coverage['generation_execution'])
    first.history['scope_policy_version'] = 1
    path = first.save(rt.state_dir)
    before = path.read_bytes()
    with pytest.raises(InitError, match='inputs changed with an unresolved model call'):
        run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert calls == [101] and path.read_bytes() == before
    assert first.coverage['generation_execution'] == original


@pytest.mark.parametrize('version', [True, -1, 7, '1', None])
def test_unsupported_scope_policy_refuses_without_checkpoint_mutation(world, version):
    rt, calls, _ = runtime(world)
    first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    first.history['scope_policy_version'] = version
    path = first.save(rt.state_dir)
    before = path.read_bytes()
    with pytest.raises(InitError, match='unsupported PR-history scope policy'):
        run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert path.read_bytes() == before and calls == [101]


def test_known_predispatch_legacy_batch_continues_with_legacy_prompt(world, monkeypatch):
    rt, calls, _ = runtime(world, unknown=False)
    with monkeypatch.context() as patch:
        patch.setattr(init_history, 'HISTORY_SCOPE_POLICY_VERSION', 0)
        first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert first.status == 'blocked' and calls == []
    digest = first.inputs_digest
    second = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert second.status == 'empty' and calls == [101, 102, 103]
    assert second.inputs_digest == digest and 'scope_policy_version' not in second.history
    for state in second.coverage['generation_execution'].values():
        assert state['generated']['system_sha256'] == '64ae0ce04c64fc68a843904bb853406c22c5c682a425059c144373d898836c8d'


@pytest.mark.parametrize('version', [2, 3])
def test_retained_citation_batches_keep_their_original_request_after_new_guidance(world, monkeypatch, version):
    rt, calls, store = runtime(world)
    with monkeypatch.context() as patch:
        patch.setattr(init_history, 'HISTORY_SCOPE_POLICY_VERSION', version)
        first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    original = deepcopy(first.coverage['generation_execution'])
    record, = [r for r, _ in store.iter_records(kind='model_call')]
    system = store.blob(record['inputs']['system'])
    assert system == init_history.SYSTEM_HISTORY_V2
    assert system != init_history.SYSTEM_HISTORY
    again = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert again.status == 'blocked' and calls == [101]
    assert again.history['scope_policy_version'] == version
    assert again.inputs_digest == first.inputs_digest
    assert again.coverage['generation_execution'] == original
    assert len(list(store.iter_records(kind='model_call'))) == 1
