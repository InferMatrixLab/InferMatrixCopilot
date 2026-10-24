"""Generator outages recover through an explicit, attributed subscription fallback."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service.models import (
    ModelGateway, ModelRole, ModelUnavailable, roles_from_env,
)
from infermatrix_copilot.kb_service.replay import export_dataset
from infermatrix_copilot.kb_service.runtime import collect_events, run_intake, trace_recorder
from infermatrix_copilot.trace_store import TraceStore
from test_kb_intake_gate import GEN, JUDGE, _generator_then_judge, _runtime

FALLBACK = ModelRole("generator", "zcode", "GLM-5.3")
PRIMARY = replace(GEN, fallback=FALLBACK)


class Transport:
    stops_at_spend = True

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answer(kwargs) if callable(self.answer) else self.answer
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(
            blocks=[SimpleNamespace(text=answer)], stop_reason="end_turn",
            usage={}, model=kwargs["model"])


def gateway(primary, fallback, *, recorder=None):
    transports = {"claude-code": primary, "zcode": fallback}
    return ModelGateway(None, transport_factory=transports.__getitem__, recorder=recorder)


@pytest.fixture(autouse=True)
def clean_model_env(monkeypatch):
    for key in ("KB_GENERATOR", "KB_JUDGE", "KB_GENERATOR_FALLBACK", "KB_JUDGE_FAMILY_WAIVER"):
        monkeypatch.delenv(key, raising=False)


def test_fallback_is_opt_in_and_keeps_codex_judge(monkeypatch):
    generator, judge = roles_from_env()
    assert generator == GEN and generator.fallback is None
    monkeypatch.setenv("KB_GENERATOR_FALLBACK", "zcode:GLM-5.3")
    generator, judge = roles_from_env()
    assert generator == PRIMARY and judge == JUDGE


@pytest.mark.parametrize("spec,judge", [
    ("zcode", "codex:gpt-6-sol:medium"),
    (GEN.label(), "codex:gpt-6-sol:medium"),
    ("codex:gpt-6-mini:low", "codex:gpt-6-sol:medium"),
])
def test_invalid_or_same_family_fallback_is_refused(monkeypatch, spec, judge):
    monkeypatch.setenv("KB_GENERATOR_FALLBACK", spec)
    monkeypatch.setenv("KB_JUDGE", judge)
    with pytest.raises(ValueError):
        roles_from_env()


GLM_JUDGE = ModelRole("judge", "zcode", "GLM-5.3")


def test_same_family_judge_needs_the_explicit_waiver(monkeypatch, caplog):
    monkeypatch.setenv("KB_GENERATOR_FALLBACK", FALLBACK.label())
    monkeypatch.setenv("KB_JUDGE", GLM_JUDGE.label())
    with pytest.raises(ValueError, match="different model family"):
        roles_from_env()
    monkeypatch.setenv("KB_JUDGE_FAMILY_WAIVER", "1")
    with caplog.at_level("WARNING", logger="infermatrix_copilot.kb_service.models"):
        generator, judge = roles_from_env()
    assert generator == PRIMARY and judge == GLM_JUDGE
    assert any("KB_JUDGE_FAMILY_WAIVER" in r.message for r in caplog.records)


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_waiver_truthy_values(monkeypatch, value):
    monkeypatch.setenv("KB_GENERATOR_FALLBACK", FALLBACK.label())
    monkeypatch.setenv("KB_JUDGE", GLM_JUDGE.label())
    monkeypatch.setenv("KB_JUDGE_FAMILY_WAIVER", value)
    assert roles_from_env()[1] == GLM_JUDGE


def test_waiver_does_not_relax_the_other_invariants(monkeypatch):
    monkeypatch.setenv("KB_JUDGE_FAMILY_WAIVER", "1")
    monkeypatch.setenv("KB_GENERATOR_FALLBACK", GEN.label())
    with pytest.raises(ValueError, match="must differ"):
        roles_from_env()


@pytest.mark.parametrize("failure", [
    "Your account is on hold and can't use Claude Code.",
    "",
])
def test_generator_outage_uses_exact_fallback_and_records_both_attempts(failure):
    records = []
    primary, fallback = Transport(failure), Transport('{"operations": []}')
    reply = gateway(primary, fallback, recorder=records.append).call_json(
        PRIMARY, system="policy", prompt="evidence")
    assert reply.role == FALLBACK and reply.served_model == "GLM-5.3"
    assert len(primary.calls) == len(fallback.calls) == 1
    assert primary.calls[0]["messages"] == fallback.calls[0]["messages"]
    assert [r["provider"] for r in records] == ["claude-code", "zcode"]
    assert records[0]["error"] and not records[1]["error"]
    assert records[1]["fallback_from"] == GEN.label()


@pytest.mark.parametrize("failure", [RuntimeError("account restricted"), TimeoutError("timeout")])
def test_unknown_dispatched_outcome_does_not_trigger_another_paid_call(failure):
    records = []
    primary, fallback = Transport(failure), Transport('{"operations": []}')
    with pytest.raises(ModelUnavailable) as caught:
        gateway(primary, fallback, recorder=records.append).call_json(
            PRIMARY, system="policy", prompt="evidence")
    assert caught.value.native_call_sent and not caught.value.native_call_completed
    assert len(primary.calls) == 1 and not fallback.calls
    assert len(records) == 1 and records[0]["error"]


def test_missing_cli_is_recorded_and_uses_fallback():
    records = []
    fallback = Transport('{"ok": true}')

    def factory(provider):
        if provider == "claude-code":
            raise ModelUnavailable("claude-code CLI is not installed")
        return fallback

    g = ModelGateway(None, transport_factory=factory, recorder=records.append)
    assert g.call_json(PRIMARY, system="s", prompt="p").role == FALLBACK
    assert "not installed" in records[0]["error"] and len(fallback.calls) == 1


def test_primary_success_never_calls_fallback():
    fallback = Transport(RuntimeError("must not be called"))
    reply = gateway(Transport('{"ok": true}'), fallback).call_json(PRIMARY, system="s", prompt="p")
    assert reply.role.label() == GEN.label() and not fallback.calls


def test_schema_failure_retains_existing_primary_repair_path():
    fallback = Transport('{"ok": true}')

    def validate(data):
        if "ok" not in data:
            raise ValueError("missing ok")

    with pytest.raises(ModelUnavailable, match="failed its schema"):
        gateway(Transport('{}'), fallback).call_json(PRIMARY, system="s", prompt="p", validate=validate)
    assert not fallback.calls


def test_fallback_reply_must_pass_the_same_validation():
    def validate(data):
        raise ValueError("invalid operations")

    with pytest.raises(ModelUnavailable, match="invalid operations"):
        gateway(Transport("account restricted"), Transport('{}')).call_json(
            PRIMARY, system="s", prompt="p", validate=validate)


def test_thresholded_call_does_not_spend_a_second_reservation():
    fallback = Transport('{"ok": true}')
    with pytest.raises(ModelUnavailable):
        gateway(Transport("account restricted"), fallback).call_json(
            PRIMARY, system="s", prompt="p", max_budget_usd=1)
    assert not fallback.calls


def test_judge_never_falls_back_even_if_given_a_fallback_role():
    fallback = Transport('{"ok": true}')
    role = replace(GEN, name="judge", fallback=FALLBACK)
    with pytest.raises(ModelUnavailable):
        gateway(Transport("account restricted"), fallback).call_json(role, system="s", prompt="p")
    assert not fallback.calls


def test_payload_omission_applies_to_both_models():
    records = []
    gateway(Transport("account restricted"), Transport('{"ok": true}'), recorder=records.append).call_json(
        PRIMARY, system="private policy", prompt="private evidence", record_payload=False)
    assert len(records) == 2
    assert all(r["system"] == r["prompt"] == r["reply"] == "" for r in records)


def test_pending_event_recovers_and_change_and_trace_attribute_glm_with_codex_review(tmp_path):
    class UnavailableBeforeDispatch(Transport):
        def validate_completion(self):
            raise RuntimeError('zcode unavailable before dispatch')

    primary = Transport("Your account is on hold and can't use Claude Code.")
    unavailable = UnavailableBeforeDispatch(None)
    g = gateway(primary, unavailable)
    rt, lifecycle = _runtime(tmp_path, g)
    rt.generator = PRIMARY
    store = TraceStore(tmp_path / "traces", environ={})
    rt.traces = store
    g._recorder = trace_recorder(store)
    collect_events(rt, lifecycle)
    assert run_intake(rt, lifecycle) is None
    assert len(rt.ledger.events("demo", "pending")) == 1
    assert unavailable.calls == []
    answer = _generator_then_judge()
    fallback = Transport(lambda kw: json.dumps(answer(FALLBACK, kw["messages"][0]["content"])))
    reviewer = Transport(lambda kw: json.dumps(answer(JUDGE, kw["messages"][0]["content"])))
    transports = {"claude-code": primary, "zcode": fallback, "codex": reviewer}
    g._factory = transports.__getitem__
    changeset = rt.ledger.changeset(run_intake(rt, lifecycle))
    assert changeset["status"] == "gated"
    assert changeset["detail"]["generator"] == FALLBACK.label()
    assert changeset["detail"]["judge"] == JUDGE.label()
    assert not rt.ledger.events("demo", "pending") and reviewer.calls
    fallback_calls = [r for r in store.query(kind="model_call") if r["model"]["provider"] == "zcode"]
    assert fallback_calls[-1]["model"]["served_model"] == "GLM-5.3"
    assert fallback_calls[-1]["result"]["fallback_from"] == GEN.label()
    out = tmp_path / "dataset.jsonl"
    export_dataset(store, out, role="generator")
    generators = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(generators) == 1 and generators[0]["model"]["provider"] == "zcode"
    assert generators[0]["changeset_id"] == changeset["id"]
    assert generators[0]["decision"]["status"] == "pass"


@pytest.mark.parametrize('failure', ['after_dispatch', 'prior_execution'])
def test_lost_fallback_response_is_not_retried_when_transport_recovers(tmp_path, failure):
    from infermatrix_copilot.providers.container_execution import ContainerPending

    class ReservedTransport(Transport):
        def validate_completion(self):
            raise ContainerPending('original execution needs reconciliation', prior_execution=True)

    primary = Transport("Your account is on hold and can't use Claude Code.")
    fallback = (Transport(RuntimeError('zcode response lost after dispatch')) if failure == 'after_dispatch'
                else ReservedTransport(None))
    g = gateway(primary, fallback)
    rt, lifecycle = _runtime(tmp_path, g)
    rt.generator = PRIMARY
    collect_events(rt, lifecycle)
    assert run_intake(rt, lifecycle) is None
    assert not rt.ledger.events('demo', 'pending')
    assert len(rt.ledger.events('demo', 'draft_unknown')) == 1
    assert len(primary.calls) == 1
    assert len(fallback.calls) == (1 if failure == 'after_dispatch' else 0)
    fallback.answer = '{"operations": []}'
    assert run_intake(rt, lifecycle) is None
    assert len(primary.calls) == 1
    assert len(fallback.calls) == (1 if failure == 'after_dispatch' else 0)


def test_evolved_intake_preserves_original_pending_through_rollback_error(tmp_path, monkeypatch):
    from infermatrix_copilot import llm
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.improve import artifacts, objectives, runtime as evolution
    from infermatrix_copilot.providers.container_execution import ContainerPending

    rt, lifecycle = _runtime(tmp_path, gateway(Transport(None), Transport(None)))
    rt.settings = Settings(_env_file=None, improve_enabled=True, improve_evolve_enabled=True)
    rt.traces = TraceStore(tmp_path / 'traces', environ={})
    monkeypatch.setattr(artifacts, 'runtime_settings', lambda settings, _: settings)
    monkeypatch.setattr(objectives, 'enabled', lambda _: True)
    monkeypatch.setattr(objectives, 'capture', lambda *args: None)
    monkeypatch.setattr(evolution, 'registry', lambda _: tmp_path / 'no-active-registry')
    monkeypatch.setattr(llm, 'LLM', lambda _: object())
    executions = []

    def pending(*args):
        executions.append(args)
        error = ContainerPending('original execution needs reconciliation', prior_execution=True)
        error.native_call_sent = False
        error.native_call_completed = False
        raise error

    monkeypatch.setattr(evolution, 'execute', pending)
    collect_events(rt, lifecycle)
    assert run_intake(rt, lifecycle) is None
    assert not rt.ledger.events('demo', 'pending')
    assert len(rt.ledger.events('demo', 'draft_unknown')) == 1
    assert run_intake(rt, lifecycle) is None
    assert len(executions) == 1
