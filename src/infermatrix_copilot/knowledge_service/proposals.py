"""Evidence-bound proposal validation and rule identity."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..contracts.models import (
    InvalidRequestError, KnowledgeEvidenceBatch, KnowledgeProposalRejection,
    KnowledgeProposalValidation, KnowledgeRuleProposal, RepositoryRef,
)
from .catalog import _appended_size, catalog_entries, document_path
from .common import (
    _HEADING, _MAX_SECTION_CHARS, _MAX_SOURCE_CHARS,
    _RULE_ID, _sha256,
)
from .prompt import validate_batch
from .lifecycle import (
    first_taken_rule_id, repeated_rule_id as _repeated_section_rule_id,
    rule_heading_ids as _section_rule_ids,
)


def section_error(rule_id: str, section: str, sources, *, allowed_sources=None) -> str:
    """The v1 section contract, shared by preflight and locked apply.

    This inspects supplied text without changing its bytes or synthesizing
    lifecycle metadata. Repository scope and proposal receipts stay outside.
    """
    heading = _HEADING.match(section.splitlines()[0] if section else "")
    if not _RULE_ID.fullmatch(rule_id):
        return "rule_id has an invalid shape"
    if heading is None or heading.group("rule") != rule_id or len(re.findall(r"^##\s+", section, re.MULTILINE)) != 1:
        return "section must contain one matching level-two rule heading"
    if not 80 <= len(section) <= _MAX_SECTION_CHARS:
        return "section length is outside 80..16384 characters"
    if not sources or len(sources) > 10 or any(not source or len(source) > _MAX_SOURCE_CHARS for source in sources):
        return "sources must contain 1..10 bounded references"
    if allowed_sources is not None and not set(sources).issubset(allowed_sources):
        return "proposal cites evidence outside this batch"
    if any(source not in section for source in sources):
        return "every proposal source must be cited in the section"
    return ""


def existing_rule_ids(workspace: Path, *, max_catalog_pages: int) -> dict[str, str]:

    """Every rule heading ID on every catalog page (all repositories
    and the general pages), mapped to the first page that heads it.
    Direct routing resolves a rule by exact ID, so an ID is unique
    tree-wide, not per page or per repository: a proposal may not
    reuse an ID that any other page already heads."""
    existing: dict[str, str] = {}
    for entry in catalog_entries(workspace, max_catalog_pages=max_catalog_pages):
        text = document_path(workspace, entry.document_id).read_text(
            encoding="utf-8"
        )
        for rule_id in _section_rule_ids(text):
            existing.setdefault(rule_id, entry.document_id)
    return existing


def _proposal_id(batch, proposal: KnowledgeRuleProposal) -> str:
    """One canonical identity projection for initial validation and locked apply."""
    identity = json.dumps({
        "batch_id": batch.batch_id,
        "repository": batch.repository.to_dict(),
        "input_index": proposal.input_index,
        "page": proposal.page_document_id,
        "rule_id": proposal.rule_id,
        "section_markdown": proposal.section_markdown,
        "sources": proposal.sources,
        "page_sha256": proposal.page_sha256,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256(identity.encode("utf-8"))


def validate_proposals(
    workspace: Path, document: dict[str, Any], batch: KnowledgeEvidenceBatch, *,
    max_rules: int, max_events: int, max_catalog_pages: int,
) -> KnowledgeProposalValidation:
    """Project untrusted model JSON onto valid, evidence-bound proposals."""
    validate_batch(batch, max_rules=max_rules, max_events=max_events)
    if not isinstance(document, dict):
        raise InvalidRequestError("proposal output must be a JSON object")
    if set(document) != {"rules"}:
        raise InvalidRequestError(
            "proposal output must contain only the rules field"
        )
    raw_rules = document.get("rules")
    if not isinstance(raw_rules, list):
        raise InvalidRequestError("proposal output rules must be an array")

    entries = {
        entry.document_id: entry
        for entry in catalog_entries(workspace, batch.repository, max_catalog_pages=max_catalog_pages)
    }
    catalog = set(entries)
    allowed_sources = {
        event.source_reference.strip() for event in batch.events
    }
    accepted: list[KnowledgeRuleProposal] = []
    rejected: list[KnowledgeProposalRejection] = []
    seen: set[tuple[str, str]] = set()
    # IDs already heading a section on any catalog page (every
    # repository), and the page each ID accepted earlier in this batch
    # went to: an ID is unique tree-wide.
    existing_ids = existing_rule_ids(workspace, max_catalog_pages=max_catalog_pages)
    proposed_ids: dict[str, str] = {}


    # Room consumed on each page by proposals accepted earlier in this
    # same batch, so two rules routed to one nearly full page do not
    # both pass here and then fail together at the validator.
    consumed: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
    for index, item in enumerate(raw_rules):
        reason = ""
        if index >= batch.max_rules:
            reason = "batch rule limit exceeded"
        elif not isinstance(item, dict):
            reason = "proposal must be an object"
        elif set(item) != {
            "page", "rule_id", "section_markdown", "sources"
        }:
            reason = "proposal fields do not match the v1 shape"
        else:
            raw_page = item["page"]
            raw_rule_id = item["rule_id"]
            raw_section = item["section_markdown"]
            raw_sources = item["sources"]
            if not all(isinstance(value, str) for value in (
                raw_page, raw_rule_id, raw_section
            )):
                reason = "proposal text fields must be strings"
                page = rule_id = section = ""
                sources = ()
            elif (
                not isinstance(raw_sources, list)
                or any(not isinstance(value, str) for value in raw_sources)
            ):
                reason = "proposal sources must be an array of strings"
                page = rule_id = section = ""
                sources = ()
            else:
                page = raw_page.strip().replace("\\", "/")
                rule_id = raw_rule_id.strip()
                section = raw_section.strip()
                sources = tuple(value.strip() for value in raw_sources)
                if not reason and page not in catalog:
                    reason = "page is outside the repository rules catalog"
                if not reason:
                    reason = section_error(rule_id, section, sources, allowed_sources=allowed_sources)
                if not reason and (page, rule_id) in seen:
                    reason = "duplicate page/rule_id in proposal output"
                elif not reason and rule_id in proposed_ids:
                    reason = (
                        "rule_id duplicates an earlier proposal on "
                        f"{proposed_ids[rule_id]}"
                    )
                if not reason and _repeated_section_rule_id(section):
                    reason = (
                        "section heads rule_id "
                        f"{_repeated_section_rule_id(section)} more "
                        "than once"
                    )
                if not reason:
                    # Every rule heading the section introduces, not
                    # only the declared one: a nested heading carrying
                    # another page's ID would otherwise land unchecked.
                    heading_id = first_taken_rule_id(_section_rule_ids(section), existing_ids, proposed_ids)
                    if heading_id:
                        owner = existing_ids.get(heading_id, proposed_ids.get(heading_id))
                        if heading_id != rule_id:
                            reason = f"nested rule heading {heading_id} already exists on {owner}"
                        elif owner == page:
                            reason = "rule_id already exists in the target page"
                        else:
                            reason = f"rule_id already exists on {owner}"



                if not reason:
                    used_bytes, used_lines = consumed[page]
                    # The normalization newline is paid once per page:
                    # after the first accepted section the page ends
                    # with one.
                    add_bytes, add_lines = _appended_size(
                        section,
                        document_path(workspace, page).read_bytes()
                        if (used_bytes, used_lines) == (0, 0) else b"\n",
                    )
                    entry = entries[page]
                    if (
                        used_bytes + add_bytes > entry.free_bytes
                        or used_lines + add_lines > entry.free_lines
                    ):
                        reason = (
                            "page full: section needs "
                            f"{add_bytes} bytes / {add_lines} lines but "
                            f"{entry.free_bytes - used_bytes} bytes / "
                            f"{entry.free_lines - used_lines} lines remain "
                            "before the page must split"
                        )
                    else:
                        consumed[page] = (
                            used_bytes + add_bytes, used_lines + add_lines
                        )

        if reason:
            rejected.append(KnowledgeProposalRejection(index, reason))
            continue

        page_data = document_path(workspace, page).read_bytes()
        page_sha256 = _sha256(page_data)
        proposal = KnowledgeRuleProposal(
            proposal_id="",
            input_index=index,
            page_document_id=page,
            rule_id=rule_id,
            section_markdown=section,
            sources=sources,
            page_sha256=page_sha256,
        )
        accepted.append(replace(proposal, proposal_id=_proposal_id(batch, proposal)))
        seen.add((page, rule_id))
        for heading_id in _section_rule_ids(section):
            proposed_ids.setdefault(heading_id, page)


    return KnowledgeProposalValidation(
        batch_id=batch.batch_id,
        repository=batch.repository,
        accepted=tuple(accepted),
        rejected=tuple(rejected),
    )
