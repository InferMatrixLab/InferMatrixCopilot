"""A history unit must retain its dispatch across unavailable/uncertain replies."""
from copy import deepcopy
import json

import pytest

from infermatrix_copilot.kb_service.init_history import PAYLOADS_ENV
from infermatrix_copilot.kb_service.init_stages import run_stage
from infermatrix_copilot.kb_service.init_support import InitError, InitRecord
from infermatrix_copilot.kb_service.models import ModelGateway, ModelUnavailable
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.llm import Block, Reply
from infermatrix_copilot.trace_store import TraceStore
from test_kb_init_history import _setup, _runtime, _modules_lifecycle, world  # noqa: F401


def runtime(world, *, unknown=True):
    _, source = _setup(world)
    calls = []

    class Boundary:
        stops_at_spend = True
        subscription_billing = True

        def complete(self, **request):
            prompt = request['messages'][0]['content']
            payload = json.loads(prompt.split('\n', 1)[1].rsplit('</untrusted_data>', 1)[0])
            calls.append(payload['pr']['number'])
            if unknown and len(calls) == 1:
                error = ModelUnavailable('simulated interrupted original call', allow_fallback=False)
                error.native_call_sent = unknown
                error.native_call_completed = False
                error.native_outcome_pending = unknown
                raise error
            return Reply(blocks=[Block(type='text', text='{"rules":[],"not_adopted":["scripted"]}')],
                         model=request['model'], stop_reason='end_turn', usage={'cost_usd': .01})

    store = TraceStore(world['tmp']/'model-traces')
    first_factory = [True]
    def factory(_):
        if not unknown and first_factory[0]:
            first_factory[0] = False
            raise ModelUnavailable('simulated rejection before transport dispatch', allow_fallback=False)
        return Boundary()
    gateway = ModelGateway(None, transport_factory=factory, recorder=trace_recorder(store))
    return _runtime(world, gateway, github=source, environ={PAYLOADS_ENV: 'retain-private'}), calls, store


@pytest.mark.parametrize('subscription', [False, True])
def test_unknown_history_extraction_cannot_dispatch_again(world, subscription):
    rt, calls, store = runtime(world)
    first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True,
                      subscription_generator=subscription)
    assert first.status == 'blocked' and calls == [101]
    original = deepcopy(first.coverage['generation_execution'])
    assert original['history:101']['generation_dispatched'] is True
    assert original['history:101']['generation_attempts'] == 1
    native = list(store.iter_records(kind='model_call'))
    assert len(native) == 1 and native[0][0]['result']['native_outcome_pending'] is True
    again = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True,
                      subscription_generator=subscription)
    assert again.status == 'blocked' and 'reconcile original dispatch' in ' '.join(again.problems)
    assert calls == [101] and len(list(store.iter_records(kind='model_call'))) == 1
    assert again.coverage['generation_execution'] == original


@pytest.mark.parametrize('subscription', [False, True])
def test_legacy_unbound_unknown_keeps_original_checkpoint(world, subscription):
    rt, calls, _ = runtime(world)
    first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True,
                      subscription_generator=subscription)
    # This is the shape written by the old release: no per-PR intent.
    first.coverage.pop('generation_execution', None)
    path = first.save(rt.state_dir); before = path.read_bytes()
    with pytest.raises(InitError, match='legacy PR-history extraction has no durable intent'):
        run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True,
                  subscription_generator=subscription)
    assert calls == [101] and path.read_bytes() == before


def test_known_predispatch_refusal_can_retry_the_same_history_unit(world):
    rt, calls, _ = runtime(world, unknown=False)
    first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert first.status == 'blocked' and calls == []
    assert first.coverage['generation_execution']['history:101']['generation_dispatched'] is False
    second = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert second.status == 'empty' and calls == [101, 102, 103]
    assert second.coverage['generation_execution']['history:101']['generation_attempts'] == 2
    assert second.coverage['generation_execution']['history:101']['generation_dispatched'] is False


def test_previous_per_pr_unknown_intent_is_read_only_compatible(world, monkeypatch):
    import hashlib
    from infermatrix_copilot.kb_service import init_history
    rt, calls, _ = runtime(world)
    with monkeypatch.context() as patch:
        patch.setattr(init_history, 'HISTORY_SCOPE_POLICY_VERSION', 0)
        first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    current = first.coverage.pop('generation_execution')['history:101']
    request = current['generation_dispatched_binding']['inputs']
    pr = next(p for p in first.history['selected'] if p['number'] == 101)
    binding = {'number': 101, 'merge_commit_sha': pr['merge_commit_sha'],
               'upstream_pin': first.pin, 'batch_inputs_sha256': first.inputs_digest,
               'generator': rt.generator.label(), 'system_sha256': request['system_sha256'],
               'prompt_sha256': request['prompt_sha256'], 'payloads': 'retain-private'}
    state = {'extract_dispatched': True, 'extract_attempts': 1,
             'extract_dispatched_binding': {'inputs': binding, 'attempt': 1,
                 'sha256': hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}}
    first.history['extraction_execution'] = {'101': state}
    first.save(rt.state_dir)
    again = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert again.status == 'blocked' and calls == [101]
    assert again.history['extraction_execution']['101'] == state
