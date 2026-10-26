"""kb init, stage 1 (skeleton): runtime, budget, checks, publisher, CLI.

Fully offline: a toy upstream repository and a toy knowledge repository are
local git repos, the models are scripted, ``gh`` is faked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from infermatrix_copilot.kb_service.config import InitConfig, RepoLifecycle
from infermatrix_copilot.kb_service.gate import JUDGE_SYSTEM
from infermatrix_copilot.kb_service.init_budget import (
    Budget, BudgetExhausted, Price, PriceError, generator_reservation, load_prices, worst_request_usd,
)
from infermatrix_copilot.kb_service.init_stages import (
    SYSTEM_MAP, SYSTEM_RULES, render_pr_body, run_stage, suggest_seeds,
)
from infermatrix_copilot.kb_service.init_support import (
    InitError, InitPublisher, InitRecord, InitRuntime, generate, publishing_allowed,
)
from infermatrix_copilot.kb_service.models import ModelReply, ModelRole, ModelUnavailable
from infermatrix_copilot.kb_service.sources import KnowledgeRepo
from infermatrix_copilot.knowledge_service.lifecycle import Page

REAL_TOOLS = Path(__file__).resolve().parents[1] / "knowledge" / "tools"
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **ENV}).stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _commit(repo: Path, files: dict[str, str], message: str = "c") -> str:
    _write(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


UPSTREAM = {
    "README.md": "# Toy\n\nToy is a tiny engine.\nRun `make test` before sending a pull request.\n",
    "docs/guide.md": "# Guide\nAll workers must call the engine step exactly once per tick.\n",
    "CONTRIBUTING.md": "# Contributing\nChanges to the util module need a matching test.\n",
    "pkg/core.py": "class Engine:\n    def step(self):\n        return 1\n",
    "pkg/util.py": "def helper():\n    return 2\n",
    "tests/test_util.py": "def test_helper():\n    pass\n",
}


def _index(title: str, tags: str, body: str) -> str:
    return (f'---\ntitle: "{title}"\ncreated: 2026-09-01\nupdated: 2026-09-01\ntype: index\n'
            f"tags: [{tags}]\nsources: []\n---\n\n# {title}\n\n{body}")


KNOWLEDGE = {
    "doc/knowledge/SCHEMA.md": "# Schema\n\n## 标签分类法\n\n- `general` `review` `other` `toy`\n\n## 溯源标记\n",
    "adapters/toy/manifest.yaml": "name: toy\n",
    "knowledge/general/_index.md": _index("General", "general", "- [Review tips](tips.md)\n"),
    "knowledge/general/tips.md": ('---\ntitle: "Review tips"\ncreated: 2026-09-01\nupdated: 2026-09-01\n'
                                  "type: guide\ntags: [general, review]\n---\n\n# Review tips\n\nRead the diff twice.\n"),
    "knowledge/repos/_index.md": _index("Repositories", "other", (
        "## Current\n\n| repo | upstream | where |\n|---|---|---|\n"
        "| other | `o/other` | [other](other/_index.md) |\n")),
    "knowledge/repos/other/_index.md": _index("Other", "other", "- [Other rules](rules.md)\n- [Other guide](guide.md)\n"),
    "knowledge/repos/other/guide.md": (
        '---\ntitle: "Other guide"\ncreated: 2026-09-01\nupdated: 2026-09-01\ntype: guide\n'
        "tags: [other]\nsources: []\n---\n\n# Other guide\n\nEngine helpers keep their tick contract documented.\n"),
    "knowledge/repos/other/rules.md": (
        '---\ntitle: "Other rules"\ncreated: 2026-09-01\nupdated: 2026-09-01\ntype: rule\n'
        "tags: [other]\nsources: []\n---\n\n# Other rules\n\n"
        "## OTH-1a — Public functions carry docstrings\n\n"
        "- 强制：every public function in the engine package has a docstring that names its tick contract.\n"),
}

DOC_RULE = {"title": "Util changes ship with their test",
            "body": ("- 触发：a PR edits `pkg/util.py`.\n"
                     "- 强制：the same PR edits `tests/test_util.py`; reviewers reject util edits without it."),
            "evidence": [{"path": "CONTRIBUTING.md", "start": 2, "end": 2}]}
REDUNDANT_RULE = {"title": "Run the tests", "body": "Run `make test` before sending a pull request.",
                  "evidence": [{"path": "README.md", "start": 4, "end": 4}]}
BAD_EVIDENCE_RULE = {"title": "Something unsupported",
                     "body": "- 强制：every engine change is benchmarked on three machines before review.",
                     "evidence": [{"path": "README.md", "start": 90, "end": 99}]}
JUDGED_BAD_RULE = {"title": "BADRULE workers may skip ticks",
                   "body": "- 允许：a worker may skip `pkg/core.py` steps when it is busy, contrary to the guide.",
                   "evidence": [{"path": "docs/guide.md", "start": 2, "end": 2}]}
SEED_RULE = {"title": "Engine functions document their tick contract",
             "body": "- 强制：public functions in `pkg/core.py` state in their docstring how often a tick calls them.",
             "evidence": [{"path": "docs/guide.md", "start": 2, "end": 2}]}


DOC_RULE2 = {"title": "The engine step keeps its once-per-tick test",
             "body": ("- 触发：a PR edits `pkg/core.py`.\n"
                      "- 强制：`tests/test_util.py` still proves each worker calls step once per tick."),
             "evidence": [{"path": "docs/guide.md", "start": 2, "end": 2}]}


class FakeGateway:
    architecture_md = "One package holds the engine and its helpers."
    index_intro = "Where toy review knowledge lives."

    def __init__(self, *, map_owner_page="repos/toy/rules.md", fail_generator=False, doc_rules=None,
                 seed_rules=True):
        self.calls = []
        self.map_owner_page = map_owner_page
        self.fail_generator = fail_generator
        self.doc_rules = doc_rules
        self.seed_rules = seed_rules

    def call_json(self, role, *, system, prompt, validate=None, max_budget_usd=None):
        self.calls.append({"role": role.name, "system": system, "prompt": prompt, "max_budget_usd": max_budget_usd})
        if system == JUDGE_SYSTEM:
            payload = json.loads(prompt.split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
            dims = payload["dimensions_to_answer"]
            answer = "no" if "BADRULE" in payload["change"]["after"] else "yes"
            data = {"dimensions": {d: answer for d in dims}, "reasons": {dims[0]: "scripted"}}
        elif self.fail_generator:
            raise ModelUnavailable("generator down")
        elif system == SYSTEM_MAP:
            data = {"title": "Toy knowledge", "rules_title": "Toy rules", "contents_heading": "Pages",
                    "index_intro": self.index_intro,
                    "architecture_md": self.architecture_md,
                    "owners": [{"owner": "core", "title": "Core", "page": self.map_owner_page,
                                "signals": ["engine", "tick"], "scope_prefixes": ["pkg/", "nope/"]}],
                    "general_links": [{"path": "general/tips.md", "why": "Generic review tips"},
                                      {"path": "general/unoffered.md", "why": "ignored"}]}
        elif system == SYSTEM_RULES and "adapt_from" in prompt:
            data = {"page_title": "Toy rules adapted from other", "rules": [SEED_RULE] if self.seed_rules else []}
        elif system == SYSTEM_RULES:
            data = {"rules": self.doc_rules if self.doc_rules is not None
                    else [DOC_RULE, REDUNDANT_RULE, BAD_EVIDENCE_RULE, JUDGED_BAD_RULE]}
        else:  # pragma: no cover - a prompt the tests do not expect
            raise AssertionError(system[:80])
        if validate is not None:
            validate(data)
        return ModelReply(role, data, json.dumps(data), role.model, {}, 0.1, cost_usd=0.01)


@pytest.fixture()
def world(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    pin = _commit(upstream, UPSTREAM)
    origin = tmp_path / "kb-origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    files = dict(KNOWLEDGE)
    for tool in ("check_knowledge_tree.py", "check_wiki_lint.py"):
        files[f"knowledge/tools/{tool}"] = (REAL_TOOLS / tool).read_text(encoding="utf-8")
    kb_sha = _commit(origin, files)
    clone = tmp_path / "kb-clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    return {"tmp": tmp_path, "upstream": upstream, "pin": pin, "origin": origin, "clone": clone, "kb_sha": kb_sha}


def _runtime(world, gateway=None, **overrides) -> InitRuntime:
    kwargs = dict(
        settings=None, state_dir=world["tmp"] / "state", registry={},
        gateway=gateway or FakeGateway(),
        generator=ModelRole("generator", "claude-code", "claude-opus-5-5"),
        judge=ModelRole("judge", "codex", "gpt-6-sol", "medium"),
        knowledge=KnowledgeRepo(world["clone"]), github=None, environ={},
        clock=lambda: 1_790_000_000.0, upstream_remote=lambda full_name: str(world["upstream"]),
        pull=lambda repository, number: {"merged": False, "merge_commit_sha": ""},
    )
    kwargs.update(overrides)
    return InitRuntime(**kwargs)


def test_interrupted_advisory_judgment_blocks_resume_before_any_new_model_call(world):
    invocations = []

    class InterruptedGateway(FakeGateway):
        def call_json(self, role, **kwargs):
            invocations.append(role.name)
            if kwargs["system"] == JUDGE_SYSTEM:
                raise KeyboardInterrupt()
            return super().call_json(role, **kwargs)

    gateway = InterruptedGateway()
    with pytest.raises(KeyboardInterrupt):
        run_stage(_runtime(world, gateway), _lifecycle(), "skeleton", dry_run=True)
    before = list(invocations)
    resumed = run_stage(_runtime(world, gateway), _lifecycle(), "skeleton", dry_run=True)
    assert resumed.status == "blocked"
    assert any("reconcile original dispatch" in problem for problem in resumed.problems)
    assert invocations == before


def _lifecycle(**init) -> RepoLifecycle:
    config = InitConfig(**{"seeds": ("repos/other/rules.md", "general/tips.md"), "source_roots": ("pkg/",),
                           "budget_usd": 30.0, **init})
    return RepoLifecycle(repo="toy", full_name="o/toy", enabled=False, mode="shadow",
                         knowledge_dir="repos/toy", init=config)


def _tree(record: InitRecord) -> dict[str, str]:
    root = Path(record.pr["dry_run_dir"]) / "tree"
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file()}


# -- the stage end to end -------------------------------------------------------

def test_skeleton_dry_run_builds_a_valid_bootstrap(world):
    gateway = FakeGateway()
    rt = _runtime(world, gateway)
    record = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    assert record.status == "dry_run", record.problems
    assert record.pin == world["pin"] and record.kb_base_sha == world["kb_sha"]
    tree = _tree(record)
    assert set(tree) == {
        "knowledge/repos/_index.md", "knowledge/repos/toy/_index.md", "knowledge/repos/toy/_routes.yaml",
        "knowledge/repos/toy/architecture.md", "knowledge/repos/toy/rules.md",
        "knowledge/repos/toy/rules-seed-other-rules.md",
    }
    # the kept doc rule and the adapted seed rule; the other three were screened out
    rules = Page.parse(tree["knowledge/repos/toy/rules.md"])
    assert [s.rule_id for s in rules.rules()] == ["TOY-I1"]
    assert rules.rules()[0].footer.status == "active"
    seed = Page.parse(tree["knowledge/repos/toy/rules-seed-other-rules.md"])
    assert [s.rule_id for s in seed.rules()] == ["TOY-I5"]
    why = {d["rule_id"]: d["why"] for d in record.dropped}
    assert "D5" in why["TOY-I2"] and "evidence" in why["TOY-I3"] and "fail" in why["TOY-I4"]
    assert record.verdicts["TOY-I1"]["verdict"] == "pass" and "TOY-I4" in record.verdicts
    assert record.evidence["TOY-I1"][0]["path"] == "CONTRIBUTING.md"
    assert record.seeds == [{"origin": "repos/other/rules.md", "kb_sha": world["kb_sha"],
                             "new_page": "repos/toy/rules-seed-other-rules.md", "new_rule_ids": ["TOY-I5"]}]
    # routes: the offered owner page, only prefixes that exist at the pin
    routes = yaml.safe_load(tree["knowledge/repos/toy/_routes.yaml"])
    assert routes == {"schema_version": 1, "owners": [
        {"owner": "core", "path": "repos/toy/rules.md", "signals": ["engine", "tick"], "scope_prefixes": ["pkg/"]}]}
    assert any("nope/" in n for n in record.notes)
    # the entry page links every page, and the shared list links the new repository
    index = tree["knowledge/repos/toy/_index.md"]
    for link in ("rules.md", "rules-seed-other-rules.md", "architecture.md", "../../general/tips.md"):
        assert f"]({link})" in index
    assert "unoffered" not in index
    assert "| toy | `o/toy` | [toy](toy/_index.md) |" in tree["knowledge/repos/_index.md"]
    body = (Path(record.pr["dry_run_dir"]) / "PR_BODY.md").read_text(encoding="utf-8")
    assert "TOY-I1" in body and "repos/other/rules.md" in body and "TOY-I4" not in body.split("Dropped")[0]
    # every generator call carried the spend threshold; the budget was charged
    assert all(c["max_budget_usd"] == 2.0 for c in gateway.calls if c["role"] == "generator")
    assert 0 < record.spent_usd < 30
    assert InitRecord.load(rt.state_dir, "toy", "skeleton").status == "dry_run"
    # init never opened the service ledger
    assert not list(rt.state_dir.rglob("kb.db"))


def test_a_repeated_run_returns_the_record_without_model_calls(world):
    rt = _runtime(world)
    first = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    gateway = FakeGateway()
    again = run_stage(_runtime(world, gateway), _lifecycle(), "skeleton", dry_run=True)
    assert again.inputs_digest == first.inputs_digest and again.status == "dry_run"
    assert gateway.calls == []


def test_an_existing_kb_is_only_appended_to(world):
    existing = {
        "knowledge/repos/toy/_index.md": _index("Toy", "toy", (
            "| page | why |\n|---|---|\n| [Toy rules](rules.md) | rules |\n"
            "| [Components](components/_index.md) | code owners |\n")),
        "knowledge/repos/toy/components/_index.md": _index("Components", "toy", "- [Engine](engine/_index.md)\n"),
        "knowledge/repos/toy/components/engine/_index.md": _index("Engine", "toy", (
            "- 主源码：`pkg/**`、`pkg/core.py`、`tests/test_util.py`、`pkg/missing/**`、`README.md`\n")),
        "knowledge/repos/toy/rules.md": (
            '---\ntitle: "Toy rules"\ncreated: 2026-09-01\nupdated: 2026-09-01\ntype: rule\ntags: [toy]\n'
            "sources: []\n---\n\n# Toy rules\n\n## TOY-1a — Old rule about the engine\n\n"
            "- 强制：changes to `pkg/gone.py` keep the tick contract intact for every worker.\n"),
        "knowledge/repos/_index.md": KNOWLEDGE["knowledge/repos/_index.md"]
        + "| toy | `o/toy` | [toy](toy/_index.md) |\n",
    }
    _commit(world["origin"], existing, "existing toy kb")
    record = run_stage(_runtime(world), _lifecycle(), "skeleton", dry_run=True)
    assert record.status == "dry_run", record.problems
    tree = _tree(record)
    assert "knowledge/repos/_index.md" not in tree            # the repository is already listed
    rules = tree["knowledge/repos/toy/rules.md"]
    assert rules.index("## TOY-1a") < rules.index("## TOY-I1")   # appended, the old rule untouched
    assert "- 强制：changes to `pkg/gone.py`" in rules
    index = tree["knowledge/repos/toy/_index.md"]
    assert index.startswith(existing["knowledge/repos/toy/_index.md"].rstrip("\n"))
    assert "- [Toy rules adapted from other](rules-seed-other-rules.md)" in index
    assert any("pkg/gone.py" in item for item in record.checklist)       # existing claim broken at the pin
    assert any("table" in item for item in record.checklist)
    # routes: the existing component page owns the code it names (deterministically, first),
    # then the model's owner
    routes = yaml.safe_load(tree["knowledge/repos/toy/_routes.yaml"])
    assert routes["owners"][0] == {"owner": "engine", "path": "repos/toy/components/engine/_index.md",
                                   "signals": ["engine"], "scope_prefixes": ["pkg/", "pkg/core.py"]}
    assert [o["owner"] for o in routes["owners"]] == ["engine", "core"]
    # new IDs follow the repository's own prefix
    assert record.verdicts and all(rid.startswith("TOY-I") for rid in record.verdicts)


def _full_rules_page(filler: int) -> dict[str, str]:
    """An existing toy KB whose rules.md has 10 + ``filler`` non-empty lines."""
    bullets = "".join(f"- note {i}: keep the tick log readable for operators.\n" for i in range(filler))
    return {
        "knowledge/repos/toy/_index.md": _index("Toy", "toy", "- [Toy rules](rules.md)\n"),
        "knowledge/repos/toy/rules.md": (
            '---\ntitle: "Toy rules"\ncreated: 2026-09-01\nupdated: 2026-09-01\ntype: rule\ntags: [toy]\n'
            "sources: []\n---\n\n# Toy rules\n\n## TOY-1a — Operator notes\n\n" + bullets),
        "knowledge/repos/_index.md": KNOWLEDGE["knowledge/repos/_index.md"]
        + "| toy | `o/toy` | [toy](toy/_index.md) |\n",
    }


def _judged_texts(gateway: FakeGateway) -> dict[str, str]:
    out = {}
    for call in gateway.calls:
        if call["system"] == JUDGE_SYSTEM:
            change = json.loads(call["prompt"].split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])["change"]
            out[change["rule_id"]] = change["after"]
    return out


def test_rules_that_fill_the_page_together_spill_to_a_sibling(world):
    # 493 lines: the page is the routed owner, so its Direct quick map (one row
    # per rule) counts toward its capacity — the map for its one rule fits
    # (499 lines), the first new rule with its own row would not
    _commit(world["origin"], _full_rules_page(483), "near-full rules page")
    gateway = FakeGateway(doc_rules=[DOC_RULE, DOC_RULE2], seed_rules=False)
    record = run_stage(_runtime(world, gateway), _lifecycle(seeds=()), "skeleton", dry_run=True)
    assert record.status == "dry_run", record.problems
    tree = _tree(record)
    page = Page.parse(tree["knowledge/repos/toy/rules.md"])
    assert [s.rule_id for s in page.rules()] == ["TOY-1a"]
    assert page.sections[0].heading.endswith("（Direct）") and "| TOY-1a |" in page.sections[0].text
    sibling = Page.parse(tree["knowledge/repos/toy/rules-doc-invariants.md"])
    assert [s.rule_id for s in sibling.rules()] == ["TOY-I1", "TOY-I2"]
    assert "](rules-doc-invariants.md)" in tree["knowledge/repos/toy/_index.md"]
    # the judge saw both rules where they are written
    judged = _judged_texts(gateway)
    assert "TOY-I1" in judged["TOY-I1"] and "TOY-I2" in judged["TOY-I2"]
    assert record.verdicts["TOY-I1"]["page"] == "repos/toy/rules-doc-invariants.md"
    assert record.verdicts["TOY-I2"]["page"] == "repos/toy/rules-doc-invariants.md"


def test_a_rule_too_big_for_the_page_on_its_own_goes_to_a_sibling(world):
    # 497 lines: not even the first new rule fits on rules.md, nor its Direct
    # quick map — so the owner is routed to the sibling page init writes
    _commit(world["origin"], _full_rules_page(487), "full rules page")
    gateway = FakeGateway(doc_rules=[DOC_RULE, DOC_RULE2], seed_rules=False)
    record = run_stage(_runtime(world, gateway), _lifecycle(seeds=()), "skeleton", dry_run=True)
    assert record.status == "dry_run", record.problems
    tree = _tree(record)
    assert "knowledge/repos/toy/rules.md" not in tree       # the full page is untouched
    sibling = Page.parse(tree["knowledge/repos/toy/rules-doc-invariants.md"])
    assert [s.rule_id for s in sibling.rules()] == ["TOY-I1", "TOY-I2"]
    assert sibling.sections[0].heading.endswith("（Direct）")
    routes = yaml.safe_load(tree["knowledge/repos/toy/_routes.yaml"])
    assert [o["path"] for o in routes["owners"]] == ["repos/toy/rules-doc-invariants.md"]
    assert any("no room for a Direct quick map" in n for n in record.notes)
    assert all(_judged_texts(gateway)[rid] for rid in ("TOY-I1", "TOY-I2"))
    assert not record.dropped


def test_the_skeleton_publishes_one_pr_with_the_shared_list(world):
    fake = FakeGh()
    env = {"ALLOW_PUSH": "1", "ALLOW_POST": "1", "KB_INIT_GIT_AUTHOR": "tzhouam <tzhouam@example.com>"}
    rt = _runtime(world, environ=env, gh_run=fake)
    record = run_stage(rt, _lifecycle(), "skeleton", dry_run=False)
    assert record.status == "published", record.problems
    assert record.pr["number"] == 7 and record.pr["branch"] == "kb/init-toy-skeleton"
    head = record.pr["head_sha"]
    assert fake.pushed == [head]
    listed = _git(world["clone"], "show", f"{head}:knowledge/repos/_index.md")
    assert "[toy](toy/_index.md)" in listed
    assert _git(world["clone"], "log", "-1", "--format=%an <%ae>", head) == "tzhouam <tzhouam@example.com>"
    changed = set(_git(world["clone"], "diff", "--name-only", world["kb_sha"], head).splitlines())
    assert changed == set(record.files)
    assert "kb init" in fake.created[0].decode()
    # without an author the stage refuses to publish before any model call
    gateway, fake2 = FakeGateway(), FakeGh()
    with pytest.raises(InitError, match="KB_INIT_GIT_AUTHOR"):
        run_stage(_runtime(world, gateway, environ={"ALLOW_PUSH": "1", "ALLOW_POST": "1"}, gh_run=fake2,
                           state_dir=world["tmp"] / "state2"), _lifecycle(), "skeleton", dry_run=False)
    assert gateway.calls == [] and fake2.pushed == []
    # a branch that already carries other content: the record says so, nothing is overwritten
    fake3 = FakeGh(remote_head="f" * 40)
    blocked = run_stage(_runtime(world, environ=env, gh_run=fake3, state_dir=world["tmp"] / "state3"),
                        _lifecycle(), "skeleton", dry_run=False)
    assert blocked.status == "blocked" and "already exists" in blocked.problems[0] and fake3.pushed == []
    assert InitRecord.load(world["tmp"] / "state3", "toy", "skeleton").status == "blocked"


def test_the_pin_is_the_upstream_default_branch_not_main(world):
    upstream = world["upstream"]
    _git(upstream, "branch", "-m", "main", "develop")
    old = world["pin"]
    newer = _commit(upstream, {"docs/extra.md": "# Extra\nMore notes for workers.\n"}, "newer on develop")
    _git(upstream, "branch", "main", old)            # a stale "main" must not be picked
    record = run_stage(_runtime(world), _lifecycle(), "skeleton", dry_run=True)
    assert record.pin == newer != old
    pinned = run_stage(_runtime(world, state_dir=world["tmp"] / "state-pin"), _lifecycle(), "skeleton",
                       dry_run=True, pin=old)
    assert pinned.pin == old


def test_a_pr_that_failed_after_the_push_is_finished_on_retry_without_rerunning(world):
    fake = FakeGh(fail_create=1)
    env = {"ALLOW_PUSH": "1", "ALLOW_POST": "1", "KB_INIT_GIT_AUTHOR": "tzhouam <tzhouam@example.com>"}
    first = run_stage(_runtime(world, environ=env, gh_run=fake), _lifecycle(), "skeleton", dry_run=False)
    assert first.status == "blocked" and "502" in first.problems[0] and len(fake.pushed) == 1
    gateway = FakeGateway()
    later = _runtime(world, gateway, environ=env, gh_run=fake, clock=lambda: 1_800_000_000.0)
    second = run_stage(later, _lifecycle(), "skeleton", dry_run=False)
    assert second.status == "published" and second.pr["head_sha"] == fake.pushed[0]
    assert len(fake.pushed) == 1 and gateway.calls == []      # same commit, no second push, no model call
    assert second.spent_usd == first.spent_usd


def test_a_pending_publication_survives_a_dry_run(world):
    fake = FakeGh(fail_create=1)
    env = {"ALLOW_PUSH": "1", "ALLOW_POST": "1", "KB_INIT_GIT_AUTHOR": "tzhouam <tzhouam@example.com>"}
    first = run_stage(_runtime(world, environ=env, gh_run=fake), _lifecycle(), "skeleton", dry_run=False)
    assert first.status == "blocked"
    with pytest.raises(InitError, match="pending"):
        run_stage(_runtime(world, environ=env, gh_run=fake), _lifecycle(), "skeleton", dry_run=True)
    retry = run_stage(_runtime(world, environ=env, gh_run=fake), _lifecycle(), "skeleton", dry_run=False)
    assert retry.status == "published" and len(fake.pushed) == 1


def test_an_owner_page_must_belong_to_this_repository(world):
    record = run_stage(_runtime(world, FakeGateway(map_owner_page="repos/other/rules.md")), _lifecycle(),
                       "skeleton", dry_run=True)
    assert record.status == "dry_run", record.problems
    routes = yaml.safe_load(_tree(record)["knowledge/repos/toy/_routes.yaml"])
    assert all(o["path"].startswith("repos/toy/") for o in routes["owners"])
    assert any("repos/other/rules.md is not a page of repos/toy" in n for n in record.notes)


def test_a_dry_run_rerun_replaces_the_previous_snapshot(world):
    rt = _runtime(world)
    first = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    assert "knowledge/repos/toy/rules-seed-other-rules.md" in _tree(first)
    gateway = FakeGateway(doc_rules=[], seed_rules=False)
    second = run_stage(_runtime(world, gateway), _lifecycle(seeds=()), "skeleton", dry_run=True)
    assert second.status == "dry_run", second.problems
    tree = _tree(second)
    assert set(tree) == set(second.files)
    assert not any("rules" in path for path in tree)


def test_a_changed_default_branch_is_followed_by_an_existing_mirror(world):
    rt = _runtime(world)
    first = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)       # the mirror now exists
    upstream = world["upstream"]
    _git(upstream, "checkout", "-q", "-b", "develop")
    newer = _commit(upstream, {"docs/extra.md": "# Extra\nMore notes for workers.\n"}, "develop only")
    _git(upstream, "branch", "-D", "main")                                # the old default is gone
    second = run_stage(_runtime(world), _lifecycle(), "skeleton", dry_run=True)
    assert first.pin == world["pin"] and second.pin == newer


def test_fallback_routes_use_normalised_roots(world):
    gateway = FakeGateway(map_owner_page="repos/elsewhere.md")         # no usable owner from the model
    record = run_stage(_runtime(world, gateway), _lifecycle(source_roots=("./pkg/",)), "skeleton", dry_run=True)
    routes = yaml.safe_load(_tree(record)["knowledge/repos/toy/_routes.yaml"])
    assert routes["owners"] == [{"owner": "toy", "path": "repos/toy/_index.md", "signals": ["toy"],
                                 "scope_prefixes": ["pkg/"]}]
    whole = run_stage(_runtime(world, gateway, state_dir=world["tmp"] / "s2"), _lifecycle(source_roots=(".",)),
                      "skeleton", dry_run=True)
    routes = yaml.safe_load(_tree(whole)["knowledge/repos/toy/_routes.yaml"])
    assert routes["owners"][0]["scope_prefixes"] == ["docs/", "pkg/", "tests/"]
    # more top-level files than the prompt shows: the fallback still sees every directory
    _commit(world["upstream"], {f"a{i:03d}.txt": "x\n" for i in range(210)}, "many root files")
    crowded = run_stage(_runtime(world, gateway, state_dir=world["tmp"] / "s3"), _lifecycle(source_roots=(".",)),
                        "skeleton", dry_run=True)
    routes = yaml.safe_load(_tree(crowded)["knowledge/repos/toy/_routes.yaml"])
    assert routes["owners"][0]["scope_prefixes"] == ["docs/", "pkg/", "tests/"]


def test_neutral_headings_leave_no_rule_heading():
    from infermatrix_copilot.kb_service.init_stages import neutral_headings
    from infermatrix_copilot.knowledge_service.lifecycle import ANY_RULE_HEADING, RULE_HEADING

    text = ("## Overview\ntext\n## SERV-1 — x\n### Deep ###\n#tag at start\n  ## indented\n#\n"
            "```\n## in code\n```\nend")
    out = neutral_headings(text)
    assert not ANY_RULE_HEADING.search(out) and not any(RULE_HEADING.match(l) for l in out.splitlines())
    assert not any(line.startswith("#") for line in out.splitlines())
    assert "**Overview**" in out and "**SERV-1 — x**" in out and "**Deep**" in out
    assert "\\#tag at start" in out and "```\n ## in code\n```" in out
    # a shorter or different inner fence does not close the outer one
    nested = "````markdown\n```\n## Explain the worker lifecycle\n```\n~~~\n# still code\n````\n## After"
    out = neutral_headings(nested)
    assert "\n ## Explain the worker lifecycle\n" in out and "\n # still code\n" in out
    assert out.endswith("````\n**After**") and not any(l.startswith("#") for l in out.splitlines())
    # a closing run must be at least as long and carry nothing after it
    assert neutral_headings("~~~~\n## a\n~~~\n## b\n~~~~ x\n## c\n~~~~~\n## d").splitlines() == [
        "~~~~", " ## a", "~~~", " ## b", "~~~~ x", " ## c", "~~~~~", "**d**"]


def test_model_headings_in_generated_prose_never_become_rules(world):
    from infermatrix_copilot.knowledge_service.lifecycle import ANY_RULE_HEADING

    gateway = FakeGateway()
    gateway.architecture_md = ("## Overview\nOne package holds the engine and its helpers.\n"
                               "## SERV-1 — engine lifecycle\nWorkers are pooled per host.\n"
                               "```text\n## TOY-I1 — inside code\n```\n")
    gateway.index_intro = "## Overview\nWhere toy review knowledge lives.\n## Scope\nThe engine."
    record = run_stage(_runtime(world, gateway), _lifecycle(), "skeleton", dry_run=True)
    assert record.status == "dry_run", record.problems        # check_tree / validate_change / linters pass
    tree = _tree(record)
    for page in ("knowledge/repos/toy/architecture.md", "knowledge/repos/toy/_index.md"):
        body = tree[page].split("\n---\n", 1)[1]
        assert not ANY_RULE_HEADING.search(body), page
    assert "**SERV-1 — engine lifecycle**" in tree["knowledge/repos/toy/architecture.md"]


def test_a_repo_tag_missing_from_the_taxonomy_blocks(world):
    lifecycle = RepoLifecycle(repo="newrepo", full_name="o/new", enabled=False, mode="shadow",
                              knowledge_dir="repos/newrepo", init=InitConfig(),
                              adapter_dir=Path("adapters/toy"))   # its adapter IS at the base; only the tag is missing
    gateway = FakeGateway()
    record = run_stage(_runtime(world, gateway), lifecycle, "skeleton", dry_run=True)
    assert record.status == "blocked" and "taxonomy" in record.problems[0]
    assert gateway.calls == []


def test_an_unavailable_generator_blocks_and_publishes_nothing(world):
    record = run_stage(_runtime(world, FakeGateway(fail_generator=True)), _lifecycle(), "skeleton", dry_run=True)
    assert record.status == "blocked" and "generator down" in record.problems[0]
    assert "dry_run_dir" not in record.pr


def test_a_generator_without_a_price_is_refused_before_any_call(world):
    gateway = FakeGateway()
    rt = _runtime(world, gateway, generator=ModelRole("generator", "claude-code", "unpriced-model"))
    record = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    assert record.status == "blocked" and "no price" in record.problems[0]
    assert gateway.calls == []


def test_an_exhausted_budget_stops_cleanly_and_lists_the_rest(world):
    # room for the map call only (threshold + one worst request is ~$3.3 each)
    record = run_stage(_runtime(world), _lifecycle(budget_usd=4.0), "skeleton", dry_run=True)
    assert record.status == "dry_run", record.problems
    assert any(item.startswith("doc invariants") for item in record.unfinished)
    assert record.spent_usd <= 4.0


def test_missing_seed_blocks(world):
    lifecycle = _lifecycle()
    lifecycle = RepoLifecycle(**{**lifecycle.__dict__, "init": InitConfig(seeds=("repos/other/nope.md",))})
    record = run_stage(_runtime(world), lifecycle, "skeleton", dry_run=True)
    assert record.status == "blocked" and "does not exist" in record.problems[0]


def test_an_adapter_missing_at_the_base_blocks_the_skeleton(world):
    """Regression (jiuwenswarm pilot, 2026-09-30): the adapter PR was not
    merged, so its manifest was absent from the knowledge repository at the
    base; the modules stage then read "no language" and ran empty instead of
    blocking. Every stage now blocks before any model call."""
    lifecycle = RepoLifecycle(**{**_lifecycle().__dict__, "adapter_dir": Path("adapters/nope")})
    gateway = FakeGateway()
    record = run_stage(_runtime(world, gateway), lifecycle, "skeleton", dry_run=True)
    assert record.status == "blocked"
    assert record.problems == ["adapters/nope/manifest.yaml does not exist in the knowledge repository at the base"]
    assert gateway.calls == []


def test_an_adapter_missing_at_the_base_is_not_hidden_by_a_cached_record(world):
    """A record from a run before this check (same inputs digest: the digest
    never covered the manifest) must not be returned as if the stage passed."""
    record = run_stage(_runtime(world), _lifecycle(), "skeleton", dry_run=True)
    assert record.status == "dry_run", record.problems
    lifecycle = RepoLifecycle(**{**_lifecycle().__dict__, "adapter_dir": Path("adapters/nope")})
    gateway = FakeGateway()
    again = run_stage(_runtime(world, gateway), lifecycle, "skeleton", dry_run=True)
    assert again.status == "blocked"
    assert again.problems == ["adapters/nope/manifest.yaml does not exist in the knowledge repository at the base"]
    assert gateway.calls == []


def test_private_upstream_is_always_a_dry_run(world):
    lifecycle = RepoLifecycle(**{**_lifecycle().__dict__, "upstream_visibility": "private"})
    record = run_stage(_runtime(world), lifecycle, "skeleton", dry_run=False)
    assert record.status == "dry_run" and any("private" in n for n in record.notes)


def test_publishing_needs_both_flags_and_stages_need_their_predecessors(world):
    assert not publishing_allowed({"ALLOW_PUSH": "1"}) and publishing_allowed({"ALLOW_PUSH": "1", "ALLOW_POST": "1"})
    with pytest.raises(InitError, match="ALLOW_PUSH"):
        run_stage(_runtime(world), _lifecycle(), "skeleton", dry_run=False)
    record = run_stage(_runtime(world), _lifecycle(), "harvest-calibration", dry_run=True)
    assert record.status == "blocked" and "run the skeleton stage first" in record.problems
    with pytest.raises(InitError, match="no knowledge_lifecycle.init"):
        run_stage(_runtime(world), RepoLifecycle(repo="toy", full_name="o/toy", enabled=False, mode="shadow",
                                                 knowledge_dir="repos/toy"), "skeleton", dry_run=True)


def test_suggest_seeds_ranks_other_repositories_pages(world):
    ranked = suggest_seeds(_runtime(world), _lifecycle())
    assert ranked and ranked[0][0] in {"repos/other/rules.md", "general/tips.md"}
    assert all(not path.startswith("repos/toy/") for path, _, _ in ranked)


# -- budget ------------------------------------------------------------------------

def test_budget_reserves_before_and_charges_after():
    budget = Budget(1.0)
    with budget.reserve(0.6) as r:
        assert budget.remaining_usd == pytest.approx(0.4)
        with pytest.raises(BudgetExhausted):
            with budget.reserve(0.5):
                pass
        r.charge(0.1)
    assert budget.spent_usd == pytest.approx(0.1)
    with pytest.raises(RuntimeError):
        with budget.reserve(0.2):
            raise RuntimeError("the call failed")
    assert budget.spent_usd == pytest.approx(0.3)   # an uncharged (failed) call costs its reservation
    with budget.reserve(0.1) as r:
        r.charge(None)                               # unknown cost: the whole reservation
    assert budget.spent_usd == pytest.approx(0.4)


def test_worst_request_bound_and_price_table(monkeypatch):
    price = Price(4.0, 20.0, 128_000)
    assert worst_request_usd(price, 1_000_000) == pytest.approx(1.25 * 4.0 + 2.56)
    assert generator_reservation(price, 2.0, 0) == pytest.approx(2.0 + 2.56)
    prices = load_prices({"KB_INIT_PRICES": json.dumps({"other-model": [1, 5, 1000]})})
    assert prices["other-model"] == Price(1.0, 5.0, 1000) and "claude-opus-5-5" in prices
    for bad in ("[", "[]", json.dumps({"m": [1, 2]}), json.dumps({"m": [-1, 2, 3]}), json.dumps({"m": [1, 2, 0]})):
        with pytest.raises(PriceError):
            load_prices({"KB_INIT_PRICES": bad})


def test_generate_charges_the_reported_cost(world):
    gateway = FakeGateway()
    rt = _runtime(world, gateway)
    budget = Budget(10.0)
    reply = generate(rt, budget, InitConfig(), system=SYSTEM_MAP, prompt="{}")
    assert reply.data["title"] == "Toy knowledge"
    assert budget.spent_usd == pytest.approx(0.01)
    assert gateway.calls[0]["max_budget_usd"] == 2.0


# -- publisher ------------------------------------------------------------------------

class FakeGh:
    """Real git for local plumbing; ls-remote, push and gh are scripted."""

    def __init__(self, *, remote_head="", prs=None, fail_create=0):
        self.remote_head = remote_head
        self.prs = list(prs or [])
        self.pushed = []
        self.created = []
        self.fail_create = fail_create

    def __call__(self, cmd, **kwargs):
        if cmd[0] == "gh":
            if cmd[1:3] == ["pr", "list"]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps(self.prs).encode(), b"")
            if cmd[1:3] == ["pr", "create"]:
                if self.fail_create:
                    self.fail_create -= 1
                    return subprocess.CompletedProcess(cmd, 1, b"", b"HTTP 502: bad gateway")
                self.created.append(kwargs.get("input"))
                self.prs = [{"number": 7, "headRefOid": self.pushed[-1]}]
                return subprocess.CompletedProcess(cmd, 0, b"https://example/pull/7", b"")
            raise AssertionError(cmd)
        if "ls-remote" in cmd:
            out = f"{self.remote_head}\trefs/heads/x\n" if self.remote_head else ""
            return subprocess.CompletedProcess(cmd, 0, out.encode(), b"")
        if "push" in cmd:
            ref = next(a for a in cmd if ":refs/heads/" in a and not a.startswith("--"))
            assert any(a.startswith("--force-with-lease=refs/heads/") and a.endswith(":") for a in cmd)
            self.pushed.append(ref.split(":")[0])
            self.remote_head = ref.split(":")[0]   # the branch now exists with this commit
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return subprocess.run(cmd, **kwargs)


def test_publisher_pushes_a_new_branch_and_opens_the_pr(world):
    fake = FakeGh()
    publisher = InitPublisher(world["clone"], "o/kb", run=fake)
    files = {"knowledge/repos/toy/new.md": "hello\n"}
    pr = publisher.open_pr(base_sha=world["kb_sha"], branch="kb/init-toy-skeleton", files=files, title="t",
                           body="b", author=("tzhouam", "tzhouam@example.com"), when=1_790_000_000)
    assert pr["number"] == 7 and fake.pushed == [pr["head_sha"]] and fake.created == [b"b"]
    # deterministic: the same inputs rebuild the same commit, and the open PR is reused
    again = publisher.open_pr(base_sha=world["kb_sha"], branch="kb/init-toy-skeleton", files=files, title="t",
                              body="b", author=("tzhouam", "tzhouam@example.com"), when=1_790_000_000)
    assert again == pr and len(fake.pushed) == 1
    assert _git(world["clone"], "show", f"{pr['head_sha']}:knowledge/repos/toy/new.md") == "hello"


def test_publisher_never_overwrites_or_writes_outside_its_paths(world):
    publisher = InitPublisher(world["clone"], "o/kb", run=FakeGh(remote_head="f" * 40))
    kwargs = dict(base_sha=world["kb_sha"], branch="kb/init-toy-skeleton", title="t", body="b",
                  author=("a", "a@b.c"), when=1)
    with pytest.raises(InitError, match="already exists"):
        publisher.open_pr(files={"knowledge/repos/toy/x.md": "x\n"}, **kwargs)
    with pytest.raises(InitError, match="refusing"):
        publisher.open_pr(files={"src/evil.py": "x\n"}, **kwargs)
    publisher = InitPublisher(world["clone"], "o/kb", run=FakeGh(prs=[{"number": 3, "headRefOid": "e" * 40}]))
    with pytest.raises(InitError, match="does not carry"):
        publisher.open_pr(files={"knowledge/repos/toy/x.md": "x\n"}, **kwargs)


# -- CLI, step, playbook ------------------------------------------------------------------

def test_cli_init_never_opens_the_service_ledger(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service import cli, runner

    seen = {}

    def fake_run(settings, name, repo, *, state_dir, params=None):
        seen.update(name=name, repo=repo, params=params)
        return type("Outcome", (), {"status": "done"})(), state_dir / "runs" / "x"

    monkeypatch.setattr(runner, "run_playbook", fake_run)
    assert cli.main(["--state-dir", str(tmp_path), "init", "toy", "--stage", "skeleton", "--dry-run"]) == 0
    assert seen == {"name": "kb-init", "repo": "toy", "params": {"stage": "skeleton", "dry_run": "true", "pin": ""}}
    assert not (tmp_path / "kb.db").exists()
    assert cli.main(["--state-dir", str(tmp_path), "init", "toy"]) == 2
    assert cli.main(["--state-dir", str(tmp_path), "init", "toy", "--stage", "skeleton", "--suggest-seeds"]) == 2


def test_the_init_step_and_playbook_are_registered():
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.playbooks.store import PlaybookStore
    from infermatrix_copilot.sdk._resources import resource_dir

    registry = register_builtin_steps(StepRegistry())
    playbook = PlaybookStore(resource_dir("playbooks"), registry).get("kb-init")
    assert playbook is not None and [s.step for s in playbook.steps] == ["knowledge.init"]


def test_doc_files_follow_globs_once_and_stay_inside(tmp_path):
    from infermatrix_copilot.profiles.establish import build_doc_corpus, doc_files

    _write(tmp_path / "r", {"README.md": "Alpha beta gamma delta epsilon zeta.\n",
                            "docs/a.md": "One two three four five six.\n", "docs/b.txt": "not markdown\n",
                            "CONTRIBUTING.md": "Seven eight nine ten eleven twelve.\n"})
    outside = tmp_path / "outside.md"
    outside.write_text("secret words here\n", encoding="utf-8")
    (tmp_path / "r" / "docs" / "link.md").symlink_to(outside)
    root = tmp_path / "r"
    names = [p.relative_to(root).as_posix() for p in doc_files(root, ("README*", "docs/**/*.md", "README.md"))]
    assert names == ["README.md", "docs/a.md"]
    corpus = build_doc_corpus(root, globs=("CONTRIBUTING.md",))
    assert "seven" in corpus and "alpha" not in corpus
    assert "alpha" in build_doc_corpus(root)   # the default set is unchanged


def test_pr_body_hides_failed_rules_from_the_table():
    record = InitRecord(stage="skeleton", repo="toy", pin="a" * 40, kb_base_sha="b" * 40,
                        verdicts={"T-1": {"verdict": "pass", "page": "p"}, "T-2": {"verdict": "fail", "page": "p"}},
                        dropped=[{"rule_id": "T-2", "why": "advisory judge: fail"}], checklist=["fix x"])
    body = render_pr_body(record, _lifecycle())
    table = body.split("<details>")[0]
    assert "| T-1 |" in table and "T-2" not in table and "- [ ] fix x" in body


# -- seeds are adapted whatever their page type, and never skipped silently ----------

class _SeedBudgetGateway(FakeGateway):
    """Runs out of budget on the first seed adaptation."""

    def call_json(self, role, *, system, prompt, validate=None, max_budget_usd=None):
        if system == SYSTEM_RULES and "adapt_from" in prompt:
            raise BudgetExhausted("budget spent")
        return super().call_json(role, system=system, prompt=prompt, validate=validate,
                                 max_budget_usd=max_budget_usd)


def test_a_guide_page_seed_is_adapted_too(world):
    record = run_stage(_runtime(world), _lifecycle(seeds=("repos/other/guide.md",)), "skeleton", dry_run=True)
    assert [s["origin"] for s in record.seeds] == ["repos/other/guide.md"]
    assert "knowledge/repos/toy/rules-seed-other-guide.md" in _tree(record)


def test_a_seed_directory_adapts_its_pages_but_not_its_index(world):
    gateway = FakeGateway()
    run_stage(_runtime(world, gateway), _lifecycle(seeds=("repos/other",)), "skeleton", dry_run=True)
    adapted = [json.loads(c["prompt"].split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])["adapt_from"]["path"]
               for c in gateway.calls if c["system"] == SYSTEM_RULES and "adapt_from" in c["prompt"]]
    assert adapted == ["repos/other/guide.md", "repos/other/rules.md"]


def test_a_seed_that_names_only_an_index_is_reported(world):
    record = run_stage(_runtime(world), _lifecycle(seeds=("repos/other/_index.md",)), "skeleton", dry_run=True)
    assert record.seeds == []
    assert any("seed repos/other/_index.md: names no content page" in c for c in record.checklist)


def test_a_seed_that_transfers_nothing_is_reported(world):
    record = run_stage(_runtime(world, FakeGateway(seed_rules=False)), _lifecycle(), "skeleton", dry_run=True)
    assert record.seeds == []
    assert "seed repos/other/rules.md: no rule transfers to this repository" in record.notes
    body = (Path(record.pr["dry_run_dir"]) / "PR_BODY.md").read_text(encoding="utf-8")
    assert "no rule transfers" in body


def test_a_budget_stop_lists_every_seed_it_did_not_adapt(world):
    lifecycle = _lifecycle(seeds=("repos/other/rules.md", "repos/other/guide.md"))
    record = run_stage(_runtime(world, _SeedBudgetGateway()), lifecycle, "skeleton", dry_run=True)
    assert sorted(u.split(":")[0] for u in record.unfinished) == [
        "seed repos/other/guide.md", "seed repos/other/rules.md"]


def test_the_pr_body_reports_the_spend_it_accounted(world):
    record = run_stage(_runtime(world), _lifecycle(), "skeleton", dry_run=True)
    assert record.spent_usd > 0
    body = (Path(record.pr["dry_run_dir"]) / "PR_BODY.md").read_text(encoding="utf-8")
    assert f"- Model spend (accounted): ${record.spent_usd:.2f}" in body


# -- a new index registers each page once ------------------------------------------

def test_unlink_listed_turns_only_listed_links_into_labels():
    from infermatrix_copilot.kb_service.init_stages import unlink_listed

    text = ("Read [the map](architecture.md), [rules](./rules.md#top) and [docs](https://x.y/a.md); "
            "![img](architecture.md) stays; [other](../other/_index.md) stays.")
    out = unlink_listed(text, {"architecture.md", "rules.md"})
    assert out == ("Read the map, rules and [docs](https://x.y/a.md); "
                   "![img](architecture.md) stays; [other](../other/_index.md) stays.")


def test_a_new_index_links_each_page_once_even_when_the_intro_links_it(world):
    """Regression (jiuwenswarm pilot, 2026-09-30): the model's intro linked
    architecture.md and the contents list linked it again; the knowledge-tree
    validator refuses a page registered twice, and the stage blocked."""
    gateway = FakeGateway()
    gateway.index_intro = "Start with [the architecture](architecture.md), then [the rules](./rules.md)."
    record = run_stage(_runtime(world, gateway), _lifecycle(), "skeleton", dry_run=True)
    assert record.status == "dry_run", record.problems
    index = _tree(record)["knowledge/repos/toy/_index.md"]
    assert index.count("](architecture.md)") == 1 and index.count("](rules.md)") == 1
    assert "Start with the architecture, then the rules." in index


def test_unlink_listed_handles_angle_destinations_and_titles():
    from infermatrix_copilot.kb_service.init_stages import unlink_listed

    text = "See [arch](<architecture.md>) and [r](rules.md 'the rules') and [s](<./rules.md#x> \"t\")."
    assert unlink_listed(text, {"architecture.md", "rules.md"}) == "See arch and r and s."


def test_a_new_index_drops_an_intro_whose_listed_links_it_cannot_rewrite(world):
    gateway = FakeGateway()
    gateway.index_intro = "Start with [the architecture][a].\n\n[a]: architecture.md"
    record = run_stage(_runtime(world, gateway), _lifecycle(), "skeleton", dry_run=True)
    assert record.status == "dry_run", record.problems
    index = _tree(record)["knowledge/repos/toy/_index.md"]
    assert index.count("architecture.md") == 1 and "Start with" not in index
    assert any("could not rewrite" in c for c in record.checklist)


def test_a_new_index_rewrites_an_angle_link_in_the_intro(world):
    gateway = FakeGateway()
    gateway.index_intro = "Read [architecture](<architecture.md>) first."
    record = run_stage(_runtime(world, gateway), _lifecycle(), "skeleton", dry_run=True)
    assert record.status == "dry_run", record.problems
    index = _tree(record)["knowledge/repos/toy/_index.md"]
    assert "Read architecture first." in index and index.count("architecture.md") == 1
