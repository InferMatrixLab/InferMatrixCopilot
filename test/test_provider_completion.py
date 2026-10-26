"""Native completion preserves evidence and records the final schema verdict."""

import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole, ModelUnavailable
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.providers.completion import complete_native
from infermatrix_copilot.trace_store import TraceStore


@pytest.mark.parametrize("provider", ["cursor", "codex", "claude-code"])
@pytest.mark.parametrize("event", [
    {"type": "tool_call", "tool_call": {"readToolCall": {"args": {"path": "source.py"}}}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
    {"type": "assistant", "message": {"content": [{"type": "future_unknown_block"}]}},
    {"type": "item.completed", "item": {"type": "future_unknown_item"}},
])
def test_native_tool_less_contract_rejects_read_tools_and_unknown_blocks(provider, event, monkeypatch):
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.providers.registry import transport_for_id

    transport = transport_for_id(Settings(_env_file=None), provider)
    data = event if provider == "claude-code" else [event]
    monkeypatch.setattr(transport, "_run", lambda *args, **kwargs: (data, False))
    # Preserve the event-audit contract even for adapters now rejected before
    # dispatch; their historical events must still be recognized as tool use.
    from infermatrix_copilot.providers.audit import assert_tool_less
    with pytest.raises(RuntimeError, match="tool calls"):
        assert_tool_less([event])
    if provider == "codex":
        with pytest.raises(ValueError, match="builtin_tools_off"):
            transport.complete(system="blind judge", messages=[])
    else:
        with pytest.raises(RuntimeError, match="tool calls"):
            transport.complete(system="blind judge", messages=[])


def test_native_completion_budget_receives_paid_usage_after_validation_failure():
    from infermatrix_copilot.budgeting import bind_call_budget

    observed = []
    paid = reply()
    paid.usage = {"input_tokens": 8, "output_tokens": 2}
    transport = SimpleNamespace(complete=lambda **kwargs: paid)
    def reject(_):
        raise ValueError("invalid conclusion")
    with bind_call_budget("domain", lambda request: "ticket", lambda ticket, facts: observed.append(dict(facts))):
        with pytest.raises(ValueError):
            complete_native(lambda: transport, request={"system": "s", "messages": [], "model": "pinned-model"},
                            validate=reject)
    assert observed == [{"sent": True, "reply": paid, "usage": paid.usage, "outcome": "failed"}]


def test_partial_usage_settles_without_an_archive_or_pacing_policy():
    from infermatrix_copilot.budgeting import bind_call_budget

    observed = []

    class Transport:
        supports_native_events = True

        def complete(self, *, native_event_sink, **kwargs):
            native_event_sink({"usage": {"cost_usd": 1.25}})
            raise OSError("response lost")

        def native_snapshot(self, events):
            return {"usage": events[0]["usage"]}

    with bind_call_budget("domain", lambda request: "ticket", lambda ticket, facts: observed.append(dict(facts))):
        with pytest.raises(OSError, match="response lost") as raised:
            complete_native(Transport, request={"system": "s", "messages": []})
    assert raised.value.native_call_sent and not raised.value.native_call_completed
    assert observed == [{"sent": True, "reply": None, "usage": {"cost_usd": 1.25}, "outcome": "failed"}]


@pytest.mark.parametrize('failure_at', ['record', 'archive_finish', 'settlement'])
@pytest.mark.parametrize('completed', [False, True])
def test_finalization_failure_retains_dispatch_and_never_calls_generator_fallback(failure_at, completed):
    from infermatrix_copilot.budgeting import bind_call_budget

    calls, settled = [], []
    failure = ModelUnavailable('finalization unavailable')
    # A callback may rethrow an error carrying metadata from another operation.
    # Only this boundary's own classification may authorize a fallback.
    failure.native_call_sent = False
    failure.native_call_completed = False

    class Transport:
        def complete(self, **kwargs):
            calls.append(kwargs['model'])
            if not completed:
                raise OSError('native response lost')
            return reply()

    class Recorder:
        def begin_call(self, identity):
            def finish(*args, **kwargs):
                if failure_at == 'archive_finish':
                    raise failure
            return SimpleNamespace(payload=lambda: {}, finish=finish)

        def __call__(self, entry):
            if failure_at == 'record':
                raise failure
            return {}

    def settle(ticket, facts):
        settled.append(dict(facts))
        if failure_at == 'settlement':
            raise failure

    role = ModelRole('generator', 'fixture', 'primary',
                     fallback=ModelRole('generator', 'fixture', 'fallback'))
    gateway = ModelGateway(None, transport_factory=lambda _: Transport(), recorder=Recorder())
    with bind_call_budget('test', lambda _: 'ticket', settle):
        with pytest.raises(ModelUnavailable, match='finalization unavailable') as caught:
            gateway.call_json(role, system='s', prompt='evidence')
    assert caught.value is failure
    assert failure.native_call_sent and failure.native_call_completed is completed
    assert failure.allow_fallback is False
    assert calls == ['primary'] and len(settled) == 1
    assert settled[0]['sent'] is True


def test_pacing_settlement_failure_retains_completed_usage():
    from infermatrix_copilot.budgeting import bind_call_budget

    paid = reply()
    paid.usage = {'input_tokens': 8, 'output_tokens': 2}
    failure = ModelUnavailable('pacing journal unavailable')
    observed = []

    def finish(*args, **kwargs):
        raise failure

    pacer = SimpleNamespace(acquire=lambda sink: 'ticket', stop_file=None, finish=finish)
    transport = SimpleNamespace(supports_native_events=True, complete=lambda **_: paid)
    with bind_call_budget('test', lambda _: 'ticket', lambda ticket, facts: observed.append(dict(facts))):
        with pytest.raises(ModelUnavailable) as caught:
            complete_native(lambda: transport, request={'system': 's', 'messages': []}, pacer=pacer)
    assert caught.value is failure
    assert failure.native_call_sent and failure.native_call_completed
    assert failure.allow_fallback is False
    assert len(observed) == 1 and observed[0]['reply'] is paid
    assert observed[0]['usage'] == paid.usage and observed[0]['outcome'] == 'failed'


@pytest.mark.parametrize("provider", ["cursor", "claude-code"])
@pytest.mark.parametrize("ending", ["success", "native_error", "policy_rejection", "archive_failure"])
def test_adapter_completion_retains_usage_before_rejection(provider, ending, monkeypatch):
    from infermatrix_copilot.budgeting import bind_call_budget
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.providers.registry import transport_for_id

    transport = transport_for_id(Settings(_env_file=None), provider)
    monkeypatch.setattr(transport, "require_cli", lambda: "offline-fixture")
    usage = ({"inputTokens": 10, "outputTokens": 2} if provider == "cursor"
             else {"input_tokens": 10, "output_tokens": 2})
    data = {"type": "result", "subtype": "success", "is_error": False,
            "result": "OK", "usage": usage}
    if ending == "native_error":
        data.update(is_error=True, subtype="error_during_execution")
    elif ending == "policy_rejection":
        data["message"] = {"content": [{"type": "tool_use", "name": "Read"}]}
    if provider == "claude-code":
        data["total_cost_usd"] = 0.21
    module = "cursor" if provider == "cursor" else "claude_code"
    monkeypatch.setattr(f"infermatrix_copilot.providers.{module}.run_cli",
                        lambda *args, **kwargs: (json.dumps(data), "", 0, False))
    receipts, records = [], []

    class Recorder:
        def begin_call(self, identity):
            def fail(event):
                raise RuntimeError("archive unavailable")
            return SimpleNamespace(event=fail, payload=lambda: {}, finish=lambda *args, **kwargs: None)

        def __call__(self, entry):
            records.append(entry)

    def invoke():
        return complete_native(lambda: transport, request={"system": "s", "messages": []},
                               identity={"provider": provider},
                               recorder=Recorder() if ending == "archive_failure" else records.append)

    with bind_call_budget("test", lambda _: "ticket",
                          lambda ticket, facts: receipts.append(dict(facts))):
        if ending == "success":
            result, _, _, _ = invoke()
            assert result.text == "OK"
        else:
            with pytest.raises(RuntimeError, match="errored|tool calls|archive unavailable"):
                invoke()
    expected = {"input_tokens": 10, "output_tokens": 2}
    if provider == "claude-code":
        expected["cost_usd"] = 0.21
    assert len(receipts) == len(records) == 1
    assert receipts[0]["sent"] is True
    assert receipts[0]["usage"] == records[0]["usage"] == expected
    assert receipts[0]["outcome"] == ("completed" if ending == "success" else "failed")


def test_claude_invalid_terminal_result_keeps_reported_usage(monkeypatch):
    from infermatrix_copilot.budgeting import bind_call_budget
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.providers.claude_code import ClaudeCodeTransport

    transport = ClaudeCodeTransport(Settings(_env_file=None))
    monkeypatch.setattr(transport, "require_cli", lambda: "offline-fixture")
    data = {"type": "result", "is_error": False, "usage": {"input_tokens": 10, "output_tokens": 2}}
    monkeypatch.setattr("infermatrix_copilot.providers.claude_code.run_cli",
                        lambda *args, **kwargs: (json.dumps(data), "", 1, False))
    receipts = []
    with bind_call_budget("test", lambda _: "ticket",
                          lambda ticket, facts: receipts.append(dict(facts))):
        with pytest.raises(RuntimeError, match="without a valid terminal result"):
            complete_native(lambda: transport, request={"system": "s", "messages": []})
    assert len(receipts) == 1
    assert receipts[0]["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert receipts[0]["outcome"] == "failed"


def reply(text='{"ok":true}'):
    return SimpleNamespace(blocks=[SimpleNamespace(text=text)], model="pinned-model",
                           stop_reason="end_turn", usage={})


def test_standalone_completion_keeps_unknown_events_and_pinned_request(tmp_path):
    store, requests = TraceStore(tmp_path, environ={}), []
    event = {"type": "future.native.event", "payload": {"opaque": ["retained"]}}

    class Transport:
        supports_native_events = True

        def complete(self, native_event_sink, **request):
            requests.append(request)
            native_event_sink(event)
            return reply()

    result, value, record, receipt = complete_native(
        Transport, request={"system": "s", "messages": [], "model": "pinned-model", "effort": "high"},
        payload={"system": "s", "prompt": ""}, recorder=trace_recorder(store),
        identity={"provider": "codex"}, validate=lambda _: {"accepted": True})
    assert requests[0]["model"] == "pinned-model" and requests[0]["effort"] == "high"
    assert value == {"accepted": True} and record["cost_usd"] is None
    attempt = json.loads(next((store.root / "attempts").glob("*/attempt.json")).read_text())
    assert event == json.loads(store.blob(attempt["native_events_sha256"]).strip())
    assert attempt["status"] == "complete" and attempt["trace_id"] == receipt["id"]


def test_schema_failure_finalizes_one_failed_receipt_after_native_success(tmp_path):
    store = TraceStore(tmp_path, environ={})

    class Transport:
        def complete(self, **_):
            return reply()

    gateway = ModelGateway(None, transport_factory=lambda _: Transport(), recorder=trace_recorder(store))

    def reject(_):
        raise ValueError("source does not support conclusion")

    with pytest.raises(ModelUnavailable, match="does not support"):
        gateway.call_json(ModelRole("judge", "codex", "pinned-model", "high"),
                          system="s", prompt="evidence", validate=reject)
    attempts = list((store.root / "attempts").glob("*/attempt.json"))
    records = store.query(kind="model_call")
    assert len(attempts) == len(records) == 1
    assert json.loads(attempts[0].read_text())["status"] == "failed"
    assert records[0]["error"] and records[0]["usage"] == {}


def test_omitting_archived_payload_keeps_returned_reply_and_unknown_cost():
    records = []
    gateway = ModelGateway(None, transport_factory=lambda _: SimpleNamespace(complete=lambda **_: reply()),
                           recorder=records.append)
    result = gateway.call_json(ModelRole("generator", "codex", "pinned-model"),
                               system="private", prompt="source", record_payload=False)
    assert result.text == '{"ok":true}' and result.data == {"ok": True} and result.cost_usd is None
    assert len(records) == 1 and records[0]["system"] == records[0]["prompt"] == records[0]["reply"] == ""


def test_live_completion_and_durable_record_share_elapsed_precision(monkeypatch):
    from infermatrix_copilot.providers import completion
    ticks = iter([100.0, 100.123456])
    monkeypatch.setattr(completion, 'time', SimpleNamespace(time=lambda: next(ticks), monotonic=lambda: 0.0))
    entries = []
    _, _, record, _ = complete_native(lambda: SimpleNamespace(complete=lambda **kwargs: reply()),
                                      request={'system': 's', 'messages': []}, recorder=entries.append)
    assert record['seconds'] == entries[0]['seconds'] == .123


@pytest.mark.parametrize('amount,expected', [(None,None),(0,0.0),(.75,.75),(float('nan'),None),(-1,None)])
@pytest.mark.parametrize('ending', ['accepted','rejected','partial'])
def test_native_capture_preserves_actual_or_unknown_cost_beside_legacy_judge_convention(tmp_path,amount,expected,ending):
    from infermatrix_copilot.trace_store import TraceStore,bind_store
    events=[]
    class Transport:
        supports_native_events=True
        def complete(self,*,native_event_sink,**kwargs):
            native_event_sink({'usage':{'input_tokens':7,'output_tokens':3,'cost_usd':amount}})
            if ending=='partial':raise OSError('response lost')
            result=reply();result.usage={'input_tokens':7,'output_tokens':3,'cost_usd':amount}
            return result
        def native_snapshot(self,values):return {'usage':values[-1]['usage']}
    def validate(_):
        if ending=='rejected':raise ValueError('invalid verdict')
    store=TraceStore(tmp_path/'traces',environ={})
    with bind_store(store):
        kwargs=dict(request={'system':'rules','messages':[]},identity={'provider':'fixture'},
            validate=validate,capture={'provider':'harness:fixture','extra_result':{
                'usd':0.0,'subscription':True,'accounting_convention':'judge-calls/v1',
                'cost_usd':99,'native_subscription':True}})
        if ending=='accepted':
            _,_,record,_=complete_native(Transport,**kwargs)
            assert record['cost_usd']==expected
        else:
            with pytest.raises((ValueError,OSError)):
                complete_native(Transport,**kwargs)
    rows=store.query(kind='model_call');assert len(rows)==1
    facts=rows[0]['result']
    assert facts['usd']==0.0 and facts['subscription'] is True
    assert facts['accounting_convention']=='judge-calls/v1'
    assert facts['cost_usd']==expected and facts['native_subscription'] is None
    assert rows[0]['usage']['input_tokens']==7
    assert bool(rows[0]['error']) is (ending!='accepted')
