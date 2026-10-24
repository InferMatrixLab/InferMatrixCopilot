"""Offline integration: original source, unchanged knowledge and call recovery."""
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from infermatrix_copilot.kb_service import maintenance
from infermatrix_copilot.kb_service.maintenance_audit import BudgetGateway, audit_unit, digest, source_evidence
from infermatrix_copilot.kb_service.maintenance_settings import MaintenanceConfig
from infermatrix_copilot.kb_service.maintenance_store import MaintenanceStore
from infermatrix_copilot.kb_service.maintenance_units import page_units
from infermatrix_copilot.kb_service.models import ModelUnavailable

from test_kb_intake_gate import PAGE, ScriptedGateway, _git, _judge_all, _rule, _runtime

PIN = "a" * 40
SOURCE_PATH = "demo/core/q.py"
SOURCE = "DEFAULT_CAPACITY = 8\n\ndef enqueue(queue, item):\n    if len(queue) >= DEFAULT_CAPACITY:\n        raise QueueFull()\n    queue.append(item)\n"
URL = f"https://github.com/org/demo/blob/{PIN}/{SOURCE_PATH}#L1-L6"


class OriginalSource:
    repository = "org/demo"
    def __init__(self):
        self.reads = []

    def head(self):
        raise AssertionError("maintenance must not replace the declared pin with latest HEAD")

    def top_level(self, pin):
        assert pin == PIN
        return {"demo"}

    def path_exists(self, pin, path):
        assert pin == PIN
        return path == SOURCE_PATH

    def file_text(self, pin, path):
        self.reads.append((pin, path))
        assert pin == PIN
        return SOURCE if path == SOURCE_PATH else None

    def pull(self, number):
        return {"merged": True, "merge_commit_sha": PIN, "number": number}

    def pr_diff(self, number, merge_sha, path):
        return "+DEFAULT_CAPACITY = 8\n"


def semantic(outcome="contradicted"):
    return {"outcome": outcome, "reason": "The source assigns capacity eight, contradicting sixteen.",
            "witnesses": [0], "conflicts": ["default queue capacity is sixteen"] if outcome == "contradicted" else [],
            "assessments": [{"claim": "default queue capacity is sixteen", "witness": 0,
                             "quote": "DEFAULT_CAPACITY = 8", "relation": "contradicts" if outcome == "contradicted" else "supports",
                             "explanation": "The assignment establishes an eight-item default."}]}


def configured_runtime(tmp_path, answer, *, prose=False, pinned=True):
    gateway = ScriptedGateway(answer)
    rt, lifecycle = _runtime(tmp_path, gateway)
    now = [datetime(2026, 10, 8, 1, 5, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()]
    rt.clock = lambda: now[0]
    rt.ledger._clock = rt.clock
    rt.lease_owner = rt.ledger.acquire_lease("maintenance-test", ttl=900)
    rt.maintenance = MaintenanceConfig(enabled=True, max_units=20, costs={
        rt.generator.label(): {"kind": "subscription", "accounted_usd": .5},
        rt.judge.label(): {"kind": "subscription", "accounted_usd": .5}})
    source = OriginalSource()
    rt.upstream_facts = lambda _: source
    origin = tmp_path / "origin"
    (origin / "skills" / "x.md").unlink()
    target = origin / "knowledge" / PAGE
    original = target.read_text()
    if prose:
        target.unlink()
        path = "repos/demo/core/architecture.md"
        target = origin / "knowledge" / path
        text = ("---\ntitle: Queue architecture\ncreated: 2026-09-01\nupdated: 2026-09-01\n"
                "type: architecture\ntags: [demo]\nsources: []\n---\n\n# Queue architecture\n\n"
                "The default queue capacity is sixteen.\n")
    else:
        path = PAGE
        text = original[:original.index("## DEMO-1a")] + _rule("DEMO-1a", claim="默认队列容量必须为16")
    if pinned:
        text = text.replace("sources: []", f'sources: ["{URL}"]') \
            .replace('sources: ["PR #10"]', f'sources: ["PR #10", "{URL}"]')
        text += f"\nSource: {URL}\n"
    target.write_text(text)
    _git(origin, "add", ".")
    _git(origin, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "known wrong admitted claim")
    snapshot = rt.knowledge.fetch()
    rt.ledger.record_activation(snapshot, {"fixture": True})
    unit = page_units(path, text, "demo", snapshot)[0]
    return rt, lifecycle, source, unit, now


def standard_answer(role, prompt):
    if role.name == "generator":
        return {"operation": {"kind": "replace", "page": PAGE, "rule_id": "DEMO-1a",
                              "new_rule_id": "DEMO-2a", "section_markdown": _rule("DEMO-2a", claim="默认队列容量必须为8")},
                "rationale": "Use the original assignment."}
    if '"claim"' in prompt:
        return semantic()
    return _judge_all("yes")(role, prompt)


def changesets(rt):
    return [rt.ledger.changeset(row[0]) for row in rt.ledger._conn.execute(
        "SELECT id FROM changesets WHERE repo='demo' ORDER BY created_at,id")]


def test_unchanged_wrong_rule_is_challenged_against_original_code(tmp_path):
    rt, lifecycle, source, unit, _ = configured_runtime(tmp_path, standard_answer)
    result = audit_unit(rt, lifecycle, unit, rt.gateway)
    assert result["outcome"] == "contradicted" and result["original_source_checked"]
    assert result["tests_executed"] is False
    assert all(pin == PIN for pin, _ in source.reads)
    assert SOURCE in rt.gateway.calls[0][1].replace("\\n", "\n")
    # The same bytes and the same applicability pin are not a cached verdict.
    repeated = audit_unit(rt, lifecycle, unit, rt.gateway)
    assert len(rt.gateway.calls) == 2
    receipt = result["quality_receipt"]
    assert receipt["policy"] == "maintenance-source-audit" and receipt["version"] == 1
    assert repeated["quality_receipt"]["input_sha256"] == receipt["input_sha256"]
    changed = audit_unit(rt, lifecycle, {**unit, "text": unit["text"] + " Additional claim."}, rt.gateway)
    assert changed["quality_receipt"]["input_sha256"] != receipt["input_sha256"]


@pytest.mark.parametrize("prose", [False, True])
def test_missing_applicability_version_remains_unknown_without_drafting(tmp_path, prose):
    rt, _, source, _, _ = configured_runtime(tmp_path, standard_answer, prose=prose, pinned=False)
    result = maintenance.tick(rt)
    assert result["status"] == "complete"
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    assert all(item["outcome"] == "unknown" for item in store.items(result["run_id"]))
    # Source applicability remains unknown; owner consistency is a separate
    # useful review and cannot promote the rule into a semantic verdict.
    assert all('"claim"' not in prompt and role != "generator" for role, prompt in rt.gateway.calls)
    assert not source.reads
    assert not changesets(rt)
    assert store.report()["valid_nights"] == 0


def test_unchanged_rule_page_introduction_is_audited_and_requires_owner_correction(tmp_path):
    rt, _, _, _, _ = configured_runtime(tmp_path, standard_answer)
    origin = tmp_path / "origin"
    page = origin / "knowledge" / PAGE
    text = page.read_text().replace("## DEMO-1a", "The default queue capacity is sixteen.\n\n## DEMO-1a")
    page.write_text(text)
    _git(origin, "add", ".")
    _git(origin, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "admitted introduction")
    snapshot = rt.knowledge.fetch()
    rt.ledger.record_activation(snapshot, {"fixture": True})
    result = maintenance.tick(rt)
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    introduction = next(item for item in store.items(result["run_id"])
                        if item["unit"]["kind"] == "legacy" and item["unit"]["page"] == PAGE)
    assert introduction["outcome"] == "contradicted" and introduction["unit"]["protected"]
    assert introduction["detail"]["original_source_checked"]
    assert introduction["detail"]["correction"]["status"] == "human"
    assert all(cs["detail"].get("correction_unit") != introduction["unit_id"] for cs in changesets(rt))
    assert rt.ledger.active_snapshot() == snapshot


def test_full_shadow_cycle_records_contradiction_gate_and_no_publication(tmp_path):
    rt, _, _, unit, _ = configured_runtime(tmp_path, standard_answer)
    snapshot = rt.ledger.active_snapshot()
    result = maintenance.tick(rt)
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    item = next(row for row in store.items(result["run_id"]) if row["unit_id"] == unit["unit_id"])
    assert item["outcome"] == "contradicted"
    assert item["detail"]["correction"]["hold"]["status"] == "would_hold"
    recorded = changesets(rt)
    assert len(recorded) == 1 and recorded[0]["kind"] == "correction"
    assert recorded[0]["status"] in {"human", "shadow_recorded"}
    assert rt.ledger.active_snapshot() == snapshot
    assert not (rt.state_dir / "outbox").exists()
    assert store.report()["valid_nights"] == 1
    calls = len(rt.gateway.calls)
    accounted = store.report()["budget"]["accounted_usd"]
    assert maintenance.tick(rt) is None
    assert len(rt.gateway.calls) == calls and store.report()["budget"]["accounted_usd"] == accounted


def test_legacy_explanatory_page_can_be_corrected_without_overwriting_metadata(tmp_path):
    def answer(role, prompt):
        if role.name == "generator":
            return {"replacement": f"# Queue architecture\n\nThe default queue capacity is eight.\n\nSource: {URL}\n",
                    "rationale": "The pinned assignment is eight."}
        return standard_answer(role, prompt)
    rt, _, _, unit, _ = configured_runtime(tmp_path, answer, prose=True)
    before = rt.knowledge.knowledge_files(rt.ledger.active_snapshot())[unit["page"]]
    result = maintenance.tick(rt)
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    item = next(row for row in store.items(result["run_id"]) if row["unit_id"] == unit["unit_id"])
    assert item["outcome"] == "contradicted"
    assert len(changesets(rt)) == 1
    correction = changesets(rt)[0]
    changed = rt.load_changeset_files(correction["id"])["files"][unit["page"]]
    assert "capacity is eight" in changed and "capacity is sixteen" not in changed
    assert "type: architecture" in changed and "created: 2026-09-01" in changed
    assert "<!-- kb:depth" not in changed and "<!-- kb:knowledge" not in changed
    assert rt.knowledge.knowledge_files(rt.ledger.active_snapshot())[unit["page"]] == before


@pytest.mark.parametrize("reply", [
    {"outcome": "verified", "reason": "hash matches", "witnesses": [0], "conflicts": []},
    {"outcome": "contradicted", "reason": "source exists", "witnesses": [0], "conflicts": []},
    {"outcome": "verified", "reason": "", "witnesses": [0], "conflicts": []},
])
def test_definitive_verdict_requires_semantic_source_comparison_not_hash_or_existence(tmp_path, reply):
    rt, lifecycle, _, unit, _ = configured_runtime(tmp_path, lambda role, prompt: reply)
    with pytest.raises((ValueError, ModelUnavailable)):
        audit_unit(rt, lifecycle, unit, rt.gateway)


def test_semantic_quote_must_come_from_offered_original_source(tmp_path):
    reply = semantic()
    reply["assessments"][0]["quote"] = "DEFAULT_CAPACITY = 16"
    rt, lifecycle, _, unit, _ = configured_runtime(tmp_path, lambda role, prompt: reply)
    with pytest.raises((ValueError, ModelUnavailable)):
        audit_unit(rt, lifecycle, unit, rt.gateway)


def test_source_hash_mismatch_stops_before_any_model_or_correction(tmp_path):
    rt, lifecycle, source, unit, _ = configured_runtime(tmp_path, standard_answer)
    refs = [{"repository": "org/demo", "sha": PIN, "path": SOURCE_PATH,
             "start": 1, "end": 1, "content_sha256": "0" * 64}]
    unit = {**unit, "sources": refs, "primary_sources": refs}
    with pytest.raises(ValueError, match="proof"):
        source_evidence(rt, lifecycle, unit)
    assert source.reads and not rt.gateway.calls


def test_successful_call_cache_replays_exact_result_without_second_charge(tmp_path):
    rt, _, _, unit, _ = configured_runtime(tmp_path, lambda role, prompt: {"answer": "original"})
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    def wrapper():
        return BudgetGateway(rt, store, rt.maintenance, run_id="test", unit=unit, phase="audit")
    first = wrapper().call_json(rt.judge, system="audit", prompt="data")
    assert wrapper().call_json(rt.judge, system="audit", prompt="data").data == first.data
    assert len(rt.gateway.calls) == 1
    assert store.report()["budget"]["accounted_usd"] == .5


def test_duplicate_span_cannot_hide_conflicting_recorded_source_hash(tmp_path):
    rt, lifecycle, source, unit, _ = configured_runtime(tmp_path, standard_answer)
    span = {"repository": "org/demo", "sha": PIN, "path": SOURCE_PATH, "start": 1, "end": 6}
    unit = {**unit, "primary_sources": [span, {**span, "content_sha256": "0" * 64}]}
    with pytest.raises(ValueError, match="recorded proof"):
        source_evidence(rt, lifecycle, unit)
    assert len(source.reads) == 1 and not rt.gateway.calls


def test_url_and_structured_duplicate_span_read_once_and_truncated_proof_refused(tmp_path):
    rt, lifecycle, source, unit, _ = configured_runtime(tmp_path, standard_answer)
    span = {"repository": "org/demo", "sha": PIN, "path": SOURCE_PATH, "start": 1, "end": 6,
            "content_sha256": digest(SOURCE)}
    unit = {**unit, "primary_sources": [URL, span]}
    evidence = source_evidence(rt, lifecycle, unit)
    assert len(evidence) == len(source.reads) == 1
    with pytest.raises(ValueError, match="truncated"):
        source_evidence(rt, lifecycle, {**unit, "sources_truncated": True})
    assert len(source.reads) == 1 and not rt.gateway.calls


@pytest.mark.parametrize("field", ["start", "end", "start_line", "end_line"])
@pytest.mark.parametrize("value", [[1], True, 1.5])
def test_malformed_source_span_metadata_is_unknown_before_model_dispatch(tmp_path, field, value):
    rt, lifecycle, source, unit, _ = configured_runtime(tmp_path, standard_answer)
    span = {"repository": "org/demo", "sha": PIN, "path": SOURCE_PATH, "start": 1, "end": 6,
            field: value}
    with pytest.raises(ValueError, match="integer line numbers"):
        source_evidence(rt, lifecycle, {**unit, "primary_sources": [span]})
    assert not source.reads and not rt.gateway.calls


def test_malformed_span_becomes_unknown_and_following_pinned_unit_is_reviewed(tmp_path):
    def answer(role, prompt):
        result = semantic("verified")
        result["reason"] = "The eight-item default matches the original assignment."
        result["assessments"][0]["claim"] = "default queue capacity is eight"
        return result
    rt, _, source, _, _ = configured_runtime(tmp_path, answer)
    origin = tmp_path / "origin"
    paths = ["repos/demo/core/malformed.md", "repos/demo/core/valid.md"]
    for path, start in zip(paths, ([1], 1)):
        span = {"repository": "org/demo", "sha": PIN, "path": SOURCE_PATH, "start": start, "end": 6}
        text = ("---\ntitle: Queue explanation\ncreated: 2026-09-01\nupdated: 2026-09-01\n"
                f"type: guide\ntags: [demo]\nsources: {json.dumps([span])}\n---\n\n"
                "# Queue explanation\n\nThe default queue capacity is eight.\n")
        (origin / "knowledge" / path).write_text(text)
    _git(origin, "add", ".")
    _git(origin, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "malformed and valid source spans")
    snapshot = rt.knowledge.fetch()
    rt.ledger.record_activation(snapshot, {"fixture": True})
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    run = store.begin_cycle(snapshot=snapshot, policy_sha256=maintenance.policy_digest(rt), eligible_repos=["demo"])
    selected = [{**page_units(path, (origin / "knowledge" / path).read_text(), "demo", snapshot)[0],
                 "selection_rank": rank} for rank, path in enumerate(paths)]
    store.add_items(run["id"], selected)
    result = maintenance.tick(rt)
    items = store.items(run["id"])
    assert result["status"] == "complete"
    assert [item["outcome"] for item in items] == ["unknown", "verified"]
    assert "integer line numbers" in items[0]["detail"]["reason"]
    assert items[1]["detail"]["original_source_checked"]
    assert len(source.reads) == len(rt.gateway.calls) == 1
    assert not changesets(rt)


def test_paused_repo_request_does_not_block_other_repo_nightly_audit(tmp_path):
    rt, lifecycle, _, _, _ = configured_runtime(tmp_path, standard_answer)
    other = replace(lifecycle, repo="other", knowledge_dir="repos/other")
    rt.registry[other.repo] = other
    rt.ledger.ensure_repo(other.repo, other.mode)
    origin = tmp_path / "origin"
    page = origin / "knowledge" / "repos/other/core/architecture.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntitle: Other queue architecture\ncreated: 2026-09-01\nupdated: 2026-09-01\n"
                    f'type: architecture\ntags: [other]\nsources: ["{URL}"]\n---\n\n'
                    f"# Queue architecture\n\nThe default queue capacity is sixteen.\nSource: {URL}\n")
    _git(origin, "add", ".")
    _git(origin, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "second repository")
    snapshot = rt.knowledge.fetch()
    rt.ledger.record_activation(snapshot, {"fixture": True})
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    store.enqueue_request("paused-first", "demo")
    rt.ledger.bump_generation("demo", pause=True, reason="owner paused this repository")
    result = maintenance.tick(rt)
    assert store.run(result["run_id"])["kind"] == "nightly"
    audited = [item for item in store.items(result["run_id"]) if item["unit"]["repo"] == "other"]
    assert audited and all(item["outcome"] == "contradicted" for item in audited)
    assert [request["id"] for request in store.pending_requests()] == ["paused-first"]


def test_calibration_stage_crash_cannot_enter_production_publication_queue(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service.maintenance_correction import propose_correction
    from infermatrix_copilot.kb_service.runtime import publish
    rt, lifecycle, _, unit, _ = configured_runtime(tmp_path, standard_answer)
    # This one-rule fixture intentionally permits replacing its only rule;
    # circuit-breaker behavior is covered separately in the gate tests.
    lifecycle = replace(lifecycle, retire_ratio=1.0)
    rt.registry[lifecycle.repo] = lifecycle
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    result = audit_unit(rt, lifecycle, unit, rt.gateway)
    original_update = rt.ledger.update_changeset
    def interrupted(changeset_id, **fields):
        if fields.get("status") == "maintenance_calibration":
            raise OSError("crash after staging and before evaluation status update")
        return original_update(changeset_id, **fields)
    monkeypatch.setattr(rt.ledger, "update_changeset", interrupted)
    with pytest.raises(OSError, match="after staging"):
        propose_correction(rt, lifecycle, store, rt.maintenance, run_id="calibration",
                           unit=unit, finding=result,
                           base=rt.knowledge.knowledge_files(unit["snapshot"]), base_sha=unit["snapshot"],
                           publish_result=False)
    monkeypatch.setattr(rt.ledger, "update_changeset", original_update)
    staged = changesets(rt)[0]
    assert staged["status"] == "gated" and staged["kind"] == "maintenance_calibration"
    auto = replace(lifecycle, mode="auto_merge")
    rt.registry[auto.repo] = auto
    rt.ledger.ensure_repo(auto.repo, "auto_merge")
    assert publish(rt, auto, staged["id"]) == "maintenance_calibration"
    assert not (rt.state_dir / "outbox").exists()


@pytest.mark.parametrize("publish_result", [True, False])
def test_maintenance_external_citation_requires_owner_and_never_generic_companion(tmp_path, monkeypatch, publish_result):
    from infermatrix_copilot.kb_service.maintenance_correction import propose_correction
    import infermatrix_copilot.kb_service.companion as companion
    rt, lifecycle, _, unit, _ = configured_runtime(tmp_path, standard_answer)
    lifecycle = replace(lifecycle, retire_ratio=1.0)
    rt.registry[lifecycle.repo] = lifecycle
    origin = tmp_path / "origin"
    (origin / "skills" / "x.md").write_text("Use the DEMO-1a queue rule.\n")
    _git(origin, "add", ".")
    _git(origin, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "external rule citation")
    snapshot = rt.knowledge.fetch()
    unit = {**unit, "snapshot": snapshot}
    monkeypatch.setattr(companion, "prepare_companion", lambda *args: pytest.fail("maintenance escaped its budgeted correction path"))
    finding = audit_unit(rt, lifecycle, unit, rt.gateway)
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    result = propose_correction(rt, lifecycle, store, rt.maintenance, run_id="citation-correction", unit=unit,
        finding=finding, base=rt.knowledge.knowledge_files(snapshot), base_sha=snapshot, publish_result=publish_result)
    cs = rt.ledger.changeset(result["changeset_id"])
    assert cs["detail"]["decision"]["status"] == "human"
    assert cs["detail"]["decision"]["reasons"][0].startswith("references outside knowledge/")
    assert not any(row["kind"] == "companion" for row in changesets(rt))
    assert not (rt.state_dir / "outbox").exists()


@pytest.mark.parametrize('publish_result', [True, False])
@pytest.mark.parametrize('failure_at', ['gate_return', 'settlement'])
def test_correction_resumes_committed_gate_without_new_model_work(tmp_path, monkeypatch, publish_result, failure_at):
    from infermatrix_copilot.kb_service import maintenance_correction as module
    rt, lifecycle, _, unit, _ = configured_runtime(tmp_path, standard_answer)
    lifecycle = replace(lifecycle, retire_ratio=1.0)
    rt.registry[lifecycle.repo] = lifecycle
    finding = audit_unit(rt, lifecycle, unit, rt.gateway)
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    target, name = ((module, 'gate_and_stage') if failure_at == 'gate_return'
                    else (rt.ledger, 'stage_intake'))
    original = getattr(target, name)

    def lost(*args, **kwargs):
        if failure_at == 'settlement':
            if kwargs.get('expected_status'):
                raise OSError('gate return lost')
            return original(*args, **kwargs)
        original(*args, **kwargs)
        raise OSError('gate return lost')

    monkeypatch.setattr(target, name, lost)
    args = dict(run_id='resume-correction', unit=unit, finding=finding,
                base=rt.knowledge.knowledge_files(unit['snapshot']), base_sha=unit['snapshot'],
                publish_result=publish_result)
    with pytest.raises(OSError, match='gate return lost'):
        module.propose_correction(rt, lifecycle, store, rt.maintenance, **args)
    row, = changesets(rt)
    calls = list(rt.gateway.calls)
    monkeypatch.setattr(target, name, original)
    if failure_at == 'settlement':
        from infermatrix_copilot.kb_service.gate_receipts import settle_gate_result
        assert row['status'] == 'review_pending'
        settle_gate_result(rt.ledger, rt.state_dir, rt.lease_owner, row['id'])
    result = module.propose_correction(rt, lifecycle, store, rt.maintenance, **args)
    assert result['changeset_id'] == row['id']
    assert rt.gateway.calls == calls
    assert len(changesets(rt)) == 1
    with pytest.raises(ModelUnavailable, match='requires reconciliation'):
        module.propose_correction(rt, lifecycle, store, rt.maintenance,
                                  **args, force_human='changed acceptance policy')
    assert rt.gateway.calls == calls
    assert len(changesets(rt)) == 1


@pytest.mark.parametrize("kind", ["correction", "maintenance_calibration", "rebuild"])
def test_generic_rebuild_cannot_strip_maintenance_lineage_or_dispatch_models(tmp_path, monkeypatch, kind):
    from infermatrix_copilot.kb_service.maintenance_correction import propose_correction
    from infermatrix_copilot.kb_service.merge import rebuild
    rt, lifecycle, _, unit, _ = configured_runtime(tmp_path, standard_answer)
    lifecycle = replace(lifecycle, retire_ratio=1.0)
    rt.registry[lifecycle.repo] = lifecycle
    finding = audit_unit(rt, lifecycle, unit, rt.gateway)
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    result = propose_correction(rt, lifecycle, store, rt.maintenance, run_id="maintenance-origin", unit=unit,
        finding=finding, base=rt.knowledge.knowledge_files(unit["snapshot"]), base_sha=unit["snapshot"],
        publish_result=kind == "correction")
    cs = {**rt.ledger.changeset(result["changeset_id"]), "kind": kind}
    calls, spent = len(rt.gateway.calls), store.report()["budget"]["accounted_usd"]
    monkeypatch.setattr(type(rt.knowledge), "fetch", lambda self: pytest.fail("generic rebuild fetched before maintenance refusal"))
    assert rebuild(rt, lifecycle, cs, "base moved") is None
    assert rt.ledger.changeset(cs["id"])["status"] == "human"
    assert len(rt.gateway.calls) == calls and store.report()["budget"]["accounted_usd"] == spent
    assert len(changesets(rt)) == 1 and not (rt.state_dir / "outbox").exists()


def test_interrupted_call_missing_cache_is_not_redispatched_or_rebilled(tmp_path, monkeypatch):
    rt, _, _, unit, _ = configured_runtime(tmp_path, lambda role, prompt: {"answer": "original"})
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    def wrapper():
        return BudgetGateway(rt, store, rt.maintenance, run_id="test", unit=unit, phase="audit")
    import infermatrix_copilot.kb_service.maintenance_audit as module
    real_write = module.atomic_write_json
    monkeypatch.setattr(module, "atomic_write_json", lambda *args: (_ for _ in ()).throw(OSError("interrupted before durable reply")))
    with pytest.raises(OSError):
        wrapper().call_json(rt.judge, system="audit", prompt="data")
    monkeypatch.setattr(module, "atomic_write_json", real_write)
    with pytest.raises(ModelUnavailable, match="not redispatched"):
        wrapper().call_json(rt.judge, system="audit", prompt="data")
    assert len(rt.gateway.calls) == 1
    assert store.report()["budget"]["accounted_usd"] == .5


def test_orphan_or_corrupted_reply_cache_cannot_masquerade_as_review(tmp_path):
    rt, _, _, unit, _ = configured_runtime(tmp_path, lambda role, prompt: {"answer": "original"})
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    def wrapper():
        return BudgetGateway(rt, store, rt.maintenance, run_id="test", unit=unit, phase="audit")
    wrapper().call_json(rt.judge, system="audit", prompt="data")
    path = next((rt.state_dir / "maintenance" / "calls").glob("*.json"))
    original_cache = path.read_text()
    document = json.loads(original_cache)
    document["data"] = {"answer": "tampered"}
    path.write_text(json.dumps(document))
    with pytest.raises((ValueError, ModelUnavailable)):
        wrapper().call_json(rt.judge, system="audit", prompt="data")
    # A syntactically sound cached answer without its durable call record is
    # also insufficient provenance, even if its filename/request hash matches.
    path.write_text(original_cache)
    rt.ledger._conn.execute("DELETE FROM maintenance_budget")
    with pytest.raises((ValueError, ModelUnavailable)):
        wrapper().call_json(rt.judge, system="audit", prompt="data")
    assert len(rt.gateway.calls) == 1


def test_paid_transport_without_enforceable_call_bound_never_dispatches(tmp_path):
    rt, _, _, unit, _ = configured_runtime(tmp_path, standard_answer)
    config = replace(rt.maintenance, costs={rt.judge.label(): {
        "kind": "api", "threshold_usd": 1, "in_usd_per_mtok": 5,
        "out_usd_per_mtok": 10, "max_output_tokens": 1000}})
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    gateway = BudgetGateway(rt, store, config, run_id="test", unit=unit, phase="audit")
    with pytest.raises(ModelUnavailable, match="upper bound"):
        gateway.call_json(rt.judge, system="audit", prompt="data")
    assert not rt.gateway.calls and store.report()["budget"]["accounted_usd"] == 0


def test_same_family_configuration_cannot_self_review_maintenance(tmp_path):
    rt, _, _, _, _ = configured_runtime(tmp_path, standard_answer)
    rt.judge = replace(rt.judge, model=rt.generator.model)
    assert maintenance.tick(rt)["status"] == "independent_reviewer_required"
    assert not rt.gateway.calls


def test_global_pause_prevents_maintenance_dispatch(tmp_path):
    rt, _, _, _, _ = configured_runtime(tmp_path, standard_answer)
    rt.ledger.bump_generation("*", pause=True, reason="test pause")
    assert maintenance.tick(rt) is None
    assert not rt.gateway.calls


@pytest.mark.parametrize("checks", [
    [{"head_sha": "f" * 40, "name": "suite", "status": "completed", "conclusion": "success"}],
    [{"head_sha": "d" * 40, "name": "suite", "status": "completed", "conclusion": "failure"},
     {"head_sha": "d" * 40, "name": "suite", "status": "completed", "conclusion": "success"}],
])
def test_restoration_ci_refuses_wrong_head_or_conflicting_required_checks(monkeypatch, checks):
    monkeypatch.setenv("KB_MAINTENANCE_REQUIRED_CHECKS", '["suite"]')
    def get(path):
        if path.endswith("/check-runs"):
            return {"check_runs": checks}
        return {"sha": "d" * 40, "statuses": []}
    rt = SimpleNamespace(github=SimpleNamespace(get=get))
    with pytest.raises(ValueError, match="CI"):
        maintenance._required_ci(rt, {"head_sha": "d" * 40})


def _ready_fixture(tmp_path, monkeypatch):
    """Real historical audits and signed owner cases; no model/network calls."""
    from infermatrix_copilot.kb_service.outbox import Outbox
    from infermatrix_copilot.knowledge_service.signing import generate_private_key, public_key_text, sign
    from infermatrix_copilot.knowledge_service.ops import KnowledgeOperation, apply_operations
    from infermatrix_copilot.knowledge_service.lifecycle import Page
    from test_kb_intake_gate import _calibrate

    def answer(role, prompt):
        if role.name == "judge" and '"claim"' in prompt and "## DEMO-3a" in prompt:
            result = semantic("verified")
            result["reason"] = "The eight-item knowledge claim matches the original assignment."
            result["assessments"][0]["claim"] = "default queue capacity is eight"
            return result
        return standard_answer(role, prompt)

    rt, lifecycle, source, _, now = configured_runtime(tmp_path, answer)
    lifecycle = _calibrate(tmp_path, rt, replace(lifecycle, mode="auto_merge", retire_ratio=1.0))
    rt.registry[lifecycle.repo] = lifecycle
    rt.ledger.ensure_repo(lifecycle.repo, "auto_merge")
    rt.maintenance = replace(rt.maintenance, consumers=("native",))
    key = generate_private_key(tmp_path / "maintenance-service.key")
    publisher = generate_private_key(tmp_path / "maintenance-publisher.key")
    rt.outbox = Outbox(rt.state_dir, key, rt.ledger, clock=rt.clock)
    rt.publisher_public_key = publisher.public_key()
    rt.containment_policy_dir = tmp_path / "authority"
    rt.containment_consumers = ["native"]
    rt.containment_enforce = True
    cfg = {"enabled": True, "consumer_id": "native", "public_keys": [public_key_text(key.public_key())],
           "policy_path": str(tmp_path / "consumer/policy.json"), "state_dir": str(tmp_path / "consumer/state")}
    origin = tmp_path / "origin"
    page = origin / "knowledge" / PAGE
    page.write_text(page.read_text() + _rule("DEMO-3a", claim="默认队列容量必须为8") + f"\nSource: {URL}\n")
    (origin / "knowledge" / "AGENTS.md").write_text("# Knowledge\n\nUse the owner-scoped executable contracts.\n")
    _git(origin, "add", ".")
    _git(origin, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "independent good calibration claim")
    snapshot = rt.knowledge.fetch()
    rt.ledger.record_activation(snapshot, {"fixture": True})
    units = page_units(PAGE, page.read_text(), "demo", snapshot)
    bad = next(u for u in units if u["block_id"] == "DEMO-1a")
    good = next(u for u in units if u["block_id"] == "DEMO-3a")
    policy = maintenance.policy_digest(rt)
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    current_time = time.time()
    for days_ago in range(7, 0, -1):
        now[0] = current_time - days_ago * 86400
        rt.ledger.heartbeat(rt.lease_owner)
        run = store.begin_cycle(snapshot=snapshot, policy_sha256=policy, eligible_repos=["demo"])
        store.add_items(run["id"], [bad])
        reviewed = audit_unit(rt, lifecycle, bad, BudgetGateway(rt, store, rt.maintenance,
            run_id=run["id"], unit=bad, phase="audit"))
        store.record_outcome(run["id"], bad["unit_id"], reviewed["outcome"], detail=reviewed)
        store.mark_run(run["id"], "complete")
    now[0] = current_time
    rt.ledger.heartbeat(rt.lease_owner)
    run = store.begin_cycle(snapshot=snapshot, policy_sha256=policy, eligible_repos=["demo"])

    base = rt.knowledge.knowledge_files(snapshot)
    op = KnowledgeOperation.from_dict({**standard_answer(rt.generator, "")["operation"],
        "evidence": f"org/demo@{PIN}:{SOURCE_PATH}:L1-L6"})
    expected = apply_operations(base, [op], release=rt.release_for("demo"), today=rt.today()).files[PAGE]
    expected = Page.parse(expected).with_frontmatter_field("updated", str(Page.parse(base[PAGE]).frontmatter_data()["updated"])).render()
    cases = [
        {"id": "good-audit", "unit": good, "expected": "verified"},
        {"id": "bad-audit", "unit": bad, "expected": "contradicted"},
        {"id": "good-correction", "unit": bad, "expected": "contradicted", "calibration_kind": "correction",
         "correction_oracle": {"expected_gate": "pass", "expected_page_sha256": digest(expected)}},
        {"id": "protected-correction", "unit": {**bad, "protected": True}, "expected": "contradicted",
         "calibration_kind": "correction", "correction_oracle": {"expected_gate": "human"}},
    ]
    directory = tmp_path / "human-cases" / "demo" / "cases"
    directory.mkdir(parents=True)
    for case in cases:
        (directory / (case["id"] + ".json")).write_text(json.dumps(sign("kb-maintenance-human-case",
            {**case, "actor": "fixture-owner", "reason": "Original source establishes the capacity contract."}, key)))
    monkeypatch.setenv("KB_MAINTENANCE_CASES_DIR", str(directory.parent.parent))
    monkeypatch.setenv("KB_CONTAINMENT_CONFIG", json.dumps(cfg))
    offset = [0.0]
    rt.clock = lambda: time.time() + offset[0]
    rt.ledger._clock = rt.clock
    rt.outbox._clock = rt.clock
    return SimpleNamespace(rt=rt, lifecycle=lifecycle, source=source, bad=bad, good=good, now=offset,
                           key=key, publisher=publisher, cfg=cfg, store=store, policy=policy, run=run)


def _install_policy(c, *, view=None):
    from infermatrix_copilot.kb_service.containment import native_ack, refresh_policy
    from infermatrix_copilot.knowledge_view import KnowledgeView
    from unittest.mock import patch
    refresh_policy(c.rt)
    if view is None:
        return native_ack(c.rt)
    with patch.object(KnowledgeView, "current", return_value=view):
        return native_ack(c.rt)


@pytest.mark.parametrize("outcome,passed", [("contradicted", False), ("verified", False), ("unknown", True)])
def test_owner_confirmed_unknown_cannot_calibrate_a_definitive_verdict(tmp_path, monkeypatch, outcome, passed):
    from infermatrix_copilot.kb_service.maintenance_calibration import calibrate, current
    from infermatrix_copilot.knowledge_service.signing import sign
    c = _ready_fixture(tmp_path, monkeypatch)
    origin = tmp_path / "origin"
    path = "repos/demo/core/deployment.md"
    text = ("---\ntitle: Deployment capacity\ncreated: 2026-09-01\nupdated: 2026-09-01\n"
            f'type: guide\ntags: [demo]\nsources: ["{URL}"]\n---\n\n'
            "# Deployment capacity\n\nThe deployed queue capacity is sixteen.\n")
    (origin / "knowledge" / path).write_text(text)
    _git(origin, "add", ".")
    _git(origin, "-c", "user.name=owner", "-c", "user.email=owner@example.invalid", "commit", "-q", "-m", "ambiguous deployment claim")
    snapshot = c.rt.knowledge.fetch()
    unit = page_units(path, text, "demo", snapshot)[0]
    case = {"id": "deployment-unknown", "actor": "fixture-owner", "unit": unit, "expected": "unknown",
            "reason": "Pinned source establishes a code default, but contains no deployment configuration."}
    (tmp_path / "human-cases/demo/cases/deployment-unknown.json").write_text(json.dumps(
        sign("kb-maintenance-human-case", case, c.key)))
    original_answer = c.rt.gateway.answer
    def answer(role, prompt):
        if role.name == "judge" and '"claim"' in prompt and "The deployed queue capacity" in prompt:
            result = semantic(outcome)
            result["reason"] = "Pinned default evidence is insufficient to identify deployed configuration." if outcome == "unknown" else "The code default alone decides deployment capacity."
            if outcome != "unknown":
                result["assessments"][0]["claim"] = "The deployed queue capacity is sixteen."
            return result
        return original_answer(role, prompt)
    c.rt.gateway.answer = answer
    record = calibrate(c.rt, c.store, c.rt.maintenance, run_id="unknown-oracle", policy_sha256=c.policy)
    assert record["correction_passed"]
    assert next(row for row in record["details"] if row["id"] == case["id"])["outcome"] == outcome
    assert record["passed"] is passed
    assert current(c.rt, c.policy) is passed
    assert record["false_accepts"] == (0 if passed else 1)


@pytest.mark.parametrize("rejected,passed", [(1, True), (2, False)])
def test_verified_case_false_reject_tolerance_stays_twenty_percent(tmp_path, monkeypatch, rejected, passed):
    from infermatrix_copilot.kb_service.maintenance_calibration import calibrate
    from infermatrix_copilot.knowledge_service.signing import sign
    c = _ready_fixture(tmp_path, monkeypatch)
    for index in range(4):
        case = {"id": f"good-audit-extra-{index}", "actor": "fixture-owner", "unit": c.good,
                "expected": "verified", "reason": "Original assignment establishes an eight-item default."}
        (tmp_path / f"human-cases/demo/cases/good-audit-extra-{index}.json").write_text(json.dumps(
            sign("kb-maintenance-human-case", case, c.key)))
    original_answer, reviewed = c.rt.gateway.answer, [0]
    def answer(role, prompt):
        if role.name == "judge" and '"claim"' in prompt and "## DEMO-3a" in prompt:
            reviewed[0] += 1
            if reviewed[0] <= rejected:
                return {"outcome": "unknown", "reason": "This review declines a definitive assessment.",
                        "witnesses": [0], "conflicts": [], "assessments": []}
        return original_answer(role, prompt)
    c.rt.gateway.answer = answer
    record = calibrate(c.rt, c.store, c.rt.maintenance, run_id="false-reject-threshold", policy_sha256=c.policy)
    assert reviewed[0] == 5 and record["correction_passed"]
    assert record["passed"] is passed


def test_general_scope_remains_auditable_without_changing_upstream_trial_roster(tmp_path, monkeypatch):
    c = _ready_fixture(tmp_path, monkeypatch)
    general = replace(c.lifecycle, repo="general", full_name="", knowledge_dir="general", mode="shadow")
    c.rt.registry[general.repo] = general
    c.rt.ledger.ensure_repo(general.repo, general.mode)
    origin = tmp_path / "origin"
    path = "general/explanation.md"
    target = origin / "knowledge" / path
    target.parent.mkdir()
    target.write_text("---\ntitle: General explanation\ncreated: 2026-09-01\nupdated: 2026-09-01\n"
                      "type: guide\ntags: [general]\nsources: []\n---\n\n# General explanation\n\n"
                      "This cross-repository guidance has no original upstream pin.\n")
    _git(origin, "add", ".")
    _git(origin, "-c", "user.name=owner", "-c", "user.email=owner@example.invalid", "commit", "-q", "-m", "general guidance scope")
    snapshot = c.rt.knowledge.fetch()
    _, units = maintenance._units(c.rt, snapshot)
    general_unit = next(unit for unit in units if unit["repo"] == "general")
    with pytest.raises(ValueError, match="applicability pin"):
        source_evidence(c.rt, general, general_unit)
    assert maintenance._eligible(c.rt) == ["demo"]
    assert c.store.report(policy_sha256=c.policy, eligible_repos=maintenance._eligible(c.rt))["seven_valid_nights_ready"]


def _publisher_outbox_items(rt):
    return [packet for path in (rt.state_dir / "outbox").glob("*.json")
            if (packet := json.loads(path.read_text())).get("purpose") == "kb-outbox-item"]


def test_private_contradiction_remains_internal_even_when_global_automatic_readiness_passes(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service.maintenance_calibration import calibrate, current
    from infermatrix_copilot.kb_service.containment import pending_holds
    from infermatrix_copilot.kb_service.containment_drill import run_revocation_drill
    c = _ready_fixture(tmp_path, monkeypatch)
    rt = c.rt
    assert calibrate(rt, c.store, rt.maintenance, run_id="owner-calibration", policy_sha256=c.policy)["passed"]
    assert run_revocation_drill(rt, c.policy)["passed"]
    _install_policy(c)
    private = replace(c.lifecycle, upstream_visibility="private")
    rt.registry[private.repo] = private
    assert private.auto_merge and not private.publishes
    # Other public repositories may have completed global readiness; private
    # visibility must independently prevent enforcement and publication.
    monkeypatch.setattr(maintenance, "readiness", lambda *args: True)
    assert current(rt, c.policy, observed_model=rt.judge.model)
    c.store.add_items(c.run["id"], [c.bad])
    before_snapshot = rt.ledger.active_snapshot()
    result = maintenance.tick(rt)
    item = c.store.items(result["run_id"])[0]
    assert item["outcome"] == "contradicted" and item["detail"]["original_source_checked"]
    correction = item["detail"]["correction"]
    assert correction["hold"]["status"] == "would_hold"
    cs = rt.ledger.changeset(correction["changeset_id"])
    assert cs["detail"]["decision"]["status"] == "pass"
    assert cs["status"] == "shadow_recorded"
    assert not pending_holds(rt)["holds"]
    assert not list((rt.containment_policy_dir / "decisions").glob("*.json"))
    assert not _publisher_outbox_items(rt)
    assert rt.ledger.active_snapshot() == before_snapshot
    assert list((rt.state_dir / "maintenance/regression-candidates").glob("*.json"))


@pytest.mark.parametrize("scope", ["*", "demo"])
def test_private_repository_respects_global_and_repository_breaker_pause(tmp_path, monkeypatch, scope):
    c = _ready_fixture(tmp_path, monkeypatch)
    rt = c.rt
    private = replace(c.lifecycle, upstream_visibility="private")
    rt.registry[private.repo] = private
    monkeypatch.setattr(maintenance, "readiness", lambda *args: True)
    c.store.add_items(c.run["id"], [c.bad])
    rt.ledger.bump_generation(scope, pause=True, reason="breaker opened")
    generation = rt.ledger.repo_state(scope)["generation"]
    calls = len(rt.gateway.calls)
    result = maintenance.tick(rt)
    if scope == "*":
        assert result is None
    else:
        assert result["status"] == "failed"
    assert len(rt.gateway.calls) == calls
    assert c.store.items(c.run["id"])[0]["outcome"] is None
    assert rt.ledger.repo_state(scope)["paused"]
    assert rt.ledger.repo_state(scope)["generation"] == generation
    assert not list((rt.containment_policy_dir / "decisions").glob("*.json"))
    assert not _publisher_outbox_items(rt)


def test_automatic_correction_requires_real_seven_nights_calibration_drill_and_current_ack(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service.maintenance_calibration import calibrate, current
    from infermatrix_copilot.kb_service.containment_drill import run_revocation_drill
    from infermatrix_copilot.kb_service.containment import record_hold
    c = _ready_fixture(tmp_path, monkeypatch)
    finding = audit_unit(c.rt, c.lifecycle, c.bad, c.rt.gateway)
    cs = {"detail": {"maintenance_policy": c.policy, "source_audit": finding, "served_generator": c.rt.generator.model}}
    assert c.store.report()["seven_valid_nights_ready"]
    assert maintenance.correction_publishable(c.rt, cs)
    calibration = calibrate(c.rt, c.store, c.rt.maintenance, run_id="owner-calibration", policy_sha256=c.policy)
    assert calibration["passed"], calibration
    assert calibration["correction_passed"] and current(c.rt, c.policy)
    assert maintenance.correction_publishable(c.rt, cs)  # real SDK drill still absent
    assert run_revocation_drill(c.rt, c.policy)["passed"]
    assert maintenance.correction_publishable(c.rt, cs) == "containment propagation incomplete"
    _install_policy(c)
    assert maintenance.correction_publishable(c.rt, cs) == ""
    record_hold(c.rt, c.bad, finding, enforce=True)
    assert maintenance.correction_publishable(c.rt, cs) == "containment propagation incomplete"
    _install_policy(c)
    assert maintenance.correction_publishable(c.rt, cs) == ""
    c.now[0] += 601
    assert maintenance.correction_publishable(c.rt, cs) == "containment propagation incomplete"


def test_merged_correction_restores_only_exact_active_bytes_and_keeps_old_context_denied(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service.activate import build_snapshot, switch_active, verify_snapshot
    from infermatrix_copilot.kb_service.containment import DECISION_PURPOSE, pending_holds, record_hold, record_restoration
    from infermatrix_copilot.kb_service.containment_drill import run_revocation_drill
    from infermatrix_copilot.kb_service.maintenance_calibration import calibrate
    from infermatrix_copilot.kb_service.maintenance_correction import propose_correction
    from infermatrix_copilot.knowledge_service.containment import (
        ContainmentError, _issue_usage, configured, knowledge_availability_check,
    )
    from infermatrix_copilot.knowledge_service.signing import sign, verify

    c = _ready_fixture(tmp_path, monkeypatch)
    rt = c.rt
    assert calibrate(rt, c.store, rt.maintenance, run_id="owner-calibration", policy_sha256=c.policy)["passed"]
    assert run_revocation_drill(rt, c.policy)["passed"]
    _install_policy(c)
    old_sha = c.bad["snapshot"]
    old_snapshot = build_snapshot(rt.state_dir, old_sha, rt.knowledge.knowledge_files(old_sha),
                                  extra=rt.knowledge.top_level_knowledge(old_sha))
    old_view = verify_snapshot(old_snapshot)
    switch_active(rt.state_dir, old_snapshot)
    _install_policy(c, view=old_view)
    context = {"knowledge_snapshot": old_sha, "documents": [{"document_id": PAGE}]}
    old_receipt = _issue_usage(context, view=old_view, config=c.cfg)
    assert knowledge_availability_check(old_receipt, config=c.cfg)["allowed"]

    finding = audit_unit(rt, c.lifecycle, c.bad, rt.gateway)
    c.store.add_items(c.run["id"], [c.bad])
    c.store.record_outcome(c.run["id"], c.bad["unit_id"], "contradicted", detail=finding)
    record_hold(rt, c.bad, finding, enforce=True)
    correction = propose_correction(rt, c.lifecycle, c.store, rt.maintenance, run_id=c.run["id"],
        unit=c.bad, finding=finding, base=rt.knowledge.knowledge_files(old_sha), base_sha=old_sha)
    cs_id = correction["changeset_id"]
    assert rt.ledger.changeset(cs_id)["status"] == "maintenance_held"
    assert not pending_holds(rt)["ready"]
    c.store.enqueue_request("same-defect-next-run", "demo")
    later = c.store.begin_cycle(snapshot=old_sha, policy_sha256=c.policy, request_id="same-defect-next-run",
                                eligible_repos=["demo"])
    calls = len(rt.gateway.calls)
    duplicate = maintenance._correction(rt, c.lifecycle, c.store, later, c.bad, finding,
                                         rt.knowledge.knowledge_files(old_sha))
    assert duplicate["changeset_id"] == cs_id and len(rt.gateway.calls) == calls
    _install_policy(c, view=old_view)
    assert not knowledge_availability_check(old_receipt, config=c.cfg)["allowed"]
    assert maintenance.advance_corrections(rt) == [{"changeset_id": cs_id, "status": "pr_requested"}]
    saved = rt.load_changeset_files(cs_id)
    origin = tmp_path / "origin"
    for path, text in saved["files"].items():
        (origin / "knowledge" / path).write_text(text)
    _git(origin, "add", ".")
    _git(origin, "-c", "user.name=owner", "-c", "user.email=owner@example.invalid", "commit", "-q", "-m", "owner merges corrected capacity")
    merge_sha = rt.knowledge.fetch()
    cs = rt.ledger.changeset(cs_id)
    rt.ledger.update_changeset(cs_id, status="merged", head_sha=merge_sha, merge_sha=merge_sha,
                               detail={**cs["detail"], "post_check": "passed"})
    rt.github = SimpleNamespace(get=lambda path: {"check_runs": [{"head_sha": merge_sha, "name": "suite",
        "status": "completed", "conclusion": "success"}]} if path.endswith("/check-runs") else {"sha": merge_sha, "statuses": []})
    assert maintenance.advance_corrections(rt) == []  # corrected content has not activated
    new_snapshot = build_snapshot(rt.state_dir, merge_sha, rt.knowledge.knowledge_files(merge_sha),
                                   extra=rt.knowledge.top_level_knowledge(merge_sha))
    new_view = verify_snapshot(new_snapshot)
    switch_active(rt.state_dir, new_snapshot)
    rt.ledger.record_activation(merge_sha, {"fixture": True, "exact_gated_bytes": True})
    missing_receipt = maintenance.advance_corrections(rt)
    assert missing_receipt[0]["status"] == "restoration_pending"
    assert "signed publisher" in missing_receipt[0]["reason"]
    envelope = sign("kb-ack", {"kind": "merge", "ok": True, "changeset_id": cs_id,
        "head_sha": merge_sha, "merge_sha": merge_sha, "post_check": "passed"}, c.publisher)
    receipts = rt.state_dir / "receipts" / "publisher"
    receipts.mkdir(parents=True)
    (receipts / (digest(json.dumps(envelope, sort_keys=True)) + ".json")).write_text(json.dumps(envelope))
    restored = maintenance.advance_corrections(rt)
    assert restored[0]["status"] == "new_content_restored_pending_ack", restored
    journal = [verify(DECISION_PURPOSE, json.loads(path.read_text()), c.key.public_key())
               for path in (rt.containment_policy_dir / "decisions").glob("*.json")]
    packet = next(row for row in journal if row["kind"] == "restoration")
    replay = record_restoration(rt, c.bad["unit_id"], approved_hashes=packet["approved_hashes"],
        approved_page_hashes=packet["approved_page_hashes"], proof=packet["proof"])
    assert replay["generation"] == restored[0]["generation"]
    assert maintenance.advance_corrections(rt) == []
    assert pending_holds(rt)["generation"] == restored[0]["generation"]
    barrier = pending_holds(rt)
    assert not barrier["ready"] and not barrier["content_current"]
    # ACKing the latest policy on the old release cannot claim deployed repair.
    _install_policy(c, view=old_view)
    assert pending_holds(rt)["ready"] and not pending_holds(rt)["content_current"]
    assert not knowledge_availability_check(old_receipt, config=c.cfg)["allowed"]
    with configured(c.cfg), pytest.raises(ContainmentError):
        old_view.path(PAGE)
    _install_policy(c, view=new_view)
    barrier = pending_holds(rt)
    assert barrier["ready"] and barrier["content_current"]
    denied = next(row for row in barrier["holds"] if row["unit_id"] == c.bad["unit_id"])
    assert c.bad["content_sha256"] in denied["denied_hashes"]
    fresh = _issue_usage({**context, "knowledge_snapshot": merge_sha}, view=new_view, config=c.cfg)
    assert knowledge_availability_check(fresh, config=c.cfg)["allowed"]
    assert not knowledge_availability_check(old_receipt, config=c.cfg)["allowed"]
    maintenance.advance_corrections(rt)
    assert rt.ledger._conn.execute("SELECT decision FROM maintenance_resolutions WHERE id=?",
        ("restored:" + cs_id,)).fetchone()[0] == "corrected_and_consumers_confirmed"


@pytest.mark.parametrize("failure", ["identity", "cache_write"])
def test_completed_api_reply_keeps_known_overspend_on_rejection(tmp_path, monkeypatch, failure):
    rt, _, _, unit, _ = configured_runtime(tmp_path, lambda role, prompt: {"answer": "original"})
    original = rt.gateway.call_json

    def reply(*args, **kwargs):
        assert kwargs.pop("max_budget_usd") == 1
        result = original(*args, **kwargs)
        return replace(result, cost_usd=9.0,
                       served_model="wrong-family" if failure == "identity" else result.served_model)

    rt.gateway.call_json = reply
    rt.gateway.maintenance_cost_bound = lambda *args, **kwargs: 1.0
    config = replace(rt.maintenance, costs={rt.judge.label(): {
        "kind": "api", "threshold_usd": 1, "in_usd_per_mtok": 5,
        "out_usd_per_mtok": 10, "max_output_tokens": 1000}})
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)

    def wrapper():
        return BudgetGateway(rt, store, config, run_id="overspend", unit=unit, phase="audit")

    if failure == "cache_write":
        import infermatrix_copilot.kb_service.maintenance_audit as module
        def fail_write(*args, **kwargs):
            raise OSError("durable reply write failed")
        monkeypatch.setattr(module, "atomic_write_json", fail_write)
    with pytest.raises(ModelUnavailable if failure == "identity" else OSError):
        wrapper().call_json(rt.judge, system="audit", prompt="data")
    row = rt.ledger._conn.execute("SELECT * FROM maintenance_budget").fetchone()
    assert row["status"] == "settled" and row["outcome"] == "failed"
    assert row["actual_microusd"] == row["charged_microusd"] == 9_000_000
    with pytest.raises(ModelUnavailable, match="not redispatched"):
        wrapper().call_json(rt.judge, system="audit", prompt="data")
    assert len(rt.gateway.calls) == 1
    assert store.report()["budget"]["accounted_usd"] == 9.0


def test_maintenance_cache_replay_preserves_native_quality_receipt(tmp_path):
    from infermatrix_copilot.kb_service.quality_review import QualityPolicy, review_call

    rt, _, _, unit, _ = configured_runtime(tmp_path, lambda role, prompt: {"answer": "original"})
    original = rt.gateway.call_json

    def reply(*args, **kwargs):
        result = original(*args, **kwargs)
        return replace(result, trace_id="native-trace", reply_sha256=digest(result.text),
                       usage={"input_tokens": 17, "output_tokens": 3})

    rt.gateway.call_json = reply
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)

    def review():
        gateway = BudgetGateway(rt, store, rt.maintenance, run_id="receipt", unit=unit, phase="audit")
        return review_call(gateway, rt.judge, policy=QualityPolicy("test", archive="verified"),
                           system="audit", prompt="data", validate=lambda value: None)

    first, cached = review(), review()
    assert first.quality_receipt == cached.quality_receipt
    assert cached.trace_id == "native-trace" and cached.reply_sha256 == digest(cached.text)
    assert cached.usage == {"input_tokens": 17, "output_tokens": 3}
    assert len(rt.gateway.calls) == 1
    assert store.report()["budget"]["accounted_usd"] == .5


def test_legacy_maintenance_cache_without_native_identity_still_replays(tmp_path):
    rt, _, _, unit, _ = configured_runtime(tmp_path, lambda role, prompt: {"answer": "original"})
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)

    def wrapper():
        return BudgetGateway(rt, store, rt.maintenance, run_id="legacy", unit=unit, phase="audit")

    wrapper().call_json(rt.judge, system="audit", prompt="data")
    path = next((rt.state_dir / "maintenance" / "calls").glob("*.json"))
    payload = json.loads(path.read_text())
    # Reconstruct the old cache and its matching ledger receipt, which never
    # included native trace fields. Do not invent a trace identity on replay.
    for key in ("native_trace_id", "native_reply_sha256", "reply_sha256"):
        payload.pop(key)
    checksum = digest(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    path.write_text(json.dumps({**payload, "reply_sha256": checksum}))
    row = rt.ledger._conn.execute("SELECT * FROM maintenance_budget").fetchone()
    metadata = json.loads(row["metadata"])
    metadata["settlement"]["reply_sha256"] = checksum
    rt.ledger._conn.execute("UPDATE maintenance_budget SET metadata=? WHERE id=?",
                           (json.dumps(metadata), row["id"]))
    cached = wrapper().call_json(rt.judge, system="audit", prompt="data")
    assert cached.data == {"answer": "original"}
    assert cached.trace_id == cached.reply_sha256 == ""
    assert len(rt.gateway.calls) == 1
    assert store.report()["budget"]["accounted_usd"] == .5


@pytest.mark.parametrize('actual', [9.0, .1, None])
@pytest.mark.parametrize('kind', ['api', 'subscription'])
def test_durable_reply_recovery_settles_original_cost_once(tmp_path, monkeypatch, actual, kind):
    rt, _, _, unit, _ = configured_runtime(tmp_path, lambda role, prompt: {'answer': 'original'})
    original_call = rt.gateway.call_json

    def reply(*args, **kwargs):
        kwargs.pop('max_budget_usd', None)
        return replace(original_call(*args, **kwargs), cost_usd=actual)

    rt.gateway.call_json = reply
    rt.gateway.maintenance_cost_bound = lambda *args, **kwargs: 1.0
    price = {'kind': 'api', 'threshold_usd': 1, 'in_usd_per_mtok': 5,
             'out_usd_per_mtok': 10, 'max_output_tokens': 1000} if kind == 'api' else {
                 'kind': 'subscription', 'accounted_usd': .5}
    config = replace(rt.maintenance, costs={rt.judge.label(): price})
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)

    def wrapper():
        return BudgetGateway(rt, store, config, run_id='recover-cost', unit=unit, phase='audit')

    settle = store.settle_cost
    def interrupted(*args, **kwargs):
        raise OSError('loss after durable reply before settlement')
    monkeypatch.setattr(store, 'settle_cost', interrupted)
    with pytest.raises(OSError, match='before settlement'):
        wrapper().call_json(rt.judge, system='audit', prompt='data')
    before = dict(rt.ledger._conn.execute('SELECT * FROM maintenance_budget').fetchone())
    assert before['status'] == 'reserved'
    assert len(list((rt.state_dir / 'maintenance/calls').glob('*.json'))) == 1
    monkeypatch.setattr(store, 'settle_cost', settle)
    recovered = wrapper().call_json(rt.judge, system='audit', prompt='data')
    after = dict(rt.ledger._conn.execute('SELECT * FROM maintenance_budget').fetchone())
    expected = int(actual * 1_000_000) if kind == 'api' and actual is not None else None
    assert after['actual_microusd'] == expected
    assert after['charged_microusd'] == (before['worst_microusd'] if expected is None else expected)
    assert after['id'] == before['id'] and after['status'] == 'settled'
    assert recovered.cost_usd == actual
    assert wrapper().call_json(rt.judge, system='audit', prompt='data') == recovered
    assert dict(rt.ledger._conn.execute('SELECT * FROM maintenance_budget').fetchone()) == after
    assert len(rt.gateway.calls) == 1


@pytest.mark.parametrize('changed', ['owner', 'lane', 'allowance', 'cost_kind'])
@pytest.mark.parametrize('pending', [False, True])
def test_cached_maintenance_result_requires_original_reservation_binding(tmp_path, changed, pending, monkeypatch):
    rt, _, _, unit, _ = configured_runtime(tmp_path, lambda role, prompt: {'answer': 'original'})
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    config = rt.maintenance
    def wrapper():
        return BudgetGateway(rt, store, config, run_id='binding', unit=unit, phase='audit')
    if pending:
        with monkeypatch.context() as patch:
            def interrupted(*args, **kwargs):
                raise OSError('settlement interrupted')
            patch.setattr(store, 'settle_cost', interrupted)
            with pytest.raises(OSError, match='settlement interrupted'):
                wrapper().call_json(rt.judge, system='audit', prompt='data')
    else:
        wrapper().call_json(rt.judge, system='audit', prompt='data')
    before = dict(rt.ledger._conn.execute('SELECT * FROM maintenance_budget').fetchone())
    path = next((rt.state_dir / 'maintenance/calls').glob('*.json'))
    cache = path.read_bytes()
    if changed == 'owner':
        unit = {**unit, 'owner': 'other-owner'}
    elif changed == 'lane':
        unit = {**unit, 'lane': 'fair' if unit.get('lane', 'priority') == 'priority' else 'priority'}
    elif changed == 'allowance':
        config = replace(config, costs={rt.judge.label(): {'kind': 'subscription', 'accounted_usd': .6}})
    else:
        config = replace(config, costs={rt.judge.label(): {'kind': 'api', 'threshold_usd': 1,
            'in_usd_per_mtok': 5, 'out_usd_per_mtok': 10, 'max_output_tokens': 1000}})
        rt.gateway.maintenance_cost_bound = lambda *args, **kwargs: 1.0
    with pytest.raises(ModelUnavailable, match='reservation provenance'):
        wrapper().call_json(rt.judge, system='audit', prompt='data')
    assert dict(rt.ledger._conn.execute('SELECT * FROM maintenance_budget').fetchone()) == before
    assert path.read_bytes() == cache and len(rt.gateway.calls) == 1
