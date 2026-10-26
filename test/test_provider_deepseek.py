"""DeepSeek Harness transport — fully offline.

The SDK is not a CLI we can fake with a shell script (it is an in-process
Python class that spawns its own runtime), so the seam under test is the
`DeepSeekHarness` constructor+run: a stub class records exactly the config it
was handed and returns a scripted `RunResult`. That covers the two things
worth guarding — the generated Cordis composition, and the mapping from
`finish_reason` onto `AgentOutcome` — without touching the network.
"""

import sys
import types
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.providers.base import AgentSessionRequest
from infermatrix_copilot.providers.deepseek import DeepSeekHarnessTransport
from infermatrix_copilot.providers.registry import transport_for_id
from infermatrix_copilot.scopes import read_only_scope
from infermatrix_copilot.tool_bridge import write_bridge_spec


@dataclass
class _FakeRunResult:
    session_id: str = "s1"
    final_response: str = "REVIEW"
    finish_reason: str = "completed"
    events: list = field(default_factory=list)
    notifications: list = field(default_factory=list)
    session_root: str = ""


class _FakeHarness:
    """Records construction kwargs; returns the scripted result."""

    captured: dict = {}
    result = _FakeRunResult()
    raises: Exception | None = None

    def __init__(self, **kwargs):
        type(self).captured = dict(kwargs)
        if type(self).raises is not None:
            raise type(self).raises

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, prompt, **kwargs):
        type(self).captured["prompt"] = prompt
        return type(self).result


@pytest.fixture
def fake_sdk(monkeypatch):
    """Install a stub `deepseek_harness` module and reset the fake."""
    module = types.ModuleType("deepseek_harness")
    module.DeepSeekHarness = _FakeHarness
    @dataclass
    class Config:
        cordis: str = ""
        session_root: str = ""
    module.DeepSeekHarnessConfig = Config
    monkeypatch.setitem(sys.modules, "deepseek_harness", module)
    _FakeHarness.captured = {}
    _FakeHarness.result = _FakeRunResult()
    _FakeHarness.raises = None
    # Protocol-mapping tests run the same worker body with a fake vendor SDK.
    # Real process/environment/deadline behavior is covered separately.
    from infermatrix_copilot.providers.deepseek_worker import run_request

    def invoke(self, *, options, env, prompt, timeout_s, step_cap=None, native_event_sink=None):
        return types.SimpleNamespace(**run_request({"options": {**options, "env": env},
                                                    "prompt": prompt, "step_cap": step_cap}))

    monkeypatch.setattr(DeepSeekHarnessTransport, "_invoke", invoke)
    return _FakeHarness


def _settings(**kw):
    # _env_file=None: the repo .env holds a real DeepSeek key, and a test that
    # silently picks it up would pass for the wrong reason (and could spend).
    kw.setdefault("anthropic_api_key", "sk-test")
    kw.setdefault("strict_backend_model", "deepseek-v4-pro")
    return Settings(_env_file=None, strict_backend="deepseek", **kw)


def _request(tmp_path, *, bridge=False, read_only=True):
    (tmp_path / "worktree").mkdir(exist_ok=True)
    scope = replace(read_only_scope(), root=str(tmp_path / "worktree"),
                    read_only=read_only)
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    spec = (write_bridge_spec(run_dir=run_dir, step_name="agent.review#lens",
                              scope=scope, repo="vllm-omni")
            if bridge else None)
    return AgentSessionRequest(
        system="SYSTEM", prompt="PROMPT", scope=scope,
        model="deepseek-v4-pro", max_iters=14, timeout_s=60.0,
        run_dir=run_dir, step_name="agent.review#lens",
        bridge_spec_path=spec)


def test_registry_resolves_deepseek():
    t = transport_for_id(_settings(), "deepseek")
    assert isinstance(t, DeepSeekHarnessTransport)
    assert "api_keyed" in t.spec.capabilities


def test_composition_pins_sandbox_and_never_grants_full_access(tmp_path):
    """The upstream minimal composition ships danger-full-access; on a shared
    machine that is not acceptable, so the generated one must pin the mode to
    the step's own scope and must never emit the dangerous value."""
    t = DeepSeekHarnessTransport(_settings())
    ro = t._composition(run_dir=tmp_path, step_name="s", cwd=tmp_path,
                        read_only=True, bridge_spec_path=None).read_text()
    rw = t._composition(run_dir=tmp_path, step_name="s2", cwd=tmp_path,
                        read_only=False, bridge_spec_path=None).read_text()
    assert "mode: read-only" in ro
    assert "mode: workspace-write" in rw
    assert "danger-full-access" not in ro
    assert "danger-full-access" not in rw
    # minimal's defining properties: no compaction plugin, no skills, no
    # runtime-context injection
    assert "compaction" not in ro
    assert "includeRuntimeContext: false" in ro
    assert "enabled: false" in ro
    # a truncated turn must be reported, never silently accepted
    assert "maxTokensAsSuccess: false" in ro


def test_composition_never_names_the_absent_mcp_plugin(tmp_path):
    """The bundled runtime compiles in 122 plugins and mcp-client is not one.
    Naming it does not fail fast -- the runtime dies on load while the SDK
    waits in initialize, which cost 30 minutes per lens when measured."""
    t = DeepSeekHarnessTransport(_settings())
    for spec in (tmp_path / "spec.json", None):
        text = t._composition(run_dir=tmp_path, step_name=f"s{spec}",
                              cwd=tmp_path, read_only=True,
                              bridge_spec_path=spec).read_text()
        assert "dsh-mcp-client" not in text
        assert "dsh-tool-bash-persistent" in text
        assert "dsh-tool-str-replace-editor" in text


def test_absent_plugin_fails_immediately_rather_than_timing_out(monkeypatch):
    """The guard that turns a boot failure into an instant, named error."""
    from infermatrix_copilot.providers import deepseek as mod
    monkeypatch.setattr(mod, "_runtime_plugins",
                        lambda: {"@deepseek-ai/dsh-fs-local"})
    with pytest.raises(RuntimeError, match="dsh-mcp-client"):
        mod._assert_plugins_bundled(
            "- id: x\n  name: '@deepseek-ai/dsh-mcp-client'\n")
    # an unscannable runtime must degrade to a no-op, never block a run
    monkeypatch.setattr(mod, "_runtime_plugins", lambda: set())
    mod._assert_plugins_bundled("- id: x\n  name: '@deepseek-ai/dsh-mcp-client'\n")


def test_unbridgeable_tools_fail_before_sdk_construction(fake_sdk, tmp_path):
    req = _request(tmp_path, bridge=True)
    with pytest.raises(ValueError, match="does not support.*MCP tool bridge"):
        DeepSeekHarnessTransport(_settings()).run_session(req)
    assert fake_sdk.captured == {}
    assert not (req.run_dir / "dsh").exists()


def test_incompatible_sdk_fails_before_construction(fake_sdk, tmp_path, monkeypatch):
    @dataclass
    class NewConfig:
        profile: str = "sdk"

    monkeypatch.setattr(sys.modules["deepseek_harness"], "DeepSeekHarnessConfig", NewConfig)
    with pytest.raises(RuntimeError, match="lacks pinned composition support"):
        DeepSeekHarnessTransport(_settings()).run_session(_request(tmp_path))
    assert fake_sdk.captured == {}


@pytest.mark.parametrize("mode", ["completion", "session"])
def test_incompatible_sdk_is_not_accounted_as_dispatched(
    fake_sdk, tmp_path, monkeypatch, mode,
):
    from infermatrix_copilot.budgeting import bind_call_budget
    from infermatrix_copilot.providers import run_harness_session
    from infermatrix_copilot.providers.completion import complete_native

    @dataclass
    class NewConfig:
        profile: str = "sdk"

    monkeypatch.setattr(sys.modules["deepseek_harness"], "DeepSeekHarnessConfig", NewConfig)
    transport = DeepSeekHarnessTransport(_settings())
    settlements = []
    with bind_call_budget("test", lambda _: "ticket",
                          lambda ticket, facts: settlements.append(dict(facts))):
        with pytest.raises(RuntimeError, match="lacks pinned composition support") as raised:
            if mode == "completion":
                complete_native(lambda: transport,
                                request={"system": "S", "messages": []})
            else:
                run_harness_session(transport, _request(tmp_path))
    assert fake_sdk.captured == {}
    assert len(settlements) == 1
    assert settlements[0]["sent"] is False
    assert settlements[0]["usage"] is None
    if mode == "completion":
        assert raised.value.native_call_sent is False


def test_harness_step_records_unsupported_bridge_as_not_dispatched(fake_sdk, tmp_path):
    from types import SimpleNamespace
    from infermatrix_copilot.budgeting import bind_call_budget
    from infermatrix_copilot.providers import run_harness_step

    req = _request(tmp_path)
    ctx = SimpleNamespace(settings=_settings(), state={}, run_dir=req.run_dir, trace=None)
    settlements = []
    with bind_call_budget("test", lambda _: "ticket",
                          lambda ticket, facts: settlements.append(dict(facts))):
        with pytest.raises(ValueError, match="does not support.*MCP tool bridge"):
            run_harness_step(ctx, SimpleNamespace(model=req.model), step_name="review",
                             system=req.system, prompt=req.prompt, scope=req.scope, max_iters=2)
    assert fake_sdk.captured == {}
    assert len(settlements) == 1 and settlements[0]["sent"] is False


def test_env_never_leaks_model_endpoint_or_keys(tmp_path, monkeypatch):
    """An inherited ANTHROPIC_BASE_URL would silently reroute the harness."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak2")
    t = DeepSeekHarnessTransport(_settings())
    env = t._env(cwd=tmp_path, model="m", system="S",
                 session_root=tmp_path / "sess")
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["DSH_MODEL"] == "m"
    assert env["DSH_SYSTEM_PROMPT"] == "S"


def test_run_session_maps_a_completed_run(fake_sdk, tmp_path):
    fake_sdk.result = _FakeRunResult(final_response="  FINDINGS  ",
                                     finish_reason="completed")
    t = DeepSeekHarnessTransport(_settings())
    out = t.run_session(_request(tmp_path))
    assert out.text == "FINDINGS"
    assert out.truncated is False
    assert out.refusals == []
    # credential travels through the SDK config, not the subprocess env
    assert fake_sdk.captured["api_key"] == "sk-test"
    assert "DEEPSEEK_API_KEY" not in fake_sdk.captured["env"]


def test_max_tokens_surfaces_as_truncation_not_success(fake_sdk, tmp_path):
    """The bug class this codebase already paid for: a pass that burns its
    completion ceiling must not read as a quietly accepted empty final."""
    fake_sdk.result = _FakeRunResult(final_response="", finish_reason="max-tokens")
    t = DeepSeekHarnessTransport(_settings())
    out = t.run_session(_request(tmp_path))
    assert out.truncated is True


def test_harness_failure_becomes_an_outcome_not_a_raise(fake_sdk, tmp_path):
    """A dead harness must not sink the ensemble: the lens reports empty with
    a refusal so the zero-yield retry can fire."""
    fake_sdk.raises = RuntimeError("runtime exploded")
    t = DeepSeekHarnessTransport(_settings())
    out = t.run_session(_request(tmp_path))
    assert out.text == ""
    assert out.refusals and "runtime exploded" in out.refusals[0]


def test_activity_parser_degrades_instead_of_inventing():
    t = DeepSeekHarnessTransport(_settings())
    calls, tools, usage = t._activity(["garbage", None, {"no": "kind"}])
    assert (calls, tools, usage.input_tokens, usage.output_tokens) == (0, [], 0, 0)
    calls, tools, usage = t._activity([
        {"kind": "tool/call", "data": {"name": "bash"}},
        {"kind": "tool/result", "data": {"name": "bash"}},
        {"kind": "turn/end", "data": {"usage": {"input_tokens": 7,
                                                "output_tokens": 3},
                                      "model": "deepseek-v4-pro"}},
    ])
    assert calls == 1 and tools == ["bash"]
    assert (usage.input_tokens, usage.output_tokens) == (7, 3)
    assert usage.served_model == "deepseek-v4-pro"


def test_auth_gap_reports_a_missing_credential(monkeypatch):
    t = DeepSeekHarnessTransport(_settings(anthropic_api_key="",
                                           openai_api_key=""))
    monkeypatch.setattr(t, "cli_path", lambda: "/usr/bin/python3")
    gap = t.auth_gap()
    assert gap and "DEEPSEEK_HARNESS_API_KEY" in gap


def test_require_cli_names_the_pip_install(monkeypatch):
    t = DeepSeekHarnessTransport(_settings())
    monkeypatch.setattr(t, "cli_path", lambda: None)
    with pytest.raises(RuntimeError, match="deepseek-harness-sdk"):
        t.require_cli()


@pytest.mark.parametrize("envelope", [False, True])
def test_runaway_session_is_stopped_by_the_step_cap(fake_sdk, tmp_path, envelope):
    """dsh owns its own loop and its bundled runtime exposes no step cap, so
    max_iters cannot be handed to the harness. Measured on pr4816: two lenses
    finished in ~40 steps while a third ran 558 steps / 564 tool calls and was
    still going 50 minutes later. The cap is ours or there is none."""
    closed = {"n": 0}

    @dataclass(slots=True)
    class Notification:
        method: str
        payload: dict

    class _Runaway(_FakeHarness):
        def run(self, prompt, **kwargs):
            cb = kwargs.get("on_notification")
            for _ in range(10_000):          # a loop that never ends on its own
                if cb:
                    event = {"type": "step/end"}
                    cb(Notification("session.event", {"event": event}) if envelope else event)
                if closed["n"]:
                    raise RuntimeError("runtime closed")
            return _FakeRunResult()

        def close(self):
            closed["n"] += 1

    import sys as _sys
    _sys.modules["deepseek_harness"].DeepSeekHarness = _Runaway
    req = _request(tmp_path)
    req.max_iters = 5
    out = DeepSeekHarnessTransport(_settings()).run_session(req)
    assert closed["n"] >= 1, "the watchdog never closed the runaway harness"
    assert out.refusals and "step cap" in out.refusals[0]


# -- model resolution (PR4) --------------------------------------------------
# dsh is the one backend with no built-in default: constructing it with an
# empty model kills every turn with "has no provider/model". The default lives
# in the registry (ProviderSpec.default_model) and is resolved by
# Settings.tier_target, so the REQUESTED model and the SERVED model are the
# same string in the target, DSH_MODEL, the constructor and the trace — a
# transport-private fallback would leave the target reporting "".

def test_tier_target_fills_the_registry_default_when_unset():
    t = Settings(_env_file=None, anthropic_api_key="sk-test",
                 strict_backend="deepseek",
                 strict_backend_model="").tier_target("eco")
    assert t.model == "deepseek-v4-pro", (
        "an unset STRICT_BACKEND_MODEL must resolve to the provider default "
        "in the TARGET, not silently inside the transport")
    assert t.kind == "harness"


def test_explicit_backend_model_wins_over_the_default():
    t = Settings(_env_file=None, anthropic_api_key="sk-test",
                 strict_backend="deepseek",
                 strict_backend_model="deepseek-v4-flash").tier_target("eco")
    assert t.model == "deepseek-v4-flash"


def test_subscription_harnesses_keep_an_empty_model():
    """Only backends that REQUIRE a model declare one; the vendor CLIs pick
    their own, and inventing a default for them would misreport the run."""
    for backend in ("claude-code", "codex", "cursor"):
        t = Settings(_env_file=None, anthropic_api_key="sk-test",
                     strict_backend=backend,
                     strict_backend_model="").tier_target("eco")
        assert t.model == "", f"{backend} must not gain a fabricated default"


def test_run_session_never_constructs_an_empty_model(fake_sdk, tmp_path):
    req = _request(tmp_path)
    req.model = ""                                   # direct-caller path
    DeepSeekHarnessTransport(
        _settings(strict_backend_model="")).run_session(req)
    assert fake_sdk.captured["model"] == "deepseek-v4-pro"


def test_complete_never_constructs_an_empty_model(fake_sdk, tmp_path):
    DeepSeekHarnessTransport(_settings(strict_backend_model="")).complete(
        system="S", messages=[{"role": "user", "content": "hi"}])
    assert fake_sdk.captured["model"] == "deepseek-v4-pro"


def test_completion_omits_tool_plugins_from_dispatched_composition(fake_sdk, monkeypatch):
    transport = DeepSeekHarnessTransport(_settings())
    invoke = transport._invoke
    compositions = []

    def inspect(**kwargs):
        compositions.append(Path(kwargs["options"]["cordis"]).read_text())
        return invoke(**kwargs)

    monkeypatch.setattr(transport, "_invoke", inspect)
    reply = transport.complete(system="S", messages=[{"role": "user", "content": "hi"}])
    assert reply.text == "REVIEW"
    assert len(compositions) == 1
    assert "dsh-tool-bash-persistent" not in compositions[0]
    assert "dsh-tool-str-replace-editor" not in compositions[0]
    assert "toolBash: false" in compositions[0]
    assert "toolJobs: false" in compositions[0]


@pytest.mark.parametrize("event", [
    {"kind": "tool-call", "data": {"name": "bash"}},
    {"type": "tool-result", "data": {"name": "str_replace_editor"}},
])
def test_completion_rejects_unexpected_native_tool_events(fake_sdk, event):
    fake_sdk.result = _FakeRunResult(events=[event])
    with pytest.raises(RuntimeError, match="tool-less completion attempted"):
        DeepSeekHarnessTransport(_settings()).complete(
            system="S", messages=[{"role": "user", "content": "hi"}])


def test_lost_completion_is_not_reported_as_a_completed_empty_reply(fake_sdk, monkeypatch):
    from infermatrix_copilot.providers.completion import complete_native
    from infermatrix_copilot.budgeting import bind_call_budget

    def lost_response(self, *args, **kwargs):
        raise OSError("SDK response lost")

    monkeypatch.setattr(fake_sdk, "run", lost_response)
    settlements = []
    with bind_call_budget("test", lambda _: "ticket",
                          lambda ticket, facts: settlements.append(dict(facts))):
        with pytest.raises(OSError, match="SDK response lost") as raised:
            complete_native(lambda: DeepSeekHarnessTransport(_settings()),
                            request={"system": "S", "messages": [{"role": "user", "content": "hi"}]})
    assert raised.value.native_call_sent is True
    assert raised.value.native_call_completed is False
    assert len(settlements) == 1
    assert settlements[0]["sent"] is True and settlements[0]["usage"] is None


def test_env_model_matches_the_constructed_model(fake_sdk, tmp_path):
    """DSH_MODEL and the constructor must agree — they are the two places the
    served model is stated, and a disagreement is how a run mislabels itself."""
    req = _request(tmp_path)
    req.model = ""
    DeepSeekHarnessTransport(
        _settings(strict_backend_model="")).run_session(req)
    assert fake_sdk.captured["env"]["DSH_MODEL"] == fake_sdk.captured["model"]
