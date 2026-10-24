"""Read-only PR fetch steps: the PR diff and the deterministic gate report.

Both are injectable via state (`diff_text`, `gate_report`) so paths below the
network are offline-testable, and both degrade to BLOCKED (never crash) when
`gh` is unavailable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ... import worktrees
from ...step import FailureKind, StepContext, StepResult
from .._common import from_state, require_repo, step
from .._common import gh as _gh
from .._common import git as _git
from .._common import repo_path as _repo_path


# `_pinned_diff` sentinel: the head is already contained in the base (a merged
# PR), so there is nothing for a local three-dot diff to show. Distinct from a
# real error because the caller's response differs — see `_diff_at_head`.
CONTAINED_HEAD = "head is contained in the base (merged PR)"


def _pinned_refs(run_id: str) -> tuple[str, str]:
    """This run's private `(base_ref, head_ref)` fetch destinations.

    Run-scoped, not PR-scoped. The refspecs below are forced, so a PR-keyed name
    would let two concurrent runs on one PR at different heads overwrite each
    other's "pinned" base between fetch and diff — reintroducing the race this
    whole path removes. They also anchor both commits against a concurrent
    `git gc`, which a FETCH_HEAD-only fetch does not, and live outside
    `refs/heads` / `refs/remotes` so nothing mistakes them for branches."""
    return f"refs/imx/{run_id}/base", f"refs/imx/{run_id}/head"


def _resolve_pr_head(repo: Path, pr: int) -> tuple[str, str, str]:
    """Resolve the PR's `(base_ref, head_sha, error)` in ONE call.

    Everything downstream — the stale gate, the fetch, the diff and the worktree
    — is then governed by this single answer. The old flow asked twice (`gh pr
    diff` resolves the head server-side; `_pr_time_checkout` resolved it again
    via `gh pr view`), so a push landing between them produced a diff and a
    review tree from different commits."""
    code, out = _gh(["pr", "view", str(pr), "--json", "baseRefName,headRefOid"],
                    cwd=repo)
    if code != 0:
        return "", "", f"pr view failed: {out[:200]}"
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return "", "", "pr view returned non-JSON"
    base_ref = str(data.get("baseRefName") or "")
    # The commits connection can be truncated at 100 entries; its last entry
    # need not be the PR head. Resolve the authoritative head directly.
    head_sha = str(data.get("headRefOid") or "")
    if not head_sha:
        return base_ref, "", "PR head unresolvable from pr view"
    return base_ref, head_sha, ""


def _fetch_pinned(repo: Path, pr: int, base_ref: str, head_sha: str,
                  run_id: str) -> tuple[str, str]:
    """Fetch base and head into this run's own refs; return `(base_sha, error)`.

    The destinations are explicit and forced. `git fetch origin <base>
    pull/N/head` — what this file used to do — gives `pull/N/head` no
    destination, so it lands in FETCH_HEAD, and the refresh of
    `refs/remotes/origin/<base>` is only git's *opportunistic* tracking-ref
    update: contingent on the conventional `remote.origin.fetch` refspec and
    suppressible by configuration. A merge-base taken against that possibly
    stale ref shows unrelated upstream churn as if it were the PR's work."""
    base_dest, head_dest = _pinned_refs(run_id)
    code, out = _git(repo, "fetch", "--no-tags", "origin",
                     f"+refs/heads/{base_ref}:{base_dest}",
                     f"+refs/pull/{pr}/head:{head_dest}", timeout=300)
    if code != 0:
        return "", f"git fetch failed: {out[:200]}"
    code, out = _git(repo, "rev-parse", head_dest)
    if code != 0:
        return "", f"fetched head unresolvable: {out[:200]}"
    fetched_head = out.strip()
    if fetched_head != head_sha:
        # the head moved between `gh pr view` and the fetch — git's own,
        # independent confirmation of what the API told us a moment ago
        return "", (f"PR head moved during fetch: {head_sha[:12]} -> "
                    f"{fetched_head[:12]}")
    code, out = _git(repo, "rev-parse", base_dest)
    if code != 0:
        return "", f"fetched base unresolvable: {out[:200]}"
    return out.strip(), ""


def _pinned_diff(repo: Path, base_sha: str,
                 head_sha: str) -> tuple[str, str, str]:
    """`merge-base(base_sha, head)..head` — the same three-dot semantics the API
    diff endpoint uses, but from the base commit THIS run fetched.

    Returns `(diff_text, detail, error)`. The error is separate from the text
    because an empty diff is a legitimate result (a PR that only deletes, or one
    whose changes are already in the base) and must not read as a failure.

    Computing the diff locally is also what makes the >20k-line case work: the
    API diff endpoint hard-fails those with HTTP 406, which is why a local
    reconstruction existed as a fallback before it became the primary path."""
    code, out = _git(repo, "merge-base", base_sha, head_sha)
    if code != 0:
        return "", "", f"merge-base failed: {out[:200]}"
    merge_base = out.strip()
    if merge_base == head_sha:
        # The head is an ANCESTOR of the base — the ordinary state of a merged
        # PR, where main now contains its commits. The three-dot diff is empty
        # by definition, so computing it here would review nothing at all. This
        # is not a failure: the caller falls back to the API diff, which is safe
        # precisely because a contained head cannot move any more.
        return "", "", CONTAINED_HEAD
    code, out = _git(repo, "diff", f"{merge_base}..{head_sha}", timeout=300)
    if code != 0:
        return "", "", f"git diff failed: {out[:200]}"
    return out, f"local git diff {merge_base[:12]}..{head_sha[:12]}", ""


def _pr_time_checkout(ctx: StepContext, repo: Path, pr: int,
                      sha: str) -> tuple[str, str, bool]:
    """Materialize the review tree at `sha` (PR-TIME state). Returns
    `(repo_path, checkout_note, pinned)` — the live checkout with a loud note
    when pinning is impossible, so reviews on post-merge main know a
    zero-survivor grep proves nothing about PR-time state.

    The latent-gap eval class (#4810 -> #4891) exists because reviewing on
    post-merge main hides exactly the sites the PR missed; a checklist rule
    could not fix what needed a mechanism."""
    dest = worktrees.dest_for(repo, pr, sha)
    ok, detail = worktrees.materialize(repo, sha, dest, _git)
    if not ok:
        ctx.trace.record("capability_gap", capability="pr.worktree",
                         step="pr.fetch_diff", effect=detail)
        return str(repo), ("checkout: CURRENT MAIN (worktree failed) — "
                           "post-PR fixes may be present; a clean grep does "
                           "NOT clear PR-time state"), False
    worktrees.hold(dest, ctx.run_dir)
    ctx.trace.record("pr_time_checkout", pr=pr, sha=sha, path=str(dest),
                     detail=detail)
    return str(dest), (f"checkout: PR-TIME TREE (head {sha[:12]}) — the tree "
                       "matches the diff exactly; repo-wide greps DO reflect "
                       "PR-time state"), True


_LINKED_ISSUE = re.compile(r"(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s*:?\s*#(\d+)",
                           re.IGNORECASE)
_BINARY_PATCH_SIZE = re.compile(r"^(literal|delta)\s+(\d+)\s*$")


def _strip_binary_patches(diff_text: str) -> tuple[str, list[dict[str, object]]]:
    """Replace bulky `GIT binary patch` bodies with a one-line summary.

    GitHub's patch stream can inline multi-megabyte binary literals for assets.
    Review agents need the file identity and byte sizes, not the encoded body.
    """
    lines = str(diff_text or "").splitlines(keepends=True)
    out: list[str] = []
    summaries: list[dict[str, object]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() != "GIT binary patch":
            out.append(line)
            i += 1
            continue

        sizes: list[dict[str, int | str]] = []
        omitted_lines = 1
        i += 1
        while i < len(lines) and not lines[i].startswith("diff --git "):
            m = _BINARY_PATCH_SIZE.match(lines[i].strip())
            if m:
                sizes.append({"kind": m.group(1), "bytes": int(m.group(2))})
            omitted_lines += 1
            i += 1

        summaries.append({"sizes": sizes, "omitted_lines": omitted_lines})
        size_text = ", ".join(
            f"{item['kind']} {item['bytes']} bytes" for item in sizes
        ) or f"{omitted_lines} lines"
        out.append(
            f"[BINARY PATCH OMITTED: {size_text}; "
            f"{omitted_lines} diff lines]\n"
        )

    return "".join(out), summaries


def _repo_full_name(ctx: StepContext, repo: str) -> str:
    """`owner/repo` of the checkout, resolved once via gh and cached in state —
    never taken from user/issue text (endpoint-injection guard)."""
    cached = ctx.state.get("_repo_full_name")
    if cached:
        return str(cached)
    code, out = _gh(["repo", "view", "--json", "nameWithOwner"], cwd=repo)
    full = ""
    if code == 0:
        try:
            full = str(json.loads(out or "{}").get("nameWithOwner") or "")
        except json.JSONDecodeError:
            full = ""
    ctx.state["_repo_full_name"] = full
    return full


def _last_n(items: list, n: int = 10) -> list:
    """Chronological last-n cap per source (B1's recall-vs-cost tradeoff)."""
    return list(items)[-n:]


def _clip(text: str, n: int = 700) -> str:
    text = str(text or "").strip()
    return text if len(text) <= n else text[:n] + " …[clipped]"


def _pr_context_bundle(ctx: StepContext, repo: str, pr: int) -> str:
    """Assemble the PR-side evidence bundle (design W1): title/body/labels
    always; discussion comments, review summaries, and inline review comments
    only in `pr_context_mode=full` (the eval-leakage policy: the frozen
    dataset's ground truth IS the human review discussion, so arm runs export
    PR_CONTEXT_MODE=no_discussion); linked issues (fixes/closes #N in body or
    branch name, capped 2) in both modes. Every sub-fetch failure degrades to
    a note — this bundle must never block the review."""
    mode = str(getattr(ctx.settings, "pr_context_mode", "full") or "full")
    parts: list[str] = []
    code, out = _gh(["pr", "view", str(pr), "--json",
                     "title,body,labels,headRefName,comments,reviews,commits"],
                    cwd=repo)
    data: dict = {}
    if code == 0:
        try:
            data = json.loads(out or "{}")
        except json.JSONDecodeError:
            data = {}
    else:
        parts.append("(pr view unavailable — partial context)")
    if data:
        labels = ", ".join(lb.get("name", "") for lb in data.get("labels") or [])
        parts.append(f"## PR description\n### {data.get('title', '')}"
                     + (f"  [labels: {labels}]" if labels else "")
                     + f"\n{_clip(data.get('body'), 4000)}")
        # commit timeline (both modes — the author's own commits, not review
        # discussion): a squashed head diff hides add-then-revert churn, and a
        # reviewer who cannot see that a described change was later removed
        # misreads the description-vs-diff mismatch (it invited re-adding a
        # reverted regression in a live run)
        subjects = [
            f"- {str(c.get('oid') or '')[:8]} "
            f"{_clip((c.get('messageHeadline') or ''), 100)}"
            for c in (data.get("commits") or [])[-20:]]
        if subjects:
            parts.append("## Commit timeline (subjects only — the diff below "
                         "is the squashed net change)\n" + "\n".join(subjects))
    if mode == "full" and data:
        comments = [f"@{c.get('author', {}).get('login', '?')}: {_clip(c.get('body'))}"
                    for c in _last_n(data.get("comments") or [])]
        if comments:
            parts.append("## PR discussion (do not repeat these concerns — "
                         "build on or extend them)\n" + "\n".join(comments))
        reviews = [f"@{r.get('author', {}).get('login', '?')} "
                   f"[{r.get('state', '?')}]: {_clip(r.get('body'))}"
                   for r in _last_n(data.get("reviews") or []) if r.get("body")]
        if reviews:
            parts.append("## Review summaries\n" + "\n".join(reviews))
        full_name = _repo_full_name(ctx, repo)
        if full_name:  # inline review comments live on a separate endpoint
            code, out = _gh(["api", f"repos/{full_name}/pulls/{pr}/comments",
                             "--paginate", "-q",
                             ".[] | {user: .user.login, path, line, body}"],
                            cwd=repo)
            if code == 0 and out.strip():
                inline = []
                for line in _last_n(out.strip().splitlines()):
                    try:
                        c = json.loads(line)
                        inline.append(f"@{c.get('user', '?')} {c.get('path')}:"
                                      f"{c.get('line')}: {_clip(c.get('body'))}")
                    except json.JSONDecodeError:
                        continue
                if inline:
                    parts.append("## Inline review comments\n" + "\n".join(inline))
    # linked issues: acceptance criteria the diff must satisfy (both modes)
    hay = f"{(data.get('body') or '')} {(data.get('headRefName') or '')}"
    for num in list(dict.fromkeys(_LINKED_ISSUE.findall(hay)))[:2]:
        code, out = _gh(["issue", "view", num, "--json", "title,body"], cwd=repo)
        if code == 0:
            try:
                idata = json.loads(out or "{}")
                parts.append(f"## Linked issue #{num}: {idata.get('title', '')}\n"
                             + _clip(idata.get("body"), 2000))
            except json.JSONDecodeError:
                continue
    return "\n\n".join(p for p in parts if p)


def _stale(ctx: StepContext, pr: int, expected: str, actual: str) -> StepResult:
    """Stop as stale, recording BOTH shas where a failed step cannot lose them.

    The executor checkpoints only successful steps, so `state_updates` on a
    failed StepResult never reaches `progress.json`. `run_trace.jsonl` is
    append-only and survives, so the structured-result assembler reads the facts
    from there."""
    ctx.trace.record("expected_head_mismatch", pr=pr, expected=expected,
                     actual=actual, step="pr.fetch_diff")
    return StepResult(
        False, FailureKind.BLOCKED,
        f"PR #{pr} head is {actual[:12] or 'unresolvable'}, not the requested "
        f"{expected[:12]} — refusing to review a different snapshot")


def _fetch_at_one_head(ctx: StepContext, repo: Path, pr: int, expected: str):
    """Resolve the head once, then derive the diff and the review tree from that
    single sha. Returns `(diff, diff_note, head_sha, repo_path, checkout_note)`
    or a BLOCKED StepResult.

    `expected` set is the strict contract: stale ⇒ stop before any fetch, and
    any failure to pin ⇒ BLOCKED rather than a quiet degrade to the live
    checkout. `expected` empty is the CLI/interactive path, which keeps today's
    tolerant behaviour — the reorder still applies, but a fetch or
    materialization failure degrades with the existing loud note instead of
    blocking, so the common path does not regress."""
    injected = str(ctx.state.get("pr_head_sha") or "")  # offline-test injection
    base_ref, head_sha, err = ("", injected, "") if injected \
        else _resolve_pr_head(repo, pr)

    if expected and head_sha != expected:
        # the gate fires BEFORE any fetch or materialization: a pinned request
        # must not touch the network or the disk for a snapshot it will refuse
        return _stale(ctx, pr, expected, head_sha)
    if not head_sha:
        ctx.trace.record("capability_gap", capability="pr.head_sha",
                         step="pr.fetch_diff", effect=err or "head unresolvable")
    else:
        ctx.state["pr_head_sha"] = head_sha

    diff = _diff_at_head(ctx, repo, pr, base_ref, head_sha, expected,
                         skip_fetch=bool(injected))
    if isinstance(diff, StepResult):
        return diff
    diff_text, diff_note = diff

    if not head_sha:  # unpinned and unresolvable: today's loud-note degrade
        note = ("checkout: CURRENT MAIN (PR head unresolvable) — the tree may "
                "already contain post-PR fixes; a clean grep does NOT clear "
                "PR-time state")
        return diff_text, diff_note, "", str(repo), note

    wt_path, note, pinned = _pr_time_checkout(ctx, repo, pr, head_sha)
    if expected and not pinned:
        return StepResult(
            False, FailureKind.BLOCKED,
            f"cannot materialize PR #{pr} at {expected[:12]} — refusing to "
            "review the live checkout for a pinned request")
    return diff_text, diff_note, head_sha, wt_path, note


def _diff_at_head(ctx: StepContext, repo: Path, pr: int, base_ref: str,
                  head_sha: str, expected: str, *, skip_fetch: bool):
    """The diff for `head_sha`, preferring the pinned local computation.

    Returns `(diff_text, note)` or a BLOCKED StepResult. `gh pr diff` is the
    fallback: it resolves the head server-side, so for an OPEN PR it can
    disagree with the tree we just materialized — the very TOCTOU window this
    reorder closes — and a pinned run must not take it. The one exception is a
    head already contained in the base (a merged PR), where the local three-dot
    diff is empty by definition and the head can no longer move, so the API diff
    is both necessary and safe."""
    if head_sha and not skip_fetch:
        base_sha, err = _fetch_pinned(repo, pr, base_ref, head_sha,
                                      ctx.run_dir.name)
        if not err:
            # the pinned base TIP (not the merge-base): the review tools bind
            # their merge-base to it, so tools and diff agree (design §8.2)
            ctx.state["pr_base_sha"] = base_sha
            ctx.state["pr_base_ref"] = base_ref
            text, detail, derr = _pinned_diff(repo, base_sha, head_sha)
            if not derr:
                return text, f" via {detail}"
            err = derr
        if err == CONTAINED_HEAD:
            ctx.trace.record("diff_fallback", pr=pr, detail="gh pr diff (merged)",
                             reason=CONTAINED_HEAD)
            code, out = _gh(["pr", "diff", str(pr)], cwd=repo)
            if code != 0:
                return StepResult(False, FailureKind.BLOCKED,
                                  f"PR #{pr} is already merged into the base and "
                                  f"gh pr diff failed: {out[:300]}")
            return out, " via gh pr diff (merged PR: head is in the base)"
        if expected:
            return StepResult(False, FailureKind.BLOCKED,
                              f"cannot pin PR #{pr} to {expected[:12]}: {err}")
        ctx.trace.record("capability_gap", capability="pr.head_fetch",
                         step="pr.fetch_diff", effect=err[:200])
    elif expected and not skip_fetch:
        return StepResult(False, FailureKind.BLOCKED,
                          f"PR #{pr} head unresolvable — cannot honor the "
                          f"requested {expected[:12]}")

    code, out = _gh(["pr", "diff", str(pr)], cwd=repo)
    if code != 0:
        return StepResult(False, FailureKind.BLOCKED,
                          f"gh pr diff failed: {out[:300]}")
    if not skip_fetch:
        ctx.trace.record("diff_fallback", pr=pr, detail="gh pr diff (unpinned)")
        return out, " via gh pr diff (unpinned)"
    return out, ""


@step("pr.fetch_diff", "deterministic", "read",
      "Fetch a PR diff via gh (read-only).")
async def _pr_fetch_diff(ctx: StepContext) -> StepResult:
    """Fetch a PR's unified diff for the downstream reviewers, pinned to one
    resolved head. Reads the PR number and `expected_head_sha` from `task_spec`;
    returns injected `diff_text` from state verbatim when present (offline
    testing). A missing PR number or a failed `gh` call degrades to BLOCKED
    rather than raising.

    **One head governs everything.** The head is resolved once, up front, and
    the stale gate, the fetch, the diff and the worktree all use that answer.
    When `expected_head_sha` is set and the PR has moved, the step stops as
    stale *before* any fetch or materialization, and never degrades to the live
    checkout: a pinned request that silently reviewed the wrong tree would be
    worse than one that stopped.

    Publishes `diff_text` to state (B2 `state_updates`)."""
    cached = from_state(ctx, "diff_text")
    if cached is not None:
        return cached
    spec = ctx.state.get("task_spec") or {}
    pr = spec.get("pr") if isinstance(spec, dict) else None
    if not pr:
        return StepResult(False, FailureKind.BLOCKED, "no PR number in task spec")
    expected = str(spec.get("expected_head_sha") or "") if isinstance(spec, dict) else ""
    if _snapshot_mode(ctx):
        return _fetch_from_snapshot(ctx, int(pr), expected)
    repo = require_repo(ctx)
    if isinstance(repo, StepResult):
        return repo

    fetched = _fetch_at_one_head(ctx, Path(repo), int(pr), expected)
    if isinstance(fetched, StepResult):
        return fetched
    out, diff_note, head_sha, wt_path, note = fetched
    raw_chars = len(out)
    out, binary_summaries = _strip_binary_patches(out)
    if binary_summaries:
        ctx.trace.record(
            "diff_binary_patches_omitted",
            pr=int(pr),
            count=len(binary_summaries),
            raw_chars=raw_chars,
            sanitized_chars=len(out),
            summaries=binary_summaries[:10],
        )
        diff_note += f"; omitted {len(binary_summaries)} binary patches"
    ctx.state["diff_text"] = out
    # PR context bundle (design W1): description + discussion + linked issues —
    # the recall evidence a diff-only review structurally lacks. Degrades to a
    # partial bundle on any sub-fetch failure; never blocks the run.
    frozen = (spec.get("params") or {}).get("business_context")
    if frozen:
        from ....review.frozen_context import frozen_pr_context

        pr_context = frozen_pr_context(frozen, include_discussion=
            str(getattr(ctx.settings, "pr_context_mode", "full")) == "full")
    else:
        pr_context = _pr_context_bundle(ctx, repo, int(pr))
    ctx.state["pr_context"] = pr_context
    ctx.state["repo_path"] = wt_path
    ctx.state["checkout_note"] = note
    return StepResult(
        True,
        summary=f"fetched PR #{pr} diff ({len(out)} chars{diff_note}, context "
                f"{len(pr_context)} chars); {note.split(' — ')[0]}",
        outputs={"state_updates": {"diff_text": out, "pr_context": pr_context,
                                   "repo_path": wt_path,
                                   "checkout_note": note,
                                   "pr_head_sha": head_sha,
                                   "pr_base_sha": str(ctx.state.get("pr_base_sha") or ""),
                                   "pr_base_ref": str(ctx.state.get("pr_base_ref") or "")}})


@step("pr.gate_check", "deterministic", "read",
      "Draft/merge-state/failing-checks gate report (deterministic).")
async def _pr_gate_check(ctx: StepContext) -> StepResult:
    """Deterministic gate check: draft/merge-state/failing checks — the issue
    class the eval showed no diff-only reviewer catches. Non-blocking: the
    findings go into the review context and the report."""
    cached = from_state(ctx, "gate_report")
    if cached is not None:
        return cached
    spec = ctx.state.get("task_spec") or {}
    pr = spec.get("pr") if isinstance(spec, dict) else None
    if not pr:
        return StepResult(False, FailureKind.BLOCKED, "no PR number in task spec")
    frozen = (spec.get("params") or {}).get("business_context")
    if frozen:
        from ....review.frozen_context import frozen_gate_report

        report, pr_state = frozen_gate_report(frozen)
        updates = {"gate_report": report, "pr_state": pr_state}
        ctx.state.update(updates)
        return StepResult(True, summary="frozen dispatch gate evidence",
                          outputs={"gate_report": report, "state_updates": updates})
    if _snapshot_mode(ctx):
        snap = _snapshot_for(ctx, int(pr))
        if isinstance(snap, StepResult):
            return snap
        report = str(snap.get("gate_report") or "gate report absent from snapshot")
        ctx.state["gate_report"] = report
        ctx.state["pr_state"] = str(snap.get("pr_state") or "")
        return StepResult(True, summary=f"[snapshot] {report.splitlines()[0][:100]}",
                          outputs={"gate_report": report,
                                   "state_updates": {"gate_report": report,
                                                     "pr_state": ctx.state["pr_state"]}})
    repo = _repo_path(ctx)
    report, pr_state, available = gate_report_for(repo, int(pr))
    if not available:
        ctx.state["gate_report"] = report
        return StepResult(True, summary="gate check unavailable (gh failed) — "
                                        "continuing without it",
                          outputs={"state_updates": {"gate_report": report}})
    # publish the PR state (OPEN|MERGED|CLOSED) — the renderer calibrates the
    # verdict wording on merged PRs (W2); previously MERGED was discarded
    # whenever no other gate fired
    ctx.state["pr_state"] = pr_state
    ctx.state["gate_report"] = report
    return StepResult(True, summary=report.splitlines()[0][:120],
                      outputs={"gate_report": report,
                               "state_updates": {
                                   "gate_report": report,
                                   "pr_state": ctx.state.get("pr_state", "")}})


def gate_report_for(repo: Path | None, pr: int) -> tuple[str, str, bool]:
    """The deterministic gate report of a PR: `(report, pr_state, available)`
    — shared by the live gate step and the shadow staging step, so a staged
    snapshot carries exactly what the live step would have computed."""
    lines: list[str] = []
    code, out = _gh(["pr", "view", str(pr), "--json",
                     "state,isDraft,mergeable,mergeStateStatus"], cwd=repo)
    if code != 0:
        return "gate check unavailable (gh failed)", "", False
    data = json.loads(out or "{}")
    pr_state = str(data.get("state") or "")
    if data.get("isDraft"):
        lines.append("PR is a DRAFT — review findings are provisional.")
    if data.get("mergeable") == "CONFLICTING" or \
            data.get("mergeStateStatus") in ("DIRTY", "BEHIND"):
        lines.append(f"MERGE STATE: {data.get('mergeStateStatus')} / "
                     f"{data.get('mergeable')} — the branch conflicts with or "
                     "trails the base; files may have moved/renamed on main. "
                     "Flag this as a blocking issue.")
    checks = ci_checks_for(repo, pr)
    if checks is not None:
        failing = [c.get("name", "?") for c in checks
                   if c.get("bucket") == "fail"
                   or str(c.get("state", "")).upper() in ("FAILURE", "ERROR")]
        if failing:
            lines.append(f"FAILING CHECKS ({len(failing)}): {failing[:8]} — "
                         "do not re-argue what CI already reports; point at the gate.")
    report = "\n".join(lines) or "gates clean (mergeable, no failing checks)"
    return report, pr_state, True


def ci_checks_for(repo: Path | None, pr: int) -> list[dict] | None:
    """`gh pr checks` as a list of `{name, state, link, bucket}`, or None when
    the checks are UNAVAILABLE. `gh pr checks` exits non-zero when any check
    is pending or failing while still printing the JSON, so the exit code is
    not the signal: parseable list output is the data, whatever the code."""
    code, out = _gh(["pr", "checks", str(pr), "--json", "name,state,link,bucket"], cwd=repo)
    try:
        checks = json.loads(out) if out and out.lstrip().startswith("[") else None
    except json.JSONDecodeError:
        checks = None
    if isinstance(checks, list):
        return checks
    return None if code != 0 or not out else []


# -- snapshot source (shadow experiments) ------------------------------------------

def _snapshot_mode(ctx: StepContext) -> bool:
    return str(getattr(ctx.settings, "pr_context_source", "live") or "live") == "snapshot"


def _snapshot_for(ctx: StepContext, pr: int):
    """The staged snapshot for this run's PR (`state['pr_snapshot']`, written
    by `improve.stage_items`), or a BLOCKED StepResult: snapshot mode without
    a snapshot, or one for another PR, is a configuration error, never a
    fallback to the network."""
    snap = ctx.state.get("pr_snapshot")
    if not isinstance(snap, dict) or not snap:
        # a shadow subprocess gets its staged snapshot as a file (the engine
        # staged it in the parent, before isolation) — a path only, never
        # a live fetch
        import os

        path = os.environ.get("PR_SNAPSHOT_FILE", "")
        if path and Path(path).is_file():
            try:
                snap = json.loads(Path(path).read_text(encoding="utf-8"))
                ctx.state["pr_snapshot"] = snap
            except (OSError, json.JSONDecodeError) as exc:
                return StepResult(False, FailureKind.BLOCKED, f"PR_SNAPSHOT_FILE unreadable: {exc}")
    if not isinstance(snap, dict) or not snap:
        return StepResult(False, FailureKind.BLOCKED,
                          "pr_context_source=snapshot but no staged snapshot in state "
                          "(run improve.stage_items first)")
    spec = ctx.state.get("task_spec") or {}
    repo = str(spec.get("repo") or "") if isinstance(spec, dict) else ""
    # PR numbers are repository-local: the snapshot must name THIS repository
    if repo and str(snap.get("repo") or "") != repo:
        return StepResult(False, FailureKind.BLOCKED,
                          f"staged snapshot is for repository {snap.get('repo')!r}, not {repo!r}")
    if int(snap.get("pr") or 0) != int(pr):
        return StepResult(False, FailureKind.BLOCKED,
                          f"staged snapshot is for PR #{snap.get('pr')}, not #{pr}")
    return snap


def _fetch_from_snapshot(ctx: StepContext, pr: int, expected: str) -> StepResult:
    """`pr.fetch_diff` under `pr_context_source=snapshot`: publish the staged
    diff, context, shadow checkout and pinned shas — no gh, no git fetch."""
    snap = _snapshot_for(ctx, pr)
    if isinstance(snap, StepResult):
        return snap
    head_sha = str(snap.get("head_sha") or "")
    if expected and head_sha != expected:
        return _stale(ctx, pr, expected, head_sha)
    shadow_dir = str(snap.get("shadow_dir") or "")
    if not shadow_dir or not Path(shadow_dir).is_dir():
        return StepResult(False, FailureKind.BLOCKED,
                          f"staged snapshot's shadow checkout is missing: {shadow_dir!r}")
    diff_text = str(snap.get("diff") or "")
    context = str(snap.get("context_text") or "")
    note = (f"checkout: SHADOW CLONE (head {head_sha[:12]}, snapshot "
            f"{str(snap.get('snapshot_sha256') or '')[:12]}) — staged before isolation; "
            "repo-wide greps DO reflect PR-time state")
    ctx.trace.record("pr_time_checkout", pr=pr, sha=head_sha, path=shadow_dir,
                     detail="staged snapshot", snapshot_sha256=snap.get("snapshot_sha256", ""))
    updates = {"diff_text": diff_text, "pr_context": context, "repo_path": shadow_dir,
               "checkout_note": note, "pr_head_sha": head_sha,
               "pr_base_sha": str(snap.get("base_sha") or ""),
               "pr_base_ref": str(snap.get("base_ref") or "")}
    ctx.state.update(updates)
    return StepResult(True, summary=f"[snapshot] PR #{pr} diff ({len(diff_text)} chars, context "
                                    f"{len(context)} chars); {note.split(' — ')[0]}",
                      outputs={"state_updates": updates})
