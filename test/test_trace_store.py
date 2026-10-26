"""trace/1: the shared trace store, the knowledge service's records, replay and export."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
from infermatrix_copilot.kb_service.replay import export_dataset, replay
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.trace_store import REDACTED, SCHEMA, TraceStore, redact, trace_context

ROOT = Path(__file__).resolve().parents[1]
JUDGE = ModelRole("judge", "codex", "gpt-6-sol", "medium")
CHEAP = ModelRole("judge", "codex", "gpt-6-mini", "low")


def test_authoritative_record_selection_keeps_controls_without_rebuilding_index(tmp_path):
    store = TraceStore(tmp_path/'traces', environ={})
    model = store.append('model_call', inputs={'prompt': 'original'})
    control = store.append('auth_probe', context=model['context'], result={'model_calls': 0})
    store.index_path.unlink()
    assert [row for row, _ in store.iter_records()] == [model, control]
    assert [row for row, _ in store.iter_records(kind='model_call')] == [model]
    assert [row for row, _ in store.iter_records(kind='auth_probe')] == [control]
    assert not store.index_path.exists()
    with pytest.raises(ValueError, match='unknown record kind'):
        list(store.iter_records(kind='model_calls'))
    path, = (store.root/'records').glob('*.jsonl')
    with path.open('a') as handle:
        handle.write('{unreadable-control\n')
    with pytest.raises(json.JSONDecodeError):
        list(store.iter_records(kind='model_call'))


class Transport:
    """A harness transport answering with scripted text (or raising)."""

    def __init__(self, answer):
        self.answer = answer

    def complete(self, *, system, messages, model, effort, role):
        text = self.answer(model, messages[0]["content"])
        if isinstance(text, Exception):
            raise text
        return SimpleNamespace(blocks=[SimpleNamespace(text=text)], model=model, stop_reason="end_turn",
                               usage={"input_tokens": 10, "output_tokens": 5})


def _gateway(store, answer):
    return ModelGateway(None, transport_factory=lambda provider: Transport(answer), recorder=trace_recorder(store))


def test_records_are_schema_v1_with_content_addressed_blobs_and_an_index(tmp_path):
    store = TraceStore(tmp_path / "traces", environ={})
    with trace_context(run_id="r1", playbook="kb-intake", repo="demo"):
        with trace_context(changeset_id="cs1", rule_ids=["DEMO-1a"]):
            first = store.append("model_call", inputs={"prompt": "same text"}, outputs={"reply": "{}"},
                                 model={"role": "judge", "model": "gpt-6-sol"})
        second = store.append("decision", inputs={"prompt": "same text"}, result={"status": "pass"})
    assert first["schema"] == SCHEMA and first["context"]["changeset_id"] == "cs1"
    assert "changeset_id" not in second["context"] and second["context"]["repo"] == "demo"
    assert first["inputs"]["prompt"] == second["inputs"]["prompt"]          # stored once
    assert store.blob(first["inputs"]["prompt"]) == "same text"
    assert len(list((tmp_path / "traces" / "blobs").rglob("*.gz"))) == 2
    assert [r["id"] for r in store.query(rule_id="DEMO-1a")] == [first["id"]]
    assert [r["id"] for r in store.query(run_id="r1", kind="decision")] == [second["id"]]
    assert store.get(first["id"]) == first
    (tmp_path / "traces" / "index.db").unlink()
    assert store.rebuild_index() == 2 and store.get(second["id"]) == second
    assert first["env"]["copilot_version"]


def test_secrets_never_reach_records_or_blobs(tmp_path):
    env = {"GH_TOKEN": "tok-value-1234567890", "KB_SIGNING_KEY": "/path/to/key.pem"}
    store = TraceStore(tmp_path / "traces", environ=env)
    secrets = ["ghp_" + "a" * 36, "github_pat_" + "b" * 40, "sk-ant-api03-" + "c" * 30,
               "AKIA" + "D" * 16, "Bearer " + "e" * 30, "tok-value-1234567890"]
    text = "headers: " + " | ".join(secrets) + " key file /path/to/key.pem"
    record = store.append("model_call", inputs={"prompt": text}, outputs={"reply": text},
                          result={"note": text, secrets[0]: 1}, error=text, context={"note": text},
                          model={"served_model": secrets[1]}, usage={secrets[2]: 3})
    raw = b"".join(p.read_bytes() for p in (tmp_path / "traces").rglob("*.jsonl"))
    raw += (tmp_path / "traces" / "index.db").read_bytes()
    import gzip
    raw += b"".join(gzip.decompress(p.read_bytes()) for p in (tmp_path / "traces").rglob("*.gz"))
    for secret in secrets:
        assert secret.encode() not in raw
    assert REDACTED in store.blob(record["inputs"]["prompt"])
    assert "/path/to/key.pem" in store.blob(record["inputs"]["prompt"])      # key PATHS are not secrets
    assert redact("nothing here", env) == "nothing here"
    as_bytes = store.put_blob(("token " + secrets[0]).encode())
    assert secrets[0] not in store.blob(as_bytes) and REDACTED in store.blob(as_bytes)
    with pytest.raises(TypeError):
        store.put_blob(b"\xff\xfe binary")


def test_the_gateway_records_every_call_including_failures(tmp_path):
    store = TraceStore(tmp_path / "traces", environ={})
    gateway = _gateway(store, lambda model, prompt: RuntimeError("quota") if "fail" in prompt else '{"verdict": "consistent"}')
    with trace_context(playbook="kb-intake", changeset_id="cs1"):
        gateway.call_json(JUDGE, system="sys", prompt="ok")
        with pytest.raises(Exception):
            gateway.call_json(JUDGE, system="sys", prompt="please fail")
    ok, failed = store.query(kind="model_call")
    assert ok["model"] == {"role": "judge", "provider": "codex", "model": "gpt-6-sol", "effort": "medium",
                           "served_model": "gpt-6-sol"}
    assert ok["usage"]["input_tokens"] == 10 and ok["context"]["changeset_id"] == "cs1"
    assert store.blob(ok["outputs"]["reply"]) == '{"verdict": "consistent"}'
    assert "quota" in failed["error"] and store.blob(failed["inputs"]["prompt"]) == "please fail"


@pytest.mark.parametrize("reply", ["", "not json", '{"unexpected": 1}'])
def test_unusable_replies_are_recorded_as_failures_and_never_exported(tmp_path, reply):
    store = TraceStore(tmp_path / "traces", environ={})
    gateway = _gateway(store, lambda model, prompt: reply if model == "gpt-6-sol" else '{"verdict": "consistent"}')

    def validate(data):
        if "verdict" not in data:
            raise ValueError("verdict missing")
    with trace_context(playbook="kb-intake"), pytest.raises(Exception):
        gateway.call_json(JUDGE, system="s", prompt="q", validate=validate)
    (call,) = store.query(kind="model_call")
    assert call["error"]
    assert export_dataset(store, tmp_path / "out.jsonl")["written"] == 0
    result = replay(store, call["id"], gateway, CHEAP)       # a failed call can still be replayed
    assert result["recorded"] is None and result["replayed"] == {"verdict": "consistent"}
    assert store.query(kind="replay")[0]["result"]["replay_of"] == call["id"]


def test_replay_asks_a_substitute_model_the_recorded_question(tmp_path):
    store = TraceStore(tmp_path / "traces", environ={})
    answers = {"gpt-6-sol": '{"dimensions": {"correct": "yes"}}', "gpt-6-mini": '{"dimensions": {"correct": "no"}}'}
    gateway = _gateway(store, lambda model, prompt: answers[model])
    with trace_context(playbook="kb-intake"):
        gateway.call_json(JUDGE, system="sys", prompt="is it correct?")
    (call,) = store.query(kind="model_call")
    result = replay(store, call["id"], gateway, CHEAP)
    assert result["recorded"] == {"dimensions": {"correct": "yes"}}
    assert result["replayed"] == {"dimensions": {"correct": "no"}} and result["agree"] is False
    (record,) = store.query(kind="replay")
    assert record["result"]["replay_of"] == call["id"] and record["result"]["replay_model"] == CHEAP.label()
    replayed_call = store.query(kind="model_call", model="gpt-6-mini")[0]
    assert replayed_call["context"]["playbook"] == "kb-replay"
    assert store.blob(replayed_call["inputs"]["prompt"]) == "is it correct?"
    answers["gpt-6-mini"] = answers["gpt-6-sol"]
    assert replay(store, call["id"], gateway, CHEAP)["agree"] is True


def test_export_joins_decisions_and_outcomes_and_drops_calibration_leaks(tmp_path):
    store = TraceStore(tmp_path / "traces", environ={})
    calib = tmp_path / "calib"
    (calib / "cases").mkdir(parents=True)
    (calib / "cases" / "c1.json").write_text(json.dumps({
        "id": "c1", "expected": "reject", "head": {"x.md": "x"},
        "evidence": [{"source_reference": "PR #8107", "title": "Run the perf step via run_benchmark.py"}]}))
    gateway = _gateway(store, lambda model, prompt: '{"verdict": "consistent"}')
    with trace_context(playbook="kb-intake", changeset_id="cs1", rule_ids=["DEMO-2a"], step="gate"):
        gateway.call_json(JUDGE, system="s", prompt=json.dumps({"source_reference": "PR #11"}))
    with trace_context(playbook="kb-intake", changeset_id="cs2", step="gate"):
        gateway.call_json(JUDGE, system="s", prompt=json.dumps({"source_reference": "PR #8107"}, indent=1))
    with trace_context(playbook="kb-intake", changeset_id="cs3", step="consistency"):
        gateway.call_json(JUDGE, system="s", prompt="rule text ... ^[PR #8107]")       # cited in rule text
    with trace_context(playbook="kb-intake", changeset_id="cs4", step="gate"):
        gateway.call_json(JUDGE, system="s", prompt="unrelated PR #81070")
    with trace_context(playbook="kb-calibrate"):
        gateway.call_json(JUDGE, system="s", prompt="calibration case")
    with trace_context(playbook="kb-replay"):
        gateway.call_json(JUDGE, system="s", prompt="a replay")
    store.append("decision", context={"changeset_id": "cs1"}, result={"status": "pass"})
    store.append("outcome", context={"changeset_id": "cs1"}, result={"outcome": "merged"})
    store.append("outcome", context={"changeset_id": "cs9", "rule_ids": ["DEMO-2a"]},
                 result={"outcome": "rule_retired", "rule_id": "DEMO-2a"})
    counts = export_dataset(store, tmp_path / "out.jsonl", calibration_dirs=[calib])
    assert counts == {"written": 2, "leak_dropped": 2, "calibration_dropped": 2, "failed_dropped": 0,
                      "structured_skipped": 0}
    row, other = [json.loads(line) for line in (tmp_path / "out.jsonl").read_text().splitlines()]
    assert other["changeset_id"] == "cs4"
    assert row["changeset_id"] == "cs1" and row["decision"] == {"status": "pass"}
    assert row["outcomes"] == ["merged"] and row["rule_outcomes"] == {"DEMO-2a": ["rule_retired"]}
    assert row["output"] == '{"verdict": "consistent"}' and row["task"] == "judge:gate"


def test_generator_calls_of_a_real_intake_join_their_change_set(tmp_path):
    from test_kb_flow import _flow_runtime
    from test_kb_intake_gate import _generator_then_judge
    from infermatrix_copilot.kb_service.runtime import collect_events, run_intake

    rt, lifecycle = _flow_runtime(tmp_path)
    store = TraceStore(tmp_path / "traces", environ={})
    answer = _generator_then_judge()
    rt.traces = store
    rt.gateway = ModelGateway(None, recorder=trace_recorder(store), transport_factory=lambda provider: Transport(
        lambda model, prompt: json.dumps(answer(SimpleNamespace(
            name="generator" if model == rt.generator.model else "judge"), prompt))))
    collect_events(rt, lifecycle)
    changeset_id = run_intake(rt, lifecycle)
    (generator_call,) = store.query(kind="model_call", role="generator")
    assert "changeset_id" not in generator_call["context"] and generator_call["context"]["draft_key"]
    assert "_accepted" not in generator_call["context"] and generator_call["context"]["attempt"] == 0
    counts = export_dataset(store, tmp_path / "gen.jsonl", role="generator")
    (row,) = [json.loads(line) for line in (tmp_path / "gen.jsonl").read_text().splitlines()]
    assert counts["written"] == 1 and row["changeset_id"] == changeset_id
    assert row["decision"]["status"] == "pass" and row["rule_ids"] == ["DEMO-2a"]
    judged = export_dataset(store, tmp_path / "judge.jsonl")
    assert judged["written"] >= 2
    assert all(json.loads(line)["changeset_id"] == changeset_id
               for line in (tmp_path / "judge.jsonl").read_text().splitlines())


def test_only_the_accepted_generator_attempt_inherits_the_decision(tmp_path):
    """A first reply the knowledge base rejects is repaired; only the repaired
    call is labelled with the staged change set's decision."""
    from test_kb_flow import _flow_runtime
    from test_kb_intake_gate import PAGE, _generator_then_judge
    from infermatrix_copilot.kb_service.runtime import collect_events, run_intake

    rt, lifecycle = _flow_runtime(tmp_path)
    store = TraceStore(tmp_path / "traces", environ={})
    answer, calls = _generator_then_judge(), []

    def reply(model, prompt):
        if model == rt.generator.model:
            calls.append(prompt)
            if len(calls) == 1:  # a bad first draft: an operation on a page that does not exist
                return json.dumps({"operations": [{"kind": "edit_same_meaning", "page": PAGE,
                                                   "rule_id": "NOPE-1a", "section_markdown": "x"}],
                                   "rationale": "r"})
        return json.dumps(answer(SimpleNamespace(name="generator" if model == rt.generator.model else "judge"),
                                 prompt))
    rt.traces = store
    rt.gateway = ModelGateway(None, recorder=trace_recorder(store),
                              transport_factory=lambda provider: Transport(reply))
    collect_events(rt, lifecycle)
    changeset_id = run_intake(rt, lifecycle)
    assert changeset_id and len(calls) == 2
    export_dataset(store, tmp_path / "gen.jsonl", role="generator")
    rows = [json.loads(line) for line in (tmp_path / "gen.jsonl").read_text().splitlines()]
    labelled = {row["id"]: row["changeset_id"] for row in rows}
    first, second = store.query(kind="model_call", role="generator")
    assert labelled == {first["id"]: "", second["id"]: changeset_id}


def test_the_sdk_exports_the_trace_store_without_private_modules():
    code = ("import sys; from infermatrix_copilot.sdk.v1 import TraceStore, TRACE_SCHEMA, trace_context, redact;"
            "bad = {'infermatrix_copilot.config', 'infermatrix_copilot.contract', 'infermatrix_copilot.mcp_server'};"
            "print(sorted(bad & set(sys.modules)), TRACE_SCHEMA)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"}).stdout.strip()
    assert out == "[] trace/1"


def test_the_knowledge_service_records_decisions_and_outcomes(tmp_path):
    from test_kb_flow import _flow_runtime, _open_pr
    from infermatrix_copilot.kb_service import merge

    rt, lifecycle = _flow_runtime(tmp_path)
    rt.traces = TraceStore(tmp_path / "traces", environ={})
    changeset_id, _publisher = _open_pr(tmp_path, rt, lifecycle)
    (decision,) = rt.traces.query(kind="decision")
    assert decision["context"]["changeset_id"] == changeset_id
    assert decision["context"]["playbook"] == "kb-intake" and decision["context"]["repo"] == lifecycle.repo
    assert decision["result"]["status"] == "pass" and decision["result"]["blocks"]
    assert decision["context"]["rule_ids"]
    rt.ledger.update_changeset(changeset_id, status="pr_open", pr_number=42, head_sha="d" * 40)
    rt.github.prs[42] = {"merged": True, "merge_commit_sha": "e" * 40, "state": "closed"}
    merge.advance(rt, lifecycle)
    (outcome,) = rt.traces.query(kind="outcome", changeset_id=changeset_id)
    assert outcome["result"] == {"outcome": "merged", "merge_sha": "e" * 40}
    assert outcome["context"]["pr"] == 42
