"""Offline admission/recovery tests; no provider is invoked."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from ledger import Ledger, Refused


def reserve(ledger, call="call", case="screening", arm="baseline", digest="request"):
    return ledger.reserve(call, case_id=case, arm=arm, request_digest=digest)


def test_unknown_call_survives_restart_and_cannot_be_bypassed(tmp_path):
    path = tmp_path / "calls.sqlite"
    original = Ledger(path, {"inputs": "frozen"})
    assert reserve(original) is None
    original.close()
    resumed = Ledger(path, {"inputs": "frozen"})
    with pytest.raises(Refused, match="unknown"):
        reserve(resumed)
    with pytest.raises(Refused, match="unresolved"):
        reserve(resumed, "invented-retry")
    resumed.close()


def test_completed_receipt_reuses_exact_identity_and_cannot_change(tmp_path):
    ledger = Ledger(tmp_path / "calls.sqlite", {})
    reserve(ledger)
    receipt = {"outcome": "judged", "usage": None, "cost_usd": None, "reply_sha256": "reply"}
    ledger.complete("call", receipt)
    assert reserve(ledger) == receipt
    ledger.complete("call", receipt)
    with pytest.raises(Refused, match="identity changed"):
        reserve(ledger, digest="changed")
    with pytest.raises(Refused, match="cannot be replaced"):
        ledger.complete("call", {**receipt, "outcome": "different"})
    ledger.close()


def test_plan_and_limit_cannot_change_after_restart(tmp_path):
    path = tmp_path / "calls.sqlite"
    Ledger(path, {"inputs": "original"}, call_limit=2).close()
    with pytest.raises(Refused, match="plan or call ceiling"):
        Ledger(path, {"inputs": "other"}, call_limit=2)
    with pytest.raises(Refused, match="plan or call ceiling"):
        Ledger(path, {"inputs": "original"}, call_limit=3)


def test_retained_allowances_cannot_fund_a_second_pair(tmp_path):
    from ledger import plan_call_limit
    plan = {'budget_revision': 'history-obligations-20261017',
            'native_call_ceiling': 519, 'retained_prior_call_allowances': 519,
            'native_call_limits': {'history': 10}}
    with pytest.raises(Refused, match='retained prior allowances plus paired'):
        plan_call_limit(plan)
    path = tmp_path / 'not-created.sqlite'
    with pytest.raises(Refused, match='retained prior allowances plus paired'):
        Ledger(path, plan)
    assert not path.exists()


@pytest.mark.parametrize('retained', [-1, True, '499', None])
def test_invalid_retained_allowance_refuses_before_ledger_creation(tmp_path, retained):
    path = tmp_path / 'not-created.sqlite'
    with pytest.raises(Refused, match='nonnegative retained prior allowances'):
        Ledger(path, {'retained_prior_call_allowances': retained,
                      'native_call_limits': {'history': 1}})
    assert not path.exists()


def test_concurrent_arms_share_one_total_ceiling(tmp_path):
    path = tmp_path / "calls.sqlite"
    Ledger(path, {}, call_limit=1).close()

    def attempt(arm):
        ledger = Ledger(path, {}, call_limit=1)
        try:
            reserve(ledger, call=arm, arm=arm)
            return "reserved"
        except Refused:
            return "refused"
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, ["baseline", "candidate"])) == ["refused", "reserved"]


def test_unresolved_call_blocks_other_cases_and_arms(tmp_path):
    ledger = Ledger(tmp_path / "calls.sqlite", {})
    reserve(ledger)
    with pytest.raises(Refused, match="concurrency is one"):
        reserve(ledger, "other", "foundation", "candidate")
    ledger.close()


def test_frozen_case_allowance_counts_native_calls_per_arm(tmp_path):
    ledger = Ledger(tmp_path / "calls.sqlite", {"native_call_limits": {"screening": 2}})
    for call in ["initial", "repair"]:
        reserve(ledger, call)
        ledger.complete(call, {"native_complete": True, "cost_usd": None})
    with pytest.raises(Refused, match="case native call ceiling"):
        reserve(ledger, "second-repair")
    with pytest.raises(Refused, match="absent"):
        reserve(ledger, "unplanned", "other-case")
    assert reserve(ledger, "candidate-initial", arm="candidate") is None
    ledger.close()


@pytest.mark.parametrize("limits", [{}, {"case": 0}, {"case": True}, {"": 1}, []])
def test_invalid_case_allowance_refused_before_creating_ledger(tmp_path, limits):
    path = tmp_path / "calls.sqlite"
    with pytest.raises(ValueError, match="native_call_limits"):
        Ledger(path, {"native_call_limits": limits})
    assert not path.exists()


@pytest.mark.parametrize("ceiling,revision", [(117, "full-workflow-20261012"), (True, "full-workflow-20261012"),
                                             (0, "full-workflow-20261012"), (116, ""), (116, "other")])
def test_revised_ceiling_requires_valid_explicit_protocol(tmp_path, ceiling, revision):
    path = tmp_path / "calls.sqlite"
    with pytest.raises(Refused):
        Ledger(path, {"native_call_ceiling": ceiling, "budget_revision": revision})
    assert not path.exists()


def test_explicit_full_workflow_ceiling_is_enforced_and_cannot_expand_on_resume(tmp_path):
    path = tmp_path / "calls.sqlite"
    plan = {"native_call_ceiling": 116, "budget_revision": "full-workflow-20261012"}
    ledger = Ledger(path, plan)
    for index in range(116):
        call = str(index)
        reserve(ledger, call)
        ledger.complete(call, {"native_complete": True})
    with pytest.raises(Refused, match="ceiling"):
        reserve(ledger, "overflow")
    ledger.close()
    with pytest.raises(Refused, match="differs"):
        Ledger(path, plan, call_limit=117)
    with pytest.raises(Refused, match="plan or call ceiling"):
        Ledger(path, {})


def test_explicit_argument_cannot_bypass_legacy_ceiling(tmp_path):
    with pytest.raises(Refused, match="exceeds"):
        Ledger(tmp_path / "calls.sqlite", {}, call_limit=37)


@pytest.mark.parametrize("revision,ceiling", [("native-generation-20261016", 207),
    ("full-workflow-20261012", 117), ("", 37), (["native-generation-20261016"], 206)])
def test_native_generation_ceiling_cannot_expand_another_revision(tmp_path, revision, ceiling):
    path = tmp_path / "calls.sqlite"
    with pytest.raises(Refused):
        Ledger(path, {"native_call_ceiling": ceiling, "budget_revision": revision})
    assert not path.exists()


def test_new_generation_plan_enforces_its_total_and_never_upgrades_an_old_ledger(tmp_path):
    plan = {"native_call_ceiling": 206, "budget_revision": "native-generation-20261016"}
    ledger = Ledger(tmp_path / "new.sqlite", plan)
    for index in range(206):
        reserve(ledger, str(index))
        ledger.complete(str(index), {"native_complete": True})
    with pytest.raises(Refused, match="ceiling"):
        reserve(ledger, "overflow")
    ledger.close()
    old_path = tmp_path / "original.sqlite"
    old = Ledger(old_path, {"native_call_ceiling": 116, "budget_revision": "full-workflow-20261012"})
    reserve(old, "original-unknown")
    original = old.db.execute("SELECT * FROM calls").fetchall()
    with pytest.raises(Refused, match="original ledger"):
        Ledger(old_path, plan)
    assert old.db.execute("SELECT * FROM calls").fetchall() == original
    with pytest.raises(Refused, match="unknown"):
        reserve(old, "original-unknown")
    old.close()


def test_foundation_revision_keeps_old_ceiling_and_frozen_pair_limits(tmp_path):
    from ledger import plan_call_limit
    with pytest.raises(Refused, match="ceiling"):
        plan_call_limit({"native_call_ceiling":233,"budget_revision":"native-generation-20261016"})
    plan = {"native_call_ceiling":233,"budget_revision":"foundation-revision-20261017",
            "native_call_limits":{"foundation":14},"retained_prior_call_allowances":205}
    path = tmp_path / 'fresh.sqlite'
    ledger = Ledger(path, plan)
    for arm in ['baseline','candidate']:
        for n in range(14):
            call = arm + str(n)
            assert reserve(ledger,call,case='foundation',arm=arm) is None
            ledger.complete(call,{"native_complete":True})
        with pytest.raises(Refused,match="case native call ceiling"):
            reserve(ledger,arm+'-extra',case='foundation',arm=arm)
    ledger.close()
    with pytest.raises(Refused,match="plan or call ceiling"):
        Ledger(path,{**plan,"native_call_limits":{"foundation":15}})


def test_lease_direct_revision_preserves_prior_ceiling_and_one_call_per_arm(tmp_path):
    from ledger import plan_call_limit
    with pytest.raises(Refused, match="ceiling"):
        plan_call_limit({"native_call_ceiling":235,"budget_revision":"foundation-revision-20261017"})
    plan = {"native_call_ceiling":235,"budget_revision":"lease-direct-20261017",
            "native_call_limits":{"direct":1},"retained_prior_call_allowances":233}
    path = tmp_path / 'lease-direct.sqlite'
    ledger = Ledger(path, plan)
    for arm in ['baseline', 'candidate']:
        call = 'lease-' + arm
        assert reserve(ledger,call,case='direct',arm=arm) is None
        ledger.complete(call,{"native_complete":True})
        with pytest.raises(Refused,match="case native call ceiling"):
            reserve(ledger,call+'-extra',case='direct',arm=arm)
    ledger.close()
    with pytest.raises(Refused,match="plan or call ceiling"):
        Ledger(path,{**plan,"native_call_limits":{"direct":2}})


def test_lease_strict_revision_freezes_prior_allowance_and_hard_limit(tmp_path):
    from ledger import plan_call_limit
    with pytest.raises(Refused,match='ceiling'):
        plan_call_limit({'native_call_ceiling':279,'budget_revision':'lease-direct-20261017'})
    plan={'native_call_ceiling':279,'budget_revision':'lease-strict-20261017',
          'native_call_limits':{'strict':22},'retained_prior_call_allowances':235}
    path=tmp_path/'strict.sqlite'
    ledger=Ledger(path,plan)
    for arm in ['candidate','baseline']:
        for number in range(22):
            identity=f'{arm}-{number}'
            reserve(ledger,identity,case='strict',arm=arm)
            ledger.complete(identity,{'native_complete':True})
        with pytest.raises(Refused,match='case native call ceiling'):
            reserve(ledger,arm+'-extra',case='strict',arm=arm)
    ledger.close()
    with pytest.raises(Refused,match='plan or call ceiling'):
        Ledger(path,{**plan,'native_call_limits':{'strict':23}})


@pytest.mark.parametrize('revision,previous_revision,ceiling,retained', [
    ('lease-strict-integrity-20261017', 'lease-strict-20261017', 323, 279),
    ('lease-strict-policy-20261017', 'lease-strict-integrity-20261017', 367, 323),
])
def test_integrity_strict_revision_retains_all_prior_allocations(
    tmp_path, revision, previous_revision, ceiling, retained,
):
    from ledger import plan_call_limit
    with pytest.raises(Refused, match='ceiling'):
        plan_call_limit({'native_call_ceiling': ceiling, 'budget_revision': previous_revision})
    plan = {'native_call_ceiling': ceiling, 'budget_revision': revision,
            'native_call_limits': {'strict': 22}, 'retained_prior_call_allowances': retained}
    path = tmp_path / 'integrity-strict.sqlite'
    ledger = Ledger(path, plan)
    for arm in ['candidate', 'baseline']:
        for number in range(22):
            identity = f'{arm}-{number}'
            reserve(ledger, identity, case='strict', arm=arm)
            ledger.complete(identity, {'native_complete': True})
        with pytest.raises(Refused, match='case native call ceiling'):
            reserve(ledger, arm + '-extra', case='strict', arm=arm)
    ledger.close()
    with pytest.raises(Refused, match='plan or call ceiling'):
        Ledger(path, {**plan, 'native_call_limits': {'strict': 23}})


def test_current_direct_revision_retains367_and_two_per_arm_without_resume_expansion(tmp_path):
    from ledger import plan_call_limit
    with pytest.raises(Refused, match="ceiling"):
        plan_call_limit({"native_call_ceiling":371,"budget_revision":"lease-strict-policy-20261017"})
    plan = {"native_call_ceiling":371,"budget_revision":"direct-current-20261017",
            "native_call_limits":{"direct":2},"retained_prior_call_allowances":367}
    path = tmp_path / "current-direct.sqlite"
    ledger = Ledger(path, plan)
    for arm in ("candidate", "baseline"):
        for number in range(2):
            call = f"{arm}-{number}"
            assert reserve(ledger, call, case="direct", arm=arm) is None
            ledger.complete(call, {"native_complete":True,"actual_usd":None})
        with pytest.raises(Refused, match="case native call ceiling"):
            reserve(ledger, arm + "-extra", case="direct", arm=arm)
    ledger.close()
    with pytest.raises(Refused, match="plan or call ceiling"):
        Ledger(path, {**plan,"native_call_limits":{"direct":3}})


@pytest.mark.parametrize('revision,previous,ceiling,retained,case,per_arm', [
    ('foundation-current-20261017', 'direct-current-20261017', 399, 371, 'foundation', 14),
    ('foundation-policy-20261017', 'foundation-current-20261017', 427, 399, 'foundation', 14),
    ('history-current-20261017', 'foundation-policy-20261017', 439, 427, 'history', 6),
    ('history-scope-20261017', 'history-current-20261017', 459, 439, 'history', 10),
    ('history-citation-20261017', 'history-scope-20261017', 479, 459, 'history', 10),
    ('history-sources-20261017', 'history-citation-20261017', 499, 479, 'history', 10),
    ('history-obligations-20261017', 'history-sources-20261017', 519, 499, 'history', 10),
])
def test_revision_retains_prior_allocations_and_frozen_case_limits(
    tmp_path, revision, previous, ceiling, retained, case, per_arm,
):
    from ledger import plan_call_limit
    with pytest.raises(Refused, match="ceiling"):
        plan_call_limit({"native_call_ceiling":ceiling,"budget_revision":previous})
    plan = {"native_call_ceiling":ceiling,"budget_revision":revision,
            "native_call_limits":{case:per_arm},"retained_prior_call_allowances":retained}
    path = tmp_path / "frozen-revision.sqlite"
    ledger = Ledger(path, plan)
    for arm in ("candidate", "baseline"):
        for number in range(per_arm):
            call = f"{arm}-{number}"
            assert reserve(ledger, call, case=case, arm=arm) is None
            ledger.complete(call, {"native_complete":True,"cost_usd":None})
        with pytest.raises(Refused, match="case native call ceiling"):
            reserve(ledger, arm + "-extra", case=case, arm=arm)
    ledger.close()
    with pytest.raises(Refused, match="plan or call ceiling"):
        Ledger(path, {**plan,"native_call_limits":{case:per_arm+1}})
