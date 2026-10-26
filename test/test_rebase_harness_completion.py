"""Harness completion needs the module's current gate and a clean outcome."""

import asyncio
from pathlib import Path

import pytest

from infermatrix_copilot.agent_loop import AgentOutcome
from infermatrix_copilot.providers import registry
from infermatrix_copilot.rebase_engine.module_rebase import (
    ModuleRunConfig,
    _harness_attempt,
    _plan_gate_opened,
)
from infermatrix_copilot.rebase_engine.rebase_tools import (
    RebasePaths,
    build_rebase_tools,
)
from infermatrix_copilot.run_trace import RunTrace
from infermatrix_copilot.scopes import PathScope, ToolScope
from infermatrix_copilot.tool_bridge import PlanGate, load_bridge_spec, make_dispatcher


@pytest.fixture
def harness_attempt(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    run = tmp_path / "run"
    signals = run / "signals"
    plans = run / "plans" / "module-fixture"
    repo.mkdir()
    signals.mkdir(parents=True)
    trace = RunTrace(run / "bridge_trace.jsonl")
    scope = ToolScope(
        name="rebase-module:fixture", allowed_tools=frozenset({"write_file"}),
        path_scope=PathScope(writable=(f"{run}/*",), primary=(f"{run}/*",)),
        read_only=False, root=str(repo),
    )
    tools = build_rebase_tools(
        [{"name": "write_file", "description": "Write a file", "input_schema": {}}],
        RebasePaths(omni_path=str(repo), vllm_path=str(repo)),
    )

    def attempt(*, gate_kind="current", refusals=(), require_plan_review=True,
                text="module complete"):
        if gate_kind == "malformed":
            trace.path.write_text(
                'partial json\n[]\nnull\n{"kind":"plan_gate_opened","ts":"yesterday"}\n'
                '{"kind":"plan_gate_opened","ts":NaN,"decision":null}\n'
            )
        if gate_kind in ("prior_same_module", "prior_other_module", "restored"):
            prefix = plans if gate_kind != "prior_other_module" else plans.with_name("module-other")
            prefix.mkdir(parents=True, exist_ok=True)
            decision = prefix / "plan.decision.md"
            decision.write_text("accepted\n")
            PlanGate(str(prefix), (), trace=trace)
            if gate_kind != "restored":
                decision.unlink()

        class Session:
            def run_session(self, req):
                # Keep the production serialization, gate, dispatcher and file
                # writes. Only the external vendor session is replaced.
                restored_scope, spec = load_bridge_spec(req.bridge_spec_path)
                prefix = spec["rebase"]["plan_write_prefix"]
                if gate_kind == "current_other_module":
                    prefix = str(plans.with_name("module-other"))
                gate = PlanGate(prefix, (), trace=trace) if prefix else None
                if gate_kind in ("current", "current_other_module"):
                    call = make_dispatcher(restored_scope, (str(repo), str(run)),
                                           trace, extra=tools, gate=gate)
                    call("write_file", {"file_path": str(Path(prefix) / "plan.decision.md"),
                                        "content": "accepted\n"})
                (signals / "module.fixture.done").write_text("MODULE_DONE fixture\n")
                return AgentOutcome(text=text, iterations=1, tool_calls=1,
                                    refusals=list(refusals))

        monkeypatch.setattr(registry, "transport_for_id", lambda *a, **kw: Session())
        config = ModuleRunConfig(
            vllm_path=str(repo), omni_path=str(repo), script_dir=str(repo),
            model="fixture-model", log_dir=str(run), signal_dir=str(signals),
            backend="zcode", paths_spec={"omni_path": str(repo), "vllm_path": str(repo)},
        )
        return asyncio.run(_harness_attempt(
            "fixture", module="fixture", config=config, scope=scope, trace=trace,
            tool_defs=[], plan_prefix=str(plans), require_plan_review=require_plan_review,
        ))

    return attempt


@pytest.mark.parametrize("gate_kind", [
    "absent", "malformed", "prior_same_module", "prior_other_module", "current_other_module",
])
def test_fresh_completion_requires_this_attempts_module_gate(harness_attempt, gate_kind):
    result = harness_attempt(gate_kind=gate_kind)
    assert result["done"] is False
    assert result["plan_done"] is False
    assert "did not open its module's plan-review gate" in result["text"]


@pytest.mark.parametrize("gate_kind", ["current", "restored"])
def test_completion_accepts_same_attempt_gate_or_bridge_restoration(harness_attempt, gate_kind):
    result = harness_attempt(gate_kind=gate_kind)
    assert result["done"] is True
    assert result["plan_done"] is True


@pytest.mark.parametrize("require_plan_review", [False, True])
@pytest.mark.parametrize("refusal", ["audit: native product write", "dsh session failed: exit 1"])
def test_fresh_completion_cannot_override_typed_provider_refusal(
    harness_attempt, require_plan_review, refusal,
):
    result = harness_attempt(gate_kind="current" if require_plan_review else "absent",
                             refusals=[refusal], require_plan_review=require_plan_review)
    assert result["done"] is False
    assert refusal in result["text"]


def test_harmless_refusal_prose_is_not_a_typed_refusal(harness_attempt):
    result = harness_attempt(text="The earlier refusal was resolved; verification passed.")
    assert result["done"] is True


def test_debug_retry_can_finish_with_gate_already_accepted(harness_attempt):
    result = harness_attempt(gate_kind="absent", require_plan_review=False)
    assert result["done"] is True
    assert result["plan_done"] is True


def test_scoped_gate_reader_ignores_malformed_or_unattributed_events(tmp_path):
    (tmp_path / "bridge_trace.jsonl").write_text(
        'partial json\n[]\n{"kind":"plan_gate_opened","ts":"yesterday"}\n'
        '{"kind":"plan_gate_opened","ts":2,"decision":null}\n'
        '{"kind":"plan_gate_opened","ts":NaN,"decision":"/plans/p.decision.md"}\n'
    )
    assert _plan_gate_opened(tmp_path, plan_prefix="/plans", since_ts=1) is False


@pytest.mark.parametrize("signal,expected", [
    ("missing", False), ("stale", False), ("wrong_module", False),
    ("malformed", False), ("fresh", True), ("rewritten", True),
    ("failure", False), ("truncated", False),
])
def test_harness_requires_current_attempt_completion_signal(tmp_path, monkeypatch,
                                                           signal, expected):
    import asyncio
    from types import SimpleNamespace

    from infermatrix_copilot import tool_bridge
    from infermatrix_copilot.providers import registry
    from infermatrix_copilot.rebase_engine.module_rebase import (
        ModuleRunConfig,
        _harness_attempt,
    )

    root = tmp_path / "repo"
    run = tmp_path / "run"
    signals = run / "signals"
    root.mkdir()
    signals.mkdir(parents=True)
    done = signals / "module.fixture.done"
    if signal in ("stale", "rewritten"):
        done.write_text("MODULE_DONE fixture\n")

    class Session:
        def run_session(self, req):
            assert str(done) in req.system
            if signal == "wrong_module":
                done.write_text("MODULE_DONE other\n")
            elif signal == "malformed":
                done.write_text("withheld MODULE_DONE fixture\n")
            elif signal in ("fresh", "rewritten", "failure", "truncated"):
                done.write_text("MODULE_DONE fixture\nOUT_OF_SCOPE_EDITS none\n")
            if signal == "failure":
                (signals / "module.fixture.fail").write_text("verification failed\n")
            return SimpleNamespace(truncated=signal == "truncated",
                                   text="withheld MODULE_DONE", iterations=1)

    monkeypatch.setattr(registry, "transport_for_id", lambda *a, **k: Session())
    monkeypatch.setattr(tool_bridge, "write_bridge_spec", lambda **k: None)
    cfg = ModuleRunConfig(vllm_path=str(root), omni_path=str(root),
                          script_dir=str(root), model="m", log_dir=str(run),
                          signal_dir=str(signals), backend="zcode")
    scope = ToolScope(name="fixture", allowed_tools=frozenset(), path_scope=None,
                      read_only=True, root=str(root))
    result = asyncio.run(_harness_attempt(
        "debug attempt", module="fixture", config=cfg, scope=scope, trace=None,
        tool_defs=[], plan_prefix="", require_plan_review=False))
    assert result["done"] is expected
    if not expected:
        assert "completion rejected" in result["text"]
