"""Semantic targets count native approved knowledge without inventing capabilities."""

import json
import re
from dataclasses import replace

import pytest
import yaml

from infermatrix_copilot.kb_service.init_stages import _page_frontmatter
from infermatrix_copilot.kb_service.knowledge_coverage import CoveragePolicy, Feature, feature_metadata, load_policy
from infermatrix_copilot.kb_service.knowledge_depth import (
    FACETS, audit_depth, build_absence_certificate, depth_page, digest, render_block, verified_blocks,
)
from infermatrix_copilot.knowledge_docs import KnowledgeDocs
from infermatrix_copilot.knowledge_service.lifecycle import DEPTH_BLOCK, depth_sections
from test_kb_init_skeleton import _commit, _git, _runtime, _tree, world  # noqa: F401
from test_kb_init_modules import _modules_lifecycle
from test_kb_knowledge_depth import DepthGateway, _baseline, _complete_breadth, _run

PIN = "a" * 40
CORE = "def run(value):\n    return helper(value)\ndef helper(value):\n    return value + 1\n"


def _policy(features=1, threshold=0.90):
    entries = tuple(Feature(f"f{i}", f"Feature {i}", "core", ("pkg/core.py",), ("docs/guide.md",),
                            f"repos/demo/components/core/feature-f{i}.md", ("pkg/core.py",)) for i in range(features))
    return CoveragePolicy(("pkg/",), ("*/tests/*",), (".py",), entries,
                          semantic_depth_per_facet_gt=threshold)


def _source(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/core.py").write_text(CORE)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("# Feature guide\nThe feature returns an incremented value.\n")


def _section(facet):
    section = {"facet": facet, "title": facet, "body": "run calls helper and returns the incremented value.",
               "interpretation": "fact", "evidence": [{"path": "pkg/core.py", "start": 1, "end": 4}]}
    if facet == "flow":
        section["trace"] = [{"path": "pkg/core.py", "symbol": "run", "start": 1, "end": 2},
                            {"path": "pkg/core.py", "symbol": "helper", "start": 3, "end": 4}]
    return section


def _page(blocks):
    return _page_frontmatter("Depth", kind="architecture", today="2026-10-02", tags=["demo"]) + "\n\n".join(blocks)


def _replace_proof(block, edit):
    match = DEPTH_BLOCK.fullmatch(block)
    body = match[5]
    proof = re.search(r"<!-- kb:depth-proof (.*?) -->", body)
    data = json.loads(proof[1])
    edit(data)
    changed = body.replace(proof[1], json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return block.replace(body, changed).replace(match[4], digest(changed))


@pytest.mark.parametrize("count,met", [(71, False), (72, True)])
def test_target_is_strict_per_dimension_with_fixed_79_feature_denominator(tmp_path, count, met):
    _source(tmp_path)
    policy = _policy(79)
    head = {}
    for i, feature in enumerate(policy.features):
        blocks = [render_block(feature, _section(facet), tmp_path, "o/demo", PIN)
                  for j, facet in enumerate(FACETS) if (i + j * 11) % 79 < count]
        head[depth_page(feature)] = _page(blocks)
    report = audit_depth(head, tmp_path, policy, PIN)
    assert report["total_facets"] == 553
    assert report["recognized_feature_count"] == 79
    assert all(row["recognized"] == count for row in report["facet_counts"].values())
    assert report["target_met"] is met
    assert report["recognized_facets"] == count * 7


def test_total_target_cannot_hide_a_dimension_below_90_percent(tmp_path):
    _source(tmp_path)
    policy = _policy(79)
    head = {}
    for i, feature in enumerate(policy.features):
        blocks = [render_block(feature, _section(facet), tmp_path, "o/demo", PIN)
                  for j, facet in enumerate(FACETS) if facet != "flow" or i < 71]
        head[depth_page(feature)] = _page(blocks)
    report = audit_depth(head, tmp_path, policy, PIN)
    assert report["recognized_facets"] > 504
    assert report["facet_counts"]["flow"]["recognized"] == 71
    assert not report["target_met"]


@pytest.mark.parametrize("value", [True, -0.1, 1, float("nan"), float("inf"), "0.9"])
def test_semantic_policy_rejects_unreachable_or_non_numeric_threshold(value):
    feature = _policy().features[0]
    data = {"schema_version": 1, "core": {"roots": ["pkg/"], "exclude": ["*/tests/*"]},
            "semantic_depth": {"per_facet_gt": value}, "features": [
                {"id": feature.id, "title": feature.title, "owner": feature.owner,
                 "source_globs": list(feature.source_globs), "docs": list(feature.docs), "page": feature.page}]}
    with pytest.raises(ValueError, match="semantic_depth"):
        load_policy(yaml.safe_dump(data), "repos/demo")
    data.pop("semantic_depth")
    assert load_policy(yaml.safe_dump(data), "repos/demo").semantic_depth_per_facet_gt is None


def test_legacy_supported_proof_is_default_and_preserved_byte_for_byte(tmp_path):
    _source(tmp_path)
    policy = _policy()
    feature = policy.features[0]
    block = render_block(feature, _section("api"), tmp_path, "o/demo", PIN)
    legacy = _replace_proof(block, lambda proof: proof.pop("basis"))
    blocks, problems = verified_blocks(_page([legacy]), feature, tmp_path, PIN, policy=policy)
    assert not problems and blocks == {"api": legacy}
    assert depth_sections(_page([legacy]))[0]["basis"] == "supported"
    report = audit_depth({depth_page(feature): _page([legacy])}, tmp_path, policy, PIN)
    assert report["covered_facets"] == report["recognized_facets"] == 1
    assert report["features"][feature.id]["facets"] == ["api"]


def test_repair_ledger_preserves_specific_failures_across_resume_without_duplicate_events():
    from infermatrix_copilot.kb_service.init_knowledge_depth import _KnowledgeDepth

    entry = {"attempts": 1, "context_sha256": "context-1", "evidence_sha256": "same-source"}
    _KnowledgeDepth._slot_result(entry, "validation", "invalid", "assertion crosses an unshown source gap")
    _KnowledgeDepth._slot_result(entry, "validation", "unjudged", "outer extraction failed")
    entry = json.loads(json.dumps(entry))
    entry.update(attempts=2, context_sha256="context-2")
    _KnowledgeDepth._slot_result(entry, "validation", "omitted", "generator omitted the requested test facet")
    entry.update(attempts=3, review_attempts=1, context_sha256="context-3")
    _KnowledgeDepth._slot_result(entry, "validation", "fail", "the test asserts another feature's behavior")
    slot = entry["facets"]["validation"]
    assert [event["status"] for event in slot["history"]] == ["invalid", "omitted", "fail"]
    assert slot["history"][0]["reason"] == "assertion crosses an unshown source gap"
    assert len(slot["history"]) == 3 and slot.get("blocked")
    entry.update(attempts=4, review_attempts=2, evidence_sha256="new-direct-test", context_sha256="context-4")
    _KnowledgeDepth._slot_result(entry, "validation", "pass", "the new assertion supports the corrected claim")
    assert "blocked" not in slot
    assert len(slot["history"]) == 4 and slot["history"][-1]["evidence_sha256"] == "new-direct-test"


def _absence(tmp_path, policy):
    feature = policy.features[0]
    certificate = build_absence_certificate(tmp_path, policy, feature, PIN)
    assert certificate
    section = _section("validation")
    section.update(basis="verified_absent", absence_certificate=certificate,
                   body="固定源码树内没有可静态关联到该功能的自动化测试入口；外部及动态覆盖仍未验证。")
    return render_block(feature, section, tmp_path, "o/demo", PIN, policy=policy)


def test_verified_absence_counts_as_recognition_but_not_positive_test_coverage(tmp_path):
    _source(tmp_path)
    policy = _policy()
    block = _absence(tmp_path, policy)
    report = audit_depth({depth_page(policy.features[0]): _page([block])}, tmp_path, policy, PIN)
    assert report["covered_facets"] == 0 and report["recognized_facets"] == 1
    assert report["facet_counts"]["validation"] == {"supported": 0, "verified_absent": 1,
        "unknown": 0, "recognized": 1, "ratio": 1.0, "target_met": True,
        "strict_recognized": 1, "lightweight_recognized": 0, "eligible_recognized": 1, "eligible_ratio": 1.0}
    assert report["features"]["f0"]["missing_facets"] == list(FACETS)
    assert "validation" not in report["features"]["f0"]["unknown_facets"]
    assert not report["production_files_with_semantic_evidence"]
    assert verified_blocks(_page([block]), policy.features[0], tmp_path, PIN)[1]


@pytest.mark.parametrize("change", ["new_test", "source", "policy", "certificate", "docs"])
def test_absence_replay_rejects_new_test_edited_scope_or_forged_search(tmp_path, change):
    _source(tmp_path)
    policy = _policy()
    feature = policy.features[0]
    block = _absence(tmp_path, policy)
    if change == "new_test":
        (tmp_path / "sdk/tests").mkdir(parents=True)
        (tmp_path / "sdk/tests/test_core.py").write_text("from pkg.core import run\ndef test_run():\n    assert run(1) == 2\n")
    elif change == "source":
        (tmp_path / "pkg/core.py").write_text(CORE + "# source scope changed\n")
    elif change == "policy":
        policy = replace(policy, exclude=("*/test/*",))
    elif change == "docs":
        (tmp_path / "docs/guide.md").write_text("# Edited owner documentation\n")
    else:
        block = _replace_proof(block, lambda proof: proof["absence_certificate"].update(source_scope_sha256="f" * 64))
    blocks, problems = verified_blocks(_page([block]), feature, tmp_path, PIN, policy=policy)
    assert not blocks and problems


@pytest.mark.parametrize("broken", ["parse", "dynamic", "read", "oversized"])
def test_incomplete_test_inventory_remains_unknown(tmp_path, monkeypatch, broken):
    _source(tmp_path)
    policy = _policy()
    test = tmp_path / "tests/test_unrelated.py"
    test.parent.mkdir()
    test.write_text("def test_other():\n    assert True\n")
    if broken == "parse":
        test.write_text("def broken(:\n")
    elif broken == "dynamic":
        test.write_text("import importlib\nmodule = importlib.import_module('pkg.core')\ndef test_other():\n    assert module\n")
    elif broken == "oversized":
        test.write_text("#" + "padding" * 300_000)
    else:
        from infermatrix_copilot.kb_service.depth_inputs import DepthContext
        original = DepthContext._file
        monkeypatch.setattr(DepthContext, "_file", lambda self, path: None if path == "tests/test_unrelated.py"
                            else original(self, path))
    assert build_absence_certificate(tmp_path, policy, policy.features[0], PIN) is None


def test_native_approval_binding_is_required_to_count_when_requested(tmp_path):
    _source(tmp_path)
    policy = _policy(threshold=0)
    feature = policy.features[0]
    block = render_block(feature, _section("api"), tmp_path, "o/demo", PIN)
    head = {depth_page(feature): _page([block])}
    row = {"feature": feature.id, "facet": "api", "page": depth_page(feature), "block_sha256": digest(block),
           "native_trace_id": "native-judge-call", "native_reply_sha256": "b" * 64,
           "dimensions": {"faithful": "yes", "non_contradictory": "yes", "does_not_weaken": "yes"}}
    good = audit_depth(head, tmp_path, policy, PIN, approvals=[row])
    assert good["recognized_facets"] == 1 and not good["approval_binding_problems"]
    assert good["approval_bindings_checked"]
    for rows in ([], [dict(row, block_sha256="c" * 64)], [dict(row, dimensions={})], [row, row]):
        bad = audit_depth(head, tmp_path, policy, PIN, approvals=rows)
        assert bad["recognized_facets"] == 0 and bad["approval_binding_problems"]
        assert not bad["target_met"]


def test_retrieval_exposes_verified_gap_and_uninjected_facets_with_existing_budget(tmp_path):
    _source(tmp_path)
    policy = _policy()
    feature = policy.features[0]
    blocks = [_absence(tmp_path, policy)]
    for facet in ("api", "configuration", "dependencies", "failure_modes", "tradeoffs"):
        section = _section(facet)
        section["body"] = "The implementation context is source-pinned. " * 28
        blocks.append(render_block(feature, section, tmp_path, "o/demo", PIN))
    knowledge = tmp_path / "knowledge"
    page = knowledge / depth_page(feature)
    page.parent.mkdir(parents=True)
    page.write_text(feature_metadata(_page(blocks), feature))
    report = KnowledgeDocs(knowledge, "repos/demo").related(["pkg/core.py"])
    document = report["documents"][0]
    assert report["content_chars"] <= 6000
    assert document["facet_basis"]["validation"] == "verified_absent"
    assert document["verified_gaps"] == {"validation": "no_statically_associated_test_entry"}
    assert set(document["not_injected_facets"]) == set(document["available_facets"]) - set(document["included_facets"])
    assert document["more_available"]
    assert "已核验缺口" in document["content"]


def _require_semantic_depth(world):
    policy = _baseline(world)
    _complete_breadth(world, policy)
    path = "adapters/toy/knowledge-coverage.yaml"
    data = yaml.safe_load((world["origin"] / path).read_text())
    data["semantic_depth"] = {"per_facet_gt": 0.90}
    _commit(world["origin"], {path: yaml.safe_dump(data)}, "require semantic depth")
    return replace(policy, semantic_depth_per_facet_gt=0.90)


def test_unmet_semantic_policy_retains_preview_and_blocks_outward_publish(world):
    from infermatrix_copilot.kb_service.init_stages import run_stage

    _require_semantic_depth(world)
    gateway = DepthGateway(reject="api")
    preview = _run(world, gateway)
    assert preview.status == "partial", preview.problems
    assert not preview.depth["done"] and not preview.coverage["semantic_depth"]["target_met"]
    assert preview.coverage["semantic_depth"]["recognized_facets"] == 6
    assert _tree(preview)
    assert (world["tmp"] / "depth/init/toy/knowledge-deepen-dryrun/PR_BODY.md").is_file()
    calls = []

    def refuse_external(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("an unmet semantic target must block before outward writes")

    runtime = _runtime(world, gateway, state_dir=world["tmp"] / "depth",
                       environ={"ALLOW_PUSH": "1", "ALLOW_POST": "1", "KB_INIT_GIT_AUTHOR": "Test <test@example.com>"},
                       gh_run=refuse_external)
    published = run_stage(runtime, _modules_lifecycle(), "knowledge-deepen", dry_run=False,
                          from_existing=True, subscription_generator=True)
    assert published.status == "blocked"
    assert any("semantic depth target" in p for p in published.problems)
    assert not calls and not published.depth["done"]
    assert published.depth["accepted"] == preview.depth["accepted"]


@pytest.mark.parametrize("unavailable", [False, True])
def test_all_unknown_or_unavailable_depth_remains_incomplete(world, unavailable):
    from infermatrix_copilot.kb_service.depth_inputs import SYSTEM_DEPTH
    from infermatrix_copilot.kb_service.models import ModelReply, ModelUnavailable

    _require_semantic_depth(world)

    class Unsupported(DepthGateway):
        def call_json(self, role, **kwargs):
            if kwargs["system"] != SYSTEM_DEPTH:
                return super().call_json(role, **kwargs)
            if unavailable:
                raise ModelUnavailable("subscription extraction temporarily unavailable")
            payload = json.JSONDecoder().raw_decode(kwargs["prompt"][kwargs["prompt"].index("{"):])[0]
            data = {"sections": [], "unknown_facets": [
                {"facet": f, "reason": "exact implementation evidence is unavailable"} for f in payload["facets"]]}
            kwargs["validate"](data)
            return ModelReply(role, data, json.dumps(data), role.model, {}, 0.0)

    record = _run(world, Unsupported())
    assert record.status == "blocked" and not record.depth["done"]
    assert not record.depth["accepted"] and not record.coverage["semantic_depth"]["target_met"]
    assert record.coverage["semantic_depth"]["recognized_facets"] == 0
    assert len(record.unfinished) == len(FACETS)
    assert all(slot["status"] != "pass" for slot in record.depth["features"]["step0"]["facets"].values())


def test_depth_completion_preserves_baseline_block_hash_when_metadata_is_added(world):
    policy = _require_semantic_depth(world)
    feature = policy.features[0]
    pin = _git(world["upstream"], "rev-parse", "HEAD")
    section = {"facet": "api", "title": "Integer step contract", "interpretation": "fact",
               "body": "Engine.step accepts an integer and returns the incremented integer.",
               "evidence": [{"path": "pkg/core.py", "start": 3, "end": 6}]}
    block = render_block(feature, section, world["upstream"], "o/toy", pin)
    legacy = _replace_proof(block, lambda proof: proof.pop("basis"))
    index = "knowledge/repos/toy/components/core/_index.md"
    _commit(world["origin"], {"knowledge/" + depth_page(feature): _page([legacy]),
        index: (world["origin"] / index).read_text() + "\n- [Existing depth](feature-depth-step0.md)\n"},
        "merge previously accepted legacy block")
    completed = _run(world, DepthGateway())
    assert completed.status == "dry_run", completed.problems
    assert completed.depth["done"] and completed.coverage["semantic_depth"]["target_met"]
    text = _tree(completed)["knowledge/" + depth_page(feature)]
    current = verified_blocks(text, feature, world["upstream"], pin, policy=policy)[0]
    assert current["api"] == legacy and digest(current["api"]) == digest(legacy)
    assert 'feature: "step0"' in text
    assert completed.coverage["semantic_depth"]["recognized_facets"] == 7


@pytest.mark.parametrize("source,valid", [
    ('import { helper } from "./real";\nfunction run() { return helper(); }\n', True),
    ('import { helper } from "./other";\nfunction run() { return helper(); }\n', False),
    ('function run() { return obj.helper(); }\n', False),
    ('import { helper } from "./real";\nfunction run(helper) { return helper(); }\n', False),
    ('import { helper as increment } from "./real";\nfunction run() { return increment(); }\n', True),
    ('import * as utils from "./real";\nfunction run() { return utils.helper(); }\n', True),
])
def test_client_flow_requires_a_bound_import_or_local_name(tmp_path, source, valid):
    from infermatrix_copilot.kb_service.knowledge_depth import verify_trace

    (tmp_path / "client.ts").write_text(source)
    (tmp_path / "real.ts").write_text("export function helper() { return 1; }\n")
    (tmp_path / "other.ts").write_text("export function helper() { return 2; }\n")
    trace = [{"path": "client.ts", "symbol": "run", "start": len(source.splitlines()), "end": len(source.splitlines())},
             {"path": "real.ts", "symbol": "helper", "start": 1, "end": 1}]
    if valid:
        verify_trace(tmp_path, trace)
    else:
        with pytest.raises(ValueError, match="does not show"):
            verify_trace(tmp_path, trace)


def test_python_flow_cannot_confuse_a_parameter_with_a_module_function(tmp_path):
    from infermatrix_copilot.kb_service.knowledge_depth import verify_trace

    (tmp_path / "core.py").write_text("def helper():\n    return 1\ndef run(helper):\n    return helper()\n")
    trace = [{"path": "core.py", "symbol": "run", "start": 3, "end": 4},
             {"path": "core.py", "symbol": "helper", "start": 1, "end": 2}]
    with pytest.raises(ValueError, match="does not call"):
        verify_trace(tmp_path, trace)


def test_checkpoint_key_order_cannot_change_new_proof_or_trigger_another_judgment(tmp_path):
    _source(tmp_path)
    feature = _policy().features[0]
    section = _section("flow")
    restored = json.loads(json.dumps(section, sort_keys=True))
    original = render_block(feature, section, tmp_path, "o/demo", PIN)
    resumed = render_block(feature, restored, tmp_path, "o/demo", PIN)
    assert resumed == original and digest(resumed) == digest(original)


def test_completed_judge_checkpoint_resumes_without_another_model_call(world, monkeypatch):
    from infermatrix_copilot.kb_service.depth_judge import SYSTEM_DEPTH_REVIEW
    from infermatrix_copilot.kb_service.init_support import InitRecord

    _baseline(world)

    class CountJudge(DepthGateway):
        def __init__(self):
            super().__init__()
            self.judge_calls = 0

        def call_json(self, role, **kwargs):
            if kwargs["system"] == SYSTEM_DEPTH_REVIEW:
                self.judge_calls += 1
            return super().call_json(role, **kwargs)

    gateway = CountJudge()
    save = InitRecord.save
    interrupted = False

    def interrupt_after_durable_judgment(record, state_dir):
        nonlocal interrupted
        save(record, state_dir)
        feature = record.depth.get("features", {}).get("step0", {})
        if record.stage == "knowledge-deepen" and feature.get("checked") and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()

    monkeypatch.setattr(InitRecord, "save", interrupt_after_durable_judgment)
    with pytest.raises(KeyboardInterrupt):
        _run(world, gateway)
    saved = InitRecord.load(world["tmp"] / "depth", "toy", "knowledge-deepen")
    assert saved.depth["features"]["step0"]["checked"]
    assert gateway.judge_calls == len(gateway.depth_calls) == 1
    completed = _run(world, gateway)
    assert completed.status == "dry_run" and completed.depth["done"]
    assert completed.spent_usd == 0.5
    assert gateway.judge_calls == len(gateway.depth_calls) == 1
    assert completed.coverage["semantic_depth"]["recognized_facets"] == 7


@pytest.mark.parametrize("phase", ["extract", "judge"])
@pytest.mark.parametrize("crash", [False, True])
def test_depth_unknown_dispatch_cannot_be_retried_as_a_correction(world, phase, crash):
    from infermatrix_copilot.kb_service.depth_judge import SYSTEM_DEPTH_REVIEW
    from infermatrix_copilot.kb_service.init_knowledge_depth import SYSTEM_DEPTH
    from infermatrix_copilot.kb_service.init_support import InitRecord
    from infermatrix_copilot.kb_service.models import ModelUnavailable

    _baseline(world)
    calls = []

    class UnknownGateway(DepthGateway):
        def call_json(self, role, **kwargs):
            if kwargs["system"] == (SYSTEM_DEPTH if phase == "extract" else SYSTEM_DEPTH_REVIEW):
                calls.append(1)
                if crash:
                    raise KeyboardInterrupt()
                error = ModelUnavailable("response lost")
                error.native_call_sent = True
                raise error
            return super().call_json(role, **kwargs)

    gateway = UnknownGateway()
    if crash:
        with pytest.raises(KeyboardInterrupt):
            _run(world, gateway)
    else:
        _run(world, gateway)
    record = InitRecord.load(world["tmp"] / "depth", "toy", "knowledge-deepen")
    entry = record.depth["features"]["step0"]
    assert entry["extraction_dispatched" if phase == "extract" else "review_dispatched"] is True
    resumed = _run(world, gateway, retry=True)
    assert calls == [1]
    assert resumed.depth["done"] is False


@pytest.mark.parametrize("body", ["const helper = () => 2; return helper();", "helper = () => 2; return helper();"])
def test_client_flow_cannot_use_a_rebound_global_callable(tmp_path, body):
    from infermatrix_copilot.kb_service.knowledge_depth import verify_trace

    (tmp_path / "client.ts").write_text("function run() { " + body + " }\nfunction helper() { return 1; }\n")
    trace = [{"path": "client.ts", "symbol": "run", "start": 1, "end": 1},
             {"path": "client.ts", "symbol": "helper", "start": 2, "end": 2}]
    with pytest.raises(ValueError, match="does not show"):
        verify_trace(tmp_path, trace)


def test_typescript_object_parameter_type_cannot_hide_a_shadowed_import(tmp_path):
    from infermatrix_copilot.kb_service.knowledge_depth import verify_trace

    (tmp_path / "client.ts").write_text('import { helper } from "./real";\n'
                                       'function run(opts: { a: number }, helper: () => number) { return helper(); }\n')
    (tmp_path / "real.ts").write_text("export function helper() { return 1; }\n")
    trace = [{"path": "client.ts", "symbol": "run", "start": 2, "end": 2},
             {"path": "real.ts", "symbol": "helper", "start": 1, "end": 1}]
    with pytest.raises(ValueError, match="does not show"):
        verify_trace(tmp_path, trace)


def test_python_flow_cannot_use_a_rebound_module_function(tmp_path):
    from infermatrix_copilot.kb_service.knowledge_depth import verify_trace

    (tmp_path / "core.py").write_text("def helper():\n    return 1\ndef run():\n    helper = lambda: 2\n    return helper()\n")
    trace = [{"path": "core.py", "symbol": "run", "start": 3, "end": 5},
             {"path": "core.py", "symbol": "helper", "start": 1, "end": 2}]
    with pytest.raises(ValueError, match="does not call"):
        verify_trace(tmp_path, trace)


def test_documented_test_entry_prevents_a_false_absence_certificate(tmp_path):
    _source(tmp_path)
    policy = _policy()
    test = tmp_path / "tests/test_protocol.py"
    test.parent.mkdir()
    test.write_text("def test_protocol():\n    assert 1 == 1\n")
    assert build_absence_certificate(tmp_path, policy, policy.features[0], PIN)
    (tmp_path / "docs/guide.md").write_text("Validation for this feature: `tests/test_protocol.py`.\n")
    assert build_absence_certificate(tmp_path, policy, policy.features[0], PIN) is None
    (tmp_path / "docs/guide.md").unlink()
    assert build_absence_certificate(tmp_path, policy, policy.features[0], PIN) is None
