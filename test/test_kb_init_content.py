"""Content placement is deterministic and cannot mutate a caller's execution state."""

from copy import deepcopy

import pytest

from infermatrix_copilot.kb_service.init_content import apply_rules, overflow_page, place_rule
from infermatrix_copilot.knowledge_service.lifecycle import LifecycleError, Page
from infermatrix_copilot.knowledge_service.ops import KnowledgeOperation


PAGE = "repos/toy/rules.md"
INDEX = "repos/toy/_index.md"
SIBLING = "repos/toy/rules-doc-invariants.md"
OPTIONS = {"tags": ("toy",), "today": "2026-10-08", "release": "init-pinned",
           "routes_text": None, "include_quickmaps": True}


def operation(rule_id="TOY-I1"):
    return KnowledgeOperation(kind="add", page=PAGE, rule_id=rule_id,
                              section_markdown=f"## {rule_id} — Dispatch callbacks\n\n"
                                               "- Keep callbacks scoped to their original request throughout asynchronous dispatch.\n")


def nearly_full_page():
    # The existing page fits its own map, but a new rule plus the next map row
    # exceeds the format capacity. The same rule fits without a generated map.
    bullets = "".join(f"- note {n}: keep the tick log readable for operators.\n" for n in range(483))
    return ('---\ntitle: "Toy rules"\ncreated: 2026-09-01\nupdated: 2026-09-01\n'
            'type: rule\ntags: [toy]\nsources: []\n---\n\n# Toy rules\n\n'
            '## TOY-1a — Operator notes\n\n' + bullets)


def test_apply_creates_linked_pages_and_preserves_all_inputs():
    files = {"repos/toy/architecture.md": "existing prose"}
    titles = {PAGE: "Stage-specific rules"}
    ops = [operation(), operation("TOY-I2")]
    before = deepcopy((files, titles, ops, OPTIONS))

    result = apply_rules(files, ops, titles=titles, **OPTIONS)

    assert (files, titles, ops, OPTIONS) == before
    assert result["repos/toy/architecture.md"] == "existing prose"
    assert result[INDEX].count("](rules.md)") == 1
    page = Page.parse(result[PAGE])
    assert page.frontmatter_data()["title"] == "Stage-specific rules"
    assert [section.rule_id for section in page.rules()] == ["TOY-I1", "TOY-I2"]
    assert all(section.footer.since == "init-pinned" for section in page.rules())


def test_placement_counts_quickmaps_and_returns_recording_deltas_only():
    files = {PAGE: nearly_full_page()}
    op = operation()
    titles = {SIBLING: "Previously resolved title"}
    before = deepcopy((files, titles, op))

    placed = place_rule(files, op, base_paths=set(files), titles=titles,
                        title="Stage-specific rules", **OPTIONS)

    assert (files, titles, op) == before
    assert placed.files is not None and not placed.reason
    assert placed.page == SIBLING
    assert placed.spills == {PAGE: [SIBLING]}
    assert placed.notes == [f"{PAGE} is full: TOY-I1 goes to {SIBLING}"]
    assert placed.titles == {}  # Never overwrite a title already chosen by the stage.
    assert placed.files[PAGE] == files[PAGE]
    assert Page.parse(placed.files[SIBLING]).frontmatter_data()["title"] == titles[SIBLING]

    manifest = place_rule(files, op, base_paths=set(files), titles=titles,
                          title="Stage-specific rules", **{**OPTIONS, "include_quickmaps": False})
    assert manifest.files is not None and manifest.page == PAGE
    assert not manifest.notes and not manifest.spills and not manifest.titles


def test_running_placements_share_new_sibling_in_order_without_mutating_prior_result():
    files = {PAGE: nearly_full_page()}
    first = place_rule(files, operation(), base_paths=set(files), titles={}, title="Toy rules", **OPTIONS)
    before = deepcopy(first)
    assert first.files is not None

    second = place_rule(first.files, operation("TOY-I2"), base_paths=set(files),
                        titles=first.titles, title="Toy rules", **OPTIONS)

    assert first == before
    assert first.page == second.page == SIBLING
    assert first.titles == {SIBLING: "Toy rules (2)"}
    assert not second.titles
    assert [section.rule_id for section in Page.parse(second.files[SIBLING]).rules()] == ["TOY-I1", "TOY-I2"]
    assert second.files[INDEX].count("](rules-doc-invariants.md)") == 1


def test_overflow_respects_base_reservations_and_legacy_sibling_names():
    reserved = {SIBLING, "repos/toy/rules-doc-invariants-3.md"}
    before = set(reserved)
    assert overflow_page(PAGE, base_paths=reserved, title="Owner rules") == (
        "repos/toy/rules-doc-invariants-4.md", "Owner rules (4)")
    assert reserved == before
    reserved.update(f"repos/toy/rules-doc-invariants-{n}.md" for n in range(3, 11))
    with pytest.raises(LifecycleError, match="no free sibling"):
        overflow_page(PAGE, base_paths=reserved, title="Owner rules")


def test_non_capacity_refusal_does_not_spill_or_modify_inputs():
    files = apply_rules({}, [operation()], titles={PAGE: "Toy rules"}, **OPTIONS)
    before = dict(files)

    refused = place_rule(files, operation(), base_paths=set(files), titles={}, title="Toy rules", **OPTIONS)

    assert files == before
    assert refused.files is None and refused.page == PAGE
    assert refused.reason.startswith("refused by the knowledge format:")
    assert not refused.spills and not refused.notes and not refused.titles


def test_unplaceable_rule_keeps_explanations_without_returning_partial_content():
    op = KnowledgeOperation(kind="add", page=PAGE, rule_id="TOY-I1",
                            section_markdown="## TOY-I1 — Oversized rule\n\n" + "- Bound the request.\n" * 500)
    files, titles = {}, {}

    refused = place_rule(files, op, base_paths=(), titles=titles, title="Toy rules", **OPTIONS)

    assert not files and not titles and op.page == PAGE
    assert refused.files is None and refused.reason == "no page could take it"
    assert refused.notes and refused.spills and refused.titles
    assert refused.page != PAGE
