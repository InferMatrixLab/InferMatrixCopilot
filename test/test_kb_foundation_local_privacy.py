"""Publication eligibility stays separate from immutable native approval."""
import copy
import hashlib
from types import SimpleNamespace

from infermatrix_copilot.kb_service import init_knowledge_parallel as parallel
from infermatrix_copilot.kb_service.init_support import InitRecord
from test_kb_foundation_context import _audit_stage
from test_kb_foundation_feedback import _stage_job, _task
from test_kb_foundation_parallel import _complete_native
from test_kb_init_knowledge import KnowledgeGateway
from test_kb_init_skeleton import world  # noqa: F401


def test_genuine_old_native_pass_is_locally_declined_without_mutating_proof(world, monkeypatch):
    original = KnowledgeGateway.call_json
    def historical(self, role, **kwargs):
        reply = original(self, role, **kwargs)
        if role.name == "generator":
            for section in reply.data.get("sections", []):
                if section["facet"] == "tradeoffs":
                    section["body"] += " Example wildcard host: `0.0.0.0`."
        return reply
    # Simulate genuine previously accepted native prose before local publication
    # checks; only the offline fixture's old tree/privacy gate is bypassed.
    with monkeypatch.context() as old:
        old.setattr(KnowledgeGateway, "call_json", historical)
        old.setattr(parallel, "local_privacy_reason", lambda text: "")
        old.setattr("infermatrix_copilot.kb_service.init_execution.run_knowledge_validators", lambda *a: [])
        rt, record, offline, traces = _complete_native(world)
    before = copy.deepcopy(record.coverage["foundation_jobs"])
    native_bytes = {p: p.read_bytes() for p in (rt.state_dir / "init" / "traces" / "records").glob("*.jsonl")}
    count = len(offline.calls)
    parallel.validate_checkpoint(_audit_stage(rt, record), record, record.inputs_digest)
    task = next(iter(before["tasks"].values()))
    applied = []
    stage = SimpleNamespace(rt=rt, record=InitRecord("knowledge", "toy"),
        _append_approved=lambda *args: applied.append(args[3]) or args[3])
    # The exact task already passed the unchanged full native/source replay above.
    monkeypatch.setattr(parallel, "_validate_result", lambda *a: None)
    parallel._apply(stage, task)
    assert "tradeoffs" not in applied and len(applied) == len(task["artifacts"]) - 1
    assert stage.record.verdicts["knowledge:" + task["owner"] + ":tradeoffs"]["verdict"] == "pass"
    assert stage.record.dropped[-1]["local_publication_exclusion"] is True
    assert "0.0.0.0" not in str(stage.record.dropped) + str(stage.record.unfinished)
    assert record.coverage["foundation_jobs"] == before and len(offline.calls) == count
    assert {p: p.read_bytes() for p in native_bytes} == native_bytes


def test_feedback_recomputes_only_matching_latest_artifact_local_problem():
    stage, job = _stage_job()
    prior = _task(job, verdict="pass")
    text = "Listen on 0.0.0.0 for wildcard IPv4 interfaces."
    artifact = {"key": "knowledge:sample:api", "owner": "sample", "page": job["page"],
                "facet": "api", "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    prior["artifacts"] = [artifact, {**artifact, "page": "other.md", "facet": "configuration"},
                          {**artifact, "owner": "other", "facet": "configuration"}]
    prior["result_sha256"] = parallel._hash({k: v for k, v in prior.items() if k != "result_sha256"})
    stage._foundation_validated_results = {prior["result_sha256"]}
    saved = {"binding": "batch", "tasks": {"initial": prior}}
    before = copy.deepcopy(saved)
    retry = list(parallel._with_review_feedback(stage, [job], saved))[0]
    facets = retry["payload"]["prior_review_feedback"]["facets"]
    assert facets["api"]["prior_artifact_text_sha256"] == artifact["text_sha256"]
    assert "local publication exclusion" in facets["api"]["last_local_publication_reasons"][0]
    assert facets["api"]["last_verdict"] == "unknown"  # Native pass never becomes a rejection.
    assert facets["api"]["prior_attempts"] == [{k: prior[k] for k in ("input_sha256", "result_sha256")}]
    assert "last_local_publication_reasons" not in facets["configuration"]
    assert "0.0.0.0" not in str(facets) and saved == before


def test_only_unsafe_items_are_excluded_and_loopback_or_finite_mode_are_preserved(monkeypatch):
    for unlimited, expected in ((True, ["api", "validation"]), (False, ["api", "configuration", "features", "validation"])):
        artifacts = [{"key": "knowledge:sample:" + facet, "owner": "sample", "page": "page.md",
            "facet": facet, "title": "Title", "text": text, "evidence": []}
            for facet, text in [("api", "127.0.0.1 remains an allowed loopback example"),
                                ("configuration", "wildcard host 0.0.0.0"),
                                ("features", "frontend wildcard host 0.0.0.0"), ("validation", "manual source checks")]]
        task = {"artifacts": artifacts, "verdicts": {}, "dropped": [], "unfinished": []}
        before = copy.deepcopy(task)
        applied = []
        stage = SimpleNamespace(rt=SimpleNamespace(unlimited_subscription=unlimited),
            record=InitRecord("knowledge", "toy"), _append_approved=lambda *a: applied.append(a[3]) or a[3])
        monkeypatch.setattr(parallel, "_validate_result", lambda *a: None)
        parallel._apply(stage, task)
        assert applied == expected and task == before
        assert len(stage.record.dropped) == (2 if unlimited else 0)
