"""Depth must survive rules, forged markers, budget stops and interrupted calls."""

import json
from dataclasses import replace

import pytest
import yaml

from infermatrix_copilot.kb_service.depth_inputs import DepthContext, SYSTEM_DEPTH
from infermatrix_copilot.kb_service.depth_judge import SYSTEM_DEPTH_REVIEW
from infermatrix_copilot.kb_service.init_stages import _page_frontmatter, run_stage
from infermatrix_copilot.kb_service.init_support import InitRecord
from infermatrix_copilot.kb_service.knowledge_depth import (
    FACETS, audit_depth, depth_page, render_block, validate_draft, verified_blocks,
)
from infermatrix_copilot.kb_service.knowledge_coverage import load_policy, policy_path
from infermatrix_copilot.kb_service.models import ModelReply
from test_kb_init_knowledge import KnowledgeGateway, _chain
from test_kb_init_modules import _modules_lifecycle
from test_kb_init_skeleton import _commit, _runtime, _tree, world  # noqa: F401

CORE = "from pkg.util import helper\nclass Engine:\n    def step(self, value):\n        if not isinstance(value, int):\n            raise ValueError('integer required')\n        return helper(value)\n"
UTIL = "DEFAULT_INCREMENT = 1\ndef helper(value, increment=DEFAULT_INCREMENT):\n    return value + increment\n"


def _flow():
    return {"facet": "flow", "title": "Validated call path", "interpretation": "fact",
            "body": "Engine.step checks the input type, then calls helper, which returns the incremented integer.",
            "evidence": [{"path": "pkg/core.py", "start": 3, "end": 6},
                         {"path": "pkg/util.py", "start": 2, "end": 3}],
            "trace": [{"path": "pkg/core.py", "symbol": "Engine.step", "start": 3, "end": 6},
                      {"path": "pkg/util.py", "symbol": "helper", "start": 2, "end": 3}]}


class DepthGateway(KnowledgeGateway):
    def __init__(self, *, reject=False, interrupt=False):
        super().__init__()
        self.reject, self.interrupt = reject, interrupt
        self.depth_calls = []

    def subscription_billing(self, role):
        return True

    def call_json(self, role, **kwargs):
        if kwargs["system"] == SYSTEM_DEPTH:
            p = json.JSONDecoder().raw_decode(kwargs["prompt"][kwargs["prompt"].index("{"):])[0]
            self.depth_calls.append(p)
            sections = [_flow()]
            bodies = {
                "api": "The caller supplies an integer to Engine.step and receives the incremented integer.",
                "configuration": "helper's increment argument defaults to DEFAULT_INCREMENT, which is one.",
                "dependencies": "Engine.step depends on the imported pkg.util.helper to implement incrementing.",
                "failure_modes": "Engine.step raises ValueError before calling helper when its input is not an integer.",
                "tradeoffs": "The explicit type check prevents type errors from reaching helper, adding a validation branch.",
                "validation": "test_step asserts the returned increment and the rejected input behavior.",
            }
            for facet, body in bodies.items():
                span = {"configuration": {"path": "pkg/util.py", "start": 1, "end": 3},
                        "validation": {"path": "tests/test_core.py", "start": 1, "end": 6}}.get(
                            facet, {"path": "pkg/core.py", "start": 1, "end": 6})
                sections.append({"facet": facet, "title": facet, "body": body,
                                 "interpretation": "inference" if facet == "tradeoffs" else "fact",
                                 "evidence": [span]})
            data = {"title": "Implementation", "sections": [s for s in sections if s["facet"] in p["facets"]]}
            if kwargs.get("validate"):
                kwargs["validate"](data)
            return ModelReply(role, data, json.dumps(data), role.model, {}, 0.01)
        if kwargs["system"] == SYSTEM_DEPTH_REVIEW:
            if self.interrupt:
                self.interrupt = False
                raise KeyboardInterrupt()
            payload = json.JSONDecoder().raw_decode(kwargs["prompt"][kwargs["prompt"].index("{"):])[0]
            data = {"facets": {facet: {"dimensions": {
                    k: "no" if (self.reject is True or self.reject == facet) and k == "faithful" else "yes"
                    for k in ("faithful", "non_contradictory", "does_not_weaken")}, "reason": "Pinned source checked"}
                    for facet in payload["sections"]}}
            if kwargs.get("validate"):
                kwargs["validate"](data)
            return ModelReply(role, data, json.dumps(data), role.model, {}, 0.01)
        return super().call_json(role, **kwargs)


def _baseline(world, *, features=1):
    gateway = KnowledgeGateway()
    _chain(world, gateway)
    rt = _runtime(world)
    baseline = {}
    for stage in ("skeleton", "modules"):
        baseline.update(_tree(InitRecord.load(rt.state_dir, "toy", stage)))
    inventory = [{"id": "step" + str(i), "title": "Integer step", "owner": "core",
                  "source_globs": ["pkg/core.py", "pkg/util.py"], "docs": ["docs/guide.md"],
                  "entry_points": ["pkg/core.py"],
                  "page": f"repos/toy/components/core/feature-step{i}.md"} for i in range(features)]
    policy = {"schema_version": 1, "core": {"roots": ["pkg/"], "exclude": ["*/tests/*"]}, "features": inventory}
    baseline[policy_path("toy")] = yaml.safe_dump(policy)
    for f in inventory:
        page = f["page"]
        baseline["knowledge/" + page] = _page_frontmatter("Integer step", kind="architecture",
                                                         today="2026-10-01", tags=["toy"]) + "Step behavior.\n"
        index = "knowledge/" + page.rsplit("/", 1)[0] + "/_index.md"
        baseline[index] = baseline.get(index, _page_frontmatter("Core", kind="index",
                                    today="2026-10-01", tags=["toy"])) + f"\n- [Step]({page.rsplit('/',1)[1]})\n"
    _commit(world["origin"], baseline, "merge feature baseline")
    _commit(world["upstream"], {"pkg/core.py": CORE, "pkg/util.py": UTIL,
             "tests/test_core.py": "import pytest\nfrom pkg.core import Engine\ndef test_step():\n    assert Engine().step(1) == 2\n    with pytest.raises(ValueError):\n        Engine().step('bad')\n"}, "implement steps")
    return load_policy(yaml.safe_dump(policy), "repos/toy")


def _complete_breadth(world, policy):
    """A strict semantic campaign starts from a structurally covered baseline."""
    from infermatrix_copilot.kb_service.knowledge_coverage import FACETS as BREADTH_FACETS, contract_block
    from test_kb_init_skeleton import _git

    pin = _git(world["upstream"], "rev-parse", "HEAD")
    files = {}
    for feature in policy.features:
        path = "knowledge/" + feature.page
        text = (world["origin"] / path).read_text()
        for facet in BREADTH_FACETS:
            text += f"\n<!-- kb:knowledge owner=feature-{feature.id} facet={facet} pin={pin} -->\n\n"
            text += "Engine.step validates integer input and calls the helper function to return the incremented value.\n\n"
            text += f"[Implementation](https://github.com/o/toy/blob/{pin}/pkg/core.py#L3-L6) "
            text += f"[Guide](https://github.com/o/toy/blob/{pin}/docs/guide.md#L1-L2)\n"
        for source in ("pkg/core.py", "pkg/util.py"):
            text += "\n" + contract_block("o/toy", pin, source, (world["upstream"] / source).read_text()) + "\n"
        files[path] = text
    _commit(world["origin"], files, "merge structurally covered feature baseline")


def _run(world, gateway, *, budget=10, retry=False):
    lifecycle = _modules_lifecycle()
    lifecycle = replace(lifecycle, init=replace(lifecycle.init, budget_usd=budget))
    rt = _runtime(world, gateway, state_dir=world["tmp"] / "depth")
    return run_stage(rt, lifecycle, "knowledge-deepen", dry_run=True, from_existing=True,
                     subscription_generator=True, retry_unfinished=retry)


def test_depth_visits_rule_bearing_owner_and_tracks_separate_metrics(world):
    policy = _baseline(world)
    gateway = DepthGateway()
    record = _run(world, gateway)
    assert record.status == "dry_run", record.problems
    body = (world["tmp"] / "depth/init/toy/knowledge-deepen-dryrun/PR_BODY.md").read_text()
    assert "Feature implementation depth" in body and "| rule |" not in body
    assert len(gateway.depth_calls) == 1
    report = record.coverage["semantic_depth"]
    assert report["complete_features"] == 1 and report["covered_facets"] == 7
    assert report["production_files_with_semantic_evidence"] == ["pkg/core.py", "pkg/util.py"]
    assert record.coverage["breadth"]["core"]["contract_files"] == 0
    page = _tree(record)["knowledge/" + depth_page(policy.features[0])]
    assert "Engine.step" in page and "kb:depth-proof" in page
    from infermatrix_copilot.knowledge_service.lifecycle import Page
    hints = Page.parse(page).frontmatter_data()
    assert hints["feature"] == policy.features[0].id
    assert hints["entry_points"] == list(policy.features[0].entry_points)
    assert hints["source_globs"] == list(policy.features[0].source_globs)
    _run(world, gateway)
    assert len(gateway.depth_calls) == 1


def test_depth_budget_resume_retains_accepted_work_and_cumulative_spend(world):
    _baseline(world, features=2)
    gateway = DepthGateway()
    before = _run(world, gateway, budget=0.5)
    assert before.status == "dry_run", before.problems
    assert before.spent_usd == 0.5 and not before.depth["done"]
    assert before.coverage["semantic_depth"]["complete_features"] == 1
    base, accepted = before.kb_base_sha, dict(before.depth["accepted"])
    _commit(world["origin"], {"unrelated.txt": "main advanced"}, "unrelated main update")
    after = _run(world, gateway, budget=1.0)
    assert after.status == "dry_run", after.problems
    assert after.spent_usd == 1 and after.kb_base_sha == base
    assert after.depth["done"] and after.coverage["semantic_depth"]["complete_features"] == 2
    assert all(after.depth["accepted"][p] == text for p, text in accepted.items())
    assert len(gateway.depth_calls) == 2


def test_fresh_depth_batch_reconnects_retained_pages_without_model_calls(world):
    policy = _baseline(world)
    first = _run(world, DepthGateway())
    page = "knowledge/" + depth_page(policy.features[0])
    retained = _tree(first)[page]
    # The accepted foundation index stays immutable in the new baseline.
    # Only already checked depth prose is retained for the new batch.
    _commit(world["origin"], {page: retained}, "retain checked depth in new baseline")
    index = "knowledge/repos/toy/components/core/_index.md"
    assert page.rsplit("/", 1)[1] not in (world["origin"] / index).read_text()

    gateway = DepthGateway()
    rt = _runtime(world, gateway, state_dir=world["tmp"] / "fresh-depth")
    record = run_stage(rt, _modules_lifecycle(), "knowledge-deepen", dry_run=True,
                       from_existing=True, subscription_generator=True)
    assert record.status == "dry_run", record.problems
    assert gateway.depth_calls == []
    assert record.coverage["semantic_depth"]["recognized_facets"] == 7
    assert page.rsplit("/", 1)[1] in _tree(record)[index]
    assert (world["origin"] / page).read_text() == retained
    assert page not in record.files


def test_target_foundation_late_facet_precedes_other_owner_pages():
    from types import SimpleNamespace
    from infermatrix_copilot.kb_service.init_knowledge_depth import _KnowledgeDepth

    feature = SimpleNamespace(id="zeta", page="repos/demo/components/core/feature-zeta.md")
    pin = "a" * 40
    marker = lambda facet, verdict="pass": (
        f"<!-- kb:knowledge owner=feature-zeta facet={facet} pin={pin} verdict={verdict} -->\n")
    validation = marker("validation") + "Actual helper asserts the saved result. [Test](actual-test.py)\n"
    current = "Large source metadata.\n" * 300 + marker("architecture") + "Long introduction.\n" * 200
    current += marker("api", "unsure") + "Unsupported API claim.\n" + validation
    current += "<!-- kb:file path=pkg/core.py -->\n" + "Static inventory.\n" * 500
    stage = object.__new__(_KnowledgeDepth)
    stage.head, stage.record = {feature.page: current}, SimpleNamespace(pin=pin)
    background = {"repos/demo/components/core/_index.md": "Unrelated navigation.\n" * 500,
                  "repos/demo/components/core/feature-alpha.md": "Another feature.\n" * 500,
                  feature.page: current}
    context = stage._feature_existing_context(feature, ["validation"], background)
    assert next(iter(context)) == feature.page
    assert context[feature.page].startswith(validation)
    assert "Unsupported API claim" not in context[feature.page]
    assert "Static inventory" not in context[feature.page]
    assert sum(len(text.encode()) for text in context.values()) <= 4000


def test_interrupted_judge_preserves_draft_without_redispatch(world):
    _baseline(world)
    gateway = DepthGateway(interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        _run(world, gateway)
    checkpoint = InitRecord.load(world["tmp"] / "depth", "toy", "knowledge-deepen")
    assert checkpoint.depth["features"]["step0"]["status"] == "extracted"
    assert checkpoint.spent_usd == 0.5
    after = _run(world, gateway)
    assert after.status == "empty", after.problems  # no accepted judgment yet
    assert len(gateway.depth_calls) == 1 and after.spent_usd == 0.5
    entry = after.depth["features"]["step0"]
    assert entry["draft"] == checkpoint.depth["features"]["step0"]["draft"]
    assert entry["status"] == "pending" and entry["review_dispatched"]
    assert "checked" not in entry and not after.depth["done"]


def test_rejected_depth_stays_out_of_pages_and_retry_preserves_spend(world):
    _baseline(world)
    gateway = DepthGateway(reject=True)
    before = _run(world, gateway)
    assert before.status == "empty" and not before.depth["accepted"]
    assert before.spent_usd == 1 and before.coverage["semantic_depth"]["covered_facets"] == 0
    gateway.reject = False
    after = _run(world, gateway, retry=True)
    assert after.status == "dry_run" and after.spent_usd == 1.5
    assert after.coverage["semantic_depth"]["complete_features"] == 1


def _source_tree(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/core.py").write_text(CORE)
    (tmp_path / "pkg/util.py").write_text(UTIL)
    from types import SimpleNamespace
    return SimpleNamespace(id="step0", owner="core", page="repos/demo/components/core/feature-step0.md")


def test_depth_flow_rejects_declared_but_uncalled_next_symbol(tmp_path):
    feature = _source_tree(tmp_path)
    (tmp_path / "pkg/util.py").write_text(UTIL + "\ndef unused(value):\n    return value\n")
    section = _flow()
    section["trace"][1] = {"path": "pkg/util.py", "symbol": "unused", "start": 5, "end": 6}
    section["evidence"][1] = {"path": "pkg/util.py", "start": 5, "end": 6}
    with pytest.raises(ValueError, match="does not call"):
        render_block(feature, section, tmp_path, "o/demo", "a" * 40)


@pytest.mark.parametrize("edit", ["body", "source", "duplicate", "rule"])
def test_depth_proofs_reject_edited_stale_duplicate_or_rule_evidence(tmp_path, edit):
    feature = _source_tree(tmp_path)
    pin = "a" * 40
    block = render_block(feature, _flow(), tmp_path, "o/demo", pin)
    page = _page_frontmatter("Depth", kind="architecture", today="2026-10-01", tags=["demo"]) + block
    assert verified_blocks(page, feature, tmp_path, pin)[0].keys() == {"flow"}
    if edit == "body":
        page = page.replace("incremented integer", "doubled integer")
    elif edit == "source":
        (tmp_path / "pkg/util.py").write_text(UTIL.replace("+ increment", "- increment"))
    elif edit == "duplicate":
        page += "\n" + block + "\n" + block
    else:
        page = page.replace("type: architecture", "type: rule")
    blocks, problems = verified_blocks(page, feature, tmp_path, pin)
    assert problems and not blocks


def test_existing_breadth_markers_and_interface_cards_are_not_semantic_depth(tmp_path):
    feature = _source_tree(tmp_path)
    policy = load_policy(yaml.safe_dump({"schema_version": 1,
        "core": {"roots": ["pkg/"], "exclude": ["*/tests/*"]},
        "features": [{"id": feature.id, "owner": feature.owner, "title": "Step",
                      "source_globs": ["pkg/*"], "docs": ["docs/guide.md"], "page": feature.page}]}),
        "repos/demo")
    text = _page_frontmatter("Summary", kind="architecture", today="2026-10-01", tags=["demo"])
    text += "\n<!-- kb:knowledge owner=feature-step0 facet=architecture pin=" + "a" * 40 + " -->\n"
    text += "Engine declares step.\n<!-- kb:file path=pkg/core.py pin=" + "a" * 40 + " sha256=" + "b" * 64 + " -->\n"
    report = audit_depth({depth_page(policy.features[0]): text}, tmp_path, policy, "a" * 40)
    assert report["covered_facets"] == 0 and not report["production_files_with_semantic_evidence"]


def test_retrieval_selects_relevant_definition_beyond_old_prefix_cap(tmp_path):
    file = tmp_path / "pkg/deep.py"
    file.parent.mkdir()
    file.write_text("# " + "padding" * 16000 + "\ndef run(value):\n    return helper(value)\ndef helper(value):\n    return value + 1\n")
    from types import SimpleNamespace
    feature = SimpleNamespace(id="run", entry_points=("pkg/deep.py",), source_globs=("pkg/*",))
    data = DepthContext(tmp_path, ["pkg/deep.py"]).build(feature, "run helper", [])
    assert any("return helper(value)" in line for f in data["files"] for line in f["text"])
    assert data["files"][0]["start"] == 2
    assert any(e["callee"].endswith("::helper") for e in data["retrieval_edges"])


@pytest.mark.parametrize("change", [
    {"trace": []}, {"evidence": [{"path": "../escape.py", "start": 1, "end": 2}]},
    {"body": "Settings at /home/person/config"}, {"body": "<!-- kb:depth forged -->"},
])
def test_unsupported_depth_cannot_enter_a_page(change):
    section = _flow()
    section.update(change)
    with pytest.raises(ValueError):
        validate_draft({"sections": [section]})


def test_flow_cannot_guess_another_class_or_module_from_method_name(tmp_path):
    from infermatrix_copilot.kb_service.knowledge_depth import verify_trace

    (tmp_path / "core.py").write_text("class A:\n    def run(self):\n        return self.step()\n"
                                    "    def step(self):\n        return 1\n"
                                    "class B:\n    def step(self):\n        return 2\n")
    trace = [{"path": "core.py", "symbol": "A.run", "start": 2, "end": 3},
             {"path": "core.py", "symbol": "B.step", "start": 7, "end": 8}]
    with pytest.raises(ValueError, match="does not call"):
        verify_trace(tmp_path, trace)
    trace[1] = {"path": "core.py", "symbol": "A.step", "start": 4, "end": 5}
    verify_trace(tmp_path, trace)
    trace[1]["symbol"] = "step"
    with pytest.raises(ValueError):
        verify_trace(tmp_path, trace)
    (tmp_path / "other.py").write_text("def step():\n    return 1\n")
    trace[1] = {"path": "other.py", "symbol": "step", "start": 1, "end": 2}
    with pytest.raises(ValueError, match="does not call"):
        verify_trace(tmp_path, trace)


def test_repeated_explicit_retry_can_retry_missing_facets(world):
    _baseline(world)
    gateway = DepthGateway(reject=True)
    _run(world, gateway)
    rejected = _run(world, gateway, retry=True)
    assert rejected.spent_usd == 2
    gateway.reject = False
    accepted = _run(world, gateway, retry=True)
    assert accepted.spent_usd == 2.5 and accepted.coverage["semantic_depth"]["complete_features"] == 1


def test_bad_flow_does_not_discard_checked_other_facets(world):
    _baseline(world)

    class BadFlow(DepthGateway):
        def call_json(self, role, **kwargs):
            result = super().call_json(role, **kwargs)
            if kwargs["system"] == SYSTEM_DEPTH:
                for section in result.data["sections"]:
                    if section["facet"] == "flow":
                        section["trace"][0]["symbol"] = "OtherEngine.step"
            return result

    record = _run(world, BadFlow())
    assert record.status == "dry_run", record.problems
    assert record.coverage["semantic_depth"]["covered_facets"] == 6
    assert record.coverage["semantic_depth"]["features"]["step0"]["missing_facets"] == ["flow"]
    assert record.depth["features"]["step0"]["skipped_facets"]


def test_one_unfaithful_facet_does_not_discard_other_approved_facets(world):
    _baseline(world)
    record = _run(world, DepthGateway(reject="api"))
    assert record.status == "dry_run", record.problems
    assert record.coverage["semantic_depth"]["covered_facets"] == 6
    assert record.coverage["semantic_depth"]["features"]["step0"]["missing_facets"] == ["api"]
    checks = record.verdicts["depth:step0"]["facets"]
    assert checks["api"]["verdict"] == "fail" and checks["flow"]["verdict"] == "pass"
    assert record.spent_usd == 1


def test_client_trace_uses_its_own_checker_and_ignores_comment_calls(tmp_path):
    from types import SimpleNamespace
    from infermatrix_copilot.knowledge_service.facts import claims_in
    from infermatrix_copilot.kb_service.knowledge_depth import verify_trace

    (tmp_path / "client.ts").write_text("function run() { return helper(); }\nfunction helper() { return 1; }\n")
    section = {"facet": "flow", "title": "Client flow", "body": "run returns helper's result.",
               "interpretation": "fact", "evidence": [{"path": "client.ts", "start": 1, "end": 2}],
               "trace": [{"path": "client.ts", "symbol": "run", "start": 1, "end": 1},
                         {"path": "client.ts", "symbol": "helper", "start": 2, "end": 2}]}
    block = render_block(SimpleNamespace(id="client"), section, tmp_path, "o/demo", "a" * 40)
    assert not any(c.kind == "symbol" for c in claims_in(block, {"client.ts"}, active=True))
    (tmp_path / "client.ts").write_text("function run() { /* helper() */ return 1; }\nfunction helper() { return 1; }\n")
    with pytest.raises(ValueError, match="does not show"):
        verify_trace(tmp_path, section["trace"])


def test_incomplete_facet_review_fails_closed():
    from types import SimpleNamespace
    from infermatrix_copilot.kb_service.depth_judge import review_facets
    from infermatrix_copilot.kb_service.init_budget import Budget
    from infermatrix_copilot.kb_service.models import ModelRole, ModelUnavailable

    class Incomplete:
        def call_json(self, *args, **kwargs):
            try:
                kwargs["validate"]({"facets": {}})
            except ValueError as exc:
                raise ModelUnavailable(str(exc)) from exc
            pytest.fail("missing facets must be rejected")

    budget = Budget(1)
    rt = SimpleNamespace(gateway=Incomplete(), judge=ModelRole.parse("judge", "codex:fixture"))
    result = review_facets(rt, budget, SimpleNamespace(judge_call_usd=0.5), feature="step", pin="a" * 40,
                          blocks={"api": "source-backed API"}, existing={}, evidence=[])
    assert result["facets"]["api"]["verdict"] == "unjudged" and budget.spent_usd == 0.5


@pytest.mark.parametrize("command, stage", [("widen", "knowledge"), ("deepen", "knowledge-deepen")])
def test_aliases_use_init_before_opening_a_ledger(monkeypatch, command, stage):
    from infermatrix_copilot.kb_service import cli

    def capture(args, state_dir):
        assert args.stage == stage and args.from_existing and args.dry_run and args.subscription_generator
        return 0

    monkeypatch.setattr(cli, "_init_command", capture)
    monkeypatch.setattr(cli, "_ledger", lambda *_: pytest.fail("alias must not open kb.db"))
    assert cli.main([command, "toy", "--dry-run", "--subscription-generator"]) == 0


@pytest.mark.parametrize("body,valid", [
    ("def run(value=helper()):\n    return value\n", False),
    ("def run(value: helper()):\n    return value\n", False),
    ("@helper()\ndef run():\n    return 1\n", False),
    ("def run(value=helper()):\n    return helper()\n", True),
    ("def run():\n    def inner():\n        return helper()\n    return inner()\n", False),
])
def test_flow_witness_is_a_call_in_the_function_body(tmp_path, body, valid):
    from infermatrix_copilot.kb_service.knowledge_depth import verify_trace

    (tmp_path / "core.py").write_text("def helper():\n    return 1\n" + body)
    trace = [{"path": "core.py", "symbol": "run", "start": 4 if body.startswith("@") else 3,
              "end": 2 + len(body.splitlines())},
             {"path": "core.py", "symbol": "helper", "start": 1, "end": 2}]
    if valid:
        verify_trace(tmp_path, trace)
    else:
        with pytest.raises(ValueError, match="does not call"):
            verify_trace(tmp_path, trace)


@pytest.mark.parametrize("binding,valid", [
    ("1", False),
    ("(1)\nconst other = () => 2", False),
    ("() => 1", True),
    ("async (value) => value", True),
    ("function () { return 1; }", True),
    ("(value: number): number => value", True),
])
def test_client_flow_target_must_be_callable(tmp_path, binding, valid):
    from infermatrix_copilot.kb_service.knowledge_depth import verify_trace

    (tmp_path / "client.ts").write_text("function run() { return helper(); }\nconst helper = " + binding + ";\n")
    trace = [{"path": "client.ts", "symbol": "run", "start": 1, "end": 1},
             {"path": "client.ts", "symbol": "helper", "start": 2, "end": 1 + len(binding.splitlines())}]
    if valid:
        verify_trace(tmp_path, trace)
    else:
        with pytest.raises(ValueError, match="not declared"):
            verify_trace(tmp_path, trace)


def test_duplicate_facet_does_not_discard_other_supported_facets(world):
    _baseline(world)

    class RepeatedFlow(DepthGateway):
        def call_json(self, role, **kwargs):
            result = super().call_json(role, **kwargs)
            if kwargs["system"] == SYSTEM_DEPTH:
                result.data["sections"].append(_flow())
                kwargs["validate"](result.data)
            return result

    record = _run(world, RepeatedFlow())
    assert record.status == "dry_run", record.problems
    assert record.coverage["semantic_depth"]["covered_facets"] == 6
    assert record.coverage["semantic_depth"]["features"]["step0"]["missing_facets"] == ["flow"]
    assert all("duplicate" in p for p in record.depth["features"]["step0"]["skipped_facets"])


def test_repair_prioritizes_features_without_accepted_depth(world):
    _baseline(world, features=2)

    class CoverageFirst(DepthGateway):
        def __init__(self):
            super().__init__()
            self.reviews = {}

        def call_json(self, role, **kwargs):
            if kwargs["system"] == SYSTEM_DEPTH_REVIEW:
                payload = json.JSONDecoder().raw_decode(kwargs["prompt"][kwargs["prompt"].index("{"):])[0]
                feature = payload["feature"]
                self.reviews[feature] = self.reviews.get(feature, 0) + 1
                self.reject = "api" if feature == "step0" else self.reviews[feature] == 1
            return super().call_json(role, **kwargs)

    gateway = CoverageFirst()
    record = _run(world, gateway, budget=1.5)
    assert record.status == "dry_run", record.problems
    assert record.spent_usd == 1.5
    assert gateway.reviews == {"step0": 1, "step1": 2}
    assert record.coverage["semantic_depth"]["covered_facets"] == 13
    assert record.coverage["semantic_depth"]["features"]["step1"]["complete"]


def test_interrupted_repair_judge_preserves_draft_and_prior_acceptance(world):
    _baseline(world)

    class InterruptedRepair(DepthGateway):
        def __init__(self):
            super().__init__()
            self.reviews = 0

        def call_json(self, role, **kwargs):
            if kwargs["system"] == SYSTEM_DEPTH_REVIEW:
                self.reviews += 1
                if self.reviews == 2:
                    raise KeyboardInterrupt()
                self.reject = "api" if self.reviews == 1 else False
            return super().call_json(role, **kwargs)

    gateway = InterruptedRepair()
    with pytest.raises(KeyboardInterrupt):
        _run(world, gateway)
    saved = InitRecord.load(world["tmp"] / "depth", "toy", "knowledge-deepen")
    assert saved.depth["features"]["step0"]["attempts"] == 2
    assert "draft" in saved.depth["features"]["step0"]
    assert len(gateway.depth_calls) == 2
    record = _run(world, gateway)
    assert record.status == "dry_run", record.problems
    assert record.coverage["semantic_depth"]["complete_features"] == 0
    assert record.coverage["semantic_depth"]["covered_facets"] == 6
    assert record.depth["features"]["step0"]["draft"] == saved.depth["features"]["step0"]["draft"]
    assert len(gateway.depth_calls) == 2 and gateway.reviews == 2
    assert not record.depth["done"]


def test_interrupted_last_extraction_cannot_be_replayed_by_explicit_retry(world):
    _baseline(world)

    class InterruptedExtraction(DepthGateway):
        def __init__(self):
            super().__init__(reject="api")
            self.generations = 0

        def call_json(self, role, **kwargs):
            if kwargs["system"] == SYSTEM_DEPTH:
                self.generations += 1
                if self.generations == 2:
                    raise KeyboardInterrupt()
            return super().call_json(role, **kwargs)

    gateway = InterruptedExtraction()
    with pytest.raises(KeyboardInterrupt):
        _run(world, gateway)
    record = _run(world, gateway)
    assert record.status == "dry_run", record.problems
    entry = record.depth["features"]["step0"]
    assert entry["status"] == "pending" and entry["attempts"] == 2
    assert record.coverage["semantic_depth"]["covered_facets"] == 6
    assert gateway.generations == 2 and record.spent_usd == 0.5
    gateway.reject = False
    retried = _run(world, gateway, retry=True)
    assert retried.coverage["semantic_depth"]["complete_features"] == 0
    assert retried.coverage["semantic_depth"]["covered_facets"] == 6
    assert gateway.generations == 2 and retried.spent_usd == 0.5
    assert retried.depth["features"]["step0"]["extraction_dispatched"]
    assert not retried.depth["done"]


def test_explicit_retry_prioritizes_features_without_accepted_depth(world):
    _baseline(world, features=2)

    class CoverageFirst(DepthGateway):
        def __init__(self):
            super().__init__()
            self.reviews = {}

        def call_json(self, role, **kwargs):
            if kwargs["system"] == SYSTEM_DEPTH_REVIEW:
                payload = json.JSONDecoder().raw_decode(kwargs["prompt"][kwargs["prompt"].index("{"):])[0]
                feature = payload["feature"]
                self.reviews[feature] = self.reviews.get(feature, 0) + 1
                self.reject = "api" if feature == "step0" else self.reviews[feature] == 1
            return super().call_json(role, **kwargs)

    gateway = CoverageFirst()
    before = _run(world, gateway, budget=1)
    assert before.coverage["semantic_depth"]["covered_facets"] == 6
    retried = _run(world, gateway, budget=1.5, retry=True)
    assert retried.spent_usd == 1.5
    assert gateway.reviews == {"step0": 1, "step1": 2}
    assert retried.coverage["semantic_depth"]["features"]["step1"]["complete"]


def test_depth_preserves_existing_quick_map_when_linking_pages(world):
    from infermatrix_copilot.kb_service.init_quick_maps import with_quick_map

    _baseline(world)
    index = "knowledge/repos/toy/components/core/_index.md"
    original = (world["origin"] / index).read_text()
    mapping = ("## 代码快速入口（Direct）\n<!-- kb-init:quick-map -->\n\n"
               "触发词：special coordination\n\n"
               "| PR 描述在做什么 | 精确规则 | 第一批 live 源码 |\n|---|---|---|\n"
               "| special coordination | Owner contract | `pkg/core.py` |\n")
    _commit(world["origin"], {index: with_quick_map(original, mapping)}, "extend quick map manually")
    record = _run(world, DepthGateway())
    assert record.status == "dry_run", record.problems
    updated = _tree(record)[index]
    assert mapping.strip() in updated
    assert "feature-depth-step0.md" in updated
