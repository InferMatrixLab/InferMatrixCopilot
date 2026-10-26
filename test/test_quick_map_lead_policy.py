"""New navigation follows active rules without rebinding retained init batches."""
from copy import deepcopy

import pytest

from infermatrix_copilot.kb_service import init_history
from infermatrix_copilot.kb_service.init_quick_maps import (
    MAX_CHARS, render_quick_map, refresh_quick_maps,
)
from infermatrix_copilot.kb_service.init_stages import run_stage
from test_kb_init_quick_maps import RULE_PAGE
from test_kb_init_history import _setup, _runtime, _modules_lifecycle, world  # noqa: F401

REVISION_RULE = (
    '\n## TOY-3a — VersionedMap revision consistency\n\n'
    '- 触发：revision checks change.\n- 强制：retain the comparison.\n'
    '- 源码：`pkg/revisions.py`\n\n'
    '<!-- kb:rule status=active since=2026-09-01 -->\n'
)


def test_new_lead_tracks_rule_title_and_trigger_without_owner_route_edits():
    page = RULE_PAGE + REVISION_RULE
    legacy = render_quick_map(page, signals=['engine', 'tick'], prefixes=['pkg/'])
    updated = render_quick_map(page, signals=['engine', 'tick'], prefixes=['pkg/'], policy_version=2)
    assert 'VersionedMap' not in legacy.split('| PR 描述')[0]
    lead = updated.split('| PR 描述')[0]
    assert '触发词：engine、tick。' in lead
    assert 'VersionedMap' in lead and 'revision checks change' in lead
    assert updated.split('| PR 描述')[1] == legacy.split('| PR 描述')[1]
    assert len(updated) <= MAX_CHARS + 1


def test_refresh_is_idempotent_and_preserves_handwritten_maps():
    path = 'repos/toy/rules.md'
    routes = 'schema_version: 1\nowners:\n- {owner: core, path: '+path+', signals: [engine], scope_prefixes: [pkg/]}\n'
    head, _, problems = refresh_quick_maps({path: RULE_PAGE + REVISION_RULE}, {}, routes,
                                          briefing_docs=(), policy_version=2)
    assert not problems and 'VersionedMap' in head[path].split('| PR 描述')[0]
    again, _, problems = refresh_quick_maps(head, {}, routes, briefing_docs=(), policy_version=2)
    assert not problems and again == head
    hand = RULE_PAGE.replace('# Core rules\n', '# Core rules\n\n## Direct map\n\nHandwritten.\n')
    actual, _, _ = refresh_quick_maps({path: hand}, {}, routes, briefing_docs=(), policy_version=2)
    assert actual[path] == hand


def test_retired_rules_are_absent_and_large_leads_remain_bounded():
    retired = REVISION_RULE.replace('status=active since=2026-09-01',
                                   'status=retired since=2026-09-01 retired_at=2026-09-02 reason=superseded evidence=checked superseded_by=TOY-1a')
    text = render_quick_map(RULE_PAGE + retired, signals=['x' * 300] * 20,
                            prefixes=[], policy_version=2)
    assert 'VersionedMap' not in text.split('| PR 描述')[0]
    assert len(text) <= MAX_CHARS + 1


@pytest.mark.parametrize('version', [True, 0, 3, '2', None])
def test_unsupported_policy_refuses(version):
    with pytest.raises(ValueError, match='quick-map policy'):
        render_quick_map(RULE_PAGE, signals=[], prefixes=[], policy_version=version)


def test_new_stage_persists_policy_and_retained_v5_keeps_old_maps(world, monkeypatch):
    gateway, source = _setup(world)
    rt = _runtime(world, gateway, github=source)
    with monkeypatch.context() as patch:
        patch.setattr(init_history, 'HISTORY_SCOPE_POLICY_VERSION', 5)
        original = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert original.status == 'dry_run', original.problems
    assert original.coverage.get('quick_map_policy_version', 1) == 1
    calls = deepcopy(gateway.extract_calls)
    resumed = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert resumed.inputs_digest == original.inputs_digest
    assert resumed.history['scope_policy_version'] == 5
    assert gateway.extract_calls == calls and len(gateway.review_calls) == 1


def test_new_history_selects_shared_navigation_policy(world):
    gateway, source = _setup(world)
    result = run_stage(_runtime(world, gateway, github=source), _modules_lifecycle(),
                       'pr-history', dry_run=True)
    assert result.status == 'dry_run', result.problems
    assert result.history['scope_policy_version'] >= 6
    assert result.coverage['quick_map_policy_version'] == 2
    # Aggregate receives the actual refreshed map, not a separate prompt repair.
    assert '规则触发：' in str(gateway.review_calls)
