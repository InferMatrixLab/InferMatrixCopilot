"""Coverage must survive real inventories, reruns, forged cards and partial stages."""

import pytest
import yaml

from infermatrix_copilot.kb_service.init_coverage import Owner
from infermatrix_copilot.kb_service.init_stages import _page_frontmatter, run_stage
from infermatrix_copilot.kb_service.init_support import InitRecord
from infermatrix_copilot.kb_service.knowledge_coverage import (
    FACETS, add_contract_pages, audit_coverage, contract_block, inventory, load_policy, policy_path,
    source_contract,
)
from test_kb_init_knowledge import KnowledgeGateway, _chain
from test_kb_init_modules import _modules_lifecycle
from test_kb_init_skeleton import _commit, _runtime, _tree, world  # noqa: F401

PIN = "a" * 40
PAGE = "repos/demo/components/core/feature-demo.md"


def test_owner_extraction_uses_all_production_roots_from_policy(world):
    import json
    from test_kb_init_knowledge import CURRENT_KNOWLEDGE_SYSTEM as SYSTEM_KNOWLEDGE

    gateway = KnowledgeGateway()
    _chain(world, gateway)
    rt = _runtime(world, gateway)
    skeleton = InitRecord.load(rt.state_dir, "toy", "skeleton")
    modules = InitRecord.load(rt.state_dir, "toy", "modules")
    baseline = {**_tree(skeleton), **_tree(modules)}
    routes = yaml.safe_load(baseline["knowledge/repos/toy/_routes.yaml"])
    tooling = next(owner for owner in routes["owners"] if owner["owner"] == "tooling")
    tooling["scope_prefixes"].append("sdks/")
    baseline["knowledge/repos/toy/_routes.yaml"] = yaml.safe_dump(routes)
    data = _policy_data()
    data["core"]["roots"] = ["pkg/", "tools/", "sdks/"]
    data["features"][0].update(owner="tooling", source_globs=["sdks/client.ts"], docs=["docs/guide.md"],
                               page="repos/toy/components/tooling/feature-demo.md")
    baseline[policy_path("toy")] = yaml.safe_dump(data)
    _commit(world["origin"], baseline, "merge initialized KB and production policy")
    _commit(world["upstream"], {"sdks/client.ts": "export function connect() { return true; }\n"}, "add SDK")
    gateway.calls.clear()
    record = run_stage(_runtime(world, gateway, state_dir=world["tmp"] / "all-production"),
                       _modules_lifecycle(), "knowledge", dry_run=True, from_existing=True)
    assert record.status == "dry_run", record.problems
    payloads = [json.loads(c["prompt"].split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
                for c in gateway.calls if c["system"] == SYSTEM_KNOWLEDGE]
    owner = next(payload for payload in payloads if payload["owner"] == "tooling")
    assert "sdks/client.ts" in [file["path"] for file in owner["files"]]


def test_explicit_feature_ownership_fills_legacy_routes_without_guessing():
    from types import SimpleNamespace
    from infermatrix_copilot.kb_service.init_coverage import Owner
    from infermatrix_copilot.kb_service.init_knowledge_inputs import source_owner, source_owners

    route = Owner("core", "repos/demo/architecture.md", ("src/",))
    feature = SimpleNamespace(owner="client", page="repos/demo/components/client/feature-client.md",
                              source_globs=("clients/*",))
    conflict = SimpleNamespace(owner="other", page="repos/demo/components/other/feature-other.md",
                               source_globs=("clients/api.tsx", "clients/shared.ts", "src/already-routed.py"))
    paths = ["clients/api.ts", "clients/shared.ts", "src/already-routed.py", "unowned/file.ts", "clients/api.tsx"]
    owners = source_owners(paths, [route], SimpleNamespace(features=[feature, conflict]))
    assert source_owner(paths[0], [route], owners)[0].owner == "client"
    assert not source_owner(paths[1], [route], owners)
    assert source_owner(paths[2], [route], owners) == [route]
    assert not source_owner(paths[3], [route], owners)
    assert not source_owner(paths[4], [route], owners)
    assert owners["client"].path == feature.page


def _policy_data():
    return {"schema_version": 1, "required": True,
            "core": {"roots": ["src/", "ui/"], "exclude": ["*/tests/*", "*.test.*"], "target": 0.85},
            "features": [{"id": "demo", "title": "Demo", "owner": "core", "source_globs": ["src/a.py"],
                          "docs": ["docs/usage.md"], "page": PAGE}]}


@pytest.fixture
def coverage_world(tmp_path):
    sources = {"src/a.py": '"""Session construction and completion."""\ndef run(session: str) -> bool:\n    return bool(session)\n',
               "ui/app.ts": "import { connect } from './client';\nexport function render() { return connect(); }\n",
               "src/__init__.py": "# empty package marker\n",
               "src/tests/test_a.py": "def test_run():\n    assert True\n",
               "ui/app.test.ts": "const test = 1;\n", "docs/usage.md": "# Demo\n\nCreate a session then complete it.\n"}
    for path, text in sources.items():
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
    policy = load_policy(yaml.safe_dump(_policy_data()), "repos/demo")
    return tmp_path, policy


def _audit(head, coverage_world, pin=PIN):
    tree, policy = coverage_world
    return audit_coverage(head, tree, policy, full_name="o/demo", pin=pin)


def _feature_page():
    text = _page_frontmatter("Demo", kind="guide", today="2026-10-01", tags=["demo"])
    for facet in FACETS:
        text += f"\n<!-- kb:knowledge owner=feature-demo facet={facet} pin={PIN} -->\n\n"
        text += "The session entry accepts a session identity and returns its completion condition. " \
                "The caller owns session selection while the entry owns the completion check.\n\n"
        text += f"[implementation](https://github.com/o/demo/blob/{PIN}/src/a.py#L1-L3), " \
                f"[usage](https://github.com/o/demo/blob/{PIN}/docs/usage.md#L1-L3)\n"
    return text


def test_inventory_includes_clients_and_keeps_unparseable_code_in_denominator(coverage_world):
    tree, policy = coverage_world
    (tree / "src/broken.py").write_text("def unfinished(\n")
    assert inventory(tree, policy) == ["src/a.py", "src/broken.py", "ui/app.ts"]


def test_extensionless_build_code_stays_in_the_denominator(coverage_world):
    tree, policy = coverage_world
    (tree / "src/Dockerfile").write_text("FROM python:3\nCOPY . /app\n")
    (tree / "src/Makefile").write_text("build:\n\tpython -m build\n")
    (tree / "ui/gradlew").write_text("#!/bin/sh\nexec java Main\n")
    (tree / "ui/LICENSE").write_text("License text\n")
    assert inventory(tree, policy) == ["src/Dockerfile", "src/Makefile", "src/a.py", "ui/app.ts", "ui/gradlew"]


def test_dockerfile_variants_and_first_party_type_declarations_count(coverage_world):
    tree, policy = coverage_world
    (tree / "src/Dockerfile.runtime.base").write_text("FROM python:3\nCOPY . /app\n")
    (tree / "ui/desktop.d.ts").write_text("interface Window { desktopReady: boolean; }\n")
    assert inventory(tree, policy) == ["src/Dockerfile.runtime.base", "src/a.py", "ui/app.ts", "ui/desktop.d.ts"]


def test_comments_and_string_examples_are_not_counted_as_client_interfaces():
    assert source_contract("ui/comments.ts", '// export function imaginary() {}\n/* class Fake {} */\n') is None
    contract = source_contract("ui/client.ts", 'export const text = "class Fake {}";\nexport function connect() {}\n')
    assert contract and "`connect`" in contract[0] and "Fake" not in contract[0]


def test_routes_rules_indexes_and_bare_links_do_not_count_as_file_knowledge(coverage_world):
    link = f"[a.py](https://github.com/o/demo/blob/{PIN}/src/a.py#L1-L3)"
    head = {"repos/demo/_index.md": _page_frontmatter("index", kind="index", today="2026-10-01", tags=["demo"]) + link,
            "repos/demo/rules.md": _page_frontmatter("rules", kind="rule", today="2026-10-01", tags=["demo"]) + link,
            "repos/demo/components/core/links.md": _page_frontmatter("links", kind="architecture", today="2026-10-01", tags=["demo"])
            + ("\n- " + link) * 10}
    assert _audit(head, coverage_world)["core"]["covered"] == 0
    head["repos/demo/components/core/links.md"] += (
        "\n\nThis directory contains source files for the application and its clients. "
        "Use this index to locate files and choose an implementation to inspect.\n\n"
        "| File | Source |\n|---|---|\n| a.py | " + link + " |\n")
    assert _audit(head, coverage_world)["core"]["covered"] == 0


def test_contract_cards_require_matching_source_hash_and_verified_body(coverage_world):
    tree, _ = coverage_world
    raw = (tree / "src/a.py").read_text()
    block = contract_block("o/demo", PIN, "src/a.py", raw)
    front = _page_frontmatter("core", kind="architecture", today="2026-10-01", tags=["demo"])
    head = {"repos/demo/components/core/contracts.md": front + block}
    assert _audit(head, coverage_world)["core"]["covered"] == 1
    head[next(iter(head))] = front + block.replace("completion", "unconditional success")
    assert _audit(head, coverage_world)["core"]["covered"] == 0
    head[next(iter(head))] = front + block
    (tree / "src/a.py").write_text(raw.replace("bool(session)", "False"))
    result = _audit(head, coverage_world)
    assert result["core"]["covered"] == 0 and result["core"]["stale_contracts"] == ["src/a.py"]


def test_every_feature_needs_each_supported_facet_and_real_source_evidence(coverage_world):
    page = _feature_page()
    assert _audit({PAGE: page}, coverage_world)["features"]["covered"] == 1
    for changed in (page.replace("facet=configuration", "facet=unknown"),
                    page.replace(PIN, "b" * 40),
                    page.replace("#L1-L3", "#L1-L999"),
                    page.replace("pin=" + PIN + " -->", "pin=" + PIN + " verdict=unsure -->")):
        assert _audit({PAGE: changed}, coverage_world)["features"]["covered"] == 0
    markers_only = _page_frontmatter("Demo", kind="guide", today="2026-10-01", tags=["demo"])
    markers_only += "\n".join(f"<!-- kb:knowledge owner=feature-demo facet={f} pin={PIN} -->" for f in FACETS)
    assert _audit({PAGE: markers_only}, coverage_world)["features"]["covered"] == 0


def test_feature_evidence_must_include_its_explicit_production_entry_point(coverage_world):
    tree, _ = coverage_world
    data = _policy_data()
    data["features"][0]["source_globs"] = ["src/*", "ui/*"]
    data["features"][0]["entry_points"] = ["ui/app.ts"]
    policy = load_policy(yaml.safe_dump(data), "repos/demo")
    assert audit_coverage({PAGE: _feature_page()}, tree, policy, full_name="o/demo", pin=PIN)["features"]["covered"] == 0


def test_missing_declared_catalog_cannot_pass_coverage(coverage_world):
    tree, _ = coverage_world
    data = _policy_data()
    data["catalog_sources"] = ["docs/missing-catalog.md"]
    policy = load_policy(yaml.safe_dump(data), "repos/demo")
    head = {PAGE: _feature_page(), "repos/demo/components/core/contracts.md":
            _page_frontmatter("contracts", kind="architecture", today="2026-10-01", tags=["demo"])
            + contract_block("o/demo", PIN, "ui/app.ts", (tree / "ui/app.ts").read_text())}
    report = audit_coverage(head, tree, policy, full_name="o/demo", pin=PIN)
    assert report["core"]["ratio"] == 1.0 and report["features"]["covered"] == 1
    assert not report["met"] and report["catalog_sources"]["missing"] == ["docs/missing-catalog.md"]


def test_contract_extraction_is_multilanguage_owner_scoped_and_idempotent(coverage_world):
    tree, policy = coverage_world
    head = {PAGE: _feature_page()}
    owners = [Owner("core", "repos/demo/components/core/_index.md", ("src/",)),
              Owner("client", "repos/demo/components/client/_index.md", ("ui/",))]
    linked = []
    kwargs = dict(repo_dir="repos/demo", full_name="o/demo", pin=PIN, today="2026-10-01",
                  tags=["demo"], link_page=lambda p, title: linked.append(p))
    assert add_contract_pages(head, tree, policy, owners, **kwargs) == []
    result = _audit(head, coverage_world)
    assert result["met"] and result["core"]["covered"] == 2
    assert any(p.startswith("repos/demo/components/client/") for p in linked)
    before = dict(head)
    assert add_contract_pages(head, tree, policy, owners, **kwargs) == []
    assert head == before


def test_client_layout_wrappers_do_not_create_nested_owner_axes(coverage_world):
    tree, policy = coverage_world
    folder = tree / "ui/frontend/src/components"
    folder.mkdir(parents=True)
    (folder / "entry.ts").write_text("export function view() {}\n")
    head = {PAGE: _feature_page()}
    add_contract_pages(head, tree, policy, [Owner("client", "repos/demo/components/client/_index.md", ("ui/",))],
                       repo_dir="repos/demo", full_name="o/demo", pin=PIN, today="2026-10-01",
                       tags=["demo"], link_page=lambda *_: None)
    assert "repos/demo/components/client/components-root/source-contracts-01.md" in head
    assert not any("/components/components/" in p or "/client/components/" in p for p in head)


def test_partial_knowledge_blocks_later_stages_even_with_a_finished_snapshot(world):
    gateway = KnowledgeGateway()
    _chain(world, gateway)
    rt = _runtime(world, gateway)
    record = InitRecord(stage="knowledge", repo="toy", pin=world["pin"], status="empty",
                        inputs_digest="partial-knowledge", coverage={"knowledge": {"targets": {"required": True, "met": False}}})
    record.save(rt.state_dir)
    result = run_stage(rt, _modules_lifecycle(), "deepen", dry_run=True)
    assert result.status == "blocked"
    assert any("knowledge targets incomplete" in p for p in result.problems)


def test_required_policy_cannot_be_skipped_or_satisfied_by_an_old_policy_result(world):
    import hashlib

    gateway = KnowledgeGateway()
    _chain(world, gateway)
    data = _policy_data()
    data["core"]["roots"] = ["pkg/", "tools/"]
    data["features"][0].update(owner="tooling", source_globs=["tools/lint/y.py"], docs=["docs/guide.md"],
                               page="repos/toy/components/tooling/feature-demo.md")
    policy = yaml.safe_dump(data)
    _commit(world["origin"], {policy_path("toy"): policy}, "require feature knowledge")
    rt = _runtime(world, gateway)
    gateway.calls.clear()
    result = run_stage(rt, _modules_lifecycle(), "deepen", dry_run=True)
    assert result.status == "blocked" and any("current policy" in p for p in result.problems)
    assert gateway.calls == []
    record = InitRecord(stage="knowledge", repo="toy", pin=world["pin"], status="empty",
                        inputs_digest="old-policy", coverage={"knowledge": {"targets": {
                            "required": True, "met": True, "policy_sha256": hashlib.sha256(b"older policy").hexdigest()}}})
    record.save(rt.state_dir)
    result = run_stage(rt, _modules_lifecycle(), "deepen", dry_run=True)
    assert result.status == "blocked" and any("current policy" in p for p in result.problems)


def test_invalid_policy_is_refused_before_any_model_call(world):
    gateway = KnowledgeGateway()
    _chain(world, gateway)
    _commit(world["origin"], {policy_path("toy"): "schema_version: 1\ncore: {}\nfeatures: []\n"}, "invalid policy")
    gateway.calls.clear()
    result = run_stage(_runtime(world, gateway), _modules_lifecycle(), "knowledge", dry_run=True)
    assert result.status == "blocked" and "knowledge coverage policy" in result.problems[0]
    assert gateway.calls == []


def test_feature_policy_cannot_append_knowledge_to_a_rule_page(world):
    gateway = KnowledgeGateway()
    _chain(world, gateway)
    data = _policy_data()
    rule = _page_frontmatter("rules", kind="rule", today="2026-10-01", tags=["toy"])
    data["features"][0].update(owner="tooling", page="repos/toy/components/tooling/rules.md")
    _commit(world["origin"], {policy_path("toy"): yaml.safe_dump(data),
                            "knowledge/repos/toy/components/tooling/rules.md": rule}, "feature target conflict")
    gateway.calls.clear()
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "knowledge", dry_run=True)
    assert record.status == "blocked" and "must be an explanatory architecture or guide page" in record.problems[0]
    assert gateway.calls == []


def test_policy_run_generates_contracts_and_reports_partial_feature_knowledge(world):
    gateway = KnowledgeGateway()
    _chain(world, gateway)
    rt = _runtime(world, gateway)
    data = _policy_data()
    data["core"]["roots"] = ["pkg/", "tools/"]
    data["features"][0].update(owner="tooling", source_globs=["tools/lint/y.py"], docs=["docs/guide.md"],
                               page="repos/toy/components/tooling/feature-demo.md")
    _commit(world["origin"], {policy_path("toy"): yaml.safe_dump(data)}, "feature coverage policy")
    record = run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=True)
    assert record.status == "dry_run", record.problems
    targets = record.coverage["knowledge"]["targets"]
    assert targets["core"]["ratio"] >= 0.85
    assert targets["features"]["covered"] == 0 and not targets["met"]
    assert any("feature without complete knowledge: demo" in u for u in record.unfinished)
    assert any("source-contracts" in p for p in record.files)
    later = run_stage(rt, _modules_lifecycle(), "deepen", dry_run=True)
    assert later.status == "blocked" and any("knowledge targets incomplete" in p for p in later.problems)


@pytest.mark.parametrize("mutation", [
    lambda p: p["core"].update(target=0),
    lambda p: p["core"].update(target=True),
    lambda p: p["core"].update(roots=["../private"]),
    lambda p: p["core"].update(suffixes=[".unsafe/extension"]),
    lambda p: p["core"].update(filenames=["../LICENSE"]),
    lambda p: p.update(catalog_sources=["../catalog.md"]),
    lambda p: p["features"].append(p["features"][0]),
    lambda p: p["features"][0].update(page="repos/other/components/core/feature.md"),
])
def test_bad_denominators_and_cross_repository_features_are_rejected(mutation):
    data = _policy_data()
    mutation(data)
    with pytest.raises(ValueError):
        load_policy(yaml.safe_dump(data), "repos/demo")


def test_source_only_feature_can_be_extracted_and_covered_without_documentation(world):
    import json
    from test_kb_init_knowledge import CURRENT_KNOWLEDGE_SYSTEM as SYSTEM_KNOWLEDGE

    gateway = KnowledgeGateway()
    _chain(world, gateway)
    data = _policy_data()
    data["core"]["roots"] = ["pkg/", "tools/"]
    data["features"][0].update(owner="tooling", source_globs=["tools/lint/y.py"], docs=[],
                               page="repos/toy/components/tooling/feature-demo.md")
    _commit(world["origin"], {policy_path("toy"): yaml.safe_dump(data)}, "source-only feature policy")
    gateway.calls.clear()
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "knowledge", dry_run=True)
    assert record.status == "dry_run", record.problems
    payloads = [json.loads(c["prompt"].split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
                for c in gateway.calls if c["system"] == SYSTEM_KNOWLEDGE]
    feature = next(payload for payload in payloads if payload["owner"] == "feature-demo")
    assert feature["docs"] == [] and feature["files"][0]["path"] == "tools/lint/y.py"
    target = record.coverage["knowledge"]["targets"]["features"]["items"]["demo"]
    assert not target["documentation_required"] and target["doc_requirement_met"]
    assert not target["doc_evidence"]  # optional documentation does not become claimed evidence


def test_source_only_feature_coverage_does_not_require_a_document_citation(coverage_world):
    from dataclasses import replace
    tree, original = coverage_world
    policy = replace(original, features=(replace(original.features[0], docs=()),))
    source_only = _feature_page().replace(f", [usage](https://github.com/o/demo/blob/{PIN}/docs/usage.md#L1-L3)", "")
    report = audit_coverage({PAGE: source_only}, tree, policy, full_name="o/demo", pin=PIN)
    target = report["features"]["items"]["demo"]
    assert target["covered"] and target["doc_requirement_met"] and not target["doc_evidence"]
