"""Candidate reuse preserves repair prompts, scopes and accepted-call identity."""

from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service.intake import draft_changes, draft_operations, draft_prompt
from infermatrix_copilot.kb_service.models import ModelRole, ModelUnavailable
from infermatrix_copilot.kb_service.sweep import sweep_page
from infermatrix_copilot.knowledge_service.lifecycle import LifecycleError
from infermatrix_copilot.knowledge_service.ops import model_operations


class Gateway:
    def __init__(self, replies):
        self.replies, self.calls = iter(replies), []

    def call_json(self, role, **kwargs):
        self.calls.append(kwargs)
        data = next(self.replies)
        if isinstance(data, Exception):
            raise data
        if kwargs.get("validate"):
            kwargs["validate"](data)
        return SimpleNamespace(data=data, role=role)


ROLE = ModelRole("generator", "test", "model")


def test_intake_scope_repair_has_identical_prompt_and_no_false_accept(monkeypatch):
    accepted = []
    monkeypatch.setattr("infermatrix_copilot.trace_store.accept_attempt", accepted.append)
    gateway = Gateway([{"operations": [{"kind": "retire", "page": "repos/other/rules.md", "rule_id": "R"}]},
                       {"operations": [], "rationale": "nothing"}])
    result = draft_changes(repo="demo", repo_dir="repos/demo", event_id=4, evidence={}, files={},
                           gateway=gateway, generator=ROLE, release="v1", today="2026-10-08")
    feedback = ("\n\nYour previous answer was rejected: repos/other/rules.md is outside "
                "repos/demo/; every page and new_page must be in this repository.")
    assert gateway.calls[1]["prompt"] == draft_prompt("demo", {}, {}, "repos/demo", event_id=4) + feedback
    assert result.attempts == [{"attempt": 0, "error": feedback.strip()}]
    assert result.empty and not result.rejected and result.rationale == "nothing"
    assert accepted == []


def test_sweep_scope_repair_keeps_page_limit_and_prompt(monkeypatch):
    accepted = []
    monkeypatch.setattr("infermatrix_copilot.trace_store.accept_attempt", accepted.append)
    gateway = Gateway([{"operations": [{"kind": "retire", "page": "repos/other/rules.md", "rule_id": "R"}]},
                       {"operations": []}])
    rt = SimpleNamespace(gateway=gateway, generator=ROLE)
    page = "repos/demo/rules.md"
    generated = {}
    result = sweep_page(rt, None, page=page, files={page: "# Empty\n"}, diff="", hints=[],
                        sweep={"tag": "v1", "from_sha": "a", "to_sha": "b"}, release="v1",
                        today="2026-10-08", generated_by=generated)
    assert result is None and generated == {} and accepted == []
    assert gateway.calls[1]["prompt"] == gateway.calls[0]["prompt"] + f"\n\nEvery operation must target {page} only."


@pytest.mark.parametrize("lane", ["intake", "sweep"])
def test_application_repair_preserves_each_lanes_exact_prompt(lane):
    page = "repos/demo/rules.md"
    gateway = Gateway([{"operations": [{"kind": "retire", "page": page, "rule_id": "R"}]},
                       {"operations": []}])
    error = "retire reason must be upstream-removed, incorrect or duplicate; use replace for superseded"
    if lane == "intake":
        result = draft_changes(repo="demo", repo_dir="repos/demo", event_id=4, evidence={}, files={},
                               gateway=gateway, generator=ROLE, release="v1", today="2026-10-08")
        assert result.attempts == [{"attempt": 0, "error": error}] and result.empty
        repair = "Fix exactly that and answer again with the full JSON object."
    else:
        result = sweep_page(SimpleNamespace(gateway=gateway, generator=ROLE), None, page=page,
                            files={page: "# Empty\n"}, diff="", hints=[],
                            sweep={"tag": "v1", "from_sha": "a", "to_sha": "b"}, release="v1", today="2026-10-08")
        assert result is None
        repair = "Fix exactly that."
    assert gateway.calls[1]["prompt"] == gateway.calls[0]["prompt"] + (
        f"\n\nYour previous answer was rejected by the knowledge base: {error}. " + repair)


def test_candidate_loop_exhausts_exact_limit_and_preserves_schema_feedback():
    error = ModelUnavailable("reply failed its schema: invalid")
    gateway = Gateway([error, error, error])
    reply, result, attempts = draft_operations(gateway, ROLE, system="s", prompt="p", prepare=lambda _: None,
        schema_repair=lambda detail: f"repair: {detail}")
    assert reply is result is None
    assert [call["prompt"] for call in gateway.calls] == ["p", "prepair: " + str(error), "prepair: " + str(error)]
    assert attempts == [{"attempt": index, "error": str(error)} for index in range(3)]


@pytest.mark.parametrize('caller', ['candidate', 'intake', 'sweep'])
def test_unknown_native_outcome_is_not_repaired_even_if_error_mentions_schema(caller):
    error = ModelUnavailable('reply failed its schema: response stream interrupted')
    error.native_call_sent = True
    error.native_call_completed = False
    gateway = Gateway([error, error, error])
    with pytest.raises(ModelUnavailable) as caught:
        if caller == 'candidate':
            draft_operations(gateway, ROLE, system='s', prompt='p', prepare=lambda _: None,
                             schema_repair=lambda detail: 'repair: ' + detail)
        elif caller == 'intake':
            draft_changes(repo='demo', repo_dir='repos/demo', event_id=1, evidence={},
                          files={}, gateway=gateway, generator=ROLE, release='v1', today='2026-10-13')
        else:
            sweep_page(SimpleNamespace(gateway=gateway, generator=ROLE), None,
                       page='repos/demo/rules.md', files={'repos/demo/rules.md': '# Empty\n'},
                       diff='', hints=[], sweep={'tag': 'v1', 'from_sha': 'a', 'to_sha': 'b'},
                       release='v1', today='2026-10-13')
    assert caught.value is error
    assert len(gateway.calls) == 1


def test_correction_zero_repair_keeps_failure_and_untraced_call(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("zero-repair correction must retain its existing trace boundary")
    monkeypatch.setattr("infermatrix_copilot.trace_store.trace_context", forbidden)
    monkeypatch.setattr("infermatrix_copilot.trace_store.accept_attempt", forbidden)
    gateway = Gateway([ModelUnavailable("reply failed its schema")])
    with pytest.raises(ModelUnavailable, match="failed its schema"):
        draft_operations(gateway, ROLE, system="s", prompt="p", prepare=lambda _: None,
                         max_repairs=0, traced=False)
    assert gateway.calls == [{"system": "s", "prompt": "p"}]


def test_model_operations_preserves_unknown_protected_and_type_rejections():
    operation = {"kind": "retire", "page": "repos/demo/rules.md", "rule_id": "R"}
    with pytest.raises(ValueError, match="human path"):
        model_operations({"operations": [{**operation, "allow_protected": True}]}, kinds=("retire",))
    with pytest.raises(ValueError, match="at most 0"):
        model_operations({"operations": [operation]}, kinds=("retire",), limit=0)
    with pytest.raises(ValueError, match="new_page must be a string"):
        model_operations({"operations": [{**operation, "new_page": False}]}, kinds=("retire",), strings=True)
    with pytest.raises(LifecycleError, match="unknown operation fields"):
        model_operations({"operations": [{**operation, "unknown": "x"}]}, kinds=("retire",))
