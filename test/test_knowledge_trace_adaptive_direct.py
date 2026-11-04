"""Adaptive traces observe delivered packets without spending context twice."""

import json
from dataclasses import replace

import pytest

from infermatrix_copilot.kb_service.repo_spec import RepoSpec
from infermatrix_copilot.knowledge_context import ContextBudget, ContextError
from infermatrix_copilot.knowledge_trace import KnowledgeTraceError
from infermatrix_copilot.knowledge_trace.adaptive import packet_fragments
from infermatrix_copilot.knowledge_view import build_manifest
from infermatrix_copilot.sdk.v1 import ChangedPath, DirectClient, DirectReviewRequest, RepositoryRef

PIN = "a" * 40


def configured_client(tmp_path, monkeypatch, *, allowed=(), mode="required"):
    root = tmp_path / "snapshot" / "knowledge"
    docs = {"AGENTS.md": "Knowledge navigation", "general/_index.md": "General navigation",
            "general/review/guides/simplification-audit.md": "Read the current source before reporting findings."}
    for name in ("r", "other"):
        docs[f"repos/{name}/_index.md"] = "Repository navigation"
        docs[f"repos/{name}/doc.md"] = (
            f"---\ntitle: API\ntype: guide\nfeature: api\nsources: [acme/{name}@{PIN}:src/api.py]\n---\n\n"
            + "## RULE-1 API contract\nPreserve zero values. 真实的源码解释。\n" * 300)
    for path, text in docs.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    link = {"repo_id": "other", "status": "confirmed", "source_pin": PIN, "catalog_hash": "b" * 64}
    specs = {name: RepoSpec(name, source_pin=PIN, catalog_hash="b" * 64, policy_hash="c" * 64,
                           dependencies=(link,) if name == "r" else ()).to_dict()
             for name in ("r", "other")}
    (root / "_repositories.yaml").write_text(json.dumps({"schema_version": 1, "repos": specs}))
    (root.parent / "MANIFEST.json").write_text(json.dumps(build_manifest(root, "trace-adaptive")))
    monkeypatch.setenv("KNOWLEDGE_ROOT", str(root.parent))
    client = DirectClient(knowledge_context_path=tmp_path / "context.sqlite", trace_root=tmp_path / "trace",
                          trace_mode=mode, allowed_knowledge_repositories=allowed)
    request = DirectReviewRequest("adaptive-review", RepositoryRef("r"), 0, PIN, "API", "API contract",
                                  (ChangedPath("src/api.py"),))
    return client, request


def tiny_budget():
    return ContextBudget(initial_tokens=1000, max_tokens=4000, model_context_tokens=8000,
                         source_reserve_tokens=2000, output_reserve_tokens=1000)


def captured_text(client, report):
    return [client.knowledge_trace.recorder.store.blob(e["payload_ref"]) for e in report["exposures"]]


def test_adaptive_sdk_captures_only_post_budget_text_and_resumes_trace(tmp_path, monkeypatch):
    client, request = configured_client(tmp_path, monkeypatch)
    opened = client.open_knowledge_context(request, budget=tiny_budget())
    session = opened["session_id"]
    first = client.read_knowledge_context(session, "repos/r/doc.md")
    fragments = packet_fragments(first)
    assert first["truncated"] and len(first["model_content"].encode()) <= 1000
    report = client.knowledge_trace.get_report(first["knowledge_trace"]["trace_id"], repository="r")
    assert captured_text(client, report) == [fragment["content"] for fragment in fragments]
    assert report["exposures"][0]["included_ranges"] == [[0, len(fragments[0]["content"])]]
    assert report["retrievals"][0]["entries"][0]["source_pins"] == [PIN]
    assert all("excerpt" not in document and "content" not in document for document in first["documents"])
    assert opened["knowledge_trace"]["trace_id"] == first["knowledge_trace"]["trace_id"]

    restarted = DirectClient(knowledge_context_path=tmp_path / "context.sqlite", trace_root=tmp_path / "trace",
                             trace_mode="required")
    retry = restarted.read_knowledge_context(session, "repos/r/doc.md")
    assert retry["model_content"] == first["model_content"]
    assert retry["consumed_tokens"] == first["consumed_tokens"]
    assert retry["knowledge_trace"]["trace_id"] == first["knowledge_trace"]["trace_id"]
    restarted.expand_knowledge_context(session, target_tokens=4000, reason="Read remaining contract")
    continuation = restarted.read_knowledge_context(session, "repos/r/doc.md")
    assert continuation["documents"][0]["continuation"]
    report = restarted.knowledge_trace.get_report(continuation["knowledge_trace"]["trace_id"])
    assert captured_text(restarted, report)[-1] == packet_fragments(continuation)[0]["content"]
    assert report["exposures"][-1]["range_status"] == "unknown"
    assert report["retrievals"][-1]["entries"][0]["continuation"] is True


def test_adaptive_plan_search_related_packets_use_same_trace(tmp_path, monkeypatch):
    client, request = configured_client(tmp_path, monkeypatch)
    plan = client.plan_adaptive(request, budget=ContextBudget(initial_tokens=4000, max_tokens=12000))
    trace_id = plan["knowledge_trace"]["trace_id"]
    report = client.knowledge_trace.get_report(trace_id)
    assert report["session"]["review_context_id"] == ""
    assert report["session"]["instrumentation"]["adaptive_context_session_id"] == plan["session_id"]
    assert captured_text(client, report) == [fragment["content"] for fragment in packet_fragments(plan)]
    assert report["retrievals"][0]["mechanism"] == "adaptive_review"
    session = plan["session_id"]
    search = client.search_knowledge_context(session, "Preserve zero")
    related = client.related_knowledge_context(session, ["src/api.py"], query="API")
    assert search["knowledge_trace"]["trace_id"] == related["knowledge_trace"]["trace_id"] == trace_id
    report = client.knowledge_trace.get_report(trace_id)
    assert {row["mechanism"] for row in report["retrievals"]} == {
        "adaptive_review", "adaptive_search", "adaptive_related"}
    for exposure in search["knowledge_trace"]["exposures"]:
        matching = next(row for row in report["exposures"] if row["exposure_id"] == exposure["exposure_id"])
        assert matching["range_status"] == "unknown"


def test_authorized_crossrepo_delivery_records_gap_and_does_not_grant_access(tmp_path, monkeypatch):
    client, request = configured_client(tmp_path, monkeypatch, allowed=("other",))
    opened = client.open_knowledge_context(request, budget=tiny_budget())
    packet = client.read_knowledge_context(opened["session_id"], "repos/other/doc.md", repository="other")
    assert packet["model_content"] and packet["knowledge_trace"]["status"] == "ready"
    report = client.knowledge_trace.get_report(packet["knowledge_trace"]["trace_id"])
    assert not report["exposures"]
    assert any(g["reason"] == "context_cross_repository_unobserved" for g in report["gaps"])
    denied = DirectClient(knowledge_context_path=tmp_path / "denied.sqlite", trace_root=tmp_path / "trace",
                          trace_mode="required")
    session = denied.open_knowledge_context(replace(request, review_id="denied"))["session_id"]
    with pytest.raises(ContextError, match="authorized"):
        denied.read_knowledge_context(session, "repos/other/doc.md", repository="other")


def test_adaptive_hash_validation_and_failed_capture_are_explicit(tmp_path, monkeypatch):
    client, request = configured_client(tmp_path, monkeypatch)
    session = client.open_knowledge_context(request)["session_id"]
    with pytest.raises(KnowledgeTraceError, match="hash mismatch"):
        client._trace_adaptive(session, {"model_content": "", "model_content_sha256": "0" * 64})
    trace_id = "kt-adaptive-" + session
    assert client.knowledge_trace.is_degraded(trace_id)
    restarted = DirectClient(knowledge_context_path=tmp_path / "context.sqlite", trace_root=tmp_path / "trace")
    assert restarted.knowledge_trace.get_report(trace_id)["trace_status"] == "partial"


def test_mcp_adaptive_context_tools_keep_trace_through_provider_restart(tmp_path, monkeypatch):
    from test_knowledge_trace_direct import fake_mcp

    _client, _request = configured_client(tmp_path, monkeypatch)
    monkeypatch.setenv("KNOWLEDGE_TRACE_ROOT", str(tmp_path / "mcp-trace"))
    # Keep the runtime context ledger in a deterministic location across tool registries.
    import infermatrix_copilot.sdk.v1.direct as direct_module
    def make_client(**kwargs):
        return DirectClient(**{**kwargs, "knowledge_context_path": tmp_path / "mcp-context.sqlite"})
    monkeypatch.setattr(direct_module, "DirectClient", make_client)
    tools = fake_mcp(monkeypatch)
    opened = tools["open_knowledge_context"](repo="r", source_pin=PIN, review_id="mcp-context")
    first = tools["search_knowledge_context"](opened["session_id"], "Preserve zero")
    trace_id = first["knowledge_trace"]["trace_id"]
    tools = fake_mcp(monkeypatch)
    retry = tools["search_knowledge_context"](opened["session_id"], "Preserve zero")
    assert retry["model_content"] == first["model_content"]
    assert retry["knowledge_trace"]["trace_id"] == trace_id
    report = tools["get_knowledge_trace"](trace_id, repo="r")
    assert report["exposures"]
    assert "error" in tools["get_knowledge_trace"](trace_id, repo="other")
    plan = tools["review"]("7", repo="r", knowledge_profile="adaptive", task_id="mcp-review",
                           expected_head_sha=PIN, title="API", body="API contract", changed_files=["src/api.py"])
    assert plan["knowledge_trace"]["trace_id"] != trace_id
    report = tools["get_knowledge_trace"](plan["knowledge_trace"]["trace_id"], repo="r")
    assert len(report["exposures"]) == len(packet_fragments(plan))
    assert all("content" not in row and "excerpt" not in row for row in plan["documents"])


# Reuse the signed-policy fixture to verify trace metadata participates in
# containment's exact-payload provenance, including after a client restart.
from test_kb_adaptive_containment import adaptive as containment_fixture


@pytest.mark.parametrize("operation", ["legacy_read", "read", "search", "related"])
def test_final_traced_sdk_payload_has_exact_containment_issuance(containment_fixture, operation):
    from infermatrix_copilot.knowledge_service.containment import digest, knowledge_usage_record
    from test_kb_adaptive_containment import FOLLOWUP

    c = containment_fixture
    client = DirectClient(knowledge_context_path=c.context_path, knowledge_maintenance=c.cfg,
                          trace_root=c.tmp_path / "trace", trace_mode="required")
    if operation == "legacy_read":
        plan = client.plan(c.request)
        packet = plan.to_dict()
        context_id = plan.review_context_id
        followup = client.read_document(FOLLOWUP, review_context_id=context_id).to_dict()
    else:
        packet = client.plan_adaptive(c.request)
        context_id = packet["review_context_id"]
        restarted = DirectClient(knowledge_context_path=c.context_path, knowledge_maintenance=c.cfg,
                                 trace_root=c.tmp_path / "trace", trace_mode="required")
        session = packet["session_id"]
        if operation == "read":
            followup = restarted.read_knowledge_context(session, FOLLOWUP)
        elif operation == "search":
            followup = restarted.search_knowledge_context(session, "UniqueFollowup values")
        else:
            followup = restarted.related_knowledge_context(session, ["src/followup.py"], query="UniqueFollowup")
    for delivered in (packet, followup):
        assert delivered["knowledge_trace"]["trace_id"]
        receipt = knowledge_usage_record(delivered, injected=True, config=c.cfg)
        assert receipt["context_sha256"] == digest(delivered)
    assert client._issued_contexts[context_id].knowledge_usage["context_sha256"] == digest(packet)
