"""Shared step infrastructure.

Two things live here so a step's definition can be self-contained:

1. the `@step` / `register_step` **self-registration** surface — decorating a
   handler (or calling `register_step` for factory-built handlers) records a
   `StepSpec` into the package-level collection when its module is loaded;
   `steps.register_builtin_steps` loads the vetted modules and flushes that
   collection into a `StepRegistry`. There is no separate
   `add(StepSpec(...))` block to keep in sync anymore.
2. the cross-module helpers every step file shares (`gh`, `repo_path`, `git`,
   `task_spec`, `gh_read_tools`, `post_step`) — one home instead of a
   late-import chain between step modules.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..step import FailureKind, StepContext, StepResult, StepSpec

# -- self-registration ---------------------------------------------------------

_COLLECTED: dict[str, StepSpec] = {}


def register_step(spec: StepSpec) -> StepSpec:
    """Imperative registration — for factory-built handlers (issue/post/phase
    steps whose handler is a closure, not a module-level `async def`)."""
    if spec.name in _COLLECTED:
        raise ValueError(f"step already registered: {spec.name}")
    _COLLECTED[spec.name] = spec
    return spec


def step(name: str, kind: str, risk: str, description: str = "", *, checkpoint: bool = True):
    """Decorator: bind a handler to its name + metadata in one place."""

    def deco(fn):
        """Register `fn` as the handler under the captured name/metadata, then
        return it unchanged so the decorated name still binds to the function."""
        register_step(StepSpec(name, kind, risk, fn, description, checkpoint))
        return fn

    return deco


def collected() -> list[StepSpec]:
    """The StepSpecs registered so far (via `@step` / `register_step`) — consumed
    once by `register_builtin_steps` to populate a StepRegistry."""
    return list(_COLLECTED.values())


# -- cross-module helpers ------------------------------------------------------

def repo_path(ctx: StepContext) -> Path | None:
    """The repo checkout path for this step: the step's `repo_path` param first,
    else the run-state `repo_path`, as a Path — or None when neither is set.

    Taking the PR-time worktree's shared hold here, rather than where the tree is
    created, is deliberate. `--resume` does not re-run a completed step: the
    executor replays its `state_updates` and skips the body, so a hold acquired
    inside `pr.fetch_diff` would exist on the first run and never on a resumed
    one — while the downstream review steps go on reading the tree the reaper is
    allowed to remove. Attaching it to *use* covers both, and the call is
    idempotent (a per-process memo), so the repeated hits this accessor takes
    cost one `dict` lookup."""
    spec = ctx.state.get("task_spec")
    frozen = spec.get("repo_path") if isinstance(spec, dict) else None
    # Precedence: the step's own param (a playbook pointing one step at a
    # scratch clone), then run state (which `pr.fetch_diff` overwrites with the
    # PR-time worktree — a run bound to a checkout still reviews the pinned tree
    # cut from it), then the spec's frozen path, so a step reached outside the
    # executor's state seed still agrees with the checkout planning saw.
    p = ctx.params.get("repo_path") or ctx.state.get("repo_path") or frozen
    if not p:
        return None
    path = Path(p)
    _hold_if_worktree(ctx, path)
    return path


def _hold_if_worktree(ctx: StepContext, path: Path) -> None:
    """Hold `path` for the rest of the run if it is a PR-time worktree. Never
    raises: a hold that cannot be taken is a reaping risk, not a review error."""
    try:
        from .. import worktrees

        root = worktrees.canonical(worktrees.worktree_root())
        if worktrees.canonical(path).startswith(root + "/"):
            worktrees.hold(path, ctx.run_dir)
    except (OSError, ValueError):
        pass


def require_repo(ctx: StepContext, *, must_exist: bool = True):
    """The repo path or a BLOCKED StepResult (concision K3 — replaces the
    per-step `repo is None -> BLOCKED` guard). Returns a `Path` on success."""
    repo = repo_path(ctx)
    if repo is None or (must_exist and not repo.exists()):
        return StepResult(False, FailureKind.BLOCKED,
                          f"repo checkout not configured (repo_path={repo}) — set "
                          "REPO_PATHS in .env or a adapter repo.path")
    return repo


def task_spec(ctx: StepContext) -> dict:
    """The run's TaskSpec dict from state, or `{}` when absent/malformed — a
    total accessor so callers can `.get()` fields without guarding."""
    spec = ctx.state.get("task_spec")
    return spec if isinstance(spec, dict) else {}


def from_state(ctx: StepContext, key: str, *, summary: str = "") -> StepResult | None:
    """The injected/offline early-return: if `key` is already in state, return an
    ok StepResult that re-publishes it (concision K7). Else None — do the fetch."""
    if key in ctx.state:
        return StepResult(True, summary=summary or f"{key} from state",
                          outputs={"state_updates": {key: ctx.state[key]}})
    return None


def published(summary: str, *, state: dict | None = None, failure=None,
              **outputs) -> StepResult:
    """Build a StepResult that publishes handoff state (concision K4). `state`
    is merged into `outputs['state_updates']` so B2 is easy to honor."""
    if state:
        outputs.setdefault("state_updates", {}).update(state)
    ok = failure is None
    return StepResult(ok, failure, summary=summary, outputs=outputs)


def no_llm_gap(ctx: StepContext, step: str, effect: str, *,
               summary: str) -> StepResult:
    """Record a `capability_gap` for a missing LLM and return an ok skip
    (concision K3 — replaces the repeated no-LLM block). E2."""
    ctx.trace.record("capability_gap", capability="llm", step=step, effect=effect)
    return StepResult(True, summary=summary)


def gh(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """Run the `gh` CLI with `args` in `cwd`, returning `(returncode, output)`
    where output is stdout or, if empty, stderr. A missing CLI yields
    `(127, ...)` instead of raising, so callers branch on the code, not an
    exception."""
    try:
        out = subprocess.run(["gh", *args], cwd=str(cwd) if cwd else None,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=120)
        return out.returncode, out.stdout or out.stderr
    except FileNotFoundError:
        return 127, "gh CLI not installed"


def git(repo: Path, *args: str, timeout: int = 120) -> tuple[int, str]:
    """Run `git args` inside `repo`, returning `(returncode, combined_output)`
    with stdout+stderr merged and stripped — one place for step handlers to shell
    out to git."""
    out = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         timeout=timeout)
    return out.returncode, (out.stdout + out.stderr).strip()


def gh_read_tools(repo: Path | None) -> dict:
    """Read-only gh tools for agent steps (int-coerced args — no injection)."""
    from ...tools import ToolDef

    def _view(kind: str, number, fields: str) -> str:
        """`gh <kind> view <number> --json <fields>`, coercing `number` to int
        (injection-safe) and truncating output to ~15k chars; returns a `gh
        failed: ...` string on nonzero exit instead of raising."""
        code, out = gh([kind, "view", str(int(number)), "--json", fields],
                       cwd=repo)
        return out[:15_000] if code == 0 else f"gh failed: {out[:400]}"

    def gh_pr_view(pr, **_: object) -> str:
        """Tool handler: return PR `pr`'s title/body/state/draft/mergeable/files
        as JSON text."""
        return _view("pr", pr, "title,body,state,isDraft,mergeable,files")

    def gh_issue_view(issue, **_: object) -> str:
        """Tool handler: return issue `issue`'s title/body/labels/comments as
        JSON text."""
        return _view("issue", issue, "title,body,labels,comments")

    def gh_ci_read(pr, **_: object) -> str:
        """Tool handler: return PR `pr`'s CI checks (name/state/bucket) as JSON
        text, truncated to ~10k chars, or a `gh failed: ...` string on error."""
        code, out = gh(["pr", "checks", str(int(pr)), "--json",
                        "name,state,bucket"], cwd=repo)
        return out[:10_000] if code == 0 else f"gh failed: {out[:400]}"

    def gh_issue_timeline(issue, **_: object) -> str:
        """Tool handler: referenced PRs/commits from the issue's timeline (W3b
        related-artifact mining — 'is a fix already in flight?'). Owner/repo
        comes from the checkout's own `gh repo view`, never from issue text
        (endpoint-injection guard); int-coerced issue number."""
        code, out = gh(["repo", "view", "--json", "nameWithOwner"], cwd=repo)
        if code != 0:
            return f"gh failed: {out[:400]}"
        try:
            full = str(__import__("json").loads(out or "{}")
                       .get("nameWithOwner") or "")
        except ValueError:
            full = ""
        if not full:
            return "gh failed: could not resolve repo nameWithOwner"
        code, out = gh(["api", f"repos/{full}/issues/{int(issue)}/timeline",
                        "-q", '.[] | select(.event=="cross-referenced" or '
                        '.event=="referenced") | {event, commit_id, '
                        'source_type: .source.issue.pull_request != null, '
                        'source_number: .source.issue.number, '
                        'source_title: .source.issue.title}'], cwd=repo)
        return out[:10_000] if code == 0 else f"gh failed: {out[:400]}"

    n = {"type": "integer"}
    return {
        "gh_pr_view": ToolDef("gh_pr_view", "Read PR metadata (read-only).",
                              {"type": "object", "properties": {"pr": n},
                               "required": ["pr"]}, gh_pr_view),
        "gh_issue_view": ToolDef("gh_issue_view", "Read an issue (read-only).",
                                 {"type": "object", "properties": {"issue": n},
                                  "required": ["issue"]}, gh_issue_view),
        "gh_ci_read": ToolDef("gh_ci_read", "Read a PR's CI checks (read-only).",
                              {"type": "object", "properties": {"pr": n},
                               "required": ["pr"]}, gh_ci_read),
        "gh_issue_timeline": ToolDef(
            "gh_issue_timeline",
            "PRs/commits referencing this issue (read-only timeline).",
            {"type": "object", "properties": {"issue": n},
             "required": ["issue"]}, gh_issue_timeline),
    }


def post_step(state_key: str, gh_args, what: str):
    """Factory for gated outward posting (PR comment / issue reply): explicit
    `post` intent AND ALLOW_POST=1, else dry-run."""

    async def handler(ctx: StepContext) -> StepResult:
        """Post `state[state_key]` via `gh_args(spec, body)` only when both the
        task's `post` intent and `ALLOW_POST` are set; otherwise a BLOCKED (no
        body) or an ok dry-run skip. On success traces a `posted_artifact` with
        the extracted URL; a nonzero gh exit escalates."""
        body = ctx.state.get(state_key, "")
        spec = task_spec(ctx)
        if not body:
            return StepResult(False, FailureKind.BLOCKED, f"no {state_key} to post")
        if not spec.get("post"):
            return StepResult(True, summary=f"not posting {what} (post flag not set)")
        if not ctx.settings.allow_post:
            return StepResult(True, summary=f"dry-run (ALLOW_POST=0): would post {what} "
                                            f"({len(body)} chars)",
                              outputs={"dry_run": True, "body": body[:2_000]})
        repo = repo_path(ctx)
        args = gh_args(spec, body)
        code, out = gh(args, cwd=repo)
        if code != 0:
            return StepResult(False, FailureKind.ESCALATE, f"posting failed: {out[:400]}")
        url_match = re.search(r"https://\S+", out or "")
        url = url_match.group(0) if url_match else ""
        ctx.trace.record("posted_artifact", what=what, url=url,
                         pr=spec.get("pr"), issue=spec.get("issue"))
        return StepResult(True, summary=f"posted {what}",
                          outputs={"url": url} if url else {})

    return handler


def record_debug_memory(ctx, *, module: str, symptom: str, root_cause: str,
                        fix_summary: str, files: list, verification: str) -> bool:
    """Persist a resolved failure/fix into the repo-scoped debug memory (the
    shared pool when no adapter owns the repo). The write contract (root cause
    + verification required) is enforced by the store; a failed write is traced
    and swallowed — closing the learning loop must never fail the fix itself."""
    try:
        from ...memory.debug_memory import DebugMemory
        from ...memory.paths import KnowledgePaths
        from ..agent_runtime.knowledge import _resolve_adapter

        adapter = _resolve_adapter(ctx)
        spec = ctx.state.get("task_spec") or {}
        db = KnowledgePaths.resolve(
            ctx.settings, str(spec.get("repo", "")),
            adapter_root=adapter.root if adapter is not None else None,
        ).shared_write_db
        DebugMemory(db).record(
            repo=str(spec.get("repo", "")), module=module,
            run_id=ctx.run_dir.name, symptom=symptom, root_cause=root_cause,
            fix_summary=fix_summary, files=files, verification=verification)
        ctx.trace.record("debug_memory_recorded", module=module,
                         symptom=symptom[:120])
        return True
    except Exception as exc:  # noqa: BLE001
        ctx.trace.record("debug_memory_write_failed", error=str(exc)[:200])
        return False
