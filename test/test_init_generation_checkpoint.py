"""Unscoped generation shares durable intent, including across units and resumes."""
import pytest

from infermatrix_copilot.kb_service.models import ModelGateway, ModelUnavailable
from infermatrix_copilot.kb_service.init_stages import run_stage
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.trace_store import TraceStore
from test_kb_init_skeleton import world, _runtime, _lifecycle  # noqa: F401
from test_kb_init_modules import _world_with_tools, _skeleton, _modules_lifecycle
from test_kb_init_deepen import CodeGateway, _churn, _chain


@pytest.mark.parametrize('stage', ['skeleton', 'modules', 'deepen'])
@pytest.mark.parametrize('subscription', [False, True])
def test_original_unknown_generation_never_dispatches_on_resume(world, stage, subscription):
    if stage != 'skeleton':
        _world_with_tools(world)
        if stage == 'modules':
            _skeleton(world)
        else:
            _churn(world)
            _chain(world, CodeGateway())
    calls = []

    class Boundary:
        stops_at_spend = True
        subscription_billing = True

        def complete(self, **request):
            calls.append(request)
            error = ModelUnavailable('simulated unknown original', allow_fallback=False)
            error.native_call_sent = True
            error.native_call_completed = False
            error.native_outcome_pending = True
            raise error

    store = TraceStore(world['tmp']/'traces')
    gateway = ModelGateway(None, transport_factory=lambda _: Boundary(), recorder=trace_recorder(store))
    rt = _runtime(world, gateway)
    lifecycle = _lifecycle() if stage == 'skeleton' else _modules_lifecycle()
    first = run_stage(rt, lifecycle, stage, dry_run=True, subscription_generator=subscription)
    assert first.status == 'blocked' and len(calls) == 1
    second = run_stage(rt, lifecycle, stage, dry_run=True, subscription_generator=subscription)
    assert second.status == 'blocked' and len(calls) == 1
    assert 'reconcile original dispatch' in ' '.join(second.problems)
    records = list(store.iter_records(kind='model_call'))
    assert len(records) == 1 and records[0][0]['result']['native_outcome_pending'] is True


def test_cached_generation_is_detached_from_caller_mutation(monkeypatch):
    from types import SimpleNamespace
    from infermatrix_copilot.kb_service.init_generation import generate_data
    from infermatrix_copilot.kb_service.models import ModelRole
    from infermatrix_copilot.kb_service import init_support
    calls = []
    monkeypatch.setattr(init_support, 'generate', lambda *a, **k:
                        calls.append(k) or SimpleNamespace(data={'rules': ['original']}))
    record = SimpleNamespace(coverage={}, inputs_digest='frozen', save=lambda _: None)
    work = SimpleNamespace(record=record, rt=SimpleNamespace(generator=ModelRole('generator', 'test', 'test'), state_dir=None),
                           budget=None, lifecycle=SimpleNamespace(init=None))
    first = generate_data(work, system='s', prompt='p', unit='u')
    first['rules'].append('mutated')
    assert generate_data(work, system='s', prompt='p', unit='u') == {'rules': ['original']}
    assert len(calls) == 1
