"""kb init, stage harvest-calibration: a calibration set from the owner's review.

Offline, on the deepen suite's toy world: the three stages run as dry runs,
the "owner review" edits their snapshots, the harvest reads it back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from infermatrix_copilot.kb_service import init_harvest
from infermatrix_copilot.kb_service.calibration import load_cases
from infermatrix_copilot.kb_service.init_harvest import label_for, set_calibration_set
from infermatrix_copilot.kb_service.init_stages import run_stage
from infermatrix_copilot.kb_service.init_support import InitError, InitRecord
from infermatrix_copilot.knowledge_service.lifecycle import Page

from test_kb_init_deepen import CodeGateway, _churn
from test_kb_init_deepen import _chain as _chain_to_modules
from test_kb_init_modules import MANIFEST, _modules_lifecycle, _world_with_tools
from test_kb_init_skeleton import DOC_RULE, DOC_RULE2, JUDGED_BAD_RULE, _runtime, _tree, world  # noqa: F401

CASES = "adapters/toy/kb-calibration/cases/"


def _three_stages(world, gateway=None):
    _world_with_tools(world)
    _churn(world)
    gateway = gateway or CodeGateway(doc_rules=[DOC_RULE, DOC_RULE2, JUDGED_BAD_RULE])
    _chain_to_modules(world, gateway)
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "deepen", dry_run=True)
    assert record.status == "dry_run", record.problems
    return gateway


def _records(world) -> dict[str, InitRecord]:
    state = world["tmp"] / "state"
    return {s: InitRecord.load(state, "toy", s) for s in ("skeleton", "modules", "deepen")}


def _snapshot_page(world, rule_id: str) -> Path:
    """The dry-run snapshot file that holds ``rule_id`` (the owner edits it)."""
    for record in _records(world).values():
        root = Path(record.pr["dry_run_dir"]) / "tree"
        for path in root.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            if f"## {rule_id} " in text:
                return path
    raise AssertionError(rule_id)


def _owner_deletes(world, rule_id: str) -> None:
    path = _snapshot_page(world, rule_id)
    page = Page.parse(path.read_text(encoding="utf-8"))
    kept = tuple(s for s in page.sections if s.rule_id != rule_id)
    path.write_text(Page(page.frontmatter, page.head, kept).render(), encoding="utf-8")


def _owner_rewrites(world, rule_id: str) -> None:
    path = _snapshot_page(world, rule_id)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("reviewers reject util edits without it", "reviewers ask for it"),
                    encoding="utf-8")


def _cases(record) -> dict[str, dict]:
    tree = _tree(record)
    return {Path(p).stem: json.loads(t) for p, t in tree.items() if p.startswith(CASES)}


def _judged(world, verdict: str) -> list[tuple[str, str]]:
    """(stage, rule ID) of every rule a stage's judge graded ``verdict`` (an ID
    stripped in one stage may come back in a later one)."""
    return sorted((stage, rid) for stage, r in _records(world).items()
                  for rid, v in r.verdicts.items() if v["verdict"] == verdict)


def _case(cases: dict[str, dict], kind: str, judged: tuple[str, str]) -> dict:
    return cases[f"init-{kind}-{judged[0]}-{judged[1]}"]


# -- the label table (design §11) ---------------------------------------------------

@pytest.mark.parametrize("verdict,state,expected", [
    ("pass", "kept", ("pass", {"source": "owner"})),
    ("pass", "absent", ("reject", {"source": "owner", "override": True})),
    ("pass", "changed", ("reject", {"source": "owner", "override": True})),
    ("fail", "absent", ("reject", {"source": "judge"})),
    ("fail", "kept", None),
    ("unsure", "kept", ("pass", {"source": "owner"})),
    ("unsure", "absent", ("reject", {"source": "owner"})),
    ("unsure", "changed", ("reject", {"source": "owner"})),
    ("unjudged", "kept", ("pass", {"source": "owner"})),
    ("unjudged", "absent", ("reject", {"source": "owner"})),
])
def test_labels_follow_the_design_table(verdict, state, expected):
    assert label_for(verdict, state) == expected


# -- the adapter edit ---------------------------------------------------------------

def test_calibration_set_is_one_line_edit():
    after = set_calibration_set(MANIFEST)
    assert after == MANIFEST + "  calibration_set: kb-calibration\n"
    assert set_calibration_set(after) == after
    empty = MANIFEST.replace("  mode: shadow\n", "  mode: shadow\n  calibration_set:  # set by the harvest\n")
    assert "  calibration_set: kb-calibration # set by the harvest\n" in set_calibration_set(empty)
    crlf = MANIFEST.replace("\n", "\r\n")
    assert set_calibration_set(crlf).endswith("  calibration_set: kb-calibration\r\n")
    with pytest.raises(InitError, match="already names"):
        set_calibration_set(MANIFEST + "  calibration_set: other-set\n")
    with pytest.raises(InitError, match="no top-level knowledge_lifecycle"):
        set_calibration_set("name: toy\n")


# -- the stage end to end -------------------------------------------------------------

def test_harvest_labels_the_owners_review(world, monkeypatch):
    monkeypatch.setattr(init_harvest, "MIN_TRUSTED_BAD", 2)
    gateway = _three_stages(world)
    passed, failed = _judged(world, "pass"), _judged(world, "fail")
    assert len(passed) >= 3 and len(failed) == 1
    rewritten = ("skeleton", "TOY-I1")                  # DOC_RULE: the owner rewords it
    assert rewritten in passed
    deleted = next(j for j in passed if j[0] == "skeleton" and j != rewritten)
    # deepen reuses the ID skeleton's judge stripped: the owner keeps deepen's rule
    kept = next(j for j in passed if j[0] == "deepen")
    assert kept[1] == failed[0][1] and failed[0][0] == "skeleton"
    _owner_deletes(world, deleted[1])
    _owner_rewrites(world, rewritten[1])
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "harvest-calibration", dry_run=True)
    assert record.status == "dry_run", record.problems
    cases = _cases(record)
    assert _case(cases, "bad", deleted)["expected"] == "reject"
    assert _case(cases, "bad", deleted)["override"] is True and _case(cases, "bad", deleted)["source"] == "owner"
    assert _case(cases, "bad", rewritten)["override"] is True
    stripped = _case(cases, "bad", failed[0])
    assert stripped["source"] == "judge" and "BADRULE" in json.dumps(stripped)
    assert _case(cases, "good", kept)["expected"] == "pass"
    # each case is self-contained: the judged text in head, not in base, and the pinned evidence text
    good = _case(cases, "good", kept)
    assert any(f"## {kept[1]} " in t for t in good["head"].values())
    assert not any(f"## {kept[1]} " in t for t in good["base"].values())
    assert good["evidence"] and all(e["text"] and "@" in e["source_reference"] for e in good["evidence"])
    # the rewritten rule's case carries the text the JUDGE saw, not the owner's words
    assert "reviewers reject util edits without it" in json.dumps(_case(cases, "bad", rewritten)["head"])
    # the written set is what calibration reads, and the adapter names it
    tree = _tree(record)
    root = Path(record.pr["dry_run_dir"]) / "tree" / "adapters" / "toy" / "kb-calibration"
    assert len(load_cases(root)) == len(cases)
    assert tree["adapters/toy/manifest.yaml"].endswith("  calibration_set: kb-calibration\n")
    assert "enabled: true" in tree["adapters/toy/manifest.yaml"]   # deepen's flip is kept
    assert set(p for p in tree if not p.startswith(CASES)) == {"adapters/toy/manifest.yaml"}
    assert any("calibration cases:" in n for n in record.notes)


def test_harvest_refuses_a_set_the_judge_graded_itself(world, monkeypatch):
    monkeypatch.setattr(init_harvest, "MIN_TRUSTED_BAD", 50)   # more than the toy rules can mutate into
    gateway = _three_stages(world)
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "harvest-calibration", dry_run=True)
    assert record.status == "blocked"
    assert any("at least 50 are needed" in p for p in record.problems)


def test_mutations_fill_in_deterministically(world, monkeypatch):
    monkeypatch.setattr(init_harvest, "MIN_TRUSTED_BAD", 1)
    gateway = _three_stages(world)
    first = run_stage(_runtime(world, gateway), _modules_lifecycle(), "harvest-calibration", dry_run=True)
    assert first.status == "dry_run", first.problems
    cases = _cases(first)
    synthetic = {k: v for k, v in cases.items() if v.get("synthetic")}
    good = [v for v in cases.values() if v["expected"] == "pass"]
    bad = [v for v in cases.values() if v["expected"] == "reject"]
    assert synthetic and len(bad) >= len(good)
    assert len({(v["rule_id"], v["mutation"]) for v in synthetic.values()}) == len(synthetic)
    for case in synthetic.values():
        assert case["expected"] == "reject" and case["source"] == "synthetic"
        assert case["mutation"] in init_harvest.MUTATIONS
    # A fresh batch rebuilds identically. Remove both owners of progress;
    # deleting only its business facts must not authorize cached artifacts.
    state = world["tmp"] / "state" / "init" / "toy"
    (state / "harvest-calibration.json").unlink()
    import shutil
    shutil.rmtree(state / "executions" / "harvest-calibration")
    second = run_stage(_runtime(world, gateway), _modules_lifecycle(), "harvest-calibration", dry_run=True)
    assert _cases(second) == cases


class _Obs:
    def __init__(self, paths):
        self.paths = paths

    def path_exists(self, sha, path):
        return path in self.paths


def test_each_mutation_kind_changes_what_the_judge_sees():
    stage = init_harvest._Harvest.__new__(init_harvest._Harvest)
    stage._observers = {"p": _Obs(set())}
    section = "## X-1 — t\n\n- 强制：`pkg/util.py::helper` returns 2.\n"
    judged = init_harvest._Judged("X-1", "skeleton", "p", "pass", "repos/toy/rules.md", section, [])
    stage._case = lambda item, text, holder, evidence: ({"b": ""}, {"h": text, "e": json.dumps(evidence)})
    broken = stage._mutate("broken_path", judged, None, [{"text": "a"}], [])
    assert "`pkg/removed-util.py::helper`" in broken["head"]["h"] and broken["synthetic"] is True
    negated = stage._mutate("negated", judged, None, [{"text": "a"}], [])
    assert "禁止" in negated["head"]["h"] and "强制" not in negated["head"]["h"]
    donor = init_harvest._Judged("Y-1", "deepen", "p", "pass", "repos/toy/components/tooling/rules.md", section, [])
    swapped = stage._mutate("sibling_evidence", judged, None, [{"text": "a"}],
                            [(judged, None, [{"text": "a"}]), (donor, None, [{"text": "b"}])])
    assert '"b"' in swapped["head"]["e"]
    # a "broken" path the upstream actually has is no bad case
    stage._observers = {"p": _Obs({"pkg/removed-util.py"})}
    assert stage._mutate("broken_path", judged, None, [{"text": "a"}], []) is None
    # evidence moved to lines that show the same supporting text is no bad case either
    ranged = init_harvest._Judged("X-3", "skeleton", "p", "pass", "p.md", section,
                                  [init_harvest.Evidence("pkg/util.py", 1, 1, "")])
    stage._evidence_dicts = lambda item, entries=None: [{"text": "return 2", "source_reference": str(entries)}]
    assert stage._mutate("shifted_range", ranged, None, [{"text": "return 2", "source_reference": "x"}], []) is None
    plain = init_harvest._Judged("X-2", "skeleton", "p", "pass", "p.md", "## X-2 — t\n\nplain words\n", [])
    assert stage._mutate("broken_path", plain, None, [], []) is None
    assert stage._mutate("negated", plain, None, [], []) is None


def test_mutations_top_trusted_bad_cases_up_to_the_floor(world):
    gateway = _three_stages(world)                              # the default floor of five
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "harvest-calibration", dry_run=True)
    assert record.status == "dry_run", record.problems           # the toy rules mutate into enough
    trusted = [c for c in _cases(record).values()
               if c["expected"] == "reject" and c["source"] in ("owner", "synthetic")]
    assert len(trusted) >= init_harvest.MIN_TRUSTED_BAD


def test_a_rule_id_reused_by_a_later_stage_gets_its_own_case(world, monkeypatch):
    monkeypatch.setattr(init_harvest, "MIN_TRUSTED_BAD", 1)
    gateway = _three_stages(world)
    state = world["tmp"] / "state"
    skeleton, deepen = InitRecord.load(state, "toy", "skeleton"), InitRecord.load(state, "toy", "deepen")
    failed = next(rid for rid, v in skeleton.verdicts.items() if v["verdict"] == "fail")
    deepen.verdicts[failed] = dict(skeleton.verdicts[failed])   # the stripped ID came back in deepen, and failed again
    deepen.save(state)
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "harvest-calibration", dry_run=True)
    assert record.status == "dry_run", record.problems
    cases = _cases(record)
    assert f"init-bad-skeleton-{failed}" in cases and f"init-bad-deepen-{failed}" in cases


def test_harvest_refuses_a_set_without_good_cases(world, monkeypatch):
    monkeypatch.setattr(init_harvest, "MIN_TRUSTED_BAD", 1)
    gateway = _three_stages(world)
    for _stage, rid in _judged(world, "pass"):
        _owner_deletes(world, rid)                       # the owner rejects every generated rule
    record = run_stage(_runtime(world, gateway), _modules_lifecycle(), "harvest-calibration", dry_run=True)
    assert record.status == "blocked" and any("no good case" in p for p in record.problems)


def test_harvest_needs_every_rule_stage_first(world):
    record = run_stage(_runtime(world), _modules_lifecycle(), "harvest-calibration", dry_run=True)
    assert record.status == "blocked" and "run the skeleton stage first" in record.problems


def test_a_record_without_judged_text_is_refused(world, monkeypatch):
    monkeypatch.setattr(init_harvest, "MIN_TRUSTED_BAD", 1)
    gateway = _three_stages(world)
    state = world["tmp"] / "state"
    record = InitRecord.load(state, "toy", "skeleton")
    for verdict in record.verdicts.values():
        verdict.pop("section", None)
    record.save(state)
    harvested = run_stage(_runtime(world, gateway), _modules_lifecycle(), "harvest-calibration", dry_run=True)
    assert harvested.status == "blocked" and any("predates the harvest" in p for p in harvested.problems)


def test_explanatory_knowledge_verdicts_are_excluded_from_rule_calibration(world, monkeypatch):
    from test_kb_init_knowledge import KnowledgeGateway

    monkeypatch.setattr(init_harvest, "MIN_TRUSTED_BAD", 1)
    gateway = _three_stages(world)
    knowledge = run_stage(_runtime(world, KnowledgeGateway()), _modules_lifecycle(), "knowledge", dry_run=True)
    assert knowledge.status == "dry_run", knowledge.problems
    assert knowledge.verdicts and all(v["kind"] == "prose" for v in knowledge.verdicts.values())
    deepen = run_stage(_runtime(world, gateway), _modules_lifecycle(), "deepen", dry_run=True)
    assert deepen.status == "dry_run", deepen.problems
    harvested = run_stage(_runtime(world, gateway), _modules_lifecycle(), "harvest-calibration", dry_run=True)
    assert harvested.status == "dry_run", harvested.problems
    cases = _cases(harvested)
    assert cases and all("knowledge:" not in json.dumps(case) for case in cases.values())
