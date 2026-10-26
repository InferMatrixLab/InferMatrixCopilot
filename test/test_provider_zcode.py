"""ZCode transport against a fake zcode CLI — offline. The stream-json event
shapes are recorded from a live zcode 0.16.9 run (2026-09-30)."""

import json
import stat
from pathlib import Path

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.llm import ModelMismatchError
from infermatrix_copilot.providers import zcode as zcode_mod
from infermatrix_copilot.providers.base import AgentSessionRequest
from infermatrix_copilot.providers.zcode import ZCodeTransport
from infermatrix_copilot.scopes import read_only_scope
from infermatrix_copilot.tool_bridge import write_bridge_spec

_FAKE_CLI = """#!/usr/bin/env python3
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
config = os.path.join(os.getcwd(), ".zcode", "config.json")
attach = sys.argv[sys.argv.index("--attach") + 1] \\
    if "--attach" in sys.argv else ""
with open(os.path.join(here, "capture.json"), "w") as f:
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(),
               "config": open(config).read() if os.path.exists(config) else "",
               "attached": open(attach).read() if attach else "",
               "env_key": os.environ.get("ANTHROPIC_API_KEY", ""),
               "data_dir": os.environ.get("ZCODE_DATA_BASE_DIR", ""),
               "personal": os.environ.get("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", ""),
               "personal_text": open(os.environ["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"]).read()
               if os.environ.get("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE") else ""}, f)
if os.path.exists(os.path.join(here, "sleep")):
    time.sleep(10)
if os.path.exists(os.path.join(here, "fail")):
    print("Error: not logged in to Z.AI", file=sys.stderr)
    sys.exit(1)
model = open(os.path.join(here, "model")).read() \\
    if os.path.exists(os.path.join(here, "model")) else "GLM-5.3-Flash"
tools = [("mcp__infermatrix-tools__read_file", {"path": "/etc/hosts"}),
         ("Read", {"file_path": "PROMPT.md"})]
if os.path.exists(os.path.join(here, "rogue")):
    tools.append(("Bash", {"command": "ls"}))
if os.path.exists(os.path.join(here, "outside")):
    tools.append(("Read", {"file_path": "/etc/passwd"}))
    tools.append(("Glob", {"pattern": "/root/.ssh/*"}))
def emit(e): print(json.dumps(e))
emit({"type": "turn.started", "payload": {"turnNumber": 0}})
emit({"type": "session.updated", "payload": {
    "providerId": "account:bigmodel-individual-coding-plan",
    "modelId": model, "toolCount": 14, "iteration": 0}})
for i, (name, args) in enumerate(tools):
    emit({"type": "tool.updated", "payload": {
        "toolCallId": f"c{i}", "toolName": name, "input": args,
        "kind": "scheduled"}})
    emit({"type": "tool.updated", "payload": {
        "toolCallId": f"c{i}", "toolName": name, "kind": "started"}})
emit({"type": "model.streaming", "payload": {"delta": "REV", "kind": "delta"}})
if not os.path.exists(os.path.join(here, "noresult")):
    emit({"type": "result", "response": "REVIEW", "usage": {
        "source": "provider", "inputTokens": 50, "outputTokens": 9}})
"""


class FakeTrace:
    def __init__(self):
        self.events = []

    def record(self, kind, **fields):
        self.events.append({"kind": kind, **fields})


def test_subscription_billing_refuses_custom_api_provider(tmp_path, monkeypatch):
    transport = _transport(tmp_path)
    monkeypatch.setattr(transport, "_host_provider_id", lambda: "account:bigmodel-individual-coding-plan")
    assert transport.subscription_billing
    transport = _transport(tmp_path)
    monkeypatch.setattr(transport, "_host_provider_id", lambda: "custom:metered-api")
    assert not transport.subscription_billing
    transport = _transport(tmp_path, zcode_provider_id="custom:override-api")
    monkeypatch.setattr(transport, "_host_provider_id", lambda: "account:bigmodel-individual-coding-plan")
    assert not transport.subscription_billing


def test_subscription_check_pins_provider_used_by_session_config(tmp_path, monkeypatch):
    transport = _transport(tmp_path)
    monkeypatch.setattr(transport, "_host_provider_id", lambda: "account:bigmodel-individual-coding-plan")
    assert transport.subscription_billing
    monkeypatch.setattr(transport, "_host_provider_id", lambda: "custom:metered-api")
    config = json.loads(transport._write_model_config(tmp_path, "GLM-5.3").read_text())
    assert config["config"]["defaultModelSelection"]["providerId"] == "account:bigmodel-individual-coding-plan"


def _transport(tmp_path: Path, **settings) -> ZCodeTransport:
    cli = tmp_path / "bin" / "zcode"
    cli.parent.mkdir(exist_ok=True)
    cli.write_text(_FAKE_CLI, encoding="utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return ZCodeTransport(Settings(
        _env_file=None, strict_backend="zcode",
        strict_backend_cli=str(cli), **settings))


def _request(tmp_path: Path, with_bridge: bool = True,
             prompt: str = "PROMPT") -> AgentSessionRequest:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scope = read_only_scope()
    scope = type(scope)(name=scope.name, allowed_tools=scope.allowed_tools,
                        read_only=True, root=str(worktree))
    bridge = write_bridge_spec(run_dir=run_dir, step_name="agent.review_diff",
                               scope=scope, repo="vllm-omni") \
        if with_bridge else None
    return AgentSessionRequest(
        system="SYS", prompt=prompt, scope=scope, model="",
        max_iters=8, timeout_s=30.0, run_dir=run_dir,
        step_name="agent.review_diff", bridge_spec_path=bridge,
        trace=FakeTrace())


def _capture(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "bin" / "capture.json").read_text(encoding="utf-8"))


def test_run_session_plan_mode_bridge_and_parse(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    transport = _transport(tmp_path)
    req = _request(tmp_path)

    outcome = transport.run_session(req)

    assert outcome.text == "REVIEW"
    assert (outcome.input_tokens, outcome.output_tokens) == (50, 9)
    assert outcome.tools_used == ["mcp__infermatrix-tools__read_file", "Read"]
    assert outcome.refusals == [] and outcome.truncated is False

    capture = _capture(tmp_path)
    argv = capture["argv"]
    assert argv[argv.index("--mode") + 1] == "plan"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    denied = next(a for a in argv if a.startswith("--disallowed-tools="))
    for tool in ("Bash", "Edit", "Write", "Agent", "WebFetch",
                 "mcp__node_repl__js"):
        assert tool in denied.split("=", 1)[1].split(",")
    prompt = next(a for a in argv if a.startswith("--prompt="))
    assert "SYS\n\nPROMPT" in prompt and str(req.scope.root) in prompt
    # the cwd is a scratch dir we own, never the PR worktree, and it holds
    # the bridge config
    assert capture["cwd"] != req.scope.root
    assert argv[argv.index("--cwd") + 1] == capture["cwd"]
    server = json.loads(capture["config"])["mcp"]["servers"][
        "infermatrix-tools"]
    assert server["type"] == "stdio"
    assert "infermatrix_copilot.tool_bridge" in server["args"]
    assert capture["env_key"] == ""  # sanitized env
    # the scratch dir is removed and the worktree untouched
    assert not Path(capture["cwd"]).exists()
    assert list(Path(req.scope.root).iterdir()) == []

    session = next(e for e in req.trace.events
                   if e["kind"] == "harness_session")
    assert session["audit_ok"] is True and session["bridge_calls"] == 1
    assert session["served_model"] == "GLM-5.3-Flash"


def test_tool_outside_allowlist_is_an_audit_refusal(tmp_path):
    transport = _transport(tmp_path)
    (tmp_path / "bin" / "rogue").write_text("", encoding="utf-8")
    req = _request(tmp_path)

    outcome = transport.run_session(req)

    assert outcome.refusals == [
        "audit: zcode tool outside the read-only allowlist: Bash"]
    session = next(e for e in req.trace.events
                   if e["kind"] == "harness_session")
    assert session["audit_ok"] is False


def test_native_read_outside_roots_is_an_audit_refusal(tmp_path):
    transport = _transport(tmp_path)
    (tmp_path / "bin" / "outside").write_text("", encoding="utf-8")

    outcome = transport.run_session(_request(tmp_path))

    # the in-session relative read and the bridge call (preventively scoped
    # by the bridge itself) are not flagged; the two escapes are
    assert outcome.refusals == [
        "audit: Glob outside session roots: /root/.ssh",
        "audit: Read outside session roots: /etc/passwd"]


def test_relative_glob_traversal_is_flagged(tmp_path):
    cwd = str(tmp_path / "session")
    roots = (cwd,)
    audit = ZCodeTransport._audit
    assert audit([("Glob", {"pattern": "../../etc/*"})],
                 roots=roots, cwd=cwd) == [
        "Glob outside session roots: "
        + str((tmp_path / "session" / "../../etc").resolve())]
    assert audit([("Glob", {"pattern": "*.md", "path": "/etc"})],
                 roots=roots, cwd=cwd) != []
    assert audit([("Glob", {"pattern": "src/**/*.py"}),
                  ("Grep", {"pattern": "x"})], roots=roots, cwd=cwd) == []


def test_complete_removes_native_reads(tmp_path):
    transport = _transport(tmp_path)
    transport.complete(system="S", messages=[])

    argv = _capture(tmp_path)["argv"]
    denied = next(a for a in argv if a.startswith("--disallowed-tools="))
    assert {"Read", "Grep", "Glob"} <= set(denied.split("=", 1)[1].split(","))


def test_complete_out_of_root_read_fails_the_call(tmp_path):
    transport = _transport(tmp_path)
    (tmp_path / "bin" / "outside").write_text("", encoding="utf-8")
    big = "x" * (zcode_mod._ARG_BUDGET + 10)

    with pytest.raises(RuntimeError, match="broke containment.*/etc/passwd"):
        transport.complete(system="S", messages=[
            {"role": "user", "content": big}])
    argv = _capture(tmp_path)["argv"]
    denied = next(a for a in argv if a.startswith("--disallowed-tools="))
    # the oversized prompt keeps Read for its attachment, nothing else
    assert "Read" not in denied.split("=", 1)[1].split(",")
    assert "Glob" in denied.split("=", 1)[1].split(",")


def test_failed_run_raises_with_stderr(tmp_path):
    transport = _transport(tmp_path)
    (tmp_path / "bin" / "fail").write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="exited 1.*not logged in"):
        transport.run_session(_request(tmp_path))
    with pytest.raises(RuntimeError, match="not logged in"):
        transport.complete(system="S", messages=[])


def test_missing_result_event_raises(tmp_path):
    transport = _transport(tmp_path)
    (tmp_path / "bin" / "noresult").write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="without a result event"):
        transport.run_session(_request(tmp_path))


def test_oversized_prompt_is_attached_not_passed_in_argv(tmp_path):
    transport = _transport(tmp_path)
    big = "x" * (zcode_mod._ARG_BUDGET + 10)
    transport.run_session(_request(tmp_path, prompt=big))

    capture = _capture(tmp_path)
    assert all(len(a) < zcode_mod._ARG_BUDGET for a in capture["argv"])
    assert "--attach" in capture["argv"]
    assert capture["attached"].endswith(big)


def test_model_assertion_matches_case_insensitively(tmp_path):
    transport = _transport(tmp_path, strict_backend_model="glm-5.3-flash")
    req = _request(tmp_path)

    assert transport.run_session(req).text == "REVIEW"
    check = next(e for e in req.trace.events
                 if e["kind"] == "harness_model_check")
    assert check["verdict"] == "ok"


def test_model_mismatch_fails_by_default(tmp_path):
    transport = _transport(tmp_path, strict_backend_model="GLM-5.3-Flash")
    (tmp_path / "bin" / "model").write_text("GLM-5.3", encoding="utf-8")

    with pytest.raises(ModelMismatchError, match="GLM-5.3"):
        transport.run_session(_request(tmp_path))


def test_model_mismatch_warns_under_policy_warn(tmp_path):
    transport = _transport(tmp_path, strict_backend_model="GLM-5.3-Flash",
                           model_mismatch_policy="warn")
    (tmp_path / "bin" / "model").write_text("GLM-5.3", encoding="utf-8")
    req = _request(tmp_path)

    assert transport.run_session(req).text == "REVIEW"
    check = next(e for e in req.trace.events
                 if e["kind"] == "harness_model_check")
    assert check["verdict"] == "mismatch"


def test_auth_gap_reports_login_fix(tmp_path, monkeypatch):
    transport = _transport(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(home))
    gap = transport.auth_gap()
    assert gap and "zcode login" in gap

    creds = home / ".zcode" / "v2" / "credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("{}", encoding="utf-8")
    assert transport.auth_gap() is None
    # the run logs in from the same data dir the readiness check inspected
    transport.complete(system="S", messages=[])
    assert _capture(tmp_path)["data_dir"] == str(home)


def test_run_session_timeout_is_truncated(tmp_path):
    transport = _transport(tmp_path)
    req = _request(tmp_path, with_bridge=False)
    req.timeout_s = 0.8
    (tmp_path / "bin" / "sleep").write_text("", encoding="utf-8")

    outcome = transport.run_session(req)

    assert outcome.truncated is True and outcome.text == ""


def test_complete_runs_in_scratch_without_bridge(tmp_path):
    transport = _transport(tmp_path)

    reply = transport.complete(
        system="CLASSIFY", messages=[{"role": "user", "content": "hi"}])

    assert reply.text == "REVIEW" and reply.model == "GLM-5.3-Flash"
    capture = _capture(tmp_path)
    assert "imc-zcode-oneshot-" in capture["cwd"]
    config = json.loads(capture["config"])
    assert config["features"] == {"memory": False, "skill": False, "subagent": False, "mcp": False}
    assert config["memory"] == {"use": False} and config["plugins"] == {"enabled": False}
    assert Path(config["storage"]["dir"]).parent == Path(capture["cwd"])
    assert "mcp" not in config
    prompt = next(a for a in capture["argv"] if a.startswith("--prompt="))
    assert "CLASSIFY" in prompt and "[USER]\nhi" in prompt


# -- per-run model pin ------------------------------------------------------------

def _host_config(tmp_path: Path, monkeypatch, provider: str = "account:test-plan") -> Path:
    base = tmp_path / "data"
    path = base / ".zcode" / "v2" / "provider_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schemaVersion": 1, "config": {
        "providerConfigRules": {"providerRules": [{"id": "keep-me", "apiKey": "sk-host-secret"}]},
        "modelConfigRules": {"providerModelRules": [], "manualProviderModelRules": []},
        "providerOrder": [provider],
        "defaultModelSelection": {"providerId": provider, "modelId": "GLM-5.3",
                                  "options": {"reasoningLevel": "max"}}}}), encoding="utf-8")
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(base))
    return path


def test_complete_pins_the_model_through_a_session_provider_config(tmp_path, monkeypatch):
    host = _host_config(tmp_path, monkeypatch)
    before = host.read_text(encoding="utf-8")
    transport = _transport(tmp_path, strict_backend_model="GLM-5.3-Flash")
    reply = transport.complete(system="S", messages=[{"role": "user", "content": "hi"}])
    assert reply.model == "GLM-5.3-Flash"
    cap = _capture(tmp_path)
    # the pin rides a file INSIDE the session scratch dir, never the host's file
    assert cap["personal"] and "imc-zcode-oneshot-" in cap["personal"]
    written = json.loads(cap["personal_text"])
    assert written["config"]["defaultModelSelection"] == {
        "providerId": "account:test-plan", "modelId": "GLM-5.3-Flash",
        "options": {"reasoningLevel": "max"}}
    # only the host's provider id is carried over: its provider rules (which
    # may hold API keys) never enter a read root of the session
    assert written["config"]["providerConfigRules"] == {"providerRules": []}
    assert "providerOrder" not in written["config"]
    assert "keep-me" not in cap["personal_text"] and "sk-host-secret" not in cap["personal_text"]
    assert host.read_text(encoding="utf-8") == before


def test_pin_writes_the_catalog_casing_of_the_requested_model(tmp_path, monkeypatch):
    _host_config(tmp_path, monkeypatch)
    transport = _transport(tmp_path, strict_backend_model="glm-5.3-flash")
    reply = transport.complete(system="S", messages=[{"role": "user", "content": "hi"}])
    assert reply.model == "GLM-5.3-Flash"           # the fake serves the catalog id; the assertion is case-insensitive
    written = json.loads(_capture(tmp_path)["personal_text"])
    assert written["config"]["defaultModelSelection"]["modelId"] == "GLM-5.3-Flash"
    # the bundled catalog under the zcode home wins over the built-in list; unknown ids pass through
    catalog = tmp_path / "data" / ".zcode" / "v2" / "runtime" / "provider" / "bundled" / "zcode-builtin.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"config": {"builtinModelIds": ["GLM-9-Ultra"]}}), encoding="utf-8")
    assert transport.canonical_model_id("glm-9-ultra") == "GLM-9-Ultra"
    assert transport.canonical_model_id("not-a-model") == "not-a-model"


def test_run_session_pins_the_request_model_at_the_configured_level(tmp_path, monkeypatch):
    _host_config(tmp_path, monkeypatch)
    transport = _transport(tmp_path, zcode_reasoning_level="high")
    req = _request(tmp_path, with_bridge=False)
    req = type(req)(**{**req.__dict__, "model": "GLM-5.3-Flash"})
    transport.run_session(req)
    written = json.loads(_capture(tmp_path)["personal_text"])
    assert written["config"]["defaultModelSelection"]["modelId"] == "GLM-5.3-Flash"
    assert written["config"]["defaultModelSelection"]["options"] == {"reasoningLevel": "high"}


def test_no_requested_model_leaves_the_host_default_alone(tmp_path, monkeypatch):
    _host_config(tmp_path, monkeypatch)
    transport = _transport(tmp_path)
    transport.complete(system="S", messages=[{"role": "user", "content": "hi"}])
    assert _capture(tmp_path)["personal"] == ""


def test_pin_without_a_host_config_names_the_coding_plan_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(tmp_path / "empty"))
    transport = _transport(tmp_path, strict_backend_model="GLM-5.3-Flash",
                           zcode_provider_id="")
    transport.complete(system="S", messages=[{"role": "user", "content": "hi"}])
    written = json.loads(_capture(tmp_path)["personal_text"])
    assert written["config"]["defaultModelSelection"]["providerId"] == \
        "account:bigmodel-individual-coding-plan"
    assert written["config"]["modelConfigRules"] == {"providerModelRules": [],
                                                     "manualProviderModelRules": []}


def test_settings_reject_an_unknown_reasoning_level():
    with pytest.raises(Exception):
        Settings(_env_file=None, zcode_reasoning_level="ludicrous")


@pytest.mark.parametrize("payload,code,retryable", [
    ({"errorCode": "model_rate_limited", "retryable": False, "providerErrorCode": "1310", "statusCode": 429}, "model_quota_exceeded", False),
    ({"errorCode": "model_rate_limited", "retryable": True, "statusCode": 429}, "model_rate_limited", True),
    ({"errorCode": "model_authentication_failed", "statusCode": 401}, "model_authentication_required", False),
    ({"errorCode": "model_connection_error", "statusCode": 503}, "model_unavailable", True),
])
def test_known_failure_events_have_typed_safe_recovery_without_raw_text(tmp_path, monkeypatch, payload, code, retryable):
    from infermatrix_copilot.providers.zcode import ZCodeFailure
    transport = _transport(tmp_path)
    payload.update(providerErrorMessage="PRIVATE_PROVIDER_MESSAGE", requestHeaders={"Authorization": "SECRET_TOKEN"})
    monkeypatch.setattr(zcode_mod, "run_cli", lambda *args, **kwargs: (
        json.dumps({"type": "session.updated", "payload": payload}), "SECRET_STDERR", 1, False))
    with pytest.raises(ZCodeFailure) as exc:
        transport.complete(system="PRIVATE_SYSTEM_PROMPT", messages=[])
    assert exc.value.code == code and exc.value.retryable is retryable
    assert "PRIVATE" not in str(exc.value) and "SECRET" not in str(exc.value)
