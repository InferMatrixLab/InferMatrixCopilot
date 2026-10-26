import json
import sys
from types import SimpleNamespace

import pytest

from infermatrix_copilot.budgeting import bind_call_budget
from infermatrix_copilot.providers.json_session import (
    JSONSessionError, readonly_environment, run_readonly_json,
)


def session(tmp_path, source, provider="cursor", **kwargs):
    cli = tmp_path / "cli.py"
    cli.write_text(source, encoding="utf-8")
    return run_readonly_json("original evidence", cwd=tmp_path,
        command=[sys.executable, str(cli)], provider=provider, model="chosen",
        output_schema={"type": "object"}, idle_timeout_s=2,
        absolute_timeout_s=4, **kwargs)


def test_codex_native_schema_and_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "publisher-secret")
    result = session(tmp_path, '''
import json, os, pathlib, sys
args = sys.argv
assert all(flag in args for flag in ["--ephemeral", "--ignore-user-config", "--strict-config", "--json"])
assert args[args.index("--sandbox") + 1] == "read-only"
assert "GH_TOKEN" not in os.environ
pathlib.Path(args[args.index("--output-last-message") + 1]).write_text('{"accepted":true}')
print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":31}}))
''', "codex")
    assert result["payload"] == {"accepted": True}
    assert result["usage"] == {"input_tokens": 31}


@pytest.mark.parametrize("output_format", ["json", "stream-json"])
def test_cursor_same_session_repair(tmp_path, output_format):
    result = session(tmp_path, '''
import json, sys
args = sys.argv
assert "--mode=ask" in args and "--force" not in args
assert args[args.index("--output-format") + 1] in {"json", "stream-json"}
sys.stdin.read()
repair = "--resume" in args
if repair:
    assert args[args.index("--resume") + 1] == "original-session"
print(json.dumps({"type":"result", "subtype":"success", "session_id":"original-session",
                  "result":'{"rules":[]}' if repair else 'not JSON'}))
''', output_format=output_format, allow_sessionless_retry=False,
        result_validator=lambda payload: None if isinstance(payload.get("rules"), list) else "rules required")
    assert result["payload"] == {"rules": []}
    assert result["session_id"] == "original-session"


def test_invalid_envelope_does_not_retry(tmp_path):
    with pytest.raises(JSONSessionError, match="invalid envelope"):
        session(tmp_path, 'print("[]")', output_format="json")


@pytest.mark.parametrize("session_id", [None, "original"])
@pytest.mark.parametrize("elapsed", [8, 11])
def test_repair_and_sessionless_retry_share_original_deadline(tmp_path, monkeypatch, session_id, elapsed):
    from infermatrix_copilot.providers import json_session

    clock = [100.0]
    deadlines = []
    monkeypatch.setattr(json_session, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def invoke(*args, **kwargs):
        deadlines.append(kwargs["absolute"])
        clock[0] += elapsed
        return {"text": "not JSON" if len(deadlines) == 1 else "{}",
                "session_id": session_id, "served_model": "chosen", "usage": None}

    monkeypatch.setattr(json_session, "_invoke", invoke)
    def run():
        return run_readonly_json("evidence", cwd=tmp_path, command=[sys.executable],
            provider="cursor", model="chosen", output_schema={"type":"object"},
            idle_timeout_s=5, absolute_timeout_s=10)
    if elapsed > 10:
        with pytest.raises(JSONSessionError) as caught:
            run()
        assert caught.value.timeout
        assert deadlines == [110.0]
    else:
        assert run()["payload"] == {}
        assert deadlines == [110.0, 110.0]


def test_sessionless_retry_cannot_gain_another_repair(tmp_path, monkeypatch):
    from infermatrix_copilot.providers import json_session

    calls = []
    def invoke(*args, **kwargs):
        calls.append(args)
        return {"text": "not JSON" if len(calls) < 3 else "{}",
                "session_id": None if len(calls) == 1 else "new-session",
                "served_model": "chosen", "usage": None}
    monkeypatch.setattr(json_session, "_invoke", invoke)
    with pytest.raises(JSONSessionError) as caught:
        run_readonly_json("evidence", cwd=tmp_path, command=[sys.executable],
            provider="cursor", output_schema={"type":"object"},
            idle_timeout_s=5, absolute_timeout_s=10, max_repair_attempts=1)
    assert caught.value.failure_class == "invalid_result"
    assert len(calls) == 2


def test_native_failure_prose_cannot_trigger_wrap_up(tmp_path, monkeypatch):
    from infermatrix_copilot.providers import json_session

    calls = []
    def invoke(*args, **kwargs):
        calls.append(args)
        kwargs["event_sink"]({"type":"result", "session_id":"original"})
        raise JSONSessionError("native failure: exceeded maximum runtime",
                               failure_class="invalid_result")
    monkeypatch.setattr(json_session, "_invoke", invoke)
    with pytest.raises(JSONSessionError) as caught:
        run_readonly_json("evidence", cwd=tmp_path, command=[sys.executable],
            provider="cursor", output_schema={"type":"object"},
            idle_timeout_s=5, absolute_timeout_s=10, wrap_up_after_s=3)
    assert caught.value.failure_class == "invalid_result"
    assert not caught.value.timeout
    assert len(calls) == 1


@pytest.mark.parametrize("provider,output_format", [
    ("cursor", "stream-json"), ("cursor", "json"),
    ("codex", "stream-json"), ("codex", "json"),
])
def test_zero_exit_failure_event_rejects_result_without_repair(tmp_path, provider, output_format):
    source = '''
import json, pathlib, sys
sys.stdin.read()
count = pathlib.Path("dispatch-count")
count.write_text(str(int(count.read_text()) + 1) if count.exists() else "1")
if "--output-last-message" in sys.argv:
 pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text('{"accepted":true}')
else:
 if "stream-json" in sys.argv:
  print(json.dumps({"type":"result", "result":'{"accepted":true}', "session_id":"original",
   "usage":{"input_tokens":0,"output_tokens":0}}))
print(json.dumps({"type":"turn.failed", "error":{"message":"native turn failed"},
 "usage":{"input_tokens":10,"output_tokens":2}, "session_id":"original",
 "result":'{"accepted":true}'}))
'''
    receipts = []
    with bind_call_budget("test", lambda _: "ticket",
                          lambda ticket, facts: receipts.append(dict(facts))):
        with pytest.raises(JSONSessionError, match="native turn failed") as caught:
            session(tmp_path, source, provider, output_format=output_format)
    assert caught.value.failure_class == "invalid_result"
    assert (tmp_path / "dispatch-count").read_text() == "1"
    assert len(receipts) == 1
    assert receipts[0]["usage"]["input_tokens"] == 10
    assert receipts[0]["usage"]["output_tokens"] == 2
    assert receipts[0]["outcome"] != "completed"


def test_budget_receipt_is_finished_for_failure(tmp_path):
    receipts = []
    with bind_call_budget("test", lambda request: request, lambda ticket, facts: receipts.append((ticket, dict(facts)))):
        with pytest.raises(JSONSessionError, match="exited with 7"):
            session(tmp_path, 'raise SystemExit(7)')
    assert len(receipts) == 1
    assert receipts[0][0]["kind"] == "harness_session"
    assert receipts[0][1]["sent"] is True
    assert receipts[0][1]["usage"] is None
    assert receipts[0][1]["outcome"] != "completed"


def test_readonly_environment_preserves_model_identity():
    result = readonly_environment({"GH_TOKEN":"secret", "OPENAI_API_KEY":"model",
        "SSH_AUTH_SOCK":"socket", "GIT_CONFIG_COUNT":"1", "GIT_CONFIG_KEY_0":"secret"})
    assert result["OPENAI_API_KEY"] == "model"
    assert "GH_TOKEN" not in result and "SSH_AUTH_SOCK" not in result
    assert "GIT_CONFIG_KEY_0" not in result


def test_zero_repairs_keeps_one_dispatch_even_with_session_id(tmp_path):
    import sys
    from infermatrix_copilot.providers.json_session import JSONSessionError, run_readonly_json
    count = tmp_path / "count"
    cli = tmp_path / "fake_cursor.py"
    cli.write_text("import json,pathlib\np=pathlib.Path(" + repr(str(count)) + ")\np.write_text(str(int(p.read_text())+1) if p.exists() else '1')\nprint(json.dumps({'session_id':'same-session','result':'invalid-json'}))\n")
    with pytest.raises(JSONSessionError) as caught:
        run_readonly_json("classification", cwd=tmp_path, command=[sys.executable, str(cli)],
            provider="cursor", output_schema={"type":"object"}, output_format="json",
            idle_timeout_s=5, absolute_timeout_s=5, max_repair_attempts=0)
    assert caught.value.failure_class == "invalid_result"
    assert count.read_text() == "1"


@pytest.mark.parametrize("output_format", ["json", "stream-json"])
@pytest.mark.parametrize("ending", ["success", "exit", "timeout"])
def test_structured_sessions_settle_canonical_usage_on_every_exit(tmp_path, output_format, ending):
    source = '''
import json, pathlib, sys, time
sys.stdin.read()
for inputs, outputs, cached in [(10, 2, 3), (20, 4, 5)]:
 print(json.dumps({"type":"turn.completed", "usage":{
  "input_tokens":inputs,"output_tokens":outputs,"cached_input_tokens":cached,"cache_write_input_tokens":0}}), flush=True)
pathlib.Path(sys.argv[sys.argv.index("--output-last-message")+1]).write_text('{"accepted":true}')
'''
    if ending == "exit":
        source += "raise SystemExit(7)\n"
    elif ending == "timeout":
        source += "time.sleep(10)\n"
    receipts = []
    with bind_call_budget("test", lambda _: "ticket",
                          lambda ticket, facts: receipts.append(dict(facts))):
        if ending == "success":
            result = session(tmp_path, source, "codex", output_format=output_format)
            # Existing public metadata retains its native shape and last report.
            assert result["usage"]["input_tokens"] == 20
            assert result["usage"]["cached_input_tokens"] == 5
        else:
            with pytest.raises(JSONSessionError):
                session(tmp_path, source, "codex", output_format=output_format)
    assert len(receipts) == 1
    assert receipts[0]["sent"] is True
    assert receipts[0]["usage"] == {
        "input_tokens": 30, "output_tokens": 6, "cache_read_input_tokens": 8,
        "cache_creation_input_tokens": 0, "input_tokens_include_cache": True}
    assert (receipts[0]["outcome"] == "completed") == (ending == "success")


@pytest.mark.parametrize("output_format", ["json", "stream-json"])
def test_event_recorder_failure_keeps_normalized_usage(tmp_path, output_format):
    receipts = []

    def broken_recorder(event):
        raise RuntimeError("archive unavailable")

    with bind_call_budget("test", lambda _: "ticket",
                          lambda ticket, facts: receipts.append(dict(facts))):
        with pytest.raises(RuntimeError, match="archive unavailable"):
            session(tmp_path, 'import json; print(json.dumps({"type":"result", "result":"{}", "usage":{"inputTokens":10,"outputTokens":2}}))',
                    output_format=output_format, event_sink=broken_recorder)
    assert len(receipts) == 1
    assert receipts[0]["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert receipts[0]["outcome"] != "completed"


@pytest.mark.parametrize("ending", ["exit", "timeout", "rejected_envelope"])
def test_buffered_multiline_failure_keeps_native_usage(tmp_path, ending):
    source = '''
import json, sys, time
sys.stdin.read()
print(json.dumps({"type":"result", "subtype":"error", "is_error":True,
 "usage":{"inputTokens":10,"outputTokens":2}}, indent=2), flush=True)
'''
    if ending == "exit":
        source += "raise SystemExit(7)\n"
    elif ending == "timeout":
        source += "time.sleep(10)\n"
    receipts = []
    with bind_call_budget("test", lambda _: "ticket",
                          lambda ticket, facts: receipts.append(dict(facts))):
        with pytest.raises(JSONSessionError) as caught:
            session(tmp_path, source, output_format="json")
    assert len(receipts) == 1
    assert receipts[0]["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert receipts[0]["outcome"] != "completed"
    if ending == "exit":
        assert caught.value.returncode == 7
    elif ending == "timeout":
        assert caught.value.timeout
    else:
        assert caught.value.failure_class == "invalid_result"
