"""`run_agent_step_ensemble` — a robustness wrapper over `run_agent_step`.

A single agent-step run has high variance (each run samples one corner of its
checklist). The ensemble runs the same step once per perspective lens, unions
the list-valued `merge_key` items, and reduces them with one verify-and-merge
call: cross-lens consensus is kept, single-lens items must survive verification
against the evidence, misreads are dropped, kept items are rewritten to be
self-contained. Generalizes to any agent step whose extension output is a list.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ...providers.outcomes import requires_reconciliation
from ...llm import parse_json_reply
from ...scopes import ToolScope
from ..step import FailureKind, StepContext, StepResult
from .runner import run_agent_step
from .utils import _build_evidence, _to_step_result


def _fallback_allowed(error: Exception) -> bool:
    """An unresolved original call cannot authorize another paid dispatch."""
    return (getattr(error, "allow_fallback", True) is not False
            and not requires_reconciliation(error))


def outcome_blocked(result: Any, output: dict | None) -> bool:
    """True when a pass returned nothing usable — no items and no tool work.

    A harness session that fails at the transport (auth, quota, CLI error)
    surfaces here as a contract-shaped `status: blocked` with zero tokens and
    zero tool calls, which is indistinguishable from a shy model unless the
    counters are checked. Callers use it to tell "this seat did not run" from
    "this seat ran and found nothing"."""
    out = output or {}
    if out.get("_tools_used"):
        return False
    status = str(out.get("status") or "").lower()
    return status in ("blocked", "failed") or not getattr(result, "ok", True)


def member_unreachable(result: Any, output: dict | None) -> bool:
    """True only when the seat DID NOT RUN — a transport-level death.

    Narrower than `outcome_blocked`, and the difference is load-bearing. A
    transport failure surfaces as a CONTRACT-SHAPED `blocked`/`failed` output
    with no tool work. An empty `output` ({}) is a different animal: it is the
    one path where `_coerce_output` returned None (`runner.py`), reached when a
    pass ran its tool loop and then burned the whole completion ceiling — the
    seat is alive and merely truncated. `outcome_blocked` answers True for both
    (a falsy `output` carries `result.ok=False`), so routing decisions must use
    THIS predicate: rerouting a truncated seat to the tier model silently
    changes the arm's configuration, which is exactly the 2026-08-16 defect the
    route-preservation invariant exists to prevent."""
    out = output or {}
    if not out:
        return False            # ran, no usable final — keep its route, re-ask
    return outcome_blocked(result, out)


def lens_backend_member(settings: Any, lens_name: str):
    """The harness Member a pass/lens should ride per
    `settings.review_lens_backends`, or None for the run's normal backend.

    Map values are "provider:model" or a bare provider id (model then comes
    from `strict_backend_model`). Role-split composition: breadth passes on a
    subscription CLI, precision roles on the tier model — the measured
    complementarity (Composer carries recall, DS carries precision) on the
    same pipeline. MoA members take precedence at the call site (an explicit
    mixture is a stronger instruction than the static map)."""
    spec = (getattr(settings, "review_lens_backends", None) or {}).get(lens_name)
    if not spec:
        return None
    from .moa import Member

    provider, _, model = str(spec).partition(":")
    provider = provider.strip()
    model = model.strip() or str(getattr(settings, "strict_backend_model", ""))
    if not provider or not model:
        return None
    return Member(model=model, provider=provider)


async def run_agent_step_ensemble(
    ctx: StepContext,
    *,
    step_name: str,
    purpose: str,
    evidence: dict[str, str],
    lenses: list[dict],
    merge_key: str,
    guidance: str = "",
    expected: str = "",
    output_extension: dict[str, str] | None = None,
    scope: ToolScope | None = None,
    extra_tools: dict[str, Any] | None = None,
    max_iters: int | None = None,
    merge_guidance: str = "",
) -> tuple[StepResult, dict]:
    """Robustness wrapper over `run_agent_step`: perspective-diverse fan-out,
    then a verify-and-merge reduction.

    A single agent-step run has high variance — each run samples one corner of
    its checklist (eval/ANALYSIS.md: RQS 0.11-0.38 across identical configs,
    while the UNION of samples covered 5/8 ground-truth issues). The ensemble
    runs the same step once per lens (a depth priority, not a restriction),
    unions the list-valued `merge_key` items, and reduces them with one
    verify-and-merge call: cross-lens consensus is kept, single-lens items must
    survive verification against the evidence, misreads are dropped, and every
    kept item is rewritten to be self-contained and evidence-grounded.

    Generalizes to any agent step whose extension output is a list — review
    comments, triage rows, debug hypotheses — lenses and merge_guidance are the
    only step-specific inputs.
    """
    if ctx.llm is None or not ctx.llm.available:
        return (StepResult(False, FailureKind.BLOCKED,
                           "LLM not configured — cannot run agent step"), {})

    budget = max_iters or ctx.settings.ensemble_lens_max_iters
    k = max(1, int(ctx.settings.ensemble_samples_per_lens))

    # MoA (design W6): heterogeneous member proposers on qualified runs — the
    # verify-and-merge reducer stays on the tier model. Member failures fall
    # back per-lens to the tier model; a whole-mixture failure IS the existing
    # tier-model ensemble (nothing below can make the run worse than today).
    from .moa import MoaBudget, moa_eligible, resolve_members

    spec0 = ctx.state.get("task_spec") or {}
    moa_members: list = []
    moa_budget = None
    if moa_eligible(ctx.settings, kind=str(spec0.get("kind", "")),
                    mode=str(spec0.get("mode", "eco")),
                    depth=str(ctx.state.get("_review_depth", ""))):
        moa_members = resolve_members(ctx.settings)
        if len(moa_members) < 2:
            moa_members = []
        else:
            moa_budget = MoaBudget.start(ctx.settings)
            ctx.trace.record(
                "moa_dispatch", step=step_name,
                members=[m.label() for m in moa_members],
                assignment={str(lens["name"]): moa_members[i % len(moa_members)].label()
                            for i, lens in enumerate(lenses)},
                max_usd=ctx.settings.moa_max_usd)  # NO token counts here (W7)

    def _lens_overrides(lens_i: int, lens_name: str = "") -> dict:
        if moa_members:
            m = moa_members[lens_i % len(moa_members)]
            return ({"harness_member": m, "model_override": m.model} if m.provider else
                    {"llm_override": ctx.llm.for_member(m), "model_override": m.model})
        m = lens_backend_member(ctx.settings, lens_name)
        if m is not None:
            return {"harness_member": m, "model_override": m.model}
        return {}

    async def _one_lens(lens: dict, j: int, idx: int = 0,
                        lens_i: int = 0) -> tuple[str, StepResult, dict]:
        """Run `run_agent_step` once for a single lens (sample `j`), instructing
        the agent to go DEEP on that lens as its depth priority while peers cover
        the rest. `idx` staggers concurrent starts: lenses share a byte-identical
        prompt prefix (tools+system+dispatch render), so giving the first lens a
        head start lets the provider cache the prefix before the siblings send
        it — simultaneous identical prefixes all miss. Returns the lens name,
        the StepResult, and the output dict."""
        if idx and ctx.settings.ensemble_stagger_seconds > 0:
            await asyncio.sleep(idx * ctx.settings.ensemble_stagger_seconds)
        suffix = f"/{j}" if k > 1 else ""
        lens_guidance = (
            f"{guidance}\n\n## Your assigned lens: {lens['name']}\n"
            f"{lens['focus']}\nGo DEEP on this lens — it is your depth "
            "priority; report other issues only if they surface on the way. "
            "Peer agents cover the other lenses.")
        overrides = _lens_overrides(lens_i, str(lens.get("name") or ""))
        # `route` is the EFFECTIVE route. It starts as the seat's routing and
        # is emptied only once the member is PROVEN dead, so a seat that merely
        # found nothing keeps its route (the 2026-08-16 invariant) while a seat
        # that could not run stops being re-tried.
        route = dict(overrides)
        member = str(overrides.get("model_override") or "") if overrides else ""

        async def _attempt(step_suffix: str, guidance_text: str, route: dict):
            from contextlib import nullcontext

            account = (moa_budget.bind(moa_members[lens_i % len(moa_members)])
                       if moa_members and route else nullcontext())
            with account:
                return await run_agent_step(
                    ctx, step_name=f"{step_name}#{lens['name']}{suffix}{step_suffix}",
                    purpose=purpose,
                    evidence=evidence, guidance=guidance_text, expected=expected,
                    output_extension=output_extension, scope=scope,
                    extra_tools=extra_tools, max_iters=budget, **route)

        def _member_died(phase: str, reason: str) -> None:
            """Record a route change. NEVER silent: an arm mislabelled after
            the fact is how 18 of 28 holdout seats once degraded invisibly.
            Both route identities are CONCRETE — "effective: tier" alone does
            not say what actually served the seat, and when the member and the
            tier resolve to the same model the fallback changes nothing at
            all, which a reader must be able to see."""
            tier_model = ""
            try:
                tier_model = str(ctx.settings.tier_target(
                    str(spec0.get("mode", "eco"))).model or "")
            except Exception:  # noqa: BLE001 — telemetry must never sink a run
                tier_model = ""
            ctx.trace.record("moa_member_fallback", lens=str(lens["name"]),
                             phase=phase, member=member,
                             requested=member, effective=tier_model or "tier",
                             same_model=bool(member and member == tier_model),
                             reason=reason[:200],
                             error=reason[:200])  # kept: existing consumers

        # -- first pass. A dead member arrives in TWO shapes: raw-API members
        # raise, while harness transports convert a dead session into an empty
        # typed outcome ("a dead harness is an outcome"). Both mean the seat did
        # not run, so both fall back to the tier model exactly once.
        try:
            result, output = await _attempt("", lens_guidance, route)
        except Exception as exc:
            if not route or not _fallback_allowed(exc):
                raise
            _member_died("first", f"{type(exc).__name__}: {exc}")
            route = {}
            result, output = await _attempt("/tier", lens_guidance, route)
        else:
            if route and member_unreachable(result, output):
                _member_died("first", "typed non-OK outcome — the seat did "
                                      "not run (harness transports do not raise)")
                route = {}
                result, output = await _attempt("/tier", lens_guidance, route)
        # `output` is {} when the lens produced no contract-conformant final —
        # the ONLY path where _coerce_output returns None, reached when a pass
        # burns its whole completion ceiling and returns empty text. Both
        # retries below used to require a truthy `output`, so that case fell
        # through BOTH and the lens was dropped silently: measured 2026-08-16,
        # five whole review passes and ~2.1M input tokens of investigation
        # discarded across one holdout, three of them on its worst item.
        # A tier fallback that ALSO did not run has nothing left to fall back
        # to: re-asking it duplicates cost and buries the typed failure the
        # caller needs to see. (A tier fallback that RAN and merely found
        # nothing is a normal quiet pass and still earns its one re-ask.)
        # Nothing left to change: the pass that just ran DID NOT RUN and there
        # is no route left to fall back from (the member was already cleared,
        # or there never was one). Re-asking it duplicates cost and buries the
        # typed failure the caller needs. A TRUNCATED seat is not this case —
        # it ran, so it still earns its one re-ask, on its own route.
        nothing_left = member_unreachable(result, output) and not route
        if (ctx.settings.ensemble_zero_yield_retry
                and not nothing_left
                and not ((output or {}).get(merge_key) or [])):
            # zero-yield lens: one cheap single-lens re-ask beats the full
            # 8-lens ensemble retry it used to trigger (T3 forensics #6).
            # The retry KEEPS this lens's routing whenever the seat actually
            # RAN: dropping it silently moved a routed seat onto the default
            # backend, so an arm labelled "Fable in the adversary seat" was
            # measured running DeepSeek there — the seat's whole purpose,
            # erased without a trace signal (2026-08-16: a quota exhaustion
            # made every routed session return is_error, and 18 of 28 holdout
            # seats degraded this way while the run still reported success).
            # It does NOT re-try a member already proven dead above: `route`
            # is empty by then, and re-asking a 403 seat only sank the run.
            seat_dead = member_unreachable(result, output) and bool(overrides)
            ctx.trace.record("lens_zero_yield_retry", lens=str(lens["name"]),
                             routed=bool(route), seat_failed=seat_dead)
            if seat_dead:
                # a ROUTED seat that produced nothing at all is a capability
                # gap, not a shy model — say so loudly enough that an arm
                # cannot be mislabelled after the fact
                ctx.trace.record(
                    "capability_gap", capability="review.routed_seat",
                    step=f"{step_name}#{lens['name']}{suffix}",
                    effect="routed seat produced no output; retrying on "
                           + (f"the same route (model={member})" if route
                              else "the tier model (member proven dead)"))
            retry_guidance = lens_guidance + (
                "\n\nYour first pass produced NO USABLE FINAL — most "
                "likely it ran past the reply ceiling. Investigate less "
                "and FILE: your final JSON must fit, so keep evidence to "
                "one quoted line per comment and findings to 15 lines."
                if not output else
                "\n\nYour first pass yielded zero candidates. Re-check "
                "your two highest-risk hunks; emit every plausible "
                "candidate (do not self-censor) or a [validated] finding "
                "for each checklist item you cleared.")
            # The retry is guarded exactly like the first pass — it used to be
            # bare, so a routed member that died here raised straight through
            # asyncio.gather and failed the whole step. Whichever shape the
            # failure takes, it falls back to tier ONCE; a tier result that is
            # itself non-OK is returned as the typed failure, never re-tried.
            try:
                result, output = await _attempt("/retry", retry_guidance, route)
            except Exception as exc:
                if not route or not _fallback_allowed(exc):
                    raise
                _member_died("retry", f"{type(exc).__name__}: {exc}")
                route = {}
                result, output = await _attempt("/retry/tier", retry_guidance,
                                                route)
            else:
                if route and member_unreachable(result, output):
                    _member_died("retry", "typed non-OK outcome on the retry")
                    route = {}
                    result, output = await _attempt("/retry/tier",
                                                    retry_guidance, route)
        return str(lens["name"]), result, output

    # lenses (and repeat samples of each lens — a single sample's item list is
    # the highest-variance link, so the union over samples is the recall
    # floor) are independent by construction — run them concurrently; the
    # sequential ensemble's wall-clock was its whole efficiency penalty
    jobs = [(li, lens, j) for li, lens in enumerate(lenses)
            for j in range(1, k + 1)]
    if ctx.settings.ensemble_parallel and len(jobs) > 1:
        runs = await asyncio.gather(
            *(_one_lens(lens, j, idx, li)
              for idx, (li, lens, j) in enumerate(jobs)))
    else:
        runs = [await _one_lens(lens, j, lens_i=li) for li, lens, j in jobs]
    samples = [(name, output) for name, _, output in runs if output]
    last_result = runs[-1][1] if runs else None
    if not samples:
        return (last_result or StepResult(
            False, FailureKind.RETRYABLE,
            f"{step_name}: all ensemble samples failed"), {})

    # collapse EXACT duplicates before numbering — identical items from repeat
    # samples carry consensus, not new information
    candidates: list[dict] = []
    by_sig: dict[str, dict] = {}
    for name, output in samples:
        for item in output.get(merge_key) or []:
            base = dict(item) if isinstance(item, dict) else {"item": item}
            sig = json.dumps(base, ensure_ascii=False, sort_keys=True)
            if sig in by_sig:
                tagged = by_sig[sig]
                tagged["consensus"] += 1
                if name not in tagged["lenses"]:
                    tagged["lenses"].append(name)
            else:
                tagged = {**base, "lenses": [name], "consensus": 1}
                by_sig[sig] = tagged
                candidates.append(tagged)

    # near-duplicate corroboration across lenses: the same concern surfaces at
    # slightly different lines/wording, which exact-signature dedup cannot see
    # — consensus stays 1 and the reducer treats independent agreement as
    # noise (a 2-lens candidate was dropped as "unlikely" in a live run whose
    # ground truth confirmed it). Tag file/line-proximate agreement so the
    # reducer's drop rule can weigh it.
    for i, a in enumerate(candidates):
        fa, la = str(a.get("file", "")), a.get("line")
        if not fa or not isinstance(la, int):
            continue
        others = {name
                  for j, b in enumerate(candidates) if j != i
                  for name in b.get("lenses") or []
                  if str(b.get("file", "")) == fa
                  and isinstance(b.get("line"), int)
                  and abs(b["line"] - la) <= 8
                  and name not in (a.get("lenses") or [])}
        if others:
            a["corroborated_by"] = sorted(others)

    def _dedup_union(key: str) -> list:
        """Union the list-valued `key` across all samples, dropping exact
        duplicates (dict/list items compared by canonical JSON, scalars by str),
        preserving first-seen order. Used to merge the deterministic base fields
        that the reducer does not judge."""
        out, seen = [], set()
        for _, o in samples:
            for v in o.get(key) or []:
                sig = json.dumps(v, ensure_ascii=False, sort_keys=True) \
                    if isinstance(v, (dict, list)) else str(v)
                if sig not in seen:
                    seen.add(sig)
                    out.append(v)
        return out

    # base fields are merged DETERMINISTICALLY — the reducer only judges the
    # merge_key items (asking it to re-emit the whole contract truncated the
    # items behind verbose scalar fields in live runs)
    merged: dict = dict(samples[0][1])
    for key in ("findings", "files_read", "files_modified", "tests_requested",
                "tests_run", "assumptions", "blockers"):
        merged[key] = _dedup_union(key)
    # the reducer judges ITEMS, not the step: models conflate the step's status
    # with the reviewed artifact's verdict, so status comes from the samples
    if any(o.get("status") == "success" for _, o in samples):
        merged["status"] = "success"

    if len(candidates) <= 2 and all(c.get("consensus", 1) >= 2
                                    for c in candidates):
        # fast path: a small union whose every item was independently
        # replicated across samples needs no arbitration — skip the
        # reducer's latency. A single-lens singleton does NOT qualify: an
        # unreplicated claim must still face verification (a hallucinated
        # blocker once sailed through here and became the entire review)
        # `corroborated_by` survives into the merged items: downstream comment
        # budgeting ranks independently-corroborated findings above singletons
        merged[merge_key] = [
            {k: v for k, v in c.items() if k not in ("lenses", "consensus")}
            for c in candidates]
        ctx.trace.record("agent_ensemble", step=step_name,
                         lenses=[name for name, _ in samples],
                         candidates=len(candidates), merged=len(candidates),
                         dropped=0, verified=False)
        return _to_step_result(merged, f"[ensemble x{len(samples)}] "), merged

    # the reducer never re-emits the merged list (free-form regeneration is
    # where findings silently died in live runs): it returns one verdict PER
    # NUMBERED CANDIDATE and code assembles the result deterministically —
    # any candidate it fails to mention is KEPT (fail-open per item)
    def _reduce_contract(verdicts_only: bool) -> dict:
        """The verdict schema, optionally without the free-text rewrite field.

        `comment` is nominally optional, but models emit it for every survivor, which
        makes the reducer's output grow with candidate count times comment length —
        unbounded, while `max_tokens` is fixed. Dropping the field is what makes the
        retry below fit: ~50 tokens per candidate instead of ~800.
        """
        verdict = ('list of {"i": candidate index, "action": '
                   '"keep"|"drop"|"dup", "of": null | index this '
                   'duplicates, "severity": optional corrected severity, ')
        if not verdicts_only:
            verdict += '"comment": optional self-contained rewrite, '
        return {"verdicts": verdict + '"why": one line}',
                "summary": "one-paragraph merged outcome"}

    reduce_contract = _reduce_contract(False)
    # the reducer verifies against the evidence, so it needs far more of it
    # than the per-lens dispatch cap (the lenses had tools; the reducer has none)
    capped, _ = _build_evidence(ctx, evidence,
                                cap=ctx.settings.ensemble_merge_evidence_chars)
    ev_text = "\n\n".join(f"### {k}\n{v}" for k, v in capped.items())
    _merge_rule3 = (
        "3. action=keep otherwise; use 'comment' to rewrite the item so it "
        "is self-contained and verifiable: FIRST the concrete fact from the "
        "evidence it is grounded in, THEN the directive (what to change, "
        "where, why). Fix line numbers to lines actually visible in the "
        "evidence. Unmentioned candidates are kept unchanged.\n")
    _merge_rule3_verdicts_only = (
        "3. action=keep otherwise. Do NOT rewrite any text: omit 'comment' "
        "entirely and the original wording is reused verbatim. Emit ONLY the "
        "verdict fields — your previous reply was cut off by the output limit, "
        "so brevity per verdict is what lets every candidate be judged. "
        "Unmentioned candidates are kept unchanged.\n")

    _MERGE_SYSTEM_HEAD = (
        "You are the verify-and-merge reducer for an ensemble of independent "
        "agent samples of the same step. You receive their NUMBERED candidate "
        "items (tagged with the lens(es) that produced each and a consensus "
        "count) plus the evidence. Emit one verdict per candidate index:\n"
        "1. VERIFY each candidate, then action=drop the ones that fail: "
        "misreads of the evidence, claims the evidence already handles, "
        "vague items, cited evidence that contradicts the diff (e.g. a "
        "quoted stacktrace or file content inconsistent with the shown "
        "change), and pure stylistic alternatives to correct behavior — "
        "give the why. IMPORTANT: the candidates come from agents that had "
        "repo tools; you hold only this evidence pack. A claim grounded in "
        "cited repo evidence (a named file/grep and what it showed) is "
        "judged on coherence and specificity — NEVER drop it merely because "
        "the cited file is outside your evidence. A candidate with "
        "consensus >= 2 needs a concrete refutation to drop. A verified "
        "item is never dropped just for low severity.\n"
        "2. action=dup with of=<index> when two candidates report the same "
        "underlying issue — rewrite the surviving index with the most "
        "precise phrasing and the highest severity of the pair.\n")

    def _merge_system(verdicts_only: bool = False) -> str:
        return (_MERGE_SYSTEM_HEAD
                + (_merge_rule3_verdicts_only if verdicts_only else _merge_rule3)
                + (f"4. {merge_guidance}\n" if merge_guidance else "")
                + "\nYour FINAL message must be a single JSON object with exactly "
                "these fields:\n"
                + json.dumps(_reduce_contract(verdicts_only), ensure_ascii=False))

    merge_system = _merge_system()
    numbered = [{"i": i, **c} for i, c in enumerate(candidates)]
    # single untooled reduction call: a tool-looped reducer measurably
    # over-dropped (reviews shrank to 1-2 comments, starving recall and
    # flipping verdicts) and added 1-2 min of latency; repo-cited claims are
    # judged on coherence, not re-derived
    merge_prompt = (
        f"## CANDIDATE ITEMS ({merge_key}, from {len(samples)} samples)\n"
        + json.dumps(numbered, ensure_ascii=False, indent=1)
        + f"\n\n## EVIDENCE\n{ev_text}")
    # the reducer is agent reasoning too — it rides the run's dual-path tier
    # BACKEND (lens agents already do via run_agent_step; without this the
    # merge silently fell back to agent_model on performance runs)
    _tt = ctx.settings.tier_target(
        (ctx.state.get("task_spec") or {}).get("mode", "eco"))
    tier_model = _tt.model
    reducer_llm = (ctx.llm.for_target(_tt)
                   if hasattr(ctx.llm, "for_target") else ctx.llm)
    reply = await asyncio.to_thread(
        reducer_llm.create, system=merge_system,
        messages=[{"role": "user", "content": merge_prompt}],
        model=tier_model, role="reducer",
        max_tokens=max(6000, ctx.settings.llm_max_tokens))
    reply_text = reply.text
    reduce_in = (reply.usage or {}).get("input_tokens", 0)
    reduce_out = (reply.usage or {}).get("output_tokens", 0)
    # `max_tokens` means the JSON was cut mid-object, which is a different failure
    # from a model that answered in prose — and the only one the repair round below
    # cannot fix, since it re-sends the oversized draft asking to keep all substance
    # under the same ceiling. Measured on a live arm: 11 of 14 reductions stopped at
    # exactly llm_max_tokens, none parsed, and every non-truncated reply parsed fine.
    truncated = str(getattr(reply, "stop_reason", "")) == "max_tokens"
    retry_text = None

    def _ok(obj) -> bool:
        return isinstance(obj, dict) and isinstance(obj.get("verdicts"), list)

    reduced = parse_json_reply(reply_text or "")
    if not _ok(reduced):
        reduced = None
        if truncated:
            # Retry the WHOLE reduction with the rewrite field removed rather than
            # repairing the fragment: verdicts must be judged against every candidate
            # at once (a `dup` verdict names another index), so batching the
            # candidates to fit would silently break duplicate detection instead.
            ctx.trace.record(
                "lens_reduce_truncated", step=step_name,
                candidates=len(candidates),
                output_tokens=(reply.usage or {}).get("output_tokens", 0))
            # candidates-only retry: re-sending the full evidence pack made
            # the retry die at max_tokens exactly like the first attempt on a
            # 19-candidate/316KB item (wave-3 pr5691 — both calls burned the
            # whole ceiling reasoning over the evidence, the union shipped
            # with duplicate findings, and precision collapsed). Dup/keep
            # decisions are judged on candidate-text coherence alone here.
            retry_prompt = (
                f"## CANDIDATE ITEMS ({merge_key}, from {len(samples)} "
                "samples)\n"
                + json.dumps(numbered, ensure_ascii=False, indent=1)
                + "\n\n(No evidence pack on this retry — judge duplicates "
                "and internal coherence from the candidate texts alone; "
                "drop only self-contradictory or vacuous items.)")
            retry = await asyncio.to_thread(
                reducer_llm.create, system=_merge_system(verdicts_only=True),
                messages=[{"role": "user", "content": retry_prompt}],
                model=tier_model, role="reducer",
                max_tokens=max(6000, ctx.settings.llm_max_tokens))
            retry_text = retry.text
            reduce_in += (retry.usage or {}).get("input_tokens", 0)
            reduce_out += (retry.usage or {}).get("output_tokens", 0)
            obj = parse_json_reply(retry_text or "")
            if _ok(obj):
                reduced = obj
        elif (reply_text or "").strip():  # one repair round (never on empty text)
            fix = reducer_llm.create(
                system=("Convert the draft into a single JSON object matching "
                        "this contract exactly (keep all substance):\n"
                        + json.dumps(reduce_contract, ensure_ascii=False)),
                messages=[{"role": "user", "content": str(reply_text)[:20_000]}],
                model=tier_model,
                max_tokens=max(6000, ctx.settings.llm_max_tokens))
            reduce_in += (fix.usage or {}).get("input_tokens", 0)
            reduce_out += (fix.usage or {}).get("output_tokens", 0)
            obj = parse_json_reply(fix.text)
            if _ok(obj):
                reduced = obj
    try:  # archive the reduction exchange — reducer failures are hard to debug
        ctx.run_dir.mkdir(parents=True, exist_ok=True)
        (ctx.run_dir / f"ensemble_{step_name.replace('/', '_')}.json").write_text(
            json.dumps({"candidates": numbered, "reply": reply_text,
                        "truncated": truncated, "retry_reply": retry_text},
                       ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    verified = reduced is not None
    kept: dict[int, dict] = {i: dict(c) for i, c in enumerate(candidates)}
    dropped: list[str] = []
    if reduced is not None:
        by_i: dict[int, dict] = {}
        for v in reduced.get("verdicts") or []:
            if isinstance(v, dict) and isinstance(v.get("i"), int) \
                    and 0 <= v["i"] < len(candidates):
                by_i[v["i"]] = v
        for i, v in by_i.items():  # drops first
            if str(v.get("action", "")).lower() == "drop":
                kept.pop(i, None)
                dropped.append(f"[{i}] {v.get('why', '')}")
        for i, v in by_i.items():  # then dups (a dup of a dropped item stays)
            of = v.get("of")
            if str(v.get("action", "")).lower() == "dup" \
                    and isinstance(of, int) and of != i and of in kept \
                    and i in kept:
                kept[of]["consensus"] = kept[of].get("consensus", 1) + 1
                kept.pop(i)
        for i, v in by_i.items():  # then rewrites
            if i in kept:
                if isinstance(v.get("comment"), str) and v["comment"].strip() \
                        and "comment" in kept[i]:
                    kept[i]["comment"] = v["comment"]
                if v.get("severity") and "severity" in kept[i]:
                    kept[i]["severity"] = str(v["severity"])
        merged["summary"] = str(reduced.get("summary")
                                or merged.get("summary") or "")
        if merge_key == "review_comments":
            # Later coverage passes need the selection decisions too. Raw
            # findings alone can otherwise resurrect a concern the reducer
            # already established was harmless or outside this change.
            merged["_rejected_review_comments"] = [
                {"candidate": candidates[i], "reason": str(v.get("why") or "")}
                for i, v in by_i.items()
                if str(v.get("action", "")).lower() == "drop"
            ]
    else:
        merged["summary"] = (f"ensemble of {len(samples)} samples "
                             "(merge reduction failed; unverified union)")
    # near-dup guard (W2): the reducer occasionally re-emits the same finding
    # (observed live: an identical dead-guard comment twice on PR 4810) — drop
    # candidates identical on (file, line, normalized first-120-chars comment).
    # When the merge FAILED outright, collapse aggressively instead: same
    # file within 8 lines = one finding (wave-3 pr5691: a failed merge
    # shipped a raw union where 4 of the 8 rendered slots were two
    # duplicated findings — in the failure path, losing a neighbor finding
    # is cheaper than shipping duplicates).
    seen_sigs: set = set()
    kept_lines: dict[str, list[int]] = {}
    deduped: list[int] = []
    for i in sorted(kept):
        c = kept[i]
        if not any(k in c for k in ("file", "line", "comment")):
            deduped.append(i)   # not review-comment-shaped — never dedup
            continue
        sig = (str(c.get("file", "")), str(c.get("line", "")),
               " ".join(str(c.get("comment", "")).lower().split())[:120])
        if sig in seen_sigs:
            continue
        if not verified:
            fpath = str(c.get("file", ""))
            line = c.get("line")
            if isinstance(line, int) and any(
                    abs(line - n) <= 8 for n in kept_lines.get(fpath, [])):
                continue
            if isinstance(line, int):
                kept_lines.setdefault(fpath, []).append(line)
        seen_sigs.add(sig)
        deduped.append(i)
    merged[merge_key] = [
        {k: v for k, v in kept[i].items() if k not in ("lenses", "consensus")}
        for i in deduped]
    ctx.trace.record(
        "agent_ensemble", step=step_name,
        lenses=[name for name, _ in samples],
        candidates=len(candidates),
        merged=len(merged.get(merge_key) or []),
        dropped=len(dropped),
        verified=verified,
        # reducer-call usage (lens usage is on each lens's agent_output event)
        input_tokens=reduce_in, output_tokens=reduce_out)
    prefix = f"[ensemble x{len(samples)}] "
    return _to_step_result(merged, prefix), merged
