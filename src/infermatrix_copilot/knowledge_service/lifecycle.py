"""Rule pages as data: sections, lifecycle footers, and byte-preserving render.

A rule page is frontmatter, a head (title + preamble), then sections. A section
starts at every ``## `` heading, and also at a ``### <ID> — <title>`` heading
whose enclosing ``## `` section is not itself a rule (owner pages group rules
under topic headings). A section whose heading is ``## <ID> — <title>`` or
``### <ID> — <title>`` is a rule; ``###`` rules INSIDE a ``##`` rule stay part
of it as nested rules. Each rule may end
with one machine-readable footer, an HTML comment that renders invisibly::

    <!-- kb:rule status=retired since=v0.30.0 retired_at=v0.31.0 reason=upstream-removed evidence="PR #8201" superseded_by=BENCH-1c -->

No footer means ``status=active``, which is every rule written before the
lifecycle existed. Parsing then rendering an untouched page returns its exact
bytes, so an operation changes only the sections it targets.

This module needs only the standard library and PyYAML and imports nothing
else except the shared format contract from the package.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Iterable

import yaml

from .format import (
    APPEND_HEADING as _TOP_RULE, RULE_HEADING, ANY_RULE_HEADING, FRONTMATTER, frontmatter_value,
)

DEPTH_FACETS = ("flow", "api", "configuration", "dependencies", "failure_modes", "tradeoffs", "validation")
DEPTH_ABSENCE_DETECTOR = "static-test-association-v2"
DEPTH_BLOCK = re.compile(r"<!-- kb:depth feature=([a-z0-9-]+) facet=([a-z_]+) pin=((?:[0-9a-f]{40}|[0-9a-f]{64})) "
                         r"sha256=([0-9a-f]{64}) -->\n(.*?)\n<!-- /kb:depth -->", re.S)


def safe_source_path(path: object) -> bool:
    """Repository-relative source paths and globs, never absolute or traversing."""
    return isinstance(path, str) and bool(path) and not path.startswith("/") \
        and "\\" not in path and ":" not in path \
        and all(part not in ("", ".", "..") for part in path.split("/"))


def depth_proof_basis(proof: dict, *, facet: str, pin: str) -> str:
    """Validate stored evidence kinds; source replay belongs to the depth auditor."""
    if not isinstance(proof, dict):
        raise ValueError("depth proof must be an object")
    basis = proof.get("basis", "supported")
    if basis not in ("supported", "verified_absent"):
        raise ValueError("unknown depth proof basis")
    certificate = proof.get("absence_certificate")
    if basis == "supported":
        if certificate is not None:
            raise ValueError("supported proof cannot contain an absence certificate")
        return basis
    required = {"version", "detector", "feature", "facet", "pin", "policy_sha256",
                "source_scope_sha256", "source_files", "docs_scope_sha256", "test_inventory_sha256", "test_files"}
    if facet != "validation" or not isinstance(certificate, dict) or set(certificate) != required \
            or certificate.get("version") != 1 or isinstance(certificate.get("version"), bool) \
            or certificate.get("facet") != facet or certificate.get("pin") != pin \
            or certificate.get("detector") != DEPTH_ABSENCE_DETECTOR \
            or not isinstance(certificate.get("feature"), str) \
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", certificate["feature"]):
        raise ValueError("invalid validation absence certificate")
    for key in ("policy_sha256", "source_scope_sha256", "docs_scope_sha256", "test_inventory_sha256"):
        if not isinstance(certificate.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", certificate[key]):
            raise ValueError("absence certificate needs bound inventory hashes")
    if any(type(certificate.get(key)) is not int or certificate[key] < 0
           for key in ("source_files", "test_files")) or not certificate["source_files"]:
        raise ValueError("absence certificate needs complete inventory counts")
    return basis


def depth_sections(text: str) -> list[dict]:
    """Read intact depth prose; upstream evidence verification belongs to init/audit."""
    rows, counts = [], {}
    for feature, facet, pin, hashed, body in DEPTH_BLOCK.findall(text):
        key = (feature, facet)
        counts[key] = counts.get(key, 0) + 1
        if facet not in DEPTH_FACETS or hashlib.sha256(body.encode()).hexdigest() != hashed:
            continue
        proof = re.search(r"\n<!-- kb:depth-proof (.*?) -->\s*$", body, re.S)
        try:
            data = json.loads(proof.group(1)) if proof else {}
            evidence = data["evidence"]
            basis = depth_proof_basis(data, facet=facet, pin=pin)
            acceptance_mode = data.get("acceptance_mode", "strict")
            if acceptance_mode not in ("strict", "lightweight") or (
                    acceptance_mode == "lightweight" and basis != "supported"):
                continue
            validation_kind = data.get("validation_kind")
            if validation_kind is not None and (facet != "validation" or validation_kind not in (
                    "automated_runtime", "automated_source_text", "helper_unit", "documented_manual")):
                continue
            if basis == "verified_absent" and data["absence_certificate"]["feature"] != feature:
                continue
            if not isinstance(evidence, list) or not 1 <= len(evidence) <= 4:
                continue
            if any(not isinstance(e, dict) or not safe_source_path(e.get("path"))
                   or not isinstance(e.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", e["sha256"])
                   or not isinstance(e.get("start"), int) or isinstance(e["start"], bool)
                   or not isinstance(e.get("end"), int) or isinstance(e["end"], bool)
                   or not 1 <= e["start"] <= e["end"] for e in evidence):
                continue
        except (ValueError, KeyError, TypeError):
            continue
        rows.append({"feature": feature, "facet": facet, "pin": pin,
                     "content": visible_text(body[:proof.start()]), "evidence": evidence,
                     "basis": basis,
                     "acceptance_mode": acceptance_mode, "validation_kind": validation_kind,
                     "gap_label": "no_statically_associated_test_entry" if basis == "verified_absent" else None})
    return [row for row in rows if counts[(row["feature"], row["facet"])] == 1]

FOOTER = re.compile(r"^<!--\s*kb:rule\s+(?P<attrs>.*?)\s*-->\s*$")
_ATTR = re.compile(r'(?P<key>[a-z_]+)=(?:"(?P<quoted>[^"]*)"|(?P<bare>[^\s"]+))')
CITATION = re.compile(r"\^\[(?P<ref>[^\]\n]{1,60})\]")


def rule_heading_ids(text: str) -> tuple[str, ...]:
    """Raw heading IDs, in order, including repeats and nested headings.

    Callers decide which pages and fenced text belong to their contract.
    """
    return tuple(match.group("rule") for match in ANY_RULE_HEADING.finditer(text))


def repeated_rule_id(text: str) -> str:
    ids = rule_heading_ids(text)
    seen: set[str] = set()
    for rule_id in ids:
        if rule_id in seen:
            return rule_id
        seen.add(rule_id)
    return ""


def first_taken_rule_id(ids, occupied, introduced=(), tombstoned=()) -> str:
    """Check a whole candidate before reserving any of its rule identities."""
    seen: set[str] = set()
    for rule_id in ids:
        if rule_id in occupied or rule_id in introduced or rule_id in tombstoned or rule_id in seen:
            return rule_id
        seen.add(rule_id)
    return ""

STATUSES = ("active", "retired")
FOOTER_KEYS = (
    "status", "since", "retired_at", "reason", "evidence",
    "supersedes", "superseded_by", "protected",
)
RETIRE_REASONS = ("upstream-removed", "superseded", "incorrect", "duplicate")


class LifecycleError(ValueError):
    """A page, footer, or operation violates the rule lifecycle contract."""


@dataclass(frozen=True)
class Footer:
    status: str = "active"
    since: str = ""
    retired_at: str = ""
    reason: str = ""
    evidence: str = ""
    supersedes: str = ""
    superseded_by: str = ""
    protected: bool = False

    @classmethod
    def parse(cls, attrs: str) -> "Footer":
        values: dict[str, str] = {}
        consumed = 0
        for match in _ATTR.finditer(attrs):
            if attrs[consumed:match.start()].strip():
                raise LifecycleError(f"malformed kb:rule footer: {attrs!r}")
            key = match.group("key")
            if key not in FOOTER_KEYS:
                raise LifecycleError(f"unknown kb:rule footer key: {key}")
            if key in values:
                raise LifecycleError(f"repeated kb:rule footer key: {key}")
            values[key] = match.group("quoted") if match.group("quoted") is not None \
                else match.group("bare")
            consumed = match.end()
        if attrs[consumed:].strip():
            raise LifecycleError(f"malformed kb:rule footer: {attrs!r}")
        status = values.get("status", "active")
        if status not in STATUSES:
            raise LifecycleError(f"kb:rule status must be one of {STATUSES}: {status}")
        protected = values.get("protected", "false")
        if protected not in {"true", "false"}:
            raise LifecycleError("kb:rule protected must be true or false")
        footer = cls(
            status=status,
            since=values.get("since", ""),
            retired_at=values.get("retired_at", ""),
            reason=values.get("reason", ""),
            evidence=values.get("evidence", ""),
            supersedes=values.get("supersedes", ""),
            superseded_by=values.get("superseded_by", ""),
            protected=protected == "true",
        )
        footer.check()
        return footer

    def check(self) -> None:
        if self.status == "retired":
            missing = [key for key in ("retired_at", "reason", "evidence")
                       if not getattr(self, key)]
            if missing:
                raise LifecycleError(f"retired rule footer lacks {', '.join(missing)}")
            if self.reason not in RETIRE_REASONS:
                raise LifecycleError(
                    f"retire reason must be one of {RETIRE_REASONS}: {self.reason}")
            if self.reason == "superseded" and not self.superseded_by:
                raise LifecycleError("a superseded rule must name superseded_by")
        elif self.retired_at or self.superseded_by:
            raise LifecycleError("an active rule cannot carry retired_at/superseded_by")

    def render(self) -> str:
        parts = []
        for key in FOOTER_KEYS:
            value = getattr(self, key)
            if key == "protected":
                if value:
                    parts.append("protected=true")
                continue
            if key == "status":
                parts.append(f"status={value}")
                continue
            if not value:
                continue
            if re.search(r'[\s"]', value):
                if '"' in value:
                    raise LifecycleError(f"footer value may not contain a quote: {key}")
                parts.append(f'{key}="{value}"')
            else:
                parts.append(f"{key}={value}")
        return f"<!-- kb:rule {' '.join(parts)} -->"


@dataclass(frozen=True)
class Section:
    """One level-two section, kept as its exact source text."""

    text: str  # includes the heading line and its trailing newline(s)

    @property
    def heading(self) -> str:
        return self.text.split("\n", 1)[0]

    @property
    def rule_id(self) -> str:
        match = RULE_HEADING.match(self.heading)
        return match.group("rule") if match else ""

    @property
    def level(self) -> int:
        match = RULE_HEADING.match(self.heading)
        return len(match.group("level")) if match else 2

    @property
    def is_rule(self) -> bool:
        return bool(self.rule_id)

    def _footer_line(self) -> tuple[int, str] | None:
        lines = self.text.rstrip("\n").split("\n")
        for index in range(len(lines) - 1, 0, -1):
            line = lines[index].strip()
            if not line:
                continue
            return (index, line) if FOOTER.match(line) else None
        return None

    @property
    def footer(self) -> Footer:
        found = self._footer_line()
        if found is None:
            return Footer()
        return Footer.parse(FOOTER.match(found[1]).group("attrs"))

    @property
    def has_footer(self) -> bool:
        return self._footer_line() is not None

    @property
    def body_without_footer(self) -> str:
        """The rule's words: the section minus its footer, trailing blanks trimmed."""
        found = self._footer_line()
        lines = self.text.rstrip("\n").split("\n")
        if found is not None:
            lines = lines[:found[0]]
        return "\n".join(lines).rstrip() + "\n"

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.body_without_footer.encode("utf-8")).hexdigest()

    @property
    def nested_rule_ids(self) -> tuple[str, ...]:
        return rule_heading_ids(self.text)

    @property
    def citations(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(m.group("ref").strip() for m in CITATION.finditer(self.text)))

    def with_footer(self, footer: Footer) -> "Section":
        footer.check()
        trailing = self.text[len(self.text.rstrip("\n")):] or "\n"
        return Section(self.body_without_footer + "\n" + footer.render() + trailing)


@dataclass(frozen=True)
class Page:
    frontmatter: str  # the exact "---...---\n" block, or "" when absent
    head: str         # text between frontmatter and the first "## " section
    sections: tuple[Section, ...] = field(default_factory=tuple)

    @classmethod
    def parse(cls, text: str) -> "Page":
        match = FRONTMATTER.match(text)
        frontmatter = match.group(0) if match else ""
        rest = text[len(frontmatter):]
        chunks = _split_sections(rest)
        head = chunks[0]
        sections = tuple(Section(chunk) for chunk in chunks[1:])
        return cls(frontmatter, head, sections)

    def render(self) -> str:
        return self.frontmatter + self.head + "".join(s.text for s in self.sections)

    # -- rules ---------------------------------------------------------------

    def rules(self) -> tuple[Section, ...]:
        return tuple(s for s in self.sections if s.is_rule)

    def rule(self, rule_id: str) -> Section:
        for section in self.sections:
            if section.rule_id == rule_id:
                return section
        raise LifecycleError(f"rule {rule_id} is not on this page")

    def has_rule(self, rule_id: str) -> bool:
        return any(section.rule_id == rule_id for section in self.sections)

    def replace_section(self, rule_id: str, new: Section | None) -> "Page":
        out = []
        found = False
        for section in self.sections:
            if section.rule_id == rule_id:
                found = True
                if new is not None:
                    out.append(new)
            else:
                out.append(section)
        if not found:
            raise LifecycleError(f"rule {rule_id} is not on this page")
        return replace(self, sections=tuple(out))

    def append_section(self, section: Section) -> "Page":
        sections = list(self.sections)
        if sections and not sections[-1].text.endswith("\n\n"):
            last = sections[-1].text
            sections[-1] = Section(last.rstrip("\n") + "\n\n")
        elif not sections and self.head and not self.head.endswith("\n\n"):
            return replace(self, head=self.head.rstrip("\n") + "\n\n", sections=(section,))
        sections.append(section)
        return replace(self, sections=tuple(sections))

    # -- frontmatter ---------------------------------------------------------

    def frontmatter_field(self, key: str) -> str | None:
        match = re.search(rf"(?m)^{re.escape(key)}:[ \t]*(?P<value>.*)$", self.frontmatter)
        return match.group("value").strip() if match else None

    def with_frontmatter_field(self, key: str, value: str) -> "Page":
        if not self.frontmatter:
            raise LifecycleError("page has no frontmatter")
        pattern = re.compile(rf"(?m)^{re.escape(key)}:[^\r\n]*$")
        if not pattern.search(self.frontmatter):
            raise LifecycleError(f"frontmatter has no {key} field")
        return replace(self, frontmatter=pattern.sub(f"{key}: {value}", self.frontmatter, count=1))

    def frontmatter_data(self) -> dict:
        try:
            data = frontmatter_value(self.frontmatter) or {}
        except yaml.YAMLError as exc:
            raise LifecycleError(f"frontmatter is not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise LifecycleError("frontmatter must be a mapping")
        return data

    def sources(self) -> list[str]:
        value = self.frontmatter_data().get("sources")
        if value is None:
            return []
        if not isinstance(value, list):
            raise LifecycleError("frontmatter sources must be a list")
        return [str(item) for item in value]

    def with_sources(self, items: list[str]) -> "Page":
        """Rewrite ``sources:`` in the page's own style (flow or block list)."""
        match = re.search(
            r"(?m)^sources:[ \t]*(?P<flow>\[.*\])?[ \t]*\r?\n(?P<block>(?:[ \t]+-[^\r\n]*\r?\n)*)",
            self.frontmatter,
        )
        if match is None:
            raise LifecycleError("frontmatter has no sources field")
        if match.group("flow") is not None or not match.group("block"):
            rendered = f"sources: {render_flow_list(items)}\n"
        else:
            indent = re.match(r"[ \t]+", match.group("block")).group(0)
            rendered = "sources:\n" + "".join(
                f'{indent}- "{_quote_free(item)}"\n' for item in items)
        return replace(
            self,
            frontmatter=self.frontmatter[:match.start()] + rendered
            + self.frontmatter[match.end():],
        )


_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def _split_sections(text: str) -> list[str]:
    """Split at ``## `` headings, and at every ``### `` heading whose enclosing
    ``## `` section is not a rule (so a level-three rule ends at its next
    sibling); ``###`` inside a ``##`` rule stays nested. Fences never split."""
    chunks: list[str] = []
    current: list[str] = []
    fence: str | None = None
    top_is_rule = False
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker[0] * len(marker)
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
        split = False
        if fence is None and not match:
            if line.startswith("## "):
                split = True
                top_is_rule = bool(_TOP_RULE.match(line))
            elif line.startswith("### ") and not top_is_rule:
                # every ### under a topic heading is its own section, so a
                # level-three rule ends at its next sibling heading
                split = True
        if split:
            chunks.append("".join(current))
            current = [line]
        else:
            current.append(line)
    chunks.append("".join(current))
    return chunks


def parse_flow_list(raw: str) -> list[str]:
    """Parse the YAML flow sequence the tree uses for ``sources:``/``tags:``."""
    raw = raw.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        raise LifecycleError(f"expected a [flow, list]: {raw!r}")
    inner = raw[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    for match in re.finditer(r'\s*(?:"(?P<q>[^"]*)"|(?P<b>[^,]+?))\s*(?:,|$)', inner):
        value = match.group("q") if match.group("q") is not None else match.group("b")
        if value is not None and value.strip():
            items.append(value.strip())
    return items


def _quote_free(item: str) -> str:
    if '"' in item:
        raise LifecycleError(f"list item may not contain a quote: {item!r}")
    return item


def render_flow_list(items: Iterable[str]) -> str:
    rendered = []
    for item in items:
        if re.fullmatch(r"[A-Za-z0-9_./:@+-]+", item):
            rendered.append(item)
        else:
            if '"' in item:
                raise LifecycleError(f"list item may not contain a quote: {item!r}")
            rendered.append(f'"{item}"')
    return "[" + ", ".join(rendered) + "]"


def expected_sources(before: list[str], page: Page) -> list[str]:
    """Deterministic ``sources:`` after a change: the old list, in order, plus
    every citation an ACTIVE rule on the page makes that is not yet listed, in
    order of first appearance. Nothing is ever removed (history stays cited)."""
    out = list(before)
    for section in page.rules():
        if section.footer.status != "active":
            continue
        for ref in section.citations:
            if ref not in out:
                out.append(ref)
    return out


def visible_text(text: str) -> str:
    """The page as reviewers and agents should see it: retired rules removed.

    A retired rule keeps its text on disk for one release cycle so the
    retirement can be reviewed and reverted, but it must stop being SERVED at
    once. Pages without retired rules are returned unchanged, byte for byte.
    """
    if "kb:rule" not in text:
        return text
    page = Page.parse(text)
    kept = []
    for section in page.sections:
        try:
            retired = section.is_rule and section.footer.status == "retired"
        except LifecycleError:
            retired = False  # L1 rejects malformed footers; never hide on doubt
        if not retired:
            kept.append(section)
    if len(kept) == len(page.sections):
        return text
    return replace(page, sections=tuple(kept)).render()

