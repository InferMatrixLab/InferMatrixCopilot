"""Generic pinned discovery inputs, complete chunks and honest failures."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from infermatrix_copilot.kb_service.config import InitConfig
from infermatrix_copilot.kb_service.feature_discovery_index import (
    build_discovery_index, discovery_scope, evidence_excerpt, iter_chunks, language_for,
    load_discovery_index, validate_evidence,
)
from infermatrix_copilot.kb_service.init_modules import modules_from_index
from infermatrix_copilot.kb_service.knowledge_coverage import inventory, load_policy, policy_path

PIN = "a" * 40


def _tree(root, files):
    for path, raw in files.items():
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(raw if isinstance(raw, bytes) else raw.encode())


def _index(tree, **options):
    return build_discovery_index(tree, pin=PIN, scope=discovery_scope(InitConfig(source_roots=("src/",))), **options)


def test_pure_library_without_docs_and_mixed_languages(tmp_path):
    _tree(tmp_path, {"src/public.py": "def connect():\n    return True\n", "src/client.ts": "export function call() {}\n",
                     "src/core.cc": "int call() { return 1; }\n", "src/lib.fiction": "publish connector capability\n",
                     "sdks/lang/tests/nested/test_connector.py": "def test_connect():\n    assert connect()\n",
                     "docs/unused.md": "Excluded documentation\n", "other/tool.py": "def another(): pass\n"})
    index = _index(tmp_path)
    assert index.docs == []
    assert index.production == ["src/client.ts", "src/core.cc", "src/lib.fiction", "src/public.py"]
    assert index.tests == ["sdks/lang/tests/nested/test_connector.py"]
    assert index.entries["src/lib.fiction"]["parse_status"] == "unknown"
    assert index.entries["src/core.cc"]["language"] == "cpp"
    assert index.scope_suggestions == [{"path": "other/tool.py", "reason": "code outside declared production roots"}]
    modules = modules_from_index(index, InitConfig(source_roots=("src/",), min_module_loc=0))
    assert set(modules["src/"]["files"]) == set(index.production)


def test_index_scope_identity_is_independent_of_feature_list(tmp_path):
    _tree(tmp_path, {"src/api.py": "def use(): return 1\n", "README.md": "Explains use API.\n"})
    common = {"schema_version": 1, "core": {"roots": ["src/"], "exclude": []}, "features": [
        {"id": "use", "title": "Use", "owner": "core", "source_globs": ["src/api.py"], "docs": [],
         "page": "repos/demo/components/core/feature-use.md"}]}
    policy = load_policy(yaml.safe_dump(common), "repos/demo")
    different = replace(policy, features=(replace(policy.features[0], title="Another name"),))
    a = build_discovery_index(tmp_path, pin=PIN, scope=discovery_scope(InitConfig(), policy), doc_globs=["README*"])
    b = build_discovery_index(tmp_path, pin=PIN, scope=discovery_scope(InitConfig(), different), doc_globs=["README*"])
    assert a.identity == b.identity and a.sha256 == b.sha256
    assert a.docs == ["README.md"]


def test_complete_chunks_cover_late_lines_and_oversize_line(tmp_path):
    lines = [f"row{i}" for i in range(950)] + ["X" * 30_000] + ["late public assertion"]
    _tree(tmp_path, {"src/library.fiction": "\n".join(lines)})
    index = _index(tmp_path)
    chunks = list(iter_chunks(index, index.production))
    assert all(len(chunk["text"]) <= 12_000 for chunk in chunks)
    assert chunks[-1]["end"] == len(lines)
    assert "late public assertion" in chunks[-1]["text"]
    long_chunks = [chunk for chunk in chunks if chunk.get("partial_line")]
    assert "".join(chunk["text"] for chunk in long_chunks) == "X" * 30_000
    assert all(chunk["start"] == 951 and chunk["end"] == 951 for chunk in long_chunks)
    covered = {n for chunk in chunks for n in range(chunk["start"], chunk["end"] + 1)}
    assert covered == set(range(1, len(lines) + 1))
    assert validate_evidence(index, {"path": "src/library.fiction", "start": 952, "end": 952})
    assert evidence_excerpt(index, {"path": "src/library.fiction", "start": 952, "end": 952})["text"] == lines[-1]


def test_read_and_parser_failures_do_not_disappear_from_inventory(tmp_path):
    _tree(tmp_path, {"src/broken.py": "def unfinished(\n", "src/unreadable.py": b"\xff",
                     "tests/nested/test_late.py": "def test_late():\n    assert 1\n"})
    index = _index(tmp_path)
    assert index.production == ["src/broken.py", "src/unreadable.py"]
    assert index.entries["src/broken.py"]["parse_status"] == "failed"
    assert index.entries["src/unreadable.py"]["status"] == "error"
    assert len(index.failures) == 2
    assert not validate_evidence(index, {"path": "src/unreadable.py", "start": 1, "end": 1})
    assert any(chunk["status"] == "error" for chunk in index.chunks)
    assert index.tests == ["tests/nested/test_late.py"]


def test_cache_reuse_hash_validation_and_scope_invalidation(tmp_path):
    root, cache = tmp_path / "repo", tmp_path / "cache.json"
    root.mkdir()
    _tree(root, {"src/api.py": "def public(): pass\n"})
    first = _index(root, cache_path=cache)
    again = _index(root, cache_path=cache)
    assert first.sha256 == again.sha256
    corrupted = json.loads(cache.read_text())
    corrupted["data"]["files"]["src/api.py"]["text"] = "corrupted"
    cache.write_text(json.dumps(corrupted))
    with pytest.raises(ValueError, match="content hash"):
        load_discovery_index(cache)
    assert _index(root, cache_path=cache).sha256 == first.sha256
    other = build_discovery_index(root, pin=PIN, scope=discovery_scope(InitConfig(source_roots=("other/",))), cache_path=cache)
    assert other.production == [] and other.identity != first.identity


@pytest.mark.parametrize('damage', ['corrupt', 'symlink'])
def test_retained_index_lookup_shares_integrity_with_explicit_cache_tolerance(tmp_path, damage):
    from infermatrix_copilot.kb_service.feature_discovery_index import load_index_snapshot
    source, cache = tmp_path / 'repo', tmp_path / 'cache'
    source.mkdir()
    cache.mkdir()
    _tree(source, {'src/api.py': 'def public(): pass\n'})
    index = _index(source, cache_path=cache / 'valid.json')
    bad = cache / '00-invalid.json'
    if damage == 'corrupt':
        bad.write_text('{}')
    else:
        bad.symlink_to(cache / 'valid.json')
    with pytest.raises(ValueError):
        load_index_snapshot(cache, sha256=index.sha256, pin=PIN)
    # The publication cache gate historically skips unrelated broken entries;
    # recovery and native consolidation retain their stricter default policy.
    assert load_index_snapshot(cache, sha256=index.sha256, pin=PIN, skip_invalid=True) == index
    with pytest.raises(ValueError, match='pin differs'):
        load_index_snapshot(cache, sha256=index.sha256, pin='b' * 40, skip_invalid=True)
    (cache / 'valid.json').unlink()
    with pytest.raises(ValueError, match='unavailable'):
        load_index_snapshot(cache, sha256=index.sha256, pin=PIN, skip_invalid=True)


def test_explicit_scope_excludes_vendor_but_retains_nested_tests(tmp_path):
    _tree(tmp_path, {"src/api.py": "def public(): pass\n", "src/vendor/imported.py": "def thirdparty(): pass\n",
                     "src/tests/deep/test_public.py": "assert public()\n", "src/api.odd": "public entry\n"})
    scope = {"roots": ["src/"], "exclude": ["*/vendor/*", "*/tests/*"], "suffixes": [".py"], "filenames": []}
    index = build_discovery_index(tmp_path, pin=PIN, scope=scope)
    assert index.production == ["src/api.py"]
    assert index.tests == ["src/tests/deep/test_public.py"]
    assert index.scope_suggestions == [{"path": "src/api.odd", "reason": "unsupported suffix outside declared suffix scope"}]


def test_safe_unknown_suffix_docs_empty_and_root_scope_policy(tmp_path):
    policy = load_policy(yaml.safe_dump({"schema_version": 1, "core": {"roots": ["."], "exclude": [], "suffixes": [".novel"]},
        "features": [{"id": "library", "title": "Library", "owner": "core", "source_globs": ["api.novel"], "docs": [],
                      "page": "repos/demo/components/core/feature-library.md"}]}), "repos/demo")
    _tree(tmp_path, {"api.novel": "public api\n"})
    assert inventory(tmp_path, policy) == ["api.novel"]
    index = build_discovery_index(tmp_path, pin=PIN, scope=discovery_scope(InitConfig(), policy))
    assert index.production == ["api.novel"]
    assert language_for("api.novel") == "unknown"
    assert policy_path("alias-name", Path("/adapters/actual_name")) == "adapters/actual_name/knowledge-coverage.yaml"


def test_mismatched_git_pin_and_dirty_source_block_inventory(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _tree(tmp_path, {"src/api.py": "def api(): pass\n"})
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.org",
                    "commit", "-qm", "source"], check=True)
    actual = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    scope = discovery_scope(InitConfig(source_roots=("src/",)))
    with pytest.raises(ValueError, match="fixed source SHA"):
        build_discovery_index(tmp_path, pin=PIN, scope=scope)
    build_discovery_index(tmp_path, pin=actual, scope=scope)
    (tmp_path / "src/api.py").write_text("def changed(): pass\n")
    with pytest.raises(ValueError, match="unchanged tracked files"):
        build_discovery_index(tmp_path, pin=actual, scope=scope)


def test_explicit_policy_inventory_keeps_the_existing_production_denominator(tmp_path):
    _tree(tmp_path, {"src/library.py": "def exported(): return 1\n", "src/__init__.py": "# package marker\n",
                     "src/docstring_only.py": '"""documentation only"""\n', "src/broken.py": "def incomplete(\n"})
    policy = load_policy(yaml.safe_dump({"schema_version": 1, "core": {"roots": ["src/"], "exclude": []},
        "features": [{"id": "library", "title": "Library", "owner": "core", "source_globs": ["src/*"], "docs": [],
                      "page": "repos/demo/components/core/feature-library.md"}]}), "repos/demo")
    index = build_discovery_index(tmp_path, pin=PIN, scope=discovery_scope(InitConfig(), policy))
    assert index.production == inventory(tmp_path, policy) == ["src/broken.py", "src/library.py"]
    assert index.entries["src/__init__.py"]["classification"] == "empty_package_marker"


def test_discovery_owner_requests_create_stable_component_navigation():
    from infermatrix_copilot.kb_service.init_modules import _Modules
    from infermatrix_copilot.kb_service.init_stages import _page_frontmatter
    from infermatrix_copilot.kb_service.init_support import InitRecord

    stage = _Modules.__new__(_Modules)
    from types import SimpleNamespace
    stage.lifecycle = SimpleNamespace(repo="demo")
    stage.repo_dir = "repos/demo"
    stage.root = "repos/demo/components"
    stage.groups = {}
    stage.tags, stage.today = ["demo"], "2026-10-05"
    stage.record = InitRecord(stage="modules", repo="demo", pin=PIN)
    stage.head = {"repos/demo/_index.md": _page_frontmatter("Demo", kind="index", today=stage.today, tags=stage.tags)}
    stage.all_files = ["src/connector.py"]
    stage.routes = {"schema_version": 1, "owners": []}
    report = {"owner_requests": [{"owner": "connector", "title": "Connectors", "source_paths": ["src/connector.py"],
                                   "feature_ids": ["connect"]}],
              "features": [{"id": "connect", "title": "Connect", "owner": "connector"}]}
    stage._discovery_catalog = lambda: report
    stage._discovery_owners()
    assert stage.routes["owners"] == [{"owner": "connector", "path": "repos/demo/components/connector/_index.md",
        "signals": ["connector"], "scope_prefixes": ["src/connector.py"]}]
    assert "Connect (`connect`)" in stage.head["repos/demo/components/connector/_index.md"]
    assert "connector/_index.md" in stage.head["repos/demo/components/_index.md"]
    stage._discovery_owners()
    assert len(stage.routes["owners"]) == 1


def test_operating_system_read_failure_stays_unknown_in_export_inventory(tmp_path, monkeypatch):
    _tree(tmp_path, {"src/unreadable.py": "def api(): pass\n"})
    real = Path.read_bytes
    def unreadable(path):
        if path.name == "unreadable.py":
            raise OSError("input read failed")
        return real(path)
    monkeypatch.setattr(Path, "read_bytes", unreadable)
    index = _index(tmp_path)
    assert index.production == ["src/unreadable.py"]
    assert index.entries["src/unreadable.py"]["status"] == "error"
    assert index.failures == [{"path": "src/unreadable.py", "kind": "source", "operation": "read", "error": "input read failed"}]
    assert not validate_evidence(index, {"path": "src/unreadable.py", "start": 1, "end": 1})


def test_explicit_declared_language_scope_records_extra_known_extensions(tmp_path):
    _tree(tmp_path, {"src/api.py": "def api(): pass\n", "src/extension.rb": "def api; end\n",
                     "src/deployment.yaml": "kind: Deployment\n"})
    scope = {"roots": ["src/"], "exclude": [], "suffixes": [".py", ".yaml"], "filenames": []}
    index = build_discovery_index(tmp_path, pin=PIN, scope=scope)
    assert index.production == ["src/api.py", "src/deployment.yaml"]
    assert index.scope_suggestions == [{"path": "src/extension.rb", "reason": "unsupported suffix outside declared suffix scope"}]


def test_extensionless_cli_scripts_use_shebang_without_expanding_explicit_policy(tmp_path):
    _tree(tmp_path, {"src/bin/tool": "#!/usr/bin/env python3\ndef run(): return 1\n",
                     "src/bin/launcher": "#!/bin/sh\nexec tool --mode batch\n", "src/LICENSE": "All rights reserved\n"})
    index = _index(tmp_path)
    assert index.production == ["src/bin/launcher", "src/bin/tool"]
    assert index.entries["src/bin/tool"]["language"] == "python"
    assert index.entries["src/bin/tool"]["parse_status"] == "parsed"
    assert index.entries["src/bin/launcher"]["language"] == "shell"
    restricted = build_discovery_index(tmp_path, pin=PIN, scope={"roots": ["src/"], "exclude": [], "suffixes": [".py"], "filenames": []})
    assert restricted.production == []


def test_test_index_never_overrides_excluded_third_party_or_testdata_scope(tmp_path):
    _tree(tmp_path, {"src/api.py": "def api(): pass\n", "src/tests/test_api.py": "assert api()\n",
                     "third_party/testdata/tests/test_external.py": "assert external()\n",
                     "vendor/tests/test_external.py": "assert external()\n"})
    scope = {"roots": ["src/"], "exclude": ["*/tests/*", "third_party/testdata/*", "vendor/tests/*"],
             "suffixes": [".py"], "filenames": []}
    index = build_discovery_index(tmp_path, pin=PIN, scope=scope)
    assert index.tests == ["src/tests/test_api.py"]
    assert not any(path.startswith(("vendor/", "third_party/")) for path in index.entries)


def test_unreadable_directory_is_a_global_inventory_error(tmp_path, monkeypatch):
    import os
    def incomplete_walk(*args, onerror, **kwargs):
        onerror(OSError("directory could not be listed"))
        return iter(())
    monkeypatch.setattr(os, "walk", incomplete_walk)
    with pytest.raises(OSError, match="directory could not be listed"):
        _index(tmp_path)


def test_broken_git_checkout_reports_a_controlled_inventory_error(tmp_path):
    (tmp_path / ".git").write_text("gitdir: does-not-exist\n")
    with pytest.raises(ValueError, match="pinned Git inventory could not be read"):
        _index(tmp_path)
