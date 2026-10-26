"""Pure rule placement for initialization, independent of its execution state.

Callers resolve stage-specific owners and titles before entering this module.
Results describe content and bookkeeping changes; callers own checkpoints,
evidence review, model budgets and publication.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Collection, Mapping, Sequence

from ..knowledge_service.lifecycle import LifecycleError
from ..knowledge_service.ops import (
    INDEX_NAME, KnowledgeOperation, apply_operations, index_line, page_over_capacity,
)
from .init_quick_maps import (
    has_hand_written_map, map_inputs, render_quick_map, with_quick_map,
)


@dataclass(frozen=True)
class PlacementResult:
    files: dict[str, str] | None
    page: str
    titles: dict[str, str]
    spills: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    reason: str = ""


def page_frontmatter(title: str, *, kind: str, today: str, tags: Sequence[str]) -> str:
    return (f'---\ntitle: "{title}"\ncreated: {today}\nupdated: {today}\ntype: {kind}\n'
            f"tags: [{', '.join(tags)}]\nsources: []\n---\n\n# {title}\n\n")


def overflow_page(page: str, *, base_paths: Collection[str], title: str) -> tuple[str, str]:
    """Choose the first init-owned sibling; pre-existing base pages stay reserved."""
    path = PurePosixPath(page)
    stem = "rules-doc-invariants" if path.name == "rules.md" else path.stem
    for n in range(1, 10):
        name = f"{stem}.md" if n == 1 and path.name == "rules.md" else f"{stem}-{n + 1}.md"
        sibling = str(path.with_name(name))
        if sibling not in base_paths:
            return sibling, f"{title} ({n + 1})"
    raise LifecycleError(f"no free sibling page for {page}")


def check_capacity_with_map(files: Mapping[str, str], pages: Collection[str], *,
                            routes_text: str | None, include_quickmaps: bool, quick_map_policy_version: int = 1) -> None:
    """Measure the map that will be rendered; manifest-only owners have no map."""
    if not include_quickmaps:
        return
    for page in sorted(pages):
        text = files.get(page)
        if text is None or has_hand_written_map(text):
            continue
        signals, prefixes = map_inputs(routes_text, page)
        with_map = with_quick_map(text, render_quick_map(text, signals=signals, prefixes=prefixes,
                                                        policy_version=quick_map_policy_version))
        over = page_over_capacity(with_map)
        if over:
            raise LifecycleError(f"{page}: page full once its Direct quick map is counted ({over})")


def apply_rules(files: Mapping[str, str], operations: Sequence[KnowledgeOperation], *,
                titles: Mapping[str, str], tags: Sequence[str], today: str, release: str,
                routes_text: str | None, include_quickmaps: bool, quick_map_policy_version: int = 1) -> dict[str, str]:
    """Add resolved rules, creating linked shells first, without mutating inputs."""
    work = dict(files)
    for page in dict.fromkeys(op.page for op in operations):
        if page in work:
            continue
        index = str(PurePosixPath(page).with_name(INDEX_NAME))
        if index not in work:
            # The stage may replace this stand-in with its real entry page later.
            work[index] = page_frontmatter("index", kind="index", today=today, tags=tags)
        title = titles[page]
        work[page] = page_frontmatter(title, kind="rule", today=today, tags=tags)
        work[index] = work[index].rstrip("\n") + "\n" + index_line(page, title)
    result = apply_operations(work, operations, release=release, today=today)
    work.update(result.files)
    check_capacity_with_map(work, {op.page for op in operations}, routes_text=routes_text,
                            include_quickmaps=include_quickmaps,
                            quick_map_policy_version=quick_map_policy_version)
    return work


def place_rule(files: Mapping[str, str], operation: KnowledgeOperation, *,
               base_paths: Collection[str], titles: Mapping[str, str], title: str,
               tags: Sequence[str], today: str, release: str,
               routes_text: str | None, include_quickmaps: bool, quick_map_policy_version: int = 1) -> PlacementResult:
    """Place one rule against the running tree, returning all bookkeeping deltas."""
    placed = operation
    new_titles: dict[str, str] = {}
    spills: dict[str, list[str]] = {}
    notes: list[str] = []

    def result(work=None, reason=""):
        return PlacementResult(work, placed.page, new_titles, spills, notes, reason)

    for _ in range(10):
        page_titles = {**titles, **new_titles}
        page_titles.setdefault(placed.page, title)
        try:
            return result(apply_rules(files, [placed], titles=page_titles, tags=tags, today=today,
                                      release=release, routes_text=routes_text,
                                      include_quickmaps=include_quickmaps,
                                      quick_map_policy_version=quick_map_policy_version))
        except LifecycleError as exc:
            if "page full" not in str(exc):
                return result(reason=f"refused by the knowledge format: {exc}")
            previous = placed.page
            try:
                page, sibling_title = overflow_page(previous, base_paths=base_paths,
                                                     title=page_titles[previous])
            except LifecycleError as full:
                return result(reason=str(full))
            if page not in titles:
                new_titles.setdefault(page, sibling_title)
            placed = replace(placed, page=page)
            if placed.page == previous:
                return result(reason=f"refused by the knowledge format: {exc}")
            spilled = spills.setdefault(previous, [])
            if placed.page not in spilled:
                spilled.append(placed.page)
            notes.append(f"{previous} is full: {placed.rule_id} goes to {placed.page}")
    return result(reason="no page could take it")
