"""MoA (design W6): member resolution, eligibility, the atomic budget ledger,
dispatch budget bindings, ensemble member assignment, issue propose-aggregate
fallbacks, and the no-secret-in-traces guarantee."""

import json
import time

import pytest

from infermatrix_copilot.engine.agent_runtime.moa import (
    Member,
    MoaBudget,
    MoaBudgetExceeded,
    moa_eligible,
    resolve_members,
)

MIX = {"members": [
    {"model": "deepseek-chat", "base_url": "https://api.example.com/v1",
     "api_key": "sk-SECRET-A"},
    {"model": "claude-sonnet-5", "api_key_env": "MOA_TEST_KEY"},
]}


# ---- config parsing / member resolution ------------------------------------

def test_malformed_llm_mixture_degrades_to_off(monkeypatch):
    from infermatrix_copilot.config import Settings

    monkeypatch.setenv("LLM_MIXTURE", "{not json")
    assert Settings().llm_mixture == {}


def test_resolve_members_secret_env_and_pricing_gate(settings, monkeypatch):
    monkeypatch.setenv("MOA_TEST_KEY", "sk-SECRET-B")
    settings.llm_mixture = {"members": MIX["members"] + [
        {"model": "totally-unpriced-model-x"}]}
    members = resolve_members(settings)
    assert [m.model for m in members] == ["deepseek-chat", "claude-sonnet-5"]
    assert members[0].api_key == "sk-SECRET-A"
    assert members[1].api_key == "sk-SECRET-B"     # resolved via api_key_env
    # labels never contain key material
    assert all("SECRET" not in m.label() for m in members)


def test_member_cap(settings):
    settings.llm_mixture = {"members": [
        {"model": "deepseek-chat"}] * 6}
    settings.moa_max_members = 3
    assert len(resolve_members(settings)) == 3


# ---- eligibility -----------------------------------------------------------

@pytest.mark.parametrize("when,kind,mode,depth,expect", [
    ("off", "pr_review", "performance", "full", False),
    ("full", "pr_review", "eco", "full", True),
    ("full", "pr_review", "eco", "light", False),
    ("full", "pr_review", "performance", "standard", True),
    ("full", "issue_answer", "eco", "", False),
    ("full", "issue_answer", "performance", "", True),
    ("performance", "pr_review", "eco", "full", False),
    ("always", "issue_answer", "eco", "", True),
])
def test_moa_eligibility_matrix(settings, when, kind, mode, depth, expect):
    settings.moa_when = when
    assert moa_eligible(settings, kind=kind, mode=mode, depth=depth) is expect


# ---- budget ledger ---------------------------------------------------------

def _budget(max_usd=1.0, deadline_s=60.0):
    return MoaBudget(max_usd=max_usd, deadline=time.monotonic() + deadline_s,
                     member_timeout_s=30.0)


def test_reservations_are_atomic_and_capped():
    b = _budget(max_usd=1.0)
    r1 = b.reserve(0.6)
    assert r1 is not None
    assert b.reserve(0.6) is None and b.tripped       # 0.6+0.6 > 1.0
    b.settle(r1, 0.1)                                  # actual << reserved
    assert b.spent() == pytest.approx(0.1)
    assert b.reserve(0.6) is not None                  # freed headroom returns


def test_overrun_records_full_charge_and_stops_new_members():
    b = _budget(max_usd=1.0)
    rid = b.reserve(0.2)
    b.settle(rid, 5.0)
    assert b.spent() == pytest.approx(5.0)
    assert b.tripped and b.reserve(0.0) is None
    b.settle(rid, 5.0)
    assert b.spent() == pytest.approx(5.0)
    with pytest.raises(MoaBudgetExceeded, match="different settlement"):
        b.settle(rid, 0.2)


def test_expired_deadline_refuses_reservation():
    b = _budget(deadline_s=-1.0)
    assert b.reserve(0.01) is None


# ---- bindings observe real dispatch facts ----------------------------------

@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 1},
                                  {"input_tokens": True, "output_tokens": 0}])
@pytest.mark.parametrize("failure", [False, True])
def test_unknown_paid_call_keeps_full_reservation(usage, failure):
    from infermatrix_copilot.budgeting import call_budget
    b = _budget()
    with b.bind(Member("deepseek-chat")):
        try:
            with call_budget({"kind": "api", "model": "deepseek-chat", "system": "s",
                              "messages": [], "max_tokens": 100}) as facts:
                reserved = sum(b._reserved.values())
                facts.update(sent=True, usage=usage)
                if failure:
                    raise OSError("provider disconnected after dispatch")
        except OSError:
            pass
    assert b.spent() == pytest.approx(reserved) and not b._reserved


def test_binding_reserves_before_dispatch_and_settles_known_usage():
    from infermatrix_copilot.budgeting import call_budget
    member, b = Member("deepseek-chat"), _budget()
    request = {"kind": "api", "model": member.model, "system": "s",
               "messages": [], "max_tokens": 100}
    with b.bind(member), b.bind(member):
        with call_budget(request) as facts:
            assert len(b._reserved) == 1
            facts.update(sent=True, usage={"input_tokens": 1, "output_tokens": 0})
    assert 0 < b.spent() < .001
    assert len(b._receipts) == 1 and not b._reserved
    with _budget(max_usd=0).bind(member), pytest.raises(MoaBudgetExceeded):
        with call_budget(request):
            pytest.fail("refused request dispatched")


def test_undispatched_call_releases_but_unknown_ticket_cannot_settle():
    from infermatrix_copilot.budgeting import call_budget
    b = _budget()
    with b.bind(Member("deepseek-chat")):
        with call_budget({"kind": "api", "model": "deepseek-chat", "max_tokens": 100}):
            pass
    assert b.spent() == 0 and not b._reserved
    with pytest.raises(MoaBudgetExceeded, match="unknown"):
        b.settle(999, 0)


# ---- no secrets in traces ---------------------------------------------------

def test_no_secret_material_in_trace_events(settings, trace, monkeypatch):
    monkeypatch.setenv("MOA_TEST_KEY", "sk-SECRET-B")
    settings.llm_mixture = MIX
    members = resolve_members(settings)
    trace.record("moa_dispatch", members=[m.label() for m in members],
                 max_usd=1.5)
    dumped = json.dumps(list(trace.events("moa_dispatch")))
    assert "SECRET" not in dumped and "sk-" not in dumped


# ---- harness members (provider registry) ------------------------------------

def test_resolve_harness_member_skips_pricing_gate(settings):
    settings.llm_mixture = {"members": [
        {"model": "composer-2.5", "provider": "cursor"},
        {"model": "deepseek-chat", "api_key": "sk-x"},
    ]}
    members = resolve_members(settings)
    assert [(m.model, m.provider) for m in members] == [
        ("composer-2.5", "cursor"), ("deepseek-chat", "")]
    assert members[0].label() == "composer-2.5@cursor"
    assert members[0].api_key == "" and members[0].base_url == ""


def test_resolve_harness_member_rejects_unknown_keeps_shipped(settings):
    """claude-code shipped with M2, so its members now resolve; only unknown
    provider ids are rejected."""
    settings.llm_mixture = {"members": [
        {"model": "claude-fable-5", "provider": "claude-code"},  # M2 shipped
        {"model": "x", "provider": "nonesuch"},                  # unknown id
        {"model": "composer-2.5", "provider": "cursor"},
    ]}
    members = resolve_members(settings)
    assert [(m.model, m.provider) for m in members] == [
        ("claude-fable-5", "claude-code"), ("composer-2.5", "cursor")]
