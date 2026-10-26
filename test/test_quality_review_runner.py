"""Shared quality execution preserves budgets, identity and durable sequencing."""
import hashlib
import json
from types import SimpleNamespace
import pytest

from infermatrix_copilot.kb_service.init_budget import Budget, BudgetExhausted
from infermatrix_copilot.kb_service.models import ModelReply, ModelRole, ModelUnavailable
from infermatrix_copilot.kb_service.quality_review import QualityPolicy, review_call, drain_review_tasks, checkpoint_review


ROLE = ModelRole('judge', 'codex', 'fixture')


@pytest.mark.parametrize('state', [None, [], {'wrong-key': {'attempt': 1}}, {'bad': None},
                                  {'bad': {'attempt': False}}, {'bad': {'attempt': 0}}])
def test_corrupt_parallel_dispatch_journal_is_preserved_before_new_work(state):
    from copy import deepcopy
    from infermatrix_copilot.kb_service.quality_review import DispatchJournal
    before, calls = deepcopy(state), []
    with pytest.raises(ModelUnavailable, match='reconcile retained state'):
        DispatchJournal(state, lambda task: calls.append('identify'), lambda update: calls.append('save'))
    assert state == before and calls == []


@pytest.mark.parametrize('state', [None, [], {'review_dispatched': 0}, {'review_dispatched': ''},
                                  {'review_dispatched': None}, {'review_attempts': -1},
                                  {'review_attempts': True}, {'checked': []}, {'checked': None}])
def test_corrupt_checkpoint_cannot_authorize_dispatch_or_overwrite(state):
    from copy import deepcopy
    original = deepcopy(state)
    calls = []
    with pytest.raises(ModelUnavailable, match='invalid execution checkpoint'):
        checkpoint_review(state, lambda: calls.append('dispatch'), lambda: calls.append('save'),
                          binding={'head': 'frozen'})
    assert calls == [] and state == original


def gateway(error=None):
    calls = []
    def call(role, **kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        data = {'verdict': 'pass'}
        kwargs['validate'](data)
        return ModelReply(role, data, '{}', 'fixture', {}, .1, None,
                          'trace', hashlib.sha256(b'{}').hexdigest())
    return SimpleNamespace(call_json=call, calls=calls)


def review(gate, budget, **kwargs):
    return review_call(gate, ROLE, policy=QualityPolicy('test'), system='rubric', prompt='frozen evidence',
                       validate=lambda _: None, budget=budget, charge_usd=.1, **kwargs)


def test_exhausted_budget_never_dispatches_and_unknown_cost_remains_explicit():
    gate = gateway()
    with pytest.raises(BudgetExhausted):
        review(gate, Budget(.01))
    assert not gate.calls
    budget = Budget(1)
    reply = review(gate, budget)
    assert budget.spent_usd == .1
    assert reply.quality_receipt['native']['cost_usd'] is None
    assert reply.quality_receipt['native']['usage'] == {}


def test_unavailable_call_is_accounted_once_and_not_replayed():
    gate = gateway(ModelUnavailable('unknown producer outcome'))
    budget = Budget(1)
    with pytest.raises(ModelUnavailable) as caught:
        review(gate, budget)
    assert budget.spent_usd == .1 and len(gate.calls) == 1
    assert caught.value.quality_receipt['outcome'] == 'unavailable'
    assert caught.value.quality_receipt['native'] is None


def test_missing_response_keeps_unknown_outcome_and_no_invented_receipt():
    error = ModelUnavailable('response lost', allow_fallback=False)
    error.native_call_sent = True
    error.native_call_completed = False
    gate = gateway(error)
    with pytest.raises(ModelUnavailable) as caught:
        review(gate, Budget(1))
    assert caught.value is error and not error.native_call_completed
    assert error.quality_receipt['outcome'] == 'unknown'
    assert error.quality_receipt['native'] is None and len(gate.calls) == 1


@pytest.mark.parametrize('kind', ['block', 'consistency'])
def test_review_keeps_lost_response_pending_across_checkpoint_resume(kind):
    from infermatrix_copilot.kb_service.gate import judge_block, check_consistency
    from infermatrix_copilot.knowledge_service.l1 import Block

    error = ModelUnavailable('native response lost', allow_fallback=False)
    error.native_call_sent = True
    error.native_call_completed = False
    gate = gateway(error)
    state = {}
    block = Block('prose', 'knowledge/demo.md', '', 'prose', 'fixture')

    def run():
        if kind == 'consistency':
            return {'owners': check_consistency({}, ['knowledge/one', 'knowledge/two'],
                                                 gateway=gate, judge=ROLE)}
        return judge_block(block, base={}, head={block.path: 'proposed content'},
                           evidence=[], gateway=gate, judge=ROLE).to_dict()

    with pytest.raises(ModelUnavailable, match='native response lost'):
        checkpoint_review(state, run, lambda: None, binding={'input': 'frozen'})
    assert state['review_dispatched'] is True and 'checked' not in state
    with pytest.raises(ModelUnavailable, match='reconcile original dispatch'):
        checkpoint_review(state, run, lambda: None, binding={'input': 'frozen'})
    assert len(gate.calls) == 1


@pytest.mark.parametrize('dependency', ['quality_review.py', 'container_worker.py', 'execution_environment.py'])
def test_init_policy_fingerprint_binds_shared_execution_dependencies(monkeypatch, dependency):
    from pathlib import Path
    from infermatrix_copilot.kb_service.init_execution import policy_digest

    baseline = policy_digest()
    original = Path.read_bytes

    def changed(path):
        data = original(path)
        return data + b'\n# changed execution policy\n' if path.name == dependency else data

    monkeypatch.setattr(Path, 'read_bytes', changed)
    assert policy_digest() != baseline


def test_quality_receipt_binds_policy_and_frozen_input():
    gate = gateway()
    first = review(gate, Budget(1)).quality_receipt
    second = review(gate, Budget(1)).quality_receipt
    changed = review_call(gate, ROLE, policy=QualityPolicy('test', version=2), system='rubric',
                          prompt='frozen evidence', validate=lambda _: None).quality_receipt
    assert first['input_sha256'] == second['input_sha256']
    assert first['input_sha256'] != changed['input_sha256']


@pytest.mark.parametrize('rejection', ['missing_archive', 'wrong_digest', 'evidence'])
def test_rejected_completed_review_retains_usage_without_accepting_proof(rejection):
    from dataclasses import replace

    gate = gateway()
    original = gate.call_json

    def call(*args, **kwargs):
        reply = original(*args, **kwargs)
        return replace(reply, usage={'input_tokens': 17, 'output_tokens': 3},
                       cost_usd=.25, trace_id='' if rejection == 'missing_archive' else 'trace',
                       reply_sha256='0' * 64 if rejection == 'wrong_digest' else reply.reply_sha256)

    gate.call_json = call

    def verify(_):
        if rejection == 'evidence':
            raise ModelUnavailable('evidence identity mismatch', allow_fallback=False)

    budget = Budget(1)
    with pytest.raises(ModelUnavailable) as caught:
        review_call(gate, ROLE, policy=QualityPolicy('test', archive='verified'),
                    system='rubric', prompt='frozen evidence', validate=lambda _: None,
                    budget=budget, charge_usd=.1, verify_reply=verify)

    error = caught.value
    assert error.native_call_sent and error.native_call_completed
    assert error.quality_receipt['outcome'] == 'unavailable'
    native = error.quality_receipt['native']
    assert native['usage'] == {'input_tokens': 17, 'output_tokens': 3}
    assert native['cost_usd'] == .25 and native['served_model'] == 'fixture'
    assert budget.spent_usd == .1 and len(gate.calls) == 1


def test_quality_receipt_carries_checkpoint_attempt_without_changing_input_identity():
    state, gate = {}, gateway()

    def run():
        return review(gate, Budget(1)).quality_receipt

    first = checkpoint_review(state, run, lambda: None, binding={"head": "frozen"}, reuse=lambda _: False)
    second = checkpoint_review(state, run, lambda: None, binding={"head": "frozen"}, reuse=lambda _: False)
    assert (first["attempt"], second["attempt"]) == (1, 2)
    assert first["call_index"] == second["call_index"] == 1
    assert first["input_sha256"] == second["input_sha256"]
    assert first["dispatch_sha256"] == second["dispatch_sha256"]


def test_parallel_quality_receipt_scopes_do_not_leak_between_workers():
    from infermatrix_copilot.kb_service.quality_review import DispatchJournal
    state, receipts = {}, {}
    journal = DispatchJournal(state, lambda task: {"id": task, "attempt": task + 1}, lambda update: update())

    def work(task):
        gate = gateway()
        return [review(gate, Budget(1)).quality_receipt for _ in range(2)]

    def settle(task, value, error):
        assert error is None
        receipts[task] = value

    drain_review_tasks(range(4), work, settle, concurrency=4, journal=journal)
    assert not state
    assert len({values[0]["dispatch_sha256"] for values in receipts.values()}) == 4
    for task, values in receipts.items():
        assert [v["attempt"] for v in values] == [task + 1, task + 1]
        assert [v["call_index"] for v in values] == [1, 2]


@pytest.mark.parametrize('fail_settlement', [False, True])
def test_parallel_settlement_retains_the_original_dispatch_binding(fail_settlement):
    from copy import deepcopy
    from infermatrix_copilot.kb_service.quality_review import DispatchJournal
    state, identified, saved = {}, [], []
    current = {'attempt': 1, 'evidence': {'head': 'original'}}

    def identify(task):
        identified.append(task)
        return current

    def checkpoint(update):
        update()
        if len(saved) == 1 and fail_settlement:
            raise OSError('result checkpoint unavailable')
        saved.append(deepcopy(state))

    def work(task):
        current['attempt'] = 2
        current['evidence']['head'] = 'changed'
        return 'completed original request'

    journal = DispatchJournal(state, identify, checkpoint)
    if fail_settlement:
        with pytest.raises(OSError):
            drain_review_tasks([0], work, lambda *args: None, concurrency=1, journal=journal)
        assert state == saved[0]
    else:
        drain_review_tasks([0], work, lambda *args: None, concurrency=1, journal=journal)
        assert state == {} and saved[-1] == {}
    assert identified == [0]
    original, = saved[0].values()
    assert original == {'attempt': 1, 'evidence': {'head': 'original'}}


@pytest.mark.parametrize('damage', ['missing', 'changed'])
def test_parallel_settlement_refuses_a_changed_durable_intent(damage):
    from copy import deepcopy
    from infermatrix_copilot.kb_service.quality_review import DispatchJournal
    state, calls, checkpoints = {}, [], []

    def save(update):
        update()
        checkpoints.append(deepcopy(state))

    def work(task):
        calls.append(task)
        key, = state
        if damage == 'missing':
            del state[key]
        else:
            state[key]['attempt'] = 2
        return 'native result'

    journal = DispatchJournal(state, lambda task: {'task': task, 'attempt': 1}, save)
    with pytest.raises(ModelUnavailable, match='reconcile retained state'):
        drain_review_tasks([0, 1], work, lambda *args: pytest.fail('settled changed identity'),
                           concurrency=1, journal=journal)
    assert calls == [0] and len(checkpoints) == 1


def test_durable_settlement_precedes_correction_dispatch():
    saved = []
    def work(attempt):
        assert len(saved) == attempt
        return attempt
    def settle(attempt, value, error):
        assert error is None
        saved.append(value)
        return [attempt + 1] if attempt < 3 else []
    assert drain_review_tasks([0], work, settle, concurrency=2)
    assert saved == [0, 1, 2, 3]


def test_checkpoint_failure_cannot_dispatch_followup():
    calls = []
    def settle(*_):
        raise OSError('checkpoint unavailable')
    with pytest.raises(OSError, match='checkpoint unavailable'):
        drain_review_tasks([0, 1], lambda n: calls.append(n), settle, concurrency=1)
    assert calls == [0]


def test_lost_judgment_response_keeps_intent_and_prevents_replay():
    from copy import deepcopy
    state, saved, calls = {}, [], []
    def run():
        calls.append(1)
        assert saved[-1]['review_dispatched'] is True
        error = ModelUnavailable('response lost')
        error.native_call_sent = True
        raise error
    save = lambda: saved.append(deepcopy(state))
    with pytest.raises(ModelUnavailable, match='response lost'):
        checkpoint_review(state, run, save, binding={'head': 'original'})
    with pytest.raises(ModelUnavailable, match='matching durable receipt'):
        checkpoint_review(state, run, save, binding={'head': 'original'})
    assert calls == [1]


def test_bound_completed_judgment_is_reused_but_stale_binding_is_refused():
    state = {}
    first = checkpoint_review(state, lambda: {'verdict':'pass'}, lambda: None, binding={'head':'original'})
    def no_dispatch():
        pytest.fail('completed review was dispatched again')
    assert checkpoint_review(state, no_dispatch, lambda: None, binding={'head':'original'}) == first
    # A changed head requires a fresh judgment, never reuse of the old result.
    assert checkpoint_review(state, lambda: {'verdict':'fail'}, lambda: None,
                             binding={'head':'changed'})['verdict'] == 'fail'


def test_failed_result_checkpoint_cannot_be_reused_or_redispatched():
    state, calls, saves = {}, [], []
    def save():
        saves.append(1)
        if len(saves) == 2:
            raise OSError('result checkpoint failed')
    def run():
        calls.append(1)
        return {'verdict':'pass'}
    with pytest.raises(OSError):
        checkpoint_review(state, run, save, binding={'head':'original'})
    with pytest.raises(ModelUnavailable, match='matching durable receipt'):
        checkpoint_review(state, run, save, binding={'head':'original'})
    assert calls == [1]


def test_checkpoint_freezes_dispatch_and_result_inputs_before_caller_mutation():
    from copy import deepcopy
    binding = {'evidence': {'head': 'original'}}
    state, saved = {}, []

    def run():
        binding['evidence']['head'] = 'changed'
        return {'verdict': 'pass'}

    result = checkpoint_review(state, run, lambda: saved.append(deepcopy(state)), binding=binding)
    original = {'evidence': {'head': 'original'}}
    assert result['evidence'] == original['evidence']
    retained = saved[0]['review_dispatched_binding']
    assert retained['inputs'] == original and retained['attempt'] == 1
    assert retained['sha256'] == hashlib.sha256(json.dumps(
        original, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert saved[-1]['review_dispatched_binding'] == retained


@pytest.mark.parametrize('damage', ['inputs', 'sha256', 'attempt', 'shape'])
def test_corrupt_checkpoint_binding_cannot_dispatch_or_replace_original(damage):
    from copy import deepcopy
    state = {}
    checkpoint_review(state, lambda: {'verdict': 'pass'}, lambda: None, binding={'head': 'original'})
    if damage == 'shape':
        state['review_dispatched_binding'] = None
    else:
        state['review_dispatched_binding'][damage] = {'head': 'changed'} if damage == 'inputs' else 'changed'
    before = deepcopy(state)
    calls = []
    with pytest.raises(ModelUnavailable, match='invalid execution binding'):
        checkpoint_review(state, lambda: calls.append('dispatch'), lambda: calls.append('save'),
                          binding={'head': 'changed'})
    assert calls == [] and state == before


def test_unresolved_legacy_checkpoint_does_not_invent_dispatch_binding():
    state = {'review_dispatched': True, 'review_attempts': 1}
    calls = []
    with pytest.raises(ModelUnavailable, match='reconcile original dispatch'):
        checkpoint_review(state, lambda: calls.append('dispatch'), lambda: calls.append('save'),
                          binding={'head': 'new'})
    assert calls == []
    assert state == {'review_dispatched': True, 'review_attempts': 1}


def test_completed_checkpoint_cannot_be_relabelled_to_new_inputs():
    from copy import deepcopy
    state = {}
    checkpoint_review(state, lambda: {'verdict': 'pass'}, lambda: None, binding={'head': 'original'})
    state['checked']['head'] = 'another-head'
    before = deepcopy(state)
    calls = []
    with pytest.raises(ModelUnavailable, match='differs from original execution binding'):
        checkpoint_review(state, lambda: calls.append('dispatch'), lambda: calls.append('save'),
                          binding={'head': 'another-head'})
    assert state == before and not calls


def test_draft_state_validation_uses_canonical_binding_validation():
    from infermatrix_copilot.kb_service.intake import _validate_draft_checkpoint
    state = {}
    checkpoint_review(state, lambda: {'draft': {}, 'trace_key': 'trace'}, lambda: None,
                      binding={'input_sha256': 'original'}, result_key='draft_result',
                      dispatch_key='draft_dispatched', attempts_key='draft_attempts')
    state['draft_dispatched_binding']['sha256'] = 'damaged'
    with pytest.raises(ModelUnavailable, match='invalid execution binding'):
        _validate_draft_checkpoint(state)
