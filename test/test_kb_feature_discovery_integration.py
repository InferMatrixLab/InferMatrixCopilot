"""Discovery is additive, but a started catalog cannot be bypassed downstream."""

import asyncio
import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from infermatrix_copilot.kb_service.config import InitConfig, LifecycleConfigError, parse_init
from infermatrix_copilot.kb_service.init_stages import _Chain, _Stage, run_stage
from infermatrix_copilot.kb_service.init_support import InitError, InitPublisher, InitRecord

from test_kb_init_skeleton import _commit, _index, _lifecycle, _runtime, world  # noqa: F401


def _artifacts(upstream_pin, *, adapter="toy", **changes):
    policy = yaml.safe_dump({
        "schema_version": 1, "required": False,
        "core": {"roots": ["pkg/"], "exclude": [], "target": 0.85},
        "features": [{"id": "step", "title": "Tick step", "owner": "core",
                      "source_globs": ["pkg/core.py"], "entry_points": ["pkg/core.py"],
                      "docs": ["README.md"], "page": "repos/toy/components/core/feature-step.md"}],
    })
    report = {"schema_version": 1, "repo": "toy", "pin": upstream_pin, "complete": True,
              "done": True, "feature_ids": ["step"],
              "features": [{"id": "step", "owner": "core", "title": "Tick step"}],
              "catalog_sha256": hashlib.sha256(policy.encode()).hexdigest(),
              "owner_requests": [{"owner": "core", "title": "Core", "feature_ids": ["step"],
                                  "source_paths": ["pkg/core.py"], "page": "repos/toy/components/core/_index.md"}],
              **changes}
    path = f"eval/feature-discovery/toy-{upstream_pin[:12]}.json"
    text = json.dumps(report, ensure_ascii=False)
    return {f"adapters/{adapter}/knowledge-coverage.yaml": policy, path: text}, report, path


def _stage(world, *, required=False, dry_run=True, adapter=None):
    lifecycle = _lifecycle(feature_discovery_required=required)
    if adapter:
        lifecycle = replace(lifecycle, adapter_dir=Path(adapter))
    stage = _Stage(_runtime(world), lifecycle, dry_run=dry_run, pin=None)
    stage.STAGE = "modules"
    stage.repo_dir = "repos/toy"
    stage._base_sha = stage.rt.knowledge.fetch()
    return stage


def _record(stage, files, report, path, *, status="dry_run"):
    catalog = next(text for name, text in files.items() if name.endswith("knowledge-coverage.yaml"))
    record = InitRecord(stage="feature-discovery", repo="toy", pin=report["pin"], status=status,
                        discovery={"done": True, "pin": report["pin"], "report_path": path,
                                   "catalog_sha256": hashlib.sha256(catalog.encode()).hexdigest(),
                                   "report_sha256": hashlib.sha256(files[path].encode()).hexdigest()})
    if status == "dry_run":
        dest = stage.rt.state_dir / "preview"
        InitPublisher.dry_run(dest, files, title="catalog", body="reviewed")
        from infermatrix_copilot.kb_service.preview import file_hashes
        record.files = sorted(files)
        record.pr = {"dry_run_dir": str(dest), "checked_files_sha256": file_hashes(files)}
    record.save(stage.rt.state_dir)
    return record


def test_flag_defaults_preserve_legacy_and_reject_non_boolean():
    assert not parse_init({}, "init", repo="toy", manifest={}).feature_discovery_required
    assert parse_init({"feature_discovery_required": True}, "init", repo="toy", manifest={}).feature_discovery_required
    with pytest.raises(LifecycleConfigError, match="true or false"):
        parse_init({"feature_discovery_required": "true"}, "init", repo="toy", manifest={})


def test_new_repository_template_enables_discovery_with_valid_configurable_scope():
    template = Path(__file__).resolve().parents[1] / "doc/architecture/templates/kb-init-new-repository.yaml"
    manifest = yaml.safe_load(template.read_text())
    configured = parse_init(manifest["knowledge_lifecycle"]["init"], "init", repo="toy", manifest=manifest)
    assert configured.feature_discovery_required
    assert configured.source_roots == (".",)
    assert "docs/**" in configured.doc_globs
    assert set(manifest["knowledge_lifecycle"]) == {"init"}


def test_old_record_loads_without_discovery_field_and_identity_is_compatible(world):
    stage = _stage(world)
    record = InitRecord(stage="skeleton", repo="toy")
    path = record.save(stage.rt.state_dir)
    data = json.loads(path.read_text())
    del data["discovery"]
    path.write_text(json.dumps(data))
    assert InitRecord.load(stage.rt.state_dir, "toy", "skeleton").discovery == {}
    assert "feature_discovery_required" not in stage._init_identity()


def test_legacy_chain_needs_no_discovery_but_required_chain_does(world):
    stage = _stage(world)
    chain = _Chain()
    stage._discovery_gate(chain, world["pin"])
    assert not chain.problems and stage._discovery_catalog() == {}
    stage.lifecycle = replace(stage.lifecycle, init=replace(stage.lifecycle.init, feature_discovery_required=True))
    stage._discovery_gate(chain, world["pin"])
    assert "merged catalog" in chain.problems[-1]


def test_started_incomplete_discovery_blocks_even_when_optional(world):
    stage = _stage(world)
    InitRecord(stage="feature-discovery", repo="toy", pin=world["pin"], status="partial").save(stage.rt.state_dir)
    chain = _Chain()
    stage._discovery_gate(chain, world["pin"])
    assert any("incomplete" in problem for problem in chain.problems)


def test_merged_catalog_reusable_without_local_record_and_uses_actual_adapter(world):
    files, report, _ = _artifacts(world["pin"], adapter="toy_package")
    _commit(world["origin"], files, "merge discovered catalog")
    stage = _stage(world, required=True, adapter="/installed/toy_package")
    chain = _Chain()
    stage._discovery_gate(chain, world["pin"])
    assert not chain.problems
    assert stage._discovery_catalog() == report
    assert report["catalog_sha256"] in chain.key and chain.pin == world["pin"]


@pytest.mark.parametrize("changes, expected", [
    ({"catalog_sha256": "0" * 64}, "catalog hash"),
    ({"feature_ids": []}, "feature IDs"),
    ({"pin": "a" * 40}, "identity"),
    ({"done": False}, "completion"),
    ({"complete": False}, "completion"),
    ({"features": [{"id": "step", "owner": "other", "title": "Tick step"}]}, "summaries"),
    ({"features": [{"id": "step", "owner": "core", "title": "Different capability"}]}, "summaries"),
    ({"owner_requests": [{"owner": "core", "title": "Core", "feature_ids": ["step"],
                           "source_paths": ["pkg/other.py"], "page": "repos/toy/components/core/_index.md"}]}, "owner request"),
    ({"owner_requests": [{"owner": "core", "title": "Core", "feature_ids": ["step"],
                           "source_paths": ["pkg/core.py"], "page": "repos/other/components/core/_index.md"}]}, "owner request"),
])
def test_frozen_report_rejects_stale_identity_denominator_or_hash(world, changes, expected):
    files, _, _ = _artifacts(world["pin"], **changes)
    _commit(world["origin"], files, "bad catalog report")
    stage = _stage(world, required=True)
    chain = _Chain()
    stage._discovery_gate(chain, world["pin"])
    assert any(expected in problem for problem in chain.problems)
    assert stage._discovery_catalog() == {}


def test_preview_overlay_is_bound_and_cannot_be_published_downstream(world):
    stage = _stage(world)
    files, report, path = _artifacts(world["pin"])
    _record(stage, files, report, path)
    chain = _Chain()
    stage._discovery_gate(chain, world["pin"])
    assert not chain.problems and chain.repo_files == files
    stage.dry_run = False
    other = _Chain()
    stage._discovery_gate(other, world["pin"])
    assert other.problems and stage._discovery_catalog() == {}


def test_malformed_frozen_catalog_yaml_becomes_a_gate_problem(world):
    files, _, _ = _artifacts(world["pin"])
    files["adapters/toy/knowledge-coverage.yaml"] = "features: [\n"
    _commit(world["origin"], files, "corrupt frozen catalog YAML")
    stage = _stage(world, required=True)
    chain = _Chain()
    stage._discovery_gate(chain, world["pin"])
    assert any("frozen catalog" in problem for problem in chain.problems)
    assert stage._discovery_catalog() == {}


def test_frozen_catalog_requires_new_owner_request_until_route_exists(world):
    files, _, _ = _artifacts(world["pin"], owner_requests=[])
    _commit(world["origin"], files, "catalog omitted new owner request")
    stage = _stage(world, required=True)
    chain = _Chain()
    stage._discovery_gate(chain, world["pin"])
    assert any("new owners without creation requests" in problem for problem in chain.problems)
    assert stage._discovery_catalog() == {}
    _commit(world["origin"], {"knowledge/repos/toy/_routes.yaml": yaml.safe_dump({
        "schema_version": 1, "owners": [{"owner": "core", "path": "repos/toy/components/core/_index.md",
                                         "scope_prefixes": ["pkg/"]}],
    })}, "owner navigation already merged")
    stage._base_sha = stage.rt.knowledge.fetch()
    chain = _Chain()
    stage._discovery_gate(chain, world["pin"])
    assert not chain.problems


def test_preview_scope_and_checkpoint_hashes_are_checked(world):
    stage = _stage(world)
    files, report, path = _artifacts(world["pin"])
    record = _record(stage, {**files, "adapters/toy/manifest.yaml": "enabled: true"}, report, path)
    chain = _Chain()
    stage._discovery_gate(chain, world["pin"])
    assert any("scope" in problem for problem in chain.problems)
    record = _record(stage, files, report, path)
    record.discovery["report_sha256"] = "0" * 64
    record.save(stage.rt.state_dir)
    chain = _Chain()
    stage._discovery_gate(chain, world["pin"])
    assert any("checkpoint" in problem for problem in chain.problems)


def test_existing_knowledge_override_cannot_bypass_discovery_gate(world):
    class ExistingStage(_Stage):
        STAGE = "knowledge"

        def _chain(self):
            return _Chain(key="from-existing")

        def _build(self, tree):
            pytest.fail("discovery prerequisite was bypassed")

    stage = ExistingStage(_runtime(world), _lifecycle(feature_discovery_required=True), dry_run=True, pin=None,
                          from_existing=True)
    from infermatrix_copilot.kb_service.init_execution import execute_init
    asyncio.run(execute_init(stage))
    record = stage.record
    assert record.status == "blocked" and any("feature-discovery" in problem for problem in record.problems)


def test_existing_discovery_requires_merged_skeleton_and_does_not_ignore_blocked_record(world):
    stage = _stage(world)
    stage.STAGE = "feature-discovery"
    assert stage._existing_skeleton_chain().problems
    _commit(world["origin"], {
        "knowledge/repos/toy/_index.md": _index("Toy", "toy", "- [Core](components/core/_index.md)\n"),
        "knowledge/repos/toy/components/core/_index.md": _index("Core", "toy", "Core runtime.\n"),
        "knowledge/repos/toy/_routes.yaml": yaml.safe_dump({"schema_version": 1, "owners": [
            {"owner": "core", "path": "repos/toy/components/core/_index.md", "scope_prefixes": ["pkg/"]}]}),
    }, "merged skeleton")
    stage._base_sha = stage.rt.knowledge.fetch()
    assert not stage._existing_skeleton_chain().problems
    InitRecord(stage="skeleton", repo="toy", status="blocked").save(stage.rt.state_dir)
    assert any("blocked" in problem for problem in stage._existing_skeleton_chain().problems)


def test_unmerged_discovery_pr_blocks_even_with_valid_report(world):
    files, report, path = _artifacts(world["pin"])
    _commit(world["origin"], files, "catalog present")
    stage = _stage(world)
    record = _record(stage, files, report, path, status="published")
    record.pr = {"number": 42}
    record.save(stage.rt.state_dir)
    stage.rt.gh_run = lambda *a, **kw: subprocess.CompletedProcess(a, 0, b'{"state":"OPEN"}', b"")
    chain = _Chain()
    stage._discovery_gate(chain, world["pin"])
    assert any("merge the feature-discovery" in problem for problem in chain.problems)
    stage.rt.gh_run = lambda *a, **kw: subprocess.CompletedProcess(a, 0, b'{"state":"MERGED"}', b"")
    chain = _Chain()
    stage._discovery_gate(chain, world["pin"])
    assert not chain.problems
    assert stage._discovery_binding()["catalog_sha256"] == report["catalog_sha256"]


def test_new_pin_requires_new_discovery_batch(world):
    stage = _stage(world)
    files, report, path = _artifacts(world["pin"])
    _record(stage, files, report, path)
    chain = _Chain()
    stage._discovery_gate(chain, "b" * 40)
    assert any("pin differs" in problem for problem in chain.problems)


def test_discovery_publisher_rejects_other_adapter_and_lifecycle_mutation_before_git(world):
    stage = _stage(world)
    files, _, path = _artifacts(world["pin"])
    publisher = InitPublisher(world["clone"], "o/kb", allowed_paths=tuple(files))
    for forbidden in ("adapters/other/knowledge-coverage.yaml", "adapters/toy/manifest.yaml",
                      "knowledge/repos/toy/architecture.md", "eval/feature-discovery/toy-other.json"):
        with pytest.raises(InitError, match="refusing to write"):
            publisher.build_commit(world["kb_sha"], {forbidden: "mutation"}, title="catalog",
                                   author=("t", "t@example.com"), when=0)
    # Catalog/report permission does not expand ordinary init publication scope.
    ordinary = InitPublisher(world["clone"], "o/kb")
    with pytest.raises(InitError, match="refusing to write"):
        ordinary.build_commit(world["kb_sha"], files, title="catalog", author=("t", "t@example.com"), when=0)
    assert publisher.build_commit(world["kb_sha"], files, title="catalog", author=("t", "t@example.com"), when=1)


def test_discovery_model_defaults_are_independent_and_do_not_change_other_runtime_roles(world, monkeypatch):
    from infermatrix_copilot.kb_service import init_stages

    captured = {}

    class Stage:
        def __init__(self, rt, lifecycle, **kwargs):
            captured.update(rt=rt, lifecycle=lifecycle, options=kwargs)

    monkeypatch.setattr(init_stages, "_stage_class", lambda name: Stage)
    rt = _runtime(world)
    original_roles = (rt.generator, rt.judge)
    init_stages._make_stage(rt, _lifecycle(), "feature-discovery", dry_run=True, from_existing=True,
                            retry_unfinished=True, budget_usd=4)
    assert captured["rt"].generator.label() == "zcode:GLM-5.3"
    assert captured["rt"].judge.label() == "codex:gpt-6.1-sol:medium"
    assert captured["rt"].discovery_concurrency == 13
    assert captured["options"]["retry_unfinished"]
    assert captured["lifecycle"].init.budget_usd == 4
    assert (rt.generator, rt.judge) == original_roles
    rt.environ = {"KB_DISCOVERY_JUDGE": "zcode:GLM-5.3", "KB_JUDGE_FAMILY_WAIVER": "true"}
    with pytest.raises(InitError, match="independent"):
        init_stages._make_stage(rt, _lifecycle(), "feature-discovery", dry_run=True)


def test_discovery_cli_forwards_incremental_options(monkeypatch, tmp_path):
    from infermatrix_copilot.kb_service.cli import main
    from infermatrix_copilot.kb_service import runner

    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs["params"])
        return SimpleNamespace(status="done"), tmp_path

    monkeypatch.setattr(runner, "run_playbook", run)
    assert main(["--state-dir", str(tmp_path), "init", "toy", "--stage", "feature-discovery",
                 "--from-existing", "--retry-unfinished", "--budget-usd", "3", "--dry-run"]) == 0
    assert calls[0]["from_existing"] == calls[0]["retry_unfinished"] == "true"
    assert calls[0]["budget_usd"] == "3.0"
    assert main(["--state-dir", str(tmp_path), "init", "toy", "--stage", "feature-discovery",
                 "--unlimited-subscription", "--dry-run"]) == 0
    assert calls[-1]["unlimited_subscription"] == "true"
