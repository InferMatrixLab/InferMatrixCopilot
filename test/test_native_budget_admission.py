"""Native dollar reservations use execution facts and the original domain accounts."""
import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.budgeting import bind_call_budget, call_budget
from infermatrix_copilot.improve.budget import BudgetBreach, BudgetRefused, Governor, governed, judge_governed
from infermatrix_copilot.llm import Block, Reply
from infermatrix_copilot.providers.completion import complete_native

LIMITS = {'test:model': {'stop_usd': .5, 'overshoot_usd': .25}}


def governor(tmp_path, *, limits=LIMITS, usd=5):
    return Governor(tmp_path, usd_week=usd, judge_calls_week=5,
                    settings=Settings(_env_file=None, improve_native_call_limits=limits))


class Transport:
    spec = SimpleNamespace(id='test')
    stops_at_spend = True

    def __init__(self, gov, *, cost=.2, fail=False):
        self.gov, self.cost, self.fail, self.calls = gov, cost, fail, []

    def complete(self, **kwargs):
        assert self.gov.remaining()['usd_reserved'] > 0
        self.calls.append(kwargs)
        if self.fail:
            raise OSError('fixture process failed')
        return Reply(blocks=[Block(type='text', text='fixture')], usage={'cost_usd': self.cost},
                     stop_reason='end_turn', model='model')


def run(transport, *, request=None):
    with governed(transport.gov):
        return complete_native(lambda: transport, request=request or {'model': 'model'})


def test_native_stop_and_overshoot_are_reserved_before_dispatch(tmp_path):
    gov = governor(tmp_path)
    transport = Transport(gov)
    _, _, record, _ = run(transport)
    assert transport.calls[0]['max_budget_usd'] == .5
    assert record['max_budget_usd'] == .5
    assert gov.remaining()['usd_settled'] == .2 and gov.remaining()['usd_reserved'] == 0


@pytest.mark.parametrize('limits,capability,usd', [({}, True, 5), (LIMITS, False, 5), (LIMITS, True, .7)])
def test_unknown_bounds_unsupported_capability_and_exhausted_account_do_not_dispatch(tmp_path, limits, capability, usd):
    gov = governor(tmp_path, limits=limits, usd=usd)
    transport = Transport(gov)
    transport.stops_at_spend = capability
    with pytest.raises(BudgetRefused):
        run(transport)
    assert transport.calls == [] and gov.remaining()['usd_reserved'] == 0


@pytest.mark.parametrize('cost,fail', [(None, False), (float('nan'), False), (.2, True)])
def test_unknown_native_price_keeps_the_whole_original_reservation(tmp_path, cost, fail):
    gov = governor(tmp_path)
    transport = Transport(gov, cost=cost, fail=fail)
    if fail:
        with pytest.raises(OSError):
            run(transport)
    else:
        run(transport)
    assert gov.remaining()['usd_settled'] == .75 and gov.remaining()['usd_reserved'] == 0


def test_actual_native_overspend_is_recorded_and_refuses_another_dispatch(tmp_path):
    gov = governor(tmp_path)
    transport = Transport(gov, cost=1.2)
    with pytest.raises(BudgetBreach):
        run(transport)
    assert gov.remaining()['usd_settled'] == 1.2
    with pytest.raises(BudgetRefused):
        run(transport)
    assert len(transport.calls) == 1


def test_caller_smaller_threshold_is_preserved_and_pre_dispatch_failure_refunds(tmp_path):
    gov = governor(tmp_path)
    transport = Transport(gov)
    run(transport, request={'model': 'model', 'max_budget_usd': .1})
    assert transport.calls[0]['max_budget_usd'] == .1
    def reject(ticket, request, execution):
        raise ValueError('second domain refuses')
    with bind_call_budget('other', lambda request: None, lambda ticket, facts: None, prepare=reject):
        with pytest.raises(ValueError, match='second domain'):
            run(transport)
    assert len(transport.calls) == 1 and gov.remaining()['usd_reserved'] == 0


def test_native_dispatch_requires_once_only_account_preparation(tmp_path):
    from infermatrix_copilot.trace_store import TraceStore
    gov = governor(tmp_path)
    archive = TraceStore(tmp_path / 'trace').begin_call({'model': 'model'})
    binding = {'schema': 'native-budget-binding/1', 'trace_root': str(archive.store.root),
               'attempt_id': archive.id, **{key: archive.meta[key] for key in ('model', 'context', 'env', 'inputs')}}
    with governed(gov), call_budget({'kind': 'harness', 'model': 'model'}) as facts:
        with pytest.raises(RuntimeError, match='before dispatch'):
            facts.update(sent=True)
        assert facts['sent'] is False
        assert facts.prepare({'provider_id': 'test', 'stops_at_spend': True,
                              'native_binding': binding}) == {'max_budget_usd': .5}
        with pytest.raises(RuntimeError, match='repeated'):
            facts.prepare({'provider_id': 'test', 'stops_at_spend': True})
    assert gov.remaining()['usd_reserved'] == 0


def test_legacy_judge_count_policy_is_explicit_and_not_a_dollar_invoice(tmp_path):
    gov = governor(tmp_path, limits={})
    native = SimpleNamespace(complete=lambda **kwargs: Reply(blocks=[Block(type='text', text='judge')],
                                                           usage={'cost_usd': 2}, model='unpriced'))
    with governed(gov), judge_governed(gov):
        complete_native(lambda: native, request={'model': 'unpriced', 'system': 'frozen', 'messages': []},
                        identity={'provider': 'test'})
    assert gov.remaining()['judge_calls_used'] == 1
    assert gov.remaining()['usd_settled'] == 0


def test_another_domains_judge_account_cannot_waive_the_dollar_account(tmp_path):
    gov, other = governor(tmp_path / 'one', limits={}), governor(tmp_path / 'two', limits={})
    with governed(gov), judge_governed(other):
        with pytest.raises(BudgetRefused):
            with call_budget({'kind': 'harness', 'model': 'model'}) as facts:
                facts.prepare({'provider_id': 'test', 'stops_at_spend': True})
    assert gov.remaining()['usd_settled'] == 0 and other.remaining()['judge_calls_used'] == 1


def test_admitted_subscription_is_a_domain_waiver_not_a_measured_zero_cost(tmp_path):
    gov = governor(tmp_path, limits={})
    with governed(gov), call_budget({'kind': 'harness_session', 'model': ''}) as facts:
        assert facts.prepare({'subscription_admitted': True, 'stops_at_spend': False}) == {}
        facts.update(sent=True, usage=None)
    assert gov.remaining()['usd_settled'] == 0


@pytest.mark.parametrize('limits', [[], {'test': LIMITS['test:model']}, {'test:': LIMITS['test:model']},
                                  {'test:model': {'stop_usd': .5}},
                                  {'test:model': {'stop_usd': True, 'overshoot_usd': .2}},
                                  {'test:model': {'stop_usd': float('inf'), 'overshoot_usd': .2}},
                                  {'test:model': {'stop_usd': .5, 'overshoot_usd': 0}}])
def test_invalid_native_policy_is_refused_at_configuration(limits):
    with pytest.raises(ValueError, match='native call limits'):
        Settings(_env_file=None, improve_native_call_limits=limits)


def test_shadow_native_limits_are_frozen_and_cannot_be_overridden(tmp_path):
    from infermatrix_copilot.improve.shadow import shadow_env, assert_boundaries
    env = shadow_env(shadow_dir=tmp_path, run_dir=tmp_path, trace_root=tmp_path,
                     executables_dir=tmp_path, repo_name='demo', ledger_dir=tmp_path,
                     native_call_limits=LIMITS,
                     environ={'IMPROVE_NATIVE_CALL_LIMITS': '{}'})
    expected = {'IMPROVE_NATIVE_CALL_LIMITS': json.dumps(LIMITS, sort_keys=True)}
    assert not assert_boundaries(env, expected=expected)
    env['IMPROVE_NATIVE_CALL_LIMITS'] = '{}'
    assert assert_boundaries(env, expected=expected)


def test_native_session_uses_the_same_admission_before_its_runner(tmp_path):
    from infermatrix_copilot.providers import run_harness_session
    from infermatrix_copilot.providers.base import AgentSessionRequest
    from infermatrix_copilot.scopes import read_only_scope
    gov = governor(tmp_path)
    calls = []
    transport = SimpleNamespace(spec=SimpleNamespace(id='test'), stops_at_spend=True,
                                run_session=lambda request: calls.append(request))
    request = AgentSessionRequest(step_name='review', system='fixture', prompt='fixture', model='model',
                                  scope=read_only_scope(), max_iters=1, timeout_s=1, run_dir=tmp_path)
    with governed(gov), pytest.raises(BudgetRefused, match='cannot enforce'):
        run_harness_session(transport, request)
    assert calls == [] and gov.remaining()['usd_reserved'] == 0


def test_budget_refusal_keeps_its_domain_exception_and_cannot_allow_fallback(tmp_path):
    gov = governor(tmp_path, limits={})
    transport = Transport(gov)
    with governed(gov), pytest.raises(BudgetRefused) as caught:
        complete_native(lambda: transport, request={'model': 'model'},
                        make_error=lambda message, allow_fallback: RuntimeError('must not translate'))
    assert caught.value.native_call_sent is False
    assert caught.value.call_budget_failure is True
    assert transport.calls == []


def test_execution_changes_are_validated_again_before_dispatch_and_refund_the_reservation(tmp_path):
    gov = governor(tmp_path)
    transport = Transport(gov)
    observed = []
    def validate(request):
        observed.append(dict(request))
        if 'max_budget_usd' in request:
            raise ValueError('fixture rejects native cap argument')
    transport.validate_completion_request = validate
    with pytest.raises(ValueError, match='rejects native cap'):
        run(transport)
    assert len(observed) == 2 and transport.calls == []
    assert gov.remaining()['usd_reserved'] == 0 and gov.remaining()['usd_settled'] == 0


def test_preparation_requires_a_registered_callback_and_finalizes_prior_accounts():
    from infermatrix_copilot.improve.budget import NativeReservation
    finished = []
    with bind_call_budget('native', lambda request: NativeReservation('model'),
                          lambda ticket, facts: finished.append(facts['sent'])):
        with pytest.raises(RuntimeError, match='no preparation callback'):
            with call_budget({'kind': 'harness'}):
                pytest.fail('unprepared context must not open')
    assert finished == [False]
