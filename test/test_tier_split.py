"""Per-tier model backends + served-model guard (plan v2).

Pins: atomic tier-config inheritance, the upfront performance-unconfigured
failure, alias normalization, the fail/warn/unverified guard verdicts, paid-
mismatch MoA settlement, served-model metrics pricing with the partial-cost
contract, tier-independent extra briefing docs, and doctor's static backend
check."""

from types import SimpleNamespace

import pytest

from infermatrix_copilot.config import (
    ResolvedTarget,
    Settings,
    TierNotConfiguredError,
)
from infermatrix_copilot.llm import (
    LLM,
    ModelMismatchError,
    Reply,
    canonical_model,
)


def _settings(**kw) -> Settings:
    kw.setdefault("agent_model", "deepseek-v4-pro[1m]")
    return Settings(_env_file=None, anthropic_api_key="global-key",
                    anthropic_base_url="https://api.deepseek.com/anthropic",
                    **kw)


# ── config: atomic inheritance ────────────────────────────────────────────────

def test_tier_unset_inherits_global_backend():
    t = _settings().tier_target("eco")
    assert (t.model, t.base_url, t.api_key, t.source) == (
        "deepseek-v4-pro[1m]", "https://api.deepseek.com/anthropic",
        "global-key", "global")
    assert t.host == "api.deepseek.com"


def test_tier_model_only_rides_global_backend():
    t = _settings(performance_model="deepseek-v4-pro").tier_target("performance")
    assert t.model == "deepseek-v4-pro" and t.api_key == "global-key"
    assert t.source == "global"


def test_tier_full_triple_is_independent_backend():
    s = _settings(performance_model="claude-opus-4-8",
                  performance_base_url="https://api.anthropic.com",
                  performance_api_key="perf-key")
    t = s.tier_target("performance")
    assert (t.model, t.host, t.api_key, t.source) == (
        "claude-opus-4-8", "api.anthropic.com", "perf-key", "tier:performance")


def test_partial_tier_credentials_rejected_at_startup():
    with pytest.raises(Exception, match="partial performance backend"):
        _settings(performance_model="m",
                  performance_base_url="https://x.example")
    with pytest.raises(Exception, match="partial eco backend"):
        _settings(eco_api_key="k")


def test_performance_unconfigured_fails_upfront():
    with pytest.raises(TierNotConfiguredError, match="PERFORMANCE_MODEL"):
        _settings().tier_target("performance")


# ── guard: normalization + verdicts ───────────────────────────────────────────

def test_canonical_model_strips_variant_and_case():
    assert canonical_model("Claude-Opus-4-8[1m]") == "claude-opus-4-8"
    assert canonical_model("deepseek-v4-pro") == "deepseek-v4-pro"


def test_canonical_model_applies_audited_aliases():
    aliases = {"claude-opus-4-8": "claude-opus-4-8-20260115"}
    assert canonical_model("claude-opus-4-8[1m]", aliases) == \
        canonical_model("claude-opus-4-8-20260115", aliases)


class _FakeResp:
    def __init__(self, model="", usage_tokens=(5, 1)):
        self.content = [SimpleNamespace(type="text", text="ok")]
        self.stop_reason = "end_turn"
        self.model = model
        self._request_id = "req_test123"
        self.usage = SimpleNamespace(
            input_tokens=usage_tokens[0], output_tokens=usage_tokens[1],
            cache_read_input_tokens=0, cache_creation_input_tokens=0)


def _fake_llm(settings, served_model):
    llm = object.__new__(LLM)
    llm.settings = settings
    llm._default_model = ""
    llm._endpoint_host = "api.deepseek.com"
    llm._client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: _FakeResp(model=served_model)))
    return llm


def test_guard_mismatch_fails_and_carries_paid_reply():
    llm = _fake_llm(_settings(), "deepseek-v4-pro")
    with pytest.raises(ModelMismatchError) as exc:
        llm.create(system="s", messages=[{"role": "user", "content": "hi"}],
                   model="claude-opus-4-8[1m]")
    err = exc.value
    assert err.requested == "claude-opus-4-8[1m]"
    assert err.served == "deepseek-v4-pro"
    assert err.reply is not None and err.reply.usage["input_tokens"] == 5


def test_guard_variant_suffix_is_not_a_mismatch():
    llm = _fake_llm(_settings(), "deepseek-v4-pro")
    r = llm.create(system="s", messages=[{"role": "user", "content": "hi"}],
                   model="deepseek-v4-pro[1m]")
    assert r.model == "deepseek-v4-pro" and r.request_id == "req_test123"


def test_guard_warn_policy_continues():
    llm = _fake_llm(_settings(model_mismatch_policy="warn"), "deepseek-v4-pro")
    r = llm.create(system="s", messages=[{"role": "user", "content": "hi"}],
                   model="claude-opus-4-8[1m]")
    assert r.model == "deepseek-v4-pro"  # served model recorded on the reply


def test_guard_absent_served_model_is_unverified_not_fatal():
    llm = _fake_llm(_settings(), "")
    r = llm.create(system="s", messages=[{"role": "user", "content": "hi"}],
                   model="deepseek-v4-pro[1m]")
    assert r.model == ""


def test_for_target_reuses_shared_client_and_sets_default_model():
    s = _settings()
    llm = _fake_llm(s, "deepseek-v4-pro")
    target = s.tier_target("eco")
    clone = llm.for_target(target)
    assert clone._client is llm._client  # same backend — client reused
    r = clone.create(system="s", messages=[{"role": "user", "content": "hi"}])
    assert r.model == "deepseek-v4-pro"  # default model came from the target


# ── MoA: paid mismatch settles, never releases ────────────────────────────────

def test_mismatch_settles_moa_budget():
    from infermatrix_copilot.engine.agent_runtime.moa import (
        Member,
        MoaBudget,
    )

    s = _settings()
    budget = MoaBudget.start(s)
    llm = _fake_llm(s, "claude-sonnet-5")
    with budget.bind(Member(model="deepseek-v4-pro")):
        with pytest.raises(ModelMismatchError):
            llm.create(model="deepseek-v4-pro", system="s", messages=[], max_tokens=10)
    assert budget.spent() > 0  # settled actual spend, not released
    assert not budget._reserved  # and the reservation is gone


# ── metrics: served-model pricing + partial contract ──────────────────────────

def test_cost_from_spans_prices_by_served_model(tmp_path):
    import json

    from infermatrix_copilot.metrics import cost_from_spans

    trace = tmp_path / "trace.jsonl"
    span = {"name": "llm", "attr": {
        "model": "claude-opus-4-8[1m]", "served_model": "deepseek-v4-pro",
        "prompt_tokens": 1_000_000, "completion_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0, "role": "agent"}}
    trace.write_text(json.dumps(span) + "\n", encoding="utf-8")
    cost = cost_from_spans(trace, None)
    assert cost["usd"] == pytest.approx(0.27)  # deepseek row, NOT opus's 15.0
    assert cost["cost_partial"] is False


def test_cost_from_spans_flags_unpriced_calls(tmp_path):
    import json

    from infermatrix_copilot.metrics import cost_from_spans

    trace = tmp_path / "trace.jsonl"
    rows = [
        {"name": "llm", "attr": {"model": "deepseek-v4-pro",
                                 "prompt_tokens": 1_000_000,
                                 "completion_tokens": 0,
                                 "cache_read_tokens": 0,
                                 "cache_creation_tokens": 0}},
        {"name": "llm", "attr": {"model": "mystery-model-9000",
                                 "prompt_tokens": 500,
                                 "completion_tokens": 500,
                                 "cache_read_tokens": 0,
                                 "cache_creation_tokens": 0}},
    ]
    trace.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    cost = cost_from_spans(trace, None)
    assert cost["cost_partial"] is True and cost["unpriced_calls"] == 1
    assert cost["usd"] == pytest.approx(0.27)  # lower bound: priced calls only
    assert cost["llm_calls"] == 2  # the unpriced call still counts + its tokens


# ── briefing: tier boundary is model-only ─────────────────────────────────────

def _adapter(tmp_path, knowledge: dict):
    from infermatrix_copilot.adapters.base import RepoAdapter

    root = tmp_path / "adapter"
    root.mkdir(parents=True, exist_ok=True)
    return RepoAdapter(name="t", root=root,
                       manifest={"name": "t", "repo": {}, "knowledge": knowledge})


def test_briefing_docs_extra_injected_for_all_tiers(tmp_path):
    kroot = tmp_path / "kn"
    kroot.mkdir()
    (kroot / "extra.md").write_text("EXTRA ROUTER", encoding="utf-8")
    ad = _adapter(tmp_path, {"briefing_docs_extra": ["extra.md"]})
    assert "EXTRA ROUTER" in ad.briefing(kroot, mode="eco")
    assert "EXTRA ROUTER" in ad.briefing(kroot, mode="performance")


def test_legacy_performance_briefing_docs_still_works_with_deprecation(tmp_path):
    kroot = tmp_path / "kn"
    kroot.mkdir()
    (kroot / "extra.md").write_text("LEGACY ROUTER", encoding="utf-8")
    ad = _adapter(tmp_path, {"performance_briefing_docs": ["extra.md"]})
    warnings: list[str] = []
    text = ad.briefing(kroot, warnings=warnings, mode="eco")
    assert "LEGACY ROUTER" in text  # eco gets it too now (model-only boundary)
    assert any("deprecated" in w for w in warnings)


# ── doctor: static backend check ──────────────────────────────────────────────

def test_doctor_flags_claude_model_on_deepseek_host():
    from infermatrix_copilot.cli.doctor import _check_model_backends

    s = _settings(agent_model="claude-sonnet-5")  # the .env.template trap
    ok, detail = _check_model_backends(s)
    assert not ok and "cannot serve" in detail and "claude-sonnet-5" in detail


def test_doctor_accepts_matching_family_and_notes_deferred_perf():
    from infermatrix_copilot.cli.doctor import _check_model_backends

    ok, detail = _check_model_backends(_settings())
    assert ok and "performance tier unconfigured" in detail


def test_doctor_silent_on_unknown_gateway():
    from infermatrix_copilot.cli.doctor import _check_model_backends

    s = Settings(_env_file=None, anthropic_api_key="k",
                 anthropic_base_url="https://gw.example.com/anthropic",
                 agent_model="claude-opus-4-8")
    ok, _ = _check_model_backends(s)
    assert ok  # unknown gateway: the probe is the real check


# ── copilot preflight ─────────────────────────────────────────────────────────

def test_resolved_target_host_defaults_to_anthropic():
    t = ResolvedTarget(role="agent", model="m", base_url="", api_key="k",
                       source="global")
    assert t.host == "api.anthropic.com"
