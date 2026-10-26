"""Structural request gates shared by SDK, MCP, and reserved-run execution.

The MCP transport and embedded SDK accept non-interactive requests without a
human approval prompt. The guarantee *"the host cannot widen a run's
permissions"* must hold **structurally**, and it must not depend on the on-disk
`request.json` being untampered — a same-user host process could rewrite it
between reservation and execution. This gate therefore runs in **two** places
(design rev 5/6):

- at the **boundary** (SDK or server, when a request is reserved), and
- in the **child** (authoritative), right after it reads `request.json`.

Either way it re-derives a *safe* `TaskSpec` from raw input, refusing anything
outside the read-only V1 surface. The allowlist of kinds is `READ_ONLY_KINDS`
from `task_spec` verbatim, so the gate can never drift from the code's own
notion of what is read-only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..task_spec import FULL_SHA_RE, READ_ONLY_KINDS, TaskSpec

# params a V1 tool may legitimately carry through to the playbook. Anything else
# in an incoming `params` map is dropped (not an error — stripped) so a tampered
# request can't smuggle a knob (e.g. force_push) into a step. Allowed params are
# strictly value-validated below — a knob may modulate cost/depth, never widen
# permissions.
_ALLOWED_PARAMS: frozenset[str] = frozenset({
    "review_depth", "deterministic_signals", "carried_findings", "business_context",
})
_REVIEW_DEPTHS = ("auto", "light", "standard", "full")

# `expected_head_sha` is deliberately NOT a param. `_ALLOWED_PARAMS` is a security
# allow-set whose job is stopping a tampered request from smuggling a step knob;
# widening it to carry a snapshot binding would weaken exactly that. It is a
# first-class TaskSpec field, validated here and carried through — a gate that
# stripped it would silently unpin the run it was asked to pin.


class PolicyError(ValueError):
    """Raised when a request cannot be reduced to a safe read-only V1 task."""


def authorize_repo_path(repo: str, raw_path: Any, settings: Any) -> str:
    """Canonicalize and authorize a caller-supplied checkout for `repo`, or "".

    Two checks, both required, because either alone leaves a real hole:

    1. **Identity** — the checkout's `origin` must be the alias's configured
       GitHub identity. A root allowlist alone would let an allowed alias be
       paired with a *different* checkout sitting under the same root, and the
       run would review the wrong repository entirely while every other guard
       said yes.
    2. **Containment** — the canonical path must sit under an allowed root, so a
       genuine clone of the right repository in an arbitrary location is still
       refused. Which clone a bot may point at is an operator's decision.

    Fails closed: an unverifiable identity (no configured full name and no
    parseable origin) is refused rather than assumed."""
    if raw_path in (None, ""):
        return ""
    if not isinstance(raw_path, str):
        raise PolicyError(
            f"repo_path must be a string, got {type(raw_path).__name__}")
    if settings is None:
        raise PolicyError("repo_path cannot be authorized without settings")

    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir() or not (path / ".git").exists():
        raise PolicyError(f"repo_path is not a Git checkout: {path}")

    from ..intent import _remote_full_name, repo_identity

    want = repo_identity(repo, settings)
    if not want:
        raise PolicyError(
            f"repo {repo!r} has no known GitHub identity (set REPO_FULL_NAMES "
            "or configure its checkout), so an explicit repo_path cannot be "
            "verified to belong to it")
    got = _remote_full_name(str(path))
    if not got:
        raise PolicyError(
            f"repo_path {path} has no parseable origin remote, so it cannot be "
            f"verified as a checkout of {want}")
    if got.lower() != str(want).lower():
        raise PolicyError(
            f"repo_path {path} is a checkout of {got}, not {want}")

    roots = [str(Path(r).expanduser().resolve())
             for r in (settings.allowed_repo_roots or [])]
    if not any(str(path) == r or str(path).startswith(r + os.sep)
               for r in roots):
        raise PolicyError(
            f"repo_path {path} is outside the allowed roots {sorted(roots)}; "
            "set MCP_ALLOWED_REPO_ROOTS to permit it")
    return str(path)


def enforce_mcp_policy(raw: dict[str, Any], *, allowed_repos: list[str],
                       settings: Any = None) -> TaskSpec:
    """Reduce `raw` (an untrusted tool-call / `request.json` dict) to a safe,
    read-only `TaskSpec`, or raise `PolicyError`.

    Structural guarantees, independent of what `raw` claims:
    - `kind` MUST be one of `READ_ONLY_KINDS` (pr_review / issue_answer /
      issue_filter). Any write/push-capable kind (rebase, debug, profile) is
      refused — this is what makes the surface read-only regardless of the file.
    - `post` is forced False (no outward writes over MCP, ever, in V1).
    - `repo` MUST be in `allowed_repos`.
    - `pr` / `issue`, when present, MUST be positive integers.
    - `params` is stripped to the allow-set; the quality-only bounded signal list
      can guide assessment but cannot grant a step knob or write authority.
    - `expected_head_sha`, when present, MUST be a full 40-hex commit id, and is
      **carried through** rather than stripped: it only ever narrows the run
      (review this head or stop as stale), so dropping it would silently unpin
      a request that asked to be pinned.
    """
    if not isinstance(raw, dict):
        raise PolicyError("request is not an object")

    kind = raw.get("kind")
    if kind not in READ_ONLY_KINDS:
        raise PolicyError(
            f"kind {kind!r} is not permitted over MCP; allowed: "
            f"{sorted(READ_ONLY_KINDS)}")

    repo = raw.get("repo")
    if isinstance(repo, str) and "/" in repo and settings is not None:
        # full `owner/repo` form: resolve through the same identity validator
        # the CLI uses, so aliases mean the same thing on every surface
        from ..intent import resolve_repo_alias

        owner, _, name = repo.partition("/")
        alias = resolve_repo_alias(owner, name, settings)
        if alias is None:
            raise PolicyError(f"repo {repo!r} does not match any configured repo")
        repo = alias
    if repo not in allowed_repos:
        raise PolicyError(
            f"repo {repo!r} not in the MCP allowlist {sorted(allowed_repos)}")

    pr = _positive_or_none(raw.get("pr"), "pr")
    issue = _positive_or_none(raw.get("issue"), "issue")

    raw_params = raw.get("params") or {}
    if not isinstance(raw_params, dict):
        raise PolicyError("params must be an object")
    params = {k: v for k, v in raw_params.items() if k in _ALLOWED_PARAMS}
    if "review_depth" in params:  # strict: a typo must not silently pass
        if str(params["review_depth"]).lower() not in _REVIEW_DEPTHS:
            raise PolicyError(
                f"review_depth {params['review_depth']!r} is not one of "
                f"{list(_REVIEW_DEPTHS)}")
        params["review_depth"] = str(params["review_depth"]).lower()
    if "carried_findings" in params:
        if kind != "pr_review":
            params.pop("carried_findings")
        else:
            from ..review.rechecks import validate_carried
            from ..contracts.models import InvalidRequestError
            try:
                validate_carried(params["carried_findings"])
            except InvalidRequestError as exc:
                raise PolicyError(str(exc)) from exc
    if "deterministic_signals" in params:
        if kind != "pr_quality":
            params.pop("deterministic_signals")
        else:
            signals = params["deterministic_signals"]
            if not isinstance(signals, list) or not all(
                    isinstance(item, str) for item in signals):
                raise PolicyError("deterministic_signals must be a list of strings")
            if len(signals) > 12 or any(len(item) > 500 for item in signals):
                raise PolicyError(
                    "deterministic_signals must contain at most 12 items of "
                    "at most 500 characters each")
            params["deterministic_signals"] = [item.strip() for item in signals]

    mode = raw.get("mode", "eco")
    if mode not in {"eco", "performance"}:
        raise PolicyError(
            f"mode {mode!r} is invalid; allowed: ['eco', 'performance']")

    expected_head_sha = _full_sha_or_empty(raw.get("expected_head_sha"))
    if "business_context" in params:
        if kind != "pr_review":
            params.pop("business_context")
        else:
            from ..review.frozen_context import validate_business_context
            from ..contracts.models import InvalidRequestError

            try:
                validate_business_context(params["business_context"], expected_head_sha, pr)
            except InvalidRequestError as exc:
                raise PolicyError(str(exc)) from exc
    # Re-authorized here on BOTH passes by design: the boundary check binds the
    # request, and the child's check re-derives it from `request.json`, which is
    # untrusted — a same-user host could rewrite the path between reservation
    # and execution.
    repo_path = authorize_repo_path(repo, raw.get("repo_path"), settings)

    # Build through the validated model. post is hard-forced False; report_only
    # is irrelevant for READ_ONLY_KINDS (they are read-only unless post), but we
    # normalize it off too for a clean record.
    return TaskSpec(kind=kind, mode=mode, repo=repo, pr=pr, issue=issue,
                    report_only=False, post=False, params=params,
                    expected_head_sha=expected_head_sha, repo_path=repo_path)


def enforce_strict_review_policy(raw: dict[str, Any], *,
                                 allowed_repos: list[str],
                                 settings: Any = None) -> TaskSpec:
    """Validate the Direct MCP's Strict compatibility path.

    Strict is the public name for the previous Eco PR-review workflow. It cannot
    select the performance tier or widen the task beyond ``pr_review``, and it
    cannot post. `expected_head_sha` rides through the shared gate below, so the
    snapshot binding survives on this path too.

    **`post` is refused, not restored.** This path used to force `post=False`
    for the shared gate and then put the caller's value back, making Strict the
    one MCP surface that could publish. A single publisher is a design
    requirement — two publishers mean two review markers and two head gates on
    one PR — and a capability flag advertising `supports_post_false` would not
    have stopped a caller from passing `post=true`. Removing the restore does
    not create an anomaly, it removes one: `enforce_mcp_policy` already
    hard-forces `post=False` for every other kind, and Strict was the outlier.
    Human-driven posting is unaffected; it lives on the CLI (`--yes` with
    `ALLOW_POST=1`), which is the surface that has a human on it.

    An explicit `post=true` is an error rather than a silent drop, so a caller
    that believes it is publishing finds out here instead of from the absence of
    a comment."""
    if not isinstance(raw, dict):
        raise PolicyError("request is not an object")
    if raw.get("kind") != "pr_review":
        raise PolicyError("strict mode only permits PR reviews")

    post = raw.get("post", False)
    if not isinstance(post, bool):
        raise PolicyError("post must be a boolean")
    if post:
        raise PolicyError(
            "strict mode cannot post: the MCP surface is never the publisher, "
            "so exactly one publisher owns a PR's review marker and head gate. "
            "Read the structured result and publish it yourself, or use the CLI "
            "with ALLOW_POST=1 for a human-driven post.")

    normalized = dict(raw)
    normalized["mode"] = "eco"
    normalized["post"] = False
    return enforce_mcp_policy(
        normalized, allowed_repos=allowed_repos, settings=settings)


def enforce_quality_review_policy(raw: dict[str, Any], *,
                                  allowed_repos: list[str],
                                  settings: Any = None) -> TaskSpec:
    """Validate the dedicated read-only PR quality workflow.

    The caller may provide bounded deterministic signals as hypotheses for the
    model to verify. They never widen permissions and are not themselves a
    quality verdict. Posting remains structurally impossible.
    """
    if not isinstance(raw, dict):
        raise PolicyError("request is not an object")
    if raw.get("kind") != "pr_quality":
        raise PolicyError("quality mode only permits PR quality assessments")
    if raw.get("post", False):
        raise PolicyError("quality mode cannot post; the caller owns publishing")
    normalized = dict(raw)
    normalized["mode"] = "eco"
    normalized["post"] = False
    return enforce_mcp_policy(
        normalized, allowed_repos=allowed_repos, settings=settings)


def _full_sha_or_empty(value: Any) -> str:
    """Coerce an optional `expected_head_sha` to "" or a full 40-hex sha.

    A prefix is refused rather than normalized: the only caller that pins a head
    (the reviewbot) holds the full sha from the GitHub API, and accepting a
    prefix would mean comparing it against a full sha somewhere downstream."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise PolicyError(
            f"expected_head_sha must be a string, got {type(value).__name__}")
    sha = value.strip().lower()
    if not FULL_SHA_RE.match(sha):
        raise PolicyError(
            "expected_head_sha must be exactly 40 hex chars (the full commit "
            f"id), got {value!r}")
    return sha


def _positive_or_none(value: Any, field: str) -> int | None:
    """Coerce `value` to a positive int, allow None/absent, else raise."""
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise PolicyError(f"{field} must be an integer, got {value!r}")
    if n <= 0:
        raise PolicyError(f"{field} must be positive, got {n}")
    return n
