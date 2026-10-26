"""Native usage survives reply, session, accounting and trace boundaries."""
from types import SimpleNamespace

import pytest

from infermatrix_copilot.budgeting import bind_call_budget
from infermatrix_copilot.providers import run_harness_session
from infermatrix_copilot.providers.codex import CodexTransport


def turn(**usage):
    return {"type": "turn.completed", "usage": usage}


def history_usage():
    return CodexTransport._usage([turn(input_tokens=15889, output_tokens=1069,
        cached_input_tokens=11776, cache_write_input_tokens=0, reasoning_output_tokens=896)])


def test_native_cache_and_reasoning_counters_survive_both_result_shapes():
    usage = history_usage()
    expected = {"input_tokens": 15889, "input_tokens_include_cache": True, "output_tokens": 1069,
                "cache_read_input_tokens": 11776, "cache_creation_input_tokens": 0,
                "reasoning_output_tokens": 896}
    assert usage.reply("answer").usage == expected
    outcome = usage.outcome("answer", tool_calls=0)
    assert outcome.usage == expected
    assert outcome.input_tokens == 15889  # cached tokens are not added twice
    outcome.usage["input_tokens"] = 1
    assert usage.reply("answer").usage == expected


def test_multi_turn_usage_is_summed_and_unreported_fields_stay_unknown():
    usage = CodexTransport._usage([
        turn(input_tokens=10, output_tokens=2, cached_input_tokens=8),
        turn(input_tokens=20, output_tokens=3, cached_input_tokens=12)])
    assert usage.reply("").usage == {"input_tokens": 30, "input_tokens_include_cache": True, "output_tokens": 5,
                                     "cache_read_input_tokens": 20}
    assert "cache_creation_input_tokens" not in usage.reply("").usage


@pytest.mark.parametrize("counter", [None, -1, True, "12", 2.5])
def test_invalid_or_missing_counter_does_not_become_a_zero_or_partial_total(counter):
    usage = CodexTransport._usage([
        turn(input_tokens=10, cached_input_tokens=8),
        turn(input_tokens=20, cached_input_tokens=counter)])
    assert usage.reply("").usage == {"input_tokens": 30, "input_tokens_include_cache": True}


def test_missing_whole_turn_usage_and_no_events_remain_unknown():
    usage = CodexTransport._usage([turn(input_tokens=10), {"type": "turn.completed"}])
    assert usage.input_tokens == 10  # backward-compatible display counter
    assert usage.reply("").usage == {}
    assert CodexTransport._usage([]).outcome("", tool_calls=0).usage == {}


def test_session_accounting_and_capture_receive_the_same_reported_usage(monkeypatch):
    captures, settlements = [], []
    monkeypatch.setattr("infermatrix_copilot.llm.capture_model_call",
                        lambda *args, **kwargs: captures.append(args[3]))
    outcome = history_usage().outcome("answer", tool_calls=0)
    transport = SimpleNamespace(spec=SimpleNamespace(id="codex"), run_session=lambda _: outcome)
    request = SimpleNamespace(system="s", prompt="p", model="m", step_name="test")
    with bind_call_budget("test", lambda _: "ticket",
                          lambda ticket, facts: settlements.append(facts["usage"])):
        assert run_harness_session(transport, request) is outcome
    assert settlements == [outcome.usage]
    assert captures[0].usage == outcome.usage
    assert captures[0].usage["cache_read_input_tokens"] == 11776
    assert "cost_usd" not in captures[0].usage


def test_rejected_reply_still_settles_reported_native_cache_usage():
    from infermatrix_copilot.providers.completion import complete_native

    settlements = []

    def reject(*, native_event_sink, **kwargs):
        native_event_sink({"type": "native.codex.event", "payload": turn(
            input_tokens=10, output_tokens=2, cached_input_tokens=8)})
        raise RuntimeError("reply rejected")

    transport = SimpleNamespace(complete=reject, supports_native_events=True,
                                native_snapshot=CodexTransport.native_snapshot)
    with bind_call_budget("test", lambda _: "ticket",
                          lambda ticket, facts: settlements.append(dict(facts))):
        with pytest.raises(RuntimeError, match="reply rejected"):
            complete_native(lambda: transport, request={"system": "s", "messages": []})
    assert settlements[0]["sent"] is True
    assert settlements[0]["outcome"] == "failed"
    assert settlements[0]["usage"] == {"input_tokens": 10, "input_tokens_include_cache": True, "output_tokens": 2,
                                       "cache_read_input_tokens": 8}


def test_cache_subsets_are_not_billed_twice_and_legacy_disjoint_counts_still_work():
    from infermatrix_copilot.budgeting import usage_cost

    inclusive = {"input_tokens": 10, "output_tokens": 0,
                 "cache_read_input_tokens": 8, "cache_creation_input_tokens": 0,
                 "input_tokens_include_cache": True}
    assert usage_cost(inclusive, 1_000_000, 0) == pytest.approx(2.8)
    disjoint = dict(inclusive, input_tokens=2)
    del disjoint["input_tokens_include_cache"]
    assert usage_cost(disjoint, 1_000_000, 0) == pytest.approx(2.8)
    for unknown in [dict(inclusive, cache_read_input_tokens=11),
                    {k: v for k, v in inclusive.items() if k != "cache_read_input_tokens"},
                    dict(inclusive, input_tokens_include_cache="true")]:
        assert usage_cost(unknown, 1_000_000, 0) is None


@pytest.mark.parametrize("provider", ["claude", "cursor"])
@pytest.mark.parametrize("raw", [None, {}, {"input_tokens": True, "output_tokens": "2"}])
def test_other_adapters_do_not_fabricate_zero_usage(provider, raw):
    from infermatrix_copilot.providers.claude_code import ClaudeCodeTransport
    from infermatrix_copilot.providers.cursor import CursorTransport

    data = {"type": "result", "usage": raw}
    usage = (ClaudeCodeTransport._usage(data) if provider == "claude"
             else CursorTransport._usage([data]))
    assert usage.reply("OK").usage == {}
    assert usage.outcome("OK", tool_calls=0).usage == {}
    assert (usage.input_tokens, usage.output_tokens) == (0, 0)


def test_claude_session_and_completion_share_cache_and_cost_facts():
    from infermatrix_copilot.providers.claude_code import ClaudeCodeTransport

    raw = {"input_tokens": 8, "output_tokens": 2, "cache_read_input_tokens": 10}
    usage = ClaudeCodeTransport._usage({"usage": raw, "total_cost_usd": 0.25})
    expected = {**raw, "cost_usd": 0.25}
    assert usage.reply("OK").usage == expected
    assert usage.outcome("OK", tool_calls=0).usage == expected
    assert "cache_creation_input_tokens" not in expected


@pytest.mark.parametrize("cost", [True, -1, float("nan"), float("inf"), "0.25"])
def test_claude_invalid_cost_is_unknown(cost):
    from infermatrix_copilot.providers.claude_code import ClaudeCodeTransport

    usage = ClaudeCodeTransport._usage({"total_cost_usd": cost})
    assert usage.cost_usd is None
    assert usage.reply("OK").usage == {}


def test_cursor_zero_alias_and_missing_result_usage_are_not_partial_totals():
    from infermatrix_copilot.providers.cursor import CursorTransport

    usage = CursorTransport._usage([{"type": "result", "usage": {
        "inputTokens": 0, "input_tokens": 99, "outputTokens": 2}}])
    assert usage.reply("OK").usage == {"input_tokens": 0, "output_tokens": 2}
    partial = CursorTransport._usage([
        {"type": "result", "usage": {"inputTokens": 10, "outputTokens": 2}},
        {"type": "result"}])
    assert partial.reply("OK").usage == {}
    assert partial.input_tokens == 10  # legacy display total, not billing attestation


def test_zcode_result_and_recovery_share_the_latest_usage_snapshot():
    from infermatrix_copilot.providers.zcode import ZCodeTransport

    events = [{"type": "session.updated", "payload": {"modelId": "model"}},
              {"type": "result", "usage": {"inputTokens": 10, "outputTokens": 3}},
              {"type": "result", "usage": {"inputTokens": 20, "outputTokens": 5,
                  "cacheReadInputTokens": 8, "costUsd": 0.1}}]
    usage = ZCodeTransport._usage(events)
    expected = {"input_tokens": 20, "output_tokens": 5,
                "cache_read_input_tokens": 8, "cost_usd": 0.1}
    assert usage.reply("OK").usage == expected
    assert usage.outcome("OK", tool_calls=0).usage == expected
    assert ZCodeTransport.native_snapshot(events)["usage"] == expected
    assert usage.input_tokens == 20  # cumulative, not 10 + 20
    events.append({"type": "result"})
    assert ZCodeTransport.native_snapshot(events)["usage"] == {}


def test_zcode_malformed_counters_remain_unknown_in_both_decoders():
    from infermatrix_copilot.providers.zcode import ZCodeTransport

    events = [{"type": "result", "usage": {"inputTokens": -1,
               "outputTokens": 1.5, "cacheReadInputTokens": True, "costUsd": float("nan")}}]
    assert ZCodeTransport._usage(events).reply("OK").usage == {}
    assert ZCodeTransport.native_snapshot(events)["usage"] == {}


def test_deepseek_native_usage_is_counted_once_and_recovery_uses_same_decoder():
    from infermatrix_copilot.providers.deepseek import DeepSeekHarnessTransport as DSH

    raw = {"inputTokens": 10, "outputTokens": 2, "cacheReadInputTokens": 8, "costUsd": .1}
    chunk = {"type": "assistant/chunk", "data": {"turn": 1, "step": 1,
             "chunk": {"type": "usage", "usage": raw}}}
    message = {"type": "assistant/message", "data": {"turn": 1, "step": 1,
               "usage": raw, "message": {"source": {"model": "served"}}}}
    events = [chunk, message, message, {"type": "turn/end", "data": {"usage": raw}}]
    _, _, usage = DSH._activity(events)
    expected = {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 8, "cost_usd": .1}
    assert usage.reply("OK").usage == expected
    assert usage.outcome("OK", tool_calls=0).usage == expected
    assert DSH.native_snapshot([{"type": "native.deepseek.event", "payload": e}
                               for e in events])["usage"] == expected
    assert usage.served_model == "served"
    assert DSH._activity([chunk])[2].reply("").usage == expected


def test_deepseek_missing_step_counters_do_not_become_partial_complete_totals():
    from infermatrix_copilot.providers.deepseek import DeepSeekHarnessTransport as DSH

    events = [{"type": "assistant/message", "data": {"turn": 1, "step": 1,
               "usage": {"inputTokens": 10, "outputTokens": 2, "costUsd": .1}}},
              {"type": "assistant/message", "data": {"turn": 1, "step": 2,
               "usage": {"inputTokens": 20, "outputTokens": True}}}]
    usage = DSH._activity(events)[2]
    assert usage.reply("").usage == {"input_tokens": 30}
    assert usage.cost_usd is None
    events.append({"type": "assistant/message", "data": {"usage": {"inputTokens": 40}}})
    assert DSH._activity(events)[2].reply("").usage == {}


def test_unrepresentable_native_cost_is_unknown():
    from infermatrix_copilot.providers.base import SessionUsage

    usage = SessionUsage()
    usage.set_reported_cost(10 ** 400)
    assert usage.cost_usd is None
    assert usage.reply("").usage == {}


@pytest.mark.parametrize('provider', ['codex', 'cursor', 'claude-code', 'deepseek', 'zcode'])
@pytest.mark.parametrize('failure', ['trace', 'observer_interrupt'])
def test_agent_session_failure_preserves_native_usage(tmp_path, monkeypatch, provider, failure):
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.providers.base import AgentSessionRequest
    from infermatrix_copilot.providers.registry import transport_for_id
    from infermatrix_copilot.scopes import read_only_scope

    transport = transport_for_id(Settings(_env_file=None), provider)
    monkeypatch.setattr(transport, 'require_cli', lambda: 'offline-fixture')
    if provider == 'codex':
        native = turn(input_tokens=10, output_tokens=2)
    elif provider == 'deepseek':
        native = {'type': 'assistant/message', 'data': {'turn': 1, 'step': 1,
                  'usage': {'inputTokens': 10, 'outputTokens': 2}}}
    else:
        native = {'type': 'result', 'is_error': False, 'result': 'OK', 'response': 'OK',
                  'usage': ({'input_tokens': 10, 'output_tokens': 2} if provider == 'claude-code'
                            else {'inputTokens': 10, 'outputTokens': 2})}
    event = native if provider == 'zcode' else {'type': f'native.{provider}.event', 'payload': native}

    def dispatch(*args, native_event_sink, **kwargs):
        native_event_sink(event)
        if provider == 'deepseek':
            return SimpleNamespace(events=[native], final_response='OK', finish_reason='completed', error=None)
        return (native if provider == 'claude-code' else [native]), False

    monkeypatch.setattr(transport, '_invoke' if provider == 'deepseek' else '_run', dispatch)
    if provider == 'deepseek':
        monkeypatch.setattr(transport, '_credential', lambda: ('offline-key', None))
    observed, settlements, captures = [], [], []

    def observer(event):
        observed.append(event)
        if failure == 'observer_interrupt':
            raise KeyboardInterrupt('receipt interrupted')

    def trace(*args, **kwargs):
        raise RuntimeError('trace unavailable')

    request = AgentSessionRequest(system='s', prompt='p', scope=read_only_scope(), model='',
                                  max_iters=2, timeout_s=5, run_dir=tmp_path,
                                  trace=SimpleNamespace(record=trace), native_event_sink=observer)
    monkeypatch.setattr('infermatrix_copilot.llm.capture_model_call',
                        lambda *args, **kwargs: captures.append(kwargs))
    with bind_call_budget('test', lambda _: 'ticket',
                          lambda ticket, facts: settlements.append(dict(facts))):
        with pytest.raises(KeyboardInterrupt if failure == 'observer_interrupt' else RuntimeError,
                           match='receipt interrupted|trace unavailable'):
            run_harness_session(transport, request)
    assert request.native_event_sink is observer
    assert observed == [event]
    assert len(settlements) == len(captures) == 1
    expected = {'input_tokens': 10, 'output_tokens': 2}
    if provider == 'codex':
        expected['input_tokens_include_cache'] = True
    assert settlements[0]['sent'] is True
    assert settlements[0]['usage'] == expected
    assert settlements[0]['outcome'] != 'completed'
    assert captures[0]['extra_result']['reported_usage'] == expected


def test_session_refusal_after_native_receipt_settles_partial_usage():
    observed = []
    outcome = CodexTransport._usage([]).outcome('', tool_calls=0, refusals=['worker response lost'])

    def dispatch(request):
        request.native_event_sink({'type': 'native.codex.event',
                                   'payload': turn(input_tokens=10, output_tokens=2)})
        return outcome

    transport = SimpleNamespace(spec=SimpleNamespace(id='codex'), run_session=dispatch,
                                native_snapshot=CodexTransport.native_snapshot)
    request = SimpleNamespace(system='s', prompt='p', model='m', step_name='test')
    with bind_call_budget('test', lambda _: 'ticket',
                          lambda ticket, facts: observed.append(dict(facts))):
        assert run_harness_session(transport, request) is outcome
    assert not hasattr(request, 'native_event_sink')
    assert len(observed) == 1
    assert observed[0]['outcome'] == 'failed'
    assert observed[0]['usage'] == {'input_tokens': 10, 'output_tokens': 2,
                                    'input_tokens_include_cache': True}


@pytest.mark.parametrize('entry', ['session', 'completion'])
def test_failed_native_call_trace_retains_usage_without_fabricated_reply(tmp_path, entry):
    from infermatrix_copilot.providers.completion import complete_native
    from infermatrix_copilot.trace_store import TraceStore, bind_store

    event = {'type': 'native.codex.event', 'payload': turn(input_tokens=10, output_tokens=2)}

    def fail(sink):
        sink(event)
        raise RuntimeError('worker lost')

    transport = SimpleNamespace(spec=SimpleNamespace(id='codex'), supports_native_events=True,
                                native_snapshot=CodexTransport.native_snapshot,
                                run_session=lambda request: fail(request.native_event_sink),
                                complete=lambda native_event_sink, **kwargs: fail(native_event_sink))
    store = TraceStore(tmp_path, environ={})
    with bind_store(store), pytest.raises(RuntimeError, match='worker lost'):
        if entry == 'session':
            run_harness_session(transport, SimpleNamespace(system='s', prompt='p', model='m', step_name='test'))
        else:
            complete_native(lambda: transport, request={'system': 's', 'messages': []}, capture=True)
    records = store.query(kind='model_call')
    assert len(records) == 1
    assert records[0]['usage'] == {'input_tokens': 10, 'output_tokens': 2,
                                   'input_tokens_include_cache': True}
    assert records[0]['outputs'] == {}
    assert records[0]['error'] == 'worker lost'
