"""The installed-consumer SDK contract, exercised without either MCP server."""

from __future__ import annotations

import os
import pytest
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from infermatrix_copilot import __version__
from infermatrix_copilot.sdk import _resources as resource_module
from infermatrix_copilot.sdk.v1 import (
    KNOWLEDGE_API_VERSION,
    ChangedPath,
    CarriedFinding,
    FindingRecheck,
    DirectClient,
    DirectCompletionRequest,
    DirectReviewRequest,
    QualityReviewRequest,
    RepositoryRef,
    StrictReviewRequest,
    StrictRuntime,
)
from infermatrix_copilot.knowledge_service import common as knowledge_module

HEAD = "a" * 40


def _request(repo: str, changed: str) -> DirectReviewRequest:
    return DirectReviewRequest(
        review_id="attempt-1",
        repository=RepositoryRef(alias=repo),
        pr_number=7,
        expected_head_sha=HEAD,
        title="Fix runtime scheduling",
        body="Correct the affected runtime path.",
        changed_paths=(ChangedPath(changed, "modified"),),
    )


def test_direct_plan_is_typed_complete_and_path_free():
    client = DirectClient()
    plan = client.plan(
        _request("vllm-omni", "vllm_omni/core/sched/scheduler.py")
    )

    assert plan.protocol_version
    assert plan.review_context_id.startswith("sha256:")
    assert plan.resource_revision.startswith("sha256:")
    assert plan.knowledge_routes
    assert plan.mandatory_review_guides
    assert plan.first_review_checklist
    assert plan.navigation_policy["stop_after_routes"] is True
    assert plan.progress_update["github_comment"] is False
    assert plan.completion_gate["operation"] == "validate_direct_review"
    assert plan.navigation_policy["fallback_document_id"] == "AGENTS.md"
    assert "fallback_entry" not in plan.navigation_policy

    wire = plan.to_dict()
    for document in [
        wire["knowledge_entry"],
        *wire["mandatory_review_guides"],
        *wire["document_maps"],
        *(route["document"] for route in wire["knowledge_routes"]),
    ]:
        assert not Path(document["document_id"]).is_absolute()
        assert "InferMatrixCopilot" not in document["document_id"]
        assert document["sha256"].startswith("sha256:")


def test_second_adapter_routes_from_packaged_adapter_registry():
    plan = DirectClient().plan(
        _request("afd-plugin", "afd_plugin/compat/vllm.py")
    )

    assert plan.routing["status"] == "ready"
    assert plan.knowledge_routes[0].owner == "compatibility"
    assert plan.knowledge_routes[0].document.document_id.startswith(
        "repos/afd-plugin/"
    )


def test_related_depth_reaches_sdk_with_content_addressed_refs_and_read_budget():
    request = replace(_request("jiuwenswarm", "jiuwenswarm/common/model_catalog.py"),
                      title="Model platform configuration", body="models API")
    client = DirectClient()
    plan = client.plan(request)
    related = plan.related_knowledge
    assert related["status"] == "ready" and related["content_chars"] <= 6000
    models = next(d for d in related["documents"] if d["feature"] == "models")
    assert "configuration" in models["available_facets"]
    assert models["source_pins"] == ["f0a69728c96b5961d993449f1a901cbd2f4dac5b"]
    document = models["document"]
    assert document["document_id"] == models["path"]
    assert document["sha256"].startswith("sha256:")
    assert not Path(document["document_id"]).is_absolute()
    assert plan.execution_budget["knowledge_file_reads"] == (
        sum(route.read_required for route in plan.knowledge_routes)
        + len(plan.mandatory_review_guides)
        + len(plan.navigation_policy["related_document_read_paths"]))


def test_completion_is_bound_to_context_and_frozen_head():
    client = DirectClient()
    plan = client.plan(
        _request("vllm-omni", "vllm_omni/core/sched/scheduler.py")
    )
    accepted = client.validate(DirectCompletionRequest(
        review_context_id=plan.review_context_id,
        expected_head_sha=HEAD,
        evidence_head_sha=HEAD,
        subtraction_signal="none",
        existing_feedback_status="checked",
    ))
    wrong_head = client.validate(DirectCompletionRequest(
        review_context_id=plan.review_context_id,
        expected_head_sha=HEAD,
        evidence_head_sha="b" * 40,
        subtraction_signal="none",
        existing_feedback_status="checked",
    ))

    assert accepted.review_complete is True
    assert wrong_head.review_complete is False
    assert "evidence_head_sha must equal expected_head_sha" in wrong_head.missing


def test_completion_rejects_unknown_other_client_and_evicted_contexts():
    request = _request("vllm-omni", "vllm_omni/core/sched/scheduler.py")
    client = DirectClient(max_issued_contexts=1)
    first = client.plan(request)
    other = DirectClient().validate(DirectCompletionRequest(
        review_context_id=first.review_context_id,
        expected_head_sha=HEAD,
        evidence_head_sha=HEAD,
        subtraction_signal="none",
    ))
    client.plan(replace(request, review_id="attempt-2"))
    evicted = client.validate(DirectCompletionRequest(
        review_context_id=first.review_context_id,
        expected_head_sha=HEAD,
        evidence_head_sha=HEAD,
        subtraction_signal="none",
    ))

    assert other.review_complete is False
    assert evicted.review_complete is False
    assert any("not issued by this DirectClient" in item for item in other.missing)
    assert any("not issued by this DirectClient" in item for item in evicted.missing)


def test_completion_passes_feedback_evidence_to_provider_gate():
    client = DirectClient()
    plan = client.plan(
        _request("vllm-omni", "vllm_omni/core/sched/scheduler.py")
    )
    decision = client.validate(DirectCompletionRequest(
        review_context_id=plan.review_context_id,
        expected_head_sha=HEAD,
        evidence_head_sha=HEAD,
        subtraction_signal="none",
        existing_feedback_status="checked",
        finding_dispositions=({
            "anchor": "src/runtime.py:7",
            "disposition": "duplicate",
            "existing_thread": "thread-1",
        },),
    ))

    assert decision.review_complete is True
    assert decision.details["existing_feedback_status"] == "checked"
    assert decision.details["finding_dispositions"] == 1
    assert decision.details["duplicate_findings_suppressed"] == 1


def test_document_reader_is_bounded_and_content_addressed():
    client = DirectClient()
    page = client.read_document("AGENTS.md", max_bytes=32)

    assert page.document_id == "AGENTS.md"
    assert page.sha256.startswith("sha256:")
    assert 0 < len(page.content.encode("utf-8")) <= 34  # UTF-8 boundary replacement
    assert page.next_offset == 32


def test_capabilities_identify_package_protocols_and_resources():
    caps = DirectClient().capabilities()

    assert caps.sdk_api_version == "1.0.0"
    assert caps.distribution_version == __version__ == "0.2.0"
    assert caps.direct_api_version
    assert caps.strict_api_version
    assert caps.quality_api_version == "1.0.0"
    assert caps.knowledge_api_version == KNOWLEDGE_API_VERSION == "1.1.0"
    assert caps.supports_idempotent_strict_start is True
    assert caps.supports_quality_review is True
    assert caps.supports_knowledge_curation is True
    assert caps.resource_revision.startswith("sha256:")
    assert {"vllm-omni", "afd-plugin"} <= set(caps.supported_repositories)


def test_sdk_resources_ignore_ambient_directory_overrides(tmp_path, monkeypatch):
    fake_knowledge = tmp_path / "knowledge"
    fake_knowledge.mkdir()
    (fake_knowledge / "AGENTS.md").write_text("poisoned\n", encoding="utf-8")
    fake_adapters = tmp_path / "adapters" / "poison"
    fake_adapters.mkdir(parents=True)
    (fake_adapters / "manifest.yaml").write_text("poisoned: true\n", encoding="utf-8")
    monkeypatch.setenv("INFERMATRIX_KNOWLEDGE_DIR", str(fake_knowledge))
    monkeypatch.setenv("INFERMATRIX_ADAPTERS_DIR", str(fake_adapters.parent))

    knowledge = resource_module.knowledge_root()
    adapters = resource_module.adapters_root()

    assert knowledge != fake_knowledge.resolve()
    assert adapters != fake_adapters.parent.resolve()
    assert (knowledge / "AGENTS.md").read_text(encoding="utf-8") != "poisoned\n"


def test_capabilities_fail_closed_when_knowledge_apply_lock_is_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(knowledge_module, "fcntl", None)

    caps = DirectClient().capabilities()

    assert caps.knowledge_api_version == KNOWLEDGE_API_VERSION
    assert caps.supports_knowledge_curation is False


def test_public_import_does_not_load_server_config_or_legacy_contract():
    script = """
import sys
from infermatrix_copilot.sdk.v1 import DirectClient, StrictRuntime
blocked = {
    'infermatrix_copilot.config',
    'infermatrix_copilot.contract',
    'infermatrix_copilot.direct_routing',
    'infermatrix_copilot.mcp_server',
}
loaded = sorted(blocked.intersection(sys.modules))
assert not loaded, loaded
assert DirectClient and StrictRuntime
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    subprocess.run([sys.executable, "-c", script], check=True, env=env)


class _FakeStrictCore:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.created = True

    def capabilities(self):
        return {"max_strict_workers": 1, "supports_file_locking": True}

    def strict_readiness(self, repo, repo_path=""):
        return []

    def quality_readiness(self, repo, repo_path=""):
        return []

    def reserve_strict_review(self, payload):
        self.requests.append(payload)
        return "run-20260829-010101-abcdef", self.created

    def reserve_quality_review(self, payload):
        self.requests.append(payload)
        return "run-quality-20260831-abcdef", self.created

    def get_status(self, run_id):
        return {
            "run_id": run_id,
            "status": {"state": "running"},
            "progress": {"completed": {}},
        }

    def get_result(self, run_id, offset=0):
        return {
            "run_id": run_id,
            "state": "done",
            "report": "complete",
            "report_path": "/private/provider/run/RUN_REPORT.md",
            "next_offset": None,
            "result": {"verdict": "APPROVE"},
            "offset_seen": offset,
        }

    def get_quality_result(self, run_id):
        return {
            "run_id": run_id,
            "state": "done",
            "result": {
                "contract_version": "1.0.0",
                "reviewed_head_sha": HEAD,
                "verdict": "ready",
                "confidence": "high",
                "reasons": [],
            },
        }

    def close(self):
        return None


def test_strict_facade_preserves_idempotency_and_hides_private_paths():
    runtime = object.__new__(StrictRuntime)
    core = _FakeStrictCore()
    runtime._core = core
    request = StrictReviewRequest(
        repository=RepositoryRef("vllm-omni"),
        pr_number=7,
        expected_head_sha=HEAD,
        repo_path="/authorized/checkout",
        idempotency_key="attempt-1",
        review_depth="full",
    )

    first = runtime.reserve_review(request)
    core.created = False
    retry = runtime.start_review(request)
    status = runtime.get_status(first.run_id)
    result = runtime.get_result(first.run_id, offset=12)

    assert first.created is True and retry.created is False
    assert core.requests[0]["params"] == {"review_depth": "full", "carried_findings": []}
    assert "review_depth" not in core.requests[0]
    assert status.to_dict()["payload"]["progress"] == {"completed": {}}
    assert result.to_dict()["payload"]["result"] == {"verdict": "APPROVE"}
    assert result.payload["offset_seen"] == 12
    assert "report_path" not in result.payload
    assert runtime.capabilities().distribution_version == "0.2.0"


def test_strict_typed_result_and_decode_error():
    from infermatrix_copilot.sdk.v1 import ResultDecodeError

    runtime = object.__new__(StrictRuntime)
    core = _FakeStrictCore()
    runtime._core = core
    result = runtime.get_result("run-20260829-010101-abcdef")
    assert result.review.verdict == "APPROVE"
    assert result.review.findings == ()

    core.get_result = lambda *_args, **_kwargs: {"state": "done", "result": {"comments": "invalid"}}
    with pytest.raises(ResultDecodeError, match="comments"):
        runtime.get_result("run-20260829-010101-abcdef")


def test_strict_sdk_preserves_frozen_business_inputs_in_authorized_request():
    from infermatrix_copilot.app.request_policy import enforce_strict_review_policy, PolicyError

    runtime = object.__new__(StrictRuntime)
    runtime._core = _FakeStrictCore()
    context = {"pull": {"number": 7, "head_sha": HEAD, "title": "Frozen title"},
        "pr_review_context": [{"body": "Historical concern", "thread_ref": "comment:1"}],
        "ci_evidence": {"head_sha": HEAD, "read_status": "unavailable"}}
    request = StrictReviewRequest(RepositoryRef("vllm-omni"), 7, HEAD, "", "frozen", business_context=context)
    runtime.reserve_review(request)
    payload = runtime._core.requests[0]
    spec = enforce_strict_review_policy(payload, allowed_repos=["vllm-omni"])
    assert spec.params["business_context"] == context
    payload["params"]["business_context"]["ci_evidence"]["head_sha"] = "b" * 40
    with pytest.raises(PolicyError, match="another head"):
        enforce_strict_review_policy(payload, allowed_repos=["vllm-omni"])


def test_typed_strict_config_rejects_checkout_outside_root(tmp_path):
    from infermatrix_copilot.sdk.v1 import StrictRuntimeConfig, InvalidRequestError

    config = StrictRuntimeConfig(
        repository=RepositoryRef("demo", "owner/demo"),
        checkout_path=str(tmp_path.parent / "elsewhere"),
        allowed_root=str(tmp_path),
    )
    with pytest.raises(InvalidRequestError, match="outside the allowed root"):
        StrictRuntime(config=config)


def test_headless_strict_runtime_does_not_import_cli_or_mcp():
    script = """
import os
import sys
import tempfile
from pathlib import Path
from infermatrix_copilot.sdk.v1 import RepositoryRef, StrictRuntime, StrictRuntimeConfig
with tempfile.TemporaryDirectory() as root:
    Path(root, '.env').write_text('STRICT_BACKEND=cursor\\n')
    os.chdir(root)
    config = StrictRuntimeConfig(
        repository=RepositoryRef('demo', 'owner/demo'),
        checkout_path=root, allowed_root=root,
        backend='codex',
        run_root=str(Path(root) / 'runs'),
    )
    with StrictRuntime(config=config) as runtime:
        assert runtime.capabilities().sdk_api_version == '1.0.0'
        assert runtime._core.settings.strict_backend == 'codex'
        assert runtime._core.settings.model_config.get('env_file') is None
        assert not {'infermatrix_copilot.mcp_server',
                    'infermatrix_copilot.cli.copilot',
                    'infermatrix_copilot.cli.entry'} & sys.modules.keys()
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    subprocess.run([sys.executable, "-c", script], check=True, env=env)


def test_quality_facade_is_typed_idempotent_and_head_bound():
    runtime = object.__new__(StrictRuntime)
    core = _FakeStrictCore()
    runtime._core = core
    request = QualityReviewRequest(
        repository=RepositoryRef("vllm-omni"),
        pr_number=7,
        expected_head_sha=HEAD,
        repo_path="/authorized/checkout",
        idempotency_key="quality-attempt-1",
        deterministic_signals=("Q2: no test changes",),
    )

    first = runtime.reserve_quality_review(request)
    core.created = False
    retry = runtime.start_quality_review(request)
    result = runtime.get_quality_result(first.run_id)

    assert first.created is True and retry.created is False
    assert core.requests[0]["kind"] == "pr_quality"
    assert core.requests[0]["post"] is False
    assert core.requests[0]["expected_head_sha"] == HEAD
    assert core.requests[0]["params"] == {
        "deterministic_signals": ["Q2: no test changes"],
    }
    assert result.terminal and result.payload["result"]["verdict"] == "ready"


def test_strict_runtime_public_capabilities_smoke(tmp_path):
    with StrictRuntime(settings_overrides={
        "_env_file": None,
        "run_root": tmp_path / "runs",
    }) as runtime:
        caps = runtime.capabilities()

    assert caps.distribution_version == "0.2.0"
    assert caps.strict_api_version == "1.4.0"
    assert caps.knowledge_api_version == KNOWLEDGE_API_VERSION
    assert caps.supports_knowledge_curation is True


def _carried():
    return CarriedFinding("finding-7", "b" * 40, "blocker", "mod_a.py", "Missing guard")


@pytest.mark.parametrize("outcome", ["fixed", "still_affected", "unverified"])
def test_direct_rechecks_are_explicit_and_bound_to_issued_findings(outcome):
    client = DirectClient()
    base = _request("vllm-omni", "vllm_omni/core/sched/scheduler.py")
    plan = client.plan(replace(base, carried_findings=(_carried(),)))
    plain = client.plan(base)
    assert plain.review_context_id != plan.review_context_id
    assert plan.to_dict()["carried_findings"][0]["finding_id"] == "finding-7"
    request = DirectCompletionRequest(
        plan.review_context_id, HEAD, HEAD, "none", existing_feedback_status="checked",
        finding_rechecks=(FindingRecheck("finding-7", HEAD, outcome, "Read current guard"),),
    )
    assert client.validate(request).review_complete
    assert not client.validate(replace(request, finding_rechecks=())).review_complete
    assert not client.validate(replace(request, review_context_id=plain.review_context_id)).review_complete
    assert not client.validate(replace(request, finding_rechecks=request.finding_rechecks * 2)).review_complete
    wrong = replace(request.finding_rechecks[0], head_sha="c" * 40)
    assert not client.validate(replace(request, finding_rechecks=(wrong,))).review_complete


def test_strict_reservation_carries_evidence_through_policy(settings):
    runtime = object.__new__(StrictRuntime)
    runtime._core = _FakeStrictCore()
    runtime.reserve_review(StrictReviewRequest(
        RepositoryRef("vllm-omni"), 7, HEAD, "", "attempt-1",
        carried_findings=(_carried(),),
    ))
    payload = runtime._core.requests[0]
    assert payload["params"]["carried_findings"] == [_carried().to_dict()]
    from infermatrix_copilot.mcp_policy import enforce_mcp_policy
    spec = enforce_mcp_policy(payload, allowed_repos=["vllm-omni"])
    assert spec.params["carried_findings"] == [_carried().to_dict()]


def test_old_strict_serialization_keeps_its_wire_shape():
    from infermatrix_copilot.sdk.v1 import StrictPollResult, StrictReviewResult, StrictRuntimeConfig

    review = StrictReviewResult("review.v1", HEAD, "COMMENT", "frozen result",
                                (), (), (), True, (), False, {})
    legacy = {"contract_version": "review.v1", "reviewed_head_sha": HEAD,
        "verdict": "COMMENT", "summary_markdown": "frozen result", "comments": (),
        "findings": (), "finding_rechecks": (), "rechecks_complete": True,
        "recheck_missing": (), "stale": False, "diagnostics": {}}
    assert review.to_dict() == legacy
    assert StrictPollResult("run-1", "done", {}, review).to_dict() == {
        "run_id": "run-1", "state": "done", "payload": {}, "review": legacy}
    config = StrictRuntimeConfig(RepositoryRef("vllm-omni", "vllm-project/vllm-omni"),
                                 "/repo", "/")
    assert config.to_dict() == {"repository": {"alias": "vllm-omni", "full_name": "vllm-project/vllm-omni"},
        "checkout_path": "/repo", "allowed_root": "/", "backend": "", "run_root": "",
        "max_workers": 1, "knowledge_maintenance": {}}


def test_new_direct_proofs_survive_result_and_nested_poll_serialization():
    from infermatrix_copilot.sdk.v1 import StrictPollResult, StrictReviewResult, StrictRuntimeConfig

    review = StrictReviewResult("review.v1", HEAD, "COMMENT", "frozen result",
        (), (), (), True, (), False, {}, finding_dispositions=({"anchor": "a.py:1"},),
        expected_head_sha=HEAD, actual_head_sha=HEAD, direct_result={"summary": "native verdict"})
    wire = review.to_dict()
    assert wire["finding_dispositions"] == ({"anchor": "a.py:1"},)
    assert wire["expected_head_sha"] == wire["actual_head_sha"] == HEAD
    assert wire["direct_result"] == {"summary": "native verdict"}
    assert StrictPollResult("run-1", "done", {}, review).to_dict()["review"] == wire
    profiles = {"codex": {"provider": "codex", "command": ["codex"]}}
    config = StrictRuntimeConfig(RepositoryRef("vllm-omni", "vllm-project/vllm-omni"),
                                 "/repo", "/", direct_profiles=profiles)
    assert config.to_dict()["direct_profiles"] == profiles
