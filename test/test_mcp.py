"""MCP server guardrails: read-only policy (incl. tamper defense), single-writer
run_status + ownership-aware reconciliation, report pagination, and the fact that
the exposed tool set is read-only only. All offline."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from infermatrix_copilot import run_status as rs
from infermatrix_copilot.cli.copilot import Copilot
from infermatrix_copilot.mcp_policy import (
    PolicyError,
    enforce_mcp_policy,
    enforce_quality_review_policy,
    enforce_strict_review_policy,
)
from infermatrix_copilot.task_spec import TaskSpec

ALLOW = ["vllm-omni"]


# ── policy gate (the structural read-only guarantee) ──────────────────────────
def test_policy_accepts_read_only_kinds_and_forces_post_off():
    for kind, extra in [("pr_review", {"pr": 5}),
                        ("pr_quality", {"pr": 5}),
                        ("issue_answer", {"issue": 9}),
                        ("issue_filter", {})]:
        spec = enforce_mcp_policy({"kind": kind, "repo": "vllm-omni", **extra},
                                  allowed_repos=ALLOW)
        assert spec.kind == kind
        assert spec.post is False and spec.report_only is False


def test_policy_preserves_valid_execution_mode():
    spec = enforce_mcp_policy(
        {"kind": "pr_review", "repo": "vllm-omni", "pr": 5,
         "mode": "performance"}, allowed_repos=ALLOW)
    assert spec.mode == "performance"


def test_policy_refuses_invalid_execution_mode():
    with pytest.raises(PolicyError, match="mode"):
        enforce_mcp_policy(
            {"kind": "pr_review", "repo": "vllm-omni", "pr": 5,
             "mode": "turbo"}, allowed_repos=ALLOW)


@pytest.mark.parametrize("bad", [
    {"kind": "pr_rebase", "repo": "vllm-omni"},      # write-capable
    {"kind": "pr_debug", "repo": "vllm-omni"},       # mutates (checkout_branch)
    {"kind": "repo_profile", "repo": "vllm-omni"},   # writes knowledge
    {"kind": "pr_review", "repo": "some-other-repo"},  # off allowlist
    {"kind": "pr_review", "repo": "vllm-omni", "pr": -3},  # non-positive
    {"kind": "pr_review", "repo": "vllm-omni", "pr": "abc"},  # non-int
    {"kind": None, "repo": "vllm-omni"},             # missing kind
])
def test_policy_refuses_unsafe_requests(bad):
    with pytest.raises(PolicyError):
        enforce_mcp_policy(bad, allowed_repos=ALLOW)


def test_policy_strips_unknown_params_and_post_flag():
    spec = enforce_mcp_policy(
        {"kind": "issue_answer", "repo": "vllm-omni", "issue": 1,
         "post": True, "params": {"force_push": True, "x": 1}},
        allowed_repos=ALLOW)
    assert spec.post is False
    assert spec.params == {}  # a tampered step knob cannot ride through


def test_policy_allows_validated_review_depth():
    spec = enforce_mcp_policy(
        {"kind": "pr_review", "repo": "vllm-omni", "pr": 7,
         "params": {"review_depth": "FULL", "force_push": True}},
        allowed_repos=ALLOW)
    assert spec.params == {"review_depth": "full"}  # normalized; rest stripped


def test_quality_policy_keeps_only_bounded_rule_hints():
    spec = enforce_quality_review_policy(
        {"kind": "pr_quality", "repo": "vllm-omni", "pr": 7,
         "mode": "performance", "params": {
             "deterministic_signals": ["Q1: short", "Q2: no tests"],
             "force_push": True,
         }},
        allowed_repos=ALLOW,
    )
    assert spec.kind == "pr_quality" and spec.mode == "eco"
    assert spec.params == {
        "deterministic_signals": ["Q1: short", "Q2: no tests"]
    }
    assert spec.post is False


def test_quality_policy_rejects_unbounded_or_non_string_hints():
    with pytest.raises(PolicyError, match="deterministic_signals"):
        enforce_quality_review_policy(
            {"kind": "pr_quality", "repo": "vllm-omni", "pr": 7,
             "params": {"deterministic_signals": [1]}},
            allowed_repos=ALLOW,
        )


def test_policy_rejects_invalid_review_depth():
    with pytest.raises(PolicyError, match="review_depth"):
        enforce_mcp_policy(
            {"kind": "pr_review", "repo": "vllm-omni", "pr": 7,
             "params": {"review_depth": "ful"}},  # typo must not pass silently
            allowed_repos=ALLOW)


def test_strict_policy_is_the_old_eco_review_and_never_posts():
    spec = enforce_strict_review_policy(
        {"kind": "pr_review", "repo": "vllm-omni", "pr": 7,
         "mode": "performance",
         "params": {"review_depth": "FULL", "force_push": True}},
        allowed_repos=ALLOW)

    assert spec == TaskSpec(
        kind="pr_review", repo="vllm-omni", pr=7, mode="eco", post=False,
        params={"review_depth": "full"})


def test_strict_refuses_an_explicit_post_rather_than_dropping_it():
    """Strict used to force post=False for the shared gate and then restore the
    caller's value, making it the one MCP path that could publish. Design
    requires a single publisher — two would mean two review markers and two head
    gates on one PR — and a capability flag would not have stopped a caller from
    asking. It is an error, not a silent drop, so a caller that believes it is
    publishing learns here rather than from the absence of a comment."""
    with pytest.raises(PolicyError, match="cannot post"):
        enforce_strict_review_policy(
            {"kind": "pr_review", "repo": "vllm-omni", "pr": 7, "post": True},
            allowed_repos=ALLOW)


HEAD = "a" * 40


def test_policy_carries_expected_head_sha_through():
    spec = enforce_mcp_policy(
        {"kind": "pr_review", "repo": "vllm-omni", "pr": 7,
         "expected_head_sha": HEAD.upper()},  # case-normalized, not rejected
        allowed_repos=ALLOW)
    assert spec.expected_head_sha == HEAD


def test_strict_policy_carries_expected_head_sha_through():
    # the binding must survive the Strict path too — this is the one that pins
    spec = enforce_strict_review_policy(
        {"kind": "pr_review", "repo": "vllm-omni", "pr": 7,
         "expected_head_sha": HEAD},
        allowed_repos=ALLOW)
    assert spec.expected_head_sha == HEAD


def test_policy_defaults_expected_head_sha_to_unpinned():
    spec = enforce_mcp_policy({"kind": "pr_review", "repo": "vllm-omni", "pr": 7},
                              allowed_repos=ALLOW)
    assert spec.expected_head_sha == ""


@pytest.mark.parametrize("bad", ["a" * 39, "a" * 41, "g" * 40, "abc1234", 12345,
                                 ["a" * 40]])
def test_policy_refuses_non_full_expected_head_sha(bad):
    # a prefix is refused rather than normalized: accepting one would mean
    # comparing a prefix against a full sha somewhere downstream
    with pytest.raises(PolicyError, match="expected_head_sha"):
        enforce_mcp_policy(
            {"kind": "pr_review", "repo": "vllm-omni", "pr": 7,
             "expected_head_sha": bad}, allowed_repos=ALLOW)


def test_task_spec_itself_rejects_a_short_head_sha():
    # the model is the last line of defense for non-MCP construction paths
    with pytest.raises(ValueError, match="expected_head_sha"):
        TaskSpec(kind="pr_review", repo="vllm-omni", pr=7,
                 expected_head_sha="abc1234")


def test_strict_policy_cannot_widen_beyond_pr_review():
    with pytest.raises(PolicyError, match="only permits PR reviews"):
        enforce_strict_review_policy(
            {"kind": "pr_rebase", "repo": "vllm-omni", "pr": 7,
             "post": True},
            allowed_repos=ALLOW)


# ── run_status: single writer + preserved ownership ───────────────────────────
def test_status_lifecycle_preserves_ownership(tmp_path):
    run_dir = tmp_path / "run-20260715-101010-abc123"
    rs.init_queued(run_dir, run_id=run_dir.name, owner_server_id="S1",
                   owner_server_pid=os.getpid())
    assert rs.read_status(run_dir)["state"] == rs.QUEUED
    rs.mark_child_started(run_dir, child_pid=4242)
    rs.mark(run_dir, rs.RUNNING)
    final = rs.mark(run_dir, rs.DONE)
    assert final["state"] == rs.DONE
    assert final["child_pid"] == 4242          # child pid preserved
    assert final["owner_server_id"] == "S1"    # ownership preserved across writes


# ── reconciliation ────────────────────────────────────────────────────────────
def test_pid_alive_treats_windows_invalid_pid_as_dead(monkeypatch):
    def invalid_pid(_pid, _signal):
        raise OSError(87, "The parameter is incorrect")

    monkeypatch.setattr(rs.os, "kill", invalid_pid)
    assert rs.pid_alive(4242) is False


def test_reconcile_after_wait_terminalizes_only_non_terminal(tmp_path):
    live = tmp_path / "run-20260715-101010-aaa111"
    rs.init_queued(live, run_id=live.name, owner_server_id="S", owner_server_pid=os.getpid())
    rs.mark(live, rs.RUNNING)
    assert rs.reconcile_after_wait(live)["state"] == rs.INTERRUPTED

    done = tmp_path / "run-20260715-101011-bbb222"
    rs.init_queued(done, run_id=done.name, owner_server_id="S", owner_server_pid=os.getpid())
    rs.mark(done, rs.DONE)
    rs.reconcile_after_wait(done)
    assert rs.read_status(done)["state"] == rs.DONE  # untouched


def test_reconcile_if_dead_respects_live_owner(tmp_path):
    run_root = tmp_path
    # live owner -> its queued run must be left alone (the multi-server bug)
    rs.register_server(run_root, "LIVE", os.getpid())
    live = tmp_path / "run-20260715-101012-ccc333"
    rs.init_queued(live, run_id=live.name, owner_server_id="LIVE", owner_server_pid=os.getpid())
    rs.reconcile_if_dead(live, run_root)
    assert rs.read_status(live)["state"] == rs.QUEUED

    # dead owner + no child -> reconciled to interrupted
    dead = tmp_path / "run-20260715-101013-ddd444"
    rs.init_queued(dead, run_id=dead.name, owner_server_id="GONE", owner_server_pid=2 ** 31 - 1)
    assert rs.reconcile_if_dead(dead, run_root)["state"] == rs.INTERRUPTED

    # dead owner but a LIVE child -> left (the child will write its own terminal)
    childrun = tmp_path / "run-20260715-101014-eee555"
    rs.init_queued(childrun, run_id=childrun.name, owner_server_id="GONE", owner_server_pid=2 ** 31 - 1)
    rs.mark_child_started(childrun, child_pid=os.getpid())  # our pid = alive
    rs.reconcile_if_dead(childrun, run_root)
    assert rs.read_status(childrun)["state"] != rs.INTERRUPTED


# ── reserve / execute_reserved (the child path) ───────────────────────────────
def test_reserve_run_is_fast_and_persists_queued(settings):
    cop = Copilot(settings)
    run_id, created = cop.reserve_run(
        TaskSpec(kind="pr_review", repo="vllm-omni", pr=1),
        owner_server_id="S1", owner_server_pid=1234)
    assert created is True  # an unkeyed reservation always creates
    run_dir = settings.run_root / run_id
    assert rs.read_status(run_dir)["state"] == rs.QUEUED
    req = run_dir / "request.json"
    assert json.loads(req.read_text())["kind"] == "pr_review"
    assert stat.S_IMODE(req.stat().st_mode) == 0o600  # least-privilege perms
    # no LLM was needed (the fixture LLM is unconfigured anyway)
    assert cop.llm.available is False


def test_execute_reserved_refuses_tampered_request_in_process(settings):
    cop = Copilot(settings)
    run_id, _ = cop.reserve_run(
        TaskSpec(kind="pr_review", repo="vllm-omni", pr=2),
        owner_server_id="S1", owner_server_pid=1234)
    run_dir = settings.run_root / run_id
    # a same-user process rewrites the reserved request to a write-capable task
    (run_dir / "request.json").write_text(json.dumps(
        {"kind": "pr_rebase", "repo": "evil", "pr": 2, "post": True}))
    code = cop.execute_reserved(run_id)
    st = rs.read_status(run_dir)
    assert code == 1
    assert st["state"] == rs.FAILED
    assert "not permitted" in st["note"]
    assert st["child_pid"] is not None  # child wrote its own pid first


def test_contained_run_dir_rejects_traversal(settings):
    cop = Copilot(settings)
    for bad in ["../etc", "run-x", "run-20260715-101010-abc123/../..", "", "nope"]:
        with pytest.raises(ValueError):
            cop._contained_run_dir(bad)


def test_cli_declined_confirm_leaves_no_run_dir(settings, monkeypatch):
    """CLI path is unchanged: it gates (plan-review + confirm) BEFORE creating a
    run dir, so a declined confirmation leaves no run-* directory behind. (The
    reserve-before-plan shape is MCP-only.)"""
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    cop = Copilot(settings)
    rc = cop.run_task(TaskSpec(kind="pr_review", repo="vllm-omni", pr=1), assume_yes=False)
    assert rc == 1  # user aborted
    dirs = list(settings.run_root.glob("run-*")) if settings.run_root.exists() else []
    assert dirs == []


# ── server core: pagination + status shape + read-only tool set ───────────────
def _core(settings):
    from infermatrix_copilot.mcp_server import CopilotMCP
    return CopilotMCP(settings)


def test_strict_readiness_reports_setup_gaps(settings):
    settings.strict_backend = "api"
    core = _core(settings)

    missing = core.strict_readiness("vllm-omni")

    assert any("model credential missing" in item for item in missing)
    assert any("checkout" in item for item in missing)
    assert any("pr-review playbook missing" in item for item in missing)


def test_strict_reservation_reports_idempotent_reuse_accurately(
        settings, monkeypatch):
    core = _core(settings)
    queued = []
    monkeypatch.setattr(core._q, "put", queued.append)

    def wrong_reservation_boundary(*_args, **_kwargs):
        raise AssertionError("RunService called the Copilot facade")

    monkeypatch.setattr(core.copilot, "reserve_run", wrong_reservation_boundary)
    monkeypatch.setattr(
        core.reservations,
        "reserve",
        lambda *_args, **_kwargs: ("run-20260829-010101-abcdef", False),
    )

    run_id, created = core.reserve_strict_review({
        "kind": "pr_review",
        "repo": "vllm-omni",
        "pr": 7,
        "post": False,
        "params": {"review_depth": "standard"},
        "expected_head_sha": "a" * 40,
        "idempotency_key": "attempt-1",
    })

    assert run_id == "run-20260829-010101-abcdef"
    assert created is False
    assert queued == []


def test_strict_readiness_requires_explicit_backend(settings, monkeypatch):
    # doc/features/provider-registry.md: selection is explicit — unset refuses
    # with the exact fix, never a silent api fallback
    missing = _core(settings).strict_readiness("vllm-omni")
    assert any("STRICT_BACKEND not set" in item for item in missing)

    # a harness backend without its CLI is a named gap, not a key complaint
    monkeypatch.setattr("infermatrix_copilot.providers.base.shutil.which",
                        lambda name: None)
    settings.strict_backend = "cursor"
    missing = _core(settings).strict_readiness("vllm-omni")
    assert any("CLI is not installed" in item for item in missing)
    assert not any("model credential" in item for item in missing)

    # declared-but-unshipped backends point at their milestone
    from infermatrix_copilot.providers import registry
    monkeypatch.setitem(registry._UNSHIPPED, "codex", "M9")
    settings.strict_backend = "codex"
    missing = _core(settings).strict_readiness("vllm-omni")
    assert any("M9" in item for item in missing)


@pytest.mark.parametrize("readiness", ["strict_readiness", "quality_readiness"])
def test_workflow_readiness_checks_completion_even_with_authenticated_cli(
    settings, monkeypatch, readiness,
):
    from infermatrix_copilot.providers.codex import CodexTransport

    settings.strict_backend = "codex"
    monkeypatch.setattr(CodexTransport, "cli_path", lambda self: "/fake/codex")
    monkeypatch.setattr(CodexTransport, "auth_gap", lambda self: "")

    def forbid_dispatch(*args, **kwargs):
        pytest.fail("readiness must not dispatch a model")

    monkeypatch.setattr(CodexTransport, "complete", forbid_dispatch)
    monkeypatch.setattr(CodexTransport, "run_session", forbid_dispatch)
    gaps = getattr(_core(settings), readiness)("vllm-omni")
    assert any("required tool-free completion" in gap
               and "builtin_tools_off" in gap for gap in gaps)
    assert not any("CLI is not installed" in gap for gap in gaps)


def test_strict_readiness_accepts_packaged_runtime(settings, tmp_path):
    repo = tmp_path / "vllm-omni"
    (repo / ".git").mkdir(parents=True)
    settings.repo_paths = {"vllm-omni": str(repo)}
    settings.anthropic_api_key = "test-key"
    settings.strict_backend = "api"
    settings.playbooks_dir.mkdir(parents=True)
    shutil.copy2(
        Path(__file__).resolve().parents[1] / "playbooks" / "pr-review.yaml",
        settings.playbooks_dir / "pr-review.yaml",
    )
    core = _core(settings)

    assert core.strict_readiness("vllm-omni") == []


def test_strict_readiness_accepts_openai_credential(settings, tmp_path):
    repo = tmp_path / "vllm-omni"
    (repo / ".git").mkdir(parents=True)
    settings.repo_paths = {"vllm-omni": str(repo)}
    settings.openai_api_key = "test-key"
    settings.strict_backend = "api"
    settings.playbooks_dir.mkdir(parents=True)
    shutil.copy2(
        Path(__file__).resolve().parents[1] / "playbooks" / "pr-review.yaml",
        settings.playbooks_dir / "pr-review.yaml",
    )

    assert _core(settings).strict_readiness("vllm-omni") == []


def test_get_result_pagination(settings):
    core = _core(settings)
    rid = "run-20260715-120000-abc123"
    rd = settings.run_root / rid
    rd.mkdir(parents=True)
    rs.init_queued(rd, run_id=rid, owner_server_id=core.server_id, owner_server_pid=core.pid)
    rs.mark(rd, rs.DONE)
    (rd / "RUN_REPORT.md").write_text("A" * 50)
    core.settings.mcp_report_max_bytes = 20
    r0 = core.get_result(rid, 0)
    assert len(r0["report"]) == 20 and r0["next_offset"] == 20
    assert r0["report_path"].endswith("RUN_REPORT.md")
    r2 = core.get_result(rid, 40)
    assert r2["next_offset"] is None  # last page
    core.close()


def test_get_result_running_has_no_report(settings):
    core = _core(settings)
    rid = "run-20260715-120001-def456"
    rd = settings.run_root / rid
    rd.mkdir(parents=True)
    rs.init_queued(rd, run_id=rid, owner_server_id=core.server_id, owner_server_pid=core.pid)
    rs.mark(rd, rs.RUNNING)  # owner is this live server -> not reconciled
    out = core.get_result(rid)
    assert out["state"] == rs.RUNNING and "report" not in out
    core.close()


def test_get_status_returns_status_and_optional_progress(settings):
    core = _core(settings)
    rid = "run-20260715-120002-aaa111"
    rd = settings.run_root / rid
    rd.mkdir(parents=True)
    rs.init_queued(rd, run_id=rid, owner_server_id=core.server_id, owner_server_pid=core.pid)
    out = core.get_status(rid)
    assert out["status"]["state"] == rs.QUEUED and out["progress"] is None
    (rd / "progress.json").write_text(json.dumps({"completed": {"fetch": {}}}))
    assert core.get_status(rid)["progress"] == {"completed": {"fetch": {}}}
    core.close()


def test_mcp_docs_are_repo_scoped(settings, tmp_path):
    k = tmp_path / "knowledge"
    (k / "general").mkdir(parents=True)
    (k / "general" / "guide.md").write_text(
        "GENERAL SEARCH NEEDLE", encoding="utf-8")
    (k / "repos" / "vllm-omni").mkdir(parents=True)
    (k / "repos" / "vllm-omni" / "rules.md").write_text(
        "REPO SEARCH NEEDLE", encoding="utf-8")
    (k / "repos" / "other").mkdir(parents=True)
    (k / "repos" / "other" / "secret.md").write_text(
        "OTHER SECRET", encoding="utf-8")
    adapter = settings.adapters_dir / "vllm_omni"
    adapter.mkdir(parents=True)
    (adapter / "manifest.yaml").write_text(json.dumps({
        "name": "vllm_omni", "repo": {"path": str(tmp_path / "repo")},
        "knowledge": {"repo_subdir": "repos/vllm-omni"},
    }), encoding="utf-8")
    settings.knowledge_dir = k
    core = _core(settings)
    assert core.doc_search("SEARCH NEEDLE")["matches"]
    assert "REPO SEARCH" in core.doc_read("repos/vllm-omni/rules.md")["content"]
    with pytest.raises(ValueError, match="outside the selected"):
        core.doc_read("repos/other/secret.md")
    core.close()


def test_mcp_docs_support_afd_plugin_scope(settings, tmp_path):
    k = tmp_path / "knowledge"
    (k / "general").mkdir(parents=True)
    (k / "general" / "guide.md").write_text("GENERAL", encoding="utf-8")
    (k / "repos" / "afd-plugin").mkdir(parents=True)
    (k / "repos" / "afd-plugin" / "rules.md").write_text(
        "AFD ONLY NEEDLE", encoding="utf-8")
    (k / "repos" / "vllm-omni").mkdir(parents=True)
    (k / "repos" / "vllm-omni" / "rules.md").write_text(
        "OMNI SECRET", encoding="utf-8")
    adapter = settings.adapters_dir / "afd_plugin"
    adapter.mkdir(parents=True)
    (adapter / "manifest.yaml").write_text(json.dumps({
        "name": "afd_plugin",
        "repo": {"path": str(tmp_path / "repo"),
                 "aliases": ["afd-plugin"],
                 "full_name": "vllm-project/afd-plugin"},
        "knowledge": {"repo_subdir": "repos/afd-plugin"},
    }), encoding="utf-8")
    settings.knowledge_dir = k
    settings.mcp_repo_allowlist = ["afd-plugin"]
    core = _core(settings)
    assert core.doc_search("AFD NEEDLE", repo="afd-plugin")["matches"]
    assert "AFD ONLY" in core.doc_read("repos/afd-plugin/rules.md",
                                       repo="afd-plugin")["content"]
    with pytest.raises(ValueError, match="outside the selected"):
        core.doc_read("repos/vllm-omni/rules.md", repo="afd-plugin")
    core.close()


def test_start_review_forwards_the_snapshot_binding(settings, monkeypatch):
    """The pin has to be reachable from the public tool, not just from a spec
    built by hand below the boundary."""
    pytest.importorskip("mcp")
    from infermatrix_copilot import mcp_server

    seen: list[dict] = []
    monkeypatch.setattr(mcp_server.CopilotMCP, "start",
                        lambda self, req: seen.append(req) or "run-1")
    mcp = mcp_server.build_mcp(settings)
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert "expected_head_sha" in tools["start_review"].inputSchema["properties"]

    asyncio.run(mcp.call_tool("start_review",
                              {"pr": 7, "expected_head_sha": HEAD}))
    assert seen and seen[0]["expected_head_sha"] == HEAD


def test_strict_review_request_carries_the_snapshot_binding(settings):
    """Same for the Direct MCP's Strict path, which is what the reviewbot calls."""
    from infermatrix_copilot.thin_mcp_server import _strict_review_request

    req = _strict_review_request("7", "vllm-omni", post=False, review_depth="",
                                 settings=settings, expected_head_sha=HEAD)
    assert req["expected_head_sha"] == HEAD
    # and it stays a first-class field, never smuggled in as a step knob
    assert "expected_head_sha" not in req.get("params", {})

    unpinned = _strict_review_request("7", "vllm-omni", post=False,
                                      review_depth="", settings=settings)
    assert "expected_head_sha" not in unpinned


def test_exposed_tools_are_read_only_only(settings):
    pytest.importorskip("mcp")
    from infermatrix_copilot.mcp_server import build_mcp

    mcp = build_mcp(settings)
    names = sorted(t.name for t in asyncio.run(mcp.list_tools()))
    assert names == ["doc_read", "doc_search", "get_capabilities", "get_result",
                     "get_status", "list_playbooks",
                     "start_issue_answer", "start_issue_triage", "start_review"]
    assert not any(bad in n for n in names
                   for bad in ("post", "push", "debug", "rebase"))


@pytest.mark.parametrize(
    ("strict_compat", "allow_post"),
    [(False, True), (True, False), (True, True)],
)
def test_child_launch_never_opens_the_post_gate(
        settings, monkeypatch, strict_compat, allow_post):
    """No MCP child may write outward, Strict included — not even when this
    server's own ALLOW_POST is on. Strict used to inherit it, because Strict
    specs could carry post=True; the policy now refuses that, so leaving the env
    gate open would only preserve a path to a second publisher on one PR."""
    from infermatrix_copilot import mcp_server

    core = _core(settings)
    core.settings.allow_post = allow_post
    core.settings.strict_backend = "codex"
    run_id, _ = core.copilot.reserve_run(
        TaskSpec(kind="pr_review", repo="vllm-omni", pr=3),
        owner_server_id=core.server_id, owner_server_pid=core.pid)
    captured = {}

    class FakeProcess:
        pid = 424242
        returncode = 0

        def wait(self):
            return 0

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(mcp_server.subprocess, "Popen", fake_popen)
    core._launch(run_id, strict_compat=strict_compat)

    assert captured["env"]["ALLOW_POST"] == "0"
    assert captured["env"]["ALLOW_PUSH"] == "0"
    assert captured["env"]["STRICT_BACKEND"] == "codex"
    expected_arg = (
        "--execute-strict-reserved" if strict_compat else "--execute-reserved")
    assert expected_arg in captured["argv"]
    core.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_subprocess_tamper_defense(settings, monkeypatch, legacy):
    """New reservations refuse tampering before launch; old ones retain the child gate."""
    core = _core(settings)
    run_id, _ = core.copilot.reserve_run(
        TaskSpec(kind="pr_review", repo="vllm-omni", pr=3),
        owner_server_id=core.server_id, owner_server_pid=core.pid)
    rd = settings.run_root / run_id
    if legacy:
        status = rs.read_status(rd)
        status.pop("request_fingerprint")
        status.pop("execution_mode")
        rs.status_path(rd).write_text(json.dumps(status), encoding="utf-8")
    (rd / "request.json").write_text(json.dumps(
        {"kind": "pr_rebase", "repo": "evil", "pr": 3, "post": True,
         "params": {"force_push": True}}))
    if legacy:
        core._launch(run_id)  # real subprocess; blocks until it exits
    else:
        from infermatrix_copilot import mcp_server

        def forbidden_launch(*_args, **_kwargs):
            pytest.fail("tampered reservation must not launch a child")

        monkeypatch.setattr(mcp_server.subprocess, "Popen", forbidden_launch)
        with pytest.raises(PolicyError, match="changed before launch"):
            core._launch(run_id)
    st = rs.read_status(rd)
    assert st["state"] == (rs.FAILED if legacy else rs.QUEUED)
    assert st["owner_server_id"] == core.server_id
    assert (rd / "console.log").exists()
    core.close()


# -- Strict runs pin the knowledge snapshot at reservation ---------------------------

def _reserve_created(core, monkeypatch, run_id="run-20260928-120000-aaaaaa"):
    (core.run_root / run_id).mkdir(parents=True, exist_ok=True)
    rs.init_queued(core.run_root / run_id, run_id=run_id, owner_server_id=core.server_id,
                   owner_server_pid=core.pid)
    monkeypatch.setattr(core._q, "put", lambda item: None)
    def reserve(spec, **_kwargs):
        (core.run_root / run_id / "request.json").write_text(
            json.dumps(spec.model_dump()), encoding="utf-8")
        return run_id, True

    monkeypatch.setattr(core.reservations, "reserve", reserve)
    return core.reserve_strict_review({
        "kind": "pr_review", "repo": "vllm-omni", "pr": 7, "post": False,
        "params": {"review_depth": "standard"}, "expected_head_sha": "a" * 40,
        "idempotency_key": "attempt-pin"})[0]


def _fake_launch(core, monkeypatch, run_id):
    from infermatrix_copilot import mcp_server

    captured = {}

    class FakeProcess:
        pid = 424243
        returncode = 0

        def wait(self):
            return 0

    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(mcp_server.subprocess, "Popen", fake_popen)
    core._launch(run_id)
    return captured


def test_a_strict_run_reviews_with_the_snapshot_active_when_it_was_reserved(settings, monkeypatch, tmp_path):
    from infermatrix_copilot.contract import build_review_result
    from infermatrix_copilot.knowledge_view import KNOWLEDGE_ROOT_ENV, _load_view
    from test_knowledge_view_routing import _snapshot

    old = _snapshot(tmp_path, "c" * 40, retire=False)
    new = _snapshot(tmp_path, "d" * 40, retire=True)
    active = tmp_path / "active"
    active.symlink_to(old)
    monkeypatch.setenv(KNOWLEDGE_ROOT_ENV, str(active))
    _load_view.cache_clear()
    core = _core(settings)
    run_id = _reserve_created(core, monkeypatch)
    swap = tmp_path / "active.tmp"
    swap.symlink_to(new)
    swap.replace(active)                       # a newer snapshot is activated meanwhile

    env = _fake_launch(core, monkeypatch, run_id)["env"]
    assert env["KNOWLEDGE_DIR"] == str((old / "knowledge").resolve())
    assert env["KNOWLEDGE_ROOT"] == str(old.resolve())
    diagnostics = build_review_result(core.run_root / run_id)["diagnostics"]
    manifest = json.loads((old / "MANIFEST.json").read_text())
    assert diagnostics["knowledge_snapshot"] == "c" * 40
    assert diagnostics["knowledge_tree_sha256"] == manifest["tree_sha256"]
    core.close()


def test_a_pruned_pinned_snapshot_fails_the_run_instead_of_switching_knowledge(settings, monkeypatch, tmp_path):
    import shutil

    from infermatrix_copilot.knowledge_view import KNOWLEDGE_ROOT_ENV, _load_view
    from test_knowledge_view_routing import _snapshot

    old = _snapshot(tmp_path, "e" * 40, retire=False)
    monkeypatch.setenv(KNOWLEDGE_ROOT_ENV, str(old))
    _load_view.cache_clear()
    core = _core(settings)
    run_id = _reserve_created(core, monkeypatch)
    shutil.rmtree(old)
    assert _fake_launch(core, monkeypatch, run_id) == {}          # never launched
    status = rs.read_status(core.run_root / run_id)
    assert status["state"] == rs.FAILED and "e" * 40 in status["note"]
    core.close()


def test_packaged_knowledge_is_pinned_even_if_a_root_is_configured_later(settings, monkeypatch, tmp_path):
    from infermatrix_copilot.contract import build_review_result
    from infermatrix_copilot.knowledge_view import KNOWLEDGE_ROOT_ENV, KnowledgeView, _load_view

    monkeypatch.delenv(KNOWLEDGE_ROOT_ENV, raising=False)
    monkeypatch.delenv("KNOWLEDGE_DIR", raising=False)
    _load_view.cache_clear()
    core = _core(settings)
    run_id = _reserve_created(core, monkeypatch)
    packaged = str(KnowledgeView.current().root)
    monkeypatch.setenv(KNOWLEDGE_ROOT_ENV, str(tmp_path / "later-activated"))   # config changes before launch
    env = _fake_launch(core, monkeypatch, run_id)["env"]
    assert env["KNOWLEDGE_DIR"] == packaged and KNOWLEDGE_ROOT_ENV not in env
    assert build_review_result(core.run_root / run_id)["diagnostics"]["knowledge_snapshot"] == "packaged"
    core.close()


def test_a_reclaimed_reservation_keeps_its_original_pin(settings, monkeypatch, tmp_path):
    from infermatrix_copilot.knowledge_view import KNOWLEDGE_ROOT_ENV, _load_view
    from test_knowledge_view_routing import _snapshot

    old = _snapshot(tmp_path, "7" * 40, retire=False)
    new = _snapshot(tmp_path, "8" * 40, retire=True)
    active = tmp_path / "active"
    active.symlink_to(old)
    monkeypatch.setenv(KNOWLEDGE_ROOT_ENV, str(active))
    _load_view.cache_clear()
    core = _core(settings)
    run_id = _reserve_created(core, monkeypatch)
    swap = tmp_path / "active.tmp"
    swap.symlink_to(new)
    swap.replace(active)
    assert _reserve_created(core, monkeypatch, run_id) == run_id     # reclaimed: created=True again
    pin = json.loads((core.run_root / run_id / "knowledge.json").read_text())
    assert pin["snapshot"] == "7" * 40
    core.close()


def test_a_pinning_failure_fails_the_run_instead_of_stranding_it(settings, monkeypatch):
    from infermatrix_copilot.knowledge_view import KnowledgeView, KnowledgeViewError

    def broken():
        raise KnowledgeViewError("KNOWLEDGE_ROOT does not resolve")

    monkeypatch.setattr(KnowledgeView, "current", classmethod(lambda cls: broken()))
    core = _core(settings)
    queued = []
    run_id = "run-20260928-120000-bbbbbb"
    (core.run_root / run_id).mkdir(parents=True)
    rs.init_queued(core.run_root / run_id, run_id=run_id, owner_server_id=core.server_id,
                   owner_server_pid=core.pid)
    monkeypatch.setattr(core._q, "put", queued.append)
    monkeypatch.setattr(core.reservations, "reserve", lambda *_a, **_k: (run_id, True))
    core.reserve_strict_review({
        "kind": "pr_review", "repo": "vllm-omni", "pr": 7, "post": False,
        "params": {"review_depth": "standard"}, "expected_head_sha": "a" * 40,
        "idempotency_key": "attempt-broken"})
    status = rs.read_status(core.run_root / run_id)
    assert queued == [] and status["state"] == rs.FAILED
    assert "could not pin the knowledge snapshot" in status["note"]
    core.close()


def test_a_configured_knowledge_dir_is_pinned_not_replaced_by_the_packaged_tree(settings, monkeypatch, tmp_path):
    from infermatrix_copilot.contract import build_review_result
    from infermatrix_copilot.knowledge_view import KNOWLEDGE_ROOT_ENV, KnowledgeView, _load_view

    monkeypatch.delenv(KNOWLEDGE_ROOT_ENV, raising=False)
    _load_view.cache_clear()
    custom = tmp_path / "custom-knowledge"
    shutil.copytree(KnowledgeView.current().root, custom, ignore=shutil.ignore_patterns("__pycache__"))
    settings.knowledge_dir = custom
    core = _core(settings)
    run_id = _reserve_created(core, monkeypatch)
    env = _fake_launch(core, monkeypatch, run_id)["env"]
    assert env["KNOWLEDGE_DIR"] == str(custom.resolve()) and KNOWLEDGE_ROOT_ENV not in env
    assert build_review_result(core.run_root / run_id)["diagnostics"]["knowledge_snapshot"] == "unverified"
    core.close()
