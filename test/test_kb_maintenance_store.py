"""No model calls: durable identity, crash budgets, fairness and true readiness."""
from datetime import datetime
import hashlib
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from infermatrix_copilot.kb_service.ledger import Ledger, LeaseError
from infermatrix_copilot.kb_service.maintenance_store import (
    BudgetExceeded, MaintenanceConflict, MaintenanceStore, budget_date, cycle_date,
)
from infermatrix_copilot.kb_service.maintenance_units import enumerate_units, page_units, select_units

PIN = "a" * 40
SNAP = "b" * 40
POLICY = "c" * 64


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 8, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

    def __call__(self):
        return self.now


@pytest.fixture
def state(tmp_path):
    clock = Clock()
    ledger = Ledger(tmp_path / "kb.db", clock=clock)
    store = MaintenanceStore(ledger)
    yield ledger, store, clock
    ledger.close()


def unit(repo="demo", owner="component", ident="ONE-1a", text=None):
    text = text or f"## {ident} — invariant\n\nUse the contract.\n"
    path = f"repos/{repo}/{owner}/rules.md"
    return page_units(path, text, repo, SNAP)[0]


def start(store, repos=("demo",)):
    return store.begin_cycle(snapshot=SNAP, policy_sha256=POLICY, eligible_repos=repos)


def test_shanghai_slot_and_calendar_budget_are_distinct():
    before = datetime(2026, 10, 8, 0, 59, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    assert cycle_date(before) == "2026-10-07"
    assert budget_date(before) == "2026-10-08"
    assert cycle_date(before + 60) == "2026-10-08"


def test_nightly_restart_keeps_original_snapshot_and_policy(state):
    ledger, store, _ = state
    first = start(store)
    restarted = MaintenanceStore(ledger)
    second = restarted.begin_cycle(snapshot="d" * 40, policy_sha256="e" * 64)
    assert first["created"] and not second["created"]
    assert second["snapshot"] == SNAP and second["policy_sha256"] == POLICY


def test_request_is_durable_idempotent_and_never_a_trial_night(state):
    _, store, _ = state
    store.enqueue_request("manual-1", "demo", {"reason": "report"})
    store.enqueue_request("manual-1", "demo", {"reason": "report"})
    with pytest.raises(MaintenanceConflict):
        store.enqueue_request("manual-1", "other")
    run = store.begin_cycle(snapshot=SNAP, policy_sha256=POLICY, request_id="manual-1", eligible_repos=["demo"])
    item = unit()
    store.add_items(run["id"], [item])
    store.record_outcome(run["id"], item["unit_id"], "verified")
    store.mark_run(run["id"], "complete")
    store.ack_request("manual-1", run["id"])
    assert not store.pending_requests()
    assert store.report()["valid_nights"] == 0


def test_outcomes_immutable_and_original_history_untouched(state):
    ledger, store, _ = state
    ledger.ensure_repo("demo", "shadow")
    ledger.record_verdict("demo", layer="L2", verdict="fail", detail={"original": True})
    before = [tuple(row) for row in ledger._conn.execute("SELECT * FROM verdicts")]
    run, item = start(store), unit()
    store.add_items(run["id"], [item])
    result = store.record_outcome(run["id"], item["unit_id"], "contradicted", detail={"source": PIN})
    assert store.record_outcome(run["id"], item["unit_id"], "contradicted", detail={"source": PIN}) == result
    with pytest.raises(MaintenanceConflict):
        store.record_outcome(run["id"], item["unit_id"], "verified")
    assert [tuple(row) for row in ledger._conn.execute("SELECT * FROM verdicts")] == before
    assert ledger._conn.execute("SELECT COUNT(*) FROM maintenance_findings").fetchone()[0] == 1


def test_unit_snapshot_digest_and_run_denominator_cannot_drift(state):
    _, store, _ = state
    run, item = start(store), unit()
    with pytest.raises(MaintenanceConflict):
        store.add_items(run["id"], [{**item, "snapshot": "different"}])
    with pytest.raises(ValueError, match="digest"):
        store.add_items(run["id"], [{**item, "text": "tampered"}])
    store.add_items(run["id"], [item])
    with pytest.raises(MaintenanceConflict):
        store.add_items(run["id"], [{**item, "lane": "fair"}])
    with pytest.raises(MaintenanceConflict):
        store.mark_run(run["id"], "complete", {"eligible_public_repos": []})


def test_global_priority_budget_preserves_ten_dollar_fair_reserve(state):
    _, store, _ = state
    store.reserve_cost("a", repo="a", owner="owner", worst_cost_usd=40)
    with pytest.raises(BudgetExceeded, match="fair reserve"):
        store.reserve_cost("b", repo="b", owner="owner", worst_cost_usd=.01)
    store.reserve_cost("c", repo="b", owner="owner", worst_cost_usd=10, lane="fair")
    with pytest.raises(BudgetExceeded, match="global"):
        store.reserve_cost("d", repo="a", owner="owner", worst_cost_usd=.01, lane="fair")
    assert store.report()["budget"]["accounted_usd"] == 50


def test_fair_rotation_cannot_spend_priority_allocation(state):
    _, store, _ = state
    store.reserve_cost("fair", repo="a", owner="owner", worst_cost_usd=10, lane="fair")
    with pytest.raises(BudgetExceeded, match="fair rotation"):
        store.reserve_cost("extra", repo="b", owner="owner", worst_cost_usd=.01, lane="fair")
    store.reserve_cost("priority", repo="b", owner="owner", worst_cost_usd=40)
    assert store.report()["budget"]["accounted_usd"] == 50


def test_owner_can_address_positive_observation_without_changing_original_audit(state):
    _, store, _ = state
    run, item = start(store), unit()
    store.add_items(run["id"], [item])
    store.record_outcome(run["id"], item["unit_id"], "verified", detail={"reason": "Original source supports the claim."})
    finding = store.findings(run_id=run["id"])[0]
    assert store.finding(finding["id"])["unit"] == item and finding["outcome"] == "verified"
    store.record_resolution(finding["id"], "confirmed_good_case", "owner", {"source_review": "exact pinned source checked"})
    assert store.items(run["id"])[0]["outcome"] == "verified"
    with pytest.raises(MaintenanceConflict):
        store.record_outcome(run["id"], item["unit_id"], "unknown")


def test_crash_reservation_never_reauthorizes_or_double_bills(state):
    ledger, store, clock = state
    kwargs = dict(repo="demo", owner="owner", worst_cost_usd=2, metadata={"prompt_sha256": "abc"})
    assert store.reserve_cost("call", **kwargs)["call_allowed"]
    restarted = MaintenanceStore(ledger)
    clock.now += 86400
    old = restarted.reserve_cost("call", **kwargs)
    assert not old["created"] and not old["call_allowed"] and old["status"] == "reserved"
    assert old["day"] == "2026-10-08"
    with pytest.raises(MaintenanceConflict, match="metadata"):
        restarted.reserve_cost("call", **{**kwargs, "metadata": {"prompt_sha256": "different"}})
    assert ledger._conn.execute("SELECT COUNT(*) FROM maintenance_budget").fetchone()[0] == 1


@pytest.mark.parametrize("outcome", ["unknown", "failed"])
def test_uncertain_or_failed_cost_stays_conservatively_charged(state, outcome):
    _, store, _ = state
    store.reserve_cost("call", repo="demo", owner="owner", worst_cost_usd=2)
    result = store.settle_cost("call", actual_cost_usd=.1, outcome=outcome)
    assert result["accounted_cost_usd"] == 2 and result["actual_cost_usd"] == .1
    assert store.settle_cost("call", actual_cost_usd=.1, outcome=outcome) == result
    with pytest.raises(MaintenanceConflict):
        store.settle_cost("call", actual_cost_usd=.01, outcome=outcome)


def test_known_api_cost_refunds_and_subscription_has_separate_accounting(state):
    _, store, _ = state
    store.reserve_cost("api", repo="demo", owner="owner", worst_cost_usd=2)
    assert store.settle_cost("api", actual_cost_usd=.2)["accounted_cost_usd"] == .2
    store.reserve_cost("sub", repo="demo", owner="owner", worst_cost_usd=2, cost_kind="subscription")
    store.settle_cost("sub", actual_cost_usd=0)
    report = store.report()["budget"]
    assert report["accounted_usd"] == 2.2 and report["actual_known_usd"] == .2
    assert report["by_cost_kind"] == {"api": .2, "subscription": 2}


@pytest.mark.parametrize("cost", [True, -1, float("nan"), float("inf")])
def test_invalid_budget_values_refused(state, cost):
    _, store, _ = state
    with pytest.raises(ValueError):
        store.reserve_cost("bad", repo="demo", owner="owner", worst_cost_usd=cost)


def test_cost_rounds_up_without_float_cap_bypass(state):
    _, store, _ = state
    row = store.reserve_cost("tiny", repo="demo", owner="owner", worst_cost_usd=.00000001)
    assert row["worst_microusd"] == 1


def test_live_lease_must_be_owned_but_requests_can_queue(state):
    ledger, store, clock = state
    owner = ledger.acquire_lease("scheduler", ttl=30)
    with pytest.raises(LeaseError):
        start(store)
    store.enqueue_request("request", "demo")
    fenced = MaintenanceStore(ledger, lease_owner=owner)
    start(fenced)
    clock.now += 31
    with pytest.raises(LeaseError):
        fenced.mark_run("nightly:2026-10-08", "complete")


def test_usage_distinguishes_retrieval_from_actual_injection(state):
    _, store, _ = state
    store.record_usage("r", "unit", kind="retrieved", count=5)
    store.record_usage("r", "unit", kind="retrieved", count=5)
    store.record_usage("i", "unit", kind="injected")
    assert store.usage() == {"unit": {"retrieved": 5, "injected": 1}}
    with pytest.raises(MaintenanceConflict):
        store.record_usage("r", "unit", kind="injected", count=5)


def test_priority_distinguishes_actual_injection_from_equal_retrieval_count():
    retrieved, injected, cold = [unit(ident=name) for name in ("GET-1a", "USE-1a", "COLD-1a")]
    usage = {retrieved["unit_id"]: {"retrieved": 10, "injected": 0},
             injected["unit_id"]: {"retrieved": 0, "injected": 10}}
    chosen = select_units([cold, retrieved, injected], {}, 3, "night", usage, fair_fraction=0, now=100)
    assert [u["unit_id"] for u in chosen] == [injected["unit_id"], retrieved["unit_id"], cold["unit_id"]]


def test_seven_distinct_real_complete_public_repo_nights_required(state):
    _, store, clock = state
    for day in range(8):
        run = start(store, ("demo", "other"))
        units = [unit(), unit("other")]
        store.add_items(run["id"], units)
        for item in units:
            store.record_outcome(run["id"], item["unit_id"], "unknown" if day == 0 else "verified")
        store.mark_run(run["id"], "complete")
        if day == 6:
            assert not store.report()["seven_valid_nights_ready"]
        if day < 7:
            clock.now += 86400
    assert store.report()["valid_nights"] == 7
    assert store.report()["seven_valid_nights_ready"]


def test_empty_missing_repo_and_budget_limited_nights_never_ready(state):
    _, store, clock = state
    start(store)
    store.mark_run("nightly:2026-10-08", "complete")
    clock.now += 86400
    run, item = start(store, ("demo", "other")), unit()
    store.add_items(run["id"], [item])
    store.record_outcome(run["id"], item["unit_id"], "verified")
    store.mark_run(run["id"], "complete")
    clock.now += 86400
    run = start(store)
    store.add_items(run["id"], [item])
    store.record_outcome(run["id"], item["unit_id"], "verified")
    store.mark_run(run["id"], "complete", {"budget_limited": True})
    assert store.report()["valid_nights"] == 0


def test_current_coverage_keeps_unknown_and_changed_identity_in_denominator(state):
    _, store, clock = state
    run, first, second = start(store), unit(), unit(ident="TWO-1a")
    store.add_items(run["id"], [first, second])
    store.record_outcome(run["id"], first["unit_id"], "verified")
    store.record_outcome(run["id"], second["unit_id"], "unknown")
    clock.now += 100
    assert store.report(units=[first, second])["coverage"] == {
        "eligible": 2, "outcomes": {"verified": 1, "unknown": 1}, "never_reviewed": 0,
        "stale_identity": 0, "oldest_review_age_seconds": 100, "attempted": 2, "successful": 1}
    changed = {**first, "content_sha256": "different"}
    assert store.report(units=[changed, second])["coverage"]["stale_identity"] == 1


def test_units_include_rules_depth_foundation_legacy_without_hash_as_pin():
    lifecycle = SimpleNamespace(repo="demo", knowledge_dir="repos/demo", protected_rules=("RULE-1a",))
    depth = f"<!-- kb:depth feature=example facet=api pin={PIN} sha256={'d'*64} -->\nAPI fact\n<!-- /kb:depth -->"
    foundation = f"<!-- kb:knowledge owner=example facet=configuration pin={PIN} verdict=pass -->\nSetting fact\n"
    files = {"repos/demo/rules.md": "## RULE-1a — rule\n\nMust preserve.\n",
             "repos/demo/topic.md": foundation + depth,
             "repos/demo/legacy.md": "# Explanation\n\nLegacy fact.\n"}
    units = enumerate_units(files, lifecycle, SNAP)
    assert {u["kind"] for u in units} == {"rule", "depth", "knowledge", "legacy"}
    assert next(u for u in units if u["kind"] == "rule")["protected"]
    assert all(u["upstream_pin"] == PIN for u in units if u["kind"] in ("depth", "knowledge"))
    assert all(hashlib.sha256(u["text"].encode()).hexdigest() == u["content_sha256"] for u in units)


def test_retired_rule_excluded_duplicate_and_malformed_rules_protected():
    text = "## DUP-1a — first\nA\n\n## DUP-1a — second\nB\n"
    units = page_units("repos/demo/rules.md", text, "demo", SNAP)
    assert len(units) == 2 and len({u["unit_id"] for u in units}) == 2
    assert all(u["protected"] for u in units)
    retired = "## OLD-1a — gone\nOld\n\n<!-- kb:rule status=retired since=v1 retired_at=v2 reason=incorrect evidence=proof -->\n"
    assert page_units("repos/demo/rules.md", retired, "demo", SNAP) == []
    malformed = "## BAD-1a — malformed\nFact\n\n<!-- kb:rule status=bogus -->\n"
    assert page_units("repos/demo/rules.md", malformed, "demo", SNAP)[0]["protected"]


def test_selection_deterministic_fair_across_repos_and_owners_and_content_changes():
    units = [unit("large", f"owner{i}", f"RULE-{i}a") for i in range(10)] + [unit("small")]
    chosen = select_units(units, {}, 10, "date", fair_fraction=.2, now=100)
    assert {u["repo"] for u in chosen if u["lane"] == "fair"} == {"large", "small"}
    assert chosen == select_units(list(reversed(units)), {}, 10, "date", fair_fraction=.2, now=100)
    history = {u["unit_id"]: {"content_sha256": u["content_sha256"], "upstream_pin": "", "finished_at": 99,
                                "outcome": "verified"} for u in units}
    target = units[-1]
    history[target["unit_id"]]["content_sha256"] = "old"
    assert select_units(units, history, 1, "date", fair_fraction=0, now=100)[0]["unit_id"] == target["unit_id"]


def test_fair_release_is_persistent_and_requires_selected_fair_attempts(state):
    ledger, store, _ = state
    run, item = start(store), {**unit(), "lane": "fair"}
    store.add_items(run["id"], [item])
    store.reserve_cost("priority", repo="demo", owner="owner", worst_cost_usd=40)
    with pytest.raises(MaintenanceConflict, match="unfinished"):
        store.release_fair_reserve()
    store.record_outcome(run["id"], item["unit_id"], "unknown")
    store.release_fair_reserve(reason="all selected fair units attempted")
    restarted = MaintenanceStore(ledger)
    restarted.reserve_cost("borrow", repo="demo", owner="owner", worst_cost_usd=10)
    assert restarted.report()["budget"]["fair_reserve_released"]
    with pytest.raises(BudgetExceeded):
        restarted.reserve_cost("over", repo="demo", owner="owner", worst_cost_usd=.01)


def test_separate_connections_share_budget_and_freeze_day_policy(state):
    ledger, store, clock = state
    other_ledger = Ledger(ledger.path, clock=clock)
    try:
        other = MaintenanceStore(other_ledger)
        store.reserve_cost("one", repo="demo", owner="owner", worst_cost_usd=39)
        with pytest.raises(BudgetExceeded):
            other.reserve_cost("two", repo="other", owner="owner", worst_cost_usd=2)
        changed = MaintenanceStore(other_ledger, daily_limit_usd=100)
        with pytest.raises(MaintenanceConflict, match="policy"):
            changed.reserve_cost("two", repo="other", owner="owner", worst_cost_usd=2)
    finally:
        other_ledger.close()


def test_readiness_requires_current_policy_roster_and_consecutive_dates(state):
    _, store, clock = state
    for _ in range(7):
        run, item = start(store), unit()
        store.add_items(run["id"], [item])
        store.record_outcome(run["id"], item["unit_id"], "verified")
        store.mark_run(run["id"], "complete")
        clock.now += 86400
    # No current scheduled run: historical seven nights cannot hide a missed date.
    assert not store.report()["seven_valid_nights_ready"]
    clock.now -= 86400
    assert store.report(policy_sha256=POLICY, eligible_repos=["demo"])["seven_valid_nights_ready"]
    assert not store.report(policy_sha256="other-policy", eligible_repos=["demo"])["seven_valid_nights_ready"]
    assert not store.report(policy_sha256=POLICY, eligible_repos=["demo", "missing"])["seven_valid_nights_ready"]


def test_findings_and_resolutions_append_without_rewriting_outcome(state):
    _, store, _ = state
    run, item = start(store), unit()
    store.add_items(run["id"], [item])
    store.record_outcome(run["id"], item["unit_id"], "contradicted", detail={"proof": "source"})
    finding = store.findings()[0]
    assert store.finding(finding["id"])["unit"] == item
    resolution = store.record_resolution(finding["id"], "confirmed", "owner", {"review": "record"})
    assert store.record_resolution(finding["id"], "confirmed", "owner", {"review": "record"}) == resolution
    with pytest.raises(MaintenanceConflict):
        store.record_resolution(finding["id"], "false_positive", "owner", {"review": "record"},
                                resolution_id=resolution["id"])
    assert store.items(run["id"])[0]["outcome"] == "contradicted"


def test_completed_run_cannot_gain_future_items(state):
    _, store, _ = state
    run = start(store)
    store.mark_run(run["id"], "complete")
    with pytest.raises(MaintenanceConflict, match="new audit items"):
        store.add_items(run["id"], [unit()])


def test_sources_keep_structured_primary_spans_and_bound_large_page_metadata():
    source = {"repository": "org/demo", "sha": PIN, "path": "src/file.py", "start": 1, "end": 2}
    import yaml
    front = "---\n" + yaml.safe_dump({"sources": [source, "repos/demo/old.md", *[f"src/file{i}.py" for i in range(100)]]}) + "---\n"
    result = page_units("repos/demo/rules.md", front + "## RULE-1a — contract\nMust preserve.\n", "demo", SNAP)[0]
    assert source in result["sources"] and "repos/demo/old.md" not in result["sources"]
    assert result["upstream_pin"] == PIN and len(result["sources"]) == 64 and result["sources_truncated"]


def test_running_eighth_cycle_can_use_seven_completed_trial_nights(state):
    _, store, clock = state
    for _ in range(7):
        run, item = start(store), unit()
        store.add_items(run["id"], [item])
        store.record_outcome(run["id"], item["unit_id"], "verified")
        store.mark_run(run["id"], "complete")
        clock.now += 86400
    assert not store.report()["seven_valid_nights_ready"]  # absent scheduled cycle
    eighth = start(store)
    assert store.report()["seven_valid_nights_ready"]
    assert store.report()["consecutive_valid_nights"] == 7
    store.mark_run(eighth["id"], "failed", {"errors": ["execution error"]})
    assert not store.report()["seven_valid_nights_ready"]


def test_block_primary_sources_do_not_inherit_unrelated_page_applicability_pin():
    old_pin = "f" * 40
    old_url = f"https://github.com/org/demo/blob/{old_pin}/old.py#L1"
    own_url = f"https://github.com/org/demo/blob/{PIN}/new.py#L1"
    text = f'---\nsources: ["{old_url}"]\n---\n## RULE-1a — contract\nUse current behavior.\n{own_url}\n'
    result = page_units("repos/demo/rules.md", text, "demo", SNAP)[0]
    assert result["upstream_pin"] == PIN
    assert result["primary_sources"] == [own_url] and result["page_sources"] == [old_url]
    assert {own_url, old_url} == set(result["sources"])


def test_late_resumption_does_not_backfill_missed_trial_nights(state):
    _, store, clock = state
    old, item = start(store), unit()
    store.add_items(old["id"], [item])
    clock.now += 86400
    second = start(store)
    store.add_items(second["id"], [item])
    clock.now += 86400
    for resumed in (old, second):
        store.record_outcome(resumed["id"], item["unit_id"], "verified")
        store.mark_run(resumed["id"], "complete")
    current = start(store)
    store.add_items(current["id"], [item])
    store.record_outcome(current["id"], item["unit_id"], "verified")
    store.mark_run(current["id"], "complete")
    report = store.report()
    assert report["valid_nights"] == 1 and report["consecutive_valid_nights"] == 1
    assert all(night["real_review_repos"] == [] for night in report["nights"][:2])


def test_fair_selection_covers_each_repo_when_limit_can_fit_roster():
    units = [unit("large", f"owner{i}", f"RULE-{i}a") for i in range(10)] \
        + [unit("second"), unit("third")]
    chosen = select_units(units, {}, 3, "date", now=100)
    assert {u["repo"] for u in chosen} == {"large", "second", "third"}
    assert all(u["lane"] == "fair" for u in chosen)
    assert chosen == select_units(list(reversed(units)), {}, 3, "date", now=100)


def test_selection_order_survives_storage_restart(state):
    ledger, store, _ = state
    run = start(store)
    units = [unit(ident=f"RULE-{i}a") for i in range(8)]
    chosen = select_units(units, {}, 8, "date", now=100)
    assert [u["selection_rank"] for u in chosen] == list(range(8))
    store.add_items(run["id"], list(reversed(chosen)))
    restored = MaintenanceStore(ledger).items(run["id"])
    assert [item["unit_id"] for item in restored] == [u["unit_id"] for u in chosen]


def test_coverage_reports_semantic_success_separately_from_attempts(state):
    _, store, _ = state
    run = start(store)
    units = [unit(ident=f"RULE-{i}a") for i in range(4)]
    store.add_items(run["id"], units)
    for item, outcome in zip(units, ("verified", "contradicted", "unknown", "execution_error")):
        store.record_outcome(run["id"], item["unit_id"], outcome)
    coverage = store.report(units=units)["coverage"]
    assert coverage["attempted"] == 4 and coverage["successful"] == 2
    assert coverage["never_reviewed"] == coverage["stale_identity"] == 0


def test_unstructured_prose_on_rule_page_is_a_protected_whole_page_unit():
    path = "repos/demo/rules.md"
    rule = "## RULE-1a — invariant\n\nMust preserve the contract.\n"
    structured = page_units(path, "# Rules\n\n" + rule, "demo", SNAP)
    text = "# Rules\n\nThe default queue capacity is sixteen.\n\n" + rule
    mixed = page_units(path, text, "demo", SNAP)
    assert next(u for u in mixed if u["kind"] == "rule") == structured[0]
    prose = next(u for u in mixed if u["kind"] == "legacy")
    assert prose["block_id"] == "legacy:page" and prose["protected"]
    assert prose["text"] == text and prose["content_sha256"] == hashlib.sha256(text.encode()).hexdigest()


def test_uncovered_generated_page_prose_includes_all_pins_and_is_not_auto_correctable():
    other = "f" * 40
    text = (f'---\nsources: ["https://github.com/org/demo/blob/{other}/old.py#L1"]\n---\n'
            f"# API\n\nThe default remains sixteen.\n\n"
            f"<!-- kb:knowledge owner=core facet=configuration pin={PIN} verdict=pass -->\n"
            "The current setting is eight.\n")
    units = page_units("repos/demo/api.md", text, "demo", SNAP)
    assert {u["kind"] for u in units} == {"knowledge", "legacy"}
    prose = next(u for u in units if u["kind"] == "legacy")
    assert prose["protected"] and prose["upstream_pin"] == ""
    assert set(prose["upstream_pins"]) == {PIN, other}


def test_routing_and_source_boilerplate_does_not_duplicate_a_rule_unit():
    text = (f"# Rules\n\n- [Related rules](rules-other.md)\nSource: https://github.com/org/demo/blob/{PIN}/q.py#L1\n\n"
            "## RULE-1a — invariant\n\nMust preserve.\n")
    assert [u["kind"] for u in page_units("repos/demo/rules.md", text, "demo", SNAP)] == ["rule"]


def test_high_impact_cold_unit_precedes_equal_age_low_impact_and_unstructured_protection():
    rule = "## RULE-1a — invariant\n\nPreserve the contract.\n"
    high = page_units("repos/demo/high/rules.md", "---\nimpact: high\n---\n" + rule, "demo", SNAP)[0]
    low = page_units("repos/demo/low/rules.md", rule, "demo", SNAP)[0]
    mixed = page_units("repos/demo/mixed/rules.md", "# Rules\n\nA default is sixteen.\n\n" + rule, "demo", SNAP)
    prose = next(u for u in mixed if u["kind"] == "legacy")
    assert prose["protected"] and prose["risk_score"] == 0
    assert high["risk_score"] > low["risk_score"] and "page_impact_high" in high["risk_reasons"]
    assert select_units([prose, low, high], {}, 1, "night", fair_fraction=0, now=100)[0]["unit_id"] == high["unit_id"]


def test_recovery_cannot_create_missing_reservation_or_daily_policy(state):
    ledger, store, _ = state
    with pytest.raises(MaintenanceConflict, match='original cost reservation is missing'):
        store.reserve_cost('missing', repo='demo', owner='owner', worst_cost_usd=2,
                           require_existing=True)
    assert ledger._conn.execute('SELECT COUNT(*) FROM maintenance_budget').fetchone()[0] == 0
    assert ledger._conn.execute('SELECT COUNT(*) FROM maintenance_budget_policy').fetchone()[0] == 0
