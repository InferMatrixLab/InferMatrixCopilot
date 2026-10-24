"""Required subscription foundation stays resumable before any outward write."""
import copy
import json
from types import SimpleNamespace

import pytest
import yaml

from infermatrix_copilot.kb_service.init_knowledge import _Knowledge
from infermatrix_copilot.kb_service.init_stages import _Stage, run_stage
from infermatrix_copilot.kb_service.init_support import InitRecord, load_prepared
from infermatrix_copilot.kb_service.knowledge_coverage import policy_path
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.llm import Block, Reply
from infermatrix_copilot.trace_store import TraceStore
from test_kb_init_knowledge import KnowledgeGateway, _chain
from test_kb_init_modules import _modules_lifecycle
from test_kb_init_skeleton import FakeGh, _commit, _runtime, _tree, world  # noqa: F401


@pytest.mark.parametrize("unlimited,dry_run,required,met,blocked", [
    (True, False, True, False, True),
    (True, False, True, None, True),
    (True, False, True, True, False),
    (True, True, True, False, False),
    (False, False, True, False, False),
    (True, False, False, False, False),
])
def test_gate_preserves_partial_legacy_and_preview_modes(
        tmp_path, monkeypatch, unlimited, dry_run, required, met, blocked):
    stage = _Knowledge.__new__(_Knowledge)
    stage.rt = SimpleNamespace(unlimited_subscription=unlimited, state_dir=tmp_path)
    stage.dry_run = dry_run
    stage.record = InitRecord("knowledge", "toy", coverage={"knowledge": {
        "targets": {"required": required, "met": met}}})
    calls = []
    monkeypatch.setattr(_Stage, "_prepare_publication", lambda self, changed: calls.append(changed) or self.record)
    record = stage._prepare_publication({"knowledge/repos/toy/feature.md": "approved"})
    assert bool(calls) is not blocked
    assert (record.status == "blocked") is blocked
    if blocked:
        assert record.pr == {} and "native progress retained" in record.problems[0]


def test_incomplete_native_publication_resumes_only_missing_facet(world):
    _chain(world, KnowledgeGateway())
    seed_rt = _runtime(world)
    baseline = {**_tree(InitRecord.load(seed_rt.state_dir, "toy", "skeleton")),
                **_tree(InitRecord.load(seed_rt.state_dir, "toy", "modules"))}
    feature_page = "repos/toy/components/tooling/feature-demo.md"
    policy = {"schema_version": 1, "required": True,
              "core": {"roots": ["pkg/", "tools/"], "target": 0.85},
              "features": [{"id": "demo", "title": "Demo", "owner": "tooling",
                            "source_globs": ["tools/lint/y.py"], "entry_points": ["tools/lint/y.py"],
                            "docs": ["docs/guide.md"], "page": feature_page}]}
    baseline[policy_path("toy")] = yaml.safe_dump(policy)
    _commit(world["origin"], baseline, "merge genuine prerequisites and required feature policy")
    state = world["tmp"] / "required-foundation"
    traces = TraceStore(state / "init" / "traces")
    calls = []
    reject_validation = True

    class NativeTransport:
        subscription_billing = True
        supports_native_events = True

        def complete(self, *, system, messages, model, effort, role, **kwargs):
            payload = json.loads(messages[0]["content"].split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
            calls.append((role, copy.deepcopy(payload)))
            if role == "generator":
                source = payload["files"][0]
                evidence = [{"path": source["path"], "start": source.get("start", 1), "end": source["end"]}]
                if payload["docs"]:
                    doc = payload["docs"][0]
                    evidence.append({"path": doc["path"], "start": 1, "end": doc["end"]})
                data = {"title": payload["owner"] + " knowledge", "sections": [
                    {"facet": facet, "title": facet.capitalize(), "interpretation": "inference",
                     "body": "The component's small interface keeps responsibility explicit. "
                             "Consumers supply the required state and preserve the documented integration boundary.",
                     "evidence": evidence} for facet in payload["facets"]]}
            else:
                reject = (reject_validation and payload["change"]["page"] == feature_page
                          and "**Validation**" in payload["change"]["after"])
                data = {"dimensions": {"faithful": "no" if reject else "yes",
                                       "does_not_weaken": "yes", "non_contradictory": "yes"},
                        "reasons": {"faithful": "scripted missing validation support" if reject else "supported"}}
            return Reply(blocks=[Block(type="text", text=json.dumps(data))], model=model)

    gateway = ModelGateway(None, transport_factory=lambda _: NativeTransport(), recorder=trace_recorder(traces))
    gh = FakeGh()
    rt = _runtime(world, gateway, state_dir=state, gh_run=gh,
                  generator=ModelRole("generator", "zcode", "GLM-5.3"),
                  environ={"ALLOW_PUSH": "1", "ALLOW_POST": "1",
                           "KB_INIT_GIT_AUTHOR": "Tester <tester@example.com>",
                           "KB_KNOWLEDGE_CONCURRENCY": "2", "KB_KNOWLEDGE_START_INTERVAL_S": "0.001"})
    first = run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=False,
                      from_existing=True, unlimited_subscription=True)
    assert first.status == "blocked", first.problems
    assert not first.coverage["knowledge"]["targets"]["met"]
    assert first.pr == {} and gh.pushed == [] and gh.created == []
    assert not InitRecord.path(state, "toy", "knowledge").with_name("knowledge-publish.json").exists()
    tasks = json.loads(json.dumps(first.coverage["foundation_jobs"]["tasks"]))
    assert sum(len(task["artifacts"]) for task in tasks.values()) == 17
    assert all(a["generator_receipt"]["native_trace_id"] and a["judge_receipt"]["native_trace_id"]
               for task in tasks.values() for a in task["artifacts"])
    count = len(calls)
    reject_validation = False
    second = run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=False,
                       from_existing=True, unlimited_subscription=True)
    assert second.status == "published", second.problems
    assert second.inputs_digest == first.inputs_digest
    assert second.coverage["knowledge"]["targets"]["met"]
    assert second.coverage["knowledge"]["targets"]["features"]["covered"] == 1
    assert len(calls) == count + 2
    assert calls[count][0] == "generator" and calls[count][1]["owner"] == "feature-demo"
    assert calls[count][1]["facets"] == ["validation"] and calls[count + 1][0] == "judge"
    for key, task in tasks.items():
        assert second.coverage["foundation_jobs"]["tasks"][key] == task
    prepared = load_prepared(second.pr["prepared"])
    assert prepared["base_sha"] == first.kb_base_sha and len(gh.pushed) == 1 and len(gh.created) == 1
    count = len(calls)
    third = run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=False,
                      from_existing=True, unlimited_subscription=True)
    assert third.status == "published" and len(calls) == count and len(gh.pushed) == 1
