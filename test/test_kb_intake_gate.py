"""Knowledge service intake, gate, publish and calibration, offline."""

from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service.calibration import load_cases, run_calibration
from infermatrix_copilot.kb_service.config import RepoLifecycle
from infermatrix_copilot.kb_service.gate import changes_between, run_gate
from infermatrix_copilot.kb_service.intake import draft_changes, draft_prompt
from infermatrix_copilot.kb_service.ledger import Ledger
from infermatrix_copilot.kb_service.models import (
    ModelGateway, ModelReply, ModelRole, ModelUnavailable, parse_json_object, roles_from_env,
)
from infermatrix_copilot.kb_service.runtime import KbRuntime, collect_events, publish, run_intake
from infermatrix_copilot.kb_service.sources import GitHubReader, KnowledgeRepo
from infermatrix_copilot.knowledge_service.lifecycle import Page
from infermatrix_copilot.knowledge_service.ops import KnowledgeOperation as Op, apply_operations

ROOT = Path(__file__).resolve().parents[1]
GEN = ModelRole("generator", "claude-code", "claude-opus-5-5")
JUDGE = ModelRole("judge", "codex", "gpt-6-sol", "medium")
PAGE = "repos/demo/core/rules.md"


def _rule(rule_id: str, cite: str = "PR #10", claim: str = "队列满时拒绝新请求并返回明确错误") -> str:
    return (f"## {rule_id} — keep the demo queue bounded\n\n"
            "- 触发：修改 demo queue 的容量或背压逻辑时。\n"
            f"- 强制：{claim}，不能无界增长。\n"
            f"- 验收：测试覆盖满队列拒绝路径。 ^[{cite}]\n")


def _tree() -> dict[str, str]:
    return {
        "repos/demo/_index.md": "# demo\n",
        "repos/demo/core/_index.md": "# core\n\n- [rules](rules.md)\n",
        "repos/demo/_routes.yaml": (
            "schema_version: 1\nowners:\n  - owner: core\n    path: repos/demo/core/rules.md\n"
            "    signals: [queue]\n    scope_prefixes: [demo/core/]\n"),
        PAGE: ("---\ntitle: \"Demo core rules\"\ncreated: 2026-09-01\nupdated: 2026-09-01\n"
               "type: rule\ntags: [demo]\nsources: [\"PR #10\"]\n---\n\n# Demo core rules\n\n"
               + _rule("DEMO-1a")),
    }


class ScriptedGateway:
    """call_json answered by a function of (role, prompt)."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def call_json(self, role, *, system, prompt, validate=None):
        self.calls.append((role.name, prompt))
        data = self.answer(role, prompt)
        if isinstance(data, Exception):
            raise data
        if validate is not None:
            try:
                validate(data)
            except (KeyError, TypeError, ValueError) as exc:
                raise ModelUnavailable(f"{role.label()} reply failed its schema: {exc!r}") from exc
        return ModelReply(role, data, json.dumps(data), role.model, {}, 0.1)


def _judge_all(value: str, consistency: str = "consistent"):
    def answer(role, prompt):
        if role.name == "generator":
            raise AssertionError("generator not expected")
        if '"directory"' in prompt:
            return {"verdict": consistency, "conflicts": []}
        dims = json.loads(prompt.split("<untrusted_data>\n", 1)[1].rsplit("\n</untrusted_data>", 1)[0])
        return {"dimensions": {d: value for d in dims["dimensions_to_answer"]}, "reasons": {}}
    return answer


# -- models ------------------------------------------------------------------------

def test_default_roles_are_pinned_and_cross_family(monkeypatch):
    monkeypatch.delenv("KB_GENERATOR", raising=False)
    monkeypatch.delenv("KB_JUDGE", raising=False)
    generator, judge = roles_from_env()
    assert generator.label() == "claude-code:claude-opus-5-5"
    assert judge.label() == "codex:gpt-6-sol:medium"
    monkeypatch.setenv("KB_JUDGE", "claude-code:claude-sonnet-5")
    with pytest.raises(ValueError, match="different model family"):
        roles_from_env()


def test_parse_json_object_accepts_raw_newlines_inside_strings():
    """Regression (kb init pilot, 2026-09-30): the generator wrote an ASCII
    diagram with literal newlines inside a JSON string; strict parsing
    rejected a well-formed answer and the stage stopped."""
    fenced = '```json\n{"architecture_md": "A\n  -> B\n\tC", "n": 1}\n```'
    assert parse_json_object(fenced) == {"architecture_md": "A\n  -> B\n\tC", "n": 1}
    assert parse_json_object('{"a": "x\ny"}') == {"a": "x\ny"}
    with pytest.raises(ModelUnavailable):
        parse_json_object('{"a": "unterminated\n')


def test_parse_json_object_and_unavailable():
    assert parse_json_object('text\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ModelUnavailable):
        parse_json_object("not json")


def test_codex_transport_pins_reasoning_effort(monkeypatch):
    from infermatrix_copilot.providers import codex

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    transport = codex.CodexTransport.__new__(codex.CodexTransport)
    transport.runner = fake_run
    transport.settings = SimpleNamespace(strict_backend_model="", strict_backend_timeout_s=5)
    monkeypatch.setattr(transport, "require_cli", lambda: "codex", raising=False)
    transport._run("hi", cwd="/tmp", timeout_s=5, model="gpt-6-sol", effort="medium")
    assert ["-m", "gpt-6-sol"] == captured["cmd"][captured["cmd"].index("-m"):captured["cmd"].index("-m") + 2]
    assert 'model_reasoning_effort="medium"' in captured["cmd"]
    with pytest.raises(ValueError):
        transport._run("hi", cwd="/tmp", timeout_s=5, effort="ludicrous")


def test_gateway_records_every_call_and_fails_closed():
    records = []

    class Transport:
        def complete(self, **kw):
            return SimpleNamespace(blocks=[], stop_reason="end_turn", usage={}, model="")

    gateway = ModelGateway(None, transport_factory=lambda p: Transport(), recorder=records.append)
    with pytest.raises(ModelUnavailable, match="returned nothing"):
        gateway.call_json(JUDGE, system="s", prompt="p")
    assert records and records[0]["requested"] == "codex:gpt-6-sol:medium"


GLM_JUDGE = ModelRole("judge", "zcode", "GLM-5.3")


def test_zcode_judge_backend_round_trips_and_is_recorded():
    """zcode:GLM-5.3 is a first-class judge backend: the label survives the
    gateway unchanged, the reply is parsed, and every call is recorded."""
    records = []

    class Transport:
        stops_at_spend = False

        def __init__(self):
            self.kwargs = []

        def complete(self, **kw):
            self.kwargs.append(kw)
            body = json.dumps({"dimensions": {"faithful": "yes"}, "reasons": {}})
            return SimpleNamespace(blocks=[SimpleNamespace(text=body)], stop_reason="end_turn",
                                   usage={}, model=kw.get("model", "GLM-5.3"))

    transport = Transport()
    gateway = ModelGateway(None, transport_factory=lambda p: transport, recorder=records.append)
    reply = gateway.call_json(GLM_JUDGE, system="s", prompt="p")
    assert reply.role.label() == "zcode:GLM-5.3" and reply.data["dimensions"]["faithful"] == "yes"
    assert transport.kwargs[0]["model"] == "GLM-5.3"
    assert records[0]["requested"] == "zcode:GLM-5.3"
    assert records[0]["provider"] == "zcode"


def test_gate_with_a_zcode_judge_uses_the_same_instrument():
    add = [Op("add", PAGE, "DEMO-2a", _rule("DEMO-2a", "PR #11"))]
    decision = _gate(add, _judge_all("yes"))
    assert decision.status == "pass"
    assert all(b.verdict == "pass" for b in decision.blocks)


# -- judge tuning surface -------------------------------------------------------------

def test_judge_tuning_is_the_single_source_the_gate_uses():
    """judge_tuning is the evolvable surface; gate.py must delegate to it and
    stay byte-identical to the pre-extraction baseline (evolution starts
    from the production behaviour, never from a silent rewording). The
    digests pin the shipped baseline — recorded when the module was
    extracted from gate.py (7aa922dd9^)."""
    import hashlib

    from infermatrix_copilot.kb_service import gate, judge_tuning

    assert gate.JUDGE_SYSTEM is judge_tuning.JUDGE_SYSTEM
    assert gate.CONSISTENCY_SYSTEM is judge_tuning.CONSISTENCY_SYSTEM
    assert gate.NEIGHBOUR_LIMIT is judge_tuning.NEIGHBOUR_LIMIT == 30

    baseline = {
        "JUDGE_SYSTEM": "2d120ade58b9ac32c0af4b0ad8854205f80fbfcad4e89ff2204b47028ec0781d",
        "CONSISTENCY_SYSTEM": "db4fd2396f0400f9d45311f0c1460d181d83f8b64489c142c398bd1eca3f0b97",
    }
    for name, expected in baseline.items():
        actual = hashlib.sha256(getattr(judge_tuning, name).encode()).hexdigest()
        assert actual == expected, (
            f"{name} drifted from the production baseline the evolution "
            "engine starts from; rewording must arrive as an engine candidate, "
            "not as part of another change")


def test_verdict_aggregation_keeps_the_fail_closed_ordering():
    from infermatrix_copilot.kb_service.judge_tuning import verdict_from_answers

    assert verdict_from_answers({"faithful": "yes", "actionable": "yes"}) == "pass"
    assert verdict_from_answers({"faithful": "no", "actionable": "yes"}) == "fail"
    assert verdict_from_answers({"faithful": "no", "actionable": "unsure"}) == "fail"
    assert verdict_from_answers({"faithful": "unsure", "actionable": "yes"}) == "human"
    assert verdict_from_answers({}) == "pass"  # the empty dimension set (no applicable L2)


def test_a_tuned_stricter_rubric_flows_through_the_gate(monkeypatch):
    """The delegation is real: editing judge_tuning changes gate behaviour —
    the hook evolution candidates use (a stricter aggregation fails blocks
    the baseline sent to people). Candidates edit the judge_tuning source in
    the arm tree; here the same edit is simulated on the imported symbol."""
    from infermatrix_copilot.kb_service import gate

    add = [Op("add", PAGE, "DEMO-2a", _rule("DEMO-2a", "PR #11"))]
    original = gate.verdict_from_answers

    def strict(answers):
        return "fail" if "unsure" in answers.values() else original(answers)

    monkeypatch.setattr(gate, "verdict_from_answers", strict)
    assert _gate(add, _judge_all("unsure")).status == "fail"
    monkeypatch.undo()
    assert _gate(add, _judge_all("unsure")).status == "human"


# -- drafting and gate --------------------------------------------------------------

def _draft_context(evidence, files=None, *, event_id=1):
    prompt = draft_prompt("demo", evidence, files or _tree(), "repos/demo", event_id=event_id)
    return json.loads(prompt.split("\n", 1)[1].split("\n\n<untrusted_data>", 1)[0])


def test_draft_sees_ids_on_unrelated_pages_and_permanent_reservations():
    files = {**_tree(), "repos/other/rules.md": _rule("OTHER-hidden"),
             "repos/demo/_tombstones.yaml": "schema_version: 1\nids:\n  - id: DEMO-purged\n"}
    context = _draft_context({"changed_files": ["demo/core/q.py"]}, files)
    assert [page["page"] for page in context["related_pages"]] == [PAGE]
    assert context["existing_rule_ids"] == ["DEMO-1a", "DEMO-purged", "OTHER-hidden"]


def test_independent_pr_drafts_get_distinct_namespaces_and_refinement_keeps_all_sources():
    first = _draft_context({"source_reference": "PR #6926"})
    second = _draft_context({"source_reference": "PR #7777"})
    assert first["new_rule_id_namespaces"] == ["DEMO-PR6926"]
    assert second["new_rule_id_namespaces"] == ["DEMO-PR7777"]
    refined = _draft_context({"evidence": [{"source_reference": "PR #7777"},
                                           {"source_reference": "PR #6926"}]}, event_id=0)
    assert refined["new_rule_id_namespaces"] == ["DEMO-PR6926", "DEMO-PR7777"]
    assert _draft_context({"source_reference": "run r1"}, event_id=42)["new_rule_id_namespaces"] == ["DEMO-E42"]


def test_draft_repairs_on_lifecycle_error():
    answers = iter([
        {"operations": [{"kind": "add", "page": PAGE, "rule_id": "DEMO-1a", "section_markdown": _rule("DEMO-1a")}]},
        {"operations": [{"kind": "add", "page": PAGE, "rule_id": "DEMO-2a", "section_markdown": _rule("DEMO-2a", "PR #11")}]},
    ])
    gateway = ScriptedGateway(lambda role, prompt: next(answers))
    draft = draft_changes(repo="demo", repo_dir="repos/demo", event_id=1,
                          evidence={"title": "t", "body": "b", "changed_files": ["demo/core/q.py"]},
                          files=_tree(), gateway=gateway, generator=GEN, release="v1", today="2026-09-28")
    assert [op.rule_id for op in draft.operations] == ["DEMO-2a"]
    assert "already exists" in draft.attempts[0]["error"]
    assert "rejected by the knowledge base" in gateway.calls[1][1]
    assert "<untrusted_data>" in gateway.calls[0][1]


def _gate(head_ops, answer, **kw):
    base = _tree()
    head = {**base, **apply_operations(base, head_ops, release="v1", today="2026-09-28").files}
    return run_gate(base=base, head=head, changes=changes_between(base, head), external_texts={},
                    evidence=[{"source_reference": "PR #11"}], gateway=ScriptedGateway(answer),
                    judge=JUDGE, release="v1", repo_dir="repos/demo", **kw)


def test_gate_pass_fail_human():
    add = [Op("add", PAGE, "DEMO-2a", _rule("DEMO-2a", "PR #11"))]
    assert _gate(add, _judge_all("yes")).status == "pass"
    assert _gate(add, _judge_all("no")).status == "fail"
    assert _gate(add, _judge_all("unsure")).status == "human"
    assert _gate(add, _judge_all("yes", "conflict")).status == "fail"
    assert _gate(add, _judge_all("yes", "unsure")).status == "human"
    assert _gate(add, lambda r, p: ModelUnavailable("down")).status == "human"
    assert _gate(add, _judge_all("yes"), protected_rules=("DEMO-2a",)).status == "human"


def test_gate_circuit_breaker_routes_to_humans():
    retire = [Op("retire", PAGE, "DEMO-1a", reason="incorrect", evidence="PR #12")]
    decision = _gate(retire, _judge_all("yes"), retire_ratio=0.5)
    assert decision.status == "human" and any("circuit breaker" in r for r in decision.reasons)


def test_gate_l1_failure_short_circuits_without_model_calls():
    base = _tree()
    head = dict(base)
    head[PAGE] = head[PAGE].replace("tags: [demo]", "tags: [other]")
    gateway = ScriptedGateway(_judge_all("yes"))
    decision = run_gate(base=base, head=head, changes=changes_between(base, head), external_texts={},
                        evidence=[], gateway=gateway, judge=JUDGE, release="v1", repo_dir="repos/demo")
    assert decision.status == "fail" and not gateway.calls


# -- end to end ----------------------------------------------------------------------

def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout


def _knowledge_remote(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    for rel, text in _tree().items():
        target = origin / "knowledge" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (origin / "knowledge" / "_format.yaml").write_text("format_version: 2\n", encoding="utf-8")
    (origin / "skills").mkdir()
    (origin / "skills" / "x.md").write_text("see DEMO-1a\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    _git(origin, "add", ".")
    _git(origin, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "init")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return clone


class FakeGitHub(GitHubReader):
    def __init__(self):
        super().__init__(fetch=self._answer, token="")

    def _answer(self, url):
        if "/search/issues" in url:
            return {"items": [{"number": 11, "pull_request": {"merged_at": "2026-09-28T01:00:00Z"}}]}
        if url.endswith("/pulls/11"):
            return {"number": 11, "title": "Bound the queue", "body": "adds backpressure",
                    "merged_at": "2026-09-28T01:00:00Z", "merge_commit_sha": "c" * 40, "user": {"login": "dev"}}
        if "/pulls/11/files" in url:
            return [{"filename": "demo/core/q.py", "patch": "+if full: raise QueueFull()"}]
        raise AssertionError(url)


def _runtime(tmp_path, gateway, *, mode="shadow", outbox=False):
    lifecycle = RepoLifecycle(repo="demo", full_name="org/demo", enabled=True, mode=mode,
                              knowledge_dir="repos/demo", calibration_set="x")
    ledger = Ledger(tmp_path / "state" / "kb.db")
    ledger.ensure_repo("demo", mode)
    box = None
    if outbox:
        from infermatrix_copilot.kb_service.outbox import Outbox
        from infermatrix_copilot.knowledge_service.signing import generate_private_key
        box = Outbox(tmp_path / "state", generate_private_key(tmp_path / "k.pem"), ledger,
                     clock=lambda: 1_790_000_000.0)  # same clock as the runtime
    rt = KbRuntime(state_dir=tmp_path / "state", ledger=ledger, registry={"demo": lifecycle},
                   gateway=gateway, generator=GEN, judge=JUDGE,
                   knowledge=KnowledgeRepo(_knowledge_remote(tmp_path)), github=FakeGitHub(),
                   outbox=box, clock=lambda: 1_790_000_000.0)
    return rt, lifecycle


def _generator_then_judge(judge_value="yes"):
    judge = _judge_all(judge_value)

    def answer(role, prompt):
        if role.name == "generator":
            return {"operations": [{"kind": "add", "page": PAGE, "rule_id": "DEMO-2a",
                                    "section_markdown": _rule("DEMO-2a", "PR #11")}], "rationale": "r"}
        return judge(role, prompt)
    return answer


def test_intake_end_to_end_shadow_records_and_publishes_nothing(tmp_path):
    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(_generator_then_judge()))
    assert collect_events(rt, lifecycle) == 1
    assert collect_events(rt, lifecycle) == 0  # idempotent
    changeset_id = run_intake(rt, lifecycle)
    changeset = rt.ledger.changeset(changeset_id)
    assert changeset["status"] == "gated"
    assert changeset["detail"]["decision"]["status"] == "pass"
    files = rt.load_changeset_files(changeset_id)["files"]
    assert "DEMO-2a" in files[PAGE]
    assert publish(rt, lifecycle, changeset_id) == "shadow_recorded"
    assert not (tmp_path / "state" / "outbox").exists()
    calls = (tmp_path / "state" / "traces" / "records")
    assert not calls.exists()  # scripted gateway bypasses the recorder; real one records


def _calibrate(tmp_path, rt, lifecycle, *, judge_label=None, passed=True):
    from dataclasses import replace

    from infermatrix_copilot.kb_service.calibration import case_set_digest
    from infermatrix_copilot.kb_service.runtime import record_calibration

    adapter = tmp_path / "adapter"
    (adapter / "cal" / "cases").mkdir(parents=True, exist_ok=True)
    (adapter / "cal" / "cases" / "c.json").write_text("{}", encoding="utf-8")
    lifecycle = replace(lifecycle, adapter_dir=adapter, calibration_set="cal")
    rt.registry["demo"] = lifecycle
    record_calibration(rt.ledger, "demo", judge=judge_label or rt.judge.label(),
                       case_set=case_set_digest(adapter / "cal"), passed=passed, at=1.0)
    return lifecycle


def test_auto_merge_requires_a_current_passing_calibration(tmp_path):
    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(_generator_then_judge()), mode="auto_merge", outbox=True)
    collect_events(rt, lifecycle)
    changeset_id = run_intake(rt, lifecycle)
    assert publish(rt, lifecycle, changeset_id) == "calibration_required"
    assert not list((tmp_path / "state" / "outbox").glob("*.json"))
    for index, kwargs in enumerate(({"judge_label": "codex:gpt-6-sol:low"}, {"passed": False})):
        sub = tmp_path / f"case{index}"
        sub.mkdir()
        rt2, lc2 = _runtime(sub, ScriptedGateway(_generator_then_judge()), mode="auto_merge", outbox=True)
        lc2 = _calibrate(sub, rt2, lc2, **kwargs)
        collect_events(rt2, lc2)
        assert publish(rt2, lc2, run_intake(rt2, lc2)) == "calibration_required"


def test_intake_auto_merge_writes_a_signed_open_pr_item(tmp_path):
    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(_generator_then_judge()), mode="auto_merge", outbox=True)
    lifecycle = _calibrate(tmp_path, rt, lifecycle)
    collect_events(rt, lifecycle)
    changeset_id = run_intake(rt, lifecycle)
    assert publish(rt, lifecycle, changeset_id) == "pr_requested"
    items = list((tmp_path / "state" / "outbox").glob("*.json"))
    envelope = json.loads(items[0].read_text(encoding="utf-8"))
    assert envelope["purpose"] == "kb-outbox-item" and envelope["payload"]["kind"] == "open_pr"
    assert envelope["payload"]["body"]["files"]["knowledge/" + PAGE].count("DEMO-2a")


def test_intake_waits_when_the_pinned_generator_is_unavailable(tmp_path):
    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(lambda r, p: ModelUnavailable("quota")))
    collect_events(rt, lifecycle)
    assert run_intake(rt, lifecycle) is None
    assert len(rt.ledger.events("demo", "pending")) == 1  # still queued, no fallback


def test_intake_human_path_enqueues(tmp_path):
    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(_generator_then_judge("unsure")))
    collect_events(rt, lifecycle)
    changeset_id = run_intake(rt, lifecycle)
    assert rt.ledger.changeset(changeset_id)["status"] == "human"
    assert rt.ledger.human_queue("demo")


@pytest.mark.parametrize('message', ['response lost', 'reply failed its schema: response lost'])
def test_unknown_generator_response_removes_event_from_automatic_redrafting(tmp_path, message, monkeypatch, capsys):
    def lost(role, prompt):
        error = ModelUnavailable(message, allow_fallback=False)
        error.native_call_sent = True
        error.native_call_completed = False
        raise error

    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(lost))
    collect_events(rt, lifecycle)
    assert run_intake(rt, lifecycle) is None
    assert len(rt.gateway.calls) == 1
    event, = rt.ledger.events('demo', 'draft_unknown')
    assert message in event['detail']
    assert not rt.ledger.events('demo', 'pending')
    assert run_intake(rt, lifecycle) is None
    assert len(rt.gateway.calls) == 1
    assert rt.ledger.changesets_of_kind('demo', 'intake') == []
    from infermatrix_copilot.kb_service import cli
    monkeypatch.setattr(cli, '_registry', lambda: {'demo': lifecycle})
    assert cli.main(['--state-dir', str(rt.state_dir), 'status']) == 0
    report = json.loads(capsys.readouterr().out)
    reported = next(row for row in report['repos'] if row['repo'] == 'demo')
    assert reported['unresolved_draft_events'] == [{'id': event['id'], 'detail': event['detail']}]


def test_intake_process_interruption_keeps_generation_intent_before_gate(tmp_path, monkeypatch, capsys):
    def interrupted(role, prompt):
        raise KeyboardInterrupt('worker interrupted')

    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(interrupted))
    collect_events(rt, lifecycle)
    with pytest.raises(KeyboardInterrupt):
        run_intake(rt, lifecycle)
    calls = list(rt.gateway.calls)
    state, = [json.loads(value) for value in rt.ledger.cursors_with_prefix('demo', 'draft:intake:').values()]
    assert state['draft_dispatched'] and 'draft_result' not in state
    assert run_intake(rt, lifecycle) is None
    assert rt.gateway.calls == calls
    assert rt.ledger.changesets_of_kind('demo', 'intake') == []
    from infermatrix_copilot.kb_service import cli
    monkeypatch.setattr(cli, '_registry', lambda: {'demo': lifecycle})
    before = rt.ledger.cursors_with_prefix('demo', 'draft:')
    assert cli.main(['--state-dir', str(rt.state_dir), 'status']) == 0
    report = json.loads(capsys.readouterr().out)
    reported = next(row for row in report['repos'] if row['repo'] == 'demo')
    assert reported['unresolved_draft_events'] == []  # hard interruption never settled the event
    checkpoint, = reported['unresolved_draft_checkpoints']
    assert checkpoint == {'cursor': next(iter(before)), 'status': 'outcome_unknown',
                          'input_sha256': state['draft_binding']['input_sha256'],
                          'base_sha': state['draft_binding']['base_sha'],
                          'generation': state['draft_binding']['generation'], 'attempts': 1}
    assert rt.ledger.cursors_with_prefix('demo', 'draft:') == before
    assert rt.gateway.calls == calls


@pytest.mark.parametrize('raw', ['', '{', 'null', '[]', '{"draft_dispatched": 0}',
                                '{"draft_dispatched": 1}', '{"draft_result": []}',
                                '{"draft_dispatched": false, "draft_result": {}}',
                                '{"draft_dispatched": true}'])
def test_status_preserves_corrupt_generation_checkpoints(tmp_path, raw, monkeypatch, capsys):
    from infermatrix_copilot.kb_service import cli
    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(_generator_then_judge()))
    rt.ledger.set_cursor('demo', 'draft:refine:original', raw)
    rt.ledger.set_cursor('demo', 'draft:sweep:completed', json.dumps({'draft_dispatched': False}))
    monkeypatch.setattr(cli, '_registry', lambda: {'demo': lifecycle})
    assert cli.main(['--state-dir', str(rt.state_dir), 'status']) == 0
    report = json.loads(capsys.readouterr().out)
    reported = next(row for row in report['repos'] if row['repo'] == 'demo')
    assert reported['unresolved_draft_checkpoints'] == [
        {'cursor': 'draft:refine:original', 'status': 'invalid_checkpoint'}]
    assert rt.ledger.get_cursor('demo', 'draft:refine:original') == raw
    assert rt.gateway.calls == []
    from infermatrix_copilot.kb_service.intake import checkpoint_draft
    with rt.ledger.lease() as owner, pytest.raises(ModelUnavailable):
        checkpoint_draft(rt.ledger, owner, unit_key='refine:original', base_sha='original',
                         accepted_trace=lambda: pytest.fail('trace requested'), repo='demo', generator=GEN,
                         generate=lambda: pytest.fail('generation dispatched'))
    assert rt.ledger.get_cursor('demo', 'draft:refine:original') == raw


def test_intake_completed_generation_resumes_before_gate_without_redrafting(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service import runtime
    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(_generator_then_judge()))
    collect_events(rt, lifecycle)
    gate = runtime.gate_and_stage

    def interrupted(*args, **kwargs):
        raise OSError('interrupted before gate reservation')

    monkeypatch.setattr(runtime, 'gate_and_stage', interrupted)
    with pytest.raises(OSError, match='before gate reservation'):
        run_intake(rt, lifecycle)
    calls = [call for call in rt.gateway.calls if call[0] == 'generator']
    assert len(calls) == 1
    monkeypatch.setattr(runtime, 'gate_and_stage', gate)
    assert run_intake(rt, lifecycle)
    assert [call for call in rt.gateway.calls if call[0] == 'generator'] == calls


@pytest.mark.parametrize('failure_at', ['judge', 'intent_reply', 'receipt', 'settlement'])
def test_intake_unknown_gate_keeps_original_draft_and_does_not_redraft(tmp_path, monkeypatch, failure_at):
    answer = _generator_then_judge()

    def respond(role, prompt):
        if failure_at == 'judge' and role.name == 'judge':
            error = ModelUnavailable('native response lost', allow_fallback=False)
            error.native_call_sent = True
            error.native_call_completed = False
            raise error
        return answer(role, prompt)

    gateway = ScriptedGateway(respond)
    rt, lifecycle = _runtime(tmp_path, gateway)
    stage = rt.ledger.stage_intake

    def interrupted(*args, **kwargs):
        if failure_at == 'settlement' and kwargs.get('expected_status'):
            raise OSError('settlement lost')
        result = stage(*args, **kwargs)
        if failure_at == 'intent_reply' and kwargs['status'] == 'review_pending':
            raise OSError('intent reply lost')
        return result

    monkeypatch.setattr(rt.ledger, 'stage_intake', interrupted)
    if failure_at == 'receipt':
        def unavailable_receipt(*args, **kwargs):
            raise OSError('receipt storage unavailable')
        monkeypatch.setattr('infermatrix_copilot.persistence.immutable_write_bytes', unavailable_receipt)
    collect_events(rt, lifecycle)
    with pytest.raises((ModelUnavailable, OSError)):
        run_intake(rt, lifecycle)
    pending, = rt.ledger.changesets('demo', ('review_pending',))
    assert rt.changeset_path(pending['id']).exists()
    assert not rt.ledger.events('demo', 'pending')
    assert len(rt.ledger.events('demo', 'drafted')) == 1
    receipt_path = rt.state_dir / 'gate-results' / f"{pending['id']}.json"
    if failure_at == 'settlement':
        receipt = json.loads(receipt_path.read_text())
        assert receipt['changeset_id'] == pending['id']
        assert receipt['generation'] == pending['generation']
        assert receipt['intent_detail'] == pending['detail']
        assert receipt['draft_sha256'] == hashlib.sha256(rt.changeset_path(pending['id']).read_bytes()).hexdigest()
        assert receipt['settlement']['detail']['decision']['status'] == 'pass'
        assert receipt['settlement']['status'] == 'gated'
        assert receipt['settlement']['verdicts']
        assert receipt_path.stat().st_mode & 0o777 == 0o600
    else:
        assert not receipt_path.exists()
    before = list(gateway.calls)
    assert run_intake(rt, lifecycle) is None
    assert gateway.calls == before


@pytest.mark.parametrize('lost_commit_reply', [False, True])
def test_completed_gate_receipt_matches_committed_decision(tmp_path, monkeypatch, lost_commit_reply):
    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(_generator_then_judge()))
    stage = rt.ledger.stage_intake

    def commit(*args, **kwargs):
        result = stage(*args, **kwargs)
        if lost_commit_reply and kwargs.get('expected_status'):
            raise OSError('committed settlement reply lost')
        return result

    monkeypatch.setattr(rt.ledger, 'stage_intake', commit)
    collect_events(rt, lifecycle)
    if lost_commit_reply:
        with pytest.raises(OSError, match='committed settlement reply lost'):
            run_intake(rt, lifecycle)
        committed, = rt.ledger.changesets('demo', ('gated',))
        changeset_id = committed['id']
        calls = list(rt.gateway.calls)
        assert run_intake(rt, lifecycle) is None
        assert rt.gateway.calls == calls
    else:
        changeset_id = run_intake(rt, lifecycle)
    row = rt.ledger.changeset(changeset_id)
    raw = (rt.state_dir / 'gate-results' / f'{changeset_id}.json').read_bytes()
    receipt = json.loads(raw)
    assert row['detail']['gate_result_sha256'] == hashlib.sha256(raw).hexdigest()
    assert row['detail']['decision'] == receipt['settlement']['detail']['decision']
    assert receipt['settlement']['drafted_events'] == receipt['intent_detail']['event_ids']
    assert receipt['repo'] == row['repo']
    assert receipt['kind'] == row['kind']


# -- calibration -----------------------------------------------------------------------

def test_shipped_calibration_set_is_well_formed():
    cases = load_cases(ROOT / "adapters" / "vllm_omni" / "kb-calibration")
    assert sum(c["expected"] == "pass" for c in cases) >= 8
    assert sum(c["expected"] == "reject" for c in cases) >= 10
    for case in cases:
        assert case["evidence"] and case["head"] != case["base"]


def test_calibration_scoring():
    directory = ROOT / "adapters" / "vllm_omni" / "kb-calibration"
    lenient = run_calibration(directory, gateway=ScriptedGateway(_judge_all("yes")), judge=JUDGE)
    assert not lenient.passed and lenient.bad_caught < lenient.bad_total

    def oracle(role, prompt):
        if '"directory"' in prompt:
            return {"verdict": "consistent", "conflicts": []}
        dims = json.loads(prompt.split("<untrusted_data>\n", 1)[1].rsplit("\n</untrusted_data>", 1)[0])
        text = json.dumps(dims["change"], ensure_ascii=False)
        if dims["change"]["op"] == "retire" and "reason=superseded" in dims["change"]["after"]:
            return {"dimensions": {d: "yes" for d in dims["dimensions_to_answer"]}, "reasons": {}}
        bad_markers = ("可选的", "按照 PR", "遵循 PR #8107", "回落到 runner 默认值", "enforce_runner_schema",
                       "无需断言", "run_benchmark.py` 只跑 `is_diffusion", "MMH3-4e", "retire")
        verdict = "no" if any(m in text for m in bad_markers) or dims["change"]["op"] == "retire" else "yes"
        return {"dimensions": {d: verdict for d in dims["dimensions_to_answer"]}, "reasons": {}}

    strict = run_calibration(directory, gateway=ScriptedGateway(oracle), judge=JUDGE)
    assert strict.passed, strict.to_dict()


@pytest.mark.parametrize("involves_new_rule", [False, True])
def test_calibration_preserves_reasons_and_attributes_consistency_like_the_gate(tmp_path, involves_new_rule):
    base = _tree()
    base.update(apply_operations(base, [Op("add", PAGE, "DEMO-old", _rule("DEMO-old"))],
                                 release="v1", today="2026-09-28").files)
    head = {**base, **apply_operations(base, [Op("add", PAGE, "DEMO-new", _rule("DEMO-new"))],
                                       release="v1", today="2026-09-28").files}
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "good.json").write_text(json.dumps({"id": "good", "expected": "pass", "base": base,
                                                 "head": head, "evidence": []}))

    def answer(role, prompt):
        payload = json.loads(prompt.split("<untrusted_data>\n", 1)[1].split("\n</untrusted_data>", 1)[0])
        if "directory" in payload:
            assert payload["changed_rule_ids"] == ["DEMO-new"]
            return {"verdict": "conflict", "conflicts": [
                ["DEMO-new" if involves_new_rule else "DEMO-old", "DEMO-1a", "overlap"]]}
        return {"dimensions": {d: "yes" for d in payload["dimensions_to_answer"]},
                "reasons": {"faithful": "supported by pinned source"}}

    report = run_calibration(tmp_path, gateway=ScriptedGateway(answer), judge=JUDGE)
    detail = report.details[0]
    assert detail["outcome"] == ("fail" if involves_new_rule else "pass")
    assert detail["blocks"][0]["reasons"] == {"faithful": "supported by pinned source"}
    assert bool(detail["consistency"][0]["preexisting"]) == (not involves_new_rule)


def test_calibration_cli_records_replayable_calls_as_calibration(tmp_path, monkeypatch, capsys):
    from infermatrix_copilot.kb_service import cli, models
    from infermatrix_copilot.trace_store import TraceStore

    class RecordedGateway(ScriptedGateway):
        def __init__(self, settings, *, recorder):
            super().__init__(_judge_all("yes"))
            self.recorder = recorder

        def call_json(self, role, *, system, prompt, validate=None):
            reply = super().call_json(role, system=system, prompt=prompt, validate=validate)
            self.recorder({"system": system, "prompt": prompt, "reply": reply.text,
                           "role": role.name, "model": role.model})
            return reply

    lifecycle = RepoLifecycle(repo="demo", full_name="org/demo", enabled=True, mode="shadow",
                              knowledge_dir="repos/demo", adapter_dir=ROOT / "adapters/vllm_omni",
                              calibration_set="kb-calibration")
    monkeypatch.setattr(cli, "_registry", lambda: {"demo": lifecycle})
    monkeypatch.setattr(models, "ModelGateway", RecordedGateway)
    monkeypatch.delenv("KB_SIGNING_KEY", raising=False)
    state = tmp_path / "state"
    assert cli.main(["--state-dir", str(state), "calibrate", "--repo", "demo"]) == 1
    capsys.readouterr()
    calls = TraceStore(state / "traces").query(kind="model_call", limit=100)
    assert calls
    assert all(c["context"]["playbook"] == "kb-calibrate" and c["context"]["repo"] == "demo"
               and c["context"]["item"] for c in calls)
    assert all(c["inputs"].get("prompt") and c["outputs"].get("reply") for c in calls)


def test_kb_intake_playbook_is_registered_and_candidate():
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.playbooks.store import PlaybookStore

    registry = register_builtin_steps(StepRegistry())
    store = PlaybookStore(ROOT / "playbooks", registry)
    playbook = store.get("kb-intake")
    assert playbook.status == "candidate"
    assert [s.step for s in playbook.steps] == ["knowledge.collect_events", "knowledge.intake", "knowledge.publish"]
    assert store.find("knowledge_maintenance") is None  # never planner-visible


def test_drafts_may_not_write_to_another_repository():
    op = {"kind": "replace", "page": PAGE, "rule_id": "DEMO-1a", "new_rule_id": "OTHER-1a",
          "new_page": "repos/other/core/rules.md", "evidence": "PR #11",
          "section_markdown": _rule("OTHER-1a", "PR #11")}
    gateway = ScriptedGateway(lambda role, prompt: {"operations": [op]})
    draft = draft_changes(repo="demo", repo_dir="repos/demo", event_id=1, evidence={"title": "t"},
                          files=_tree(), gateway=gateway, generator=GEN, release="v1", today="2026-09-28")
    assert draft.rejected and draft.empty
    assert "repos/other/core/rules.md is outside repos/demo/" in gateway.calls[1][1]


def test_conflicting_drafts_stay_pending_and_rejections_are_not_no_rules(tmp_path):
    rt, lifecycle = _runtime(tmp_path, None)
    for n in (1, 2, 3):
        rt.ledger.record_event("demo", "merged_pr", str(n), {"title": f"t{n}", "changed_files": []})

    def answer(role, prompt):
        if role.name == "generator":
            if '"title": "t3"' in prompt:
                return {"operations": [{"kind": "add", "page": "repos/elsewhere/rules.md", "rule_id": "X-1a",
                                        "section_markdown": _rule("X-1a", "PR #3")}]}
            return {"operations": [{"kind": "add", "page": PAGE, "rule_id": "DEMO-2a",
                                    "section_markdown": _rule("DEMO-2a", "PR #1")}]}
        return _judge_all("yes")(role, prompt)

    rt.gateway = ScriptedGateway(answer)
    run_intake(rt, lifecycle)
    status = {e["external_id"]: e["status"] for e in
              rt.ledger.events("demo", "pending") + rt.ledger.events("demo", "drafted")
              + rt.ledger.events("demo", "rejected") + rt.ledger.events("demo", "done")}
    assert status == {"1": "drafted", "2": "pending", "3": "rejected"}
    event, = rt.ledger.events('demo', 'pending')
    assert json.loads(event['detail'])['draft_attempt'] == 1
    calls = len([call for call in rt.gateway.calls if call[0] == 'generator'])
    assert run_intake(rt, lifecycle)
    assert len([call for call in rt.gateway.calls if call[0] == 'generator']) == calls + 1
    assert rt.ledger.event(event['id'])['status'] == 'drafted'


def test_merged_pr_discovery_paginates_in_merge_order(tmp_path):
    pages = {
        1: [{"number": 100 + i, "pull_request": {"merged_at": f"2026-09-28T02:{i % 60:02d}:00Z"}} for i in range(100)],
        2: [{"number": 7, "pull_request": {"merged_at": "2026-09-28T00:30:00Z"}}],
    }

    def fetch(url):
        from urllib.parse import parse_qs, urlparse
        page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        return {"items": pages.get(page, [])}

    reader = GitHubReader(fetch=fetch, token="")
    found = reader.merged_prs_since("org/demo", "2026-09-28T00:00:00Z", limit=5)
    assert found[0] == (7, "2026-09-28T00:30:00Z")  # the older merge on page 2 is not skipped
    assert [n for n, _ in found] == [7, 100, 160, 101, 161]  # equal merge times order by number


def test_tied_merge_times_do_not_stall_repeated_collection(tmp_path):
    items = [{"number": n, "pull_request": {"merged_at": "2026-09-28T03:00:00Z"}} for n in range(1, 61)]

    class Tied(FakeGitHub):
        def _answer(self, url):
            if "/search/issues" in url:
                return {"items": items}
            number = int(url.rstrip("/").split("/pulls/")[1].split("/")[0].split("?")[0])
            if "/files" in url:
                return []
            return {"number": number, "title": f"pr {number}", "body": "", "merged_at": "2026-09-28T03:00:00Z",
                    "merge_commit_sha": "c" * 40, "user": {"login": "dev"}}

    rt, lifecycle = _runtime(tmp_path, None)
    rt.github = Tied()
    first = collect_events(rt, lifecycle)
    second = collect_events(rt, lifecycle)
    assert (first, second) == (50, 10)
    assert collect_events(rt, lifecycle) == 0


@pytest.mark.parametrize("reply", [
    {"dimensions": None},
    {"dimensions": {"faithful": "yes", "non_contradictory": "yes", "actionable": 1}},
    {"dimensions": [], "reasons": {}},
    {"dimensions": {"faithful": "yes", "non_contradictory": "yes", "actionable": "yes"}, "reasons": "x"},
])
def test_malformed_judge_replies_become_human_decisions(reply):
    gateway = ModelGateway(None, transport_factory=lambda p: SimpleNamespace(
        complete=lambda **kw: SimpleNamespace(blocks=[SimpleNamespace(text=json.dumps(reply))],
                                              stop_reason="end_turn", usage={}, model="m")))
    base = _tree()
    head = {**base, **apply_operations(base, [Op("add", PAGE, "DEMO-2a", _rule("DEMO-2a", "PR #11"))],
                                       release="v1", today="2026-09-28").files}
    decision = run_gate(base=base, head=head, changes=changes_between(base, head), external_texts={},
                        evidence=[], gateway=gateway, judge=JUDGE, release="v1", repo_dir="repos/demo")
    assert decision.status == "human"


@pytest.mark.parametrize("operation", [
    {"kind": "add"},
    {"kind": "add", "page": PAGE, "rule_id": "DEMO-2a", "surprise": 1},
    {"kind": "add", "page": PAGE, "rule_id": 5},
    {"kind": "add", "page": PAGE, "rule_id": "DEMO-2a", "section_markdown": ["x"]},
])
def test_malformed_generator_replies_are_repaired_or_rejected(operation):
    replies = iter([{"operations": [operation]}] * 3)
    gateway = ModelGateway(None, transport_factory=lambda p: SimpleNamespace(
        complete=lambda **kw: SimpleNamespace(blocks=[SimpleNamespace(text=json.dumps(next(replies)))],
                                              stop_reason="end_turn", usage={}, model="m")))
    draft = draft_changes(repo="demo", repo_dir="repos/demo", event_id=1, evidence={"title": "t"},
                          files=_tree(), gateway=gateway, generator=GEN, release="v1", today="2026-09-28")
    assert draft.rejected and len(draft.attempts) == 3  # repaired twice, then rejected (not retried forever)
    assert all("failed its schema" in a["error"] for a in draft.attempts)


def test_overlapping_intake_backs_off_while_another_process_holds_the_lease(tmp_path):
    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(_generator_then_judge()))
    collect_events(rt, lifecycle)
    other = Ledger(tmp_path / "state" / "kb.db")
    holder = other.acquire_lease("other-process", ttl=600)
    assert run_intake(rt, lifecycle) is None
    assert len(rt.ledger.events("demo", "pending")) == 1  # untouched
    other.release_lease(holder)
    assert run_intake(rt, lifecycle) is not None


def test_kb_run_uses_the_cli_state_dir(tmp_path, monkeypatch):
    import os

    from infermatrix_copilot.cli.entry import main as cli_main
    from infermatrix_copilot.engine.steps import knowledge as knowledge_steps
    from infermatrix_copilot.kb_service import runtime as runtime_module

    seen = {}

    def fake_from_env(settings, *, state_dir=None):
        seen["state_dir"] = str(state_dir)
        raise RuntimeError("stop after resolving the runtime")

    monkeypatch.setattr(runtime_module.KbRuntime, "from_env", staticmethod(fake_from_env))
    monkeypatch.setenv("KB_STATE_DIR", str(tmp_path / "wrong"))
    monkeypatch.setenv("ADAPTERS_DIR", str(ROOT / "adapters"))
    chosen = tmp_path / "chosen"
    # the executor turns the step's exception into a blocked run (exit 1)
    assert cli_main(["kb", "--state-dir", str(chosen), "run", "--playbook", "kb-intake",
                     "--repo", "vllm-omni"]) == 1
    assert seen == {"state_dir": str(chosen)}
    assert os.environ["KB_STATE_DIR"] == str(tmp_path / "wrong")  # env untouched

def test_lease_lost_during_judging_preserves_original_pending_intent(tmp_path):
    """Deterministic takeover: while the judge is answering, the lease expires
    and another process takes it. The original worker must stage nothing."""
    base_answer = _generator_then_judge()
    state = {"taken": False}
    db = tmp_path / "state" / "kb.db"

    def answer(role, prompt):
        if role.name == "judge" and not state["taken"]:
            future = Ledger(db, clock=lambda: 1e12)  # far past any TTL
            future.acquire_lease("usurper", ttl=600)
            state["taken"] = True
        return base_answer(role, prompt)

    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(answer))
    collect_events(rt, lifecycle)
    assert run_intake(rt, lifecycle) is None
    assert state["taken"]
    assert rt.ledger.changesets("demo", ("gated", "failed", "human")) == []
    assert not rt.ledger.events("demo", "pending")  # cannot redraft an already dispatched review
    pending, = rt.ledger.changesets("demo", ("review_pending",))
    assert rt.changeset_path(pending['id']).exists()
    assert rt.ledger.events("demo", "drafted")[0]['detail'] == pending['id']


def test_lease_keeper_renews_during_long_work(tmp_path):
    import time as _time

    ledger = Ledger(tmp_path / "kb.db")
    with ledger.lease(ttl=1.0, renew_every=0.2) as owner:
        _time.sleep(1.5)  # longer than the TTL; the keeper must have renewed it
        with ledger.fenced(owner):
            pass


def test_late_generator_failure_cannot_reset_an_event_staged_by_the_new_holder(tmp_path):
    db = tmp_path / "state" / "kb.db"

    def answer(role, prompt):
        if role.name == "generator":
            # meanwhile: the lease expires, another worker takes it and stages the event
            usurper = Ledger(db, clock=lambda: 1e12)
            owner = usurper.acquire_lease("usurper", ttl=600)
            event_id = usurper.events("demo", "pending")[0]["id"]
            usurper.stage_intake(owner, "demo", "demo-intake-usurped", detail={}, status="gated",
                                 verdicts=[], human_reason="", drafted_events=[event_id])
            return ModelUnavailable("quota")  # ... and only now does the original call fail
        raise AssertionError("judge not reached")

    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(answer))
    collect_events(rt, lifecycle)
    assert run_intake(rt, lifecycle) is None
    drafted = rt.ledger.events("demo", "drafted")
    assert len(drafted) == 1 and drafted[0]["detail"] == "demo-intake-usurped"


def _conflicting(pairs, seen=None):
    def answer(role, prompt):
        if '"directory"' in prompt:
            if seen is not None:
                seen.append(prompt)
            return {"verdict": "conflict", "conflicts": [[a, b, "they disagree"] for a, b in pairs]}
        return _judge_all("yes")(role, prompt)
    return answer


ADD = [Op("add", PAGE, "DEMO-2a", _rule("DEMO-2a", "PR #11"))]


def test_a_conflict_already_on_main_does_not_fail_the_change():
    decision = _gate(ADD, _conflicting([("DEMO-1a", "OLD-9z")]))        # neither rule is the change's
    assert decision.status == "pass", decision.reasons
    (item,) = decision.consistency
    assert item["verdict"] == "consistent" and item["conflicts"] == []
    assert item["preexisting"] == [["DEMO-1a", "OLD-9z", "they disagree"]]
    decision = _gate(ADD, _conflicting([("DEMO-1a", "OLD-9z"), ("DEMO-2a", "DEMO-1a")]))
    assert decision.status == "fail"                                    # the new rule is in it: ours
    assert decision.consistency[0]["conflicts"] == [["DEMO-2a", "DEMO-1a", "they disagree"]]
    assert _gate(ADD, _conflicting([("", "")])).status == "fail"        # names no rule: counted against it
    assert _gate(ADD, _conflicting([(None, None)])).status == "fail"    # null IDs: likewise


def test_a_rule_nested_in_a_changed_rule_is_part_of_the_change():
    text = _rule("DEMO-2a", "PR #11") + "\n### DEMO-2a1 — a nested case\n\n- 强制：嵌套规则。 ^[PR #11]\n"
    nested = [Op("add", PAGE, "DEMO-2a", text)]
    assert _gate(nested, _conflicting([("DEMO-2a1", "DEMO-1a")])).status == "fail"


def test_the_consistency_judge_is_told_which_rules_changed():
    seen: list[str] = []
    _gate(ADD, _conflicting([], seen))
    data = json.loads(seen[0].split("<untrusted_data>\n", 1)[1].rsplit("\n</untrusted_data>", 1)[0])
    assert data["changed_rule_ids"] == ["DEMO-2a"]
