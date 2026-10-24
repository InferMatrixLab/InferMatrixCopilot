"""Real discovery stage publication with offline native transport and archives."""
import asyncio
from dataclasses import replace
import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from infermatrix_copilot.kb_service.init_feature_discovery import SYSTEM_DISCOVER, SYSTEM_REVIEW, SYSTEM_CONSOLIDATE, discovery_run_config
from infermatrix_copilot.kb_service.init_support import InitError
from infermatrix_copilot.kb_service.init_stages import run_stage
from infermatrix_copilot.kb_service.models import ModelGateway
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.llm import Block, Reply
from infermatrix_copilot.trace_store import TraceStore

from test_kb_init_skeleton import FakeGh, _commit, _index, _lifecycle, _runtime, _tree, world  # noqa: F401


class NativeScript:
    """Native subscription stub; the production gateway/recorder stay intact."""
    subscription_billing = True
    supports_native_events = True
    stops_at_spend = True

    def __init__(self, *, path="pkg/core.py", owner="core", candidates=True, supported=True, served=None):
        self.path, self.owner = path, owner
        self.candidates, self.supported, self.served = candidates, supported, served
        self.calls = []

    def complete(self, *, system, messages, model, role, native_event_sink=None, **kwargs):
        prompt = messages[0]["content"]
        payload = json.loads(prompt.split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
        self.calls.append({"system": system, "role": role, "payload": payload, "model": model})
        if system == SYSTEM_DISCOVER:
            rows = []
            if self.candidates and payload["round"] != "doc":
                offered = next((f for f in payload.get("files", []) if f["path"] == self.path), None)
                if offered:
                    rows = [{"id": "engine-step", "title": "Engine step", "owner": self.owner,
                             "description": "Advance the engine through the public step contract.",
                             "relation": "new", "related_id": "", "aliases": [],
                             "evidence": [{"path": self.path, "start": offered["start"], "end": offered["end"]}]}]
            data = {"candidates": rows}
        elif system in (SYSTEM_REVIEW, SYSTEM_CONSOLIDATE):
            data = {"decisions": {row["id"]: {"supported": "yes" if self.supported else "no",
                    "relation": "new" if self.supported else "unknown", "related_id": "",
                    "reason": "The cited public implementation supports the capability." if self.supported else "Unsupported candidate boundary."}
                    for row in payload["candidates"]}}
        else:
            raise AssertionError(f"unexpected native call: {system}")
        if native_event_sink:
            native_event_sink({"type": "native.session.started", "payload": {"session_id": role + "-offline"}})
            native_event_sink({"type": "native.response", "payload": {"model": self.served or model}})
        return Reply(blocks=[Block(type="text", text=json.dumps(data))], stop_reason="end_turn",
                     usage={"input_tokens": 42, "output_tokens": 20}, model=self.served or model)


def _setup(world, *, adapter="toy", policy=None, script=None, archived=True):
    files = {
        "knowledge/repos/_index.md": _index("Repositories", "other", "- [Other](other/_index.md)\n- [Toy](toy/_index.md)\n"),
        "knowledge/repos/toy/_index.md": _index("Toy", "toy", "- [Components](components/_index.md)\n"),
        "knowledge/repos/toy/components/_index.md": _index("Components", "toy", "- [Core](core/_index.md)\n"),
        "knowledge/repos/toy/components/core/_index.md": _index("Core", "toy", "Engine runtime.\n\n## 代码快速入口（Direct）\n\nRead `pkg/core.py` for the engine step.\n"),
        "knowledge/repos/toy/_routes.yaml": yaml.safe_dump({"schema_version": 1, "owners": [
            {"owner": "core", "path": "repos/toy/components/core/_index.md", "scope_prefixes": ["pkg/"]}]}),
        f"adapters/{adapter}/manifest.yaml": "name: toy\nrepo:\n  full_name: o/toy\n",
    }
    if policy:
        files[f"adapters/{adapter}/knowledge-coverage.yaml"] = yaml.safe_dump(policy, sort_keys=False)
    _commit(world["origin"], files, "merged skeleton and adapter")
    script = script or NativeScript()
    state = world["tmp"] / "discovery-stage"
    store = TraceStore(state / "init" / "traces")
    gateway = ModelGateway(None, transport_factory=lambda provider: script,
                           recorder=trace_recorder(store) if archived else None)
    # Shared transport scheduling has separate pacing tests; no clock waits in
    # this fully offline stage test. Native calls and trace receipts remain real.
    gateway.configure_zcode_pacing = lambda pacer: None
    rt = _runtime(world, gateway, state_dir=state)
    lifecycle = _lifecycle(seeds=(), doc_globs=("does-not-exist/**/*.md",), source_roots=("pkg/", "extra/"),
                           feature_discovery_required=True)
    lifecycle = replace(lifecycle, adapter_dir=Path("/installed") / adapter)
    return rt, lifecycle, script, store


def _run(rt, lifecycle):
    return run_stage(rt, lifecycle, "feature-discovery", dry_run=True, from_existing=True,
                     unlimited_subscription=True)


def _save_legacy_record(rt, record):
    """Old runtimes retain native proofs, without executor phase artifacts."""
    from infermatrix_copilot.kb_service.init_support import InitRecord

    # Original upstream647 dry-run writers did not persist prepared JSON.
    # Discovery instead retained independent catalog/report hashes in its state.
    preview = Path(record.pr["dry_run_dir"])
    preview.with_name("feature-discovery-publish.json").unlink()
    record.pr.pop("checked_files_sha256", None)
    for key in ("validation", "validation_pending", "prepared"):
        record.pr.pop(key, None)
    root = InitRecord.path(rt.state_dir, record.repo, "feature-discovery").parent
    shutil.rmtree(root / "executions" / "feature-discovery")
    record.save(rt.state_dir)


def _report(record):
    files = _tree(record)
    path = next(path for path in files if path.startswith("eval/feature-discovery/"))
    return files, json.loads(files[path]), path


def _full_report(record):
    _, report, _ = _report(record)
    artifact = report['full_artifact']
    data = Path(artifact['path']).read_bytes()
    assert artifact == record.discovery['full_artifact']
    assert len(data) == artifact['size_bytes']
    assert hashlib.sha256(data).hexdigest() == artifact['sha256']
    return json.loads(data)


def _policy():
    return {"schema_version": 1, "required": False, "semantic_depth": {"per_facet_gt": 0.92},
            "core": {"roots": ["pkg/"], "exclude": ["*/tests/*"], "target": 0.97, "suffixes": [".py"]},
            "features": [{"id": "existing-step", "title": "Existing step", "owner": "core",
                          "source_globs": ["pkg/core.py"], "entry_points": ["pkg/core.py"], "docs": [],
                          "page": "repos/toy/components/core/feature-existing-step.md"}]}


def test_discovery_runtime_configuration_and_effective_reasoning():
    rt = SimpleNamespace(generator=SimpleNamespace(provider='zcode'),
                         gateway=ModelGateway(SimpleNamespace(zcode_reasoning_level='low')))
    assert discovery_run_config(rt, {}) == {'packet_chars': 24000,
        'zcode_start_interval_s': 15.0, 'zcode_reasoning_level': 'low'}
    assert discovery_run_config(rt, {'KB_DISCOVERY_PACKET_CHARS': '96000',
        'KB_DISCOVERY_START_INTERVAL_S': '2.5'}) == {'packet_chars': 96000,
        'zcode_start_interval_s': 2.5, 'zcode_reasoning_level': 'low'}


@pytest.mark.parametrize('key,value', [('KB_DISCOVERY_PACKET_CHARS', '23999'),
    ('KB_DISCOVERY_PACKET_CHARS', '192001'), ('KB_DISCOVERY_PACKET_CHARS', '96000.5'),
    ('KB_DISCOVERY_PACKET_CHARS', 'garbage'), ('KB_DISCOVERY_START_INTERVAL_S', '0'),
    ('KB_DISCOVERY_START_INTERVAL_S', 'nan'), ('KB_DISCOVERY_START_INTERVAL_S', 'inf'),
    ('KB_DISCOVERY_START_INTERVAL_S', '61')])
def test_invalid_discovery_configuration_refuses_before_model_dispatch(key, value):
    rt = SimpleNamespace(generator=SimpleNamespace(provider='zcode'), gateway=ModelGateway(None))
    with pytest.raises(InitError, match=key):
        discovery_run_config(rt, {key: value})


@pytest.mark.parametrize('changed', ['packet', 'pacing', 'reasoning'])
def test_completed_discovery_cannot_resume_under_changed_configuration(world, monkeypatch, changed):
    rt, lifecycle, script, store = _setup(world)
    first = _run(rt, lifecycle)
    assert first.status == 'dry_run', first.problems
    calls = len(script.calls)
    if changed == 'packet':
        rt.environ['KB_DISCOVERY_PACKET_CHARS'] = '96000'
    elif changed == 'pacing':
        rt.environ['KB_DISCOVERY_START_INTERVAL_S'] = '10'
    else:
        rt.gateway._settings = SimpleNamespace(zcode_reasoning_level='low')
    second = _run(rt, lifecycle)
    assert second.status == 'blocked'
    assert any('inputs changed' in p for p in second.problems)
    assert len(script.calls) == calls


@pytest.mark.parametrize('changed', ['packet', 'pacing', 'reasoning'])
def test_prepared_discovery_rejects_changed_config_and_restores_exact_publication(world, changed):
    from infermatrix_copilot.kb_service.init_support import load_prepared
    rt, lifecycle, script, _ = _setup(world)
    rt.environ.update(ALLOW_PUSH='1', ALLOW_POST='1', KB_INIT_GIT_AUTHOR='t <t@example.com>')
    gh = FakeGh(fail_create=1)
    rt.gh_run = gh

    def publish():
        return run_stage(rt, lifecycle, 'feature-discovery', dry_run=False, from_existing=True,
                         unlimited_subscription=True)

    pending = publish()
    assert pending.status == 'blocked' and pending.pr.get('prepared'), pending.problems
    prepared = load_prepared(pending.pr['prepared'])
    prepared_bytes = Path(pending.pr['prepared']).read_bytes()
    calls, pushes = len(script.calls), list(gh.pushed)
    if changed == 'packet':
        rt.environ['KB_DISCOVERY_PACKET_CHARS'] = '96000'
    elif changed == 'pacing':
        rt.environ['KB_DISCOVERY_START_INTERVAL_S'] = '10'
    else:
        rt.gateway._settings = SimpleNamespace(zcode_reasoning_level='low')
    rejected = publish()
    assert rejected.status == 'blocked'
    assert any('discovery inputs changed' in p for p in rejected.problems)
    assert len(script.calls) == calls and gh.pushed == pushes
    assert Path(pending.pr['prepared']).read_bytes() == prepared_bytes
    rt.environ.pop('KB_DISCOVERY_PACKET_CHARS', None)
    rt.environ.pop('KB_DISCOVERY_START_INTERVAL_S', None)
    rt.gateway._settings = None
    restored = publish()
    assert restored.status == 'published', restored.problems
    assert len(script.calls) == calls and gh.pushed == pushes
    assert load_prepared(restored.pr['prepared']) == prepared


def test_effective_native_reasoning_is_bound_to_report_and_both_archives(world, monkeypatch):
    rt, lifecycle, script, store = _setup(world)
    rt.environ.update(KB_DISCOVERY_PACKET_CHARS='96000', KB_DISCOVERY_START_INTERVAL_S='10')
    rt.gateway._settings = SimpleNamespace(zcode_reasoning_level='low')
    record = _run(rt, lifecycle)
    assert record.status == 'dry_run', record.problems
    _, report, _ = _report(record)
    assert report['run_config'] == record.discovery['run_config'] == {
        'packet_chars': 96000, 'zcode_start_interval_s': 10.0, 'zcode_reasoning_level': 'low'}
    for candidate in _full_report(record)['candidates']:
        for proof in candidate['generator_receipts']:
            archived = store.get(proof['trace_id'])
            assert archived['model']['effort'] == ''
            assert archived['model']['native_reasoning_level'] == 'low'
            attempt = json.loads((store.root / 'attempts' / archived['result']['native_attempt_id'] / 'attempt.json').read_text())
            assert attempt['model']['native_reasoning_level'] == 'low'


def test_discovery_uses_runtime_environment_instead_of_ambient_environment(world, monkeypatch):
    monkeypatch.setenv('KB_DISCOVERY_PACKET_CHARS', 'invalid-ambient-value')
    rt, lifecycle, _, _ = _setup(world)
    record = _run(rt, lifecycle)
    assert record.status == 'dry_run', record.problems
    assert record.discovery['run_config']['packet_chars'] == 24000


def test_zcode_role_effort_does_not_mislabel_effective_native_configuration(world):
    rt, lifecycle, _, store = _setup(world)
    rt.environ['KB_DISCOVERY_GENERATOR'] = 'zcode:GLM-5.3:low'
    rt.gateway._settings = SimpleNamespace(zcode_reasoning_level='max')
    record = _run(rt, lifecycle)
    assert record.status == 'dry_run', record.problems
    _, report, _ = _report(record)
    assert report['run_config']['zcode_reasoning_level'] == 'max'
    for candidate in _full_report(record)['candidates']:
        for proof in candidate['generator_receipts']:
            model = store.get(proof['trace_id'])['model']
            assert model['effort'] == 'low'
            assert model['native_reasoning_level'] == 'max'


def test_configured_reasoning_environment_is_loaded_by_settings(monkeypatch):
    from infermatrix_copilot.config import Settings
    monkeypatch.setenv('ZCODE_REASONING_LEVEL', 'low')
    assert ModelGateway(Settings(_env_file=None)).zcode_reasoning_level == 'low'


def test_native_archive_reasoning_metadata_mismatch_is_rejected(world):
    from infermatrix_copilot.kb_service.init_feature_discovery import _FeatureDiscovery, _prompt, validate_candidates
    from infermatrix_copilot.kb_service.models import ModelRole, ModelUnavailable
    rt, lifecycle, _, store = _setup(world)
    role = ModelRole('generator', 'zcode', 'GLM-5.3')
    reply = rt.gateway.call_json(role, system=SYSTEM_DISCOVER,
        prompt=_prompt({'round': 'source', 'files': []}), validate=validate_candidates)
    archived = store.get(reply.trace_id)
    path = store.root / 'attempts' / archived['result']['native_attempt_id'] / 'attempt.json'
    attempt = json.loads(path.read_text())
    attempt['model']['native_reasoning_level'] = 'low'
    path.write_text(json.dumps(attempt))
    stage = _FeatureDiscovery(rt, lifecycle, dry_run=True, pin=None)
    with pytest.raises(ModelUnavailable, match='reasoning level'):
        stage._verify_native_receipt(reply)


def test_first_source_only_catalog_has_native_review_and_no_knowledge_rewrite(world):
    rt, lifecycle, script, store = _setup(world)
    record = _run(rt, lifecycle)
    assert record.status == "dry_run", record.problems
    files, report, path = _report(record)
    assert set(files) == {"adapters/toy/knowledge-coverage.yaml", path}
    policy = yaml.safe_load(files["adapters/toy/knowledge-coverage.yaml"])
    assert policy["features"][0]["docs"] == []
    assert report["complete"] and report["done"] and report["feature_ids"] == ["engine-step"]
    assert report["historical_denominator"] == 0 and report["facet_denominator"] == 7
    assert report["catalog_sha256"] == hashlib.sha256(files["adapters/toy/knowledge-coverage.yaml"].encode()).hexdigest()
    assert report["scan_mode"] == "full" and report["scan_paths"] is None
    assert report["unit_summary"]["associated"] == record.discovery["unit_resolution"]["associated"]
    assert report["residual_summary"]["completed_rounds"] == 2
    bundle = report["evidence_bundles"]["engine-step"]
    assert bundle["pin"] == record.pin and bundle["catalog_sha256"] == report["catalog_sha256"]
    assert bundle["refs"] and bundle["unit_ids"]
    assert all("text" not in ref for ref in bundle["refs"])
    candidate = _full_report(record)["candidates"][0]
    assert candidate["status"] == "accepted"
    receipts = candidate["generator_receipts"] + [candidate["judge_receipt"]]
    for receipt in receipts:
        saved = store.get(receipt["trace_id"])
        assert not saved["error"] and saved["outputs"]["reply"] == "sha256:" + receipt["reply_sha256"]
    assert {call["model"] for call in script.calls} == {"GLM-5.3", "gpt-6.1-sol"}


def test_first_catalog_without_supported_features_blocks_but_preserves_checkpoint(world):
    rt, lifecycle, _, _ = _setup(world, script=NativeScript(supported=False))
    record = _run(rt, lifecycle)
    assert record.status == "blocked" and not record.discovery["done"]
    assert any("no implemented feature" in problem for problem in record.problems)
    assert record.discovery["reviews"]["engine-step"]["supported"] == "no"
    assert record.discovery["reviews"]["engine-step"]["attempts"] == 4
    assert not record.pr.get("dry_run_dir")


def test_existing_seed_survives_all_unknown_candidates_without_target_changes(world):
    original = _policy()
    rt, lifecycle, _, _ = _setup(world, policy=original, script=NativeScript(supported=False))
    record = _run(rt, lifecycle)
    assert record.status == "dry_run", record.problems
    files, report, _ = _report(record)
    path = "adapters/toy/knowledge-coverage.yaml"
    current = yaml.safe_load(files.get(path) or rt.knowledge.show(record.kb_base_sha, path))
    assert current == original
    assert path not in files  # an unchanged catalog is not rewritten
    assert report["feature_ids"] == ["existing-step"]
    assert report["counts"] == {"unknown": 1}
    assert report["historical_denominator"] == report["facet_denominator"] == 7


def test_missing_native_archive_prevents_first_catalog_acceptance(world):
    rt, lifecycle, script, _ = _setup(world, archived=False)
    record = _run(rt, lifecycle)
    assert record.status == "blocked" and not record.discovery["done"]
    assert all(task["status"] == "unknown" for task in record.discovery["tasks"].values())
    assert any("archive receipt" in task["reason"] for task in record.discovery["tasks"].values())
    assert not [call for call in script.calls if call["role"] == "judge"]


def test_actual_adapter_name_and_new_unknown_language_owner_request(world):
    _commit(world["upstream"], {"extra/connector.novel": "public connector opens a channel\n"}, "novel public library")
    rt, lifecycle, _, _ = _setup(world, adapter="toy_package",
        script=NativeScript(path="extra/connector.novel", owner="connector"))
    record = _run(rt, lifecycle)
    assert record.status == "dry_run", record.problems
    files, report, path = _report(record)
    assert set(files) == {"adapters/toy_package/knowledge-coverage.yaml", path}
    assert report["owner_requests"] == [{"owner": "connector", "title": "connector", "feature_ids": ["engine-step"],
        "source_paths": ["extra/connector.novel"], "page": "repos/toy/components/connector/_index.md"}]
    from infermatrix_copilot.kb_service.knowledge_coverage import load_policy, inventory
    parsed = load_policy(files["adapters/toy_package/knowledge-coverage.yaml"], "repos/toy")
    # New catalog preserves the discoverable extension for the breadth/depth
    # inventory; a catalog entry must not disappear downstream.
    assert ".novel" in parsed.suffixes
    assert "extra/connector.novel" in inventory(world["upstream"], parsed)


def test_native_receipt_requires_replayable_reply_blob(world):
    from infermatrix_copilot.kb_service.init_feature_discovery import _FeatureDiscovery, _prompt, validate_candidates
    from infermatrix_copilot.kb_service.models import ModelRole, ModelUnavailable

    rt, lifecycle, _, store = _setup(world)
    role = ModelRole("generator", "zcode", "GLM-5.3")
    payload = {"round": "source", "files": [{"path": "pkg/core.py", "start": 1, "end": 3,
                                               "text": "class Engine:\n def step(self):\n  return 1"}]}
    reply = rt.gateway.call_json(role, system=SYSTEM_DISCOVER, prompt=_prompt(payload), validate=validate_candidates)
    stage = _FeatureDiscovery(rt, lifecycle, dry_run=True, pin=None)
    stage._verify_native_receipt(reply)
    digest = reply.reply_sha256
    blob = store.root / "blobs" / digest[:2] / f"{digest}.gz"
    blob.write_bytes(b"corrupt archived response")
    with pytest.raises(ModelUnavailable, match="archive"):
        stage._verify_native_receipt(reply)


def test_extensionless_cli_remains_in_first_catalog_production_scope(world):
    _commit(world["upstream"], {"extra/bin/engine": "#!/usr/bin/env python3\ndef step(): return 1\n"}, "extensionless library CLI")
    rt, lifecycle, _, _ = _setup(world, script=NativeScript(path="extra/bin/engine", owner="cli"))
    record = _run(rt, lifecycle)
    assert record.status == "dry_run", record.problems
    files, report, _ = _report(record)
    from infermatrix_copilot.kb_service.knowledge_coverage import load_policy, inventory
    policy = load_policy(files["adapters/toy/knowledge-coverage.yaml"], "repos/toy")
    assert "engine" in policy.filenames
    assert "extra/bin/engine" in inventory(world["upstream"], policy)
    assert report["owner_requests"][0]["owner"] == "cli"


def test_invalid_existing_catalog_blocks_before_native_discovery_calls(world):
    policy = _policy()
    policy["core"]["target"] = 0
    rt, lifecycle, script, _ = _setup(world, policy=policy)
    record = _run(rt, lifecycle)
    assert record.status == "blocked"
    assert any("policy" in problem or "catalog" in problem for problem in record.problems)
    assert script.calls == []


def test_busy_discovery_batch_preserves_active_checkpoint_before_parent_run(world, monkeypatch):
    import fcntl
    from infermatrix_copilot.kb_service.init_support import InitError, InitRecord

    rt, lifecycle, script, _ = _setup(world)
    checkpoint = InitRecord(stage="feature-discovery", repo="toy", pin=world["pin"],
                            status="started", spent_usd=1.5,
                            discovery={"identity": "active-batch", "tasks": {"source:active": {"status": "pending"}}})
    path = checkpoint.save(rt.state_dir)
    before = path.read_bytes()
    journal = path.with_name("discovery-budget.json")
    journal.write_text('{"identity":"active-batch","spent_usd":1.5,"reserved_usd":0.5}')
    journal_before = journal.read_bytes()
    lock_path = path.with_name("feature-discovery.lock")
    # Even reading the parent's inputs before acquiring the lease is forbidden.
    monkeypatch.setattr(rt.knowledge, "fetch", lambda: pytest.fail("parent stage ran before the discovery lease"))
    with lock_path.open("a+b") as active_lock:
        fcntl.flock(active_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(InitError, match="already running"):
            _run(rt, lifecycle)
    assert path.read_bytes() == before
    assert journal.read_bytes() == journal_before
    assert script.calls == []


def test_discovery_lease_is_released_when_parent_run_fails(world, monkeypatch):
    import fcntl
    from infermatrix_copilot.kb_service.init_feature_discovery import _FeatureDiscovery
    from infermatrix_copilot.kb_service.init_support import InitError

    rt, lifecycle, _, _ = _setup(world)

    def fail(stage):
        raise InitError("parent input failure")

    from infermatrix_copilot.kb_service import init_execution
    monkeypatch.setattr(init_execution, "bind", fail)
    stage = _FeatureDiscovery(rt, lifecycle, dry_run=True, pin=None)
    with pytest.raises(InitError, match="parent input failure"):
        asyncio.run(init_execution.execute_init(stage))
    lock_path = rt.state_dir / "init" / "toy" / "feature-discovery.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_resumed_missing_native_archive_invalidates_only_affected_candidate(world):
    from infermatrix_copilot.kb_service.init_feature_discovery import _FeatureDiscovery
    rt, lifecycle, _, store = _setup(world)
    record = _run(rt, lifecycle)
    assert record.status == 'dry_run'
    stage = _FeatureDiscovery(rt, lifecycle, dry_run=True, pin=None)
    state = record.discovery
    from infermatrix_copilot.kb_service.models import ModelRole
    stage.rt = replace(rt, generator=ModelRole.parse('generator', state['candidates']['engine-step']['generator_receipts'][0]['requested']),
                       judge=ModelRole.parse('judge', state['reviews']['engine-step']['judge_receipt']['requested']))
    stage._validate_saved_archives(state)
    assert state['reviews']['engine-step']['supported'] == 'yes'
    generator_proof = state['candidates']['engine-step']['generator_receipts'][0]
    digest = generator_proof['reply_sha256']
    (store.root / 'blobs' / digest[:2] / f'{digest}.gz').unlink()
    stage._validate_saved_archives(state)
    assert state['reviews']['engine-step']['supported'] == 'unsure'
    assert 'archive' in state['candidates']['engine-step']['invalid_reason']


def test_completed_empty_shard_requires_native_archive_before_cache_reuse(world):
    from infermatrix_copilot.kb_service.init_feature_discovery import _FeatureDiscovery
    from infermatrix_copilot.kb_service.init_support import InitError
    from infermatrix_copilot.kb_service.models import ModelRole
    rt, lifecycle, _, store = _setup(world, policy=_policy(), script=NativeScript(candidates=False))
    record = _run(rt, lifecycle)
    assert record.status == 'dry_run'
    task = next(t for t in record.discovery['tasks'].values() if t['status'] == 'complete')
    proof = task['receipt']
    stage = _FeatureDiscovery(replace(rt, generator=ModelRole.parse('generator',proof['requested'])), lifecycle, dry_run=True, pin=None)
    assert stage._cache_reusable(record)
    digest=proof['reply_sha256']
    (store.root/'blobs'/digest[:2]/f'{digest}.gz').unlink()
    with pytest.raises(InitError,match='archives'):
        stage._cache_reusable(record)
    assert task['status']=='complete'  # Cache refusal never rewrites retained proofs.


@pytest.mark.parametrize('tamper',['decision','candidate'])
def test_restored_approval_binds_native_verdict_and_input_candidate(world,tamper):
    from infermatrix_copilot.kb_service.init_feature_discovery import _FeatureDiscovery,_hash
    from infermatrix_copilot.kb_service.models import ModelRole
    rt,lifecycle,_,_=_setup(world,policy=_policy(),script=NativeScript(supported=tamper!='decision'))
    record=_run(rt,lifecycle)
    assert record.status=='dry_run'
    state=record.discovery;row=state['candidates']['engine-step'];checked=state['reviews']['engine-step']
    stage=_FeatureDiscovery(replace(rt,generator=ModelRole.parse('generator',row['generator_receipts'][0]['requested']),
        judge=ModelRole.parse('judge',checked['judge_receipt']['requested'])),lifecycle,dry_run=True,pin=None)
    if tamper=='decision':
        checked.update(supported='yes',relation='new')
    else:
        row['description']='A different claim that the independent judge never assessed.'
        checked['candidate_sha256']=_hash(row)
    failures=stage._validate_saved_archives(state)
    assert 'engine-step' in failures and checked['judge_receipt']
    assert state['reviews']['engine-step']['supported']=='unsure'
    assert 'native approval binding' in row['invalid_reason']


@pytest.mark.parametrize('damage', ['missing', 'tampered'])
def test_cached_compact_report_requires_original_external_artifact(world, damage):
    from infermatrix_copilot.kb_service.init_support import InitRecord
    rt, lifecycle, script, _ = _setup(world, policy=_policy(), script=NativeScript(candidates=False))
    first = _run(rt, lifecycle)
    assert first.status == 'dry_run', first.problems
    files, compact, _ = _report(first)
    assert 'adapters/toy/knowledge-coverage.yaml' not in files  # Unchanged catalog comes from base.
    path = Path(compact['full_artifact']['path']); original = path.read_bytes()
    checkpoint = InitRecord.path(rt.state_dir, lifecycle.repo, 'feature-discovery')
    checkpoint_bytes = checkpoint.read_bytes(); calls = len(script.calls)
    if damage == 'missing': path.unlink()
    else: path.write_bytes(original + b'corruption')
    with pytest.raises(InitError, match='full report artifact'):
        _run(rt, lifecycle)
    assert checkpoint.read_bytes() == checkpoint_bytes and len(script.calls) == calls
    path.write_bytes(original)
    restored = _run(rt, lifecycle)
    assert restored == first and len(script.calls) == calls


@pytest.mark.parametrize('damage', ['missing', 'tampered'])
def test_prepared_compact_artifact_blocks_publication_until_exact_restore(world, damage):
    from infermatrix_copilot.kb_service.init_support import load_prepared
    rt, lifecycle, script, _ = _setup(world)
    rt.environ.update(ALLOW_PUSH='1', ALLOW_POST='1', KB_INIT_GIT_AUTHOR='t <t@example.com>')
    gh = FakeGh(fail_create=1); rt.gh_run = gh
    def publish():
        return run_stage(rt, lifecycle, 'feature-discovery', dry_run=False, from_existing=True,
                         unlimited_subscription=True)
    first = publish()
    assert first.status == 'blocked' and first.pr.get('prepared'), first.problems
    artifact = Path(first.discovery['full_artifact']['path']); original = artifact.read_bytes()
    prepared = Path(first.pr['prepared']); prepared_bytes = prepared.read_bytes()
    calls, pushes = len(script.calls), list(gh.pushed)
    if damage == 'missing': artifact.unlink()
    else: artifact.write_bytes(original + b'corruption')
    blocked = publish()
    assert blocked.status == 'blocked' and any('full report artifact' in p for p in blocked.problems)
    assert prepared.read_bytes() == prepared_bytes and len(script.calls) == calls and gh.pushed == pushes
    artifact.write_bytes(original)
    restored = publish()
    assert restored.status == 'published', restored.problems
    assert prepared.read_bytes() == prepared_bytes and len(script.calls) == calls and gh.pushed == pushes
    compact = json.loads(load_prepared(prepared)['files'][restored.discovery['report_path']])
    assert compact['full_artifact'] == restored.discovery['full_artifact']


def test_completed_legacy_preview_rerenders_without_new_native_calls(world, monkeypatch):
    from infermatrix_copilot.kb_service.init_support import InitRecord
    rt, lifecycle, script, _ = _setup(world)
    first = _run(rt, lifecycle)
    assert first.status == 'dry_run'
    _, compact, path = _report(first)
    full = _full_report(first)
    # Represent a genuine old-format completed preview, retaining all native
    # scan, primary and boundary receipts; this is not a prepared publication.
    old_text = json.dumps(full, ensure_ascii=False, indent=2) + '\n'
    (Path(first.pr['dry_run_dir']) / 'tree' / path).write_text(old_text)
    first.discovery.pop('report_format'); first.discovery.pop('full_artifact')
    first.discovery['report_sha256'] = hashlib.sha256(old_text.encode()).hexdigest()
    _save_legacy_record(rt, first)
    receipts = json.dumps(first.discovery['reviews'], sort_keys=True)
    calls = len(script.calls)
    monkeypatch.setattr(script, 'complete', lambda **kwargs: pytest.fail('legacy report rerender called a model'))
    restored = _run(rt, lifecycle)
    assert restored.status == 'dry_run', restored.problems
    assert len(script.calls) == calls
    assert json.dumps(restored.discovery['reviews'], sort_keys=True) == receipts
    assert _report(restored)[1] == compact
    assert _full_report(restored) == full
    assert InitRecord.load(rt.state_dir, lifecycle.repo, 'feature-discovery').discovery['full_artifact'] == compact['full_artifact']


def test_legacy_published_report_is_immutable_without_compact_marker(world, monkeypatch):
    rt, lifecycle, script, _ = _setup(world)
    first = _run(rt, lifecycle)
    first.status = 'published'
    first.discovery.pop('report_format'); first.discovery.pop('full_artifact')
    first.save(rt.state_dir)
    calls = len(script.calls)
    monkeypatch.setattr(script, 'complete', lambda **kwargs: pytest.fail('published history called a model'))
    assert _run(rt, lifecycle) == first and len(script.calls) == calls


def test_cached_missing_boundary_archive_preserves_proofs_and_blocks_resampling(world):
    from infermatrix_copilot.kb_service.init_support import InitRecord
    rt, lifecycle, script, store = _setup(world)
    first = _run(rt, lifecycle)
    checked = first.discovery['boundary_reviews']['engine-step']
    archive = store.get(checked['judge_receipt']['trace_id'])
    # Responses may share a blob with the primary review; remove only the
    # distinct boundary attempt so primary validation still succeeds.
    (store.root / 'attempts' / archive['result']['native_attempt_id'] / 'attempt.json').unlink()
    path = InitRecord.path(rt.state_dir, lifecycle.repo, 'feature-discovery')
    original = path.read_bytes(); calls = len(script.calls)
    with pytest.raises(InitError, match='archives'):
        _run(rt, lifecycle)
    assert path.read_bytes() == original and len(script.calls) == calls


@pytest.mark.parametrize("damage", ["edit", "remove", "add"])
def test_legacy_discovery_preview_tampering_refuses_before_any_new_calls(world, damage):
    rt, lifecycle, script, _ = _setup(world)
    record = _run(rt, lifecycle)
    assert record.status == "dry_run", record.problems
    _save_legacy_record(rt, record)
    from infermatrix_copilot.kb_service.init_support import InitRecord
    checkpoint = InitRecord.path(rt.state_dir, "toy", "feature-discovery")
    before = checkpoint.read_bytes()
    calls = len(script.calls)
    tree = Path(record.pr["dry_run_dir"]) / "tree"
    target = tree / record.discovery["report_path"]
    if damage == "edit":
        target.write_text("unchecked replacement")
    elif damage == "remove":
        target.unlink()
    else:
        (tree / "unchecked.md").write_text("new file")
    with pytest.raises(InitError, match="preview"):
        _run(rt, lifecycle)
    assert checkpoint.read_bytes() == before and len(script.calls) == calls
