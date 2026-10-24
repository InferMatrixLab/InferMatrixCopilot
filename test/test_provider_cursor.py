"""Cursor transport against a fake cursor-agent CLI — fully offline.

The fake binary reproduces the real CLI's observable contract (stream-json
on stdout, prompt on stdin, cwd = the session worktree) and captures its
invocation next to itself, so assertions cover exactly the seam the real
CLI would see."""

import json
import os
import stat

import pytest
from pathlib import Path

from infermatrix_copilot.config import Settings
from infermatrix_copilot.providers.base import AgentSessionRequest
from infermatrix_copilot.providers.cursor import CursorTransport
from infermatrix_copilot.scopes import read_only_scope
from infermatrix_copilot.tool_bridge import write_bridge_spec

_FAKE_CLI = """#!/usr/bin/env python3
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
text = sys.stdin.read()
with open(os.path.join(here, "capture.json"), "w") as f:
    json.dump({"argv": sys.argv[1:], "stdin": text, "cwd": os.getcwd(),
               "env_anthropic": os.environ.get("ANTHROPIC_BASE_URL", "")}, f)
if os.path.exists(os.path.join(here, "sleep")):
    time.sleep(10)
events = os.path.join(here, "events.jsonl")
if os.path.exists(events):
    sys.stdout.write(open(events).read())
else:
    mcp = "yes" if os.path.exists(os.path.join(os.getcwd(), ".cursor", "mcp.json")) else "no"
    print(json.dumps({"type": "tool_call", "tool_call": {"readToolCall": {
        "args": {"path": os.path.join(os.getcwd(), "somefile.py")}}}}))
    print(json.dumps({"type": "result", "result": "REVIEW mcp=" + mcp,
                      "model": "composer-2.5",
                      "usage": {"inputTokens": 10, "outputTokens": 5}}))
"""


class FakeTrace:
    def __init__(self):
        self.events = []

    def record(self, kind, **fields):
        self.events.append({"kind": kind, **fields})


def _fake_cli(tmp_path: Path) -> Path:
    cli = tmp_path / "bin" / "cursor-agent"
    cli.parent.mkdir()
    cli.write_text(_FAKE_CLI, encoding="utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli


def _transport(tmp_path: Path, **kw) -> CursorTransport:
    return CursorTransport(Settings(
        _env_file=None, strict_backend="cursor",
        strict_backend_cli=str(_fake_cli(tmp_path)),
        strict_backend_model="composer-2.5", **kw))


def _request(tmp_path: Path, transport: CursorTransport,
             with_bridge: bool = True) -> AgentSessionRequest:
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
        system="SYS", prompt="PROMPT", scope=scope, model="composer-2.5",
        max_iters=8, timeout_s=30.0, run_dir=run_dir,
        step_name="agent.review_diff", bridge_spec_path=bridge,
        trace=FakeTrace())


def test_run_session_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://wrong.gateway/v1")
    transport = _transport(tmp_path)
    req = _request(tmp_path, transport)

    outcome = transport.run_session(req)

    # the harness saw the MCP bridge config; the worktree was restored after
    assert outcome.text == "REVIEW mcp=yes"
    assert not (Path(req.scope.root) / ".cursor").exists()
    assert outcome.tool_calls == 1 and outcome.truncated is False
    assert (outcome.input_tokens, outcome.output_tokens) == (10, 5)
    assert outcome.refusals == []  # read stayed inside the worktree

    capture = json.loads(
        (tmp_path / "bin" / "capture.json").read_text(encoding="utf-8"))
    assert capture["cwd"] == os.path.realpath(req.scope.root) or \
        capture["cwd"] == req.scope.root  # session runs IN the worktree
    assert capture["stdin"].startswith("SYS\n\nPROMPT")
    assert "--model" in capture["argv"]
    assert capture["env_anthropic"] == ""  # sanitized env: no gateway leak

    session_events = [e for e in req.trace.events
                      if e["kind"] == "harness_session"]
    assert session_events and session_events[0]["audit_ok"] is True
    assert session_events[0]["served_model"] == "composer-2.5"


def test_run_session_flags_out_of_bounds_read(tmp_path):
    transport = _transport(tmp_path)
    req = _request(tmp_path, transport, with_bridge=False)
    (tmp_path / "bin" / "events.jsonl").write_text("\n".join([
        json.dumps({"type": "tool_call", "tool_call": {"readToolCall": {
            "args": {"path": "/etc/passwd"}}}}),
        json.dumps({"type": "result", "result": "REVIEW"}),
    ]) + "\n", encoding="utf-8")

    outcome = transport.run_session(req)

    assert outcome.text == "REVIEW"
    assert any("outside session roots" in r for r in outcome.refusals)
    session = [e for e in req.trace.events if e["kind"] == "harness_session"]
    assert session[0]["audit_ok"] is False


def test_run_session_timeout_is_truncated_not_a_crash(tmp_path):
    transport = _transport(tmp_path)
    req = _request(tmp_path, transport, with_bridge=False)
    req.timeout_s = 0.8
    (tmp_path / "bin" / "sleep").write_text("", encoding="utf-8")

    outcome = transport.run_session(req)

    assert outcome.truncated is True and outcome.text == ""


def test_complete_flattens_messages_and_runs_in_scratch_cwd(tmp_path):
    transport = _transport(tmp_path)
    (tmp_path / "bin" / "events.jsonl").write_text(json.dumps({
        "type": "result", "result": "REVIEW", "usage": {"inputTokens": 10, "outputTokens": 5}}))

    reply = transport.complete(
        system="CLASSIFY", messages=[{"role": "user", "content": "review pr 5"}])

    assert reply.text.startswith("REVIEW")
    capture = json.loads(
        (tmp_path / "bin" / "capture.json").read_text(encoding="utf-8"))
    assert "[USER]\nreview pr 5" in capture["stdin"]
    assert "CLASSIFY" in capture["stdin"]
    # one-shots run in an empty scratch dir, never in a repo
    assert "imc-cursor-oneshot-" in capture["cwd"]
    assert reply.usage["input_tokens"] == 10
    assert "--force" not in capture["argv"] and "--approve-mcps" not in capture["argv"]
    assert capture["argv"][capture["argv"].index("--mode") + 1] == "ask"
    assert capture["argv"][capture["argv"].index("--workspace") + 1] == capture["cwd"]


def test_audit_checks_paths_on_all_native_tools(tmp_path):
    # live-smoke lesson: grep/ls/glob calls carry paths too — a read-only
    # audit that checks only readToolCall lets an out-of-tree grep through
    from infermatrix_copilot.providers.audit import audit_events

    events = [
        {"type": "tool_call", "tool_call": {"grepToolCall": {
            "args": {"path": "/etc", "pattern": "key"}}}},
        {"type": "tool_call", "tool_call": {"lsToolCall": {
            "args": {"path": str(tmp_path)}}}},
    ]
    audit = audit_events(events, roots=(str(tmp_path),))
    assert audit.other_tool_calls == 2
    assert audit.violations == ["grep outside session roots: /etc"]
    assert audit.tools_used == ["grep", "ls"]


def test_audit_exempts_cli_mcp_result_spool(tmp_path):
    # cursor-agent buffers MCP tool results under ~/.cursor/projects/<p>/
    # agent-tools/ and reads them back — that is bridge-output consumption,
    # not an out-of-tree read
    from infermatrix_copilot.providers.audit import audit_events

    spool = "/home/u/.cursor/projects/some-worktree/agent-tools/abc123.txt"
    events = [{"type": "tool_call", "tool_call": {"readToolCall": {
        "args": {"path": spool}}}}]
    audit = audit_events(events, roots=(str(tmp_path),))
    assert audit.ok and audit.file_reads == 1


def test_cli_absence_is_a_named_error(tmp_path, monkeypatch):
    monkeypatch.setattr("infermatrix_copilot.providers.base.shutil.which",
                        lambda name: None)
    transport = CursorTransport(Settings(_env_file=None,
                                         strict_backend="cursor"))
    req = _request(tmp_path, transport, with_bridge=False)
    try:
        transport.run_session(req)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "STRICT_BACKEND_CLI" in str(exc)


def test_audit_resolves_relative_paths_against_session_cwd(tmp_path):
    """The CLI emits worktree-relative paths for in-tree accesses; they must
    resolve against the SESSION cwd, not the auditing process's cwd."""
    from infermatrix_copilot.providers.audit import audit_events

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    events = [
        {"type": "tool_call", "tool_call": {"readToolCall":
            {"args": {"path": "pkg/mod.py"}}}},        # in-tree, relative
        {"type": "tool_call", "tool_call": {"grepToolCall":
            {"args": {"path": "/etc"}}}},               # absolute, outside
    ]
    audit = audit_events(events, roots=(str(tmp_path),), cwd=str(tmp_path))
    assert [v for v in audit.violations if "pkg/mod.py" in v] == []
    assert any("/etc" in v for v in audit.violations)


@pytest.mark.parametrize('event', [
    {'type':'result','result':'plausible JSON','subtype':'error_max_turns','is_error':False},
    {'type':'result','result':{'ok':True}},
    {'type':'result','result':'plausible JSON','is_error':'false'},
    {'type':'error','error':{}}, {'type':'turn.failed'},
])
@pytest.mark.parametrize('entry', ['completion','session','readonly'])
def test_terminal_failure_classification_is_shared_across_all_callers(tmp_path,event,entry):
    transport = _transport(tmp_path)
    event = {**event,'model':'reported-model','usage':{'inputTokens':7,'outputTokens':3}}
    (tmp_path/'bin/events.jsonl').write_text(json.dumps(event))
    captured = []
    with pytest.raises((RuntimeError,ValueError), match='failure|malformed'):
        if entry=='readonly':
            transport.readonly_events(json.dumps(event))
        elif entry=='session':
            req=_request(tmp_path,transport,with_bridge=False);req.native_event_sink=captured.append
            transport.run_session(req)
        else:
            transport.complete(system='rules',messages=[{'role':'user','content':'evidence'}],native_event_sink=captured.append)
    if entry!='readonly':
        snapshot=transport.native_snapshot(captured)
        assert snapshot['usage']['input_tokens']==7 and snapshot['usage']['output_tokens']==3
        assert snapshot['served_model']=='reported-model'
    assert len(transport.readonly_events(json.dumps(event),validate=False))==1


def test_clean_exit_without_terminal_result_is_not_a_successful_completion(tmp_path):
    transport = _transport(tmp_path)
    (tmp_path/'bin/events.jsonl').write_text(json.dumps({'type':'system','model':'reported-model'}))
    with pytest.raises(RuntimeError, match='successful terminal'):
        transport.complete(system='rules',messages=[{'role':'user','content':'evidence'}])


def test_nonzero_exit_with_success_shaped_output_is_rejected_after_usage_capture(tmp_path):
    import subprocess
    transport = _transport(tmp_path)
    event={'type':'result','result':'{"ok":true}','usage':{'inputTokens':7,'outputTokens':3}}
    transport.runner=lambda *args,**kwargs: subprocess.CompletedProcess(args[0],1,json.dumps(event),'launch failure')
    captured=[]
    with pytest.raises(RuntimeError,match='successful terminal'):
        transport.complete(system='rules',messages=[{'role':'user','content':'evidence'}],native_event_sink=captured.append)
    assert transport.native_snapshot(captured)['usage']['input_tokens']==7



def test_malformed_model_metadata_remains_unknown_without_losing_usage():
    events=[{'type':'result','result':'OK','model':{'name':'invented'},
             'usage':{'inputTokens':7,'outputTokens':3}}]
    decoded=CursorTransport.decode_event(events[0])
    assert decoded.served_model is None and decoded.error is None
    snapshot=CursorTransport.native_snapshot([{'type':'native.cursor.event','payload':events[0]}])
    assert snapshot['served_model']=='' and snapshot['usage']['input_tokens']==7


def test_readonly_json_requires_a_terminal_object_but_legacy_result_type_is_optional():
    with pytest.raises(ValueError,match='malformed'):
        CursorTransport.readonly_events('{"type":"system","result":"not a result"}')
    assert CursorTransport.readonly_events('{"result":"legacy"}')==[{'result':'legacy'}]
