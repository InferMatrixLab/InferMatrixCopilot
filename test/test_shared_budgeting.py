"""Shared reservation lifetimes preserve each domain's durable accounting."""
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.budgeting import bind_call_budget, call_budget, reserved_call, settlement_charge
from infermatrix_copilot.improve.budget import BudgetRefused, Governor, governed
from infermatrix_copilot.llm import LLM

NOW = 1782864000


def test_call_bindings_replace_same_account_and_stack_independent_accounts():
    events = []
    def acquire(request):
        events.append(("reserve", request["model"]))
        return len(events)
    def finish(ticket, facts):
        events.append(("finish", ticket, facts["sent"], facts["usage"]))
    with bind_call_budget("knowledge", acquire, finish), bind_call_budget("weekly", acquire, finish):
        with bind_call_budget("knowledge", acquire, finish):
            with call_budget({"model": "m"}) as facts:
                assert events == [("reserve", "m"), ("reserve", "m")]
                facts.update(sent=True, usage=None)
        assert [event[0] for event in events] == ["reserve", "reserve", "finish", "finish"]
        with bind_call_budget("weekly", None, None):
            with call_budget({"model": "m"}):
                assert len(events) == 5
    before = len(events)
    with call_budget({"model": "m"}):
        pass
    assert len(events) == before


def test_later_account_refusal_finalizes_earlier_ticket_without_dispatch():
    events = []
    def refuse(request):
        raise BudgetRefused("not affordable")
    with bind_call_budget("one", lambda request: "ticket", lambda ticket, facts: events.append((ticket, facts["sent"]))):
        with bind_call_budget("two", refuse, lambda *args: pytest.fail("not acquired")):
            with pytest.raises(BudgetRefused), call_budget({}):
                pytest.fail("refused call dispatched")
    assert events == [("ticket", False)]


def test_every_account_finalizes_when_one_settlement_fails():
    events = []
    def failed(ticket, facts):
        events.append(ticket)
        raise OSError("ledger unavailable")
    with bind_call_budget("one", lambda request: "one", lambda ticket, facts: events.append(ticket)):
        with bind_call_budget("two", lambda request: "two", failed):
            with pytest.raises(OSError), call_budget({}):
                pass
    assert events == ["two", "one"]


def governor(path):
    return Governor(path, usd_week=5, judge_calls_week=5, clock=lambda: NOW)


def test_reservation_never_dispatches_before_acquire_and_finalizes_interruption():
    events = []
    with pytest.raises(SystemExit):
        with reserved_call(lambda: events.append("reserved") or "ticket",
                           lambda call: events.append(dict(call))) as call:
            assert events == ["reserved"]
            call["sent"] = True
            raise SystemExit()
    assert events[-1] == {"reservation": "ticket", "sent": True, "actual_usd": None, "outcome": "unknown"}
    with pytest.raises(OSError):
        with reserved_call(lambda: (_ for _ in ()).throw(OSError()), lambda _: pytest.fail("not acquired")):
            pytest.fail("not acquired")


@pytest.mark.parametrize("actual", [None, True, float("nan"), -1])
def test_untrustworthy_amount_is_never_a_refund(actual):
    assert settlement_charge(.5, actual) == .5
    assert settlement_charge(.5, .125, outcome="unknown") == .5
    assert settlement_charge(.5, .75, outcome="failed") == .75


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": None}, {"input_tokens": "garbage"},
                                  {"input_tokens": -1, "output_tokens": 0},
                                  {"input_tokens": True, "output_tokens": 0},
                                  {"input_tokens": float("nan"), "output_tokens": 0}])
def test_unknown_usage_settles_full_once_across_instances(tmp_path, usage):
    gov = governor(tmp_path)
    token = gov.reserve_call("claude-sonnet-5", 4000, 100)
    amount = gov.remaining()["usd_reserved"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        charges = list(pool.map(lambda _: governor(tmp_path).settle_call(token, usage, "claude-sonnet-5"), range(12)))
    assert charges == [pytest.approx(amount)] * 12
    assert gov.remaining()["usd_settled"] == pytest.approx(amount)
    assert gov.remaining()["usd_reserved"] == 0
    with pytest.raises(BudgetRefused, match="different settlement"):
        gov.settle_call(token, {"input_tokens": 1, "output_tokens": 0}, "claude-sonnet-5")


def test_forfeit_release_and_judge_receipts_are_durable_and_idempotent(tmp_path):
    gov = governor(tmp_path)
    token = gov.reserve_call("claude-sonnet-5", 100, 10)
    charged = gov.forfeit_call(token)
    assert governor(tmp_path).forfeit_call(token) == charged
    released = gov.reserve_call("claude-sonnet-5", 100, 10)
    gov.release_call(released)
    governor(tmp_path).release_call(released)
    judge = gov.reserve_judge_call()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: governor(tmp_path).settle_judge_call(judge), range(8)))
    assert gov.remaining()["judge_calls_used"] == 1
    assert gov.remaining()["judge_calls_reserved"] == 0
    before = gov.remaining()
    with pytest.raises(BudgetRefused, match="unknown reservation"):
        gov.settle_call(gov.week() + "|missing", {"input_tokens": 1}, "claude-sonnet-5")
    with pytest.raises(BudgetRefused, match="unknown judge"):
        gov.settle_judge_call(gov.week() + "|judge|missing")
    assert gov.remaining() == before


def test_old_week_file_and_unfinished_reservations_are_not_reset(tmp_path):
    gov = governor(tmp_path)
    token = gov.week() + "|legacy"
    old = {"week": gov.week(), "usd_settled": .125, "reservations": {
        token: {"usd": .5, "model": "claude-sonnet-5", "at": NOW, "purpose": "legacy"}},
        "named": {"experiment": .25}, "judge_calls": 2, "judge_reserved": 1,
        "breaches": [], "refused": 0}
    gov._path(gov.week()).write_text(json.dumps(old))
    assert gov.settle_call(token, None, "claude-sonnet-5") == .5
    saved = json.loads(gov._path(gov.week()).read_text())
    assert saved["usd_settled"] == .625 and saved["named"] == old["named"]
    assert saved["judge_reserved"] == 1 and saved["judge_calls"] == 2
    # Legacy anonymous settlement remains available; an unverifiable token
    # cannot steal another outstanding judge reservation.
    with pytest.raises(BudgetRefused):
        gov.settle_judge_call(gov.week() + "|judge|old-unrecorded")
    gov.settle_judge_call()
    assert gov.remaining()["judge_calls_used"] == 3


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize("usage", [None, SimpleNamespace(), SimpleNamespace(input_tokens=None, output_tokens=None,
                                                                           prompt_tokens=None, completion_tokens=None)])
def test_governed_llm_success_without_usage_retains_the_whole_reservation(tmp_path, settings, provider, usage):
    llm = LLM.__new__(LLM)
    llm.settings, llm._default_model, llm._provider = settings, "claude-sonnet-5", provider
    llm._guard_served_model = lambda *args: None
    llm._client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn", model="claude-sonnet-5", usage=usage)))
    llm._create_openai = lambda **kwargs: SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="ok", tool_calls=[]), finish_reason="stop")], model="claude-sonnet-5", usage=usage)
    gov = governor(tmp_path)
    with governed(gov):
        assert llm.create(system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=20).text == "ok"
    assert gov.remaining()["usd_settled"] > 0
    assert gov.remaining()["usd_reserved"] == 0


def test_explicit_zero_token_usage_can_settle_zero(tmp_path):
    gov = governor(tmp_path)
    token = gov.reserve_call("claude-sonnet-5", 1000, 100)
    assert gov.settle_call(token, {"input_tokens": 0, "output_tokens": 0}, "claude-sonnet-5") == 0


@pytest.mark.parametrize("path", ["budgeting.py", "persistence.py", "git_objects.py", "providers/completion.py",
                                  "app/workflow_execution.py", "kb_service/init_execution.py"])
def test_shared_budget_infrastructure_remains_protected_from_self_edit(path):
    from infermatrix_copilot.improve.artifacts import ArtifactError, validate_policy
    with pytest.raises(ArtifactError, match="protected mutation"):
        validate_policy({"paths": ["src/infermatrix_copilot/" + path]})


@pytest.mark.parametrize("written_before_error", [False, True])
def test_failed_weekly_settlement_recovers_without_refund_or_double_charge(tmp_path, monkeypatch, written_before_error):
    import infermatrix_copilot.improve.budget as module
    gov = governor(tmp_path)
    token = gov.reserve_call("claude-sonnet-5", 1000, 100)
    original = module.atomic_write_bytes

    def fail(path, data):
        if written_before_error:
            original(path, data)
        raise OSError("settlement storage unavailable")

    usage = {"input_tokens": 10, "output_tokens": 1}
    monkeypatch.setattr(module, "atomic_write_bytes", fail)
    with pytest.raises(OSError):
        gov.settle_call(token, usage, "claude-sonnet-5")
    monkeypatch.setattr(module, "atomic_write_bytes", original)
    recovered = governor(tmp_path)
    expected = recovered.settle_call(token, usage, "claude-sonnet-5")
    assert recovered.settle_call(token, usage, "claude-sonnet-5") == expected
    assert recovered.remaining()["usd_settled"] == pytest.approx(expected, abs=1e-6)
    assert recovered.remaining()["usd_reserved"] == 0


def test_maintenance_interrupt_charges_full_and_cannot_redispatch(tmp_path):
    from test_kb_maintenance_flow import configured_runtime
    from infermatrix_copilot.kb_service.maintenance_audit import BudgetGateway
    from infermatrix_copilot.kb_service.maintenance_store import MaintenanceStore
    from infermatrix_copilot.kb_service.models import ModelUnavailable
    calls = []

    def interrupted(role, prompt):
        calls.append(role)
        raise SystemExit("interrupted paid call")

    rt, lifecycle, source, unit, now = configured_runtime(tmp_path, interrupted)
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    def gateway():
        return BudgetGateway(rt, store, rt.maintenance, run_id="interrupted", unit=unit, phase="audit")
    with pytest.raises(SystemExit):
        gateway().call_json(rt.judge, system="audit", prompt="original pinned source")
    row = rt.ledger._conn.execute("SELECT * FROM maintenance_budget").fetchone()
    assert row["status"] == "settled" and row["outcome"] == "failed"
    assert row["charged_microusd"] == row["worst_microusd"] == 500_000
    with pytest.raises(ModelUnavailable, match="not redispatched"):
        gateway().call_json(rt.judge, system="audit", prompt="original pinned source")
    assert len(calls) == 1


def test_settlement_keeps_exact_integer_domain_units_above_float_precision():
    actual = 2 ** 53 + 1
    settled = settlement_charge(1, actual)
    assert type(settled) is int and settled == actual
    assert settlement_charge(actual + 2, actual, fixed_accounting=True) == actual + 2
    assert settlement_charge(1, actual, outcome='unknown') == actual


@pytest.mark.parametrize('amount,expected', [(None,None),(True,None),('0',None),(-1,None),
    (float('nan'),None),(float('inf'),None),(0,0.0),(.75,.75),(10**1000,None)])
def test_reported_amount_normalization_is_shared_and_missing_is_not_zero(amount,expected):
    from infermatrix_copilot.budgeting import normalized_cost
    from infermatrix_copilot.providers.base import SessionUsage
    assert normalized_cost(amount)==expected
    assert SessionUsage.normalized_cost(amount)==expected


@pytest.mark.parametrize('amount,expected', [(0,0.0),(.25,.25),(-1,None),(float('nan'),None),('0.25',None)])
def test_domain_settlement_prefers_reported_amount_and_keeps_invalid_unknown(amount,expected):
    from infermatrix_copilot.improve.budget import actual_usd
    # Native amounts remain authoritative even when tokens imply another price.
    assert actual_usd('claude-sonnet-5',{'cost_usd':amount,'input_tokens':1000,'output_tokens':100})==expected


def test_actual_native_overspend_is_recorded_before_refusing_next_call(tmp_path):
    from infermatrix_copilot.improve.budget import BudgetBreach,BudgetRefused
    gov=governor(tmp_path)
    token=gov.reserve_call('claude-sonnet-5',1000,100)
    with pytest.raises(BudgetBreach):
        gov.settle_call(token,{'cost_usd':2.5,'input_tokens':0,'output_tokens':0},'claude-sonnet-5')
    assert gov.remaining()['usd_settled']==2.5 and gov.remaining()['usd_reserved']==0
    with pytest.raises(BudgetBreach):
        gov.settle_call(token,{'cost_usd':2.5},'claude-sonnet-5')
    assert gov.remaining()['usd_settled']==2.5
    with pytest.raises(BudgetRefused,match='no further calls'):
        gov.reserve_call('claude-sonnet-5',1,1)


def test_reported_zero_releases_reservation_while_invalid_cost_keeps_it(tmp_path):
    gov=governor(tmp_path)
    zero=gov.reserve_call('claude-sonnet-5',1000,100)
    assert gov.settle_call(zero,{'cost_usd':0,'input_tokens':1000,'output_tokens':100},'claude-sonnet-5')==0
    unknown=gov.reserve_call('claude-sonnet-5',1000,100)
    reserved=gov.remaining()['usd_reserved']
    assert gov.settle_call(unknown,{'cost_usd':float('inf'),'input_tokens':0,'output_tokens':0},'claude-sonnet-5')==reserved
