"""Every page kb init routes to through a knowledge-side ``_routes.yaml``
carries a Direct quick map, checked with the production extractor.

Regression for the first real run (afd-plugin skeleton, PR #265): routed pages
without a ``## … Direct …`` section fail ``test_every_routed_page_yields_a_quick_map``.
Offline, on the skeleton/modules/deepen suites' toy world.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from infermatrix_copilot.direct_routing import _direct_quick_map, _direct_quick_map_text
from infermatrix_copilot.kb_service.init_quick_maps import (
    HARD_CAP, MARKER, MAX_CHARS, QUICK_MAP_HEADING, has_hand_written_map, map_inputs, owner_pages,
    quick_map_problems, refresh_quick_maps, render_quick_map, with_quick_map,
)
from infermatrix_copilot.kb_service.init_stages import run_stage, validate_change
from infermatrix_copilot.kb_service.init_support import knowledge_changes
from infermatrix_copilot.knowledge_service.facts import FactsError
from infermatrix_copilot.knowledge_service.l1 import check_changeset, check_tree
from infermatrix_copilot.knowledge_service.lifecycle import ANY_RULE_HEADING, RULE_HEADING, Page

from test_kb_init_deepen import CodeGateway, _chain, _churn
from test_kb_init_modules import CardGateway, _modules_lifecycle, _world_with_tools
from test_kb_init_skeleton import (  # noqa: F401 - the fixture is used by name
    FakeGateway, _commit, _lifecycle, _runtime, _tree, world,
)

RULE_PAGE = (
    '---\ntitle: "Core rules"\ncreated: 2026-09-01\nupdated: 2026-09-01\ntype: rule\n'
    "tags: [toy]\nsources: []\n---\n\n# Core rules\n\nHow the engine is reviewed.\n\n"
    "## TOY-1a — Ticks are idempotent\n\n- 触发：a PR edits `pkg/core.py` or `pkg/util.py::helper`.\n"
    "- 强制：a second `Engine.step` call returns the same value.\n\n"
    "<!-- kb:rule status=active since=2026-09-01 -->\n\n"
    "## TOY-1b — Helpers stay pure\n\n- 强制：`pkg/util.py` never imports the engine.\n\n"
    "<!-- kb:rule status=active since=2026-09-01 -->\n"
)


def _routed_pages(tree: dict[str, str]) -> list[str]:
    routes = yaml.safe_load(tree["knowledge/repos/toy/_routes.yaml"])
    return list(dict.fromkeys(str(o["path"]) for o in routes["owners"]))


def _production_check(tree: dict[str, str], pages: list[str], tmp_path: Path) -> dict[str, str]:
    """What ``test_every_routed_page_yields_a_quick_map`` would see: the file
    on disk through ``_direct_quick_map``."""
    out = {}
    for page in pages:
        target = tmp_path / page
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(tree[f"knowledge/{page}"], encoding="utf-8")
        text, status = _direct_quick_map(str(target))
        assert text.startswith(QUICK_MAP_HEADING) or status == "unavailable"
        out[page] = status
    return out


# -- rendering ---------------------------------------------------------------------

def test_the_heading_is_never_a_rule_heading():
    assert RULE_HEADING.match(QUICK_MAP_HEADING) is None
    section = render_quick_map(RULE_PAGE, signals=["engine"], prefixes=["pkg/"])
    page = with_quick_map(RULE_PAGE, section)
    parsed = Page.parse(page)
    assert [s.rule_id for s in parsed.rules()] == ["TOY-1a", "TOY-1b"]
    assert parsed.sections[0].heading == QUICK_MAP_HEADING and not parsed.sections[0].is_rule
    # not even the loose ID scanner (which L1's duplicate-ID check uses across
    # pages) reads the heading as an ID: "Direct" is not the first word
    assert [m.group("rule") for m in ANY_RULE_HEADING.finditer(page)] == ["TOY-1a", "TOY-1b"]
    assert not check_tree({"repos/toy/rules.md": page, "repos/toy/rules-2.md": page.replace("TOY-1", "TOY-2"),
                           "repos/toy/_index.md": "---\ntitle: t\ntype: index\n---\n# t\n\n- [a](rules.md)\n- [b](rules-2.md)\n"})


def test_rules_become_rows_with_their_trigger_and_paths():
    section = render_quick_map(RULE_PAGE, signals=["engine", "tick"], prefixes=["pkg/"])
    lines = section.splitlines()
    assert lines[0] == QUICK_MAP_HEADING and lines[1] == MARKER
    assert "触发词：engine、tick。" in section
    assert "| a PR edits `pkg/core.py` or `pkg/util.py::helper`. | TOY-1a | `pkg/core.py`、`pkg/util.py::helper` |" in lines
    assert "| Helpers stay pure | TOY-1b | `pkg/util.py` |" in lines
    text, status = _direct_quick_map_text(with_quick_map(RULE_PAGE, section))
    assert status == "ok" and "TOY-1b" in text


def test_a_page_without_rules_gets_one_entry_row():
    card = "---\ntitle: c\ntype: architecture\n---\n\n# Card\n\nThe lint worker.\n\n**Key files**\n\n- `tools/lint/y.py`\n"
    section = render_quick_map(card, signals=["lint"], prefixes=["tools/lint/"])
    assert "| lint | 入口 | `tools/lint/y.py` |" in section
    page = with_quick_map(card, section)
    assert page.endswith("\n") and _direct_quick_map_text(page)[1] == "ok"
    assert Page.parse(page).sections[0].heading == QUICK_MAP_HEADING
    bare = "---\ntitle: c\ntype: architecture\n---\n\n# Card\n\nNothing named here.\n"
    assert render_quick_map(bare, signals=[], prefixes=["tools/lint/"]).count("| （无触发词） | 入口 | `tools/lint/` |") == 1


def test_the_map_never_exceeds_the_caps():
    rules = "".join(
        f"## TOY-{n}x — Rule number {n} with a long title that repeats itself to fill the row\n\n"
        f"- 触发：a PR edits `pkg/module_{n}/handler.py` or `pkg/module_{n}/state.py` and mentions {n}.\n"
        f"- 强制：keep {n}.\n\n<!-- kb:rule status=active since=2026-09-01 -->\n\n" for n in range(1, 120))
    page = RULE_PAGE.split("## TOY-1a")[0] + rules
    section = render_quick_map(page, signals=[f"signal {i}" for i in range(12)], prefixes=["pkg/"])
    assert len(section) <= MAX_CHARS + 1 and len(section) < HARD_CAP
    assert section.rstrip("\n").endswith("| … | … | … |")
    assert _direct_quick_map_text(with_quick_map(page, section))[1] == "ok"
    assert "| " in section and "\\|" not in section


def test_pipes_in_cells_are_escaped():
    page = RULE_PAGE.replace("Helpers stay pure", "Helpers | stay pure")
    section = render_quick_map(page, signals=["a|b"], prefixes=[])
    assert "| Helpers \\| stay pure | TOY-1b |" in section and "触发词：a\\|b。" in section


def test_regeneration_replaces_only_the_init_map_and_keeps_a_hand_written_one():
    first = with_quick_map(RULE_PAGE, render_quick_map(RULE_PAGE, signals=["engine"], prefixes=["pkg/"]))
    grown = first.replace("## TOY-1b", "## TOY-1c — A third rule\n\n- 强制：x.\n\n"
                          "<!-- kb:rule status=active since=2026-09-01 -->\n\n## TOY-1b")
    second = with_quick_map(grown, render_quick_map(grown, signals=["engine"], prefixes=["pkg/"]))
    assert second.count(QUICK_MAP_HEADING) == 1 and second.count(MARKER) == 1
    parsed = Page.parse(second)
    assert parsed.sections[0].heading == QUICK_MAP_HEADING
    assert [s.rule_id for s in parsed.rules()] == ["TOY-1a", "TOY-1c", "TOY-1b"]
    assert "| A third rule | TOY-1c |" in second
    hand = RULE_PAGE.replace("# Core rules\n", "# Core rules\n\n## Direct code map\n\n| a | b | c |\n|---|---|---|\n| x | y | z |\n")
    assert has_hand_written_map(hand) and not has_hand_written_map(first)
    assert with_quick_map(hand, render_quick_map(hand, signals=["engine"], prefixes=["pkg/"])) == hand


def test_regeneration_keeps_what_the_page_gained_after_the_map():
    """The map is the run of table rows after its separator; an index line,
    prose and even a second table appended after it belong to the page."""
    index = ("---\ntitle: \"Tooling\"\ntype: index\ntags: [toy]\nsources: []\n---\n\n# Tooling\n\n"
             "- [Card](card.md)\n")
    first = with_quick_map(index, render_quick_map(index, signals=["lint"], prefixes=["tools/"]))
    gained = first.rstrip("\n") + "\n- [Tooling rules](rules.md)\n\nSee also:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    second = with_quick_map(gained, render_quick_map(gained, signals=["lint", "fmt"], prefixes=["tools/"]))
    assert second.count(QUICK_MAP_HEADING) == 1 and "触发词：lint、fmt。" in second
    assert "- [Tooling rules](rules.md)" in second and "See also:" in second and "| 1 | 2 |" in second
    assert second.index("| lint、fmt | 入口 |") < second.index("- [Tooling rules](rules.md)") < second.index("| 1 | 2 |")
    lines = second.splitlines()
    assert lines.count("|---|---|") == 1 and lines.count("|---|---|---|") == 1


def test_a_map_that_no_longer_fits_blocks_instead_of_going_stale():
    filler = "".join(f"- note {i}: keep the tick log readable for operators.\n" for i in range(470))
    page = RULE_PAGE.replace("How the engine is reviewed.\n", "How the engine is reviewed.\n\n" + filler)
    routes = ("schema_version: 1\nowners:\n- {owner: core, path: repos/toy/rules.md, signals: [engine],"
              " scope_prefixes: [pkg/]}\n")
    head, notes, problems = refresh_quick_maps({"repos/toy/rules.md": page}, {}, routes, briefing_docs=())
    assert not problems and MARKER in head["repos/toy/rules.md"]
    grown = head["repos/toy/rules.md"] + "".join(
        f"## TOY-{n}z — More\n\n- 强制：{n}.\n\n<!-- kb:rule status=active since=2026-09-01 -->\n\n" for n in range(4))
    head2, _, problems = refresh_quick_maps({"repos/toy/rules.md": grown}, head, routes, briefing_docs=())
    assert problems == ["quick map: repos/toy/rules.md has no room for its Direct quick map (page capacity): "
                        "split the page by hand, or route another page"]
    assert head2["repos/toy/rules.md"] == grown          # nothing half-written
    assert map_inputs(routes, "repos/toy/rules.md") == (["engine"], ["pkg/"])
    assert map_inputs(routes, "repos/toy/other.md") == ([], [])


def test_a_newly_truncated_map_blocks_but_an_old_one_is_tolerated():
    # level-three rules right after the map are part of the served section
    rules = "".join(f"### TOY-{n}q — Rule {n}\n\n- 强制：{'x' * 60} {n}.\n\n"
                    f"<!-- kb:rule status=active since=2026-09-01 -->\n\n" for n in range(60))
    page = RULE_PAGE.split("## TOY-1a")[0] + rules
    mapped_page = with_quick_map(page, render_quick_map(page, signals=[], prefixes=["pkg/"]))
    assert _direct_quick_map_text(mapped_page)[1] == "truncated"
    files = {"repos/toy/rules.md": mapped_page}
    assert quick_map_problems(files, ["repos/toy/rules.md"], base={}) == [
        "quick map: repos/toy/rules.md: the Direct section as served exceeds 3500 characters (truncated); keep "
        "the map first and short, and start the rules with a `## ` heading so level-three rules are not part of it"]
    assert quick_map_problems(files, ["repos/toy/rules.md"], base=files) == []    # unchanged: known debt
    assert len(quick_map_problems(files, ["repos/toy/rules.md"])) == 1            # no base given: strict


def test_a_regenerated_map_is_a_prose_change_for_l1():
    base = with_quick_map(RULE_PAGE, render_quick_map(RULE_PAGE, signals=["engine"], prefixes=["pkg/"]))
    head = with_quick_map(base, render_quick_map(base, signals=["engine", "tick"], prefixes=["pkg/"]))
    assert base != head
    index = "---\ntitle: t\ntype: index\n---\n# t\n\n- [a](rules.md)\n"
    before = {"repos/toy/rules.md": base, "repos/toy/_index.md": index}
    after = {"repos/toy/rules.md": head, "repos/toy/_index.md": index}
    result = check_changeset(before, after, knowledge_changes(before, after))
    assert not result.issues, result.issues
    assert {b.kind for b in result.blocks} == {"prose"}


def test_quick_map_problems_name_the_page():
    good = with_quick_map(RULE_PAGE, render_quick_map(RULE_PAGE, signals=[], prefixes=[]))
    files = {"repos/toy/rules.md": good, "repos/toy/bare.md": RULE_PAGE,
             "repos/toy/empty.md": RULE_PAGE.replace("## TOY-1a", "## Direct map\n\n## TOY-1a")}
    problems = quick_map_problems(files, ["repos/toy/rules.md", "repos/toy/bare.md", "repos/toy/empty.md",
                                          "repos/toy/missing.md"])
    assert len(problems) == 3
    assert problems[0].startswith("quick map: repos/toy/bare.md yields no Direct quick map")
    assert problems[1].startswith("quick map: repos/toy/empty.md yields no Direct quick map")
    assert problems[2] == "quick map: routed page repos/toy/missing.md does not exist"
    assert owner_pages("schema_version: 1\nowners:\n- {owner: a, path: p.md, scope_prefixes: [x/]}\n"
                       "- {owner: b, path: p.md, scope_prefixes: [y/]}\n") == ["p.md"]
    assert owner_pages(None) == []


def test_validate_change_blocks_a_routed_page_without_a_map():
    index = "---\ntitle: t\ntype: index\n---\n# t\n\n- [a](rules.md)\n"
    files = {"repos/toy/rules.md": RULE_PAGE, "repos/toy/_index.md": index}

    class Unreachable:   # the claim checks are not under test: nothing to check
        def head(self):
            return "a" * 40

        def top_level(self, sha):
            return set()

    problems = validate_change(files, files, observer=Unreachable(), rules={}, evidence=[],
                               quick_map_pages=["repos/toy/rules.md"])
    assert problems == [p for p in problems if p.startswith("quick map: repos/toy/rules.md")]
    assert len(problems) == 1


# -- the stages --------------------------------------------------------------------

def test_skeleton_routes_only_pages_that_yield_a_map(world, tmp_path):
    record = run_stage(_runtime(world), _lifecycle(), "skeleton", dry_run=True)
    assert record.status == "dry_run", record.problems
    tree = _tree(record)
    pages = _routed_pages(tree)
    assert pages == ["repos/toy/rules.md"]
    assert set(_production_check(tree, pages, tmp_path).values()) == {"ok"}
    page = tree["knowledge/repos/toy/rules.md"]
    parsed = Page.parse(page)
    assert parsed.sections[0].heading == QUICK_MAP_HEADING and MARKER in parsed.sections[0].text
    ids = [s.rule_id for s in parsed.rules()]
    assert ids and all(f"| {i} |" in parsed.sections[0].text for i in ids)


def test_modules_maps_every_new_group_page(world, tmp_path):
    _world_with_tools(world)
    gateway = CardGateway()
    tree: dict[str, str] = {}   # a dry-run tree holds the stage's changes: overlay the chain
    for stage in ("skeleton", "modules"):
        record = run_stage(_runtime(world, gateway), _modules_lifecycle(), stage, dry_run=True)
        assert record.status == "dry_run", (stage, record.problems)
        tree.update(_tree(record))
    pages = _routed_pages(tree)
    assert len(pages) >= 2 and any(p.endswith("/_index.md") for p in pages)
    assert set(_production_check(tree, pages, tmp_path).values()) == {"ok"}
    for page in pages:
        assert Page.parse(tree[f"knowledge/{page}"]).sections[0].heading == QUICK_MAP_HEADING


def test_deepen_regenerates_the_map_with_the_new_rules(world, tmp_path):
    _world_with_tools(world)
    _churn(world)
    gateway = CodeGateway()
    tree: dict[str, str] = {}
    for stage in ("skeleton", "modules", "deepen"):
        record = run_stage(_runtime(world, gateway), _modules_lifecycle(), stage, dry_run=True)
        assert record.status == "dry_run", (stage, record.problems)
        tree.update(_tree(record))
    pages = _routed_pages(tree)
    assert set(_production_check(tree, pages, tmp_path).values()) == {"ok"}
    written = set(record.verdicts)
    assert written
    for page in pages:
        text = tree[f"knowledge/{page}"]
        assert text.count(QUICK_MAP_HEADING) == 1 and text.count(MARKER) == 1
        parsed = Page.parse(text)
        assert parsed.sections[0].heading == QUICK_MAP_HEADING
        for rule in parsed.rules():
            assert f"| {rule.rule_id} |" in parsed.sections[0].text


def test_a_briefing_doc_is_never_made_an_owner(world):
    """An existing repository whose only entry page names code: it cannot be
    routed when the adapter reads it into every prompt (init does not write
    to it, so it could never carry init's map)."""
    _commit(world["origin"], {
        "adapters/toy/manifest.yaml": "name: toy\nknowledge:\n  repo_subdir: repos/toy\n  briefing_docs:\n"
                                      "  - repos/toy/components/core/_index.md\n",
        "knowledge/repos/toy/_index.md": "---\ntitle: \"Toy\"\ncreated: 2026-09-01\nupdated: 2026-09-01\n"
                                         "type: index\ntags: [toy]\nsources: []\n---\n\n# Toy\n\n"
                                         "- [core](components/_index.md)\n",
        "knowledge/repos/toy/components/_index.md": "---\ntitle: \"Components\"\ncreated: 2026-09-01\n"
                                                    "updated: 2026-09-01\ntype: index\ntags: [toy]\nsources: []\n"
                                                    "---\n\n# Components\n\n- [core](core/_index.md)\n",
        "knowledge/repos/toy/components/core/_index.md": "---\ntitle: \"Core\"\ncreated: 2026-09-01\n"
                                                         "updated: 2026-09-01\ntype: index\ntags: [toy]\n"
                                                         "sources: []\n---\n\n# Core\n\n主源码：`pkg/`\n",
    }, "existing toy pages")
    record = run_stage(_runtime(world), _lifecycle(seeds=()), "skeleton", dry_run=True)
    assert record.status == "dry_run", record.problems
    tree = _tree(record)
    assert "repos/toy/components/core/_index.md" not in _routed_pages(tree)
    assert any("components/core/_index.md" in c and "briefing doc" in c for c in record.checklist)
    assert "knowledge/repos/toy/components/core/_index.md" not in tree   # untouched: not in the change


def test_a_full_owner_page_is_only_rerouted_to_the_page_that_took_its_overflow(world):
    """A seed page beside a full rules page is not that page's overflow:
    routing the engine's code to it would serve unrelated knowledge."""
    from test_kb_init_skeleton import _full_rules_page

    _commit(world["origin"], _full_rules_page(487), "full rules page")
    gateway = FakeGateway(doc_rules=[], seed_rules=True)          # nothing spills; a seed page appears
    record = run_stage(_runtime(world, gateway), _lifecycle(), "skeleton", dry_run=True)
    assert record.status == "dry_run", record.problems
    tree = _tree(record)
    assert "knowledge/repos/toy/rules-seed-other-rules.md" in tree
    routes = yaml.safe_load(tree["knowledge/repos/toy/_routes.yaml"])
    assert [o["path"] for o in routes["owners"]] == []
    assert any(c.startswith("owner core dropped: repos/toy/rules.md has no room") for c in record.checklist)


def test_a_manifest_routed_page_is_measured_without_a_map(world):
    """No routes file, no map: counting a hypothetical map would spill a rule
    that fits to a sibling the manifest never routes to, and Direct would
    miss it."""
    from test_kb_init_manifest_routed import ManifestCodeGateway, _manifest_routed_kb, _toy
    from test_kb_init_modules import TOOLS

    # a rule page with no rule yet (deepen skips owners that already bear
    # rules), 493 non-empty lines: the code rule fits, a map on top would not
    filler = "".join(f"- note {i}: keep the tick log readable for operators.\n" for i in range(483))
    near_full = ('---\ntitle: "Core rules"\ncreated: 2026-09-01\nupdated: 2026-09-01\ntype: rule\n'
                 "tags: [toy]\nsources: []\n---\n\n# Core rules\n\n" + filler)
    _manifest_routed_kb(world, core_rules=near_full)
    _commit(world["upstream"], TOOLS, "tools")
    _commit(world["upstream"], {"pkg/util.py": "def helper():\n    return 2\n\n# tick helper\n"}, "util")
    gateway = ManifestCodeGateway(doc_rules=[], seed_rules=False)
    for stage in ("skeleton", "modules"):
        record = run_stage(_runtime(world, gateway), _toy(seeds=()), stage, dry_run=True)
        assert record.status in ("dry_run", "empty"), (stage, record.problems)
    record = run_stage(_runtime(world, gateway), _toy(seeds=()), "deepen", dry_run=True)
    assert record.status == "dry_run", record.problems
    tree = _tree(record)
    core = Page.parse(tree["knowledge/repos/toy/components/core/rules.md"])
    assert core.rules() and "knowledge/repos/toy/components/core/rules-2.md" not in tree
    assert not any("is full" in n for n in record.notes)
    assert QUICK_MAP_HEADING not in tree["knowledge/repos/toy/components/core/rules.md"]


def test_rule_without_source_paths_does_not_claim_owner_scope_as_evidence():
    page = RULE_PAGE.replace("`pkg/core.py`", "engine state").replace(
        "`pkg/util.py::helper`", "the helper").replace("`pkg/util.py`", "the utility")
    section = render_quick_map(page, signals=["engine"], prefixes=["pkg/", "tools/generate/"])
    rows = [line for line in section.splitlines() if "| TOY-" in line]
    assert len(rows) == 2
    assert all(line.endswith("| — |") for line in rows)
    assert "tools/generate/" not in section and "`pkg/`" not in section
    assert _direct_quick_map_text(with_quick_map(page, section))[1] == "ok"


def test_refresh_uses_exact_per_rule_paths_without_fabricating_fallbacks():
    page = RULE_PAGE.replace("`pkg/util.py` never imports", "the utility never imports")
    routes = "schema_version: 1\nowners:\n  - owner: core\n    path: repos/toy/rules.md\n    signals: [engine]\n    scope_prefixes: [pkg/, tools/generate/]\n"
    files, notes, problems = refresh_quick_maps(
        {"repos/toy/rules.md": page}, {}, routes, briefing_docs=())
    assert not problems and not notes
    quick, status = _direct_quick_map_text(files["repos/toy/rules.md"])
    assert status == "ok"
    assert "| Helpers stay pure | TOY-1b | — |" in quick
    assert "| TOY-1a | `pkg/core.py`、`pkg/util.py::helper` |" in quick
    assert "tools/generate/" not in quick


def test_entry_page_retains_owner_scope_navigation_without_rule_evidence():
    page = "---\ntitle: Entry\ntype: index\n---\n\n# Entry\n\nOwner navigation.\n"
    section = render_quick_map(page, signals=["engine"], prefixes=["pkg/", "tools/generate/"])
    assert "| engine | 入口 | `pkg/`、`tools/generate/` |" in section
