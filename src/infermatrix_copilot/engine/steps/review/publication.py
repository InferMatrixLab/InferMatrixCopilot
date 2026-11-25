"""Finalize independent Strict findings against the pinned host context."""
from __future__ import annotations

import asyncio

from ....ut_coverage import CoverageReport, PublicDef, cap_gap_comments
from .anchor import resolve_review_comments
from .feedback import existing_threads, match_thread
from .utils import (
    _SEVERITY_ORDER, _render_review_md, _render_review_summary, _review_verdict,
    disposition_records, finalize_review_dispositions,
)


async def finalize_review_publication(ctx, packet, carried, rechecks, recheck_missing):
    """Apply thread suppression and output budgets after substantive selection."""
    diff = ctx.state.get("diff_text") or ""
    pr_state = str(ctx.state.get("pr_state") or "")
    output = {key: packet.get(key) for key in ("review_comments", "findings", "summary")}
    ut_report = CoverageReport(candidates=[PublicDef(**row) for row in packet["ut_candidates"]])
    all_candidates, anchor_stats = resolve_review_comments(
        [c for c in (output.get("review_comments") or []) if isinstance(c, dict)], str(diff))
    output["review_comments"] = all_candidates
    ctx.trace.record("anchor_resolution", **anchor_stats)
    substantive_findings, withheld = finalize_review_dispositions(output)
    titles = {row["finding_id"]: row for row in carried}
    active = []
    review_notes = []
    for check in rechecks:
        original = titles[check["finding_id"]]
        if check["outcome"] == "still_affected":
            active.append({"severity": original.get("severity"), "evidence": check["evidence"]})
            review_notes.append(f"Still affected: {original['title']} — {check['evidence']}")
        elif check["outcome"] == "unverified":
            review_notes.append(f"Recheck unverified: {original['title']}")
    review_notes.extend(recheck_missing)
    verdict = _review_verdict([*substantive_findings, *active], pr_state)
    if review_notes and verdict == "APPROVE":
        verdict = "COMMENT"
    output["_recheck_notes"] = review_notes
    if review_notes:
        output["summary"] = ""
    existing = {}
    feedback_status = "not_needed"
    if substantive_findings:
        threads, feedback_status = await asyncio.to_thread(
            existing_threads, ctx, (ctx.state.get("task_spec") or {}).get("pr"), ctx.state.get("pr_head_sha"))
        for finding in substantive_findings:
            thread = match_thread(finding, threads)
            if thread:
                existing[id(finding)] = thread
    published = [c for c in substantive_findings if id(c) not in existing]
    output["_existing_findings"] = [
        {**c, "thread_url": existing[id(c)]} for c in substantive_findings if id(c) in existing
    ]
    output["_feedback_status"] = feedback_status
    # at most three untested-function comments; the rest are named, not lost
    published, ut_dropped = cap_gap_comments(published, ut_report)
    output["review_comments"] = published
    output["_withheld_findings"] = withheld
    comments = sorted(output.get("review_comments") or [],
                      key=lambda c: (_SEVERITY_ORDER.get(
                          str(c.get("severity", "minor")).lower(), 2),
                          -len(c.get("corroborated_by") or [])))
    for c in comments:
        c.pop("corroborated_by", None)
    output["review_comments"] = comments[:8]
    final_selection = [
        (c.get("file"), c.get("line"), c.get("comment"), c.get("severity"))
        for c in output["review_comments"]
    ]
    if sorted(map(tuple, packet["draft_selection"]), key=repr) != sorted(final_selection, key=repr):
        # The reducer's prose describes its draft selection. After promotion,
        # verification or a budget cut it may contradict the published set.
        # Let the renderer derive a truthful summary from the final comments.
        output["summary"] = ""
    if ut_dropped:
        output["summary"] = (str(output.get("summary") or "").rstrip()
                             + "\n\nAlso without a unit test: "
                             + ", ".join(f"`{n}`" for n in ut_dropped) + ".").lstrip()
    # Recorded AFTER the cut: a candidate the review chose to publish but the
    # budget dropped is `over_budget`, never `excluded`. A consumer checking
    # that no withheld finding was published must not trip over a healthy
    # review whose ninth comment simply did not fit.
    output["_finding_dispositions"] = disposition_records(
        all_candidates, output["review_comments"], existing)
    if withheld or existing:
        ctx.trace.record(
            "finding_dispositions",
            withheld=len(withheld) + len(existing),
            published=len(output["review_comments"]),
            dispositions=[r["disposition"]
                          for r in output["_finding_dispositions"]],
        )
    rich = sum(1 for c in comments[:8]
               if _SEVERITY_ORDER.get(str(c.get("severity", "minor")).lower(),
                                      2) <= _SEVERITY_ORDER["major"]) >= 2
    # Overflow renders on evidence-rich reviews (unchanged), and ALSO for any
    # cut comment the verify pass individually CONFIRMED on the tree — wave-2
    # forensics measured three verify-confirmed findings (one echoing the GT
    # thread) silently dying here because the kept eight were minors and the
    # rich gate never opened. A confirmed tail is evidence, not noise; the
    # unverified tail still renders only on rich reviews.
    output["_review_overflow"] = [
        c for c in comments[8:]
        if _SEVERITY_ORDER.get(str(c.get("severity", "minor")).lower(), 2)
        <= _SEVERITY_ORDER["minor"]
        and (rich or c.get("_verified"))][:4]
    return {
        "review_text": _render_review_md(output, pr_state, verdict=verdict),
        "review_summary": _render_review_summary(output, pr_state, verdict=verdict),
        "review_comments": output["review_comments"],
        "review_verdict": verdict,
        "review_finding_dispositions": output["_finding_dispositions"],
        "review_feedback_status": feedback_status,
    }
