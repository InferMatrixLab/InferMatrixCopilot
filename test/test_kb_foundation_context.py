"""Offered foundation evidence is complete, bounded and independently replayable."""
import copy
import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service import init_knowledge_parallel as parallel
from infermatrix_copilot.kb_service.init_knowledge import _Knowledge
from infermatrix_copilot.kb_service.init_knowledge_inputs import (
    SYSTEM_KNOWLEDGE, SYSTEM_KNOWLEDGE_V4, foundation_evidence, knowledge_prompt, knowledge_system,
)
from infermatrix_copilot.kb_service.init_support import InitError, InitRecord
from test_kb_foundation_parallel import _complete_native
from test_kb_init_modules import _modules_lifecycle
from test_kb_init_skeleton import _runtime, _tree, world  # noqa: F401


@pytest.mark.parametrize("ranges,start,end,expected", [
    ([(315, 530), (1, 314)], 298, 321, True),
    ([(19, 100), (1, 346), (347, 603)], 343, 412, True),
    ([(1, 20), (19, 40), (41, 80)], 12, 79, True),
    ([(15, 42), (44, 79)], 42, 44, False),
    ([(1, 165)], 159, 166, False),
    ([(1, 3), (8, 10)], 4, 8, False),
])
def test_adjacent_and_overlap_union_preserves_real_gaps(ranges, start, end, expected):
    original = copy.deepcopy(ranges)
    assert parallel.range_is_offered(ranges, start, end) is expected
    assert ranges == original  # Stored old input hashes must not change.


def _packet():
    sources = {"src/large.py": "\n".join("x" * 110 for _ in range(180)),
               "src/helper.py": "def cleanup():\n    return 'clean'\n",
               "docs/guide.md": "# Guide\nCall cleanup after the representative path.\n"}
    stage = SimpleNamespace(record=InitRecord("knowledge", "toy", pin="a" * 40),
        lifecycle=SimpleNamespace(full_name="o/toy"),
        observer=SimpleNamespace(file_text=lambda pin, path: sources.get(path)))
    def item(path, start, end):
        lines = sources[path].splitlines()
        return {"path": path, "start": start, "end": end, "total_lines": len(lines),
                "language": "python", "text": "\n".join(f"{n}: {lines[n-1]}" for n in range(start, end+1))}
    payload = {"repository": "o/toy", "pin": "a" * 40, "foundation_prompt_version": 4,
               "files": [item("src/large.py", 1, 180), item("src/helper.py", 1, 2)],
               "docs": [item("docs/guide.md", 1, 2)]}
    return stage, payload


def test_full_offered_packet_keeps_late_lines_and_non_anchor_files_without_extra_reads():
    stage, payload = _packet()
    packet = foundation_evidence(stage, payload)
    generated = json.loads(knowledge_prompt(payload).split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
    assert [p["text"] for p in packet] == [p["text"] for p in generated["files"] + generated["docs"]]
    assert packet[0]["text"][-1].startswith("180: ")
    assert len("\n".join(packet[0]["text"]).encode()) > 8192
    assert packet[1]["path"] == "src/helper.py" and packet[-1]["source_kind"] == "docs"
    assert all("@" + stage.record.pin + ":" in p["source_reference"] for p in packet)


@pytest.mark.parametrize("damage", ["text", "end", "total_lines", "pin", "source_cap", "doc_cap"])
def test_full_packet_tamper_and_original_generation_caps_fail_closed(damage):
    stage, payload = _packet()
    if damage == "text":
        payload["files"][1]["text"] = "1: def cleanup():\n2:     return 'tampered'"
    elif damage == "end":
        payload["files"][1]["end"] = 3
    elif damage == "total_lines":
        payload["files"][1]["total_lines"] = 100
    elif damage == "pin":
        payload["pin"] = "b" * 40
    elif damage == "source_cap":
        payload["files"] = payload["files"][:1] * 6
    else:
        payload["docs"] = payload["files"][:1] * 2
    with pytest.raises(InitError, match="foundation shown packet"):
        foundation_evidence(stage, payload)


def test_packet_read_failure_is_unknown_before_generator_dispatch():
    from infermatrix_copilot.knowledge_service.facts import FactsError
    stage, payload = _packet()
    def unavailable(*args):
        raise FactsError("unreadable fixed snapshot")
    stage.observer.file_text = unavailable
    with pytest.raises(InitError, match="cannot read pinned source"):
        foundation_evidence(stage, payload)


def test_bad_offered_packet_cannot_dispatch_either_model():
    from infermatrix_copilot.kb_service.init_budget import Budget
    from infermatrix_copilot.kb_service.init_coverage import Owner
    stage, payload = _packet()
    payload.update(owner="sample", facets=["api"])
    payload["files"][1]["text"] = "1: fabricated line\n2: fabricated line"
    calls = []
    stage.rt = SimpleNamespace(gateway=SimpleNamespace(call_json=lambda *a, **k: calls.append(k)),
                               unlimited_subscription=True)
    stage.head, stage.STAGE, stage.budget = {}, "knowledge", Budget(None)
    result = parallel._worker(stage, {"payload": payload, "offered": parallel.offered_ranges(payload["files"] + payload["docs"]),
        "owner": Owner("sample", "repos/toy/sample.md", ()), "page": "repos/toy/sample.md", "requested": ["api"]})
    assert calls == [] and result["artifacts"] == []
    assert "differs from pinned source" in result["error"]


def _audit_stage(rt, record):
    stage = _Knowledge(rt=rt, lifecycle=_modules_lifecycle(), dry_run=True, pin=record.pin)
    stage.repo_dir = stage.lifecycle.knowledge_dir
    stage._base_sha = record.kb_base_sha
    stage._knowledge_run_pin = record.pin
    stage.overlay = {**_tree(InitRecord.load(rt.state_dir, "toy", "skeleton")),
                     **_tree(InitRecord.load(rt.state_dir, "toy", "modules"))}
    stage.base = rt.knowledge.knowledge_files(record.kb_base_sha)
    return stage


def _native_payload(traces, artifact, role):
    native = traces.get(artifact[role + "_receipt"]["native_trace_id"])
    prompt = traces.blob(native["inputs"]["prompt"])
    payload = json.loads(prompt.split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
    return native, payload


def test_version4_native_generator_and_judge_bind_the_same_full_packet_and_resume(world):
    rt, record, offline, traces = _complete_native(world)
    before = InitRecord.path(rt.state_dir, "toy", "knowledge").read_bytes()
    calls = len(offline.calls)
    for task in record.coverage["foundation_jobs"]["tasks"].values():
        assert task["payload"]["foundation_prompt_version"] == 4
        for artifact in task["artifacts"]:
            generator, gen = _native_payload(traces, artifact, "generator")
            judge, judged = _native_payload(traces, artifact, "judge")
            assert task["payload"]["foundation_generation_policy_version"] == 1
            assert traces.blob(generator["inputs"]["system"]) == knowledge_system(task["payload"])
            assert [p["text"] for p in judged["evidence"]] == [p["text"] for p in gen["files"] + gen["docs"]]
            assert [p["path"] for p in judged["evidence"]] == [p["path"] for p in gen["files"] + gen["docs"]]
    parallel.validate_checkpoint(_audit_stage(rt, record), record, record.inputs_digest)
    assert len(offline.calls) == calls and InitRecord.path(rt.state_dir, "toy", "knowledge").read_bytes() == before


def test_real_historical_version3_native_receipts_remain_replayable_without_reclassification(world, monkeypatch):
    worker, options = parallel._worker, _Knowledge._input_options
    def legacy_worker(stage, job):
        job = copy.deepcopy(job)
        job["payload"].pop("foundation_prompt_version", None)
        job["payload"].pop("foundation_generation_policy_version", None)
        return worker(stage, job)
    def legacy_options(stage):
        legacy = {**options(stage), "knowledge_prompt_version": 3}
        legacy.pop("foundation_generation_policy_version")
        return legacy
    with monkeypatch.context() as old:
        old.setattr(parallel, "_worker", legacy_worker)
        old.setattr(_Knowledge, "_input_options", legacy_options)
        rt, record, offline, traces = _complete_native(world)
    before = InitRecord.path(rt.state_dir, "toy", "knowledge").read_bytes()
    calls = len(offline.calls)
    for task in record.coverage["foundation_jobs"]["tasks"].values():
        assert "foundation_prompt_version" not in task["payload"]
        artifact = task["artifacts"][0]
        native, gen = _native_payload(traces, artifact, "generator")
        assert traces.blob(native["inputs"]["system"]) == SYSTEM_KNOWLEDGE
        _, judged = _native_payload(traces, artifact, "judge")
        assert isinstance(judged["evidence"][0]["text"], str)
    stage = _audit_stage(rt, record)
    assert stage._input_options()["knowledge_prompt_version"] == 4
    parallel.validate_checkpoint(stage, record, record.inputs_digest)
    assert len(offline.calls) == calls and InitRecord.path(rt.state_dir, "toy", "knowledge").read_bytes() == before


def test_native_version4_rejects_genuine_judge_receipt_with_the_old_narrow_packet(world, monkeypatch):
    from infermatrix_copilot.kb_service.init_stages import _Stage
    # Construct actual native receipts for an old-shaped judge packet, then
    # verify them with production validation enabled. All three yes answers
    # cannot substitute for binding the required version4 review input.
    with monkeypatch.context() as fixture:
        fixture.setattr(_Knowledge, "_judge_evidence", _Stage._judge_evidence)
        fixture.setattr(parallel, "_validate_native", lambda *args: None)
        rt, record, offline, traces = _complete_native(world)
    before = InitRecord.path(rt.state_dir, "toy", "knowledge").read_bytes()
    calls = len(offline.calls)
    with pytest.raises(InitError, match="native approval binding mismatch"):
        parallel.validate_checkpoint(_audit_stage(rt, record), record, record.inputs_digest)
    assert len(offline.calls) == calls and InitRecord.path(rt.state_dir, "toy", "knowledge").read_bytes() == before


def test_adjacent_chunk_citation_is_actually_reviewed_and_native_replayable(world, monkeypatch):
    from test_kb_init_knowledge import KnowledgeGateway
    worker, call = parallel._worker, KnowledgeGateway.call_json
    def split_worker(stage, job):
        job = copy.deepcopy(job)
        source = job["payload"]["files"][0]
        lines = source["text"].splitlines()
        if len(lines) >= 2:
            job["payload"]["files"][:1] = [
                {**source, "end": 1, "text": lines[0]},
                {**source, "start": 2, "text": "\n".join(lines[1:])}]
            job["offered"] = parallel.offered_ranges(job["payload"]["files"] + job["payload"]["docs"])
        return worker(stage, job)
    def spanning_call(self, role, **kwargs):
        reply = call(self, role, **kwargs)
        if kwargs["system"] == knowledge_system({"foundation_prompt_version": 4, "foundation_generation_policy_version": 1}):
            payload = json.loads(kwargs["prompt"].split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
            first = payload["files"][0]
            end = max(f["end"] for f in payload["files"] if f["path"] == first["path"])
            for section in reply.data["sections"]:
                section["evidence"] = [{"path": first["path"], "start": 1, "end": end}]
        return reply
    monkeypatch.setattr(parallel, "_worker", split_worker)
    monkeypatch.setattr(KnowledgeGateway, "call_json", spanning_call)
    rt, record, offline, traces = _complete_native(world)
    assert len(record.evidence) == 12
    assert all(len(t["offered"][t["payload"]["files"][0]["path"]]) == 2
               for t in record.coverage["foundation_jobs"]["tasks"].values())
    parallel.validate_checkpoint(_audit_stage(rt, record), record, record.inputs_digest)


def test_independent_rejection_is_not_promoted_by_extra_offered_context(world):
    from test_kb_breadth_subscription import SubscriptionKnowledgeGateway, GENERATOR
    from test_kb_init_knowledge import _chain
    from infermatrix_copilot.kb_service.init_stages import run_stage
    gateway = SubscriptionKnowledgeGateway(rejected=True)
    _chain(world, gateway)
    record = run_stage(_runtime(world, gateway, generator=GENERATOR), _modules_lifecycle(),
                       "knowledge", dry_run=True, unlimited_subscription=True)
    assert all(not key.endswith(":architecture") for key in record.evidence)
    assert all(v["verdict"] == "fail" for key, v in record.verdicts.items() if key.endswith(":architecture"))
    assert all(a["facet"] != "architecture" for t in record.coverage["foundation_jobs"]["tasks"].values()
               for a in t["artifacts"])
