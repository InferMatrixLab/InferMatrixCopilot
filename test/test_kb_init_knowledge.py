"""Offline end-to-end checks for explanatory knowledge, routing and evidence."""

import json
from pathlib import Path

import pytest

from infermatrix_copilot.kb_service.init_knowledge import (
    FACETS, MAX_DOC_BYTES, SYSTEM_KNOWLEDGE, _Knowledge, knowledge_prompt, validate_sections,
)
from infermatrix_copilot.kb_service.init_knowledge_inputs import (
    SYSTEM_KNOWLEDGE_V4, SYSTEM_KNOWLEDGE_V5, knowledge_system,
    FOUNDATION_GENERATION_POLICY_VERSION,
)
CURRENT_KNOWLEDGE_SYSTEM = knowledge_system({"foundation_generation_policy_version": FOUNDATION_GENERATION_POLICY_VERSION})
CURRENT_KNOWLEDGE_SYSTEM_V4 = knowledge_system({"foundation_prompt_version": 4,
    "foundation_generation_policy_version": FOUNDATION_GENERATION_POLICY_VERSION})
CURRENT_KNOWLEDGE_SYSTEM_V5 = knowledge_system({"foundation_prompt_version": 5,
    "foundation_generation_policy_version": FOUNDATION_GENERATION_POLICY_VERSION})
FOUNDATION_SYSTEMS = {SYSTEM_KNOWLEDGE, SYSTEM_KNOWLEDGE_V4, SYSTEM_KNOWLEDGE_V5,
                      CURRENT_KNOWLEDGE_SYSTEM, CURRENT_KNOWLEDGE_SYSTEM_V4, CURRENT_KNOWLEDGE_SYSTEM_V5}
from infermatrix_copilot.kb_service.init_stages import run_stage
from infermatrix_copilot.kb_service.init_support import InitRecord
from infermatrix_copilot.knowledge_service.lifecycle import Page

from test_kb_init_modules import CardGateway, _modules_lifecycle, _world_with_tools
from test_kb_init_skeleton import _commit, _runtime, _tree, world  # noqa: F401


def test_explicit_existing_baseline_uses_merged_routes_without_stage_records(world):
    gateway = KnowledgeGateway()
    _chain(world, gateway)
    skeleton = InitRecord.load(_runtime(world).state_dir, "toy", "skeleton")
    modules = InitRecord.load(_runtime(world).state_dir, "toy", "modules")
    _commit(world["origin"], {**_tree(skeleton), **_tree(modules)}, "merge initialized owner map")
    rt = _runtime(world, gateway, state_dir=world["tmp"] / "rerun")
    blocked = run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=True)
    assert blocked.status == "blocked" and "run the skeleton stage first" in blocked.problems
    gateway.calls.clear()
    record = run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=True, from_existing=True)
    assert record.status == "dry_run", record.problems
    assert record.coverage["knowledge"]["total_owners"] == 2
    assert not InitRecord.path(rt.state_dir, "toy", "skeleton").exists()


def test_existing_baseline_does_not_override_blocked_stage_or_missing_map(world):
    rt = _runtime(world, KnowledgeGateway())
    record = run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=True, from_existing=True)
    assert record.status == "blocked" and any("merged repository index" in p for p in record.problems)
    _chain(world, rt.gateway)
    modules = InitRecord.load(rt.state_dir, "toy", "modules")
    modules.status = "blocked"
    modules.save(rt.state_dir)
    record = run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=True, from_existing=True)
    assert record.status == "blocked" and "the modules stage is blocked; finish it first" in record.problems


def test_subscription_generator_requires_declared_billing_and_preserves_unknown_cost(world):
    from infermatrix_copilot.kb_service.init_budget import Budget
    from infermatrix_copilot.kb_service.init_support import generate
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole, ModelUnavailable
    from infermatrix_copilot.llm import Block, Reply

    class Transport:
        subscription_billing = False
        seen = None

        def complete(self, **kwargs):
            self.seen = kwargs
            return Reply(blocks=[Block(type="text", text='{"sections": []}')],
                         stop_reason="end_turn", usage={"input_tokens": 12}, model="GLM-5.3")

    transport = Transport()
    rt = _runtime(world, ModelGateway(None, transport_factory=lambda provider: transport),
                  generator=ModelRole("generator", "zcode", "GLM-5.3"),
                  subscription_generator=True)
    budget = Budget(0.1)
    with pytest.raises(ModelUnavailable, match="authenticated subscription"):
        generate(rt, budget, _modules_lifecycle().init, system="extract", prompt="source")
    assert transport.seen is None
    transport.subscription_billing = True
    reply = generate(rt, budget, _modules_lifecycle().init, system="extract", prompt="source")
    assert "max_budget_usd" not in transport.seen
    assert reply.cost_usd is None and reply.usage["input_tokens"] == 12
    assert budget.spent_usd == 0  # stage accounting excludes subscription fees, not a measured price


def test_subscription_check_binds_the_transport_that_receives_the_uncapped_call():
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
    from infermatrix_copilot.llm import Block, Reply

    class SubscriptionTransport:
        subscription_billing = True
        called = False

        def complete(self, **kwargs):
            self.called = True
            assert "max_budget_usd" not in kwargs
            return Reply(blocks=[Block(type="text", text='{"ok":true}')], model="GLM-5.3")

    class ApiTransport:
        subscription_billing = False

        def complete(self, **kwargs):
            pytest.fail("the unreserved call reached a different billing provider")

    subscription = SubscriptionTransport()
    transports = iter([subscription, ApiTransport()])
    gateway = ModelGateway(None, transport_factory=lambda provider: next(transports))
    role = ModelRole("generator", "zcode", "GLM-5.3")
    assert gateway.subscription_billing(role)
    assert gateway.call_json(role, system="extract", prompt="source").data == {"ok": True}
    assert subscription.called
    assert not gateway.subscription_billing(role)


def test_owner_overview_precedes_partial_symbol_cards(tmp_path):
    from types import SimpleNamespace
    from infermatrix_copilot.kb_service.init_coverage import Owner

    docs = tmp_path / "owner-docs"
    docs.mkdir()
    for number in range(10):
        (docs / f"card-{number}.md").write_text("# Worker method\n\nSource pkg/worker.py.\n")
    (docs / "architecture.md").write_text("# Worker architecture\n\nStatus: partial. Source pkg/worker.py.\n")
    stage = _Knowledge.__new__(_Knowledge)
    stage.lifecycle = SimpleNamespace(init=SimpleNamespace(doc_globs=("owner-docs/**/*.md",)))
    offered = stage._docs_for(tmp_path, Owner("worker", "repos/demo/components/worker/_index.md", ("pkg/",)))
    assert offered[0]["path"] == "owner-docs/architecture.md"


class KnowledgeGateway(CardGateway):
    def __init__(self, *, bad_evidence=False, rejected=False, cost=0.01, **kwargs):
        super().__init__(**kwargs)
        self.bad_evidence = bad_evidence
        self.rejected = rejected
        self.cost = cost

    def call_json(self, role, *, system, prompt, validate=None, max_budget_usd=None):
        if system not in FOUNDATION_SYSTEMS:
            return super().call_json(role, system=system, prompt=prompt, validate=validate,
                                     max_budget_usd=max_budget_usd)
        from infermatrix_copilot.kb_service.models import ModelReply

        self.calls.append({"role": role.name, "system": system, "prompt": prompt})
        payload = json.loads(prompt.split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
        source = payload["files"][0]
        evidence = [{"path": "unoffered.py" if self.bad_evidence else source["path"],
                     "start": 1, "end": source["end"]}]
        data = {"title": f"{payload['owner']} knowledge", "sections": [
            {"facet": "architecture", "title": "Responsibilities",
             "body": "BADRULE" if self.rejected else f"The owner exposes code from `{source['path']}`.",
             "interpretation": "fact", "evidence": evidence},
            {"facet": "tradeoffs", "title": "Design tradeoffs",
             "body": "The small interface reduces integration work; independent lifecycle management adds work.",
             "interpretation": "inference", "evidence": evidence}]}
        data["sections"] = [s for s in data["sections"] if s["facet"] in payload["facets"]]
        if validate is not None:
            validate(data)
        return ModelReply(role, data, json.dumps(data), role.model, {}, 0.1, cost_usd=self.cost)


def _chain(world, gateway, **init):
    _world_with_tools(world)
    for stage in ("skeleton", "modules"):
        record = run_stage(_runtime(world, gateway), _modules_lifecycle(**init), stage, dry_run=True)
        assert record.status in ("dry_run", "empty"), record.problems


def test_knowledge_visits_every_owner_even_with_rules_and_low_rule_target(world):
    gateway = KnowledgeGateway()
    _chain(world, gateway, coverage_target=0.01)
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(coverage_target=0.01),
                       "knowledge", dry_run=True)
    assert record.status == "dry_run", record.problems
    calls = [c for c in gateway.calls if c["system"] == CURRENT_KNOWLEDGE_SYSTEM]
    assert len(calls) == 2                 # both core and tooling, regardless of existing rules
    tree = _tree(record)
    assert "adapters/toy/manifest.yaml" not in tree  # knowledge does not flip service flags
    for path in ("knowledge/repos/toy/components/core/knowledge-core.md",
                 "knowledge/repos/toy/components/tooling/knowledge.md"):
        page = Page.parse(tree[path])
        assert page.frontmatter_data()["type"] == "architecture"
        assert not page.rules()            # explanatory content is not reclassified as a rule
        assert "Inference / 设计推断" in page.render()
        assert record.pin in page.sources()[0]
        assert f"/blob/{record.pin}/" in page.render()
    assert "](core/_index.md)" in tree["knowledge/repos/toy/components/_index.md"]
    assert "](knowledge-core.md)" in tree["knowledge/repos/toy/components/core/_index.md"]
    knowledge = record.coverage["knowledge"]
    assert knowledge["total_owners"] == 2
    assert knowledge["covered_by_facet"]["architecture"] == 2
    assert knowledge["covered_by_facet"]["configuration"] == 0
    assert any("missing knowledge: core/api" == u for u in record.unfinished)
    assert all(v["kind"] == "prose" for v in record.verdicts.values())
    body = (Path(record.pr["dry_run_dir"]) / "PR_BODY.md").read_text()
    assert "| configuration | 0/2 |" in body and "knowledge facet" in body


def test_knowledge_drops_unshown_evidence_and_failed_claims(world):
    gateway = KnowledgeGateway(bad_evidence=True)
    _chain(world, gateway)
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "knowledge", dry_run=True)
    assert record.status == "empty", record.problems
    assert len(record.dropped) == 4
    assert all(d["why"] == "evidence outside shown input" for d in record.dropped)
    assert not record.verdicts             # rejected before judging or writing

    gateway.bad_evidence = False
    gateway.rejected = True
    InitRecord.path(_runtime(world).state_dir, "toy", "knowledge").unlink()
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "knowledge", dry_run=True)
    assert record.status == "dry_run", record.problems
    assert record.coverage["knowledge"]["covered_by_facet"]["architecture"] == 0
    assert record.coverage["knowledge"]["covered_by_facet"]["tradeoffs"] == 2
    assert all("BADRULE" not in text for text in _tree(record).values())


@pytest.mark.parametrize("failed_owner", ["core", "tooling"])
def test_unusable_component_draft_preserves_checked_work_and_visits_remaining_owners(world, failed_owner):
    from infermatrix_copilot.kb_service.models import ModelUnavailable

    class PartialGateway(KnowledgeGateway):
        def call_json(self, role, *, system, prompt, **kwargs):
            if system == CURRENT_KNOWLEDGE_SYSTEM:
                payload = json.loads(prompt.split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
                if payload["owner"] == failed_owner:
                    raise ModelUnavailable("reply is not a JSON object")
            return super().call_json(role, system=system, prompt=prompt, **kwargs)

    gateway = PartialGateway()
    _chain(world, gateway)
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "knowledge", dry_run=True)
    assert record.status == "dry_run", record.problems
    assert record.coverage["knowledge"]["covered_by_facet"]["architecture"] == 1
    assert record.coverage["knowledge"]["owners"][failed_owner]["facets"]["architecture"] == "missing"
    assert any(f"knowledge owner {failed_owner}: unusable draft" in u for u in record.unfinished)
    core = "knowledge/repos/toy/components/core/knowledge-core.md"
    tooling = "knowledge/repos/toy/components/tooling/knowledge.md"
    assert (core in _tree(record)) == (failed_owner != "core")
    assert (tooling in _tree(record)) == (failed_owner != "tooling")


def test_knowledge_budget_stop_reports_partial_work_and_remaining_owners(world):
    gateway = KnowledgeGateway(cost=1)
    _chain(world, gateway, budget_usd=6.0)
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(budget_usd=6.0), "knowledge", dry_run=True)
    assert record.status == "dry_run", record.problems
    assert any("budget exhausted" in n for n in record.notes)
    assert record.coverage["knowledge"]["total_owners"] == 2
    assert record.coverage["knowledge"]["covered_by_facet"]["architecture"] == 1
    assert any(u.startswith("knowledge owner ") for u in record.unfinished)
    assert record.spent_usd <= 6.0


def test_existing_knowledge_is_preserved_and_only_missing_facets_are_requested(world):
    gateway = KnowledgeGateway()
    _chain(world, gateway)
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "knowledge", dry_run=True)
    initial = _tree(record)
    _commit(world["origin"], initial, "accept explanatory knowledge")
    InitRecord.path(_runtime(world).state_dir, "toy", "knowledge").unlink()
    gateway.calls.clear()
    repeat = run_stage(_runtime(world, gateway), _modules_lifecycle(), "knowledge", dry_run=True)
    assert repeat.status == "empty", repeat.problems
    assert not repeat.files
    calls = [c for c in gateway.calls if c["system"] == CURRENT_KNOWLEDGE_SYSTEM]
    assert len(calls) == 2
    for call in calls:
        payload = json.loads(call["prompt"].split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
        assert "architecture" not in payload["facets"] and "tradeoffs" not in payload["facets"]
    assert repeat.coverage["knowledge"]["covered_by_facet"]["architecture"] == 2


def test_sources_do_not_offer_or_attest_a_partially_cut_line(tmp_path):
    (tmp_path / "a.py").write_text("abc\n" + "x" * 100 + "\n")
    offered = _Knowledge._sources(tmp_path, ["a.py"], 12)
    assert offered == [{"path": "a.py", "text": "1: abc", "end": 1, "total_lines": 2, "language": "python"}]


def test_removed_and_symlinked_entry_points_are_not_offered(tmp_path):
    (tmp_path / "real.py").write_text("def run(): pass\n")
    (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
    sources = _Knowledge._sources(tmp_path, ["missing.py", "link.py", "real.py"], 1000)
    assert [item["path"] for item in sources] == ["real.py"]


def test_owner_docs_are_ranked_before_shared_collection_caps(tmp_path):
    from types import SimpleNamespace
    from infermatrix_copilot.kb_service.init_coverage import Owner

    (tmp_path / "README.md").write_text("# Repository overview\n\nArchitecture and configuration entry points.\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    for number in range(45):
        (docs / f"guide-{number:02}.md").write_text("# Unrelated guide\n")
    (docs / "z-design.md").write_text("# Worker lifecycle\n\nCurrent implementation lives in pkg/worker.py.\n")
    stage = _Knowledge.__new__(_Knowledge)
    stage.lifecycle = SimpleNamespace(init=SimpleNamespace(doc_globs=("README*", "docs/**/*.md")))
    stage.docs = [("README.md", "# Repository overview\n")]  # shared input exhausted its cap
    owner = Owner("worker", "repos/demo/components/worker/_index.md", ("pkg/",))
    offered = stage._docs_for(tmp_path, owner)
    assert [item["path"] for item in offered] == ["docs/z-design.md", "README.md"]
    assert "pkg/worker.py" in offered[0]["text"]


def test_owner_docs_keep_readme_context_and_skip_symlinks(tmp_path):
    from types import SimpleNamespace
    from infermatrix_copilot.kb_service.init_coverage import Owner

    (tmp_path / "README_CN.md").write_text("# 使用说明\n\n本仓库的安装与模型配置入口。\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "external.md").write_text("# Worker\nSecret reference outside the configured docs.\n")
    (docs / "worker.md").symlink_to(tmp_path / "external.md")
    stage = _Knowledge.__new__(_Knowledge)
    stage.lifecycle = SimpleNamespace(init=SimpleNamespace(doc_globs=("README*", "docs/**/*.md")))
    owner = Owner("worker", "repos/demo/components/worker/_index.md", ("pkg/",))
    assert [item["path"] for item in stage._docs_for(tmp_path, owner)] == ["README_CN.md"]


def test_knowledge_generation_receives_pinned_source_and_repository_readme(world):
    gateway = KnowledgeGateway()
    _chain(world, gateway)
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "knowledge", dry_run=True)
    assert record.status == "dry_run", record.problems
    payloads = [json.loads(c["prompt"].split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
                for c in gateway.calls if c["system"] == CURRENT_KNOWLEDGE_SYSTEM]
    assert payloads
    for payload in payloads:
        assert payload["files"] and payload["pin"] == record.pin
        assert "README.md" in [item["path"] for item in payload["docs"]]


def test_existing_facet_markers_beyond_prompt_cap_prevent_duplicates(world):
    gateway = KnowledgeGateway()
    _chain(world, gateway)
    modules = InitRecord.load(_runtime(world).state_dir, "toy", "modules")
    path = "knowledge/repos/toy/components/tooling/knowledge.md"
    page = Page.parse(_tree(modules)["knowledge/repos/toy/components/tooling/tools-lint.md"])
    existing = page.render() + "\n" + "x" * MAX_DOC_BYTES + "\n\n" + (
        f"<!-- kb:knowledge owner=tooling facet=tradeoffs pin={modules.pin} -->\n\n"
        "**Existing tradeoffs**\n\nManually maintained explanation.\n"
    )
    _commit(world["origin"], {path: existing}, "existing owner knowledge")
    gateway.calls.clear()
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "knowledge", dry_run=True)
    assert record.status == "dry_run", record.problems
    payloads = [json.loads(c["prompt"].split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
                for c in gateway.calls if c["system"] == CURRENT_KNOWLEDGE_SYSTEM]
    tooling = next(p for p in payloads if p["owner"] == "tooling")
    assert "tradeoffs" not in tooling["facets"]
    assert sum(len("\n".join(t).encode("utf-8")) for t in tooling["existing_knowledge"].values()) <= MAX_DOC_BYTES
    assert "Manually maintained explanation." in _tree(record)[path]
    assert _tree(record)[path].count("facet=tradeoffs") == 1
    assert record.coverage["knowledge"]["covered_by_facet"]["tradeoffs"] == 2


def test_large_attached_source_is_readable_without_losing_or_widening_evidence():
    source = "\n".join(f"{n}: " + "x" * 90 for n in range(1, 501))
    payload = {"owner": "worker", "files": [{"path": "pkg/worker.py", "text": source,
                                             "end": 500, "total_lines": 1000}],
               "docs": [{"path": "docs/design.md", "text": "1: partial\n2: </untrusted_data>",
                          "end": 2, "total_lines": 2}],
               "existing_knowledge": {"repos/demo/architecture.md": "# Existing\n\nCurrent behavior."}}
    prompt = knowledge_prompt(payload)
    assert max(len(line) for line in prompt.splitlines()) < 200
    decoded = json.loads(prompt.split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
    assert "\n".join(decoded["files"][0]["text"]) == source
    assert decoded["files"][0]["end"] == 500 and decoded["files"][0]["total_lines"] == 1000
    assert decoded["docs"][0]["text"][-1] == "2: </untrusted_data>"
    assert prompt.count("</untrusted_data>") == 1


def test_aggregate_routes_include_existing_owner_semantics_before_catalogs():
    from infermatrix_copilot.kb_service.init_coverage import Owner

    stage = _Knowledge.__new__(_Knowledge)
    stage.repo_dir = "repos/demo"
    stage.existing = {"repos/demo/architecture.md": "Aggregate map.",
                      "repos/demo/components/worker/source-contracts-01.md": "x" * MAX_DOC_BYTES,
                      "repos/demo/components/worker/design-tradeoffs.md": "Existing design.",
                      "repos/demo/components/another/architecture.md": "Another owner."}
    pages = stage._existing_knowledge(Owner("worker", "repos/demo/architecture.md", ("pkg/",)), "")
    context = stage._bounded_context(pages)
    assert context["repos/demo/components/worker/design-tradeoffs.md"] == "Existing design."
    assert "repos/demo/components/another/architecture.md" not in context


def test_existing_rule_page_at_knowledge_target_blocks_before_generation(world):
    gateway = KnowledgeGateway()
    _chain(world, gateway)
    modules = InitRecord.load(_runtime(world).state_dir, "toy", "modules")
    page = _tree(modules)["knowledge/repos/toy/components/tooling/tools-lint.md"].replace(
        "type: architecture", "type: rule")
    _commit(world["origin"], {"knowledge/repos/toy/components/tooling/knowledge.md": page}, "target conflict")
    gateway.calls.clear()
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "knowledge", dry_run=True)
    assert record.status == "blocked"
    assert "must be an explanatory architecture or guide page" in record.problems[0]
    assert gateway.calls == []


def test_starting_knowledge_invalidates_a_cached_later_stage(world):
    gateway = KnowledgeGateway()
    _chain(world, gateway, coverage_target=0.01)
    rt = _runtime(world, gateway)
    lifecycle = _modules_lifecycle(coverage_target=0.01)
    before = run_stage(rt, lifecycle, "deepen", dry_run=True)
    assert before.status == "dry_run", before.problems
    InitRecord(stage="knowledge", repo="toy", pin=before.pin, status="blocked",
               inputs_digest="new-knowledge-pass").save(rt.state_dir)
    after = run_stage(rt, lifecycle, "deepen", dry_run=True)
    assert after.status == "blocked"
    assert any("knowledge stage is blocked" in p for p in after.problems)


def test_uncertain_knowledge_stays_out_of_pages_and_does_not_count_as_covered(world):
    from infermatrix_copilot.kb_service.gate import JUDGE_SYSTEM

    class UnsureGateway(KnowledgeGateway):
        def call_json(self, role, **kwargs):
            reply = super().call_json(role, **kwargs)
            if kwargs["system"] == JUDGE_SYSTEM:
                payload = json.loads(kwargs["prompt"].split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
                if payload["change"]["op"] == "prose":
                    reply.data["dimensions"]["faithful"] = "unsure"
            return reply

    gateway = UnsureGateway()
    _chain(world, gateway)
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "knowledge", dry_run=True)
    assert record.status == "empty", record.problems
    assert record.coverage["knowledge"]["covered_by_facet"]["architecture"] == 0
    assert record.coverage["knowledge"]["owners"]["core"]["facets"]["architecture"] == "needs_review"
    assert not record.files and not record.pr
    assert record.verdicts["knowledge:core:architecture"]["verdict"] == "unsure"
    assert any("draft retained in model traces" in item for item in record.unfinished)


@pytest.mark.parametrize("change", [
    {"facet": "rules"}, {"interpretation": "historical-intent"}, {"body": "## fake heading"},
    {"evidence": []}, {"evidence": [{"path": "a.py", "start": True, "end": 2}]},
    {"body": "<!-- kb:knowledge owner=fake facet=api pin=bad -->"},
])
def test_invalid_knowledge_cannot_claim_facet_coverage(change):
    section = {"facet": FACETS[0], "title": "Architecture", "body": "Useful prose.",
               "interpretation": "fact", "evidence": [{"path": "a.py", "start": 1, "end": 2}]}
    section.update(change)
    with pytest.raises(ValueError):
        validate_sections({"sections": [section]})


@pytest.mark.parametrize("feature_mode", [False, True])
def test_capped_foundation_offers_related_validation_without_claiming_execution(world, feature_mode):
    gateway = KnowledgeGateway()
    _chain(world, gateway)
    pin = _commit(world["upstream"], {
        "pkg/cache_api.py":"class CachedRevision:\n    def verify(self):\n        return 7\n",
        "tests/test_cache_api.py":"from pkg.cache_api import CachedRevision\n\ndef test_revision():\n    assert CachedRevision().verify() == 7\n",
    }, "pinned implementation and validation")
    if feature_mode:
        import yaml
        from test_kb_init_knowledge_coverage import _policy_data
        from infermatrix_copilot.kb_service.knowledge_coverage import policy_path
        policy = _policy_data()
        policy["core"]["roots"] = ["pkg/", "tools/"]
        policy["features"][0].update(owner="core", source_globs=["pkg/cache_api.py"],
            docs=["docs/guide.md"], page="repos/toy/components/core/feature-demo.md")
        _commit(world["origin"], {policy_path("toy"):yaml.safe_dump(policy)}, "declare feature coverage")
    gateway.calls.clear()
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "knowledge", dry_run=True, pin=pin)
    assert record.status == "dry_run", record.problems
    packets = [json.loads(call["prompt"].split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
               for call in gateway.calls if call["system"] == CURRENT_KNOWLEDGE_SYSTEM]
    assert packets and all(packet["foundation_generation_policy_version"] == FOUNDATION_GENERATION_POLICY_VERSION for packet in packets)
    related = [item for packet in packets for item in packet["files"]
               if item["path"] == "tests/test_cache_api.py"]
    assert related and any("assert CachedRevision().verify() == 7" in "\n".join(item["text"]) for item in related)
    if feature_mode:
        feature = next(packet for packet in packets if packet["owner"] == "feature-demo")
        assert any(item["path"] == "tests/test_cache_api.py" for item in feature["files"])
    assert all("source text only, no execution outcome evidence" in item["test_context"] for item in related)
    for packet in packets:
        assert sum(len("\n".join(item["text"]).encode()) for item in packet["files"]) <= 100000
    # The scripted generator intentionally offers no validation section: test
    # availability must not be promoted to accepted semantic coverage.
    assert all(row["facets"]["validation"] == "missing"
               for row in record.coverage["knowledge"]["owners"].values())


@pytest.mark.parametrize("old_version", [None, 1])
def test_changed_capped_evidence_policy_does_not_rebind_unknown_dispatch(world, monkeypatch, old_version):
    from infermatrix_copilot.kb_service import init_knowledge_parallel as parallel
    from infermatrix_copilot.kb_service.init_support import InitError
    from infermatrix_copilot.kb_service.models import ModelUnavailable
    gateway = KnowledgeGateway()
    _chain(world, gateway)
    rt = _runtime(world, gateway)
    original = _Knowledge._input_options
    dispatches = []
    def legacy_options(stage):
        options = original(stage)
        assert options["foundation_test_evidence_version"] == 2
        if old_version is None:
            options.pop("foundation_test_evidence_version")
        else:
            options["foundation_test_evidence_version"] = old_version
        return options
    def lost_reply(stage, job):
        dispatches.append(job["owner"].owner)
        raise ModelUnavailable("simulated lost original worker response")
    with monkeypatch.context() as patch:
        patch.setattr(_Knowledge, "_input_options", legacy_options)
        patch.setattr(parallel, "_worker", lost_reply)
        run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=True)
    path = InitRecord.path(rt.state_dir, "toy", "knowledge")
    saved = InitRecord.load(rt.state_dir, "toy", "knowledge")
    assert saved.coverage["foundation_jobs"]["inflight"] and dispatches
    before = path.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("changed evidence policy must not dispatch an old unknown job")
    monkeypatch.setattr(parallel, "_worker", forbidden)
    try:
        result = run_stage(rt, _modules_lifecycle(), "knowledge", dry_run=True)
    except InitError as exc:
        assert "frozen input identity differs" in str(exc)
    else:
        assert result.status == "blocked"
        assert any("frozen input identity differs" in problem for problem in result.problems)
    assert path.read_bytes() == before
