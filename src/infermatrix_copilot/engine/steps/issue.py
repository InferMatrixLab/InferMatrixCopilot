"""Issue-facing steps: fetch, drafted answer, triage table, gated posting."""

from __future__ import annotations

import json

from ..step import FailureKind, StepContext, StepResult, StepSpec
from ._common import from_state, register_step, require_repo, step
from ._common import gh as _gh
from ._common import gh_read_tools as _gh_read_tools
from ._common import post_step as _post_step
from ._common import repo_path as _repo_path


@step("issue.fetch", "deterministic", "read",
      "Fetch an issue via gh (read-only).")
async def _issue_fetch(ctx: StepContext) -> StepResult:
    """Fetch issue material via `gh` for the downstream answer/triage agents.
    Reads `task_spec`: with an `issue` number, fetches that one issue
    (`issue view` — title/body/labels/comments); with no number but
    `kind == "issue_filter"`, fetches recent open issues (`issue list`, capped by
    `params.limit`) for triage mode; any other no-number case is BLOCKED. Returns
    injected `issue_text` from state verbatim when present (offline testing); a
    failed `gh` call degrades to BLOCKED.

    Publishes the raw JSON as `issue_text` to state (B2 `state_updates`)."""
    cached = from_state(ctx, "issue_text")
    if cached is not None:
        return cached
    spec = ctx.state.get("task_spec") or {}
    issue = spec.get("issue") if isinstance(spec, dict) else None
    kind = spec.get("kind") if isinstance(spec, dict) else ""
    repo = require_repo(ctx)
    if isinstance(repo, StepResult):
        return repo
    if not issue:
        if kind != "issue_filter":
            return StepResult(False, FailureKind.BLOCKED, "no issue number in task spec")
        # triage mode: recent open issues instead of a single one
        limit = str(ctx.params.get("limit", 20))
        code, out = _gh(["issue", "list", "--state", "open", "--limit", limit,
                         "--json", "number,title,labels,createdAt"], cwd=repo)
        if code != 0:
            return StepResult(False, FailureKind.BLOCKED, f"gh issue list failed: {out[:500]}")
        ctx.state["issue_text"] = out
        n = len(json.loads(out or "[]"))
        return StepResult(True, summary=f"fetched {n} open issues for triage",
                          outputs={"state_updates": {"issue_text": out}})
    code, out = _gh(["issue", "view", str(issue), "--json",
                     "title,body,labels,comments"], cwd=repo)
    if code != 0:
        return StepResult(False, FailureKind.BLOCKED, f"gh issue view failed: {out[:500]}")
    ctx.state["issue_text"] = out
    return StepResult(True, summary=f"fetched issue #{issue}",
                      outputs={"state_updates": {"issue_text": out}})


async def _maybe_moa_draft(ctx, *, step_name: str, purpose: str, guidance: str,
                           extension: dict, material: str):
    """MoA for issue answering (design W6 — issues have no lens ensemble):
    when eligible with >=2 members, N member proposers draft concurrently over
    the same evidence (STRICT budget: a refused reservation SKIPS the member —
    no per-member legacy drafts) and one tier-model aggregator synthesizes the
    slot-structured answer, its call reserved from the same cap. Fallbacks:
    zero successful proposals => None (caller runs exactly ONE legacy tier
    draft); aggregator unreservable/failed/invalid => the first proposal.
    Returns (StepResult, output) or None for the legacy path."""
    import asyncio
    import json as _json

    from ..agent_runtime import run_agent_step
    from ..agent_runtime.moa import (
        Member,
        MoaBudget,
        MoaBudgetExceeded,
        moa_eligible,
        resolve_members,
    )
    from ..agent_runtime.utils import _to_step_result

    spec = ctx.state.get("task_spec") or {}
    if not moa_eligible(ctx.settings, kind=str(spec.get("kind", "")),
                        mode=str(spec.get("mode", "eco"))):
        return None
    members = resolve_members(ctx.settings)
    if len(members) < 2:
        return None
    budget = MoaBudget.start(ctx.settings)
    ctx.trace.record("moa_dispatch", step=step_name,
                     members=[m.label() for m in members],
                     max_usd=ctx.settings.moa_max_usd)

    async def _propose(m):
        try:
            route = ({"harness_member": m} if m.provider else
                     {"llm_override": ctx.llm.for_member(m)})
            with budget.bind(m):
                return await run_agent_step(
                    ctx, step_name=f"{step_name}#moa/{m.model}", purpose=purpose,
                    guidance=guidance, evidence={"issue_text": material},
                    output_extension=extension,
                    extra_tools=_gh_read_tools(_repo_path(ctx)),
                    max_iters=ctx.settings.max_agent_iters,
                    model_override=m.model, **route)
        except (MoaBudgetExceeded, Exception) as exc:
            ctx.trace.record("moa_member_dropped", member=m.label(),
                             error=f"{type(exc).__name__}: {exc}"[:200])
            return None

    runs = await asyncio.gather(*(_propose(m) for m in members))
    proposals = [(r, o) for r_o in runs if r_o is not None
                 for r, o in [r_o] if o and o.get("status") == "success"]
    if not proposals:
        return None  # exactly ONE legacy tier draft (the pre-MoA baseline)
    _tt = ctx.settings.tier_target(spec.get("mode", "eco"))
    tier_model = _tt.model
    agg = ctx.llm.for_target(_tt) if hasattr(ctx.llm, "for_target") else ctx.llm
    contract_desc = _json.dumps({k: v for k, v in extension.items()},
                                ensure_ascii=False)
    prompt = ("Synthesize ONE best answer from these independent proposals "
              "(prefer agreement; keep the strongest evidence; fill every "
              "slot you can). Proposals:\n\n"
              + "\n\n---\n\n".join(_json.dumps(
                  {k: o.get(k) for k in ("answer_draft", *extension)},
                  ensure_ascii=False)[:8000] for _, o in proposals)
              + "\n\n## ISSUE\n<untrusted_data>\n" + material[:20_000]
              + "\n</untrusted_data>\n\nYour FINAL message must be one JSON "
              "object with fields: " + contract_desc
              + ' plus {"status": "success", "summary": "...", '
              '"confidence": "high|medium|low"}')
    try:
        from ...llm import parse_json_reply

        with budget.bind(Member(model=tier_model)):
            reply = agg.create(system="You are the synthesis aggregator for a "
                                      "mixture of independent issue-answer drafts.",
                               messages=[{"role": "user", "content": prompt}],
                               model=tier_model, role="moa_aggregator",
                               max_tokens=ctx.settings.llm_max_tokens)
        merged = parse_json_reply(reply.text or "")
    except (MoaBudgetExceeded, Exception):
        merged = None
    if not isinstance(merged, dict) or not any(
            merged.get(k) for k in ("answer_draft", "root_cause")):
        result, output = proposals[0]  # aggregator failed — first proposal
        ctx.trace.record("moa_aggregator_fallback", step=step_name)
        return result, output
    merged.setdefault("status", "success")
    merged.setdefault("summary", "MoA-synthesized answer "
                                 f"({len(proposals)} proposals)")
    return _to_step_result(merged, f"[moa x{len(proposals)}] "), merged


def _issue_agent_step(step_name: str, purpose: str, guidance: str,
                      extension: dict, render):
    """Issue-facing agent steps on the unified runtime (修正方案 P1)."""

    async def handler(ctx: StepContext) -> StepResult:
        """Run the configured issue agent step on the unified runtime, reading
        `issue_text` from state as evidence (BLOCKED when absent) and granting the
        read-only gh tools. On success, `render` turns the agent output into a
        (state key, text) pair which is stored and published (B2 `state_updates`,
        e.g. `draft_answer` / `triage_table`); the draft is never auto-posted."""
        from ..agent_runtime import run_agent_step

        material = ctx.state.get("issue_text", "")
        if not material:
            return StepResult(False, FailureKind.BLOCKED, "no issue_text in state")
        moa_result = None
        if step_name == "agent.draft_issue_answer":
            moa_result = await _maybe_moa_draft(
                ctx, step_name=step_name, purpose=purpose, guidance=guidance,
                extension=extension, material=str(material))
        if moa_result is not None:
            result, output = moa_result
        else:
            result, output = await run_agent_step(
                ctx, step_name=step_name, purpose=purpose, guidance=guidance,
                evidence={"issue_text": str(material)},
                output_extension=extension,
                extra_tools=_gh_read_tools(_repo_path(ctx)),
                max_iters=ctx.settings.max_agent_iters,  # T3: the default review
                # budget left ~zero headroom for grep-heavy triage (issue4842
                # blocked 2/3 replicates at the cap)
            )
        if result.ok:
            key, text = render(output)
            ctx.state[key] = text
            result.outputs[key] = text
            result.outputs.setdefault("state_updates", {})[key] = text
            result.summary = f"{key} produced — {result.summary}"
        elif result.failure is FailureKind.ESCALATE:
            # An incomplete-but-substantive draft ships with caveats instead of
            # being discarded (eval: 3 escalated runs held correct diagnoses and
            # delivered nothing). Empty/thin drafts still escalate. The prose
            # floor alone is wrong for triage: a complete one-row triage table
            # renders under 200 chars, so structured rows count as substantive
            # on their own (a high-confidence needs_review triage of issue5123
            # was discarded and blocked the run).
            key, text = render(output)
            if output.get("triage_table") or len(text.strip()) > 300:
                text = (f"> ⚠ draft shipped with caveats — agent self-assessed "
                        f"confidence: {output.get('confidence', 'low')}; "
                        f"verify before relying on it.\n\n{text}")
                ctx.state[key] = text
                result = StepResult(
                    True, summary=f"{key} produced with caveats — {result.summary}",
                    outputs={**result.outputs, key: text},
                    changed_files=result.changed_files)
                result.outputs.setdefault("state_updates", {})[key] = text
        return result

    return handler


_SLOT_ORDER = (("root_cause", "Root cause"), ("fix", "Fix"),
               ("workaround", "Workaround"), ("preconditions", "Preconditions"),
               ("verification", "Verification"), ("prevention", "Prevention"),
               ("disposition", "Disposition"))


def _render_answer(output: dict) -> tuple[str, str]:
    """Render the draft-answer agent output into the `("draft_answer", text)`
    pair. Structured slots (W3: root_cause/fix/workaround/preconditions/
    verification/prevention/disposition) are the canonical source when the
    CORE is present (root_cause AND (fix OR disposition)) — the draft is
    NEVER discarded: draft paragraphs not already contained in a slot append
    under "Additional context". With only peripheral slots, the draft stays
    the body and present slots append as labeled sections (same containment
    dedup). No slots ⇒ the old contract: `answer_draft`, else salvaged raw
    text, else `summary`. Appends an epistemics caveat when merge-state
    claims lack a gh verification call (forensics #7)."""
    slots = {k: str(output.get(k) or "").strip() for k, _ in _SLOT_ORDER}
    draft = str(output.get("answer_draft") or output.get("_raw_text")
                or output.get("summary", ""))
    filled = {k: v for k, v in slots.items() if v}
    core = bool(slots["root_cause"]) and bool(slots["fix"] or slots["disposition"])

    def _sections(keys) -> str:
        return "\n\n".join(f"### {title}\n{slots[k]}"
                           for k, title in _SLOT_ORDER if k in keys)

    def _leftover_paras(body: str) -> list[str]:
        """Draft paragraphs not contained verbatim in any slot (non-lossy)."""
        slot_text = "\n".join(filled.values()).lower()
        return [p for p in body.split("\n\n")
                if p.strip() and p.strip().lower() not in slot_text]

    if core:
        text = _sections(filled)
        extra = _leftover_paras(draft)
        if extra:
            text += "\n\n### Additional context\n" + "\n\n".join(extra)
    elif filled:
        text = draft
        for k, title in _SLOT_ORDER:
            if k in filled and slots[k].lower() not in draft.lower():
                text += f"\n\n**{title}:** {slots[k]}"
    else:
        text = draft
        disp = str(output.get("disposition") or "").strip()
        if disp and disp.lower() not in text.lower():
            text += f"\n\n**Disposition:** {disp}"
    tools_used = output.get("_tools_used") or []
    lowered = text.lower()
    if (("merged" in lowered or "on main" in lowered)
            and "gh_pr_view" not in tools_used):
        text += ("\n\n> ⚠ merge-state statements above were not verified via "
                 "gh this run — treat as unconfirmed.")
    return "draft_answer", text


def _render_triage(output: dict) -> tuple[str, str]:
    """Render the triage agent's `triage_table` rows into the
    `("triage_table", markdown)` pair — a markdown table (issue/type/module/
    priority/labels). Falls back to the agent's `summary` when there are no rows."""
    rows = output.get("triage_table") or []
    if not rows:
        return "triage_table", str(output.get("summary", ""))
    lines = ["| Issue | Type | Module | Priority | Labels |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| #{r.get('number', '?')} {str(r.get('title', ''))[:60]} | "
                     f"{r.get('type', '?')} | {r.get('module', '?')} | "
                     f"{r.get('priority', '?')} | "
                     f"{', '.join(r.get('labels') or [])} |")
    return "triage_table", "\n".join(lines)


register_step(StepSpec(
    "agent.draft_issue_answer", "agent", "read",
    _issue_agent_step(
        "agent.draft_issue_answer",
        "Draft a helpful, factual answer to the repository issue.",
        "Ground every claim in the issue text or code you actually "
        "read (use your repo tools). Never invent APIs; say plainly "
        "when unsure. Check gh_issue_timeline for PRs/commits that "
        "already reference this issue before diagnosing from scratch. "
        "FINAL CHECKLIST before answering: re-read the title, body and "
        "every comment; confirm each question the reporter asked is "
        "addressed; state the preconditions your fix needs to work; if "
        "this failure is a footgun others will hit, add a prevention "
        "suggestion. The draft is never auto-posted.",
        {"answer_draft": "the complete draft reply (markdown)",
         "root_cause": "the diagnosed root cause, with file:line evidence",
         "fix": "the concrete fix or correct invocation",
         "workaround": "interim workaround if the fix is not immediate, else empty",
         "preconditions": "what must hold for the fix to work (weights, "
                          "hardware, versions), else empty",
         "verification": "exact command/check proving the fix worked",
         "prevention": "guard/warning/docs change preventing recurrence, "
                       "else empty",
         "disposition": "close / keep-open / duplicate-of-#N / needs-info — "
                        "match the thread's last maintainer action; include "
                        "the reopen condition"},
        _render_answer),
    "Draft an issue answer (governed agent step; never auto-posted)."))

register_step(StepSpec(
    "agent.triage_issues", "agent", "read",
    _issue_agent_step(
        "agent.triage_issues",
        "Triage the GitHub issues: classify each and route it.",
        "For each issue: type (bug/feature/question), affected "
        "module (verify module paths with repo tools when unsure), "
        "priority, suggested labels.",
        {"triage_table":
         "list of {number, title, type, module, priority, labels}"},
        _render_triage),
    "Classify/label/route issues (governed agent step, read-only)."))

register_step(StepSpec(
    "issue.post_answer", "script", "push",
    _post_step("draft_answer",
               lambda spec, body: ["issue", "comment", str(spec.get("issue")),
                                    "--body", body],
               "issue answer"),
    "Post the drafted answer (explicit post flag + ALLOW_POST)."))
