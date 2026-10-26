"""Workflow defaults, canonical adapters and durable execution policy agree."""
from dataclasses import replace
import asyncio
import json

import pytest

from infermatrix_copilot.app.completion_execution import completion_settings_for_run
from infermatrix_copilot.providers import llm_for, run_harness_session
from infermatrix_copilot.providers.registry import bind_transport_policy, transport_for_id
from infermatrix_copilot.trace_store import TraceStore
from test_completion_profile_configuration import configured, reserve  # noqa: F401
from test_readonly_sandbox import worker_receipt  # noqa: F401
from test_sandbox_agent_session import session  # noqa: F401


def test_completion_default_effort_is_captured_before_native_dispatch(configured):
    settings, calls = configured
    settings = settings.model_copy(update={'strict_backend_effort': 'high'})
    llm = llm_for(settings)
    llm.validate_completion()
    assert llm.create(system='rubric', messages=[{'role': 'user', 'content': 'source'}]).text
    rows = list(TraceStore(settings.trace_store_root).iter_records(kind='model_call'))
    assert len(calls) == len(rows) == 1
    assert rows[0][0]['model']['effort'] == 'high'


def test_session_default_effort_binds_the_original_request_and_receipt(session):
    settings, calls, request = session
    settings = settings.model_copy(update={'strict_backend_effort': 'high'})
    request = replace(request, effort='')
    outcome = run_harness_session(transport_for_id(settings, 'codex'), request)
    assert outcome.text and len(calls) == 1
    path, = request.run_dir.glob('native-sessions/*/session-intent.json')
    assert json.loads(path.read_bytes())['effort'] == 'high'
    store = TraceStore(path.parent / 'traces')
    record, = [r for r, _ in store.iter_records(kind='model_call')]
    assert record['model']['effort'] == 'high'
    store.validate_native_attempt(record)
    assert run_harness_session(transport_for_id(settings, 'codex'), request) == outcome
    assert len(calls) == 1


def test_unsupported_default_effort_refuses_before_model_or_host_probe(configured):
    settings, calls = configured
    settings = settings.model_copy(update={'strict_backend': 'cursor', 'strict_backend_effort': 'high',
                                           'native_completion_profiles': {}})
    with pytest.raises(ValueError, match='reasoning effort'):
        llm_for(settings).validate_completion()
    assert not calls


def test_native_pin_restores_original_effort(configured):
    settings, _ = configured
    old, _ = reserve(settings, 'empty-effort')
    old_bytes = (old / 'native-completion.json').read_bytes()
    explicit = settings.model_copy(update={'strict_backend_effort': 'high',
        'native_session_profiles': settings.native_completion_profiles})
    current, _ = reserve(explicit, 'new-v4')
    pin = json.loads((current / 'native-completion.json').read_bytes())
    assert pin['schema'] == 'run-completion/4' and pin['strict_backend_effort'] == 'high'
    changed = explicit.model_copy(update={'strict_backend_effort': 'low'})
    assert completion_settings_for_run(changed, current).strict_backend_effort == 'high'
    assert completion_settings_for_run(explicit, old).strict_backend_effort == ''
    assert (old / 'native-completion.json').read_bytes() == old_bytes


def test_transport_composition_is_context_local_and_follows_async_threads(configured):
    settings, calls = configured
    def policy(label):
        def prepare(transport):
            transport.test_policy = label
            return transport
        return prepare
    async def work(label):
        with bind_transport_policy(policy(label)):
            return await asyncio.to_thread(lambda: transport_for_id(settings, 'codex').test_policy)
    async def all_work():
        return await asyncio.gather(work('first'), work('second'))
    with bind_transport_policy(policy('outer')):
        assert asyncio.run(all_work()) == ['first', 'second']
        assert transport_for_id(settings, 'codex').test_policy == 'outer'
    assert not hasattr(transport_for_id(settings, 'codex'), 'test_policy') and not calls


def test_transport_composition_cannot_change_the_selected_backend(configured):
    from infermatrix_copilot.providers.cursor import CursorTransport
    settings, calls = configured
    with bind_transport_policy(lambda _: CursorTransport(settings)):
        with pytest.raises(ValueError, match='selected provider'):
            transport_for_id(settings, 'codex')
    assert not calls
