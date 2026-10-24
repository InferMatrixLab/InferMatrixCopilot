"""Versioned generation guidance binds real workflows, not semantic verdicts."""
from copy import deepcopy
import hashlib

import pytest

from infermatrix_copilot.kb_service.init_knowledge_inputs import (
    FOUNDATION_GENERATION_POLICY_VERSION, SOURCE_EXPLANATION_POLICY, knowledge_system,
)
from infermatrix_copilot.kb_service.init_stages import run_stage
from infermatrix_copilot.kb_service.init_support import InitError, InitRecord
from infermatrix_copilot.kb_service.models import ModelUnavailable
from test_kb_init_knowledge import KnowledgeGateway, _chain
from test_kb_init_modules import _modules_lifecycle
from test_kb_init_skeleton import _runtime, world  # noqa: F401


LEGACY_HASHES = {
    3: "2b4ee659dc954d970c64138cc67687c17a9d345f18bad59866599db5eaad2558",
    4: "75df7a1352373467b5a2a06ff78cf9b2c5e09feaf96f9be581749041959dfef2",
    5: "b7875a3ee83bff8ac66d3b7bb9edeac6eac677ae2688d57f71b9560c2a70c0ed",
}


@pytest.mark.parametrize("version", [3, 4, 5])
def test_historical_system_bytes_remain_exact_and_new_policy_is_explicit(version):
    payload = {} if version == 3 else {"foundation_prompt_version": version}
    legacy = knowledge_system(payload)
    # Captured from sealed81fb wheel, not calculated from the current selector.
    assert hashlib.sha256(legacy.encode()).hexdigest() == LEGACY_HASHES[version]
    current = knowledge_system({**payload, "foundation_generation_policy_version": FOUNDATION_GENERATION_POLICY_VERSION})
    assert current == legacy + SOURCE_EXPLANATION_POLICY
    assert payload == ({} if version == 3 else {"foundation_prompt_version": version})


@pytest.mark.parametrize("version", [None, True, False, 0, 2, "1"])
def test_unknown_generation_policy_cannot_silently_use_historical_guidance(version):
    with pytest.raises(InitError, match="unsupported foundation generation policy"):
        knowledge_system({"foundation_generation_policy_version": version})


def test_new_generation_policy_cannot_reidentify_a_retained_unknown_job(world, monkeypatch):
    from infermatrix_copilot.kb_service import init_knowledge as workflow
    from infermatrix_copilot.kb_service import init_knowledge_parallel as parallel

    gateway = KnowledgeGateway(); _chain(world, gateway)
    runtime = _runtime(world, gateway)
    options, jobs = workflow._Knowledge._input_options, workflow.run_jobs
    dispatched = []

    def legacy_options(stage):
        value = options(stage)
        assert value.pop("foundation_generation_policy_version") == 1
        return value

    def legacy_jobs(stage, values):
        values = deepcopy(values)
        for job in values:
            job["payload"].pop("foundation_generation_policy_version")
        return jobs(stage, values)

    def lost_reply(stage, job):
        dispatched.append(job["owner"].owner)
        error = ModelUnavailable("simulated lost original model outcome", allow_fallback=False)
        error.native_call_sent = error.native_outcome_pending = True
        raise error

    with monkeypatch.context() as legacy:
        legacy.setattr(workflow._Knowledge, "_input_options", legacy_options)
        legacy.setattr(workflow, "run_jobs", legacy_jobs)
        legacy.setattr(parallel, "_worker", lost_reply)
        run_stage(runtime, _modules_lifecycle(), "knowledge", dry_run=True)
    saved = InitRecord.load(runtime.state_dir, "toy", "knowledge")
    state = saved.coverage["foundation_jobs"]
    assert state["inflight"] and dispatched
    assert state["requests"] and all("foundation_generation_policy_version" not in
        envelope["request"]["job"]["payload"] for envelope in state["requests"].values())
    path = InitRecord.path(runtime.state_dir, "toy", "knowledge")
    before, calls = path.read_bytes(), len(gateway.calls)
    monkeypatch.setattr(parallel, "_worker", lambda *_: pytest.fail("unknown job was redispatched"))
    with pytest.raises(InitError, match="frozen input identity differs|unresolved model call"):
        run_stage(runtime, _modules_lifecycle(), "knowledge", dry_run=True)
    assert path.read_bytes() == before and len(gateway.calls) == calls


def test_foundation_policy_does_not_change_depth_request_options(world):
    from infermatrix_copilot.kb_service.init_knowledge import _Knowledge
    from infermatrix_copilot.kb_service.init_knowledge_depth import _KnowledgeDepth
    runtime=_runtime(world,KnowledgeGateway())
    lifecycle=_modules_lifecycle()
    values=[]
    for cls in [_Knowledge,_KnowledgeDepth]:
        stage=cls(runtime,lifecycle,dry_run=True,pin=None)
        stage._base_sha=runtime.knowledge.fetch()
        stage.repo_dir="repos/toy";stage.overlay={}
        values.append(stage._input_options())
    assert values[0]["foundation_generation_policy_version"]==1
    assert "foundation_generation_policy_version" not in values[1]
