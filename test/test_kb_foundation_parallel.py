"""Foundation workers retain native approval, evidence boundaries and recovery."""
import copy
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from pathlib import Path

import pytest

from infermatrix_copilot.kb_service import init_knowledge_parallel as parallel
from infermatrix_copilot.kb_service.init_budget import Budget, BudgetExhausted
from infermatrix_copilot.kb_service.init_coverage import Owner
from infermatrix_copilot.kb_service.init_knowledge import _Knowledge
from infermatrix_copilot.kb_service.init_stages import run_stage
from infermatrix_copilot.kb_service.init_support import InitError, InitRecord, checkpoint_budget
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.llm import Block, Reply
from infermatrix_copilot.trace_store import TraceStore
from test_kb_breadth_subscription import SubscriptionKnowledgeGateway
from test_kb_init_knowledge import KnowledgeGateway, _chain
from test_kb_init_modules import _modules_lifecycle
from test_kb_init_skeleton import _runtime, _tree, world  # noqa: F401


def test_pool_shared_cap_deterministic_assembly_and_single_failure(tmp_path, monkeypatch):
    lock, active, peak = threading.Lock(), 0, 0
    completed, applied, saves = [], [], []
    stage = SimpleNamespace(rt=SimpleNamespace(unlimited_subscription=True, environ={}, state_dir=tmp_path, gateway=None),
        record=InitRecord(stage="knowledge", repo="toy", inputs_digest="identity"), budget=Budget(None))
    stage.budget = checkpoint_budget(None, stage.record, tmp_path)
    jobs = [{"owner": Owner(f"owner-{i}", "same-page", ()), "payload": {}, "offered": {}} for i in range(32)]

    def worker(stage, job):
        nonlocal active, peak
        index = int(job["owner"].owner.split("-")[-1])
        with lock:
            active += 1
            peak = max(peak, active)
        # Generator followed by judges remains inside the same shared worker slot.
        with stage.budget.reserve(1):
            time.sleep((13 - index % 13) / 500)
        with lock:
            active -= 1
        completed.append(index)
        return {"input_sha256": str(index), "error": "unavailable" if index == 4 else "", "index": index}

    monkeypatch.setattr(parallel, "_worker", worker)
    monkeypatch.setattr(parallel, "_validate_result", lambda stage, result: None)
    monkeypatch.setattr(parallel, "_validate_fresh", lambda stage, result: None)
    monkeypatch.setattr(parallel, "_apply", lambda stage, result: applied.append(result["index"]) or [])
    monkeypatch.setattr(InitRecord, "save", lambda record, path: saves.append(threading.current_thread().name))
    list(parallel.run_jobs(stage, jobs))
    assert 1 < peak <= 13 and stage.budget.spent_usd == 32 and stage.budget._reserved == 0
    assert completed != list(range(32)) and applied == list(range(32))
    # Workers persist budget reservations; only the coordinator adds domain
    # results, once for each completed job.
    assert saves.count(threading.current_thread().name) == 64  # Intent and result per job.
    assert any("owner-4" in item for item in stage.record.unfinished)
    saved = stage.record.coverage['foundation_jobs']
    assert len(saved['requests']) == 32 and len(saved['contexts']) == 1
    assert {r['request']['context_sha256'] for r in saved['requests'].values()} == set(saved['contexts'])


@pytest.mark.parametrize('damage', [None, 'owner', 'key', 'empty_list'])
def test_foundation_crash_after_dispatch_holds_original_owner_on_resume(tmp_path, monkeypatch, damage):
    stage = SimpleNamespace(rt=SimpleNamespace(unlimited_subscription=False, state_dir=tmp_path, gateway=None),
        record=InitRecord(stage="knowledge", repo="toy", inputs_digest="frozen"))
    stage.budget = checkpoint_budget(None, stage.record, tmp_path)
    job = {"owner": Owner("owner", "page", ()), "payload": {}, "offered": {}}
    calls = []

    def interrupted(stage, job):
        durable = InitRecord.load(tmp_path, "toy", "knowledge")
        assert durable.coverage["foundation_jobs"]["inflight"]
        calls.append(1)
        raise KeyboardInterrupt()

    monkeypatch.setattr(parallel, "_worker", interrupted)
    with pytest.raises(KeyboardInterrupt):
        list(parallel.run_jobs(stage, [job]))
    stage.record = InitRecord.load(tmp_path, "toy", "knowledge")
    stage.budget = checkpoint_budget(None, stage.record, tmp_path)
    if damage is not None:
        from infermatrix_copilot.kb_service.models import ModelUnavailable
        saved = stage.record.coverage['foundation_jobs']
        key, = saved['inflight']
        if damage == 'owner':
            saved['inflight'][key]['owner'] = 'another-owner'
        elif damage == 'key':
            saved['inflight']['wrong-key'] = saved['inflight'].pop(key)
        else:
            saved['inflight'] = []
        before = copy.deepcopy(saved['inflight'])
        with pytest.raises(ModelUnavailable, match='reconcile retained state'):
            list(parallel.run_jobs(stage, [job]))
        assert calls == [1] and saved['inflight'] == before
        return
    assert list(parallel.run_jobs(stage, [{**job, "payload": {"correction": "new prompt"}}])) == []
    assert calls == [1]
    assert any("reconcile original dispatch" in message for message in stage.record.unfinished)


@pytest.mark.parametrize("value", ["0", "14", "1.5", "01", "bad"])
def test_invalid_parallel_config_and_default_serial(value):
    rt = SimpleNamespace(unlimited_subscription=True, environ={"KB_KNOWLEDGE_CONCURRENCY": value})
    with pytest.raises(InitError, match="1 through 13"):
        parallel.parallelism(rt)
    rt.unlimited_subscription = False
    assert parallel.parallelism(rt) == 1


def test_budget_atomic_ceiling_and_failure_drain():
    budget = Budget(3)
    barrier = threading.Barrier(3)
    accepted = []
    def call():
        try:
            with budget.reserve(1):
                accepted.append(1)
                barrier.wait(timeout=2)
                raise RuntimeError("native failed")
        except (RuntimeError, BudgetExhausted):
            pass
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: call(), range(6)))
    assert len(accepted) == 3 and budget.spent_usd == 3 and budget._reserved == 0


def test_late_discovery_ranges_are_complete_and_gaps_are_not_offered(tmp_path):
    lines = [f"line {i}" for i in range(1, 20001)]
    (tmp_path / "giant.ts").write_text("\n".join(lines))
    refs = [{"path": "giant.ts", "start": 7000, "end": 7010, "pin": "a" * 40},
            {"path": "giant.ts", "start": 19000, "end": 19005, "pin": "a" * 40}]
    stage = _Knowledge.__new__(_Knowledge)
    stage.record = InitRecord("knowledge", "toy", pin="a" * 40)
    stage.source_index = SimpleNamespace(identity={"pin": "a" * 40}, entries={
        "giant.ts": {"status": "ready", "lines": lines, "sha256": "digest"}})
    stage._frozen_discovery = {"candidates": [{"id": "new-feature", "relation": "new", "status": "accepted", "evidence": refs}]}
    shown = parallel.preferred_sources(stage, tmp_path, "new-feature", ["giant.ts"], 100000)
    assert [(item["start"], item["end"]) for item in shown] == [(7000, 7010), (19000, 19005)]
    assert "19000: line 19000" in shown[1]["text"]
    ranges = parallel.offered_ranges(shown)
    stage._section(Owner("new", "page", ()), "page", {}, {"facet": "api", "evidence": [
        {"path": "giant.ts", "start": 7010, "end": 19000}]}, ranges)
    assert stage.record.dropped[0]["why"] == "evidence outside shown input"
    old = parallel.preferred_sources(stage, tmp_path, "old-feature", ["giant.ts"], 100)
    assert old[0]["end"] < 7000  # legacy source selection is untouched
    tiny = parallel.preferred_sources(stage, tmp_path, "new-feature", ["giant.ts"], 10)
    assert all(item["end"] < 7000 for item in tiny) and len(stage.record.unfinished) == 2
    refs[0]["pin"] = "b" * 40
    with pytest.raises(InitError, match="no longer matches"):
        parallel.preferred_sources(stage, tmp_path, "new-feature", ["giant.ts"], 100000)


def _complete_native(world, *, publish=False):
    seed_gateway = KnowledgeGateway()
    _chain(world, seed_gateway)
    if publish:
        from test_kb_init_skeleton import _commit
        skeleton = InitRecord.load(_runtime(world).state_dir, "toy", "skeleton")
        modules = InitRecord.load(_runtime(world).state_dir, "toy", "modules")
        _commit(world["origin"], {**_tree(skeleton), **_tree(modules)}, "merge prerequisite pages")
    state = _runtime(world).state_dir if not publish else world["tmp"] / "publish-state"
    traces = TraceStore(state / "init" / "traces")
    offline = KnowledgeGateway()

    class NativeTransport:
        subscription_billing = True
        supports_native_events = True
        def complete(self, *, system, messages, model, effort, role, **kwargs):
            result = offline.call_json(ModelRole(role, "zcode" if role == "generator" else "codex", model),
                                       system=system, prompt=messages[0]["content"])
            if role == "generator":
                prototype = result.data["sections"][0]
                for facet in ("api", "configuration", "features", "validation"):
                    result.data["sections"].append({**prototype, "facet": facet})
                text = json.dumps(result.data)
            else:
                text = result.text
            return Reply(blocks=[Block(type="text", text=text)], model=model)

    gateway = ModelGateway(None, transport_factory=lambda _: NativeTransport(), recorder=trace_recorder(traces))
    rt = _runtime(world, gateway, state_dir=state, generator=ModelRole("generator", "zcode", "GLM-5.3"))
    if publish:
        from test_kb_init_skeleton import FakeGh
        rt.gh_run = FakeGh(fail_create=1)
    rt.environ = {**rt.environ, "KB_KNOWLEDGE_CONCURRENCY": "2", "KB_KNOWLEDGE_START_INTERVAL_S": "0.001"}
    if publish:
        rt.environ.update(ALLOW_PUSH="1", ALLOW_POST="1", KB_INIT_GIT_AUTHOR="Tester <tester@example.com>")
    record = run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=not publish,
                       from_existing=publish, unlimited_subscription=True)
    assert record.status == ("blocked" if publish else "dry_run"), record.problems
    assert not record.unfinished
    return rt, record, offline, traces



@pytest.mark.parametrize('condition', ['complete', 'different_page', 'unknown', 'legacy', 'lost_save_reply'])
def test_foundation_resume_settles_only_matching_completed_intent(world, condition, monkeypatch):
    rt, record, offline, traces = _complete_native(world)
    original_tree, calls = _tree(record), len(offline.calls)
    saved = record.coverage['foundation_jobs']
    result = next(iter(saved['tasks'].values()))
    binding = {'owner': result['owner'], 'binding': record.inputs_digest, 'attempt': 1,
               'input_sha256': result['input_sha256'], 'page': result['page'],
               'requested': result['requested']}
    assert result.get('outcome_unknown') is False
    assert parallel._dispatch_binding(SimpleNamespace(record=record), **{
        k: result[k] for k in ('owner', 'payload', 'offered', 'page', 'requested')}) == binding
    if condition == 'different_page':
        binding['page'] = 'another-page'
    if condition == 'unknown':
        result['outcome_unknown'] = True
    if condition == 'legacy':
        result.pop('outcome_unknown')
    if condition in ('unknown', 'legacy'):
        result['result_sha256'] = parallel._hash({k: v for k, v in result.items() if k != 'result_sha256'})
    pending = {parallel._hash(binding): binding}
    saved['inflight'] = copy.deepcopy(pending)
    record.status = 'started'
    record.save(rt.state_dir)
    # Reconstruct a pre-stage-artifact checkpoint from completed, archived
    # worker receipts. Keeping the completed stage cache would correctly reject
    # these changed business facts before worker recovery can run.
    executions = InitRecord.path(rt.state_dir, 'toy', 'knowledge').parent / 'executions' / 'knowledge'
    executions.rename(world['tmp'] / 'completed-stage-executions')
    if condition == 'lost_save_reply':
        save = InitRecord.save
        interrupted = []

        def lose_reply(current, path):
            save(current, path)
            if (not interrupted and current.coverage.get('foundation_jobs', {}).get('tasks')
                    and current.coverage['foundation_jobs'].get('inflight') == {}):
                interrupted.append(True)
                raise OSError('lost checkpoint commit reply')

        monkeypatch.setattr(InitRecord, 'save', lose_reply)
        failed = run_stage(rt, _modules_lifecycle(), 'knowledge', dry_run=True, unlimited_subscription=True)
        assert failed.status == 'blocked'
        assert 'lost checkpoint commit reply' in ' '.join(failed.problems)
        assert interrupted and len(offline.calls) == calls
        monkeypatch.setattr(InitRecord, 'save', save)
    second = run_stage(rt, _modules_lifecycle(), 'knowledge', dry_run=True, unlimited_subscription=True)
    assert _tree(second) == original_tree and len(offline.calls) == calls
    assert second.coverage['foundation_jobs']['inflight'] == (
        {} if condition in ('complete', 'lost_save_reply') else pending)
    second.status = 'started'
    second.save(rt.state_dir)
    third = run_stage(rt, _modules_lifecycle(), 'knowledge', dry_run=True, unlimited_subscription=True)
    assert _tree(third) == original_tree and len(offline.calls) == calls
    assert third.coverage['foundation_jobs']['inflight'] == second.coverage['foundation_jobs']['inflight']


@pytest.mark.parametrize("damage", ["judge", "generator", "owner", "facet", "offered", "section"])
def test_native_checkpoint_replay_exact_approval_and_tamper_rejected(world, damage):
    rt, record, offline, traces = _complete_native(world)
    artifacts = [a for task in record.coverage["foundation_jobs"]["tasks"].values() for a in task["artifacts"]]
    assert len(artifacts) == 12 and all(a["judge_receipt"]["native_trace_id"] for a in artifacts)
    first_tree = _tree(record)
    call_count = len(offline.calls)
    # Genuine saved worker approvals reconstruct exact pages without native rejudging.
    record.status = "started"
    record.save(rt.state_dir)
    second = run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=True, unlimited_subscription=True)
    assert _tree(second) == first_tree and len(offline.calls) == call_count
    # Crossing a day boundary keeps the exact original native approval usable.
    second.status = "started"
    second.save(rt.state_dir)
    rt.clock = lambda: 1_790_000_000.0 + 86400
    next_day = run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=True, unlimited_subscription=True)
    assert len(offline.calls) == call_count and len(next_day.evidence) == 12
    saved = copy.deepcopy(record.coverage["foundation_jobs"])
    task = next(iter(saved["tasks"].values()))
    artifact = task["artifacts"][0]
    if damage in ("judge", "generator"):
        artifact[damage + "_receipt"]["native_reply_sha256"] = "0" * 64
    elif damage == "owner":
        artifact["owner"] = "another-owner"
    elif damage == "facet":
        artifact["facet"] = "api" if artifact["facet"] != "api" else "features"
    elif damage == "offered":
        task["offered"][artifact["evidence"][0]["path"]] = [[1, 100000]]
        task["input_sha256"] = parallel._hash({"payload": task["payload"], "offered": task["offered"]})
    else:
        artifact["section"]["interpretation"] = "inference"
    task["result_sha256"] = parallel._hash({k: v for k, v in task.items() if k != "result_sha256"})
    second.coverage["foundation_jobs"] = saved
    second.status = "started"
    second.save(rt.state_dir)
    with pytest.raises(InitError, match="foundation"):
        run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=True, unlimited_subscription=True)
    assert len(offline.calls) == call_count


def test_compact_genuine_handoff_uses_original_archive_for_late_ranges(tmp_path):
    from infermatrix_copilot.kb_service.init_feature_discovery import compact_discovery_report, write_full_discovery_report
    from test_kb_feature_discovery_report import full_report
    stage = _Knowledge.__new__(_Knowledge)
    stage.record = InitRecord("knowledge", "toy", pin="a" * 40)
    worker_state, origin_state = tmp_path / "worker", tmp_path / "original"
    stage.rt = SimpleNamespace(state_dir=worker_state)
    lines = ["readable"] * 20000
    stage.source_index = SimpleNamespace(identity={"pin": "a" * 40}, entries={
        "giant.ts": {"status": "ready", "lines": lines, "sha256": "digest"}})
    full = full_report()
    full["repo"] = "toy"
    full["candidates"] = [{"id": "late", "relation": "new", "status": "accepted", "evidence": [
        {"path": "giant.ts", "start": 7000, "end": 7002, "pin": "a" * 40}]},
        {"id": "frontend", "relation": "implementation_supplement", "related_id": "late", "status": "accepted", "evidence": [
        {"path": "giant.ts", "start": 19000, "end": 19002, "pin": "a" * 40}]},
        {"id": "alias", "relation": "alias", "related_id": "frontend", "status": "accepted", "evidence": [
        {"path": "giant.ts", "start": 19500, "end": 19502, "pin": "a" * 40}]}]
    artifact = write_full_discovery_report(origin_state, full)
    compact = compact_discovery_report(full, artifact)
    stage._frozen_discovery = compact
    genuine = InitRecord("feature-discovery", "toy", pin="a" * 40, status="published", discovery={
        **{k: full[k] for k in ("pin", "catalog_sha256", "index_sha256", "run_config")},
        "done": True, "report_format": compact["report_format"], "full_artifact": artifact})
    genuine.save(worker_state)
    assert "candidates" not in compact
    shown = parallel.preferred_sources(stage, tmp_path, "late", ["giant.ts"], 100000)
    assert [(item["start"], item["end"]) for item in shown] == [(7000, 7002), (19500, 19502), (19000, 19002)]
    assert Path(artifact["path"]).is_relative_to(origin_state)
    del stage._foundation_full_discovery
    genuine.discovery["full_artifact"] = {**artifact, "sha256": "0" * 64}
    genuine.save(worker_state)
    with pytest.raises(InitError, match="artifact bindings differ"):
        parallel.preferred_sources(stage, tmp_path, "late", ["giant.ts"], 100000)


def test_titleless_section_uses_facet_heading_in_serial_mode(world):
    gateway = KnowledgeGateway()
    _chain(world, gateway)
    original = gateway.call_json
    def no_title(role, **kwargs):
        reply = original(role, **kwargs)
        if "sections" in reply.data:
            for section in reply.data["sections"]:
                section.pop("title", None)
        return reply
    gateway.call_json = no_title
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "knowledge", dry_run=True)
    assert record.status == "dry_run" and len(record.evidence) == 4
    assert any("**Architecture**" in text for text in _tree(record).values())


def test_fresh_bad_artifact_preserves_other_approved_facets(monkeypatch):
    result = {"artifacts": [{"key": "good", "verdict": {"verdict": "pass"}},
                            {"key": "bad", "page": "page", "verdict": {"verdict": "pass"}}],
              "unfinished": [], "dropped": [], "verdicts": {}}
    def validate(stage, isolated):
        if any(item["key"] == "bad" for item in isolated["artifacts"]):
            raise InitError("native archive incomplete")
    monkeypatch.setattr(parallel, "_validate_result", validate)
    parallel._validate_fresh(None, result)
    assert [item["key"] for item in result["artifacts"]] == ["good"]
    assert result["verdicts"]["bad"]["verdict"] == "unjudged"
    assert "native archive incomplete" in result["unfinished"][0]


def test_shared_page_assembly_preserves_existing_sections_and_both_owners():
    from infermatrix_copilot.kb_service.init_stages import _page_frontmatter
    from infermatrix_copilot.knowledge_service.pinned_claims import Evidence
    stage = _Knowledge.__new__(_Knowledge)
    stage.repo_dir = "repos/toy"
    stage.today, stage.tags = "2026-10-06", ["toy"]
    stage.lifecycle = SimpleNamespace(full_name="o/toy")
    stage.record = InitRecord("knowledge", "toy", pin="a" * 40)
    page = "repos/toy/components/shared/knowledge.md"
    historical = "Existing accepted explanation remains byte-for-byte."
    stage.head = {page: _page_frontmatter("Shared", kind="architecture", today=stage.today, tags=stage.tags) + historical + "\n"}
    for owner, facet in (("left", "architecture"), ("right", "api")):
        result = stage._append_approved(Owner(owner, page, ()), page, "Shared", facet,
            f"**{facet}**\n\n{owner} independently approved body.\n", [Evidence("pkg/a.py", 1, 1, "b" * 64)])
        assert result is not None
    assert historical in stage.head[page]
    assert stage.head[page].index("owner=left") < stage.head[page].index("owner=right")
    assert len(stage.record.evidence) == 2


@pytest.mark.parametrize("damage", ["missing_generator", "missing_judge", "tampered_generator", "tampered_judge", "binding", "config", "role"])
def test_completed_cache_cannot_skip_proof_replay_or_overwrite_on_failure(world, damage):
    rt, record, offline, traces = _complete_native(world)
    assert record.status == "dry_run" and record.unfinished == []
    artifact = next(iter(record.coverage["foundation_jobs"]["tasks"].values()))["artifacts"][0]
    if damage.startswith("missing_") or damage.startswith("tampered_"):
        role = damage.rsplit("_", 1)[1]
        native = traces.get(artifact[role + "_receipt"]["native_trace_id"])
        attempt = traces.root / "attempts" / native["result"]["native_attempt_id"] / "attempt.json"
        if damage.startswith("missing_"):
            attempt.unlink()
        else:
            saved = json.loads(attempt.read_text())
            saved["status"] = "interrupted"
            attempt.write_text(json.dumps(saved))
    elif damage == "binding":
        record.coverage["foundation_jobs"]["binding"] = "different-inputs"
        record.save(rt.state_dir)
    elif damage == "config":
        rt.gateway._settings = SimpleNamespace(zcode_reasoning_level="low")
    else:
        rt.judge = ModelRole("judge", "codex", "different-model", "medium")
    path = InitRecord.path(rt.state_dir, "toy", "knowledge")
    before, calls = path.read_bytes(), len(offline.calls)
    with pytest.raises(InitError, match="foundation"):
        run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=True, unlimited_subscription=True)
    assert path.read_bytes() == before and len(offline.calls) == calls


@pytest.mark.parametrize("damage", ["missing_generator", "missing_judge", "digest", "role", "config"])
def test_prepared_publication_checks_original_proofs_before_resume(world, damage):
    rt, record, offline, traces = _complete_native(world, publish=True)
    assert record.pr["prepared"] and record.status == "blocked" and not record.unfinished
    artifact = next(iter(record.coverage["foundation_jobs"]["tasks"].values()))["artifacts"][0]
    restore = None
    if damage.startswith("missing_"):
        role = damage.rsplit("_", 1)[1]
        native = traces.get(artifact[role + "_receipt"]["native_trace_id"])
        attempt = traces.root / "attempts" / native["result"]["native_attempt_id"] / "attempt.json"
        restore = attempt, attempt.read_bytes()
        attempt.unlink()
    elif damage == "digest":
        record.inputs_digest = "wrong-inputs"
        record.save(rt.state_dir)
    elif damage == "role":
        rt.judge = ModelRole("judge", "codex", "different-model", "medium")
    else:
        rt.gateway._settings = SimpleNamespace(zcode_reasoning_level="low")
    path = InitRecord.path(rt.state_dir, "toy", "knowledge")
    before, calls, pushes = path.read_bytes(), len(offline.calls), len(rt.gh_run.pushed)
    with pytest.raises(InitError, match="foundation"):
        run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=False, from_existing=True, unlimited_subscription=True)
    assert path.read_bytes() == before and len(offline.calls) == calls and len(rt.gh_run.pushed) == pushes
    if restore:
        restore[0].write_bytes(restore[1])
        resumed = run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=False, from_existing=True, unlimited_subscription=True)
        assert resumed.status == "published" and len(offline.calls) == calls
        # A previously published record remains immutable historical output,
        # including retained limitations; archive loss cannot trigger a rerun.
        resumed.unfinished.append("retained historical scan limitation")
        resumed.save(rt.state_dir)
        restore[0].unlink()
        published_bytes = path.read_bytes()
        cached = run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=False, from_existing=True, unlimited_subscription=True)
        assert cached.status == "published" and path.read_bytes() == published_bytes and len(offline.calls) == calls


def test_original_foundation_request_retains_rendering_and_job_after_crash(tmp_path, monkeypatch):
    from dataclasses import replace
    from infermatrix_copilot.kb_service.config import InitConfig
    from infermatrix_copilot.kb_service.init_budget import Price
    stage = SimpleNamespace(rt=SimpleNamespace(unlimited_subscription=False, state_dir=tmp_path, gateway=None,
                            generator=ModelRole('generator', 'codex', 'fixture'), judge=ModelRole('judge', 'claude', 'fixture')),
        record=InitRecord(stage='knowledge', repo='toy', pin='a'*40, inputs_digest='frozen'),
        head={'page': 'original context'}, tags=['toy'], today='2026-10-13')
    stage.rt.subscription_generator = True
    stage.rt.prices = {'fixture': Price(1, 2, 100)}
    stage.lifecycle = SimpleNamespace(init=InitConfig(judge_call_usd=0.25))
    stage.budget = checkpoint_budget(None, stage.record, tmp_path)
    job = {'owner': Owner('owner', 'page', ()), 'payload': {'facets': ['flow']}, 'offered': {},
           'page': 'page', 'requested': ['flow']}
    calls = []
    def interrupted(stage, job):
        calls.append(1)
        raise KeyboardInterrupt()
    monkeypatch.setattr(parallel, '_worker', interrupted)
    with pytest.raises(KeyboardInterrupt):
        list(parallel.run_jobs(stage, [job]))
    stage.record = InitRecord.load(tmp_path, 'toy', 'knowledge')
    saved = stage.record.coverage['foundation_jobs']
    key, = saved['inflight']; scope = (key, saved['inflight'][key]['attempt'])
    stage.head['page'] = 'new context'; stage.today = '2026-10-14'; job['payload']['facets'] = ['api']
    stage.rt.subscription_generator = False
    stage.rt.prices['fixture'] = Price(10, 20, 1000)
    stage.lifecycle.init = replace(stage.lifecycle.init, judge_call_usd=0.75)
    original = parallel.original_job_request(stage, scope=scope)
    assert original['job']['payload']['facets'] == ['flow']
    assert original['context']['head'] == {'page': 'original context'}
    assert original['context']['today'] == '2026-10-13'
    assert original['context']['judge']['provider'] == 'claude'
    assert original['context']['subscription_generator'] is True
    assert original['context']['prices']['fixture'] == {
        'in_usd_per_mtok': 1, 'out_usd_per_mtok': 2, 'max_output_tokens': 100}
    assert original['context']['init']['judge_call_usd'] == 0.25
    assert original['sequence'] == 0 and len(saved['contexts']) == 1
    assert not list(parallel.run_jobs(stage, [job])) and calls == [1]
    saved['contexts'][original['context_sha256']]['head']['page'] = 'tampered'
    with pytest.raises(InitError, match='context is missing or mismatched'):
        parallel.original_job_request(stage, scope=scope)
