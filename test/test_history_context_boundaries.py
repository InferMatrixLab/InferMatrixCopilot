"""Corrupt context cannot trigger another judgment; dirty files cannot replace a pin."""
import pytest

from infermatrix_copilot.kb_service import init_history
from infermatrix_copilot.kb_service.init_stages import run_stage
from test_history_aggregate_context import _context_case, world  # noqa: F401
from test_kb_init_history import _modules_lifecycle


@pytest.mark.parametrize('fault', ['hash', 'range', 'path', 'missing'])
def test_corrupt_context_blocks_before_another_judgment(world, fault):
    gateway, _, rt = _context_case(world)
    gateway.fail_review = 1
    first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert first.status == 'blocked' and len(gateway.review_calls) == 1
    commit = first.history['commits'][0]
    if fault == 'missing':
        del commit['context_evidence']
    else:
        entry = commit['context_evidence'][0]
        if fault == 'hash': entry['sha256'] = '0' * 64
        elif fault == 'range': entry['end'] = 1_000_000
        else: entry['path'] = '../../outside.py'
    first.save(rt.state_dir)
    again = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert again.status == 'blocked' and again.problems
    assert len(gateway.review_calls) == 1 and gateway.extract_calls == [101,102,103]


def test_oversized_context_is_not_clipped_or_dispatched(world, monkeypatch):
    gateway, _, rt = _context_case(world)
    original = gateway.call_json
    def call(role, **request):
        reply = original(role, **request)
        if len(gateway.extract_calls) == 3:
            monkeypatch.setattr(init_history, 'MAX_CODE_BYTES', 1)
        return reply
    gateway.call_json = call
    record = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert record.status == 'blocked' and 'extraction bound' in ' '.join(record.problems)
    assert gateway.review_calls == []


def test_aggregate_uses_committed_context_after_source_worktree_changes(world):
    gateway, _, rt = _context_case(world)
    original = gateway.call_json
    def call(role, **request):
        reply = original(role, **request)
        if len(gateway.extract_calls) == 3:
            (world['upstream']/'tests/test_context.py').write_text('uncommitted replacement\n')
        return reply
    gateway.call_json = call
    record = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert record.status == 'dry_run', record.problems
    files = [f for c in gateway.review_calls[0]['review_context']['commits'] for f in c['files']]
    assert any(f['text'] == 'def test_regression():\n    assert True' for f in files)
    assert all('uncommitted replacement' not in f['text'] for f in files)
