"""Bounded refinement of a draft PR review before publication.

Coverage promotion, the targeted second round, and per-comment verification
share the same draft result but never own step registration or GitHub effects.
The review handler supplies a frozen diff, tools, and tracing context.
"""

from __future__ import annotations

import asyncio
import json

from ....llm import parse_json_reply
from ....review.severity import SEVERITY_GUIDANCE, STRICT_SEVERITY_DEMOTION
from ...step import StepContext
from .utils import _SEVERITY_ORDER


async def _promote_uncovered(ctx: StepContext, output: dict,
                             spec: dict) -> dict:
    """Coverage-promotion pass: one tool-less LLM call that promotes
    maintainer-relevant concerns ALREADY PRESENT in the run's own findings/
    validated/blockers lines into review comments.

    Train forensics showed the recall loss is often not investigation but
    conversion: the ground-truth concern sat verbatim in a lens's findings or
    the run's blockers ("the VoxCPM2 regression is a separate mechanism",
    "NPU lane is red", "diff evidence was truncated") and never became a
    comment. Promotion is grounded by construction — the call may only cite
    material from the lines it is shown, never new claims — so it raises
    recall without the speculation cost of widening the lens budget."""
    # `[resolved]` lines are the review's record of a concern it closed, not
    # raw material: a residual that needs action is filed by the reviewer as
    # its own `publish` comment (prompts.py), and mining the record here
    # re-published lines that explicitly decline their point (#155).
    findings = [str(x) for x in (output.get("findings") or [])
                if not str(x).lstrip().lower().startswith("[resolved]")][:60]
    blockers = [str(x) for x in (output.get("blockers") or [])][:10]
    assumptions = [str(x) for x in (output.get("assumptions") or [])][:10]
    comments = output.get("review_comments") or []
    if not (findings or blockers) or ctx.llm is None \
            or not getattr(ctx.llm, "available", False):
        ctx.trace.record("review_coverage_skipped",
                         step="agent.review_diff",
                         reason="no findings" if not (findings or blockers)
                         else "no llm")
        return output
    system = (
        "You are the coverage editor for a PR review. You receive the "
        "review's KEPT COMMENTS plus the reviewers' raw FINDINGS/BLOCKERS/"
        "ASSUMPTIONS lines. Identify up to 3 maintainer-relevant concerns "
        "that appear in the raw lines but are NOT covered by any kept "
        "comment, and promote each into a comment object. Prioritize these "
        "classes: blast-radius of a changed default/shared value, benchmark "
        "evidence for perf/capacity changes, test integrity (a test that "
        "cannot fail or is never selected), dependency-version "
        "compatibility, resource lifecycle on abort paths, scope explicitly "
        "left unfixed (linked-issue remainder), red CI on the head, "
        "machinery duplicating a named existing helper (sibling contrast), "
        "a refuted PR-body claim, and — MOST OFTEN MISSED — GUARANTEE-GAP "
        "RESIDUE hiding in the [validated]/[claim-verified]/[sweep] lines: "
        "any verified fact that records a WEAKER guarantee than the PR "
        "needs (a mock that authors the very value the test asserts, a "
        "single-variant validation of a multi-variant feature, a gate "
        "relocated to a slower lane, 'proves route propagation only', "
        "'covered by weekly only') is a FINDING wearing a validation "
        "stamp — promote it as the pointed question or ask it implies. "
        "Rules: "
        "A true code fact is not automatically a defect. Require a concrete "
        "current trigger and adverse effect or an unmet requirement. Do not "
        "promote harmless unused configuration or hypothetical future code "
        "changes. Respect REJECTED COMMENTS and their reasons; reopen one "
        "only with new evidence that defeats the rejection. "
        "(1) ONLY promote what the raw lines already state — no new claims, "
        "no re-investigation; quote the source line in `evidence`. (2) A "
        "promoted line KEEPS its directive force: a concern naming a "
        "concrete defect, missing update, or duplicated machinery is "
        "phrased as the change to make (name both files and the helper), "
        "NEVER as a 'could you confirm…?' question; only a validation with "
        "a genuinely unresolved residual becomes a scoping ask. (3) "
        "choose minor or major using the shared impact definitions below; "
        "this promotion pass does not create blocker/nit comments. "
        "(4) If everything relevant is "
        "already covered, return no additions. Reply with exactly one JSON "
        'object: {"additions": [{"file": str, "line": int, "severity": '
        '"major"|"minor", "comment": str, "evidence": str}]}')
    system += "\n\n" + SEVERITY_GUIDANCE
    prompt = (
        "## KEPT COMMENTS\n"
        + json.dumps([{k: c.get(k) for k in ("file", "line", "severity",
                                             "comment")}
                      for c in comments], ensure_ascii=False, indent=1)
        + "\n\n## RAW FINDINGS\n" + "\n".join(f"- {x}" for x in findings)
        + "\n\n## REJECTED COMMENTS\n"
        + json.dumps(output.get("_rejected_review_comments") or [],
                     ensure_ascii=False)
        + ("\n\n## BLOCKERS\n" + "\n".join(f"- {x}" for x in blockers)
           if blockers else "")
        + ("\n\n## ASSUMPTIONS\n" + "\n".join(f"- {x}" for x in assumptions)
           if assumptions else ""))
    try:
        _tt = ctx.settings.tier_target(spec.get("mode", "eco"))
        llm = (ctx.llm.for_target(_tt)
               if hasattr(ctx.llm, "for_target") else ctx.llm)
        reply = await asyncio.to_thread(
            llm.create, system=system,
            messages=[{"role": "user", "content": prompt}],
            model=ctx.settings.review_promotion_model or _tt.model,
            role="reducer",
            max_tokens=max(4096, ctx.settings.llm_max_tokens))
        obj = parse_json_reply(reply.text or "")
    except Exception as exc:  # never fail the review over the extra pass —
        ctx.trace.record("review_coverage_skipped",   # but say WHY, loudly
                         step="agent.review_diff",
                         reason=f"{type(exc).__name__}: {exc}"[:200])
        return output
    additions = (obj or {}).get("additions") if isinstance(obj, dict) else None
    kept: list[dict] = []
    for a in (additions or [])[:3]:
        if not isinstance(a, dict) or not a.get("comment") \
                or not a.get("evidence"):
            continue
        # mechanical near-dup guard vs kept comments: the prompt's "NOT
        # covered by any kept comment" rule was observed re-adding a
        # merged-away duplicate (wave-3 pr6049 — the reducer collapsed two
        # candidates, promotion re-added the merged-away variant, judges
        # penalized the pair). Nearby anchor alone is not duplication (a
        # linked-issue remainder often anchors beside the defect), so the
        # guard also requires substantial text overlap.
        def _words(c):
            return set(str(c.get("comment") or "").lower().split())
        aw = _words(a)
        if aw and any(
                str(a.get("file") or "") == str(c.get("file") or "")
                and isinstance(a.get("line"), int)
                and isinstance(c.get("line"), int)
                and abs(a["line"] - c["line"]) <= 8
                and len(aw & _words(c)) >= max(4, len(aw) // 2)
                for c in comments):
            continue
        kept.append({"file": str(a.get("file") or "?"),
                     "line": a.get("line"),
                     "severity": str(a.get("severity") or "minor").lower(),
                     "comment": str(a["comment"]),
                     "evidence": str(a["evidence"]),
                     # ordering tag: promoted items carry a protected-class
                     # concern; under the comment budget they rank ahead of
                     # uncorroborated same-severity items (stripped at cap)
                     "corroborated_by": ["coverage"]})
    if kept:
        output = dict(output)
        output["review_comments"] = list(comments) + kept
        ctx.trace.record("review_coverage_promoted",
                         step="agent.review_diff", added=len(kept))
    else:
        ctx.trace.record("review_coverage_skipped",
                         step="agent.review_diff",
                         reason="model returned no valid additions",
                         parsed=obj is not None,
                         reply_head=str(reply.text or "")[:200])
    return output


def _uncovered_hunks(diff: str, output: dict) -> list[str]:
    """Hunk clusters with no comment anchored near them and no findings line
    citing a nearby file:line — the second round's coverage seed, at the
    granularity GT actually has. v14's file-level seed measured too coarse on
    the wave-3 gate: one comment anywhere in a file marked the whole file
    covered, while the human GT this is judged against is per-hunk inline
    comments (the losses concentrated on multi-hunk files whose comments
    clustered on one region). Test files included: an uncovered test hunk is
    where test-integrity findings hide. Returns `path:start` entries."""
    import re as _re

    from .anchor import diff_index
    regions = {path: entry.starts for path, entry in diff_index(diff).items() if entry.starts}
    comments = output.get("review_comments") or []
    cited: dict[str, list[int]] = {}
    for f in output.get("findings") or []:
        # Only CLAIM/RESOLVED anchors count as coverage. Blanket
        # [sweep]/[validated] stamps do not: wave-3 forensics found them to
        # be false negatives on exactly the GT zones ("TP group build …
        # verified" where the crash was), and their density suppressed the
        # second round on every hunk it could have rescued (pr5678: skipped
        # with four missed-GT hunks "covered" by self-issued stamps).
        low = str(f).lstrip().lower()
        if not low.startswith(("[claim-", "[resolved]")):
            continue
        for base, num in _re.findall(r"([\w.\-]+\.\w+):(\d+)", str(f)):
            cited.setdefault(base, []).append(int(num))
    out: list[str] = []
    for path, starts in regions.items():
        base = path.rsplit("/", 1)[-1]
        file_comments = [c.get("line") for c in comments
                         if str(c.get("file") or "").endswith(base)
                         and isinstance(c.get("line"), int)]
        file_cited = cited.get(base, [])
        has_any_comment = any(str(c.get("file") or "").endswith(base)
                              for c in comments)
        for s in starts:
            near = any(abs(n - s) <= 40 for n in file_comments + file_cited)
            # a file-level comment (no line) covers a single-hunk file only
            if not near and not (has_any_comment and len(starts) == 1):
                out.append(f"{path}:{s}")
    return out


async def _second_round(ctx: StepContext, output: dict, common: dict,
                        diff: str) -> dict:
    """Coverage-driven second investigation round (RFC q3): one bounded pass
    seeded by the run's own coverage holes, instead of a bigger fixed pass
    count. Additions face the same verify pass as first-round comments."""
    from ...agent_runtime import run_agent_step

    if not ctx.settings.review_second_round:
        return output
    # 8 seeds, not 14: a 14-seed round-2 was measured producing finals too
    # large to coerce on the wave-4 big items (both 14-seed runs failed,
    # both sub-10-seed runs succeeded) — fewer, deeper hunk visits beat a
    # sweep that never files
    uncovered = _uncovered_hunks(diff, output)[:8]
    claims_unchecked = "[claim-" not in " ".join(
        str(f) for f in (output.get("findings") or []))
    if len(uncovered) < ctx.settings.review_second_round_min_files \
            and not claims_unchecked:
        ctx.trace.record("review_second_round", step="agent.review_diff",
                         skipped="no coverage holes", uncovered=0)
        return output
    kept = [{k: c.get(k) for k in ("file", "line", "severity", "comment")}
            for c in (output.get("review_comments") or [])]
    guidance = (
        "You are the SECOND-ROUND reviewer. The first round produced the "
        "KEPT COMMENTS in your evidence; your job is ONLY its coverage "
        "holes — do not re-litigate or duplicate what is already covered.\n"
        "Respect rejected_comments and their reasons. Reopen a rejected "
        "concern only with new evidence defeating that reason. A true code "
        "fact needs a current trigger and adverse effect or an unmet "
        "requirement to become a finding; harmless unused values and "
        "hypothetical future changes are not defects.\n"
        + (("These diff hunks (file:start-line) have NO comment and NO "
            "recorded verification near them. Review each AT MAINTAINER "
            "INLINE GRANULARITY: page to the hunk in situ, and either raise "
            "one specific localized comment (the concrete ask a human "
            "reviewer would leave on that hunk) or record a one-line "
            "[validated]/[resolved] findings entry naming file:line — "
            "silence on a changed hunk is not an option:\n"
            + "\n".join(f"- {p}" for p in uncovered) + "\n")
           if uncovered else "")
        + (("The PR body's checkable claims were never verified (no "
            "[claim-*] findings line exists). Build the claim ledger now: "
            "verify or refute each checkable body claim (checklist 13).\n")
           if claims_unchecked else "")
        + "Every comment needs verbatim-quoted evidence with file:line. "
          "Budget discipline: reserve your last round for the output "
          "contract.")
    # per-file diff slices for the seed files: on big PRs the shared pr_diff
    # evidence is CAPPED and the tail files' hunks never reach any pass —
    # wave-3 pr5691's round-2 read the right 10 files but held the same
    # truncated diff blob, so the beyond-cap hunks stayed unreviewable. The
    # slices come from the UNCAPPED diff text.
    seed_paths = {p.rsplit(":", 1)[0] for p in uncovered}
    from ....review.diff import parse_diff
    hunk_evidence = "".join(item.raw for item in parse_diff(diff)
                            if item.new_path in seed_paths)[:100_000]
    from ...agent_runtime.ensemble import (
        lens_backend_member,
        outcome_blocked as _seat_produced_nothing,
    )

    member = lens_backend_member(ctx.settings, "round2")
    routing = ({"harness_member": member, "model_override": member.model}
               if member is not None else {})
    round_kwargs = {**common, "step_name": "agent.review_diff#round2",
                    "guidance": common["guidance"] + "\n\n## SECOND ROUND\n"
                    + guidance,
                    "evidence": {**common["evidence"],
                                 "uncovered_hunk_diffs": hunk_evidence,
                                 "kept_comments": json.dumps(
                                     kept, ensure_ascii=False),
                                 "rejected_comments": json.dumps(
                                     output.get("_rejected_review_comments")
                                     or [], ensure_ascii=False)}}
    result, extra = await run_agent_step(
        ctx, **round_kwargs,
        max_iters=ctx.settings.review_second_round_max_iters, **routing)
    if routing and _seat_produced_nothing(result, extra):
        # A routed second round has no retry of its own, so a transport
        # failure on that backend silently deleted the whole coverage pass:
        # measured 2026-08-16, a Fable-5 quota exhaustion left 16 of 20
        # holdout items reporting "8 uncovered hunks, 0 added" — the pass
        # that exists to close coverage holes contributed nothing, invisibly.
        # Fall back to the run's own backend and say so.
        ctx.trace.record("capability_gap", capability="review.routed_seat",
                         step="agent.review_diff#round2",
                         effect="routed second round produced no output; "
                                "retrying on the run's default backend")
        result, extra = await run_agent_step(
            ctx, **{**round_kwargs,
                    "step_name": "agent.review_diff#round2/fallback"},
            max_iters=ctx.settings.review_second_round_max_iters)
    if not result.ok and not (extra or {}).get("review_comments"):
        ctx.trace.record("review_second_round", step="agent.review_diff",
                         uncovered=len(uncovered), added_comments=0,
                         added_findings=0, failed=True)
        return output
    new_comments = []
    for c in (extra or {}).get("review_comments") or []:
        if not isinstance(c, dict) or not c.get("comment"):
            continue
        # near-dup guard: same file + line within 8 of an existing comment
        dup = any(str(c.get("file") or "") == str(e.get("file") or "")
                  and isinstance(c.get("line"), int)
                  and isinstance(e.get("line"), int)
                  and abs(c["line"] - e["line"]) <= 8
                  for e in (output.get("review_comments") or []))
        if not dup:
            new_comments.append(c)
    new_findings = [str(f) for f in (extra or {}).get("findings") or []
                    if str(f).lstrip().lower().startswith(
                        ("[validated]", "[resolved]", "[claim-", "[sweep]",
                         "[upstream-verify]"))]
    out2 = dict(output)
    if new_comments:
        out2["review_comments"] = list(
            output.get("review_comments") or []) + new_comments
    if new_findings:
        out2["findings"] = list(output.get("findings") or []) + new_findings
    ctx.trace.record("review_second_round", step="agent.review_diff",
                     uncovered=len(uncovered),
                     claims_unchecked=claims_unchecked,
                     added_comments=len(new_comments),
                     added_findings=len(new_findings))
    return out2


_VERIFY_GUIDANCE = """You verify ONE draft review comment against the PR-time tree.

In order, with the minimum tool calls (your budget is small):
1. Anchor: does the cited file:line (or quoted snippet) exist as the comment
   claims? Read that region of the file.
2. Claim: is the asserted problem true of the code you just read? If the
   comment asserts consumers/callers/tests elsewhere, grep for them and read
   the one that decides the claim.
3. Actionability: establish a concrete trigger on this PR head and its
   adverse effect, or a specific unmet requirement. A code fact being true
   does not establish a defect. Unused optional configuration without an
   affected consumer, and a problem contingent on hypothetical future code
   changes, are not actionable. Check rejected_comments before reopening
   a concern; identify new evidence that defeats its rejection.
4. Severity: apply the shared severity definitions below to the demonstrated
   trigger and impact; confirming the claim does not by itself justify P0.

Verdicts:
- confirmed: you READ the code that makes the claim true. You MUST return
  `evidence` as SELF-CONTAINED PROOF a reader with no repo access can check:
  the current trigger and adverse effect (or unmet requirement), together
  with the decisive code line(s) QUOTED VERBATIM with their file:line, e.g.
  'serving_speech.py:3711 `extra_args["tts_local_seed"] = seed` — set for
  every model, no qwen3_tts gate'. A narrative like "read the file, claim
  holds" is NOT proof and scores as speculation downstream. When the
  decisive code lies OUTSIDE the diff (a consumer, a sibling platform, a CI
  lane rule), say so explicitly in the evidence — 'unchanged by this diff,
  present in the PR-time tree: <file:line> `quote`' — a diff-only reader
  must see why the quoted line is not in the diff, or the finding reads as
  fabrication. Optionally
  return a tightened `comment` (sharper wording, exact file/line) — keep
  the substance, never soften a confirmed defect.
- refuted: the code CONTRADICTS the claim (misread, already handled, wrong
  file). Refuted is an evidence conclusion, never a budget one.
- not_actionable: the cited facts may be true, but no current defect or
  unmet requirement is demonstrated. Explain why the proposed ask is not
  needed on this head. This verdict drops the comment.
- unverifiable: you could not decide within budget.

Confirming from plausibility alone is the one failure this pass exists to
prevent — when in doubt between confirmed and unverifiable, unverifiable.""" + "\n\n" + SEVERITY_GUIDANCE


async def _verify_comments(ctx: StepContext, output: dict,
                           common: dict) -> dict:
    """Per-comment agentic verification (val-gate lesson: with recall at
    parity the arm lost on per-comment grounding). Each draft comment gets
    one small tool-loop that must anchor and re-derive the claim on the
    PR-time tree: refuted and non-actionable comments drop, unverifiable ones demote one
    severity step, confirmed ones may be tightened in wording/position. A
    verification-step failure keeps the comment unchanged — this pass may
    only improve precision, never silently delete recall."""
    from ...agent_runtime import run_agent_step

    comments = list(output.get("review_comments") or [])
    if not comments or not ctx.settings.review_verify_comments:
        return output
    sem = asyncio.Semaphore(ctx.settings.review_verify_concurrency)
    diff_ev = str((common.get("evidence") or {}).get("pr_diff") or "")

    async def _one(i: int, c: dict):
        async with sem:
            # pr_diff leads the evidence pack so the big block is byte-
            # identical across the fan-out and rides the provider cache
            probe = {k: v for k, v in c.items() if k != "corroborated_by"}
            result, out = await run_agent_step(
                ctx, step_name=f"agent.verify_comment#{i}",
                purpose="Verify one draft review comment against the "
                        "PR-time tree.",
                guidance=_VERIFY_GUIDANCE,
                expected="verdict confirmed|refuted|not_actionable|unverifiable, with "
                         "optional comment/line/severity corrections",
                evidence={"pr_diff": diff_ev,
                          "draft_comment": json.dumps(
                              probe, ensure_ascii=False),
                          "rejected_comments": json.dumps(
                              output.get("_rejected_review_comments") or [],
                              ensure_ascii=False)},
                output_extension={
                    "verdict": "confirmed|refuted|not_actionable|unverifiable",
                    "comment": "optional tightened rewrite",
                    "line": "optional corrected line number",
                    "severity": "optional corrected severity",
                    "evidence": "confirmed only: verbatim-quoted decisive "
                                "code line(s) with file:line, current trigger "
                                "and adverse effect or unmet requirement — "
                                "proof a repo-less reader can check"},
                extra_tools=common.get("extra_tools"),
                max_iters=ctx.settings.review_verify_max_iters)
            return i, result, (out or {})

    results = await asyncio.gather(*(_one(i, c)
                                     for i, c in enumerate(comments)))
    demote = STRICT_SEVERITY_DEMOTION
    kept: list[dict] = []
    n_drop = n_demote = 0
    for i, result, out in sorted(results, key=lambda r: r[0]):
        c = dict(comments[i])
        verdict = str(out.get("verdict") or "").lower()
        if not result.ok or verdict not in ("confirmed", "refuted",
                                            "not_actionable", "unverifiable"):
            kept.append(c)          # fail-open: never delete on pass failure
            continue
        if verdict in ("refuted", "not_actionable"):
            n_drop += 1
            continue
        if verdict == "confirmed":
            c["_verified"] = True  # budget/overflow: a confirmed comment's
            # tail placement is evidence, not noise — see the overflow gate
            if out.get("comment"):
                c["comment"] = str(out["comment"])
            if isinstance(out.get("line"), int):
                c["line"] = out["line"]
            sev = str(out.get("severity") or "").lower()
            if sev in _SEVERITY_ORDER:
                c["severity"] = sev
            if out.get("evidence"):
                # replace the drafting-stage narrative with the verifier's
                # quoted proof: the review is judged by readers with no repo
                # access, and a claim whose evidence they can check from the
                # quote alone is the difference between "grounded" and
                # "speculative" (precision sat at .55 across five configs
                # until the rendered evidence became self-proving)
                c["evidence"] = str(out["evidence"])
        else:                        # unverifiable: keep, one step down
            s = str(c.get("severity", "minor")).lower()
            c["severity"] = demote.get(s, "nit")
            n_demote += 1
        kept.append(c)
    ctx.trace.record("review_comments_verified", step="agent.review_diff",
                     total=len(comments), dropped=n_drop, demoted=n_demote)
    out2 = dict(output)
    out2["review_comments"] = kept
    return out2


async def refine_review(
    ctx: StepContext, output: dict, *, common: dict, diff: str,
    spec: dict, depth: str,
) -> dict:
    """Run the bounded post-draft passes in their evidence-preserving order.

    A successful non-light draft first re-files its own verified residuals,
    then mines uncovered findings and hunks. Every depth, including light,
    verifies the resulting comments before the handler applies its final
    disposition and comment budget.
    """
    if depth != "light":
        output = await _promote_uncovered(ctx, output, spec)
        output = await _second_round(ctx, output, common, diff)
    return await _verify_comments(ctx, output, common)
