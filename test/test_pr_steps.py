import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from infermatrix_copilot.engine.steps import register_builtin_steps
from infermatrix_copilot.engine.steps.pr import extract_signature
from infermatrix_copilot.engine.registry import StepRegistry
from infermatrix_copilot.engine.step import FailureKind, StepContext, StepResult
from infermatrix_copilot.push import PushPolicy


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo, check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


@pytest.fixture()
def pr_repos(tmp_path):
    """origin repo with main + a 'feature' PR branch, and a working clone."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@e.c")
    _git(origin, "config", "user.name", "t")
    (origin / "core.py").write_text("x = 1\n")
    (origin / "docs.md").write_text("# docs\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "base")
    _git(origin, "checkout", "-q", "-b", "feature")
    (origin / "feature.py").write_text("f = 1\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "pr change")
    _git(origin, "checkout", "-q", "main")

    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True,
                   capture_output=True)
    _git(work, "config", "user.email", "t@e.c")
    _git(work, "config", "user.name", "t")
    return origin, work


def _ctx(settings, trace, tmp_path, state, params=None, llm=None):
    return StepContext(settings=settings, state=state, params=params or {},
                       run_dir=tmp_path / "rundir", trace=trace, llm=llm)


@pytest.fixture()
def registry():
    return register_builtin_steps(StepRegistry())


def test_strict_frozen_discussion_and_ci_are_used_without_live_metadata_fetch(
    registry, settings, trace, tmp_path, pr_repos, monkeypatch,
):
    from infermatrix_copilot.engine.steps.pr import fetch

    _, work = pr_repos
    head = _git(work, "rev-parse", "HEAD")
    context = {"pull": {"number": 7, "head_sha": head, "title": "Frozen title", "state": "open"},
        "pr_review_context": [{"body": "Frozen discussion concern", "thread_ref": "comment:1"}],
        "ci_evidence": {"head_sha": head, "read_status": "partial", "checked_at": "dispatch-time"}}
    state = {"repo_path": str(work), "task_spec": {"pr": 7, "expected_head_sha": head,
        "params": {"business_context": context}}}
    monkeypatch.setattr(fetch, "_fetch_at_one_head", lambda *_args: (
        "diff --git a/core.py b/core.py\n", "", head, str(work), "frozen source"))
    monkeypatch.setattr(fetch, "_gh", lambda *_args, **_kwargs: pytest.fail("live metadata replaced frozen input"))
    ctx = _ctx(settings, trace, tmp_path, state)
    assert asyncio.run(registry.get("pr.fetch_diff").handler(ctx)).ok
    assert asyncio.run(registry.get("pr.gate_check").handler(ctx)).ok
    assert "Frozen discussion concern" in state["pr_context"]
    assert "dispatch-time" in state["gate_report"] and '"partial"' in state["gate_report"]
    assert state["pr_state"] == "OPEN"


def test_checkout_sets_branch_and_push_policy(registry, settings, trace, tmp_path, pr_repos):
    _, work = pr_repos
    state = {"repo_path": str(work), "task_spec": {"pr": 7},
             "pr_meta": {"headRefName": "feature", "baseRefName": "main",
                         "remote": "origin"}}
    step = registry.get("pr.checkout_branch")
    result = asyncio.run(step.handler(_ctx(settings, trace, tmp_path, state,
                                           params={"force_push": True})))
    assert result.ok, result.summary
    assert _git(work, "branch", "--show-current") == "pr-7-feature"
    policy = state["push_policy"]
    assert isinstance(policy, PushPolicy)
    assert policy.allowed and policy.branch == "feature" and policy.force_with_lease


def test_checkout_report_only_disallows_push(registry, settings, trace, tmp_path,
                                             pr_repos, monkeypatch):
    from infermatrix_copilot.engine import worktrees
    from infermatrix_copilot.engine.lifecycle import finalize

    monkeypatch.setattr(worktrees, "worktree_root", lambda: tmp_path / "private")
    _, work = pr_repos
    original_head = _git(work, "rev-parse", "HEAD")
    state = {"repo_path": str(work), "task_spec": {"pr": 7, "report_only": True},
             "pr_meta": {"headRefName": "feature", "remote": "origin"}}
    step = registry.get("pr.checkout_branch")
    result = asyncio.run(step.handler(_ctx(settings, trace, tmp_path, state)))
    assert result.ok, result.summary
    assert state["push_policy"].allowed is False
    private = Path(state["repo_path"])
    assert private != work and private.is_dir()
    assert _git(private, "rev-parse", "HEAD") == _git(work, "rev-parse", "origin/feature")
    assert _git(work, "rev-parse", "HEAD") == original_head
    assert _git(work, "branch", "--show-current") == "main"
    assert _git(work, "status", "--porcelain") == ""
    assert "pr-7-feature" not in _git(work, "branch", "--list")
    assert result.outputs["state_updates"]["repo_path"] == str(private)
    asyncio.run(finalize(tmp_path / "rundir", None))


def test_report_only_rebase_stays_private_across_resume(
    registry, settings, trace, tmp_path, pr_repos, monkeypatch,
):
    from infermatrix_copilot.engine import worktrees
    from infermatrix_copilot.engine.lifecycle import finalize
    from infermatrix_copilot.engine.steps.pr.rebase import _rebase_in_progress

    monkeypatch.setattr(worktrees, "worktree_root", lambda: tmp_path / "private")
    origin, work = pr_repos
    (origin / "other.py").write_text("o = 1\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "main moves on")
    original_head = _git(work, "rev-parse", "HEAD")
    state = {"repo_path": str(work), "task_spec": {"pr": 7, "report_only": True},
             "pr_meta": {"headRefName": "feature", "remote": "origin"}}
    checkout = asyncio.run(registry.get("pr.checkout_branch").handler(
        _ctx(settings, trace, tmp_path, state)))
    assert checkout.ok, checkout.summary
    private = Path(state["repo_path"])
    # A linked worktree has a .git file. Rebase state lives in its Git metadata.
    marker = Path(_git(private, "rev-parse", "--git-path", "rebase-merge"))
    if not marker.is_absolute():
        marker = private / marker
    marker.mkdir()
    assert _rebase_in_progress(private)
    marker.rmdir()
    assert not _rebase_in_progress(private)
    # Executor resume replays only JSON-simple state_updates, not the handler.
    resumed = {"task_spec": state["task_spec"],
               **checkout.outputs["state_updates"]}
    for name in ("pr.rebase_onto_base", "pr.analyze_diff"):
        result = asyncio.run(registry.get(name).handler(
            _ctx(settings, trace, tmp_path, resumed)))
        assert result.ok, f"{name}: {result.summary}"
    assert (private / "other.py").exists() and (private / "feature.py").exists()
    assert resumed["affected_modules"] == ["root"]
    assert _git(private, "rev-parse", "HEAD") != checkout.outputs["state_updates"]["pr_initial_head_sha"]
    assert _git(work, "rev-parse", "HEAD") == original_head
    assert _git(work, "branch", "--show-current") == "main"
    assert _git(work, "status", "--porcelain") == ""
    assert not (work / "other.py").exists()
    assert not resumed["push_policy"]["allowed"]

    # A second run of the same PR gets a distinct mutable checkout.
    second_root = tmp_path / "second"
    second_state = {"repo_path": str(work), "task_spec": state["task_spec"],
                    "pr_meta": state["pr_meta"]}
    second = asyncio.run(registry.get("pr.checkout_branch").handler(
        _ctx(settings, trace, second_root, second_state)))
    assert second.ok and second_state["repo_path"] != str(private)
    asyncio.run(finalize(tmp_path / "rundir", None))
    asyncio.run(finalize(second_root / "rundir", None))


def test_report_only_rebase_rejects_redirected_or_missing_private_tree(
    registry, settings, trace, tmp_path, pr_repos, monkeypatch,
):
    from infermatrix_copilot.engine import worktrees
    from infermatrix_copilot.engine.lifecycle import finalize

    monkeypatch.setattr(worktrees, "worktree_root", lambda: tmp_path / "private")
    _, work = pr_repos
    original_head = _git(work, "rev-parse", "HEAD")
    state = {"repo_path": str(work), "task_spec": {"pr": 7, "report_only": True},
             "pr_meta": {"headRefName": "feature", "remote": "origin"}}
    dest = worktrees.mutable_dest_for(work, 7, tmp_path / "rundir")
    dest.parent.mkdir(parents=True)
    dest.symlink_to(work, target_is_directory=True)
    refused = asyncio.run(registry.get("pr.checkout_branch").handler(
        _ctx(settings, trace, tmp_path, state)))
    assert not refused.ok and "redirected" in refused.summary
    assert _git(work, "rev-parse", "HEAD") == original_head
    assert _git(work, "branch", "--show-current") == "main"
    dest.unlink()

    checkout = asyncio.run(registry.get("pr.checkout_branch").handler(
        _ctx(settings, trace, tmp_path, state)))
    assert checkout.ok, checkout.summary
    resumed = {"task_spec": state["task_spec"],
               **checkout.outputs["state_updates"]}
    _git(dest, "checkout", "-q", "-b", "unexpected")
    attached = asyncio.run(registry.get("pr.rebase_onto_base").handler(
        _ctx(settings, trace, tmp_path, resumed)))
    assert not attached.ok and "detached" in attached.summary
    # A real retry starts after run teardown releases the prior shared hold.
    asyncio.run(finalize(tmp_path / "rundir", None))
    retry_state = {"repo_path": str(work), "task_spec": state["task_spec"],
                   "pr_meta": state["pr_meta"]}
    retry = asyncio.run(registry.get("pr.checkout_branch").handler(
        _ctx(settings, trace, tmp_path, retry_state)))
    assert not retry.ok and "detached" in retry.summary
    assert _git(work, "branch", "--show-current") == "main"
    _git(dest, "checkout", "-q", "--detach")
    asyncio.run(finalize(tmp_path / "rundir", None))
    _git(work, "worktree", "remove", "--force", str(dest))
    missing = asyncio.run(registry.get("pr.rebase_onto_base").handler(
        _ctx(settings, trace, tmp_path, resumed)))
    assert not missing.ok and "missing" in missing.summary
    assert _git(work, "rev-parse", "HEAD") == original_head


def test_patch_gate_reviews_committed_range_and_push_binds_head(
    registry, settings, trace, tmp_path, git_repo, monkeypatch,
):
    from infermatrix_copilot.engine.steps.review import patch_gate
    from infermatrix_copilot.review.reviewer import ReviewVerdict

    base = _git(git_repo, "rev-parse", "HEAD")
    (git_repo / "mod_a.py").write_text("A = 2\n")
    _git(git_repo, "add", "mod_a.py")
    _git(git_repo, "commit", "-q", "-m", "fix committed change")
    seen = []
    def approve(_llm, *, diff_text, **_kwargs):
        seen.append(diff_text)
        return ReviewVerdict("lgtm")
    monkeypatch.setattr(patch_gate, "run_patch_review", approve)
    state = {
        "repo_path": str(git_repo), "task_spec": {"kind": "pr_debug"},
        "pr_initial_head_sha": base,
        "push_policy": PushPolicy(allowed=True, branch="feature"),
    }
    result = asyncio.run(registry.get("review.patch_gate").handler(
        _ctx(settings, trace, tmp_path, state, params={"pre_push": True})
    ))
    assert result.ok and seen and "+A = 2" in seen[0]
    assert result.outputs["state_updates"]["approved_change_set"]["base_sha"] == base
    push = registry.get("ci.push")
    assert asyncio.run(push.handler(_ctx(settings, trace, tmp_path, state))).ok

    (git_repo / "mod_a.py").write_text("A = 3\n")
    _git(git_repo, "add", "mod_a.py")
    _git(git_repo, "commit", "-q", "-m", "changed after approval")
    stale = asyncio.run(push.handler(_ctx(settings, trace, tmp_path, state)))
    assert not stale.ok and "head changed" in stale.summary


def test_pre_push_patch_gate_requires_a_baseline_and_clean_tree(
    registry, settings, trace, tmp_path, git_repo,
):
    step = registry.get("review.patch_gate")
    state = {"repo_path": str(git_repo), "task_spec": {"kind": "pr_debug"}}
    missing = asyncio.run(step.handler(_ctx(
        settings, trace, tmp_path, state, params={"pre_push": True}
    )))
    assert not missing.ok and "explicit base" in missing.summary

    state["pr_initial_head_sha"] = _git(git_repo, "rev-parse", "HEAD")
    empty = asyncio.run(step.handler(_ctx(
        settings, trace, tmp_path, state, params={"pre_push": True}
    )))
    assert not empty.ok and "no committed changes" in empty.summary
    (git_repo / "mod_a.py").write_text("uncommitted\n")
    dirty = asyncio.run(step.handler(_ctx(
        settings, trace, tmp_path, state, params={"pre_push": True}
    )))
    assert not dirty.ok and "commit tracked" in dirty.summary


@pytest.mark.parametrize(
    ("reply", "status", "ok"),
    [("OK", "verified", True), ("PROBLEM: dropped import", "problem", False),
     ("probably fine", "inconclusive", True)],
)
def test_rebase_advisory_reports_explicit_status(
    registry, settings, trace, tmp_path, git_repo, reply, status, ok,
):
    from types import SimpleNamespace

    class LLM:
        available = True

        def create(self, **_kwargs):
            return SimpleNamespace(text=reply, usage={})

    state = {"repo_path": str(git_repo), "rebase_base_sha": _git(git_repo, "rev-parse", "HEAD")}
    result = asyncio.run(registry.get("agent.verify_module").handler(
        _ctx(settings, trace, tmp_path, state, llm=LLM())
    ))
    assert result.ok is ok
    assert result.outputs["verification_status"] == status


def test_clean_rebase_and_analyze(registry, settings, trace, tmp_path, pr_repos):
    origin, work = pr_repos
    # advance origin main with a NON-conflicting commit
    (origin / "other.py").write_text("o = 1\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "main moves on")

    state = {"repo_path": str(work), "task_spec": {"pr": 7},
             "pr_meta": {"headRefName": "feature", "remote": "origin"}}
    for name in ("pr.checkout_branch", "pr.rebase_onto_base", "pr.analyze_diff"):
        result = asyncio.run(registry.get(name).handler(
            _ctx(settings, trace, tmp_path, state)))
        assert result.ok, f"{name}: {result.summary}"
    # rebased on top of new main: both files reachable
    assert (work / "other.py").exists() and (work / "feature.py").exists()
    analyze = state["affected_modules"]
    assert analyze == ["root"]  # feature.py at top level, no adapter in sandbox
    assert state["primary_files"] == ["*feature.py"]


@pytest.mark.parametrize("report_only", [False, True])
def test_conflict_without_llm_aborts_and_escalates(registry, settings, trace,
                                                   tmp_path, pr_repos,
                                                   report_only, monkeypatch):
    from infermatrix_copilot.engine import worktrees
    from infermatrix_copilot.engine.lifecycle import finalize
    from infermatrix_copilot.engine.steps.pr.rebase import _rebase_in_progress

    monkeypatch.setattr(worktrees, "worktree_root", lambda: tmp_path / "private")
    origin, work = pr_repos
    # conflicting change on main touching the same line as a new feature commit
    (origin / "core.py").write_text("x = 2\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "main edits core")
    _git(origin, "checkout", "-q", "feature")
    (origin / "core.py").write_text("x = 3\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "feature edits core")
    _git(origin, "checkout", "-q", "main")

    state = {"repo_path": str(work), "task_spec": {"pr": 7, "report_only": report_only},
             "pr_meta": {"headRefName": "feature", "remote": "origin"}}
    assert asyncio.run(registry.get("pr.checkout_branch").handler(
        _ctx(settings, trace, tmp_path, state))).ok
    result = asyncio.run(registry.get("pr.rebase_onto_base").handler(
        _ctx(settings, trace, tmp_path, state)))
    assert not result.ok and result.failure is FailureKind.ESCALATE
    assert "core.py" in result.outputs["conflicts"]
    # rebase aborted -> workspace clean, no rebase in progress
    checkout = Path(state["repo_path"])
    assert _git(checkout, "status", "--porcelain") == ""
    assert not _rebase_in_progress(checkout)
    if report_only:
        assert checkout != work
        assert _git(work, "branch", "--show-current") == "main"
        assert (work / "core.py").read_text() == "x = 1\n"
        assert _git(work, "status", "--porcelain") == ""
    assert list(trace.events("rebase_conflict"))
    asyncio.run(finalize(tmp_path / "rundir", None))


def test_extract_signature_prefers_root_cause_over_symptom():
    log = (
        "collecting...\n"
        "E   ImportError: cannot import name 'SchedulerOutput'\n"
        "... later the engine dies ...\n"
        "APIConnectionError: Connection refused\n"
    )
    assert "ImportError" in extract_signature(log)
    assert extract_signature("") == "unknown failure"


def test_group_failures_and_cap(registry, settings, trace, tmp_path):
    failures = [
        {"name": "gpu-test-1", "log": "E   ImportError: cannot import name 'X'"},
        {"name": "gpu-test-2", "log": "blah\nE   ImportError: cannot import name 'X'"},
        {"name": "cpu-test", "log": "AssertionError: bad output"},
    ]
    state = {"ci_failures": failures, "task_spec": {"pr": 7}}
    result = asyncio.run(registry.get("pr.group_failures").handler(
        _ctx(settings, trace, tmp_path, state)))
    assert result.ok
    groups = state["failure_groups"]
    assert len(groups) == 2
    assert sorted(len(g["jobs"]) for g in groups) == [1, 2]

    settings.pr_debug_max_groups = 1
    result = asyncio.run(registry.get("pr.group_failures").handler(
        _ctx(settings, trace, tmp_path, dict(state))))
    assert not result.ok and result.failure is FailureKind.ESCALATE
    assert "safety cap" in result.summary

    settings.pr_debug_max_groups = 6
    result = asyncio.run(registry.get("pr.group_failures").handler(
        _ctx(settings, trace, tmp_path, dict(state), params={"max_groups": 1})
    ))
    assert not result.ok and "safety cap of 1" in result.summary


def test_debug_group_blocked_without_llm(registry, settings, trace, tmp_path):
    state = {"repo_path": "/tmp", "task_spec": {"pr": 7}}
    ctx = _ctx(settings, trace, tmp_path, state)
    ctx.item = {"signature": "E ImportError", "jobs": ["j1"]}
    result = asyncio.run(registry.get("agent.debug_group").handler(ctx))
    assert not result.ok and result.failure is FailureKind.BLOCKED


def test_debug_group_does_not_accept_model_success_without_a_commit(
    registry, settings, trace, tmp_path, git_repo, monkeypatch,
):
    from infermatrix_copilot.engine import agent_runtime

    async def claimed_success(*_args, **_kwargs):
        return StepResult(True, summary="done"), {
            "root_cause": "wrong condition", "fix_summary": "fixed",
            "verification": "pytest passed",
        }

    monkeypatch.setattr(agent_runtime, "run_agent_step", claimed_success)
    state = {"repo_path": str(git_repo), "task_spec": {"pr": 7}}
    ctx = _ctx(settings, trace, tmp_path, state)
    ctx.item = {"signature": "one failing job", "jobs": ["ci"]}
    result = asyncio.run(registry.get("agent.debug_group").handler(ctx))
    assert not result.ok and result.failure is FailureKind.BLOCKED
    assert "observed commit" in result.summary


def test_post_review_gating(registry, settings, trace, tmp_path):
    step = registry.get("pr.post_review")
    # post flag not set -> no-op
    state = {
        "review_text": "looks fine\n\n**Verdict:** COMMENT",
        "review_summary": "looks fine\n\n**Verdict:** COMMENT",
        "review_comments": [],
        "task_spec": {"pr": 7, "post": False},
        "pr_state": "OPEN",
    }
    result = asyncio.run(step.handler(_ctx(settings, trace, tmp_path, state)))
    assert result.ok and "not posting" in result.summary
    # post flag set but ALLOW_POST=0 -> dry-run
    state["task_spec"]["post"] = True
    result = asyncio.run(step.handler(_ctx(settings, trace, tmp_path, state)))
    assert result.ok and result.outputs.get("dry_run") is True
    assert result.outputs["payload"]["event"] == "COMMENT"


_REVIEW_DIFF = """\
diff --git a/a.py b/a.py
index 1111111..2222222 100644
--- a/a.py
+++ b/a.py
@@ -9,3 +9,4 @@
 context
-old
+new
 tail
+added
"""


def _finding(path, line, severity="major", text="fix this"):
    return {
        "file": path,
        "line": line,
        "severity": severity,
        "comment": text,
        "evidence": "verified against the hunk",
    }


def test_review_payload_validates_diff_lines_and_preserves_fallbacks():
    from infermatrix_copilot.engine.steps.pr.publish import _review_payload

    state = {
        "diff_text": _REVIEW_DIFF,
        "review_comments": [
            _finding("a.py", 10),
            _finding("a.py", 99, "minor", "stale line"),
            _finding("missing.py", 1, "minor", "wrong path"),
        ],
        "review_summary": "summary\n\n**Verdict:** REQUEST CHANGES",
        "review_text": "full\n\n**Verdict:** REQUEST CHANGES",
        "pr_state": "OPEN",
        "pr_head_sha": "a" * 40,
    }
    payload, downgraded = _review_payload(state)

    assert payload["event"] == "COMMENT"
    assert payload["commit_id"] == "a" * 40
    assert payload["comments"] == [{
        "path": "a.py",
        "line": 10,
        "side": "RIGHT",
        "body": "**[major]** fix this\n\nEvidence: verified against the hunk",
    }]
    assert downgraded == 2
    assert "a.py:99" in payload["body"]
    assert "missing.py:1" in payload["body"]


def test_review_payload_refuses_changed_head():
    from infermatrix_copilot.engine.steps.pr.publish import _review_payload

    state = {
        "diff_text": _REVIEW_DIFF,
        "review_comments": [_finding("a.py", 10)],
        "review_summary": "summary\n\n**Verdict:** REQUEST CHANGES",
        "pr_state": "OPEN",
        "pr_head_sha": "a" * 40,
    }
    with pytest.raises(ValueError, match="PR head changed after review"):
        _review_payload(state, current_head="b" * 40)


@pytest.mark.parametrize("reviewed_head", ["", "a" * 40])
@pytest.mark.parametrize("findings", [[], [_finding("a.py", 10)]])
def test_post_review_refuses_unbound_or_changed_head_without_any_write(
        registry, settings, trace, tmp_path, monkeypatch, reviewed_head, findings):
    from infermatrix_copilot.engine.steps.pr import publish

    settings.allow_post = True
    state = {"repo_path": str(tmp_path), "task_spec": {"pr": 7, "post": True},
             "diff_text": _REVIEW_DIFF, "review_comments": findings,
             "review_text": "Review complete.", "pr_head_sha": reviewed_head}
    monkeypatch.setattr(publish, "_repo_full_name", lambda ctx, repo: "owner/repo")

    def read_only_gh(args, cwd=None):
        assert args[:2] == ["pr", "view"], "stale reviews must not write GitHub"
        return 0, json.dumps({"headRefOid": "b" * 40})

    monkeypatch.setattr(publish, "_gh", read_only_gh)
    ctx = _ctx(settings, trace, tmp_path, state)
    result = asyncio.run(registry.get("pr.post_review").handler(ctx))
    assert not result.ok and result.failure is FailureKind.BLOCKED
    assert result.outputs["stale"] is True
    assert not (ctx.run_dir / "github_review_payload.json").exists()


@pytest.mark.parametrize(
    ("comments", "pr_state", "review_text", "event"),
    [
        ([_finding("a.py", 10, "major")], "OPEN", "", "COMMENT"),
        ([_finding("a.py", 10, "minor")], "OPEN", "", "COMMENT"),
        ([], "OPEN", "**Verdict:** APPROVE", "COMMENT"),
        ([_finding("a.py", 10, "major")], "MERGED", "", "COMMENT"),
    ],
)
def test_review_event_mapping(comments, pr_state, review_text, event):
    from infermatrix_copilot.engine.steps.pr.publish import _event_for_review

    assert _event_for_review(comments, pr_state, review_text) == event


@pytest.mark.parametrize("commit_count", [1, 100])
def test_post_review_submits_one_review_with_inline_comments(
        registry, settings, trace, tmp_path, monkeypatch, commit_count):
    from infermatrix_copilot.engine.steps.pr import publish

    settings.allow_post = True
    state = {
        "repo_path": str(tmp_path),
        "task_spec": {"pr": 7, "post": True},
        "diff_text": _REVIEW_DIFF,
        "review_comments": [
            _finding("a.py", 10),
            _finding("a.py", 99, "minor", "stale line"),
        ],
        "review_summary": "summary\n\n**Verdict:** REQUEST CHANGES",
        "review_text": "full\n\n**Verdict:** REQUEST CHANGES",
        "pr_state": "OPEN",
        "pr_head_sha": "b" * 40,
    }
    captured = {}
    monkeypatch.setattr(
        publish, "_repo_full_name", lambda ctx, repo: "owner/repo")

    def fake_gh(args, cwd=None):
        if args[:2] == ["pr", "view"]:
            # A longer PR's first 100 commits can end before the reviewed head.
            data = {"commits": [{"oid": "b" * 40 if commit_count == 1 else "a" * 40}
                                for _ in range(commit_count)]}
            if "headRefOid" in args[args.index("--json") + 1].split(","):
                data["headRefOid"] = "b" * 40
            return 0, json.dumps(data)
        captured["args"] = args
        captured["cwd"] = cwd
        payload_path = Path(args[args.index("--input") + 1])
        captured["payload"] = json.loads(payload_path.read_text(encoding="utf-8"))
        return 0, json.dumps({"html_url": "https://github.com/owner/repo/pull/7#review"})

    monkeypatch.setattr(publish, "_gh", fake_gh)
    result = asyncio.run(registry.get("pr.post_review").handler(
        _ctx(settings, trace, tmp_path, state)))

    assert result.ok, result.summary
    assert captured["args"][:3] == ["api", "--method", "POST"]
    assert captured["args"][3] == "repos/owner/repo/pulls/7/reviews"
    assert captured["payload"]["commit_id"] == "b" * 40
    assert len(captured["payload"]["comments"]) == 1
    assert result.outputs["inline"] == 1
    assert result.outputs["downgraded"] == 1
    assert result.outputs["event"] == "COMMENT"
    assert result.outputs["url"].endswith("#review")


@pytest.mark.parametrize("head", [None, ""])
def test_post_review_without_authoritative_head_does_not_write(
        registry, settings, trace, tmp_path, monkeypatch, head):
    from infermatrix_copilot.engine.steps.pr import publish

    settings.allow_post = True
    state = {"repo_path": str(tmp_path), "task_spec": {"pr": 7, "post": True},
             "diff_text": _REVIEW_DIFF, "review_comments": [_finding("a.py", 10)],
             "review_text": "Review complete.", "pr_head_sha": "b" * 40}
    monkeypatch.setattr(publish, "_repo_full_name", lambda ctx, repo: "owner/repo")

    def fake_gh(args, cwd=None):
        assert args[:2] == ["pr", "view"], "must not submit an unverified review"
        assert "headRefOid" in args[args.index("--json") + 1].split(",")
        return 0, json.dumps({"headRefOid": head, "commits": [{"oid": "b" * 40}]})

    monkeypatch.setattr(publish, "_gh", fake_gh)
    ctx = _ctx(settings, trace, tmp_path, state)
    result = asyncio.run(registry.get("pr.post_review").handler(ctx))

    assert not result.ok and result.failure is FailureKind.ESCALATE
    assert "PR head SHA missing" in result.summary
    assert not (ctx.run_dir / "github_review_payload.json").exists()


def test_post_review_api_failure_escalates_with_payload(
        registry, settings, trace, tmp_path, monkeypatch):
    from infermatrix_copilot.engine.steps.pr import publish

    settings.allow_post = True
    state = {
        "repo_path": str(tmp_path),
        "task_spec": {"pr": 7, "post": True},
        "diff_text": _REVIEW_DIFF,
        "review_comments": [_finding("a.py", 10)],
        "review_summary": "summary\n\n**Verdict:** REQUEST CHANGES",
        "review_text": "full\n\n**Verdict:** REQUEST CHANGES",
        "pr_state": "OPEN",
        "pr_head_sha": "c" * 40,
    }
    monkeypatch.setattr(
        publish, "_repo_full_name", lambda ctx, repo: "owner/repo")

    def fake_gh(args, cwd=None):
        if args[:2] == ["pr", "view"]:
            return 0, json.dumps({"headRefOid": "c" * 40})
        return 1, "HTTP 422 stale line"

    monkeypatch.setattr(publish, "_gh", fake_gh)

    result = asyncio.run(registry.get("pr.post_review").handler(
        _ctx(settings, trace, tmp_path, state)))

    assert not result.ok and result.failure is FailureKind.ESCALATE
    assert "422" in result.summary
    assert Path(result.outputs["artifacts"][0]).is_file()


def test_worktree_materialize_creates_and_reuses(git_repo, tmp_path):
    """materialize pins a detached worktree at a sha, reuses a matching one,
    and swaps a stale one; failures return (False, why) instead of raising."""
    import subprocess

    from infermatrix_copilot.engine import worktrees
    from infermatrix_copilot.engine.steps._common import git as _git

    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo,
                         capture_output=True, text=True).stdout.strip()
    dest = tmp_path / "wt"
    ok, detail = worktrees.materialize(git_repo, sha, dest, _git)
    assert ok and "created" in detail and (dest / "mod_a.py").exists()
    ok, detail = worktrees.materialize(git_repo, sha, dest, _git)
    assert ok and "reused" in detail
    ok, detail = worktrees.materialize(git_repo, "0" * 40, dest, _git)
    assert not ok and "failed" in detail


def test_worktree_key_separates_same_basename_repos(tmp_path):
    """Two clones whose directories share a basename must not share a tree.

    The old key was `<basename>-pr<n>`, so a fork and its upstream checked out
    as `a/vllm-omni` and `b/vllm-omni` collided — and at the same sha the
    collision was silent, since HEAD matched and the tree was reused."""
    from infermatrix_copilot.engine import worktrees

    a = tmp_path / "a" / "repo"
    b = tmp_path / "b" / "repo"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    sha = "c" * 40
    assert worktrees.dest_for(a, 7, sha) != worktrees.dest_for(b, 7, sha)
    # and distinct heads of one repo stay distinct, so no cross-run force-remove
    assert worktrees.dest_for(a, 7, sha) != worktrees.dest_for(a, 7, "d" * 40)


def test_worktree_reuse_refuses_a_foreign_tree(git_repo, tmp_path):
    """A path whose tree belongs to another repository is not reused on name."""
    import subprocess

    from infermatrix_copilot.engine import worktrees
    from infermatrix_copilot.engine.steps._common import git as _git

    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "t@e.x"],
                   check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "t"],
                   check=True)
    (other / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-qm", "c"], check=True)
    other_sha = subprocess.run(["git", "-C", str(other), "rev-parse", "HEAD"],
                               capture_output=True, text=True).stdout.strip()

    dest = tmp_path / "wt"
    ok, _ = worktrees.materialize(other, other_sha, dest, _git)
    assert ok
    # same path, same HEAD, different owning repository -> not ours
    owned, why = worktrees.owned_by(git_repo, dest, other_sha, _git)
    assert not owned and "different repository" in why


def test_strip_binary_patches_preserves_headers_and_omits_body():
    from infermatrix_copilot.engine.steps.pr.fetch import _strip_binary_patches

    diff = (
        "diff --git a/tests/ref.png b/tests/ref.png\n"
        "new file mode 100644\n"
        "GIT binary patch\n"
        "literal 1771357\n"
        "zcmW)ndpwi>`^S+qr_8D55JoF=Y7>RzF\n"
        "literal 0\n"
        "HcmV?d00001\n"
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n+++ b/src/a.py\n"
        "+code\n"
    )

    stripped, summaries = _strip_binary_patches(diff)

    assert "diff --git a/tests/ref.png b/tests/ref.png" in stripped
    assert "[BINARY PATCH OMITTED: literal 1771357 bytes, literal 0 bytes;" in stripped
    assert "zcmW)" not in stripped
    assert "+code" in stripped
    assert summaries == [{
        "sizes": [
            {"kind": "literal", "bytes": 1771357},
            {"kind": "literal", "bytes": 0},
        ],
        "omitted_lines": 5,
    }]


def test_fetch_diff_pins_pr_time_checkout(settings, trace, tmp_path, git_repo,
                                          monkeypatch):
    """pr.fetch_diff publishes repo_path pinned to the PR head (injected sha in
    tests) plus a checkout_note; the worktree lands under ~/.infermatrix-copilot."""
    import asyncio
    import subprocess

    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.engine.steps.pr import fetch as fetch_mod
    from infermatrix_copilot.run_trace import RunTrace

    from infermatrix_copilot.engine import worktrees as wt_mod

    monkeypatch.setattr(fetch_mod, "_gh",
                        lambda args, cwd=None: (0, "diff --git a/x b/x"))
    monkeypatch.setattr(wt_mod.Path, "home", staticmethod(lambda: tmp_path))
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo,
                         capture_output=True, text=True).stdout.strip()
    registry = register_builtin_steps(StepRegistry())
    settings.repo_paths = {"vllm-omni": str(git_repo)}
    state = {"task_spec": {"kind": "pr_review", "pr": 7, "repo": "vllm-omni"},
             "repo_path": str(git_repo), "pr_head_sha": sha}
    ctx = StepContext(settings=settings, state=state, params={},
                      run_dir=tmp_path / "run", trace=RunTrace(tmp_path / "t.jsonl"),
                      llm=None)
    result = asyncio.run(registry.get("pr.fetch_diff").handler(ctx))
    assert result.ok, result.summary
    upd = result.outputs["state_updates"]
    assert "PR-TIME TREE" in upd["checkout_note"]
    assert upd["pr_head_sha"] == sha
    # keyed on repo identity AND head, not just the PR number
    assert upd["repo_path"].endswith(f"-pr7-{sha[:12]}")
    assert (tmp_path / ".infermatrix-copilot" / "worktrees").exists()


def _pr_upstream(tmp_path):
    """A bare upstream + a clone, with `refs/pull/7/head` branched from the CURRENT
    base tip, and the clone's `refs/remotes/origin/main` left deliberately stale
    one commit behind. Returns `(work, base_tip, pr_head)`."""
    import subprocess

    up = tmp_path / "up.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(up)],
                   check=True)

    def g(*args, cwd=work):
        return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                              capture_output=True, text=True).stdout.strip()

    subprocess.run(["git", "clone", "-q", str(up), str(work)], check=True)
    g("config", "user.email", "t@e.x")
    g("config", "user.name", "t")
    (work / "base.py").write_text("A = 1\n")
    g("add", "-A")
    g("commit", "-qm", "base")
    g("push", "-q", "origin", "main")
    stale = g("rev-parse", "HEAD")
    # upstream moves on: this churn belongs to main, NOT to the PR
    (work / "churn.py").write_text("CHURN = 1\n")
    g("add", "-A")
    g("commit", "-qm", "churn")
    g("push", "-q", "origin", "main")
    base_tip = g("rev-parse", "HEAD")
    # the PR branches from the CURRENT tip, so a stale base would attribute the
    # churn commit to it
    g("checkout", "-q", "-b", "pr7")
    (work / "pr.py").write_text("PR = 1\n")
    g("add", "-A")
    g("commit", "-qm", "pr work")
    pr_head = g("rev-parse", "HEAD")
    g("push", "-q", "origin", "HEAD:refs/pull/7/head")
    g("checkout", "-q", "main")
    g("update-ref", "refs/remotes/origin/main", stale)  # the stale tracking ref
    return work, base_tip, pr_head


def test_pinned_diff_ignores_a_stale_origin_tracking_ref(tmp_path):
    """The base comes from the ref THIS run fetched, never `origin/<base>`.

    `git fetch origin <base> pull/N/head` gave `pull/N/head` no destination, so
    it landed in FETCH_HEAD while the refresh of `refs/remotes/origin/<base>` was
    only git's opportunistic tracking-ref update. A merge-base against that
    possibly-stale ref attributes unrelated upstream churn to the PR."""
    from infermatrix_copilot.engine.steps._common import git as _git
    from infermatrix_copilot.engine.steps.pr.fetch import (
        _fetch_pinned,
        _pinned_diff,
    )

    work, base_tip, pr_head = _pr_upstream(tmp_path)

    # the control: what the old stale-ref computation would have produced
    stale_base = _git(work, "rev-parse", "refs/remotes/origin/main")[1].strip()
    stale_mb = _git(work, "merge-base", stale_base, pr_head)[1].strip()
    stale_diff = _git(work, "diff", f"{stale_mb}..{pr_head}")[1]
    assert "churn.py" in stale_diff  # upstream's work, credited to the PR

    base_sha, err = _fetch_pinned(work, 7, "main", pr_head, "run-test-1")
    assert not err, err
    assert base_sha == base_tip  # the commit this fetch retrieved
    text, detail, derr = _pinned_diff(work, base_sha, pr_head)
    assert not derr, derr
    assert "pr.py" in text and "churn.py" not in text


def test_fetch_diff_uses_authoritative_head_with_truncated_commits(
        registry, settings, trace, tmp_path, monkeypatch):
    """The first 100 commits of a longer PR must not falsely trip the stale gate.

    The authoritative head governs the real fetch, diff and materialized tree,
    even when the returned commit list ends at an earlier commit.
    """
    from infermatrix_copilot.engine import worktrees as wt_mod
    from infermatrix_copilot.engine.steps.pr import fetch as fetch_mod

    work, _base_tip, pr_head = _pr_upstream(tmp_path)
    commits = [{"oid": f"{index:040x}"} for index in range(1, 101)]
    assert commits[-1]["oid"] != pr_head

    def fake_gh(args, cwd=None):
        if args[:2] == ["pr", "view"]:
            fields = args[args.index("--json") + 1].split(",")
            data = {"baseRefName": "main", "commits": commits}
            if "headRefOid" in fields:
                data["headRefOid"] = pr_head
            return 0, json.dumps(data)
        return 0, "{}"

    monkeypatch.setattr(fetch_mod, "_gh", fake_gh)
    monkeypatch.setattr(wt_mod.Path, "home", staticmethod(lambda: tmp_path))
    state = {"task_spec": {"kind": "pr_review", "pr": 7, "repo": "vllm-omni",
                           "expected_head_sha": pr_head},
             "repo_path": str(work)}
    result = asyncio.run(registry.get("pr.fetch_diff").handler(
        _ctx(settings, trace, tmp_path, state)))

    assert result.ok, result.summary
    updates = result.outputs["state_updates"]
    assert updates["pr_head_sha"] == pr_head
    assert "pr.py" in updates["diff_text"]
    assert (Path(updates["repo_path"]) / "pr.py").read_text() == "PR = 1\n"
    assert not list(trace.events("expected_head_mismatch"))


@pytest.mark.parametrize("head", [None, ""])
def test_resolve_pr_head_does_not_infer_missing_head_from_commits(
        tmp_path, monkeypatch, head):
    from infermatrix_copilot.engine.steps.pr import fetch as fetch_mod

    def fake_gh(args, cwd=None):
        assert "headRefOid" in args[args.index("--json") + 1].split(",")
        return 0, json.dumps({"baseRefName": "main", "headRefOid": head,
                              "commits": [{"oid": "a" * 40}]})

    monkeypatch.setattr(fetch_mod, "_gh", fake_gh)
    base, actual, error = fetch_mod._resolve_pr_head(tmp_path, 7)
    assert base == "main"
    assert actual == ""
    assert error == "PR head unresolvable from pr view"


def test_merged_pr_falls_back_to_the_api_diff(settings, trace, tmp_path,
                                              monkeypatch):
    """A merged PR's head is an ancestor of the base, so the three-dot diff is
    empty by definition. Reviewing nothing would be worse than the TOCTOU the
    local path exists to close — and a contained head cannot move, so the API
    diff is safe here even for a pinned request."""
    import asyncio
    import subprocess

    from infermatrix_copilot.engine import worktrees as wt_mod
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.engine.steps.pr import fetch as fetch_mod
    from infermatrix_copilot.run_trace import RunTrace

    work, _base_tip, pr_head = _pr_upstream(tmp_path)
    # merge the PR so main contains its head, exactly like a merged PR
    subprocess.run(["git", "merge", "-q", "--ff", pr_head], cwd=work, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=work, check=True)

    def fake_gh(args, cwd=None):
        if args[:2] == ["pr", "view"]:
            return 0, json.dumps({"baseRefName": "main", "headRefOid": pr_head})
        return 0, "diff --git a/pr.py b/pr.py\n+PR = 1\n"

    monkeypatch.setattr(fetch_mod, "_gh", fake_gh)
    monkeypatch.setattr(wt_mod.Path, "home", staticmethod(lambda: tmp_path))
    registry = register_builtin_steps(StepRegistry())
    trace_path = tmp_path / "t.jsonl"
    state = {"task_spec": {"kind": "pr_review", "pr": 7, "repo": "vllm-omni",
                           "expected_head_sha": pr_head},
             "repo_path": str(work)}
    ctx = StepContext(settings=settings, state=state, params={},
                      run_dir=tmp_path / "run", trace=RunTrace(trace_path),
                      llm=None)
    result = asyncio.run(registry.get("pr.fetch_diff").handler(ctx))

    assert result.ok, result.summary
    assert "pr.py" in result.outputs["state_updates"]["diff_text"]
    assert "merged" in result.summary
    fallbacks = list(RunTrace(trace_path).events("diff_fallback"))
    assert fallbacks and fallbacks[0]["reason"] == fetch_mod.CONTAINED_HEAD


def test_pinned_refs_are_run_scoped(tmp_path):
    """Two runs on one PR must not overwrite each other's pinned refs.

    The refspecs are forced, so a PR-keyed name would let the second run silently
    repoint the first run's base between its fetch and its diff."""
    from infermatrix_copilot.engine.steps._common import git as _git
    from infermatrix_copilot.engine.steps.pr.fetch import _fetch_pinned

    work, base_tip, pr_head = _pr_upstream(tmp_path)
    for run_id in ("run-a", "run-b"):
        base_sha, err = _fetch_pinned(work, 7, "main", pr_head, run_id)
        assert not err and base_sha == base_tip
    for run_id in ("run-a", "run-b"):
        code, out = _git(work, "rev-parse", f"refs/imx/{run_id}/head")
        assert code == 0 and out.strip() == pr_head


def test_fetch_pinned_detects_a_head_that_moved(tmp_path):
    """git's own confirmation of the head gate: if what we fetched is not the sha
    the API just reported, the PR moved and the run must not proceed."""
    from infermatrix_copilot.engine.steps.pr.fetch import _fetch_pinned

    work, _base_tip, pr_head = _pr_upstream(tmp_path)
    base_sha, err = _fetch_pinned(work, 7, "main", "f" * 40, "run-moved")
    assert not base_sha and "head moved during fetch" in err
    assert pr_head[:12] in err


def test_fetch_diff_blocks_on_a_stale_expected_head(settings, trace, tmp_path,
                                                    git_repo, monkeypatch):
    """A pinned request whose PR has moved stops before any fetch or worktree,
    and records both shas in the trace — where a failed step cannot lose them."""
    import asyncio
    import json as _json

    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import FailureKind, StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.engine.steps.pr import fetch as fetch_mod
    from infermatrix_copilot.run_trace import RunTrace

    actual, expected = "a" * 40, "b" * 40
    calls: list[list[str]] = []

    def fake_gh(args, cwd=None):
        calls.append(list(args))
        if args[:2] == ["pr", "view"]:
            return 0, _json.dumps({"baseRefName": "main", "headRefOid": actual})
        return 0, "diff --git a/x b/x"

    monkeypatch.setattr(fetch_mod, "_gh", fake_gh)
    monkeypatch.setattr(fetch_mod, "_git",
                        lambda *a, **k: pytest.fail("no git before the gate"))
    registry = register_builtin_steps(StepRegistry())
    trace_path = tmp_path / "t.jsonl"
    state = {"task_spec": {"kind": "pr_review", "pr": 7, "repo": "vllm-omni",
                           "expected_head_sha": expected},
             "repo_path": str(git_repo)}
    ctx = StepContext(settings=settings, state=state, params={},
                      run_dir=tmp_path / "run", trace=RunTrace(trace_path),
                      llm=None)
    result = asyncio.run(registry.get("pr.fetch_diff").handler(ctx))

    assert not result.ok and result.failure is FailureKind.BLOCKED
    assert expected[:12] in result.summary and actual[:12] in result.summary
    assert ["pr", "diff", "7"] not in calls  # never asked for a diff
    mismatch = list(RunTrace(trace_path).events("expected_head_mismatch"))
    assert mismatch and mismatch[0]["expected"] == expected
    assert mismatch[0]["actual"] == actual


def test_fetch_diff_matching_expected_head_proceeds(settings, trace, tmp_path,
                                                    git_repo, monkeypatch):
    """The gate is a filter, not a blocker: the right head reviews normally."""
    import asyncio
    import subprocess

    from infermatrix_copilot.engine import worktrees as wt_mod
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.engine.steps.pr import fetch as fetch_mod
    from infermatrix_copilot.run_trace import RunTrace

    monkeypatch.setattr(fetch_mod, "_gh",
                        lambda args, cwd=None: (0, "diff --git a/x b/x"))
    monkeypatch.setattr(wt_mod.Path, "home", staticmethod(lambda: tmp_path))
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo,
                         capture_output=True, text=True).stdout.strip()
    registry = register_builtin_steps(StepRegistry())
    state = {"task_spec": {"kind": "pr_review", "pr": 7, "repo": "vllm-omni",
                           "expected_head_sha": sha},
             "repo_path": str(git_repo), "pr_head_sha": sha}
    ctx = StepContext(settings=settings, state=state, params={},
                      run_dir=tmp_path / "run", trace=RunTrace(tmp_path / "t.jsonl"),
                      llm=None)
    result = asyncio.run(registry.get("pr.fetch_diff").handler(ctx))

    assert result.ok, result.summary
    assert result.outputs["state_updates"]["pr_head_sha"] == sha


@pytest.mark.parametrize("kind", ["pr_review", "pr_quality"])
def test_review_tasks_cannot_push_even_with_allow_push(registry, settings, trace, tmp_path, monkeypatch, kind):
    from infermatrix_copilot.engine.steps.pr import publish
    settings.allow_push = True
    state = {"repo_path": str(tmp_path), "task_spec": {"kind": kind},
             "push_policy": PushPolicy(allowed=True, remote="origin", branch="feature")}
    def unexpected(*args, **kwargs):
        raise AssertionError("review task attempted a git command")
    monkeypatch.setattr(publish.subprocess, "run", unexpected)
    result = asyncio.run(registry.get("ci.push").handler(_ctx(settings, trace, tmp_path, state)))
    assert not result.ok
    assert "comment-only" in result.summary
