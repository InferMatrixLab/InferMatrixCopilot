"""Read-only streaming and buffered sessions prepare every account before send."""
import json
import sys
from types import SimpleNamespace

import pytest

from infermatrix_copilot.budgeting import bind_call_budget
from infermatrix_copilot.config import Settings
from infermatrix_copilot.improve.budget import BudgetRefused, Governor, governed
from infermatrix_copilot.providers.json_session import run_readonly_json


def session(tmp_path, *, provider='cursor', output_format='stream-json', repair=False):
    cli = tmp_path / 'fixture.py'
    cli.write_text('''
import json,sys
from pathlib import Path
args=sys.argv
with Path('dispatches').open('a') as f:f.write('original\\n')
text='not JSON' if REPAIR and '--resume' not in args else '{"ok":true}'
if '--output-last-message' in args:
 Path(args[args.index('--output-last-message')+1]).write_text(text)
 print(json.dumps({'type':'turn.completed','usage':{'input_tokens':4,'output_tokens':2}}))
else:
 print(json.dumps({'type':'result','subtype':'success','session_id':'original',
  'result':text,'usage':{'inputTokens':4,'outputTokens':2}}))
'''.replace('REPAIR', repr(repair)))
    return run_readonly_json('frozen evidence', cwd=tmp_path, command=[sys.executable, str(cli)],
                             provider=provider, model='model', output_schema={'type': 'object'},
                             idle_timeout_s=3, absolute_timeout_s=10, output_format=output_format)


@pytest.mark.parametrize('provider', ['cursor', 'codex'])
@pytest.mark.parametrize('output_format', ['stream-json', 'json'])
def test_required_account_prepares_once_before_actual_readonly_dispatch(tmp_path, provider, output_format):
    events, settlements = [], []
    def prepare(ticket, request, execution):
        assert not (tmp_path / 'dispatches').exists()
        events.append(execution)
        assert request['kind'] == 'harness_session' and request['model'] == 'model'
        return {}
    with bind_call_budget('readonly-account', lambda _: SimpleNamespace(prepare_required=True),
                          lambda ticket, facts: settlements.append(dict(facts)), prepare=prepare):
        assert session(tmp_path, provider=provider, output_format=output_format)['payload'] == {'ok': True}
    assert events == [{'provider_id': provider, 'subscription_admitted': False, 'stops_at_spend': False}]
    assert len(settlements) == 1 and settlements[0]['sent'] is True
    assert settlements[0]['usage']['input_tokens'] == 4 and settlements[0]['usage']['output_tokens'] == 2
    assert (tmp_path / 'dispatches').read_text() == 'original\n'


@pytest.mark.parametrize('output_format', ['stream-json', 'json'])
def test_preparation_refusal_has_original_type_and_never_opens_a_process(tmp_path, output_format):
    settlements = []
    def prepare(*args):
        raise ValueError('domain refused this readonly call')
    with bind_call_budget('readonly-account', lambda _: SimpleNamespace(prepare_required=True),
                          lambda ticket, facts: settlements.append(dict(facts)), prepare=prepare):
        with pytest.raises(ValueError, match='domain refused') as caught:
            session(tmp_path, output_format=output_format)
    assert caught.value.call_budget_failure is True
    assert len(settlements) == 1 and settlements[0]['sent'] is False
    assert not (tmp_path / 'dispatches').exists()


@pytest.mark.parametrize('output_format', ['stream-json', 'json'])
def test_each_same_session_format_repair_prepares_and_settles_independently(tmp_path, output_format):
    calls, settlements = [], []
    with bind_call_budget('readonly-account', lambda _: SimpleNamespace(prepare_required=True),
                          lambda ticket, facts: settlements.append(dict(facts)),
                          prepare=lambda ticket, request, execution: calls.append(request) or {}):
        result = session(tmp_path, output_format=output_format, repair=True)
    assert result['payload'] == {'ok': True} and result['session_id'] == 'original'
    assert len(calls) == len(settlements) == 2
    assert all(facts['sent'] and facts['usage']['input_tokens'] == 4 for facts in settlements)
    assert (tmp_path / 'dispatches').read_text() == 'original\noriginal\n'


@pytest.mark.parametrize('output_format', ['stream-json', 'json'])
def test_native_dollar_session_without_admitted_context_refuses_in_its_owner_before_send(tmp_path, output_format):
    governor = Governor(tmp_path / 'ledger', usd_week=5, judge_calls_week=5,
                        settings=Settings(_env_file=None, improve_native_call_limits={
                            'codex:model': {'stop_usd': .5, 'overshoot_usd': .25}}))
    with governed(governor), pytest.raises(BudgetRefused, match='cannot enforce'):
        session(tmp_path, provider='codex', output_format=output_format)
    assert governor.remaining()['usd_reserved'] == governor.remaining()['usd_settled'] == 0
    assert not (tmp_path / 'dispatches').exists()


@pytest.mark.parametrize('output_format', ['stream-json', 'json'])
@pytest.mark.parametrize('events', [[], [{'type': 'turn.completed'}, {'type': 'turn.started'}]])
def test_codex_result_file_without_a_terminal_turn_cannot_complete_or_retry(tmp_path, output_format, events):
    from infermatrix_copilot.providers.json_session import JSONSessionError
    cli = tmp_path / 'incomplete.py'
    cli.write_text("import json,pathlib,sys\npathlib.Path('dispatches').write_text('original')\n"
                   "pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('{\"ok\":true}')\n"
                   'for event in '+repr(events)+':print(json.dumps(event))\n')
    settlements = []
    with bind_call_budget('readonly-account', lambda _: 'original',
                          lambda ticket, facts: settlements.append(dict(facts))):
        with pytest.raises(JSONSessionError, match='no terminal result'):
            run_readonly_json('frozen', cwd=tmp_path, command=[sys.executable, str(cli)],
                              provider='codex', model='model', output_schema={'type': 'object'},
                              idle_timeout_s=2, absolute_timeout_s=4, output_format=output_format)
    assert len(settlements) == 1 and settlements[0]['sent'] is True
    assert settlements[0]['outcome'] != 'completed' and (tmp_path / 'dispatches').read_text() == 'original'


def test_completion_subscription_attestation_cannot_waive_an_agent_account(tmp_path):
    from infermatrix_copilot.providers import run_harness_session
    from infermatrix_copilot.providers.base import native_budget_facts
    calls = []
    transport = SimpleNamespace(spec=SimpleNamespace(id='codex'), subscription_billing=True,
        configured_subscription_admission={'fixture': True},
        completion_profile=SimpleNamespace(sandbox=SimpleNamespace(expected_context={'fixture': True})),
        run_session=lambda request: calls.append(request) or pytest.fail('session escaped completion containment'))
    assert native_budget_facts(transport)['subscription_admitted'] is True
    assert native_budget_facts(transport, session=True)['subscription_admitted'] is False
    request = SimpleNamespace(system='frozen', prompt='frozen', model='model', step_name='fixture')
    governor = Governor(tmp_path / 'ledger', usd_week=0, judge_calls_week=1, settings=Settings(
        _env_file=None, improve_native_call_limits={'codex:model': {'stop_usd': .5, 'overshoot_usd': .25}}))
    with governed(governor), pytest.raises(BudgetRefused, match='cannot enforce'):
        run_harness_session(transport, request)
    assert calls == [] and governor.remaining()['usd_settled'] == 0


@pytest.mark.parametrize('entry', ['adapter', 'wrapper'])
def test_agent_session_refuses_a_completion_profile_before_any_host_dispatch(tmp_path, entry):
    from infermatrix_copilot.providers import run_harness_session
    from infermatrix_copilot.providers.codex import CodexTransport
    transport = CodexTransport(SimpleNamespace(strict_backend_model='model'))
    transport.completion_profile = object()
    transport._run = lambda *args, **kwargs: pytest.fail('completion profile escaped to a host process')
    request = SimpleNamespace(system='frozen', prompt='frozen', model='model', step_name='fixture',
        bridge_spec_path=None, scope=SimpleNamespace(root=tmp_path), run_dir=tmp_path, timeout_s=1,
        native_event_sink=None, trace=None)
    with pytest.raises(ValueError, match='does not support agent sessions'):
        (transport.run_session(request) if entry == 'adapter' else run_harness_session(transport, request))
