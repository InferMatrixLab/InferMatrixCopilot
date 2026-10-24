"""Coverage metrics do not own immutable execution and rendering authority."""
from copy import deepcopy
from infermatrix_copilot.kb_service.init_generation import replace_coverage
from infermatrix_copilot.kb_service.init_support import InitRecord
from infermatrix_copilot.kb_service.init_stages import run_stage
from test_kb_init_history import _setup, _runtime, _modules_lifecycle, world  # noqa: F401


def test_metric_replacement_preserves_original_execution_authority():
    record = InitRecord(stage='deepen', repo='toy', inputs_digest='frozen')
    record.coverage = {'quick_map_policy_version': 2, 'generation_execution': {'unit': {'unknown': True}},
                       'quality_reviews': {'unit': {'review_dispatched': True}}, 'old_metric': 1}
    authority = deepcopy(record.coverage)
    replace_coverage(record, {'new_metric': 2, 'quick_map_policy_version': 1})
    assert 'old_metric' not in record.coverage and record.coverage['new_metric'] == 2
    for key in ('quick_map_policy_version', 'generation_execution', 'quality_reviews'):
        assert record.coverage[key] == authority[key]


def test_modules_and_deepen_keep_policy_across_metric_updates_and_resume(world):
    gateway, _ = _setup(world)
    rt = _runtime(world, gateway)
    from infermatrix_copilot.kb_service.init_support import InitRecord
    for stage in ('modules', 'deepen'):
        old = InitRecord.load(rt.state_dir, 'toy', stage)
        assert old is not None and old.coverage['quick_map_policy_version'] == 2
        before = deepcopy(old.to_dict()) if hasattr(old, 'to_dict') else deepcopy(old.pr)
        resumed = run_stage(rt, _modules_lifecycle(), stage, dry_run=True)
        assert resumed.status == old.status and resumed.inputs_digest == old.inputs_digest
        assert resumed.pr == old.pr
        assert resumed.coverage['quick_map_policy_version'] == 2
