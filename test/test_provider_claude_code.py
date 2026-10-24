"""Claude Code transport against a fake claude CLI — fully offline.

The fake reproduces the -p --output-format json contract observed live on
claude 2.1.232 (single JSON object: result / num_turns / stop_reason /
total_cost_usd / usage / modelUsage) and captures its invocation."""

import json
import stat
from pathlib import Path

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.providers.base import AgentSessionRequest
from infermatrix_copilot.providers.claude_code import ClaudeCodeTransport
from infermatrix_copilot.scopes import read_only_scope
from infermatrix_copilot.tool_bridge import write_bridge_spec

_FAKE_CLI = """#!/usr/bin/env python3
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
text = sys.stdin.read()
with open(os.path.join(here, "capture.json"), "w") as f:
    json.dump({"argv": sys.argv[1:], "stdin": text, "cwd": os.getcwd(),
               "env_key": os.environ.get("ANTHROPIC_API_KEY", ""),
               "env_claudecode": os.environ.get("CLAUDECODE", "")}, f)
if os.path.exists(os.path.join(here, "sleep")):
    time.sleep(10)
print("some stray warning line")
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "REVIEW", "num_turns": 3, "stop_reason": "end_turn",
    "total_cost_usd": 0.21,
    "usage": {"input_tokens": 100, "output_tokens": 20,
              "cache_read_input_tokens": 5,
              "cache_creation_input_tokens": 7},
    "modelUsage": {"claude-haiku-4-5": {"costUSD": 0.001},
                   "claude-fable-5": {"costUSD": 0.19}}}))
"""


class FakeTrace:
    def __init__(self):
        self.events = []

    def record(self, kind, **fields):
        self.events.append({"kind": kind, **fields})


def _transport(tmp_path: Path) -> ClaudeCodeTransport:
    cli = tmp_path / "bin" / "claude"
    cli.parent.mkdir(exist_ok=True)
    cli.write_text(_FAKE_CLI, encoding="utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return ClaudeCodeTransport(Settings(
        _env_file=None, strict_backend="claude-code",
        strict_backend_cli=str(cli)))


def _request(tmp_path: Path, with_bridge: bool = True) -> AgentSessionRequest:
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
        system="SYS", prompt="PROMPT", scope=scope, model="",
        max_iters=8, timeout_s=30.0, run_dir=run_dir,
        step_name="agent.review_diff", bridge_spec_path=bridge,
        trace=FakeTrace())


def test_run_session_flags_parse_and_governance(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    monkeypatch.setenv("CLAUDECODE", "1")
    transport = _transport(tmp_path)
    req = _request(tmp_path)

    outcome = transport.run_session(req)

    assert outcome.text == "REVIEW" and outcome.iterations == 3
    assert (outcome.input_tokens, outcome.output_tokens) == (100, 20)
    assert outcome.truncated is False

    capture = json.loads(
        (tmp_path / "bin" / "capture.json").read_text(encoding="utf-8"))
    argv = capture["argv"]
    # governance flags: built-ins denied, bridge-only via strict mcp config
    assert argv[argv.index("--tools") + 1] == ""
    assert json.loads(argv[argv.index("--settings") + 1]) == {"disableAllHooks": True}
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1] == "mcp__infermatrix-tools"
    assert argv[argv.index("--max-turns") + 1] == "8"
    assert argv[argv.index("--system-prompt") + 1] == "SYS"
    assert "--exclude-dynamic-system-prompt-sections" in argv
    # prompt rides stdin; the system prompt does NOT (it has its own channel)
    assert capture["stdin"] == "PROMPT"
    # sanitized env: no API key (subscription auth) and no host marker
    assert capture["env_key"] == "" and capture["env_claudecode"] == ""

    # mcp config written NEXT TO the spec, never into the worktree
    config = Path(argv[argv.index("--mcp-config") + 1])
    assert config.parent == req.bridge_spec_path.parent
    body = json.loads(config.read_text(encoding="utf-8"))
    assert "tool_bridge" in " ".join(
        body["mcpServers"]["infermatrix-tools"]["args"])

    session = [e for e in req.trace.events if e["kind"] == "harness_session"]
    assert session[0]["cost_usd"] == 0.21
    assert session[0]["served_model"] == "claude-fable-5"  # max-cost entry


def test_bridge_activity_counts_only_new_trace_lines(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    trace = run_dir / "bridge_trace.jsonl"
    trace.write_text(json.dumps({"kind": "tool_call", "tool": "grep"}) + "\n",
                     encoding="utf-8")
    before = ClaudeCodeTransport._trace_lines(run_dir)
    with trace.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "tool_call", "tool": "read_file"}) + "\n")
        f.write(json.dumps({"kind": "tool_refused", "tool": "read_file"}) + "\n")
    calls, used = ClaudeCodeTransport._bridge_activity(run_dir, before)
    assert (calls, used) == (1, ["read_file"])


def test_run_session_timeout_is_truncated(tmp_path):
    transport = _transport(tmp_path)
    req = _request(tmp_path, with_bridge=False)
    req.timeout_s = 0.8
    (tmp_path / "bin" / "sleep").write_text("", encoding="utf-8")

    outcome = transport.run_session(req)

    assert outcome.truncated is True and outcome.text == ""


def test_complete_is_toolless_and_scratch(tmp_path):
    transport = _transport(tmp_path)

    reply = transport.complete(
        system="CLASSIFY", messages=[{"role": "user", "content": "hi"}])

    assert reply.text == "REVIEW"
    assert reply.usage["cache_read_input_tokens"] == 5
    capture = json.loads(
        (tmp_path / "bin" / "capture.json").read_text(encoding="utf-8"))
    assert "imc-claude-oneshot-" in capture["cwd"]
    argv = capture["argv"]
    assert "--mcp-config" not in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert json.loads(argv[argv.index("--settings") + 1]) == {"disableAllHooks": True}
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1] == ""
    assert argv[argv.index("--system-prompt") + 1] == "CLASSIFY"
    assert "[USER]\nhi" in capture["stdin"]


@pytest.mark.parametrize("stdout,code", [
    ("", 1), ("warning only", 0), ("{}", 0),
    ('{"type":"result"}', 0),
    ('{"type":"result","is_error":false}', 0),
    ('{"type":"result","is_error":false,"result":"OK"}', 1),
])
@pytest.mark.parametrize("mode", ["completion", "session"])
def test_invalid_terminal_output_never_becomes_success(
    tmp_path, monkeypatch, stdout, code, mode,
):
    transport = _transport(tmp_path)
    monkeypatch.setattr("infermatrix_copilot.providers.claude_code.run_cli",
                        lambda *args, **kwargs: (stdout, "private diagnostic", code, False))
    with pytest.raises(RuntimeError, match="without a valid terminal result"):
        if mode == "completion":
            transport.complete(system="S", messages=[])
        else:
            transport.run_session(_request(tmp_path, with_bridge=False))


def test_budget_error_envelope_retains_reported_spend(tmp_path, monkeypatch):
    transport = _transport(tmp_path)
    data = {"type": "result", "is_error": True, "subtype": "error_max_budget_usd",
            "total_cost_usd": 0.02, "usage": {"input_tokens": 10, "output_tokens": 2}}
    monkeypatch.setattr("infermatrix_copilot.providers.claude_code.run_cli",
                        lambda *args, **kwargs: (json.dumps(data), "", 1, False))
    reply = transport.complete(system="S", messages=[], max_budget_usd=0.01)
    assert reply.text == "" and reply.stop_reason == "max_budget"
    assert reply.usage["cost_usd"] == 0.02


@pytest.mark.parametrize("metadata", [None, [], "bad", {}, {"costUSD": "bad"},
    {"costUSD": True}, {"costUSD": -1}, {"costUSD": float("nan")},
    {"costUSD": float("inf")}, {"costUSD": 10 ** 400}])
def test_malformed_model_metadata_retains_known_usage(metadata):
    data = {"usage": {"input_tokens": 10, "output_tokens": 2},
            "total_cost_usd": .02,
            "modelUsage": {"unknown-cost-model": metadata, "helper": {"costUSD": .001}}}
    snapshot = ClaudeCodeTransport.native_snapshot([
        {"type": "native.claude-code.event", "payload": data}])
    assert snapshot["usage"] == {"input_tokens": 10, "output_tokens": 2, "cost_usd": .02}
    assert snapshot["served_model"] == ""


@pytest.mark.parametrize("subtype", ["error_during_execution", "error_max_budget_usd", "error_max_turns"])
def test_session_error_keeps_receipt_and_cannot_look_completed(tmp_path, monkeypatch, subtype):
    from infermatrix_copilot.providers import run_harness_session
    from infermatrix_copilot.budgeting import bind_call_budget

    transport = _transport(tmp_path)
    data = {"type": "result", "is_error": True, "subtype": subtype,
            "stop_reason": "max_turns",
            "usage": {"input_tokens": 10, "output_tokens": 2}, "total_cost_usd": .02,
            "modelUsage": {"bad": {"costUSD": "invalid"}}}
    monkeypatch.setattr("infermatrix_copilot.providers.claude_code.run_cli",
                        lambda *args, **kwargs: (json.dumps(data), "", 1, False))
    settlements = []
    with bind_call_budget("test", lambda _: "ticket",
                          lambda ticket, facts: settlements.append(dict(facts))):
        if subtype == "error_max_turns":
            outcome = run_harness_session(transport, _request(tmp_path, with_bridge=False))
            assert outcome.truncated
        else:
            with pytest.raises(RuntimeError, match="session errored"):
                run_harness_session(transport, _request(tmp_path, with_bridge=False))
    assert len(settlements) == 1
    assert settlements[0]["usage"] == {"input_tokens": 10, "output_tokens": 2, "cost_usd": .02}
    if subtype != "error_max_turns":
        assert settlements[0]["outcome"] != "completed"



def test_native_authentication_status_does_not_claim_subscription_billing():
    from types import SimpleNamespace
    transport=ClaudeCodeTransport(SimpleNamespace())
    for method in ('claude.ai','oauth_token','api_key','api_key_helper','third_party'):
        assert transport.auth_status_issue(0,json.dumps({'loggedIn':True,'authMethod':method})) is None
    assert getattr(transport,'subscription_billing',False) is False
    assert 'unavailable' in transport.auth_status_issue(0,'{}')
    assert 'not logged in' in transport.auth_status_issue(1,'SECRET_ACCOUNT')
