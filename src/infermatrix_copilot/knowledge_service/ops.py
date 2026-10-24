"""Knowledge Ops API 2.0: typed changes to rule pages, with the lifecycle.

The v1 curation path (``KnowledgeCurator``) can only append a section. The
knowledge service needs every change a maintainer makes by hand, expressed as a
typed operation the quality gate can judge one by one:

====================  =========================================================
``add``               append a new rule (optionally on a new ``rules-*.md`` page)
``edit_same_meaning`` reword an active rule under the same ID (gate must confirm
                      the meaning is unchanged)
``replace``           retire an active rule as ``superseded`` and add its
                      successor under a NEW ID, atomically
``retire``            retire an active rule (upstream removed it, it was wrong,
                      or it duplicates another); its text stays for one cycle
``purge``             delete a retired rule's text and tombstone its ID forever
====================  =========================================================

``apply_operations`` is a pure function over a mapping of knowledge-relative
paths to text, so the service, the gate and the verifier all compute exactly
the same result. After the operations it writes the MECHANICAL consequences the
verifier later recomputes: ``updated:``, ``sources:`` (append-only), an index
line for every new page, and tombstones for purged IDs.

Standard library + PyYAML + ``lifecycle`` only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable, Mapping

import yaml

from .format import (
    RULE_PAGE as _RULE_PAGE, MAX_SECTION_CHARS as _MAX_SECTION_CHARS,
    PAGE_MAX_BYTES, PAGE_MAX_LINES, page_over_capacity,
)

from .lifecycle import (
    RETIRE_REASONS, Footer, LifecycleError, Page, Section, expected_sources,
    first_taken_rule_id, rule_heading_ids,
)

KNOWLEDGE_OPS_API_VERSION = "2.0.0"
OP_KINDS = ("add", "edit_same_meaning", "replace", "retire", "purge")
TOMBSTONES_NAME = "_tombstones.yaml"
INDEX_NAME = "_index.md"

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class KnowledgeOperation:
    kind: str
    page: str
    rule_id: str
    section_markdown: str = ""
    new_rule_id: str = ""
    new_page: str = ""
    reason: str = ""
    evidence: str = ""
    page_title: str = ""
    allow_protected: bool = False

    def to_dict(self) -> dict:
        return {key: value for key, value in self.__dict__.items() if value not in ("", False)}

    @classmethod
    def from_dict(cls, data: Mapping) -> "KnowledgeOperation":
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise LifecycleError(f"unknown operation fields: {sorted(unknown)}")
        return cls(**{key: data[key] for key in data})


@dataclass(frozen=True)
class OperationsResult:
    files: dict[str, str]            # every created or changed path -> new text
    touched_rules: tuple[str, ...]   # rule IDs whose sections changed
    created_pages: tuple[str, ...] = field(default_factory=tuple)


def model_operations(data: dict, *, kinds: tuple[str, ...], limit: int | None = None,
                     strings: bool = False, kind_error: str = "") -> list[KnowledgeOperation]:
    """Parse model operations without granting protected-rule or publication rights."""
    items = data.get("operations")
    if not isinstance(items, list) or (limit is not None and len(items) > limit):
        raise ValueError("operations must be a list" + (f" of at most {limit}" if limit is not None else ""))
    result = []
    for item in items:
        if not isinstance(item, dict) or item.get("kind") not in kinds:
            raise ValueError(kind_error or f"operation kind must be one of {kinds}")
        if item.get("allow_protected"):
            raise ValueError("the generator may not request the human path")
        if strings:
            if not item.get("page") or not item.get("rule_id"):
                raise ValueError("every operation needs page and rule_id")
            for key in KnowledgeOperation.__dataclass_fields__:
                if key not in ("kind", "allow_protected") and key in item and not isinstance(item[key], str):
                    raise ValueError(f"{key} must be a string")
        result.append(KnowledgeOperation.from_dict(item))
    return result


def repo_scope(page: str) -> str:
    """``repos/<repo>`` or ``general`` for a knowledge-relative page path."""
    parts = PurePosixPath(page).parts
    if len(parts) >= 2 and parts[0] == "repos":
        return f"repos/{parts[1]}"
    if parts and parts[0] == "general":
        return "general"
    raise LifecycleError(f"page is outside repos/<repo>/ and general/: {page}")


def tombstones_path(page: str) -> str:
    return f"{repo_scope(page)}/{TOMBSTONES_NAME}"


def load_tombstones(text: str | None) -> list[dict]:
    if not text:
        return []
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise LifecycleError("tombstones must be schema_version 1")
    items = data.get("ids") or []
    if not isinstance(items, list) or any(
        not isinstance(item, dict) or not item.get("id") for item in items
    ):
        raise LifecycleError("tombstones ids must be a list of {id, ...}")
    return items


def render_tombstones(items: list[dict]) -> str:
    lines = [
        "# IDs of purged knowledge rules. An ID listed here is never reused.",
        "# Written by the knowledge service; recomputed by the publisher's local gate.",
        "schema_version: 1",
        "ids:",
    ]
    for item in items:
        lines.append(
            f"  - {{id: {item['id']}, purged_at: {item.get('purged_at', '')}, "
            f"page: {item.get('page', '')}}}"
        )
    return "\n".join(lines) + "\n"


def all_rule_ids(files: Mapping[str, str]) -> dict[str, int]:
    """Heading IDs at any level across every rule page, with their counts."""
    counts: dict[str, int] = {}
    for path, text in files.items():
        if not path.endswith(".md"):
            continue
        for rule_id in rule_heading_ids(_without_fences(text)):
            counts[rule_id] = counts.get(rule_id, 0) + 1
    return counts


def all_tombstoned_ids(files: Mapping[str, str]) -> set[str]:
    out: set[str] = set()
    for path, text in files.items():
        if PurePosixPath(path).name == TOMBSTONES_NAME:
            out.update(str(item["id"]) for item in load_tombstones(text))
    return out


def _without_fences(text: str) -> str:
    out, fence = [], None
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else (fence or marker)
            out.append("\n")
            continue
        out.append("\n" if fence else line)
    return "".join(out)


def _check_section(section_markdown: str, rule_id: str, level: int = 2) -> Section:
    text = section_markdown.strip("\n") + "\n"
    section = Section(text)
    marks = "#" * level
    if section.rule_id != rule_id or section.level != level:
        raise LifecycleError(f"section heading must be '{marks} {rule_id} — <title>'")
    if level == 2 and len(re.findall(r"(?m)^## ", text)) != 1:
        raise LifecycleError("a rule section has exactly one level-two heading")
    if level == 3 and (re.search(r"(?m)^## ", text) or len(
            [m for m in re.finditer(r"(?m)^### ", text)]) != 1):
        raise LifecycleError("a level-three rule section has exactly one heading and no ## heading")
    if section.has_footer:
        raise LifecycleError("the service writes lifecycle footers; do not include one")
    if not 80 <= len(text) <= _MAX_SECTION_CHARS:
        raise LifecycleError("section length is outside 80..16384 characters")
    return section


def _new_page_text(page: str, title: str, today: str, template: Page | None) -> str:
    tags = "[]"
    if template is not None and template.frontmatter_field("tags") is not None:
        tags = template.frontmatter_field("tags")
    if not title or '"' in title:
        raise LifecycleError("a new page needs a title without quotes")
    return (
        "---\n"
        f'title: "{title}"\n'
        f"created: {today}\n"
        f"updated: {today}\n"
        "type: rule\n"
        f"tags: {tags}\n"
        "sources: []\n"
        "---\n\n"
        f"# {title}\n\n"
    )


def index_line(page: str, title: str) -> str:
    return f"- [{title}]({PurePosixPath(page).name})\n"


def apply_operations(
    files: Mapping[str, str],
    operations: Iterable[KnowledgeOperation],
    *,
    release: str,
    today: str,
) -> OperationsResult:
    """Apply typed operations to ``files`` (knowledge-relative path -> text).

    Raises ``LifecycleError`` on the first invalid operation; nothing is
    partially applied because the input mapping is never mutated.
    """
    if not _DATE.fullmatch(today):
        raise LifecycleError("today must be YYYY-MM-DD")
    if not release or re.search(r'[\s"]', release):
        raise LifecycleError("release must be a non-empty token")
    work: dict[str, str] = dict(files)
    original_ids = all_rule_ids(files)
    tombstoned = all_tombstoned_ids(files)
    added_ids: set[str] = set()
    touched_pages: dict[str, list[str]] = {}  # page -> sources before any change
    created: list[str] = []
    touched_rules: list[str] = []
    purged: dict[str, list[dict]] = {}

    def page_of(path: str, *, create_title: str = "") -> Page:
        if not _RULE_PAGE.fullmatch(PurePosixPath(path).name):
            raise LifecycleError(f"not a rule page (rules.md / rules-<topic>.md): {path}")
        repo_scope(path)
        text = work.get(path)
        if text is None:
            if not create_title:
                raise LifecycleError(f"page does not exist: {path}")
            sibling = str(PurePosixPath(path).with_name("rules.md"))
            template = Page.parse(work[sibling]) if sibling in work else None
            index = str(PurePosixPath(path).with_name(INDEX_NAME))
            if index not in work:
                raise LifecycleError(f"a new page's directory needs an {INDEX_NAME}: {index}")
            text = _new_page_text(path, create_title, today, template)
            work[path] = text
            created.append(path)
            work[index] = work[index].rstrip("\n") + "\n" + index_line(path, create_title)
        page = Page.parse(text)
        if page.frontmatter_field("type") != "rule":
            raise LifecycleError(f"page is not type: rule: {path}")
        touched_pages.setdefault(path, page.sources() if path not in created else [])
        return page

    def active(page: Page, rule_id: str, op: KnowledgeOperation) -> Section:
        section = page.rule(rule_id)
        footer = section.footer
        if footer.status != "active":
            raise LifecycleError(f"rule {rule_id} is not active")
        if footer.protected and not op.allow_protected:
            raise LifecycleError(f"rule {rule_id} is protected; only the human path may change it")
        return section

    def fresh_section(markdown: str, rule_id: str) -> Section:
        section = _check_section(markdown, rule_id)
        ids = (rule_id, *(nested for nested in section.nested_rule_ids if nested != rule_id))
        taken = first_taken_rule_id(ids, original_ids, added_ids, tombstoned)
        if taken:
            reason = "was purged and is reserved forever" if taken in tombstoned else "already exists in the tree"
            raise LifecycleError(f"rule ID {reason}: {taken}")
        added_ids.update(ids)
        return section

    def retire(page: Page, old: Section, op: KnowledgeOperation, reason: str, successor: str = "") -> None:
        footer = old.footer
        retired = old.with_footer(Footer(
            status="retired", since=footer.since, retired_at=release,
            reason=reason, evidence=op.evidence,
            supersedes=footer.supersedes,  # keep the chain A -> B -> C
            superseded_by=successor, protected=footer.protected,
        ))
        work[op.page] = page.replace_section(op.rule_id, retired).render()

    for op in operations:
        if op.kind not in OP_KINDS:
            raise LifecycleError(f"unknown operation kind: {op.kind}")
        if op.kind == "add":
            section = fresh_section(op.section_markdown, op.rule_id)
            page = page_of(op.page, create_title=op.page_title)
            page = page.append_section(section.with_footer(Footer(status="active", since=release)))
            work[op.page] = page.render()
            touched_rules.append(op.rule_id)
        elif op.kind == "edit_same_meaning":
            page = page_of(op.page)
            old = active(page, op.rule_id, op)
            section = _check_section(op.section_markdown, op.rule_id, old.level)
            if set(section.nested_rule_ids) != set(old.nested_rule_ids):
                raise LifecycleError("an edit may not add or drop nested rule IDs")
            trailing = old.text[len(old.text.rstrip("\n")):]
            new = Section(section.text.rstrip("\n") + (trailing or "\n"))
            if old.has_footer:
                new = new.with_footer(old.footer)
            work[op.page] = page.replace_section(op.rule_id, new).render()
            touched_rules.append(op.rule_id)
        elif op.kind == "replace":
            if not op.new_rule_id or op.new_rule_id == op.rule_id:
                raise LifecycleError("replace needs a different new_rule_id")
            if not op.evidence:
                raise LifecycleError("replace needs evidence")
            page = page_of(op.page)
            old = active(page, op.rule_id, op)
            section = fresh_section(op.section_markdown, op.new_rule_id)
            retire(page, old, op, "superseded", op.new_rule_id)
            target = op.new_page or op.page
            target_page = page_of(target, create_title=op.page_title)
            successor = section.with_footer(Footer(
                status="active", since=release, supersedes=op.rule_id))
            work[target] = target_page.append_section(successor).render()
            touched_rules += [op.rule_id, op.new_rule_id]
        elif op.kind == "retire":
            if op.reason not in RETIRE_REASONS or op.reason == "superseded":
                raise LifecycleError("retire reason must be upstream-removed, incorrect or duplicate; use replace for superseded")
            if not op.evidence:
                raise LifecycleError("retire needs evidence")
            page = page_of(op.page)
            old = active(page, op.rule_id, op)
            retire(page, old, op, op.reason)
            touched_rules.append(op.rule_id)
        elif op.kind == "purge":
            page = page_of(op.page)
            old = page.rule(op.rule_id)
            if old.footer.status != "retired":
                raise LifecycleError(f"only a retired rule can be purged: {op.rule_id}")
            if old.footer.protected and not op.allow_protected:
                raise LifecycleError(f"rule {op.rule_id} is protected; only the human path may purge it")
            if old.footer.retired_at == release:
                raise LifecycleError("a rule is purged one full release after it was retired")
            work[op.page] = page.replace_section(op.rule_id, None).render()
            for rid in dict.fromkeys((op.rule_id, *old.nested_rule_ids)):
                purged.setdefault(tombstones_path(op.page), []).append(
                    {"id": rid, "purged_at": release, "page": op.page})
            touched_rules.append(op.rule_id)

    for path, before_sources in touched_pages.items():
        page = Page.parse(work[path])
        page = page.with_frontmatter_field("updated", today)
        page = page.with_sources(expected_sources(before_sources, page))
        work[path] = page.render()
    for path, entries in purged.items():
        work[path] = render_tombstones(load_tombstones(work.get(path)) + entries)
    for path in touched_pages:
        over = page_over_capacity(work[path])
        if over:
            raise LifecycleError(f"page full, add to a new rules-<topic>.md page instead: {path}: {over}")

    changed = {path: text for path, text in work.items() if files.get(path) != text}
    return OperationsResult(
        files=changed,
        touched_rules=tuple(dict.fromkeys(touched_rules)),
        created_pages=tuple(created),
    )
