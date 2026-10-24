"""The knowledge quality gate: L1, per-block L2 judgement, change-set consistency.

``pass`` requires L1 clean AND every applicable L2 dimension "yes" on every
block AND a "consistent" change set. Anything uncertain goes to people:

* ``fail``  — an L1 issue, or a judge "no" (the drafter may get one appeal);
* ``human`` — a judge "unsure", a model that is unavailable or unparseable, a
  protected rule, a reference outside knowledge/ (companion PR), or a
  circuit breaker. Nothing uncertain is ever auto-merged (fail closed).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from ..budgeting import native_outcome_unknown

from ..knowledge_service.l1 import Block, Change, ChangesetResult, check_changeset
from ..knowledge_service.lifecycle import LifecycleError, Page
from .judge_tuning import (CONSISTENCY_SYSTEM, JUDGE_SYSTEM, NEIGHBOUR_LIMIT,
                           verdict_from_answers)
from .models import ModelGateway, ModelRole, ModelUnavailable
from .quality_review import QualityPolicy, QualityPolicyError, review_call

VERDICTS = ("yes", "no", "unsure")
# the L2 dimensions each block kind must answer "yes" on. Structural: the
# per-kind sets are shared with validators and the depth reviewer — prompt
# wording is the tunable part and lives in judge_tuning.
DIMENSIONS = {
    "add": ("faithful", "non_contradictory", "actionable"),
    "edit": ("faithful", "non_contradictory", "actionable", "same_meaning"),
    "supersede": ("deletion_justified",),
    "retire": ("deletion_justified",),
    "purge": ("deletion_justified",),
    "prose": ("faithful", "does_not_weaken", "non_contradictory"),
}


@dataclass
class BlockVerdict:
    block: Block
    verdict: str                  # pass | fail | human
    dimensions: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    model: str = ""
    quality_receipt: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {**self.block.to_dict(), "verdict": self.verdict, "dimensions": self.dimensions,
                "reasons": self.reasons, "model": self.model,
                **({"quality_receipt": self.quality_receipt} if self.quality_receipt else {})}


@dataclass
class GateDecision:
    status: str                   # pass | fail | human
    reasons: list[str]
    l1: ChangesetResult
    blocks: list[BlockVerdict] = field(default_factory=list)
    consistency: list[dict] = field(default_factory=list)
    upstream: dict = field(default_factory=dict)   # {repository, sha} the facts were observed on
    facts: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status, "reasons": self.reasons,
            "l1_issues": [i.to_dict() for i in self.l1.issues],
            "external_refs": [list(ref) for ref in self.l1.external_refs],
            "blocks": [b.to_dict() for b in self.blocks],
            "consistency": self.consistency,
            "upstream": self.upstream, "facts": self.facts,
        }


def _section_text(files: dict[str, str], path: str, rule_id: str) -> str:
    text = files.get(path)
    if not text or not rule_id:
        return text or ""
    try:
        return Page.parse(text).rule(rule_id).text
    except LifecycleError:
        return ""


def _owner_dir(path: str) -> str:
    return str(PurePosixPath(path).parent)


def _neighbours(files: dict[str, str], path: str, rule_id: str,
                 limit: int = NEIGHBOUR_LIMIT) -> list[dict]:
    """Other ACTIVE rules in the same owner directory, as context."""
    out = []
    directory = _owner_dir(path)
    for other, text in sorted(files.items()):
        if _owner_dir(other) != directory or not other.endswith(".md"):
            continue
        for section in Page.parse(text).rules():
            if section.rule_id == rule_id:
                continue
            try:
                if section.footer.status != "active":
                    continue
            except LifecycleError:
                continue
            out.append({"page": other, "rule_id": section.rule_id,
                        "text": "\n".join(section.body_without_footer.splitlines()[:10])})
            if len(out) >= limit:
                return out
    return out


def validate_block_review(data: dict, block: Block) -> None:
    dimensions = DIMENSIONS[block.op]
    answers = data.get("dimensions")
    if not isinstance(answers, dict):
        raise ValueError("dimensions must be an object")
    if data.get("reasons") is not None and not isinstance(data["reasons"], dict):
        raise ValueError("reasons must be an object")
    for name in dimensions:
        if answers.get(name) not in VERDICTS:
            raise ValueError(f"dimension {name} must be one of {VERDICTS}")


def block_review_verdict(data: dict, block: Block) -> str:
    validate_block_review(data, block)
    return verdict_from_answers({name: data["dimensions"][name] for name in DIMENSIONS[block.op]})


def block_review_result(reply, block: Block, judge: ModelRole) -> BlockVerdict:
    dimensions = DIMENSIONS[block.op]
    answers = {name: reply.data["dimensions"][name] for name in dimensions}
    verdict = block_review_verdict(reply.data, block)
    reasons = {k: str(v) for k, v in (reply.data.get("reasons") or {}).items() if k in dimensions}
    return BlockVerdict(block, verdict, answers, reasons, reply.served_model or judge.label(),
                        getattr(reply, "quality_receipt", {}))


def judge_block(block: Block, *, base: dict[str, str], head: dict[str, str], evidence: list[dict],
                gateway: ModelGateway, judge: ModelRole, evidence_for=None, budget=None, charge_usd=None,
                quality_policy=None) -> BlockVerdict:
    """``evidence_for(rule_text, evidence)`` narrows and expands the evidence for
    this one block (None: the change set's evidence as recorded)."""
    dimensions = DIMENSIONS[block.op]
    before = _section_text(base, block.path, block.rule_id) if block.op != "add" else ""
    after = _section_text(head, block.path, block.rule_id) if block.op != "purge" else ""
    if evidence_for is not None and block.kind == "rule":
        evidence = evidence_for(after or before, evidence)
    payload = {
        "change": {"op": block.op, "page": block.path, "rule_id": block.rule_id,
                   "before": before, "after": after},
        "surrounding_rules": _neighbours(head, block.path, block.rule_id),
        "evidence": evidence,
        "dimensions_to_answer": list(dimensions),
    }
    prompt = "<untrusted_data>\n" + json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True).replace(
        "<", "\\u003c") + "\n</untrusted_data>\n"

    try:
        reply = review_call(gateway, judge, policy=quality_policy or QualityPolicy("block"),
                            system=JUDGE_SYSTEM, prompt=prompt, validate=lambda data: validate_block_review(data, block),
                            budget=budget, charge_usd=charge_usd)
    except QualityPolicyError:
        raise
    except ModelUnavailable as exc:
        if native_outcome_unknown(exc):
            raise  # Preserve the owning checkpoint's unresolved paid intent.
        return BlockVerdict(block, "human", reasons={"model": str(exc)}, model=judge.label(),
                            quality_receipt=getattr(exc, "quality_receipt", {}))
    return block_review_result(reply, block, judge)


def check_consistency(head: dict[str, str], owner_dirs: list[str], *, gateway: ModelGateway,
                      judge: ModelRole, changed: set[str] | None = None) -> list[dict]:
    """``changed``: the rule IDs the change adds, edits, retires or supersedes.
    Only a conflict naming one of them is the change's; a conflict between
    rules it does not touch was already on main, is kept as ``preexisting``,
    and never fails the change (refining could not fix it). A conflict that
    names no rule counts against the change. None: every conflict counts."""
    from ..knowledge_service.l1 import _sha as sha256_text

    results = []
    for directory in owner_dirs:
        pages = {p: t for p, t in sorted(head.items())
                 if _owner_dir(p) == directory and p.endswith(".md")}
        rules = []
        for path, text in pages.items():
            for section in Page.parse(text).rules():
                try:
                    footer = section.footer
                except LifecycleError:
                    continue
                if footer.status == "active":
                    rules.append({"page": path, "rule_id": section.rule_id,
                                  "supersedes": footer.supersedes,
                                  "text": section.body_without_footer})
        prompt = "<untrusted_data>\n" + json.dumps({"directory": directory, "rules": rules,
                                                   **({"changed_rule_ids": sorted(changed)} if changed is not None else {})},
                                                   ensure_ascii=False, indent=1).replace("<", "\\u003c") \
            + "\n</untrusted_data>\n"

        def validate(data: dict) -> None:
            if data.get("verdict") not in ("consistent", "conflict", "unsure"):
                raise ValueError("verdict must be consistent, conflict or unsure")
            if not isinstance(data.get("conflicts") or [], list):
                raise ValueError("conflicts must be a list")

        try:
            reply = review_call(gateway, judge, policy=QualityPolicy("consistency"),
                                system=CONSISTENCY_SYSTEM, prompt=prompt, validate=validate)
            verdict, conflicts = reply.data["verdict"], reply.data.get("conflicts") or []
        except ModelUnavailable as exc:
            if native_outcome_unknown(exc):
                raise  # Do not dispatch another owner review after a lost call.
            verdict, conflicts = "unsure", [["", "", str(exc)]]
        preexisting: list = []
        if verdict == "conflict" and changed is not None and conflicts:
            ours = [c for c in conflicts if _names_changed(c, changed)]
            preexisting = [c for c in conflicts if not _names_changed(c, changed)]
            conflicts = ours
            if not ours:
                verdict = "consistent"
        results.append({
            "owner_dir": directory, "verdict": verdict, "conflicts": conflicts, "preexisting": preexisting,
            "pages": {p: sha256_text(t) for p, t in pages.items()},
        })
    return results


def _names_changed(conflict, changed: set[str]) -> bool:
    """Whether a reported conflict involves a changed rule. Anything malformed,
    or naming no rule (empty, null, not a string), counts as the change's."""
    if not isinstance(conflict, (list, tuple)) or len(conflict) < 2:
        return True
    ids = conflict[:2]
    if not all(isinstance(i, str) and i.strip() for i in ids):
        return True
    return any(i.strip() in changed for i in ids)


def _changed_rule_ids(blocks, base: dict[str, str], head: dict[str, str]) -> set[str]:
    """The rule IDs of every changed rule block, with the ``###`` rules nested
    inside them (on either side of the change)."""
    out: set[str] = set()
    for block in blocks:
        if block.kind != "rule":
            continue
        out.add(block.rule_id)
        for files in (base, head):
            try:
                out.update(Page.parse(files[block.path]).rule(block.rule_id).nested_rule_ids)
            except (KeyError, LifecycleError):
                continue
    return out


def run_gate(*, base: dict[str, str], head: dict[str, str], changes: list[Change],
             external_texts: dict[str, str], evidence: list[dict], gateway: ModelGateway,
             judge: ModelRole, release: str, repo_dir: str,
             protected_rules: tuple[str, ...] = (),
             retire_ratio: float = 0.10, max_files: int = 50, facts=None,
             evidence_for=None) -> GateDecision:
    """``facts`` observes the repository's public upstream (None: the
    repository has no upstream to attest, or it publishes nothing)."""
    l1 = check_changeset(base, head, changes, external_texts=external_texts, release=release)
    reasons: list[str] = []
    if not l1.ok:
        return GateDecision("fail", [f"L1: {i.code} {i.path} {i.detail}" for i in l1.issues], l1)
    upstream, signed_facts = {}, []
    if facts is not None:
        from ..knowledge_service.facts import FactsError, attest
        from .upstream_facts import change_claims

        try:
            claims = change_claims(head, l1.blocks, facts.top_level(facts.head()))
            upstream, signed_facts, problems = attest(claims, facts) if claims else ({}, [], [])
        except FactsError as exc:
            return GateDecision("human", [f"upstream facts could not be checked: {exc}"], l1)
        if problems:
            return GateDecision("fail", [f"upstream fact: {p}" for p in problems], l1,
                                upstream=upstream, facts=signed_facts)
    if l1.external_refs:
        reasons.append("references outside knowledge/ need a companion PR: "
                       + ", ".join(f"{rid}@{path}" for rid, path in l1.external_refs))
    touched = {b.rule_id for b in l1.blocks if b.kind == "rule"}
    if touched & set(protected_rules):
        reasons.append(f"protected rules: {sorted(touched & set(protected_rules))}")
    # the breaker is per repository: count this repository's active rules only
    active_before = sum(
        1 for path, text in base.items() if path.startswith(repo_dir + "/")
        for s in Page.parse(text).rules() if _is_active(s))
    removed = len(l1.retired) + len(l1.purged)
    if active_before and removed / active_before > retire_ratio:
        reasons.append(f"circuit breaker: {removed} of {active_before} rules retired/purged")
    if len(changes) > max_files:
        reasons.append(f"circuit breaker: {len(changes)} files changed (limit {max_files})")
    blocks = [judge_block(b, base=base, head=head, evidence=evidence, gateway=gateway, judge=judge,
                          evidence_for=evidence_for) for b in l1.blocks]
    if any(b.verdict == "fail" for b in blocks):
        return GateDecision("fail", reasons + [f"L2 rejected {b.block.rule_id or b.block.path}"
                                               for b in blocks if b.verdict == "fail"], l1, blocks,
                            upstream=upstream, facts=signed_facts)
    owner_dirs = sorted({_owner_dir(b.path) for b in l1.blocks})
    consistency = check_consistency(head, owner_dirs, gateway=gateway, judge=judge,
                                    changed=_changed_rule_ids(l1.blocks, base, head))
    if any(item["verdict"] == "conflict" for item in consistency):
        return GateDecision("fail", reasons + ["change set is inconsistent"], l1, blocks, consistency,
                            upstream, signed_facts)
    if any(b.verdict == "human" for b in blocks):
        reasons.append("L2 was not sure about " + ", ".join(
            b.block.rule_id or b.block.path for b in blocks if b.verdict == "human"))
    if any(item["verdict"] == "unsure" for item in consistency):
        reasons.append("consistency check was not sure")
    return GateDecision("human" if reasons else "pass", reasons, l1, blocks, consistency,
                        upstream, signed_facts)


def _is_active(section) -> bool:
    try:
        return section.footer.status == "active"
    except LifecycleError:
        return True


def changes_between(base: dict[str, str], head: dict[str, str]) -> list[Change]:
    out = []
    for rel in sorted(set(base) | set(head)):
        if base.get(rel) == head.get(rel):
            continue
        status = "A" if rel not in base else "D" if rel not in head else "M"
        out.append(Change(f"knowledge/{rel}", status, "" if status == "A" else "100644",
                          "" if status == "D" else "100644"))
    return out


def signable_blocks(decision: GateDecision) -> list[dict]:
    return [{"block_id": b.block.block_id, "kind": b.block.kind, "path": b.block.path,
             "rule_id": b.block.rule_id, "op": b.block.op, "sha256": b.block.sha256,
             "verdict": b.verdict, "model": b.model} for b in decision.blocks]


def summarize(decision: GateDecision) -> dict[str, Any]:
    return decision.to_dict()
