"""Knowledge Ops API 2.0 and the L1 gate, on a small synthetic tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from infermatrix_copilot.knowledge_service.l1 import (
    Change, check_changeset, check_tree,
)
from infermatrix_copilot.knowledge_service.lifecycle import (
    Footer, LifecycleError, Page,
)
from infermatrix_copilot.knowledge_service.ops import (
    KnowledgeOperation as Op, apply_operations, load_tombstones,
)

TODAY = "2026-09-28"
REL = "v0.31.0"
PAGE = "repos/demo/core/rules.md"


def _rule(rule_id: str, extra: str = "", cite: str = "PR #10") -> str:
    return (
        f"## {rule_id} — keep the demo queue bounded {extra}\n\n"
        "- 触发：修改 demo queue 的容量或背压逻辑时。\n"
        "- 强制：队列满时拒绝新请求并返回明确错误，不能无界增长。\n"
        f"- 验收：测试覆盖满队列拒绝路径。 ^[{cite}]\n"
    )


def _tree() -> dict[str, str]:
    return {
        "repos/demo/_index.md": "# demo\n",
        "repos/demo/core/_index.md": "# core\n\n- [rules](rules.md)\n",
        PAGE: (
            "---\n"
            'title: "Demo core rules"\n'
            "created: 2026-09-01\n"
            "updated: 2026-09-01\n"
            "type: rule\n"
            "tags: [demo]\n"
            'sources: ["PR #10"]\n'
            "---\n\n"
            "# Demo core rules\n\n"
            + _rule("DEMO-1a") + "\n" + _rule("DEMO-1b")
        ),
        "repos/demo/guide.md": "# guide\n\nSee DEMO-1b for the queue rule.\n",
    }


def _changes(base: dict, head: dict) -> list[Change]:
    out = []
    for path in sorted(set(base) | set(head)):
        if base.get(path) == head.get(path):
            continue
        status = "A" if path not in base else "D" if path not in head else "M"
        out.append(Change(f"knowledge/{path}", status, "100644", "" if status == "D" else "100644"))
    return out


def _apply(base: dict, *ops: Op) -> dict:
    result = apply_operations(base, ops, release=REL, today=TODAY)
    return {**base, **result.files}


def test_parse_render_is_byte_exact_on_the_shipped_tree():
    root = Path(__file__).resolve().parents[1] / "knowledge"
    for path in root.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert Page.parse(text).render() == text, path


def test_add_writes_footer_sources_and_updated():
    base = _tree()
    head = _apply(base, Op("add", PAGE, "DEMO-2a", _rule("DEMO-2a", cite="PR #11")))
    page = Page.parse(head[PAGE])
    assert page.rule("DEMO-2a").footer == Footer(status="active", since=REL)
    assert page.sources() == ["PR #10", "PR #11"]
    assert page.frontmatter_field("updated") == TODAY
    result = check_changeset(base, head, _changes(base, head))
    assert result.ok, result.issues
    assert [(b.kind, b.rule_id, b.op) for b in result.blocks] == [("rule", "DEMO-2a", "add")]


def test_add_to_new_page_appends_index_line():
    base = _tree()
    new = "repos/demo/core/rules-backpressure.md"
    head = _apply(base, Op("add", new, "DEMO-3a", _rule("DEMO-3a"), page_title="Demo backpressure"))
    assert head["repos/demo/core/_index.md"].endswith("- [Demo backpressure](rules-backpressure.md)\n")
    result = check_changeset(base, head, _changes(base, head))
    assert result.ok, result.issues
    assert {b.op for b in result.blocks} == {"add"}


def test_replace_supersedes_atomically_and_flags_dangling_guide():
    base = _tree()
    head = _apply(base, Op("replace", PAGE, "DEMO-1b", _rule("DEMO-1c"),
                           new_rule_id="DEMO-1c", evidence="PR #12"))
    page = Page.parse(head[PAGE])
    assert page.rule("DEMO-1b").footer.superseded_by == "DEMO-1c"
    assert page.rule("DEMO-1c").footer.supersedes == "DEMO-1b"
    result = check_changeset(base, head, _changes(base, head))
    assert ("dangling_reference", "repos/demo/guide.md") in {(i.code, i.path) for i in result.issues}
    head["repos/demo/guide.md"] = "# guide\n\nSee DEMO-1c for the queue rule.\n"
    result = check_changeset(base, head, _changes(base, head))
    assert result.ok, result.issues
    ops = sorted((b.rule_id, b.op) for b in result.blocks if b.kind == "rule")
    assert ops == [("DEMO-1b", "supersede"), ("DEMO-1c", "add")]
    assert any(b.kind == "prose" and b.path == "repos/demo/guide.md" for b in result.blocks)


def test_retire_then_purge_tombstones_and_reserves_the_id():
    base = _tree()
    retired = _apply(base, Op("retire", PAGE, "DEMO-1a", reason="upstream-removed", evidence="PR #13"))
    assert check_changeset(base, retired, _changes(base, retired)).ok
    with pytest.raises(LifecycleError, match="one full release"):
        apply_operations(retired, [Op("purge", PAGE, "DEMO-1a")], release=REL, today=TODAY)
    purged = {**retired, **apply_operations(
        retired, [Op("purge", PAGE, "DEMO-1a")], release="v0.32.0", today=TODAY).files}
    assert [t["id"] for t in load_tombstones(purged["repos/demo/_tombstones.yaml"])] == ["DEMO-1a"]
    result = check_changeset(retired, purged, _changes(retired, purged))
    assert result.ok, result.issues
    with pytest.raises(LifecycleError, match="reserved forever"):
        apply_operations(purged, [Op("add", PAGE, "DEMO-1a", _rule("DEMO-1a"))], release="v0.32.0", today=TODAY)


def test_protected_rules_need_the_human_path():
    base = _tree()
    page = Page.parse(base[PAGE])
    base[PAGE] = page.replace_section(
        "DEMO-1a", page.rule("DEMO-1a").with_footer(Footer(protected=True))).render()
    with pytest.raises(LifecycleError, match="protected"):
        apply_operations(base, [Op("retire", PAGE, "DEMO-1a", reason="incorrect", evidence="x")],
                         release=REL, today=TODAY)
    head = _apply(base, Op("retire", PAGE, "DEMO-1a", reason="incorrect", evidence="x", allow_protected=True))
    codes = {i.code for i in check_changeset(base, head, _changes(base, head)).issues}
    assert "protected_rule_retired" in codes


def test_edit_same_meaning_keeps_footer_and_id():
    base = _apply(_tree(), Op("add", PAGE, "DEMO-2a", _rule("DEMO-2a")))
    head = _apply(base, Op("edit_same_meaning", PAGE, "DEMO-2a", _rule("DEMO-2a", extra="(clarified)")))
    assert Page.parse(head[PAGE]).rule("DEMO-2a").footer.since == REL
    result = check_changeset(base, head, _changes(base, head))
    assert result.ok and [b.op for b in result.blocks] == ["edit"]


@pytest.mark.parametrize("mutate,code", [
    (lambda h: h.__setitem__(PAGE, h[PAGE].replace('sources: ["PR #10"]', 'sources: ["PR #99"]')), "sources_mismatch"),
    (lambda h: h.__setitem__(PAGE, h[PAGE].replace("tags: [demo]", "tags: [other]")), "frontmatter_changed"),
    (lambda h: h.__setitem__(PAGE, h[PAGE].replace(_rule("DEMO-1a"), "")), "active_rule_removed"),
    (lambda h: h.__setitem__(PAGE, h[PAGE] + "\n" + _rule("DEMO-1b")), "duplicate_rule_id"),
])
def test_forged_changes_are_issues(mutate, code):
    base = _tree()
    head = dict(base)
    mutate(head)
    assert code in {i.code for i in check_changeset(base, head, _changes(base, head)).issues}


@pytest.mark.parametrize("path,mode,code", [
    ("knowledge/skills/x/run.py", "100644", "path_not_whitelisted"),
    ("knowledge/tools/check.py", "100644", "path_not_whitelisted"),
    ("knowledge/AGENTS.md", "100644", "path_not_whitelisted"),
    ("src/infermatrix_copilot/x.py", "100644", "path_not_whitelisted"),
    ("knowledge/repos/demo/core/rules.md", "100755", "mode_not_regular"),
    ("knowledge/repos/demo/core/link.md", "120000", "mode_not_regular"),
])
def test_whitelist_and_modes(path, mode, code):
    result = check_changeset({}, {}, [Change(path, "M", "100644", mode)])
    assert code in {i.code for i in result.issues}


def test_prose_changes_become_prose_blocks():
    base = _tree()
    head = dict(base)
    head[PAGE] = head[PAGE].replace("# Demo core rules\n\n", "# Demo core rules\n\nNew intro.\n\n")
    head["repos/demo/_routes.yaml"] = "schema_version: 1\nowners: []\n"
    result = check_changeset(base, head, _changes(base, head))
    assert result.ok, result.issues
    assert {(b.kind, b.path) for b in result.blocks} == {("prose", PAGE), ("prose", "repos/demo/_routes.yaml")}


def test_external_references_are_reported_for_companion_prs():
    base = _tree()
    head = _apply(base, Op("retire", PAGE, "DEMO-1a", reason="incorrect", evidence="PR #14"))
    result = check_changeset(base, head, _changes(base, head),
                             external_texts={"skills/x/SKILL.md": "apply DEMO-1a first"})
    assert result.external_refs == (("DEMO-1a", "skills/x/SKILL.md"),)


def test_shipped_tree_has_no_tree_level_issues():
    root = Path(__file__).resolve().parents[1] / "knowledge"
    files = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
             for p in root.rglob("*") if p.is_file() and p.suffix in {".md", ".yaml"}}
    assert check_tree(files) == []


def _copy_real_tree(tmp_path):
    """knowledge/ + doc/knowledge/ in a fresh git repo: the validators follow
    links into doc/ and ask git about local/."""
    import shutil
    import subprocess
    repo = Path(__file__).resolve().parents[1]
    dst = tmp_path / "repo"
    shutil.copytree(repo / "knowledge", dst / "knowledge",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(repo / "doc" / "knowledge", dst / "doc" / "knowledge")
    subprocess.run(["git", "init", "-q", str(dst)], check=True)
    return dst / "knowledge"


def _load(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
            for p in root.rglob("*") if p.is_file() and p.suffix in {".md", ".yaml"}
            and "tools" not in p.relative_to(root).parts}


def test_real_tree_operations_pass_both_validators(tmp_path):
    from infermatrix_copilot.knowledge_service.validation import validate_workspace

    root = _copy_real_tree(tmp_path)
    files = _load(root)
    page = "repos/vllm-omni/components/scheduler/rules.md"
    first = Page.parse(files[page]).rules()[0].rule_id
    second = Page.parse(files[page]).rules()[1].rule_id
    new_page = "repos/vllm-omni/components/scheduler/rules-kb-test.md"
    ops = [
        Op("add", new_page, "SCHEDKB-1a", _rule("SCHEDKB-1a", cite="PR #8201"), page_title="Scheduler KB test"),
        Op("retire", page, second, reason="incorrect", evidence="PR #8202"),
        Op("replace", page, first, _rule("SCHEDKB-1b", cite="PR #8203"),
           new_rule_id="SCHEDKB-1b", new_page=new_page, evidence="PR #8203"),
    ]
    # The shipped tree advances independently of the synthetic fixtures.
    today = max(TODAY, Page.parse(files[page]).frontmatter_field("updated"))
    result = apply_operations(files, ops, release="v0.31.0", today=today)
    head = {**files, **result.files}
    # active pages still citing the retired IDs are flagged for the change set
    # to fix; the point here is that both validators accept the written tree
    for path, text in result.files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    changes = _changes(files, head)
    check = check_changeset(files, head, changes)
    codes = {i.code for i in check.issues}
    assert codes <= {"dangling_reference"}, check.issues
    report = validate_workspace(root.parent)
    assert len(report.results) == 2
    assert report.passed, report.problems()


def test_full_page_is_refused_with_a_split_hint():
    base = _tree()
    base[PAGE] = base[PAGE] + "\n" + ("filler line\n" * 3000)
    with pytest.raises(LifecycleError, match="page full"):
        apply_operations(base, [Op("add", PAGE, "DEMO-9a", _rule("DEMO-9a"))], release=REL, today=TODAY)


def test_retired_rules_are_not_served(tmp_path, monkeypatch):
    from infermatrix_copilot.adapters.base import render_briefing_docs
    from infermatrix_copilot.knowledge_docs import KnowledgeDocs
    from infermatrix_copilot.knowledge_service.lifecycle import visible_text

    base = _tree()
    head = _apply(base, Op("retire", PAGE, "DEMO-1a", reason="incorrect", evidence="PR #15"))
    served = visible_text(head[PAGE])
    assert "DEMO-1a" not in served and "DEMO-1b" in served
    assert visible_text(base[PAGE]) == base[PAGE]
    root = tmp_path / "k"
    for path, text in head.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    docs = KnowledgeDocs(root, "repos/demo")
    assert "DEMO-1a" not in docs.read(PAGE)["content"]
    assert not [h for h in docs.search("DEMO-1a") if h["path"] == PAGE]
    assert "DEMO-1a" not in render_briefing_docs(root, [PAGE])


def _nested_rule(rule_id: str, nested: str) -> str:
    return _rule(rule_id).rstrip("\n") + f"\n\n### {nested} — nested detail\n\n- 细节：保持同一队列约束。\n"


def test_review_regressions_page_type_protection_chain_nested_index():
    # 1. leaving the lifecycle by changing page type
    base = _tree()
    head = dict(base)
    head[PAGE] = base[PAGE].replace("type: rule", "type: guide").replace(_rule("DEMO-1a"), "")
    codes = {i.code for i in check_changeset(base, head, _changes(base, head)).issues}
    assert {"page_type_changed", "active_rule_removed"} <= codes
    # 2. dropping protected=true while retiring
    base = _tree()
    page = Page.parse(base[PAGE])
    base[PAGE] = page.replace_section("DEMO-1a", page.rule("DEMO-1a").with_footer(Footer(protected=True))).render()
    head = dict(base)
    page = Page.parse(base[PAGE])
    head[PAGE] = page.replace_section("DEMO-1a", page.rule("DEMO-1a").with_footer(Footer(
        status="retired", retired_at=REL, reason="incorrect", evidence="x"))).render()
    codes = {i.code for i in check_changeset(base, head, _changes(base, head)).issues}
    assert {"protected_rule_retired", "protection_removed"} <= codes
    # 3. A -> B -> C keeps B's backlink
    base = _tree()
    b = _apply(base, Op("replace", PAGE, "DEMO-1a", _rule("DEMO-1c"), new_rule_id="DEMO-1c", evidence="PR #1"))
    c = _apply(b, Op("replace", PAGE, "DEMO-1c", _rule("DEMO-1d"), new_rule_id="DEMO-1d", evidence="PR #2"))
    assert Page.parse(c[PAGE]).rule("DEMO-1c").footer.supersedes == "DEMO-1a"
    assert check_tree(c) == []
    # 4. nested IDs retire and purge with their parent
    base = _tree()
    base[PAGE] = base[PAGE] + "\n" + _nested_rule("DEMO-5a", "DEMO-5a1")
    base["repos/demo/guide.md"] += "Also DEMO-5a1.\n"
    retired = _apply(base, Op("retire", PAGE, "DEMO-5a", reason="incorrect", evidence="PR #3"))
    result = check_changeset(base, retired, _changes(base, retired))
    assert "DEMO-5a1" in result.retired
    assert ("dangling_reference", "repos/demo/guide.md") in {(i.code, i.path) for i in result.issues}
    purged = {**retired, **apply_operations(retired, [Op("purge", PAGE, "DEMO-5a")],
                                            release="v0.32.0", today=TODAY).files}
    purged["repos/demo/guide.md"] = "# guide\n"
    retired["repos/demo/guide.md"] = "# guide\n"
    result = check_changeset(retired, purged, _changes(retired, purged))
    assert result.ok, result.issues
    assert set(result.purged) == {"DEMO-5a", "DEMO-5a1"}
    # 5. a new page without its index line
    base = _tree()
    head = _apply(base, Op("add", "repos/demo/core/rules-x.md", "DEMO-6a", _rule("DEMO-6a"), page_title="X"))
    head["repos/demo/core/_index.md"] = base["repos/demo/core/_index.md"]
    codes = {i.code for i in check_changeset(base, head, _changes(base, head)).issues}
    assert "index_missing" in codes


def test_review_regressions_nested_edit_purge_timing_replace_nested():
    base = _tree()
    base[PAGE] = base[PAGE] + "\n" + _nested_rule("DEMO-7a", "DEMO-7a1")
    head = dict(base)
    head[PAGE] = base[PAGE].replace("\n### DEMO-7a1 — nested detail\n\n- 细节：保持同一队列约束。\n", "\n")
    codes = {i.code for i in check_changeset(base, head, _changes(base, head)).issues}
    assert "nested_rule_ids_changed" in codes
    retired = _apply(_tree(), Op("retire", PAGE, "DEMO-1a", reason="incorrect", evidence="PR #4"))
    forged = {**retired, **apply_operations(retired, [Op("purge", PAGE, "DEMO-1a")],
                                            release="v0.32.0", today=TODAY).files}
    forged["repos/demo/_tombstones.yaml"] = forged["repos/demo/_tombstones.yaml"].replace("v0.32.0", REL)
    codes = {i.code for i in check_changeset(retired, forged, _changes(retired, forged)).issues}
    assert "purge_too_early" in codes
    ok = {**retired, **apply_operations(retired, [Op("purge", PAGE, "DEMO-1a")],
                                        release="v0.32.0", today=TODAY).files}
    assert "purge_release_mismatch" in {i.code for i in check_changeset(
        retired, ok, _changes(retired, ok), release="v0.33.0").issues}
    assert check_changeset(retired, ok, _changes(retired, ok), release="v0.32.0").ok
    with pytest.raises(LifecycleError, match="already exists"):
        apply_operations(_tree(), [Op("replace", PAGE, "DEMO-1a", _nested_rule("DEMO-1e", "DEMO-1b"),
                                      new_rule_id="DEMO-1e", evidence="PR #5")], release=REL, today=TODAY)


def test_level_three_rules_under_topic_headings_are_rules(tmp_path):
    root = Path(__file__).resolve().parents[1] / "knowledge"
    page = "repos/vllm-omni/components/serving/rules-engine-lifecycle.md"
    files = {page: (root / page).read_text(encoding="utf-8")}
    parsed = Page.parse(files[page])
    assert "SERV-5r" in [s.rule_id for s in parsed.rules()]
    assert parsed.rule("SERV-5r").level == 3
    result = apply_operations(files, [Op("retire", page, "SERV-5r", reason="incorrect", evidence="PR #9")],
                              release=REL, today=TODAY)
    head = {**files, **result.files}
    assert Page.parse(head[page]).rule("SERV-5r").footer.status == "retired"
    from infermatrix_copilot.knowledge_service.lifecycle import visible_text
    assert "### SERV-5r —" not in visible_text(head[page])
    assert "### SERV-5a —" in visible_text(head[page])
    deleted = dict(files)
    deleted[page] = Page.parse(files[page]).replace_section("SERV-5r", None).render()
    codes = {i.code for i in check_changeset(files, deleted, _changes(files, deleted)).issues}
    assert "active_rule_removed" in codes


def test_new_page_link_required_even_when_index_is_prose():
    base = _tree()
    head = _apply(base, Op("add", "repos/demo/core/rules-y.md", "DEMO-8a", _rule("DEMO-8a"), page_title="Y"))
    head["repos/demo/core/_index.md"] = "# core, rewritten\n"
    codes = {i.code for i in check_changeset(base, head, _changes(base, head)).issues}
    assert "index_missing" in codes


def test_level_three_rule_ends_at_its_sibling_heading():
    text = (
        "---\ntitle: \"t\"\ncreated: 2026-09-01\nupdated: 2026-09-01\ntype: rule\ntags: []\nsources: []\n---\n\n"
        "# t\n\n## Topic\n\n" + _rule("DEMO-1a").replace("## ", "### ", 1)
        + "\n### General guidance\n\nkeep this visible\n\n## DEMO-2a — top rule here\n\n"
        "- body line one.\n\n### DEMO-2a1 — nested stays with its parent\n\n- nested.\n"
    )
    page = Page.parse(text)
    assert page.render() == text
    assert "General guidance" not in page.rule("DEMO-1a").text
    assert "DEMO-2a1" in page.rule("DEMO-2a").nested_rule_ids
    files = {PAGE: text, "repos/demo/core/_index.md": "# core\n"}
    head = apply_operations(files, [Op("retire", PAGE, "DEMO-1a", reason="incorrect", evidence="x")],
                            release=REL, today=TODAY).files[PAGE]
    from infermatrix_copilot.knowledge_service.lifecycle import visible_text
    served = visible_text(head)
    assert "keep this visible" in served and "### DEMO-1a —" not in served


def test_new_metadata_files_need_no_index_line():
    base = _tree()
    head = dict(base)
    head["repos/demo/_routes.yaml"] = "schema_version: 1\nowners: []\n"
    head["repos/demo/_index.md"] = "# demo, retitled\n"
    codes = {i.code for i in check_changeset(base, head, _changes(base, head)).issues}
    assert "index_missing" not in codes
