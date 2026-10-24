"""Original discovery calls replay through the live policies without dispatch."""
from copy import deepcopy
from functools import partial
import json
from types import SimpleNamespace
import pytest

from infermatrix_copilot.kb_service.config import InitConfig
from infermatrix_copilot.kb_service.init_budget import Budget
from infermatrix_copilot.kb_service.init_feature_discovery import DiscoveryEngine, call_discovery_model
from infermatrix_copilot.kb_service.init_support import execution_snapshot
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole, ModelUnavailable
from infermatrix_copilot.kb_service.quality_review import native_receipt, verify_archived_receipt
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.llm import Block, Reply
from infermatrix_copilot.trace_store import TraceStore
from test_kb_feature_discovery import PIN, approve, candidate, index


def test_discovery_generation_and_review_replay_use_original_accounts(tmp_path):
    idx = index(tmp_path, docs=False)
    store, calls = TraceStore(tmp_path/'traces', environ={}), []
    def complete(**kwargs):
        calls.append(kwargs['role'])
        data = {'candidates': [candidate()]} if kwargs['role'] == 'generator' else approve([candidate()])
        return Reply(blocks=[Block(type='text', text=json.dumps(data))], model=kwargs['model'])
    gateway = ModelGateway(None, recorder=trace_recorder(store),
        transport_factory=lambda _: SimpleNamespace(complete=complete, subscription_billing=True))
    rt = SimpleNamespace(gateway=gateway, generator=ModelRole('generator', 'codex', 'fixture'),
                         judge=ModelRole('judge', 'codex', 'fixture'), subscription_generator=True,
                         unlimited_subscription=False, prices={})
    init, budget = InitConfig(), Budget(10, calls={})
    def verify(reply):
        verify_archived_receipt(store, native_receipt(reply), reply.role, reply.text)
    state = {}
    engine = DiscoveryEngine(idx, seeds=[], owners=['runtime'], state=state,
        call=partial(call_discovery_model, rt, budget, init, verify_reply=verify), save=lambda: None,
        concurrency=1, repository='owner/repo', pin=PIN, execution=execution_snapshot(rt, init))
    assert engine.scan() and engine.review()
    original_calls, original_accounts = list(calls), deepcopy(budget.calls)
    restored_state = json.loads(json.dumps(state, sort_keys=True))
    offline = DiscoveryEngine(idx, seeds=[], owners=[], state=restored_state, call=None,
                               save=lambda: None, repository='owner/repo', pin=PIN)
    results = {}
    for key, envelope in restored_state['dispatch_requests'].items():
        scope = key, envelope['binding']['attempt']
        request = offline.original_request(scope=scope)
        result, consumed = offline.replay_original_task(store, scope=scope)
        assert len(consumed) == 1
        context = request['context']
        application = DiscoveryEngine(idx, seeds=context['seeds'], owners=context['owners'],
            state=deepcopy(context['state']), call=None, save=lambda: None, repository='owner/repo', pin=PIN)
        if request['phase'] == 'extract':
            application._accept_extraction(request['task'], result)
            assert application.state['tasks'][request['task'][0]] == state['tasks'][request['task'][0]]
        else:
            application._accept_reviews(request['task'], result, context['policy'])
            assert application.state['reviews']['session'] == state['reviews']['session']
        results[request['phase']] = result
        interrupted = {**deepcopy(context['state']), 'dispatch_intents': {key: deepcopy(envelope['binding'])},
            'dispatch_requests': deepcopy(restored_state['dispatch_requests']),
            'dispatch_contexts': deepcopy(restored_state['dispatch_contexts'])}
        interrupted['candidates']['independent'] = candidate('independent')
        recovery = DiscoveryEngine(idx, seeds=[], owners=[], state=interrupted, call=None,
                                    save=lambda: None, repository='owner/repo', pin=PIN)
        before = deepcopy(interrupted)
        proposed, recovered_calls, digest = recovery.prepare_original_result(store, scope=scope)
        assert interrupted == before and key in proposed['dispatch_intents']
        assert proposed['candidates']['independent'] == interrupted['candidates']['independent']
        assert proposed['reconciled_dispatches'][key] == digest and recovered_calls == consumed
        repeated = DiscoveryEngine(idx, seeds=[], owners=[], state=deepcopy(proposed), call=None,
                                    save=lambda: None, repository='owner/repo', pin=PIN)
        assert repeated.prepare_original_result(store, scope=scope) == (proposed, recovered_calls, digest)
        if request['phase'] == 'extract':
            assert proposed['tasks'][request['task'][0]] == state['tasks'][request['task'][0]]
            interrupted['tasks'][request['task'][0]] = {'status': 'changed'}
        else:
            assert proposed['reviews']['session'] == state['reviews']['session']
            interrupted['candidates']['session']['title'] = 'changed after dispatch'
        changed = deepcopy(interrupted)
        with pytest.raises(ModelUnavailable, match='authority changed'):
            recovery.prepare_original_result(store, scope=scope)
        assert interrupted == changed
    assert set(results) == {'extract', 'review'}
    assert calls == original_calls and budget.calls == original_accounts
    assert restored_state == json.loads(json.dumps(state, sort_keys=True))
    # An extra started call without a receipt must retain uncertainty even
    # when all successful records would otherwise reproduce an accepted task.
    from infermatrix_copilot.kb_service.quality_review import _attempt_scope
    with _attempt_scope(*scope):
        store.begin_call({'role': 'judge', 'provider': 'codex', 'model': 'fixture',
                          'system': 'interrupted', 'prompt': 'interrupted'})
    before = deepcopy(restored_state)
    with pytest.raises(ModelUnavailable, match='missing or ambiguous native outcomes'):
        offline.replay_original_task(store, scope=scope)
    assert restored_state == before and calls == original_calls and budget.calls == original_accounts
