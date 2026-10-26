"""Explicit uncapped depth keeps accounting and cannot dispatch paid fallbacks."""

import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service import cli, runner
from infermatrix_copilot.kb_service.init_budget import Budget, BudgetExhausted
from infermatrix_copilot.kb_service.init_history import _checkpoint_budget
from infermatrix_copilot.kb_service.init_stages import run_stage
from infermatrix_copilot.kb_service.init_support import InitError, InitRecord, generate
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole, ModelUnavailable
from infermatrix_copilot.llm import Block, Reply
from test_kb_init_modules import _modules_lifecycle
from test_kb_init_skeleton import _runtime, world  # noqa: F401
from test_kb_knowledge_depth import DepthGateway, _baseline, _complete_breadth
from test_kb_init_skeleton import _commit
from infermatrix_copilot.kb_service.knowledge_coverage import policy_path


def test_uncapped_checkpoint_keeps_prior_spend_and_crash_reservation(tmp_path):
    record = InitRecord(stage="knowledge-deepen", repo="toy", spent_usd=60.0)
    capped = _checkpoint_budget(60.0, record, tmp_path)
    assert not capped.can_reserve(0.5)
    with pytest.raises(BudgetExhausted):
        with capped.reserve(0.5):
            pytest.fail("exhausted capped budget dispatched a call")
    uncapped = _checkpoint_budget(None, record, tmp_path)
    assert uncapped.limit_usd is None and uncapped.remaining_usd is None
    with pytest.raises(RuntimeError, match="interrupted"):
        with uncapped.reserve(0.5):
            checkpoint = InitRecord.load(tmp_path, "toy", "knowledge-deepen")
            assert checkpoint.spent_usd == 60.5
            raise RuntimeError("interrupted")
    resumed = _checkpoint_budget(None, InitRecord.load(tmp_path, "toy", "knowledge-deepen"), tmp_path)
    with resumed.reserve(0.5) as reservation:
        reservation.charge(0.5)
    assert resumed.spent_usd == 61.0
    json.dumps({"ceiling": resumed.limit_usd, "accounted": resumed.spent_usd}, allow_nan=False)


@pytest.mark.parametrize("amount", [-1.0, float("inf"), float("nan")])
def test_uncapped_budget_still_rejects_invalid_reservations(amount):
    with pytest.raises(ValueError, match="finite"):
        Budget(None).can_reserve(amount)


@pytest.mark.parametrize("command", [["deepen", "toy"], ["init", "toy", "--stage", "knowledge-deepen"]])
def test_cli_forwards_explicit_unlimited_mode_without_a_ceiling(tmp_path, monkeypatch, command):
    seen = []

    def run(settings, playbook, repo, *, state_dir, params):
        seen.append(params)
        return SimpleNamespace(status="done"), tmp_path

    monkeypatch.setattr(runner, "run_playbook", run)
    monkeypatch.setattr(cli, "_ledger", lambda *_: pytest.fail("init opened kb.db"))
    assert cli.main(command + ["--unlimited-subscription", "--dry-run"]) == 0
    assert seen[0]["unlimited_subscription"] == "true"
    assert seen[0]["stage"] == "knowledge-deepen" and "budget_usd" not in seen[0]


@pytest.mark.parametrize("command", [
    ["init", "toy", "--stage", "skeleton"],
    ["init", "toy", "--stage", "deepen"],
    ["init", "toy", "--suggest-seeds"],
    ["deepen", "toy", "--budget-usd", "100"],
    ["init", "toy", "--stage", "knowledge-deepen", "--budget-usd", "100"],
])
def test_cli_rejects_unlimited_mode_outside_depth_or_with_budget(monkeypatch, command):
    monkeypatch.setattr(runner, "run_playbook", lambda *_a, **_k: pytest.fail("invalid options ran a playbook"))
    assert cli.main(command + ["--unlimited-subscription", "--dry-run"]) == 2


class Transport:
    def __init__(self, *, subscription=True):
        self.subscription_billing = subscription
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return Reply(blocks=[Block(type="text", text='{"sections": []}')],
                     model="reported-model", usage={"input_tokens": 12})


@pytest.mark.parametrize("unavailable_role", ["generator", "judge"])
def test_unlimited_stage_rejects_either_paid_transport_before_calls(world, unavailable_role):
    transports = {"zcode": Transport(subscription=unavailable_role != "generator"),
                  "codex": Transport(subscription=unavailable_role != "judge")}
    rt = _runtime(world, ModelGateway(None, transport_factory=transports.__getitem__),
                  generator=ModelRole("generator", "zcode", "GLM-5.3"))
    with pytest.raises(InitError, match=unavailable_role + ":.*authenticated subscription"):
        run_stage(rt, _modules_lifecycle(), "knowledge-deepen", dry_run=True,
                  from_existing=True, unlimited_subscription=True)
    assert not any(t.calls for t in transports.values())


def test_unlimited_stage_refuses_unauthenticated_transport_before_calls(world):
    def unavailable(provider):
        raise ModelUnavailable("CLI is not logged in")

    rt = _runtime(world, ModelGateway(None, transport_factory=unavailable),
                  generator=ModelRole("generator", "zcode", "GLM-5.3"))
    with pytest.raises(InitError, match="authenticated subscription backend is unavailable"):
        run_stage(rt, _modules_lifecycle(), "knowledge-deepen", dry_run=True,
                  from_existing=True, unlimited_subscription=True)


def test_unlimited_stage_refuses_configured_fallback_before_dispatch(world):
    transport = Transport()
    generator = ModelRole("generator", "zcode", "GLM-5.3",
                          fallback=ModelRole("generator", "other", "paid-model"))
    rt = _runtime(world, ModelGateway(None, transport_factory=lambda _: transport), generator=generator)
    with pytest.raises(InitError, match="without fallback"):
        run_stage(rt, _modules_lifecycle(), "knowledge-deepen", dry_run=True,
                  from_existing=True, unlimited_subscription=True)
    assert transport.calls == []


@pytest.mark.parametrize("generator,judge", [
    (ModelRole("generator", "claude-code", "claude-opus-5-5"), ModelRole("judge", "codex", "gpt-6-sol")),
    (ModelRole("generator", "zcode", "GLM-5.4"), ModelRole("judge", "codex", "gpt-6-sol")),
    (ModelRole("generator", "zcode", "GLM-5.3"), ModelRole("judge", "other", "gpt-6-sol")),
])
def test_unlimited_stage_rejects_a_different_model_protocol_before_auth_or_calls(world, generator, judge):
    gateway = ModelGateway(None, transport_factory=lambda _: pytest.fail("wrong protocol reached auth or dispatch"))
    rt = _runtime(world, gateway, generator=generator, judge=judge)
    with pytest.raises(InitError, match="Zcode GLM-5.3.*independent Codex"):
        run_stage(rt, _modules_lifecycle(), "knowledge-deepen", dry_run=True,
                  from_existing=True, unlimited_subscription=True)


@pytest.mark.parametrize("stage, options", [
    ("skeleton", {"unlimited_subscription": True}),
    ("knowledge-deepen", {"unlimited_subscription": True, "budget_usd": 100.0}),
    ("knowledge-deepen", {"unlimited_subscription": "true"}),
])
def test_stage_api_rejects_invalid_unlimited_options_before_dispatch(world, stage, options):
    transport = Transport()
    rt = _runtime(world, ModelGateway(None, transport_factory=lambda _: transport))
    with pytest.raises(InitError, match="unlimited"):
        run_stage(rt, _modules_lifecycle(), stage, dry_run=True, **options)
    assert transport.calls == []


def test_unlimited_generate_rechecks_billing_and_preserves_unknown_cost(world):
    subscription, paid = Transport(), Transport(subscription=False)
    transports = iter([subscription, paid])
    rt = _runtime(world, ModelGateway(None, transport_factory=lambda _: next(transports)),
                  generator=ModelRole("generator", "zcode", "GLM-5.3"), unlimited_subscription=True)
    budget = Budget(None)
    reply = generate(rt, budget, _modules_lifecycle().init, system="extract", prompt="source")
    assert reply.cost_usd is None and reply.usage == {"input_tokens": 12}
    assert "max_budget_usd" not in subscription.calls[0]
    with pytest.raises(ModelUnavailable, match="authenticated subscription"):
        generate(rt, budget, _modules_lifecycle().init, system="extract", prompt="source")
    assert paid.calls == []


def test_unlimited_depth_runs_past_adapter_ceiling_and_accounts_judges(world):
    _baseline(world, features=2)
    gateway = DepthGateway()
    lifecycle = _modules_lifecycle()
    lifecycle = replace(lifecycle, init=replace(lifecycle.init, budget_usd=0.5))
    rt = _runtime(world, gateway, generator=ModelRole("generator", "zcode", "GLM-5.3"),
                  state_dir=world["tmp"] / "unlimited-depth")
    record = run_stage(rt, lifecycle, "knowledge-deepen", dry_run=True, from_existing=True,
                       unlimited_subscription=True)
    assert record.status == "dry_run", record.problems
    assert record.coverage["semantic_depth"]["complete_features"] == 2
    assert record.depth["done"] and len(gateway.depth_calls) == 2
    assert record.spent_usd == 1.0
    assert any("observability only" in note and "remain unknown" in note for note in record.notes)


def test_execution_partition_keeps_global_denominator_and_publication_gate(world):
    import yaml

    policy = _baseline(world, features=2)
    _complete_breadth(world, policy)
    path = policy_path("toy")
    original = subprocess.check_output(
        ["git", "-C", str(world["origin"]), "show", "HEAD:" + path], text=True)
    policy = yaml.safe_load(original)
    policy["semantic_depth"] = {"per_facet_gt": 0.90}
    _commit(world["origin"], {path: yaml.safe_dump(policy)}, "require full semantic target")
    gateway = DepthGateway()
    rt = _runtime(world, gateway, generator=ModelRole("generator", "zcode", "GLM-5.3"),
                  state_dir=world["tmp"] / "partition")
    record = run_stage(rt, _modules_lifecycle(), "knowledge-deepen", dry_run=True,
                       from_existing=True, unlimited_subscription=True, feature_ids=("step0",))
    report = record.coverage["semantic_depth"]
    assert record.status == "partial" and not record.depth["done"]
    assert report["recognized_facets"] == 7 and not report["target_met"]
    assert report["recognized_facet_ratio"] == 0.5
    assert set(report["features"]) == {"step0", "step1"}
    assert all(row["recognized"] == 1 and not row["target_met"] for row in report["facet_counts"].values())
    assert len(gateway.depth_calls) == 1


def test_structural_gap_blocks_strict_campaign_before_model_calls(world):
    import yaml

    _baseline(world)
    path = policy_path("toy")
    data = yaml.safe_load((world["origin"] / path).read_text())
    data["semantic_depth"] = {"per_facet_gt": 0.90}
    _commit(world["origin"], {path: yaml.safe_dump(data)}, "strict policy without structural baseline")
    gateway = DepthGateway()
    rt = _runtime(world, gateway, generator=ModelRole("generator", "zcode", "GLM-5.3"),
                  state_dir=world["tmp"] / "missing-breadth")
    record = run_stage(rt, _modules_lifecycle(), "knowledge-deepen", dry_run=True,
                       from_existing=True, unlimited_subscription=True)
    assert record.status == "blocked" and not record.depth["done"]
    assert not record.coverage["breadth"]["met"]
    assert "structural coverage" in record.problems[0]
    assert gateway.depth_calls == []


def test_explicit_feature_order_only_changes_execution_not_global_audit(world):
    _baseline(world, features=2)
    gateway = DepthGateway()
    rt = _runtime(world, gateway, generator=ModelRole("generator", "zcode", "GLM-5.3"),
                  state_dir=world["tmp"] / "ordered-partition")
    record = run_stage(rt, _modules_lifecycle(), "knowledge-deepen", dry_run=True,
                       from_existing=True, unlimited_subscription=True, feature_ids=("step1", "step0"))
    assert [payload["feature"]["id"] for payload in gateway.depth_calls] == ["step1", "step0"]
    assert record.coverage["semantic_depth"]["total_features"] == 2
    assert record.coverage["semantic_depth"]["total_facets"] == 14
