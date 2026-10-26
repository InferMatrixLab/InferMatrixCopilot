"""Pinned review-only context must survive the extraction/aggregate boundary."""
from copy import deepcopy
import json

import pytest

from infermatrix_copilot.kb_service import init_history
from infermatrix_copilot.kb_service.init_stages import run_stage
from infermatrix_copilot.kb_service.init_support import InitRecord
from infermatrix_copilot.knowledge_service.pinned_claims import Evidence, check_evidence
from test_kb_init_history import _setup, _runtime, _modules_lifecycle, world  # noqa: F401
from test_kb_init_skeleton import _commit, _git


def _context_case(world):
    _commit(world['upstream'], {'tests/test_context.py': 'def test_regression():\n    assert True\n'},
            'pinned regression context')
    gateway, source = _setup(world)
    original = source.history_evidence
    def evidence(repo, number):
        item = original(repo, number)
        item['files'].append({'filename':'tests/test_context.py', 'patch':'+assert True'})
        return item
    source.history_evidence = evidence
    return gateway, source, _runtime(world, gateway, github=source)


def test_aggregate_preserves_pinned_context_without_relaxing_citation_ownership(world):
    gateway, source, rt = _context_case(world)
    result = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert result.status == 'dry_run', result.problems
    packet = gateway.review_calls[0]
    context = packet['review_context']
    assert context['upstream_pin'] == result.pin == source.pin
    assert context['may_be_used_as_rule_citations'] is False
    assert context['test_execution_claimed'] is False
    assert [c['upstream_pr'] for c in context['commits']] == [101,102]
    for commit in context['commits']:
        test = next(f for f in commit['files'] if f['path']=='tests/test_context.py')
        assert test['text'] == 'def test_regression():\n    assert True'
        assert test['start']==1 and test['end']==2
        assert check_evidence([Evidence.from_dict(test)], rt.upstream(result.repo, 'o/toy').observer(result.pin, pull=rt.pull)) == []
    assert all(not e['path'].startswith('tests/') for entries in packet['rule_evidence'].values() for e in entries)
    assert all('text' not in e for c in result.history['commits'] for e in c['context_evidence'])
    calls = deepcopy(gateway.review_calls)
    again = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert again.pr == result.pr and gateway.review_calls == calls


@pytest.mark.parametrize('version', range(5))
def test_retained_history_versions_keep_legacy_aggregate_input(world, monkeypatch, version):
    gateway, source, rt = _context_case(world)
    with monkeypatch.context() as scope:
        scope.setattr(init_history, 'HISTORY_SCOPE_POLICY_VERSION', version)
        first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert first.status == 'dry_run', first.problems
    assert 'review_context' not in gateway.review_calls[0]
    assert all('context_evidence' not in c for c in first.history['commits'])
    again = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert again.pr == first.pr and len(gateway.review_calls)==1
