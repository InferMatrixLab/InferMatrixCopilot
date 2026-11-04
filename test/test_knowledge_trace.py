"""Knowledge-use traces distinguish routing, delivery and declared application."""

from __future__ import annotations

import concurrent.futures
import gzip
import json
from pathlib import Path

import pytest

from infermatrix_copilot.knowledge_trace import KnowledgeTrace, KnowledgeTraceError, extract_rule_sections, markdown


DOC = "repos/demo/components/rules.md"
BODY = "### DEMO-1a — Preserve zero\n\nZero is an explicit request value.\n"


def session(tmp_path, **kwargs):
    recorder = KnowledgeTrace(tmp_path, mode="required")
    trace_id = recorder.start_session("demo", "direct", source_revision="head-1", knowledge_snapshot="snap-1", **kwargs)["trace_id"]
    return recorder, trace_id


def exposed(recorder, trace_id, **kwargs):
    return recorder.provide(trace_id, step_id="defaults", document_id=DOC, content=BODY,
                            boundary="provider_response", representation="full_document", **kwargs)["result"]


def assessment(exposure, **kwargs):
    return {"event_type": "assessment_recorded", "step_id": "defaults", "assessment": {
        "knowledge_ref_id": exposure["rule_refs"][0]["knowledge_ref_id"],
        "exposure_ids": [exposure["exposure_id"]], "applicability": "applicable", "verification": "supported",
        "rationale": "Zero must survive the default-value branch.", "check": "Run explicit-zero input through the consumer.",
        "evidence_ids": ["test-1"], "decision_ids": ["finding-2"], **kwargs}}


def test_full_chain_survives_restart_and_exports_evidence(tmp_path):
    recorder, trace_id = session(tmp_path, instrumentation={"host_context": "host_reported"})
    recorder.retrieve(trace_id, step_id="defaults", entries=[{"path": DOC, "reason": "guidance zero"}], mechanism="routing")
    exposure = exposed(recorder, trace_id)
    recorder.record_events(trace_id, [{"event_type": "evidence_registered", "step_id": "defaults", "evidence": {
        "evidence_id": "test-1", "kind": "test", "source_revision": "head-1", "command": "pytest test_zero.py",
        "result_summary": "one passed", "environment_fingerprint": "python-3.11", "content": "1 passed"}}, assessment(exposure)])
    recorder.finish_session(trace_id)
    other = KnowledgeTrace(tmp_path, mode="required")
    report = other.report(trace_id)
    assert report["schema"] == "knowledge-use-report/1"
    assert report["status"] == report["trace_status"] == "complete"
    assert report["counts"]["provided_rules"] == report["counts"]["assessments"] == 1
    assert report["counts"]["unassessed_rules"] == 0
    assert report["exposures"][0]["rule_refs"][0]["coverage"] == "body_complete"
    text = markdown(report)
    for expected in ("guidance zero", "pytest test_zero.py", "one passed", "python-3.11", "finding-2", "Run explicit-zero"):
        assert expected in text
    assert other.query(repository="demo", rule_id="DEMO-1a")


def test_retrieval_is_not_exposure_and_quick_map_is_not_rule_body(tmp_path):
    recorder, trace_id = session(tmp_path)
    recorder.retrieve(trace_id, step_id="route", entries=[{"path": DOC, "rule_ids": ["DEMO-1a"]}], mechanism="route")
    assert recorder.report(trace_id)["counts"]["exposures"] == 0
    recorder.provide(trace_id, step_id="route", document_id=DOC, content="- DEMO-1a: preserve zero",
                     boundary="provider_response", representation="quick_map", rule_ids=["DEMO-1a"])
    report = recorder.report(trace_id)
    assert report["exposures"][0]["rule_refs"][0]["coverage"] == "reference_only"
    assert report["counts"]["unassessed_rules"] == 1
    assert not report["assessments"]


def test_partial_body_and_utf8_ranges_are_precise(tmp_path):
    recorder, trace_id = session(tmp_path)
    event = recorder.provide(trace_id, step_id="read", document_id=DOC, content=BODY,
        boundary="model_request", representation="excerpt", truncated=True, offset=9, offset_unit="utf8_bytes")
    exposure = event["result"]["exposure"]
    assert exposure["included_ranges"] == [[9, 9 + len(BODY.encode("utf-8"))]]
    assert exposure["rule_refs"][0]["coverage"] == "body_excerpt"


def test_unknown_and_split_utf8_source_ranges_are_not_fabricated(tmp_path):
    recorder, trace_id = session(tmp_path)
    unknown = exposed(recorder, trace_id, offset=None)["exposure"]
    assert unknown["included_ranges"] == [] and unknown["range_status"] == "unknown"
    split = recorder.provide(trace_id, step_id="read", document_id=DOC,
        content="�", boundary="provider_response", offset=7,
        offset_unit="utf8_bytes", source_length=1)["result"]["exposure"]
    assert split["included_ranges"] == [[7, 8]]


def test_host_corrections_cannot_remove_observed_content_or_evidence(tmp_path):
    recorder, trace_id = session(tmp_path)
    exposure = exposed(recorder, trace_id)
    content_event = recorder.report(trace_id)["events"][-1]["result"]["event_id"]
    evidence_event = recorder.record_events(trace_id, [{"event_type": "evidence_registered",
        "evidence": {"evidence_id": "test-1", "kind": "test", "source_revision": "head-1"}}])["events"][0]
    for event in [
        {"event_type": "content_provided", "supersedes": content_event,
         "exposure": {"source_exposure_id": exposure["exposure_id"], "boundary": "host_context"}},
        {"event_type": "evidence_registered", "supersedes": evidence_event["result"]["event_id"],
         "evidence": {"evidence_id": "replacement", "kind": "test"}},
    ]:
        with pytest.raises(KnowledgeTraceError, match="immutable"):
            recorder.record_events(trace_id, [event])
    recorder.record_events(trace_id, [assessment(exposure)])
    report = recorder.report(trace_id)
    assert report["evidence"][0]["evidence_id"] == "test-1"
    assert report["assessments"][0]["evidence_ids"] == ["test-1"]


def test_host_entity_ids_cannot_retarget_existing_report_links(tmp_path):
    recorder, trace_id = session(tmp_path)
    first = exposed(recorder, trace_id)
    second = recorder.provide(trace_id, step_id="other", document_id="repos/demo/other.md",
        content=BODY, boundary="provider_response")["result"]
    receipt = {"event_type": "content_provided", "step_id": "defaults", "exposure": {
        "source_exposure_id": first["exposure_id"], "exposure_id": "host-1", "boundary": "host_context"}}
    declaration = assessment(first, assessment_id="assessment-1", verification="unresolved", evidence_ids=[])
    recorder.record_events(trace_id, [receipt, declaration])
    with pytest.raises(KnowledgeTraceError, match="exposure_id conflicts"):
        recorder.record_events(trace_id, [{**receipt, "exposure": {
            **receipt["exposure"], "source_exposure_id": second["exposure_id"]}}])
    with pytest.raises(KnowledgeTraceError, match="assessment_id conflicts"):
        recorder.record_events(trace_id, [assessment(first, assessment_id="assessment-1",
            verification="unresolved", evidence_ids=[], rationale="A different assertion")])
    report = recorder.report(trace_id)
    assert next(e for e in report["exposures"] if e["exposure_id"] == "host-1")["document_id"] == DOC
    assert len(report["assessments"]) == 1


def test_full_usage_batch_retry_is_idempotent_after_normalization(tmp_path):
    recorder, trace_id = session(tmp_path)
    first = exposed(recorder, trace_id)
    events = [
        {"event_type": "content_provided", "exposure": {
            "source_exposure_id": first["exposure_id"], "boundary": "host_context"}},
        {"event_type": "evidence_registered", "evidence": {
            "evidence_id": "test-1", "kind": "test", "source_revision": "head-1", "content": "one passed"}},
        assessment(first),
    ]
    original = recorder.record_events(trace_id, events, idempotency_key="whole-batch")
    retried = recorder.record_events(trace_id, events, idempotency_key="whole-batch")
    assert [e["id"] for e in retried["events"]] == [e["id"] for e in original["events"]]
    assert len(recorder.report(trace_id)["assessments"]) == 1


@pytest.mark.parametrize("bad_event", [
    {"event_type": "gap_recorded", "step_id": ["bad"], "gap": {"reason": "invalid"}},
    {"event_type": "gap_recorded", "step_id": "bad\nstep", "gap": {"reason": "invalid"}},
])
def test_invalid_steps_cannot_poison_persisted_reports(tmp_path, bad_event):
    recorder, trace_id = session(tmp_path)
    with pytest.raises(KnowledgeTraceError, match="step_id"):
        recorder.record_events(trace_id, [bad_event])
    assert recorder.report(trace_id)["counts"]["events"] == 1


def test_engine_moa_step_labels_are_opaque_and_preserved(tmp_path):
    recorder, trace_id = session(tmp_path)
    step_id = "agent.answer_issue#moa/provider/model:reasoning"
    recorder.retrieve(trace_id, step_id=step_id, entries=[{"path": DOC}], mechanism="routing")
    recorder.provide(trace_id, step_id=step_id, document_id=DOC, content=BODY,
                     boundary="model_request", representation="full_document")
    report = recorder.report(trace_id)
    assert report["steps"][0]["step_id"] == step_id
    assert report["retrievals"][0]["step_id"] == step_id
    assert report["exposures"][0]["step_id"] == step_id


def test_rule_parser_ignores_mentions_and_fenced_examples():
    content = "DEMO-2b appears here\n```md\n### DEMO-3c — example\nbody\n```\n" + BODY
    assert [s["rule_id"] for s in extract_rule_sections(content)] == ["DEMO-1a"]
    assert not extract_rule_sections("### Rule DEMO-4d\nbody\n")
    nested = "## DIFF-2af — parent\nParent.\n### DIFF-4aa — child\nChild.\n## Next\nAfter.\n"
    sections = extract_rule_sections(nested)
    assert [s["rule_id"] for s in sections] == ["DIFF-2af", "DIFF-4aa"]
    assert all(nested[s["end"]:].startswith("## Next") for s in sections)


@pytest.mark.parametrize("path", ["../general/rules.md", "/general/rules.md", "repos/other/rules.md", "general/../rules.md", "general\\rules.md"])
def test_repository_scope_cannot_be_escaped(tmp_path, path):
    recorder, trace_id = session(tmp_path)
    with pytest.raises(KnowledgeTraceError):
        recorder.provide(trace_id, step_id="read", document_id=path, content=BODY, boundary="provider_response")


def test_navigation_shared_general_and_repository_namespace_are_distinct(tmp_path):
    recorder, trace_id = session(tmp_path)
    for path in ("AGENTS.md", "README.md", "repos/_index.md", "general/review/rules.md"):
        recorder.provide(trace_id, step_id="read", document_id=path, content=BODY, boundary="provider_response")
    assert len(recorder.report(trace_id)["references"]) == 8


def test_host_receipt_preserves_exact_source_and_rejects_identity_override(tmp_path):
    recorder, trace_id = session(tmp_path)
    exposure = exposed(recorder, trace_id)
    receipt = {"event_type": "content_provided", "step_id": "prompt", "exposure": {
        "boundary": "host_context", "source_exposure_id": exposure["exposure_id"]}}
    result = recorder.record_events(trace_id, [receipt])["events"][0]["result"]["exposure"]
    assert result["payload_ref"] == exposure["exposure"]["payload_ref"]
    assert result["reference"] == exposure["exposure"]["reference"]
    assert result["boundary"] == "host_context" and result["reported_by"] == "host"
    for key, value in (("reference", {}), ("payload_ref", "bad"), ("rule_refs", []), ("document_id", "repos/other/rules.md"), ("included_ranges", [[0, 1]])):
        receipt["exposure"][key] = value
        with pytest.raises(KnowledgeTraceError, match="cannot override"):
            recorder.record_events(trace_id, [receipt])
        del receipt["exposure"][key]


def test_invalid_assessments_and_missing_evidence_are_refused(tmp_path):
    recorder, trace_id = session(tmp_path)
    exposure = exposed(recorder, trace_id)
    for changes in ({"knowledge_ref_id": "unknown"}, {"exposure_ids": ["unknown"]}, {"evidence_ids": []},
                    {"evidence_ids": ["unknown"]}, {"rationale": ""}, {"check": ""}):
        with pytest.raises(KnowledgeTraceError):
            recorder.record_events(trace_id, [assessment(exposure, **changes)])
    assert not recorder.report(trace_id)["assessments"]


def test_stale_evidence_is_visible_and_weakens_capture_status(tmp_path):
    recorder, trace_id = session(tmp_path)
    exposure = exposed(recorder, trace_id)
    recorder.record_events(trace_id, [{"event_type": "evidence_registered", "evidence": {
        "evidence_id": "test-1", "source_revision": "old-head", "kind": "test"}}, assessment(exposure)])
    recorder.finish_session(trace_id)
    report = recorder.report(trace_id)
    assert report["evidence"][0]["revision_status"] == "stale"
    assert report["trace_status"] == "partial"
    assert any(g["reason"] == "assessment_evidence_revision_stale" for g in report["gaps"])


def test_snapshot_and_session_identity_are_immutable(tmp_path):
    recorder, trace_id = session(tmp_path, trace_id="session-fixed")
    with pytest.raises(KnowledgeTraceError, match="immutable"):
        recorder.start_session("other", "direct", trace_id=trace_id)
    with pytest.raises(KnowledgeTraceError, match="snapshot"):
        exposed(recorder, trace_id, snapshot="snap-2")


def test_duplicate_delivery_batches_are_idempotent_across_instances(tmp_path):
    recorder, trace_id = session(tmp_path)
    exposure = exposed(recorder, trace_id)
    events = [{"event_type": "content_provided", "step_id": "prompt", "exposure": {
        "boundary": "host_context", "source_exposure_id": exposure["exposure_id"]}}]
    first = recorder.record_events(trace_id, events, idempotency_key="host-1")
    second = KnowledgeTrace(tmp_path, mode="required").record_events(trace_id, events, idempotency_key="host-1")
    assert first == second
    assert recorder.report(trace_id)["counts"]["exposures"] == 2
    events[0]["step_id"] = "different"
    with pytest.raises(KnowledgeTraceError, match="conflicts"):
        recorder.record_events(trace_id, events, idempotency_key="host-1")


def test_duplicate_evidence_is_idempotent_and_batches_link_assessments(tmp_path):
    recorder, trace_id = session(tmp_path)
    exposure = exposed(recorder, trace_id)
    events = [{"event_type": "evidence_registered", "evidence": {
        "evidence_id": "test-1", "kind": "test", "source_revision": "head-1", "content": "passed"}}, assessment(exposure)]
    first = recorder.record_events(trace_id, events, idempotency_key="batch")
    second = KnowledgeTrace(tmp_path, mode="required").record_events(trace_id, events, idempotency_key="batch")
    assert first == second


def test_wall_clock_does_not_define_session_causality(tmp_path):
    recorder = KnowledgeTrace(tmp_path, mode="required")
    times = iter([100, 100, 99, 98])
    recorder.store._clock = lambda: next(times)
    trace_id = recorder.start_session("demo", "strict")["trace_id"]
    exposed(recorder, trace_id)
    recorder.record_events(trace_id, [{"event_type": "gap_recorded", "gap": {"reason": "native_reads_unknown"}}])
    recorder.finish_session(trace_id)
    report = recorder.report(trace_id)
    assert [e["result"]["session_seq"] for e in report["events"]] == [1, 2, 3, 4]
    assert report["status"] == "complete"


def _write_duplicate(root, trace_id):
    return KnowledgeTrace(root, mode="required").record_events(trace_id, [{"event_type": "gap_recorded",
        "step_id": "x", "gap": {"reason": "native_file_reads_unknown"}}], idempotency_key="parallel")["events"][0]["id"]


def test_cross_process_duplicate_append_is_serialized(tmp_path):
    _, trace_id = session(tmp_path)
    with concurrent.futures.ProcessPoolExecutor(max_workers=3) as pool:
        ids = list(pool.map(_write_duplicate, [str(tmp_path)] * 6, [trace_id] * 6))
    assert len(set(ids)) == 1
    assert KnowledgeTrace(tmp_path).report(trace_id)["counts"]["events"] == 2


def test_index_rebuild_and_missing_blobs_preserve_honest_report(tmp_path):
    recorder, trace_id = session(tmp_path)
    exposed(recorder, trace_id)
    before = recorder.report(trace_id)
    (tmp_path / "index.db").unlink()
    other = KnowledgeTrace(tmp_path, mode="required")
    assert other.report(trace_id) == before
    for path in (tmp_path / "blobs").rglob("*.gz"):
        path.unlink()
    report = other.report(trace_id)
    assert report["exposures"][0]["content_available"] is False
    assert report["gaps"][0]["reason"] == "archived_content_unavailable"


def test_redaction_reaches_metadata_and_content(tmp_path, monkeypatch):
    secret = "private-value-123456789"
    monkeypatch.setenv("TEST_API_KEY", secret)
    recorder, trace_id = session(tmp_path)
    recorder.provide(trace_id, step_id="read", document_id=DOC, content=BODY + secret, boundary="provider_response")
    recorder.record_events(trace_id, [{"event_type": "gap_recorded", "gap": {"reason": secret}}])
    raw = b"".join(p.read_bytes() for p in (tmp_path / "records").glob("*.jsonl"))
    raw += b"".join(gzip.decompress(p.read_bytes()) for p in (tmp_path / "blobs").rglob("*.gz"))
    assert secret.encode() not in raw
    assert b"[REDACTED]" in raw


def test_off_storage_failures_required_policy_and_report_paging(tmp_path):
    off_root = tmp_path / "off"
    off = KnowledgeTrace(off_root, mode="off")
    assert off.start_session("demo", "direct")["status"] == "off"
    assert not off_root.exists()
    bad_root = tmp_path / "file"
    bad_root.write_text("not a directory")
    assert KnowledgeTrace(bad_root, mode="best_effort").start_session("demo", "direct")["status"] == "degraded"
    with pytest.raises(KnowledgeTraceError, match="required"):
        KnowledgeTrace(bad_root, mode="required").start_session("demo", "direct")
    recorder, trace_id = session(tmp_path / "good")
    exposed(recorder, trace_id)
    first = recorder.report(trace_id, limit=1)
    assert first["counts"]["events"] == 2 and len(first["events"]) == 1
    assert first["next_cursor"] == "1"
    second = recorder.report(trace_id, cursor=first["next_cursor"], limit=1)
    assert second["next_cursor"] is None
    assert second["events"][0]["result"]["event_type"] == "content_provided"
    for limit in (0, -1, 1001, True):
        with pytest.raises(KnowledgeTraceError):
            recorder.report(trace_id, limit=limit)
