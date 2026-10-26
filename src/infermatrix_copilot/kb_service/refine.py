"""Refine and recheck: what happens to a change the quality gate rejected.

A change the gate rejects is never merged (design v8, D12). For the service's
own change sets the generator gets the rejection reasons, proposes a refined
change on current main, and the WHOLE gate runs again (L1, L2 per block,
consistency; nothing from the rejected judgement is reused). At most
``MAX_REFINES`` refinements per original change; after that it stays unmerged
and goes to people with every round's reasons.

Triggers: a change set the gate failed when it was staged (``failed`` with a
``fail`` decision), and a PR the publisher's local gate refused because of the
change itself (``refine_needed``). A refinement of a change that already has a
PR replaces it the way a rebuild does: the old PR waits in ``superseding``
until its close is observed, the refined change gets a PR of its own.
"""

from __future__ import annotations

import uuid

MAX_REFINES = 2
REFINE_MARK = "refine of "
REFINABLE = ("intake", "rebuild", "refine")


def reasons_of(changeset: dict) -> list[str]:
    """Everything the gate said against this change, most specific first."""
    detail = changeset["detail"]
    return list(dict.fromkeys([str(p) for p in detail.get("gate_problems") or []]
                              + reasons_from_decision(detail.get("decision") or {})))[:30]


def reasons_from_decision(decision: dict) -> list[str]:
    reasons: list[str] = []
    for issue in decision.get("l1_issues") or []:
        reasons.append(f"L1 {issue.get('code')} {issue.get('path', '')} {issue.get('detail', '')}".strip())
    for block in decision.get("blocks") or []:
        if block.get("verdict") == "fail":
            why = "; ".join(f"{k}: {v}" for k, v in (block.get("reasons") or {}).items())
            reasons.append(f"L2 rejected {block.get('rule_id') or block.get('path')}: {why}".strip())
    for item in decision.get("consistency") or []:
        if item.get("verdict") == "conflict":
            reasons.append(f"consistency conflict in {item.get('owner_dir')}: {item.get('conflicts')}")
    reasons += [str(r) for r in decision.get("reasons") or []]
    return list(dict.fromkeys(r for r in reasons if r))[:30]


def needs_refinement(changeset: dict) -> bool:
    detail = changeset["detail"]
    if changeset["kind"] not in REFINABLE or detail.get("refined_as"):
        return False
    if changeset["status"] == "refine_needed":
        return True
    return changeset["status"] == "failed" and (detail.get("decision") or {}).get("status") == "fail"


def _refine_of(rt, repo: str, old_id: str) -> str | None:
    """A refinement already staged for ``old_id`` (resumed, never staged twice),
    in whatever status staging left it (companion_pending included)."""
    for candidate in rt.ledger.changesets_of_kind(repo, "refine"):
        try:
            evidence = rt.load_changeset_files(candidate["id"]).get("evidence") or []
        except (OSError, ValueError):
            continue
        if any(str(item.get("source_reference") or "") == REFINE_MARK + old_id for item in evidence):
            return candidate["id"]
    return None


def refine(rt, lifecycle, changeset: dict) -> str | None:
    """Refine one rejected change set; returns the refined change set's id or
    None (sent to people). Needs the scheduler's lease."""
    from ..knowledge_service.lifecycle import LifecycleError
    from ..trace_store import accepted_key, trace_context
    from .intake import MAX_OPS_PER_EVENT, checkpoint_draft
    from .merge import _outcome, _request_close
    from .models import ModelUnavailable
    from .runtime import gate_and_stage, publish, require_settled_reviews

    owner = getattr(rt, "lease_owner", None)
    if not owner:
        return None
    require_settled_reviews(rt, lifecycle.repo, kinds=('refine',), binding={'refine_of': changeset['id']})
    detail = changeset["detail"]
    reasons = reasons_of(changeset) or ["the quality gate rejected the change"]
    round_ = int(detail.get("refine_round") or 0) + 1
    history = [*(detail.get("refine_history") or []), {"changeset": changeset["id"], "reasons": reasons}]

    def to_people(why: str) -> None:
        rt.ledger.update_changeset(changeset["id"], status="refine_exhausted",
                                   detail={**detail, "refine_history": history})
        lines = [f"round {i}: {h['changeset']}: " + "; ".join(h["reasons"])[:300] for i, h in enumerate(history)]
        rt.ledger.enqueue_human(lifecycle.repo, f"{why}; not merged. " + " | ".join(lines)[:1800], changeset["id"])
        _outcome(rt, changeset, "refine_exhausted", rounds=len(history))

    if round_ > MAX_REFINES:
        to_people(f"still rejected after {MAX_REFINES} refinements")
        return None
    new_id = _refine_of(rt, lifecycle.repo, changeset["id"])
    if new_id is None:
        base_sha = rt.knowledge.fetch()
        base = rt.knowledge.knowledge_files(base_sha)
        external = rt.knowledge.external_texts(base_sha)
        evidence = rt.load_changeset_files(changeset["id"]).get("evidence") or []
        release, today = rt.release_for(lifecycle.repo), rt.today()
        # ALL the evidence: an intake change set can combine several events,
        # and a refinement must not silently drop the rules of later ones
        prompt_evidence = {"title": "; ".join(str(e.get("title") or "") for e in evidence if e.get("title")),
                           "body": "\n\n".join(str(e.get("body") or "") for e in evidence if e.get("body")),
                           "changed_files": sorted({f for e in evidence for f in e.get("changed_files") or []}),
                           "evidence": evidence,
                           "previous_operations": detail.get("operations") or [],
                           "gate_feedback": reasons,
                           "instruction": "The quality gate rejected the previous operations for the reasons in "
                                          "gate_feedback. Propose corrected operations that fix exactly those "
                                          "problems, or none if the change should not be made."}
        key, holder = f"refine:{lifecycle.repo}:{changeset['id']}:{uuid.uuid4().hex[:8]}", {}
        try:
            with trace_context(draft_key=key, _accepted=holder, step="refine", refine_of=changeset["id"]):
                # the whole change set is refined at once: as many operations as
                # the events it combines may carry
                limit = max(MAX_OPS_PER_EVENT * max(1, len(evidence)), len(detail.get("operations") or []))
                draft, draft_trace = checkpoint_draft(rt.ledger, owner, unit_key='refine:' + changeset['id'],
                                      base_sha=base_sha,
                                      accepted_trace=lambda: accepted_key(key, holder),
                                      repo=lifecycle.repo, repo_dir=lifecycle.knowledge_dir, event_id=0,
                                      evidence=prompt_evidence, files=base, gateway=rt.gateway,
                                      generator=rt.generator, release=release, today=today,
                                      max_operations=limit)
        except ModelUnavailable:
            return None  # the pinned generator is unavailable: tried again next pass
        except LifecycleError as exc:
            to_people(f"refinement could not be drafted: {exc}")
            return None
        if draft.rejected or not draft.operations or draft.result is None:
            to_people("the generator proposed no valid refinement")
            return None
        mark = {"source_reference": REFINE_MARK + changeset["id"], "title": "; ".join(reasons)[:300]}
        new_id = gate_and_stage(rt, lifecycle, owner, kind="refine", base=base, base_sha=base_sha,
                                external=external, operations=draft.operations, result=draft.result,
                                evidence=[*evidence, mark], event_ids=[], release=release,
                                draft_keys=[draft_trace] if draft_trace else [],
                                extra_detail={"refine_of": changeset['id'], "refine_round": round_, "refine_history": history,
                                              "generator": draft.generator})
    open_pr = changeset.get("pr_number") is not None
    rt.ledger.update_changeset(changeset["id"], status="superseding" if open_pr else "refined",
                               detail={**detail, "refined_as": new_id, "refine_history": history,
                                       "superseded_because": "refined after the quality gate rejected it"})
    _outcome(rt, changeset, "refined", replaced_by=new_id, round=round_)
    if open_pr:
        _request_close(rt, lifecycle.repo, rt.ledger.changeset(changeset["id"]))
    if rt.ledger.changeset(new_id)["status"] == "gated":
        publish(rt, lifecycle, new_id)
    return new_id


def refine_pending(rt, lifecycle) -> list[str]:
    """One pass: refine every rejected change set of this repository."""
    events: list[str] = []
    if not getattr(rt, "lease_owner", None):
        return events
    for changeset in rt.ledger.changesets(lifecycle.repo, ("failed", "refine_needed")):
        if not needs_refinement(changeset):
            continue
        new_id = refine(rt, lifecycle, changeset)
        events.append(f"refined {changeset['id']} as {new_id}" if new_id else f"refine_stopped {changeset['id']}")
    return events
