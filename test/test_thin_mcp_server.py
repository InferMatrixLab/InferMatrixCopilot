import sys
from pathlib import Path
from types import ModuleType

import pytest

from infermatrix_copilot.knowledge_docs import KnowledgeDocsError
from infermatrix_copilot.thin_mcp_server import (
    build_mcp,
    _direct_completion_result,
    _direct_knowledge_routes,
    _docs,
    _normalize_repo,
)
from infermatrix_copilot.config import Settings


def _fake_mcp(monkeypatch):
    fastmcp_module = ModuleType("mcp.server.fastmcp")

    class FakeMCP:
        def __init__(self, *_args, **_kwargs):
            self.tools = {}
            self.tool_annotations = {}

        def tool(self, **kwargs):
            def register(fn):
                self.tools[fn.__name__] = fn
                self.tool_annotations[fn.__name__] = kwargs.get("annotations")
                return fn

            return register

    class FakeToolAnnotations:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    fastmcp_module.FastMCP = FakeMCP
    types_module = ModuleType("mcp.types")
    types_module.ToolAnnotations = FakeToolAnnotations
    mcp_module = ModuleType("mcp")
    mcp_module.__path__ = []
    server_module = ModuleType("mcp.server")
    server_module.__path__ = []
    mcp_module.server = server_module
    mcp_module.types = types_module
    server_module.fastmcp = fastmcp_module
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)
    monkeypatch.setitem(sys.modules, "mcp.types", types_module)

    class FakeCore:
        def __init__(self):
            self.settings = Settings(
                repo_full_names={"vllm-omni": "vllm-project/vllm-omni"})
            self.requests = []
            self.repo_configurations = []

        def start_strict_review(self, request):
            self.requests.append(request)
            return "run-20260730-120000-abc123"

        def strict_readiness(self, repo, repo_path=""):
            self.repo_configurations.append((repo, repo_path))
            return []

        def get_result(self, run_id, offset=0):
            return {"run_id": run_id, "state": "done", "offset": offset}

        def get_status(self, run_id):
            return {"run_id": run_id, "status": {"state": "running"}}

    core = FakeCore()
    return build_mcp(core=core), core


def test_tool_annotations_mark_the_read_only_surface(monkeypatch):
    """Approval-gating hosts (codex) read these hints; keep them truthful.

    Unannotated tools trigger a per-call approval dialog in codex and are
    auto-cancelled in its headless runs (#86), so every tool declares its
    hints: `review` can reserve a Strict run and `rfc_request` can modify
    authorized RFC state. Knowledge context tools only read source content;
    their private session ledger does not modify knowledge or forge data.
    """
    mcp, _core = _fake_mcp(monkeypatch)

    assert set(mcp.tool_annotations) == set(mcp.tools)
    review = mcp.tool_annotations["review"]
    assert review is not None
    assert review.readOnlyHint is False
    assert review.destructiveHint is False
    assert review.openWorldHint is True
    for name in ("validate_direct_review", "get_review_result",
                 "get_review_status", "update_knowledge", "doc_search",
                 "doc_read", "open_knowledge_context", "read_knowledge_context",
                 "search_knowledge_context", "related_knowledge_context",
                 "expand_knowledge_context"):
        hints = mcp.tool_annotations[name]
        assert hints is not None, name
        assert hints.readOnlyHint is True, name
        assert hints.openWorldHint is False, name
    for name in ("rfc_capabilities", "rfc_status"):
        hints = mcp.tool_annotations[name]
        assert hints.readOnlyHint is True and hints.idempotentHint is True
        assert hints.openWorldHint is True
    rfc_request = mcp.tool_annotations["rfc_request"]
    assert rfc_request.readOnlyHint is False and rfc_request.destructiveHint is True
    assert rfc_request.idempotentHint is False and rfc_request.openWorldHint is True


def test_direct_entrypoints_do_not_resolve_repo(monkeypatch):
    mcp, core = _fake_mcp(monkeypatch)
    assert set(mcp.tools) == {
        "review",
        "validate_direct_review",
        "get_review_result",
        "get_review_status",
        "update_knowledge",
        "doc_search",
        "doc_read",
        "rfc_request",
        "rfc_capabilities",
        "rfc_status",
        "open_knowledge_context",
        "read_knowledge_context",
        "search_knowledge_context",
        "expand_knowledge_context",
        "related_knowledge_context",
    }

    review = mcp.tools["review"](
        target="https://github.com/owner/repo/pull/1",
        repo="owner/repo",
        mode="direct",
        post=True,
    )
    assert set(review) == {
        "mode",
        "knowledge_entry",
        "knowledge_routes",
        "related_knowledge",
        "mandatory_review_guides",
        "routing",
        "navigation_policy",
        "execution_budget",
        "untested_public_api",
        "first_review_checklist",
        "progress_update",
        "completion_gate",
        "diagnostics",
    }
    assert review["mode"] == "direct"
    assert review["untested_public_api"] == {"status": "no_diff"}
    assert core.requests == []
    assert Path(review["knowledge_entry"]).parts[-2:] == ("knowledge", "AGENTS.md")
    assert review["knowledge_routes"] == []
    assert len(review["mandatory_review_guides"]) == 1
    assert Path(review["mandatory_review_guides"][0]).as_posix().endswith(
        "general/review/guides/simplification-audit.md"
    )
    # `owner/repo` is not a served repo. This used to report `needs_pr_context`
    # because the empty-description guard ran before the repo guard — misleading,
    # since no description would make an unserved repo routable, and unsafe once
    # the unrouted path started deriving routes from changed files.
    assert review["routing"]["status"] == "unsupported_exact_router"
    assert review["navigation_policy"]["progress_before_knowledge"] is True
    assert review["navigation_policy"]["use_embedded_quick_maps"] is True
    assert review["navigation_policy"]["read_mandatory_review_guides"] is True
    assert review["navigation_policy"]["stop_after_routes"] is True
    assert review["execution_budget"]["profile"] == "code"
    assert review["execution_budget"]["total_command_calls"] == 20
    assert review["execution_budget"]["hard_ceiling"] is True
    assert review["execution_budget"]["extension_command_calls"] == 4
    assert any(
        "subtraction" in item
        for item in review["first_review_checklist"]
    )
    assert any(
        "pins the original failure path" in item
        for item in review["first_review_checklist"]
    )
    assert any(
        "consumers, trust boundaries, and lifecycle ownership" in item
        for item in review["first_review_checklist"]
    )
    assert any(
        "semantic-analogue census" in item
        and "REUSE, EXTEND, or NEW" in item
        and "transport, layout, side tensors, lifecycle, and hook boundary" in item
        for item in review["first_review_checklist"]
    )
    assert any(
        "before reading knowledge, searching source, or running tests"
        in item
        for item in review["first_review_checklist"]
    )
    assert any(
        "bounded rg searches" in item
        for item in review["first_review_checklist"]
    )
    assert any(
        "compatibility preflight" in item
        and "environment fingerprint" in item
        for item in review["first_review_checklist"]
    )
    # Two failure classes the wave-1 arm missed and the unassisted baseline caught.
    # Neither belongs to a component owner, so no knowledge route can carry them and
    # the checklist is the only channel that reaches every Direct review.
    assert any(
        "fixture, mock, or fake injected" in item
        for item in review["first_review_checklist"]
    )
    assert any(
        "lowest version the project's own constraints still permit" in item
        for item in review["first_review_checklist"]
    )
    assert any(
        "at the frozen head SHA" in item
        and "fetch the PR head ref" in item
        for item in review["first_review_checklist"]
    )
    assert any(
        "existing feedback is a final deduplication input" in item
        for item in review["first_review_checklist"]
    )
    assert any(
        "extends_existing" in item and "Suppress duplicates" in item
        for item in review["first_review_checklist"]
    )
    assert review["progress_update"] == {
        "deadline_seconds": 60,
        "channel": "host_conversation",
        "required_fields": [
            "head_sha",
            "ci_status",
            "mergeability",
            "early_findings",
        ],
        "early_findings_status": "preliminary",
        "continue_review": True,
        "github_comment": False,
        "emit_before": [
            "knowledge_read",
            "source_search",
            "tests",
        ],
        "do_not_wait_for": [
            "ci_completion",
            "mergeability_resolution",
        ],
    }
    assert review["completion_gate"] == {
        "tool": "validate_direct_review",
        "evidence_head_sha": "Required: the frozen head commit SHA every cited source file and validation result was read at; fetch the PR head ref when the local checkout holds another revision.",
        "existing_feedback_status": {
            "checked": "PR feedback was fetched after independent source verification and every candidate was classified.",
            "disabled": "PR_CONTEXT_MODE=no_discussion explicitly disabled feedback for evaluation.",
            "unavailable": "PR feedback could not be fetched; report this validation gap.",
            "not_applicable": "The target is a local/worktree review without a PR.",
        },
        "finding_dispositions": "For checked PR reviews: [{anchor, disposition, existing_thread?, head_recheck?}] where disposition is new, duplicate, extends_existing, or resolved_or_outdated; resolved/outdated items require head_recheck=fixed or still_affected.",
        "subtraction_signal": {
            "none": "No helper/class/fallback/compatibility/public-behavior expansion; no subtraction evidence required.",
            "triggered": "Require subtraction items or minimality_proof.",
        },
        "triggered_require_one_of": [
            "subtraction[{anchor, action, risk}]",
            "minimality_proof{scope_ledger, abstraction_census, why_no_safe_deletion}",
        ],
        "final_comment_count": 1,
        "if_missing": "partial_review",
    }
    assert set(review["diagnostics"]["timing_ms"]) == {
        "routing",
        "related_knowledge",
        "execution_budget",
        "total",
    }

    update = mcp.tools["update_knowledge"](repo="owner/repo")
    assert set(update) == {"knowledge_entry"}
    assert Path(update["knowledge_entry"]).parts[-2:] == (
        "knowledge",
        "CONTRIBUTING.md",
    )


def test_direct_routes_title_body_before_changed_files(monkeypatch):
    mcp, _core = _fake_mcp(monkeypatch)
    review = mcp.tools["review"](
        target="https://github.com/vllm-project/vllm-omni/pull/4762",
        repo="vllm-project/vllm-omni",
        mode="direct",
        title="[Core / Bugfix] Add Mechanism for Endpoint Rejection",
        body=(
            "Add pipeline config endpoint restrictions and return a 400 from "
            "the OpenAI serving layer."
        ),
        changed_files=[
            "vllm_omni/config/config_factory.py",
            "vllm_omni/entrypoints/openai/api_server.py",
            "vllm_omni/model_executor/models/qwen3_omni/model.py",
        ],
    )

    assert review["routing"]["status"] == "ready"
    assert review["routing"]["changed_files_role"] == "scope_validation_only"
    assert [route["owner"] for route in review["knowledge_routes"]] == [
        "configuration",
        "serving",
    ]
    assert review["knowledge_entry"] == review["knowledge_routes"][0]["path"]
    assert all(
        Path(route["path"]).is_file()
        for route in review["knowledge_routes"]
    )
    # read_required tracks whether the embedded map is COMPLETE, not merely present:
    # the serving section exceeds the 3500 cap, so its map is clipped and the host is
    # told to open the page (with a budgeted read) instead of trusting a partial map.
    for route in review["knowledge_routes"]:
        assert "## Direct" in route["quick_map"]
        assert len(route["quick_map"]) <= 3500
        assert route["quick_map_status"] in {"ok", "truncated"}
        assert route["read_required"] is (route["quick_map_status"] != "ok")
    assert review["execution_budget"]["knowledge_file_reads"] == len(
        [r for r in review["knowledge_routes"] if r["read_required"]]
    ) + len(review["mandatory_review_guides"]) + len(review["navigation_policy"]["related_document_read_paths"])
    scope = {
        item["owner"]: item
        for item in review["routing"]["scope_validation"]
    }
    assert scope["model-executor"]["selected_from_description"] is False
    assert review["execution_budget"]["profile"] == "code"
    # was 0 unconditionally; the budget now grants exactly one read per route whose
    # map is not fully deliverable, so it cannot contradict read_required
    assert review["execution_budget"]["knowledge_file_reads"] == 2 + len(review["navigation_policy"]["related_document_read_paths"])


def test_direct_ranks_diffusion_owner_for_scheduler_managed_kv_pr(monkeypatch):
    mcp, _core = _fake_mcp(monkeypatch)
    review = mcp.tools["review"](
        target="https://github.com/vllm-project/vllm-omni/pull/6094",
        repo="vllm-project/vllm-omni",
        mode="direct",
        title=(
            "[Diffusion] Add native KV cache initialization and "
            "Scheduler-managed block allocation"
        ),
        body=(
            "Discover KV geometry, calculate available memory, initialize "
            "paged_scheduler allocation, and keep dense_legacy gated. "
            "HunyuanImage3 is the first consumer."
        ),
        changed_files=[
            "vllm_omni/config/omni_config.py",
            "vllm_omni/diffusion/diffusion_engine.py",
            "vllm_omni/diffusion/diffusion_kv/manager.py",
            "vllm_omni/diffusion/sched/base_scheduler.py",
            "vllm_omni/diffusion/worker/diffusion_worker.py",
            (
                "vllm_omni/diffusion/models/hunyuan_image3/"
                "hunyuan_image3_transformer.py"
            ),
        ],
    )

    owners = [route["owner"] for route in review["knowledge_routes"]]
    assert owners[:2] == ["model:hunyuan-image3", "diffusion"]
    assert "serving" not in owners
    assert any(
        "resource or cache changes" in item
        for item in review["first_review_checklist"]
    )


def test_direct_docs_only_budget_stays_small(monkeypatch):
    mcp, _core = _fake_mcp(monkeypatch)
    review = mcp.tools["review"](
        target="https://github.com/vllm-project/vllm-omni/pull/4950",
        mode="direct",
        title="Fix MiniCPM-o 4.5 TTS request example",
        body="Correct the serving request and TTS response documentation.",
        changed_files=["recipes/OpenBMB/MiniCPM-o-4_5.md"],
    )

    assert review["execution_budget"]["profile"] == "docs_only"
    assert review["execution_budget"]["validation_commands"] == 2
    assert review["execution_budget"]["total_command_calls"] == 12
    assert review["execution_budget"]["command_output_chars"] == 12000
    assert review["execution_budget"]["hard_ceiling"] is True


def test_direct_serving_quick_map_covers_request_and_lifecycle(monkeypatch):
    mcp, _core = _fake_mcp(monkeypatch)

    docs_review = mcp.tools["review"](
        target="https://github.com/vllm-project/vllm-omni/pull/4950",
        mode="direct",
        title="Fix MiniCPM-o 4.5 TTS request example",
        body="Correct chat_template_kwargs and the TTS response contract.",
        changed_files=["recipes/OpenBMB/MiniCPM-o-4_5.md"],
    )
    assert [
        route["owner"] for route in docs_review["knowledge_routes"]
    ] == ["model:minicpm-o-4-5", "serving"]
    serving_map = next(
        route["quick_map"]
        for route in docs_review["knowledge_routes"]
        if route["owner"] == "serving"
    )
    assert "chat_template_kwargs" in serving_map
    assert "chat_completion_full_generator" in serving_map

    lifecycle_review = mcp.tools["review"](
        target="https://github.com/vllm-project/vllm-omni/pull/4834",
        mode="direct",
        title="[Bugfix] Guard generation during partial wake",
        body=(
            "Preserve sleep/wake stage scope, validate worker ACKs, and make "
            "wake idempotent."
        ),
        changed_files=[
            "vllm_omni/entrypoints/async_omni.py",
            "vllm_omni/worker/base.py",
        ],
    )
    owners = [route["owner"] for route in lifecycle_review["knowledge_routes"]]
    assert owners == ["serving"]
    assert "SERV-5a" in lifecycle_review["knowledge_routes"][0]["quick_map"]


def test_direct_routes_model_rules_without_index_navigation():
    routing = _direct_knowledge_routes(
        "vllm-omni",
        title="Fix MiniCPM-o 4.5 TTS request example",
        body="The serving request must reach the TTS stage input processor.",
        changed_files=["recipes/OpenBMB/MiniCPM-o-4_5.md"],
    )

    assert routing["status"] == "ready"
    assert [route["owner"] for route in routing["routes"]] == [
        "model:minicpm-o-4-5",
        "serving",
        "model-executor",
    ]
    assert len(routing["routes"]) == 3
    assert all(route["quick_map"] for route in routing["routes"])


_HEAD_SHA = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"


def test_direct_completion_requires_subtraction_signal_classification():
    result = _direct_completion_result(evidence_head_sha=_HEAD_SHA)

    assert result["status"] == "partial_review"
    assert result["publish_ready"] is False
    assert result["missing"] == [
        "subtraction_signal must be 'none' or 'triggered'"
    ]


def test_direct_completion_requires_evidence_head_sha():
    result = _direct_completion_result(subtraction_signal="none")

    assert result["status"] == "partial_review"
    assert result["publish_ready"] is False
    assert result["missing"] == [
        "evidence_head_sha must be the frozen head commit SHA "
        "(7-40 hex characters) that every cited source file and "
        "validation result was read at"
    ]


def test_direct_completion_rejects_non_commit_evidence_reference():
    result = _direct_completion_result(
        subtraction_signal="none",
        evidence_head_sha="main",
    )

    assert result["status"] == "partial_review"
    assert result["publish_ready"] is False


def test_small_fix_accepts_no_subtraction_signal_without_full_proof():
    result = _direct_completion_result(
        subtraction_signal="none",
        evidence_head_sha=_HEAD_SHA,
    )

    assert result["status"] == "complete"
    assert result["publish_ready"] is True
    assert result["subtraction_required"] is False
    assert result["subtraction_items"] == 0
    assert result["evidence_head_sha"] == _HEAD_SHA


def test_pr_review_requires_feedback_status():
    result = _direct_completion_result(
        subtraction_signal="none",
        evidence_head_sha=_HEAD_SHA,
        existing_feedback_status="",
    )

    assert result["status"] == "partial_review"
    assert result["publish_ready"] is False
    assert result["missing"] == [
        (
            "existing_feedback_status must be 'checked', 'disabled', "
            "'unavailable', or 'not_applicable'"
        )
    ]


def test_checked_feedback_counts_and_suppresses_duplicate_dispositions():
    result = _direct_completion_result(
        subtraction_signal="none",
        evidence_head_sha=_HEAD_SHA,
        existing_feedback_status="checked",
        finding_dispositions=[
            {
                "anchor": "src/runtime.py:42",
                "disposition": "duplicate",
                "existing_thread": "PRRT_duplicate",
            },
            {
                "anchor": "src/runtime.py:91",
                "disposition": "extends_existing",
                "existing_thread": "PRRT_extension",
            },
            {
                "anchor": "src/config.py:12",
                "disposition": "new",
            },
        ],
    )

    assert result["status"] == "complete"
    assert result["finding_dispositions"] == 3
    assert result["duplicate_findings_suppressed"] == 1


def test_non_new_disposition_requires_existing_thread():
    result = _direct_completion_result(
        subtraction_signal="none",
        evidence_head_sha=_HEAD_SHA,
        existing_feedback_status="checked",
        finding_dispositions=[{
            "anchor": "src/runtime.py:42",
            "disposition": "resolved_or_outdated",
        }],
    )

    assert result["status"] == "partial_review"
    assert "existing_thread" in result["missing"][0]


def test_feedback_dispositions_require_checked_status():
    result = _direct_completion_result(
        subtraction_signal="none",
        evidence_head_sha=_HEAD_SHA,
        existing_feedback_status="unavailable",
        finding_dispositions=[{
            "anchor": "src/runtime.py:42",
            "disposition": "new",
        }],
    )

    assert result["status"] == "partial_review"
    assert result["missing"] == [
        "finding_dispositions require existing_feedback_status='checked'"
    ]


def test_no_discussion_evaluation_can_explicitly_disable_feedback():
    result = _direct_completion_result(
        subtraction_signal="none",
        evidence_head_sha=_HEAD_SHA,
        existing_feedback_status="disabled",
    )

    assert result["status"] == "complete"
    assert result["existing_feedback_status"] == "disabled"


def test_resolved_outdated_rechecks_the_pinned_head():
    result = _direct_completion_result(
        subtraction_signal="none",
        evidence_head_sha=_HEAD_SHA,
        existing_feedback_status="checked",
        finding_dispositions=[
            {
                "anchor": "src/fixed.py:10",
                "disposition": "resolved_or_outdated",
                "existing_thread": "PRRT_fixed",
                "head_recheck": "fixed",
            },
            {
                "anchor": "src/live.py:20",
                "disposition": "resolved_or_outdated",
                "existing_thread": "PRRT_live",
                "head_recheck": "still_affected",
            },
        ],
    )

    assert result["status"] == "complete"
    assert result["duplicate_findings_suppressed"] == 1
    assert result["resolved_or_outdated_still_affected"] == 1


def test_triggered_subtraction_requires_evidence():
    result = _direct_completion_result(
        subtraction_signal="triggered",
        evidence_head_sha=_HEAD_SHA,
    )

    assert result["status"] == "partial_review"
    assert result["publish_ready"] is False
    assert result["subtraction_required"] is True
    assert result["missing"] == [
        "triggered subtraction requires subtraction items or a concrete "
        "minimality_proof with scope_ledger, abstraction_census, and "
        "why_no_safe_deletion"
    ]


def test_issue_5559_trigger_accepts_single_comment_with_subtractions():
    result = _direct_completion_result(
        subtraction_signal="triggered",
        evidence_head_sha=_HEAD_SHA,
        subtraction=[
            {
                "anchor": "examples/offline_inference/text_to_image/text_to_image.py:358",
                "action": "DELETE the unproven tokenizer fallback compatibility path",
                "risk": "low: default fail-fast behavior remains unchanged",
            },
            {
                "anchor": "examples/offline_inference/image_to_image/image_edit.py:116",
                "action": "MERGE the duplicated AR-stage application helper into one owner",
                "risk": "medium: preserve both public CLI call paths",
            },
        ],
    )

    assert result["status"] == "complete"
    assert result["publish_ready"] is True
    assert result["subtraction_required"] is True
    assert result["subtraction_items"] == 2
    assert result["final_comment_count"] == 1


def test_direct_completion_accepts_concrete_minimality_proof():
    result = _direct_completion_result(
        subtraction_signal="triggered",
        evidence_head_sha=_HEAD_SHA,
        minimality_proof={
            "scope_ledger": "Every changed production file maps to the requested API fix.",
            "abstraction_census": "No new helper, class, projection, fallback, or compatibility branch.",
            "why_no_safe_deletion": "Deleting any changed branch removes the only validated consumer path.",
        },
    )

    assert result["status"] == "complete"
    assert result["publish_ready"] is True
    assert result["minimality_proof"] is True


def test_direct_completion_rejects_malformed_subtraction_and_two_comments():
    result = _direct_completion_result(
        subtraction_signal="triggered",
        evidence_head_sha=_HEAD_SHA,
        subtraction=[{
            "anchor": "examples/task.py:120",
            "action": "",
            "risk": "low",
        }],
        final_comment_count=2,
    )

    assert result["status"] == "partial_review"
    assert result["publish_ready"] is False
    assert len(result["missing"]) == 3


def test_direct_completion_does_not_count_bug_fixes_as_subtraction():
    result = _direct_completion_result(
        subtraction_signal="triggered",
        evidence_head_sha=_HEAD_SHA,
        subtraction=[{
            "anchor": "src/adapter.py:42",
            "action": "FIX the incorrect default value",
            "risk": "low",
        }],
    )

    assert result["status"] == "partial_review"
    assert result["publish_ready"] is False


def test_no_signal_rejects_contradictory_subtraction_evidence():
    result = _direct_completion_result(
        subtraction_signal="none",
        evidence_head_sha=_HEAD_SHA,
        subtraction=[{
            "anchor": "src/adapter.py:42",
            "action": "DELETE unused compatibility branch",
            "risk": "low",
        }],
    )

    assert result["status"] == "partial_review"
    assert result["publish_ready"] is False
    assert result["missing"] == [
        "subtraction_signal 'none' cannot include subtraction evidence"
    ]


def test_full_github_repo_name_maps_to_knowledge_repo():
    assert _normalize_repo("vllm-project/vllm-omni") == "vllm-omni"
    docs = _docs("vllm-project/vllm-omni")
    assert docs.read("repos/vllm-omni/_index.md")["path"] == (
        "repos/vllm-omni/_index.md"
    )


def test_same_repo_name_under_another_owner_is_rejected():
    with pytest.raises(KnowledgeDocsError, match="unsupported knowledge repo"):
        _docs("another-owner/vllm-omni")


def test_strict_maps_to_the_old_eco_request(monkeypatch):
    mcp, core = _fake_mcp(monkeypatch)

    result = mcp.tools["review"](
        target="https://github.com/vllm-project/vllm-omni/pull/5172",
        mode="strict",
        review_depth="full",
    )

    diagnostics = result.pop("diagnostics")
    assert result == {
        "run_id": "run-20260730-120000-abc123",
        "mode": "strict",
        "execution_mode": "eco",
        "post": False,
    }
    assert set(diagnostics["timing_ms"]) == {"start_strict_review", "total"}
    assert core.requests == [{
        "kind": "pr_review",
        "repo": "vllm-omni",
        "pr": 5172,
        "mode": "eco",
        "post": False,
        "params": {"review_depth": "full"},
    }]
    assert core.repo_configurations == [("vllm-omni", "")]


def test_strict_refuses_an_explicit_post_at_the_public_surface(monkeypatch):
    """Refused here as well as in the policy, so the caller gets the reason
    rather than a generic rejection from deeper in the stack — and no run is
    reserved for a request that could never have published."""
    mcp, core = _fake_mcp(monkeypatch)

    result = mcp.tools["review"](
        target="https://github.com/vllm-project/vllm-omni/pull/5172",
        mode="strict",
        post=True,
    )

    assert "cannot post" in result["error"]
    assert core.requests == []  # nothing was started


def test_strict_does_not_post_by_default(monkeypatch):
    mcp, core = _fake_mcp(monkeypatch)

    result = mcp.tools["review"](target="PR #6", mode="strict")

    assert result["post"] is False
    assert core.requests[0]["mode"] == "eco"
    assert core.requests[0]["post"] is False


def test_strict_reports_setup_gaps_before_starting(monkeypatch):
    mcp, core = _fake_mcp(monkeypatch)
    core.strict_readiness = lambda repo, repo_path="": [
        "model credential missing",
        "checkout missing",
    ]

    result = mcp.tools["review"](
        target="https://github.com/vllm-project/vllm-omni/pull/5172",
        mode="strict",
    )

    assert result == {
        "error": (
            "Strict mode is not ready: model credential missing; "
            "checkout missing"
        )
    }
    assert core.requests == []


# -- Direct's deliverable must never be silently empty -------------------------


def test_every_routed_page_yields_a_quick_map():
    """Direct's product IS the embedded map. A rule page whose Direct heading is
    renamed hands the host an empty map plus "don't open this", and nothing today
    notices. Calls the PRODUCTION extractor so this gate cannot drift from the
    server the way a second copy of the heading regex would."""
    from pathlib import Path

    from infermatrix_copilot.direct_routing import load_routes
    from infermatrix_copilot.knowledge_view import KnowledgeView
    from infermatrix_copilot.thin_mcp_server import _direct_quick_map, _knowledge_path

    root = KnowledgeView.current().root
    pages = []
    for repo_dir in sorted((root / "repos").iterdir()):
        table = load_routes(repo_dir.name) if repo_dir.is_dir() else None
        if not table:
            continue
        pages += [_knowledge_path(str(r["path"])) for r in table["owners"]]
        if table["models"]:
            pages += [str(p) for p in sorted(
                (root / table["models"]["dir"]).glob(f"*/{table['models']['page']}"))]
    assert pages, "no routed pages found — the enumeration itself is broken"

    # Pages whose Direct section exceeds the 3500-char cap. They still behave
    # correctly (status=truncated -> read_required, budget grants the read), but a
    # shipped page that ALWAYS forces a file read defeats the point of an embedded
    # map, so this is recorded debt that must not grow. Fix by trimming the section,
    # not by widening the list or the cap.
    known_truncated = {"components/serving/rules.md"}

    broken, newly_truncated = [], []
    for page in pages:
        if not Path(page).is_file():
            broken.append(f"{page}: missing")
            continue
        text, status = _direct_quick_map(page)
        if status == "unavailable" or not text.strip():
            broken.append(f"{page}: {status}")
        elif status == "truncated" and not any(
                Path(page).as_posix().endswith(k) for k in known_truncated):
            newly_truncated.append(page)
    assert not broken, (
        "routed pages with no deliverable Direct quick map — the host receives an "
        f"empty map: {broken}")
    assert not newly_truncated, (
        "routed pages whose Direct section grew past the cap, so the host now gets a "
        f"clipped map and must open the file: {newly_truncated}")


def test_missing_quick_map_fails_closed_at_runtime(tmp_path, monkeypatch):
    """The conformance test above only covers the shipped tree.
    Explicit compatibility/test paths must degrade to "open the page" rather
    than emit an empty map."""
    from infermatrix_copilot import thin_mcp_server as tms

    page = tmp_path / "rules.md"
    page.write_text("## 开发快速入口\n\nno Direct heading here\n", encoding="utf-8")
    route = tms._direct_route("owner", str(page), "test")
    assert route["quick_map_status"] == "unavailable"
    assert route["read_required"] is True

    ok = tmp_path / "good.md"
    ok.write_text("## Direct code map\n\n- entry\n", encoding="utf-8")
    good = tms._direct_route("owner", str(ok), "test")
    assert good["quick_map_status"] == "ok" and good["read_required"] is False


def test_budget_allows_exactly_the_reads_the_routes_require(monkeypatch):
    """`read_required: True` next to `knowledge_file_reads: 0` is an instruction the
    host cannot satisfy. Normal routes must stay at 0."""
    from infermatrix_copilot.thin_mcp_server import _direct_execution_budget

    assert _direct_execution_budget(["a.py"])["knowledge_file_reads"] == 0
    assert _direct_execution_budget(
        ["a.py"], knowledge_file_reads=2)["knowledge_file_reads"] == 2


# -- scope fallback ------------------------------------------------------------


def _routes(title="", body="", files=None, repo="vllm-omni"):
    from infermatrix_copilot.thin_mcp_server import _direct_knowledge_routes
    return _direct_knowledge_routes(repo, title=title, body=body,
                                    changed_files=files or [])


SERVING = "vllm_omni/entrypoints/openai/serving_speech.py"
EXEC1 = "vllm_omni/model_executor/models/ming_tts/patch_emission.py"
EXEC2 = "vllm_omni/model_executor/models/common/ming/audio_vae.py"


def test_unsupported_repo_never_routes_regardless_of_description():
    """The repo guard must run before anything derives routes from changed files,
    or an unserved repo gets this repo's owner knowledge."""
    for title, body in (("", ""), ("qwen3-tts payload fix", "serving change")):
        out = _routes(title, body, [SERVING], repo="some/other-repo")
        assert out["status"] == "unsupported_exact_router"
        assert out["routes"] == []


def test_fallback_fills_the_void_when_description_routes_nothing():
    out = _routes("Update WeChat QR code", "no routable vocabulary", [SERVING])
    assert out["status"] == "scope_fallback"
    assert [r["owner"] for r in out["routes"]] == ["serving"]
    assert out["selected_by"] == "title_body+changed_files"
    assert out["changed_files_role"] == "selected_fallback_routes"
    assert out["routes"][0]["reason"].startswith("changed files:")


def test_fallback_when_no_description_at_all():
    """needs_pr_context used to return zero routes even with changed files in hand."""
    out = _routes("", "", [SERVING])
    assert out["status"] == "scope_fallback"
    assert [r["owner"] for r in out["routes"]] == ["serving"]


def test_no_description_and_no_scope_still_asks_for_context():
    out = _routes("", "", [])
    assert out["status"] == "needs_pr_context"
    assert out["routes"] == [] and "required" in out


def test_agreeing_description_route_suppresses_fallback_and_changes_nothing():
    out = _routes("[Bugfix] serving endpoint request handling",
                  "openai endpoint", [SERVING])
    assert out["status"] == "ready"
    assert "serving" in [r["owner"] for r in out["routes"]]
    assert out["selected_by"] == "title_body"
    assert out["changed_files_role"] == "scope_validation_only"


def test_competing_scope_owners_are_ordered_by_evidence():
    """Two model-executor files vs one serving file — the higher match count wins the
    slot, deterministically, not whichever appears first in the route table."""
    out = _routes("Update WeChat QR code", "", [SERVING, EXEC1, EXEC2])
    assert out["status"] == "scope_fallback"
    assert out["routes"][0]["owner"] == "model-executor"


def test_fallback_displaces_the_weakest_route_at_a_full_cap():
    """Exercises the routes.pop() branch: three description routes already fill the
    cap and none matches the changed-file owner, so the weakest is displaced."""
    out = _routes(
        "[Perf] qwen3-tts deploy yaml endpoint scheduler batch",
        "config deploy openai endpoint request scheduler batch sampling",
        [EXEC1, EXEC2])
    owners = [r["owner"] for r in out["routes"]]
    assert len(owners) == 3, owners
    assert out["status"] == "scope_fallback"
    assert "model-executor" in owners  # the changed-file owner got in


def test_precap_match_dropped_by_the_cap_still_triggers_fallback():
    """`selected_from_description` reports the PRE-cap fact. If agreement were judged
    on it, an owner displaced by [:3] would suppress the fallback while never
    reaching the host."""
    out = _routes(
        "[Perf] qwen3-tts deploy yaml endpoint scheduler batch",
        "config deploy openai endpoint request scheduler batch sampling",
        [EXEC1, EXEC2])
    scope = {s["owner"]: s for s in out["scope_validation"]}
    assert "model-executor" in scope
    delivered = [r["owner"] for r in out["routes"]]
    # whatever the pre-cap flag says, the owner the diff implies IS delivered
    assert "model-executor" in delivered


def test_unavailable_quick_map_wires_through_review_end_to_end(monkeypatch, tmp_path):
    """The helper-level budget test does not prove the count reaches the response."""
    mcp, _core = _fake_mcp(monkeypatch)
    from infermatrix_copilot import thin_mcp_server as tms

    real = tms._direct_route

    def blind(owner, path, reason):
        route = real(owner, path, reason)
        route.update(quick_map="", quick_map_status="unavailable", read_required=True)
        return route

    monkeypatch.setattr(tms, "_direct_route", blind)
    review = mcp.tools["review"](
        target="https://github.com/vllm-project/vllm-omni/pull/1",
        repo="vllm-project/vllm-omni", mode="direct",
        title="[Bugfix] serving endpoint request handling", body="openai endpoint",
        changed_files=[SERVING])
    n = len([r for r in review["knowledge_routes"] if r["read_required"]])
    assert n >= 1
    assert review["execution_budget"]["knowledge_file_reads"] == (
        n + len(review["mandatory_review_guides"]) + len(review["navigation_policy"]["related_document_read_paths"])
    )
    assert "unavailable" in review["navigation_policy"]["open_route_file_when"]


def test_heading_only_section_is_not_a_usable_quick_map(tmp_path):
    """The extract includes its own heading, so `## Direct ...` with nothing under it
    is truthy while carrying no map — it must not pass as ok."""
    from infermatrix_copilot import thin_mcp_server as tms
    page = tmp_path / "r.md"
    page.write_text("## Direct code map\n\n## next section\n", encoding="utf-8")
    route = tms._direct_route("owner", str(page), "test")
    assert route["quick_map_status"] == "unavailable"
    assert route["read_required"] is True
