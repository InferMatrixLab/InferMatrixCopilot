"""End-to-end Strict review, offline.

Every other test in this suite exercises one seam. This one drives the whole
contract the way the reviewbot will: `start_strict_review` -> policy -> reserve
-> claim -> the real `pr-review` playbook over a real git checkout -> terminal
`run_status.json` -> `get_result`'s structured payload. Only two things are
faked — the GitHub CLI and the reviewer model — because everything between them
is what PR 2 changed and what a mocked-out test would stop proving.

It is the offline half of the plan's "end-to-end on a real PR" check: same
assertions, no network and no API key.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from infermatrix_copilot import idempotency as idem
from infermatrix_copilot import run_status as rs
from infermatrix_copilot.engine import worktrees as wt_mod
from infermatrix_copilot.engine.agent_runtime import BASE_OUTPUT_SCHEMA
from infermatrix_copilot.engine.steps import _common as common_mod
from infermatrix_copilot.engine.steps.pr import fetch as fetch_mod
from infermatrix_copilot.llm import Block, Reply
from infermatrix_copilot.mcp_server import CopilotMCP

REPO_ROOT = Path(__file__).resolve().parents[1]

# The finding the scripted reviewer returns, carrying the internal bookkeeping
# real runs accumulate — the boundary must drop those, not the content.
FINDING = {
    "file": "feature.py", "line": 2, "severity": "major",
    "comment": "ZeroDivisionError when x is 0.",
    "evidence": "return 1 / x", "suggestion": "guard x == 0",
    "_verified": True, "_anchor_unverified": False,
    "corroborated_by": ["lens-1"],
}


class ScriptedLLM:
    """Replies in order; records that it was called at all."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[str] = []
        self.available = True

    def create(self, *, system, messages, tools=None, model=None,
               max_tokens=None, on_text=None, role=""):
        self.calls.append(role)
        if not self._replies:
            raise AssertionError("scripted LLM ran out of replies")
        return self._replies.pop(0)


def _review_reply(**extra) -> Reply:
    """A reply satisfying the agent runtime's structured-output contract."""
    base = {k: ([] if "list" in v else "x") for k, v in BASE_OUTPUT_SCHEMA.items()}
    base.update(status="success", summary="reviewed", confidence="high",
                next_action="none", failure_kind=None)
    base.update(extra)
    return Reply(blocks=[Block(type="text", text=json.dumps(base))])


def _git(*args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


def _pr_checkout(tmp_path: Path) -> tuple[Path, str]:
    """A real clone whose upstream carries `refs/pull/7/head`.

    Real git, not a fixture double: the head gate, the pinned `refs/imx/*`
    fetch and the SHA-keyed worktree are all git behavior, and mocking git would
    mock away the parts under test."""
    up, work = tmp_path / "up.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(up)],
                   check=True, capture_output=True)
    subprocess.run(["git", "clone", "-q", str(up), str(work)], check=True,
                   capture_output=True)
    _git("config", "user.email", "t@e.x", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "base.py").write_text("A = 1\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "base", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    _git("checkout", "-q", "-b", "pr7", cwd=work)
    (work / "feature.py").write_text("def risky(x):\n    return 1 / x\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "add feature", cwd=work)
    head = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "-q", "origin", "HEAD:refs/pull/7/head", cwd=work)
    _git("checkout", "-q", "main", cwd=work)
    return work, head


def _fake_gh(head: str):
    """Answer only the reads this playbook makes; anything else is a failure
    rather than a silent empty result, so an unmocked call cannot pass."""
    def gh(args, cwd=None):
        if args[:2] == ["pr", "view"]:
            fields = args[4] if len(args) > 4 else ""
            data: dict = {}
            if "baseRefName" in fields:
                data["baseRefName"] = "main"
            if "headRefOid" in fields:
                data["headRefOid"] = head
            if "commits" in fields:
                data["commits"] = [{"oid": head, "messageHeadline": "add feature"}]
            if "title" in fields:
                data.update(title="Add feature", body="Adds a divide helper.",
                            labels=[], headRefName="pr7", comments=[],
                            reviews=[])
            if "state" in fields:
                data.update(state="OPEN", isDraft=False, mergeable="MERGEABLE",
                            mergeStateStatus="CLEAN")
            return 0, json.dumps(data)
        if args[:2] == ["pr", "checks"]:
            return 0, "[]"
        if args[:2] == ["repo", "view"]:
            return 0, json.dumps({"nameWithOwner": "acme/widget"})
        return 1, "unexpected gh call: " + " ".join(args)
    return gh


class Harness:
    def __init__(self, core: CopilotMCP, llm: ScriptedLLM, work: Path, head: str):
        self.core, self.llm, self.work, self.head = core, llm, work, head

    def start(self, **overrides) -> str:
        request = {"kind": "pr_review", "repo": "widget", "pr": 7}
        request.update(overrides)
        return self.core.start_strict_review(request)

    def run(self, run_id: str) -> int:
        """Execute the reserved run in-process.

        The server would fork a child here; that path has its own tests, and
        forking would put the scripted model out of reach. Everything the child
        does — the run lock, the execution claim, the authoritative policy
        re-check, planning, the playbook — runs exactly as it would there."""
        return self.core.copilot.execute_strict_reserved(run_id)

    def result(self, run_id: str) -> dict:
        return self.core.get_result(run_id)


@pytest.fixture()
def strict(settings, tmp_path, monkeypatch):
    work, head = _pr_checkout(tmp_path)

    playbooks = tmp_path / "playbooks"
    playbooks.mkdir(exist_ok=True)
    shutil.copy(REPO_ROOT / "playbooks" / "pr-review.yaml",
                playbooks / "pr-review.yaml")
    settings.playbooks_dir = playbooks
    settings.repo_paths = {"widget": str(work)}
    settings.repo_full_names = {"widget": "acme/widget"}
    settings.mcp_repo_allowlist = ["widget"]
    settings.default_repo = "widget"
    settings.review_depth = "light"   # one model call, deterministically
    settings.eco_model = "fake-eco"
    settings.anthropic_api_key = "unused-by-the-scripted-model"

    monkeypatch.setattr(fetch_mod, "_gh", _fake_gh(head))
    monkeypatch.setattr(common_mod, "gh", _fake_gh(head))
    # PR-time worktrees default to ~/.infermatrix-copilot/worktrees, which is a
    # developer's real scratch directory. A test must never materialize into it.
    monkeypatch.setattr(wt_mod.Path, "home", staticmethod(lambda: tmp_path))

    core = CopilotMCP(settings)
    llm = ScriptedLLM([_review_reply(review_comments=[dict(FINDING)])])
    core.copilot.llm = llm
    try:
        yield Harness(core, llm, work, head)
    finally:
        core.close()


# ── the happy path ────────────────────────────────────────────────────────────
def test_public_sdk_strict_request_reaches_workflow_and_typed_publication_input(strict, monkeypatch):
    from infermatrix_copilot.sdk.v1 import (
        RepositoryRef, ReviewRuntime, StrictReviewRequest, review_head_error,
    )
    # Retain the fixture's real run service and workflow; only the transport
    # boundary is in-process so its scripted model is available to the executor.
    monkeypatch.setattr(strict.core._q, "put", lambda *_: None)
    runtime = object.__new__(ReviewRuntime)
    runtime._core = strict.core
    request = StrictReviewRequest(RepositoryRef("widget", "acme/widget"), 7, strict.head,
                                  "", "sdk-framework", review_depth="light")
    reserved = runtime.reserve_review(request)
    assert reserved.created
    assert runtime.start_review(request).run_id == reserved.run_id
    assert strict.run(reserved.run_id) == 0
    result = runtime.get_result(reserved.run_id)
    assert result.terminal and result.review.reviewed_head_sha == strict.head
    assert result.review.verdict == "REQUEST CHANGES" and result.review.findings
    assert review_head_error(result.review.reviewed_head_sha, strict.head) == ""
    assert review_head_error(result.review.reviewed_head_sha, "f" * 40)
    assert "report_path" not in result.payload


def test_pinned_review_returns_a_structured_result(strict):
    """What the reviewbot actually consumes: a verdict and findings as data, at
    a head it named, with no Markdown to scrape."""
    run_id = strict.start(expected_head_sha=strict.head,
                          idempotency_key="attempt-1")
    assert strict.run(run_id) == 0

    out = strict.result(run_id)
    assert out["state"] == rs.DONE
    result = out["result"]
    assert result["reviewed_head_sha"] == strict.head
    assert result["verdict"] == "REQUEST CHANGES"
    assert result["stale"] is False
    assert result["contract_version"]

    (comment,) = result["comments"]
    assert comment == {k: v for k, v in FINDING.items()
                       if not k.startswith("_") and k != "corroborated_by"}
    # the report paging stays for hosts that already use it
    assert out["report"] and "report_path" in out


def test_the_review_runs_against_the_pinned_worktree(strict):
    """The tree is cut at the requested head and keyed by repo identity + sha,
    so a concurrent run at another head cannot delete it."""
    run_id = strict.start(expected_head_sha=strict.head)
    assert strict.run(run_id) == 0

    progress = json.loads(
        (Path(strict.core.settings.run_root) / run_id / "progress.json")
        .read_text(encoding="utf-8"))
    tree = Path(progress["completed"]["fetch"]["outputs"]["state_updates"]
                ["repo_path"])
    assert tree.name.endswith(f"-pr7-{strict.head[:12]}")
    assert _git("rev-parse", "HEAD", cwd=tree) == strict.head
    assert (tree / "feature.py").is_file()
    # and this run's own pinned refs exist, holding base and head against gc
    assert _git("rev-parse", f"refs/imx/{run_id}/head",
                cwd=strict.work) == strict.head


def test_an_unpinned_review_still_works(strict):
    """The CLI path sends no head. It must not have been narrowed into
    requiring one."""
    run_id = strict.start()
    assert strict.run(run_id) == 0
    result = strict.result(run_id)["result"]
    assert result["verdict"] == "REQUEST CHANGES"
    assert result["reviewed_head_sha"] == strict.head
    assert result["stale"] is False


# ── the stale path ────────────────────────────────────────────────────────────
def test_a_moved_head_stops_before_reviewing_anything(strict):
    """The headline guarantee: a snapshot that no longer exists is refused, not
    silently reviewed at whatever the PR points at now."""
    stale_sha = "b" * 40
    run_id = strict.start(expected_head_sha=stale_sha)

    assert strict.run(run_id) != 0
    out = strict.result(run_id)
    assert out["state"] == rs.BLOCKED
    # the reason reaches the polling client, not just the child's console
    assert stale_sha[:12] in out["note"] and strict.head[:12] in out["note"]

    result = out["result"]
    assert result["stale"] is True
    assert result["expected_head_sha"] == stale_sha
    assert result["actual_head_sha"] == strict.head
    assert result["verdict"] == "" and result["comments"] == []
    # nothing was reviewed, and no tree was materialized for a refused snapshot
    assert strict.llm.calls == []
    worktrees = wt_mod.worktree_root()
    assert not worktrees.exists() or not list(worktrees.iterdir())


# ── retry semantics ───────────────────────────────────────────────────────────
def test_a_lost_response_retry_returns_the_finished_run(strict):
    """The reviewbot's real failure mode: it started a review, lost the run_id,
    and asks again. It must be handed the finished run — a second execution
    would review the same commit twice and post twice."""
    key = "attempt-42"
    first = strict.start(expected_head_sha=strict.head, idempotency_key=key)
    assert strict.run(first) == 0

    again = strict.start(expected_head_sha=strict.head, idempotency_key=key)
    assert again == first
    # the scripted model has exactly one reply left unused; a second execution
    # would consume it, so this asserts on work done, not just on the id
    assert len(strict.llm.calls) == 1
    assert strict.result(again)["result"]["verdict"] == "REQUEST CHANGES"


def test_a_second_child_for_a_finished_run_does_no_work(strict):
    """The serial-child window, end to end.

    A is spawned and the server dies before A records its pid; a retry
    legitimately reclaims the reservation and enqueues B; A then wakes, runs the
    whole review and marks it done. B arrives afterwards, to a free lock and a
    finished run. `RunLock` never saw the two overlap, so only the state check
    can stop B — which is why starting execution is a compare-and-set and not a
    write.

    A is modelled by a foreign pid because both children are this process here;
    the assertion is that B leaves A's ownership and A's outcome untouched."""
    run_id = strict.start(expected_head_sha=strict.head)
    run_dir = Path(strict.core.settings.run_root) / run_id
    assert rs.claim_for_execution(run_dir, child_pid=4242) is True   # child A
    rs.mark(run_dir, rs.DONE)

    assert strict.core.copilot.execute_strict_reserved(run_id) == 0  # child B

    status = rs.read_status(run_dir)
    assert status["state"] == rs.DONE      # never walked back to planning
    assert status["child_pid"] == 4242     # B did not take ownership
    assert strict.llm.calls == []          # and reviewed nothing


def test_a_new_attempt_key_reviews_again(strict):
    """A deliberate re-review is a new attempt id, and must not be deduped."""
    strict.llm._replies.append(_review_reply(review_comments=[]))
    first = strict.start(expected_head_sha=strict.head, idempotency_key="gen-1")
    strict.run(first)
    second = strict.start(expected_head_sha=strict.head, idempotency_key="gen-2")
    assert second != first
    strict.run(second)

    assert len(strict.llm.calls) == 2
    assert strict.result(second)["result"]["verdict"] == "APPROVE"


# ── the boundary refuses what it says it refuses ──────────────────────────────
def test_strict_cannot_be_asked_to_post(strict):
    """One publisher owns a PR's review marker; the MCP surface is never it."""
    from infermatrix_copilot.mcp_policy import PolicyError

    with pytest.raises(PolicyError, match="cannot post"):
        strict.start(post=True)
    assert not list(Path(strict.core.settings.run_root).glob("run-*"))


def test_a_key_reused_for_a_different_pr_is_refused(strict):
    """The index is a cache; the persisted request is the authority."""
    run_id = strict.start(expected_head_sha=strict.head, idempotency_key="k")
    assert strict.run(run_id) == 0
    with pytest.raises(idem.IdempotencyError, match="different request"):
        strict.start(pr=8, idempotency_key="k")


@pytest.mark.parametrize('kind', ['pending', 'unfinished', 'sandbox', 'host_timeout', 'not_sent', 'cleanup'])
def test_strict_unknown_native_outcome_keeps_original_sdk_identity(strict, monkeypatch, kind):
    from infermatrix_copilot.sdk.v1 import RepositoryRef, ReviewRuntime, StrictReviewRequest
    from infermatrix_copilot.providers.json_session import JSONSessionError
    from infermatrix_copilot.sandbox.base import SandboxPending
    error = JSONSessionError('injected missing original response', timeout=kind in ('host_timeout', 'not_sent'))
    if kind == 'pending':
        error.native_outcome_pending = True
    elif kind == 'unfinished':
        error.native_call_sent, error.native_call_completed = True, False
    elif kind == 'sandbox':
        error = SandboxPending('injected original execution requires reconciliation')
    elif kind == 'not_sent':
        error.native_call_sent = False
    elif kind == 'cleanup':
        from infermatrix_copilot.process_execution import ProcessCleanupError, ProcessOutcome
        error = ProcessCleanupError(ProcessOutcome('partial', '', 0))
    calls = []

    def unknown(**kwargs):
        calls.append(True)
        raise error

    monkeypatch.setattr(strict.llm, 'create', unknown)
    monkeypatch.setattr(strict.core._q, 'put', lambda *_: None)
    runtime = object.__new__(ReviewRuntime)
    runtime._core = strict.core
    request = StrictReviewRequest(RepositoryRef('widget', 'acme/widget'), 7, strict.head,
                                 '', 'original-unknown-' + kind, review_depth='light')
    handle = runtime.reserve_review(request)
    assert runtime.start_review(request).run_id == handle.run_id
    assert strict.run(handle.run_id) != 0
    expected = rs.BLOCKED if kind == 'not_sent' else rs.INTERRUPTED
    assert runtime.get_status(handle.run_id).state == expected
    assert runtime.get_result(handle.run_id).state == expected
    assert runtime.start_review(request).run_id == handle.run_id
    assert strict.run(handle.run_id) == 0  # duplicate child cannot execute again
    assert len(calls) == 1
    assert runtime.get_result(handle.run_id).state == expected
