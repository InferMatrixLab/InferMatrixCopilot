"""The common application binds navigation policy, including its capacity check."""
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service import init_execution, init_content
from infermatrix_copilot.kb_service.init_support import InitError, InitRecord
from infermatrix_copilot.kb_service.init_stages import run_stage
from test_kb_init_skeleton import _runtime, _lifecycle, world  # noqa: F401
from test_kb_init_quick_maps import RULE_PAGE


def test_generic_stage_binds_policy_and_reuses_completed_result(world):
    rt = _runtime(world)
    first = run_stage(rt, _lifecycle(), 'skeleton', dry_run=True)
    assert first.status == 'dry_run', first.problems
    assert first.coverage['quick_map_policy_version'] == 2
    again = run_stage(rt, _lifecycle(), 'skeleton', dry_run=True)
    assert again.inputs_digest == first.inputs_digest and again.pr == first.pr


@pytest.mark.parametrize('version', [True, 0, 3, '2', None])
def test_invalid_persisted_policy_refuses_before_external_work(tmp_path, version):
    record = InitRecord(stage='skeleton', repo='toy', inputs_digest='old')
    record.coverage['quick_map_policy_version'] = version
    record.save(tmp_path)
    path = InitRecord.path(tmp_path, 'toy', 'skeleton')
    before = path.read_bytes()
    work = SimpleNamespace(rt=SimpleNamespace(state_dir=tmp_path),
                           lifecycle=SimpleNamespace(repo='toy'), STAGE='skeleton')
    with pytest.raises(InitError, match='unsupported quick-map policy'):
        init_execution.bind(work)
    assert path.read_bytes() == before


def test_capacity_check_uses_same_rule_lead_as_renderer(monkeypatch):
    captured = []
    monkeypatch.setattr(init_content, 'page_over_capacity', lambda text: captured.append(text) or None)
    routes = 'schema_version: 1\nowners:\n- {owner: core, path: repos/toy/rules.md, signals: [engine], scope_prefixes: [pkg/]}\n'
    for version in (1, 2):
        init_content.check_capacity_with_map({'repos/toy/rules.md': RULE_PAGE}, {'repos/toy/rules.md'},
            routes_text=routes, include_quickmaps=True, quick_map_policy_version=version)
    assert '规则触发：' not in captured[0] and '规则触发：' in captured[1]
