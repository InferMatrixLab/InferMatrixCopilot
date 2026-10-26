"""Outward-writing PR steps: guarded pushes and structured GitHub reviews.

Both are choke points for the safety model. ``ci.push`` uses ``guard_push``;
``pr.post_review`` keeps the explicit-post plus ``ALLOW_POST`` gates, validates
finding locations against the fetched diff, and submits one GitHub review with
inline threads.
"""

from __future__ import annotations

import json
import re
import subprocess

from ....push import PushPolicy, guard_push
from ....review.publication_policy import review_head_error, review_evidence_error
from ...step import FailureKind, StepContext, StepResult, StepSpec
from ..review.anchor import diff_index
from ..review.anchor import normalize_path as _normalize_path
from .._common import gh as _gh
from .._common import register_step, step
from .._common import repo_path as _repo_path
from .._common import task_spec as _task_spec
from .fetch import _repo_full_name


@step("ci.push", "script", "push",
      "Guarded push (PushPolicy AND protected branches; dry-run default).")
async def _push(ctx: StepContext) -> StepResult:
    """The single C4 push choke point: authorize a git push through `guard_push`
    (PushPolicy AND protected branches) before ever running it. Rehydrates the
    `push_policy` from state (dict or PushPolicy) and the protected-branch list
    from state or settings.

    A denied decision returns FORBIDDEN. When authorized but `ALLOW_PUSH=0`
    (the default), it stays a dry run — reports the command it *would* run, never
    executes. Only with pushes enabled does it run the git command; a non-zero
    exit returns ESCALATE with the stderr tail."""
    if _task_spec(ctx).get("kind") in {"pr_review", "pr_quality"}:
        return StepResult(False, FailureKind.FORBIDDEN,
                          "Review and quality tasks have comment-only authority")
    repo = _repo_path(ctx)
    raw = ctx.state.get("push_policy")
    policy = raw if isinstance(raw, PushPolicy) else PushPolicy(**(raw or {}))
    protected = ctx.state.get("protected_branches") or ctx.settings.protected_branches
    ctx.trace.record("push_requested", remote=policy.remote, branch=policy.branch)
    decision = guard_push(policy, list(protected))
    if not decision.allowed:
        return StepResult(False, FailureKind.FORBIDDEN, decision.reason)
    if _task_spec(ctx).get("kind") in {"pr_debug", "pr_rebase"}:
        from ....review.change_set import ChangeSet, ChangeSetError

        approval = ctx.state.get("approved_change_set") or {}
        try:
            current = ChangeSet.capture(repo, str(approval.get("base_sha") or ""))
        except ChangeSetError as exc:
            return StepResult(False, FailureKind.FORBIDDEN, f"pre-push approval unavailable: {exc}")
        if current.head_sha != approval.get("head_sha"):
            return StepResult(False, FailureKind.FORBIDDEN,
                              "PR head changed after patch review; review the new commit")
    if not ctx.settings.allow_push:
        return StepResult(True, summary=f"dry-run (ALLOW_PUSH=0): {' '.join(decision.command)}",
                          outputs={"dry_run": True, "command": list(decision.command)})
    out = subprocess.run(list(decision.command), cwd=str(repo), capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=300)
    if out.returncode != 0:
        return StepResult(False, FailureKind.ESCALATE,
                          f"push failed: {out.stderr[-1_000:]}")
    return StepResult(True, summary=f"pushed {policy.remote} HEAD:{policy.branch}")


# Why a finding could not be attached. The distinction matters because two of these
# are statements about OUR input and only the third is about the finding.
_WHY_CLIPPED = ("the fetched diff for this file was clipped mid-hunk, so its line "
                "could not be confirmed against the full changed range")
_WHY_ABSENT = ("this file does not appear in the diff we fetched, so its line could "
               "not be checked at all")
_WHY_OUTSIDE = ("their line falls outside the changed lines in the fetched diff "
                "(no clipping detected in that file)")
_WHY_NO_LOCATION = "the finding did not carry a usable file and line"
_WHY_UNVERIFIED = ("the finding quoted a code snippet that could not be matched to a "
                   "unique changed line, so its declared line could not be corroborated")


def _as_line_number(value: object) -> int:
    """Return `value` as a 1-based line number, or 0 if it is not one.

    `int()` is far too permissive for a value that becomes a PUBLISHED anchor:
    `int(1.9)` and `int(True)` are both 1, so a malformed line silently lands on
    line 1 and gets posted there if line 1 happens to be addressable. Conversely
    `str.isdigit()` is too permissive as a guard — `"²".isdigit()` is True while
    `int("²")` raises, which would crash payload construction — so digit strings
    must also be ASCII. bool is excluded explicitly because it subclasses int.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isascii() and text.isdigit():
            try:
                return int(text)
            except ValueError:
                # even an all-ASCII digit string can refuse to convert: CPython caps
                # int(str) at sys.get_int_max_str_digits() (4300), and letting that
                # escape would abort the whole payload over one absurd finding
                return 0
    return 0


_FULL_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _right_side_diff_lines(diff: str) -> dict[str, tuple[set[int], str]]:
    """RIGHT-side line numbers addressable by GitHub's review API, per path, with a
    state saying how much that inventory can be trusted.

    A projection over ``anchor.diff_index`` — one walk serves both this validator and
    the snippet resolver, because two parsers would have to agree forever on what
    counts as a right-side line and would drift.

    Paths present map to ``(lines, "no_clipping_detected" | "incomplete")``; a path
    ABSENT from the mapping is *unknown*, never "complete" — the caller must not read
    a missing key as proof the file was unchanged.

    What the state does and does not prove: comparing a hunk's declared new-side count
    against the lines actually observed detects a clip INSIDE a hunk. It cannot detect
    an omitted file, an omitted whole hunk, a clip exactly on a hunk boundary, or a
    truncated final line — each is indistinguishable from a well-formed shorter diff.
    So ``no_clipping_detected`` means exactly that, and deliberately is not named
    ``complete``: the strongest honest claim is "we found no evidence of clipping in
    this file", and a future reader should not be able to inflate the name into more.
    """
    return {
        path: ({num for segment in index.right for num, _ in segment}, index.state)
        for path, index in diff_index(diff).items()
    }


def _inline_comment(comment: dict) -> dict:
    severity = str(comment.get("severity") or "minor").lower()
    body = f"**[{severity}]** {str(comment.get('comment') or '').strip()}"
    evidence = str(comment.get("evidence") or "").strip()
    if evidence:
        body += f"\n\nEvidence: {evidence}"
    return {
        "path": _normalize_path(comment.get("file")),
        "line": int(comment["line"]),
        "side": "RIGHT",
        "body": body,
    }


def _partition_comments(
    comments: list[dict], diff: str,
) -> tuple[list[dict], list[dict]]:
    """Split findings into API-addressable comments and body fallbacks."""
    inventory = _right_side_diff_lines(diff)
    inline: list[dict] = []
    downgraded: list[dict] = []
    for raw in comments:
        comment = dict(raw) if isinstance(raw, dict) else {
            "comment": str(raw), "file": "", "line": None,
        }
        path = _normalize_path(comment.get("file"))
        # `int()` is too permissive for a value that becomes a published anchor:
        # int(1.9) and int(True) are both 1, so a malformed line silently lands on
        # line 1 and, if line 1 happens to be addressable, gets POSTED there. Only a
        # real integer (bool is an int subclass, hence the explicit exclusion) or a
        # digit string is a location; everything else is malformed.
        line = _as_line_number(comment.get("line"))
        entry = inventory.get(path) if path else None
        # A snippet was supplied and could not be resolved. Do NOT fall back to the
        # declared line even when it is addressable: we looked for corroboration and
        # failed to find it, which is weaker evidence than never having looked. An
        # addressable line is not the same thing as the intended anchor.
        unverified = bool(comment.pop("_anchor_unverified", False))
        if entry and line > 0 and line in entry[0] and not unverified:
            comment["file"], comment["line"] = path, line
            inline.append(_inline_comment(comment))
        else:
            comment["file"], comment["line"] = path, line or "?"
            # each finding carries the reason for ITS OWN demotion: a single review
            # can hold all of these at once, and one blanket sentence mislabels the rest.
            # Malformed location is checked FIRST — otherwise a finding that named no
            # usable line at all gets blamed on our diff being clipped, which is the
            # opposite of the truth.
            # unverified is checked FIRST: the resolver clears `line` when a snippet
            # could not be corroborated, so the no-location branch would otherwise
            # claim the model never gave a location when in fact we rejected it
            if unverified:
                comment["_why"] = _WHY_UNVERIFIED
            elif not path or line <= 0:
                comment["_why"] = _WHY_NO_LOCATION
            elif entry is None:
                comment["_why"] = _WHY_ABSENT
            elif entry[1] == "incomplete":
                comment["_why"] = _WHY_CLIPPED
            else:
                comment["_why"] = _WHY_OUTSIDE
            downgraded.append(comment)
    return inline, downgraded


def _fallback_section(comments: list[dict], reason: str = "") -> str:
    """Render demoted findings, grouped by WHY each was demoted.

    A single `reason` still applies to the whole set for the stale-head case, where
    one fact genuinely covers every finding. Otherwise each finding brings its own
    `_why`, because a review can simultaneously contain findings whose file was
    clipped, whose file was absent, and whose line was simply outside the diff — and
    a blanket sentence would misattribute two of the three, blaming the reviewer for
    a gap in our own input.
    """
    if not comments:
        return ""

    def render(comment: dict) -> str:
        location = f"{comment.get('file') or '?'}:{comment.get('line') or '?'}"
        severity = str(comment.get("severity") or "minor").lower()
        text = str(comment.get("comment") or "").strip()
        evidence = str(comment.get("evidence") or "").strip()
        suffix = f" Evidence: {evidence}" if evidence else ""
        return f"- `{location}` [{severity}] — {text}{suffix}"

    lines = ["### Findings without a valid current diff anchor", ""]
    if reason:
        lines += [reason, ""]
        lines += [render(comment) for comment in comments]
        return "\n".join(lines)

    grouped: dict[str, list[dict]] = {}
    for comment in comments:
        grouped.setdefault(str(comment.get("_why") or _WHY_OUTSIDE), []).append(comment)
    for why, group in grouped.items():
        lines += [f"These findings are preserved here because {why}:", ""]
        lines += [render(comment) for comment in group]
        lines.append("")
    return "\n".join(lines).rstrip()


def _event_for_review(comments: list[dict], pr_state: str = "",
                      review_text: str = "") -> str:
    """A review assessment never changes GitHub approval/blocking state."""
    return "COMMENT"


def _review_payload(state: dict, *, current_head: str = "") -> tuple[dict, int]:
    """Build the API payload and return it with the downgrade count."""
    raw_comments = state.get("review_comments")
    comments = list(raw_comments) if isinstance(raw_comments, list) else []
    problem = review_evidence_error(mode="strict", comments=comments,
                                   dispositions=state.get("review_finding_dispositions") or ())
    if problem:
        raise ValueError(problem)
    reviewed_head = str(state.get("pr_head_sha") or "").strip()
    if current_head:
        problem = review_head_error(reviewed_head, current_head)
        if problem:
            raise ValueError(problem)
    inline, downgraded = _partition_comments(
        comments, str(state.get("diff_text") or ""))
    review_text = str(state.get("review_text") or "")
    body = str(state.get("review_summary") or review_text or "Review complete.")
    fallback = _fallback_section(downgraded)
    if fallback:
        body = f"{body.rstrip()}\n\n{fallback}"
    payload = {
        "body": body,
        "event": _event_for_review(
            comments, str(state.get("pr_state") or ""), review_text),
        "comments": inline,
    }
    commit_id = reviewed_head
    if commit_id:
        payload["commit_id"] = commit_id
    return payload, len(downgraded)


def _current_pr_head(repo, pr: int) -> tuple[str, str]:
    """Resolve the current PR head SHA immediately before the write."""
    code, out = _gh(["pr", "view", str(int(pr)), "--json", "headRefOid"], cwd=repo)
    if code != 0:
        return "", out[:400]
    try:
        sha = str(json.loads(out or "{}").get("headRefOid") or "")
    except (AttributeError, json.JSONDecodeError):
        sha = ""
    return sha, "" if sha else "PR head SHA missing from gh response"


async def _post_review(ctx: StepContext) -> StepResult:
    """Submit one GitHub review after both outward-write gates pass."""
    spec = _task_spec(ctx)
    if not (ctx.state.get("review_text") or ctx.state.get("review_comments")):
        return StepResult(False, FailureKind.BLOCKED, "no review output to post")
    if not spec.get("post"):
        return StepResult(True, summary="not posting GitHub review "
                          "(post flag not set)")

    try:
        payload, downgraded = _review_payload(ctx.state)
    except ValueError as exc:
        return StepResult(False, FailureKind.BLOCKED, str(exc))
    inline_count = len(payload["comments"])
    if not ctx.settings.allow_post:
        return StepResult(
            True,
            summary=(
                "dry-run (ALLOW_POST=0): would post GitHub review "
                f"({inline_count} inline, {downgraded} downgraded, "
                f"event={payload['event']})"
            ),
            outputs={"dry_run": True, "payload": payload},
        )

    pr = spec.get("pr")
    if not pr:
        return StepResult(False, FailureKind.BLOCKED, "no PR number to post")
    repo = _repo_path(ctx)
    if repo is None:
        return StepResult(False, FailureKind.BLOCKED,
                          "repo checkout not configured")
    full_name = _repo_full_name(ctx, str(repo))
    if not _FULL_REPO.fullmatch(full_name):
        return StepResult(False, FailureKind.BLOCKED,
                          "could not resolve safe repository nameWithOwner")
    current_head, head_error = _current_pr_head(repo, int(pr))
    if not current_head:
        return StepResult(
            False, FailureKind.ESCALATE,
            f"could not verify current PR head before posting: {head_error}")
    problem = review_head_error(str(ctx.state.get("pr_head_sha") or "").strip(), current_head)
    if problem:
        return StepResult(False, FailureKind.BLOCKED, problem, outputs={"stale": True})
    payload, downgraded = _review_payload(ctx.state, current_head=current_head)
    inline_count = len(payload["comments"])

    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    payload_path = ctx.run_dir / "github_review_payload.json"
    payload_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    from ....knowledge_service.containment import (
        ContainmentError, configuration, knowledge_availability_check, knowledge_publication_guard,
    )
    try:
        with knowledge_publication_guard():
            if configuration()["enabled"]:
                from ....app.run_service import read_knowledge_pin
                usage = read_knowledge_pin(ctx.run_dir).get("knowledge_usage")
                if not knowledge_availability_check(usage)["allowed"]:
                    return StepResult(False, FailureKind.BLOCKED, "knowledge held; reassessment required")
            code, out = _gh([
                "api", "--method", "POST",
                f"repos/{full_name}/pulls/{int(pr)}/reviews",
                "--input", str(payload_path),
            ], cwd=repo)
    except ContainmentError:
        return StepResult(False, FailureKind.BLOCKED, "knowledge policy unavailable; reassessment required")
    if code != 0:
        return StepResult(
            False,
            FailureKind.ESCALATE,
            f"GitHub review posting failed: {out[:400]}",
            outputs={"artifacts": [str(payload_path)],
                     "inline": inline_count, "downgraded": downgraded,
                     "event": payload["event"]},
        )

    try:
        response = json.loads(out or "{}")
    except json.JSONDecodeError:
        response = {}
    url = str(response.get("html_url") or "")
    if not url:
        match = re.search(r"https://\S+", out or "")
        url = match.group(0) if match else ""
    ctx.trace.record(
        "posted_artifact", what="GitHub PR review", url=url, pr=int(pr),
        inline=inline_count, downgraded=downgraded, event=payload["event"])
    outputs = {
        "inline": inline_count,
        "downgraded": downgraded,
        "event": payload["event"],
        "payload_path": str(payload_path),
    }
    if url:
        outputs["url"] = url
    return StepResult(
        True,
        summary=(
            f"posted GitHub review ({inline_count} inline, "
            f"{downgraded} downgraded, event={payload['event']})"
        ),
        outputs=outputs,
    )


register_step(StepSpec(
    "pr.post_review", "script", "push", _post_review,
    "Post one GitHub review with validated inline comments "
    "(explicit post flag + ALLOW_POST)."))
