"""Meta-improvement engine P0, second slice: the shadow boundary (design
§8.2) — read fence, strict extras, executable/credential allowlist, the
executor's step refusal, the independent shadow clone with its pinned base,
the base-bound repo tools, snapshot-mode PR steps and item staging."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from infermatrix_copilot import tools
from infermatrix_copilot.improve import shadow
from infermatrix_copilot.improve.execution import shadow_agent_policy
from infermatrix_copilot.scopes import READ_TOOLS, read_only_scope, shadow_scope
from infermatrix_copilot.tools import ToolDef


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True).stdout.strip()


def _repo_with_advanced_base(tmp_path) -> tuple[Path, str, str, str]:
    """A repo whose PR (branch `feature`) targets the NON-default branch
    `develop`, and where `develop` advanced after the fork. Returns
    `(repo, base_tip, head, merge_base)`."""
    repo = tmp_path / "src"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", **os.environ}
    run = lambda *a: subprocess.run(["git", *a], cwd=str(repo), env=env, check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    (repo / ".gitignore").write_text("*.pyc\n")
    run("add", "."); run("commit", "-qm", "root")
    run("checkout", "-qb", "develop")
    (repo / "a.txt").write_text("one\ntwo\n")
    run("commit", "-qam", "develop 1")
    fork = _git(repo, "rev-parse", "HEAD")
    run("checkout", "-qb", "feature")
    (repo / "b.txt").write_text("pr change\n")
    run("add", "."); run("commit", "-qm", "feature")
    head = _git(repo, "rev-parse", "HEAD")
    run("checkout", "-q", "develop")
    (repo / "c.txt").write_text("landed after the fork\n")
    run("add", "."); run("commit", "-qm", "develop 2")
    base_tip = _git(repo, "rev-parse", "HEAD")
    run("remote", "add", "origin", "https://example.invalid/never.git")
    run("checkout", "-q", "main")
    return repo, base_tip, head, fork


# -- layer 1: read fence + strict extras ---------------------------------------------

def test_read_fence_refuses_every_escape_and_is_off_by_default(tmp_path):
    inside = tmp_path / "shadow"
    (inside / "pkg").mkdir(parents=True)
    (inside / "pkg" / "f.txt").write_text("inside")
    (inside / ".git" / "objects" / "info").mkdir(parents=True)
    (inside / ".git" / "objects" / "info" / "alternates").write_text("/somewhere/else/objects")
    outside = tmp_path / "host.txt"
    outside.write_text("host secret")
    (inside / "link").symlink_to(outside)
    scope = shadow_scope(inside)
    escapes = [
        ("read_file", {"path": str(outside)}),                       # absolute path outside
        ("read_file", {"path": "pkg/../../host.txt"}),               # `..` traversal
        ("read_file", {"path": "link"}),                             # symlink to the host
        ("read_file", {"path": ".git/objects/info/alternates"}),     # the source object store
        ("list_dir", {"path": str(tmp_path)}),
        ("grep", {"pattern": "secret", "path": str(tmp_path)}),
    ]
    for name, args in escapes:
        out = tools.dispatch(name, args, scope=scope)
        assert not out["ok"] and out["error"].startswith("refused:"), (name, args, out)
    assert tools.dispatch("read_file", {"path": "pkg/f.txt"}, scope=scope)["result"] == "inside"
    assert "pkg/" in tools.dispatch("list_dir", {"path": str(inside)}, scope=scope)["result"]
    assert "f.txt" in tools.dispatch("grep", {"pattern": "inside", "path": str(inside)}, scope=scope)["result"]
    # the same calls under an ordinary scope behave exactly as today
    plain = read_only_scope()
    assert tools.dispatch("read_file", {"path": str(outside)}, scope=plain)["result"] == "host secret"
    assert tools.dispatch("read_file", {"path": str(inside / "link")}, scope=plain)["result"] == "host secret"
    assert tools.dispatch("list_dir", {"path": str(tmp_path)}, scope=plain)["ok"]


def test_strict_scope_filters_and_refuses_undeclared_or_internal_write_extras(tmp_path):
    inside = tmp_path / "shadow"
    inside.mkdir()
    (inside / "f.txt").write_text("x")
    calls = []
    extras = {
        "diff_stat": ToolDef("diff_stat", "declared", {"type": "object", "properties": {}}, lambda **_: "stat"),
        "skill_update_candidate": ToolDef("skill_update_candidate", "writes knowledge",
                                          {"type": "object", "properties": {}},
                                          lambda **_: calls.append("wrote") or "ok", internal_write=True),
        "gh_pr_view": ToolDef("gh_pr_view", "undeclared", {"type": "object", "properties": {}},
                              lambda **_: calls.append("gh") or "ok"),
        "peek": ToolDef("peek", "reads a path", {"type": "object", "properties": {}},
                        lambda path, **_: Path(path).read_text(), read_path_arg="path"),
    }
    scope = shadow_scope(inside, tools=("diff_stat", "peek"))
    advertised = {d["name"] for d in tools.tool_definitions_for(scope, extras)}
    assert advertised == READ_TOOLS | {"diff_stat", "peek"}
    assert tools.dispatch("diff_stat", {}, scope=scope, extra=extras)["result"] == "stat"
    for name in ("skill_update_candidate", "gh_pr_view"):
        out = tools.dispatch(name, {}, scope=scope, extra=extras)
        assert not out["ok"] and "refused" in out["error"]
    assert calls == []                                                   # never executed
    assert tools.dispatch("peek", {"path": "f.txt"}, scope=scope, extra=extras)["result"] == "x"
    assert not tools.dispatch("peek", {"path": str(tmp_path / "..")}, scope=scope, extra=extras)["ok"]
    # an ordinary scope keeps the historical extra bypass
    assert tools.dispatch("gh_pr_view", {}, scope=read_only_scope(), extra=extras)["ok"]


# -- layer 3: credentials and executables ------------------------------------------

def test_shadow_env_is_an_allowlist_with_a_one_off_path(tmp_path, monkeypatch):
    exe = shadow.make_executables_dir(tmp_path / "bin")
    assert sorted(p.name for p in exe.iterdir()) == ["git", "grep", "python3"]
    assert all(p.is_symlink() for p in exe.iterdir())
    with pytest.raises(RuntimeError, match="not found"):
        shadow.make_executables_dir(tmp_path / "bin2", ("definitely-not-a-binary-xyz",))
    host = {"GH_TOKEN": "ghp_x", "GITHUB_TOKEN": "y", "GH_CONFIG_DIR": "/x", "SLACK_TOKEN": "z",
            "KB_SIGNING_PAT": "p", "ANTHROPIC_API_KEY": "sk-ant-ok", "ECO_MODEL": "m", "ECO_API_KEY": "k",
            "HOME": "/home/t", "PATH": "/usr/bin:/bin", "REPO_PATHS": "demo=/prod/demo",
            "ALLOW_POST": "1", "RANDOM_VAR": "1"}
    env = shadow.shadow_env(shadow_dir=tmp_path / "s", run_dir=tmp_path / "run", trace_root=tmp_path / "t",
                            executables_dir=exe, repo_name="demo", environ=host, ledger_dir=tmp_path / "ledger")
    assert not any(k.startswith(("GH_", "GITHUB_")) for k in env)
    assert "SLACK_TOKEN" not in env and "KB_SIGNING_PAT" not in env and "RANDOM_VAR" not in env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-ok" and env["ECO_API_KEY"] == "k" and env["HOME"] == "/home/t"
    assert env["PATH"] == str(exe) and json.loads(env["REPO_PATHS"]) == {"demo": str(tmp_path / "s")}   # what Settings parses
    assert env["ALLOW_POST"] == "0" and env["ALLOW_PUSH"] == "0" and env["IMPROVE_SHADOW"] == "1"
    assert env["PR_CONTEXT_SOURCE"] == "snapshot"
    assert env["IMPROVE_GOVERNED"] == "1" and env["IMPROVE_LEDGER_DIR"] == str(tmp_path / "ledger")
    assert shadow.assert_boundaries(env) == []
    unmetered = shadow.shadow_env(shadow_dir=tmp_path / "s", run_dir=tmp_path / "run", trace_root=tmp_path / "t",
                                  executables_dir=exe, repo_name="demo", environ=host)
    assert any("unmetered" in p for p in shadow.assert_boundaries(unmetered))
    bad = {**env, "GH_TOKEN": "x", "ALLOW_POST": "1", "PATH": f"{exe}:/usr/bin"}
    problems = shadow.assert_boundaries(bad)
    assert any("GH_TOKEN" in p for p in problems) and any("outward" in p for p in problems)
    assert any("PATH" in p for p in problems)
    # under the shadow environment the whitelisted binaries resolve and gh does not
    assert subprocess.run(["git", "--version"], env=env, capture_output=True).returncode == 0
    with pytest.raises(FileNotFoundError):
        subprocess.run(["gh", "--version"], env=env, capture_output=True)


# -- layer 2: the executor refuses non-read steps ------------------------------------

def test_executor_refuses_non_read_steps_under_shadow(tmp_path, settings):
    from execution_helpers import application_executor as Executor
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepResult, StepSpec
    from infermatrix_copilot.playbooks.store import Playbook, PlaybookStep
    from infermatrix_copilot.run_trace import RunTrace

    ran: list[str] = []

    async def reader(ctx):
        ran.append("read")
        return StepResult(True, summary="ok")

    async def poster(ctx):
        ran.append("push")
        return StepResult(True, summary="posted")

    registry = StepRegistry()
    registry.register(StepSpec("x.read", "deterministic", "read", reader, ""))
    registry.register(StepSpec("pr.post_review", "script", "push", poster, ""))
    run_dir = tmp_path / "run"
    trace = RunTrace(run_dir / "run_trace.jsonl")
    ex = Executor(registry, settings.model_copy(update={"improve_shadow": True}), run_dir=run_dir, trace=trace)
    playbook = Playbook(name="pr-review", version=1, status="active", task_kinds=["pr_review"], repos=[],
                        steps=[PlaybookStep(id="r", step="x.read"), PlaybookStep(id="p", step="pr.post_review")])
    outcome = asyncio.run(ex.run(playbook, {"task_spec": {"repo": "demo"}}))
    assert outcome.status == "blocked" and "shadow run refused step 'p'" in outcome.blocked_reason
    assert ran == ["read"]
    assert trace.events("step_refused")
    # not in shadow: the same playbook reaches the posting step
    ex2 = Executor(registry, settings, run_dir=tmp_path / "run2", trace=RunTrace(tmp_path / "run2" / "t.jsonl"))
    assert asyncio.run(ex2.run(playbook, {"task_spec": {"repo": "demo"}})).status == "done"
    assert ran == ["read", "read", "push"]


# -- layer 4: the shadow clone and the base-bound tools --------------------------------

def test_shadow_clone_has_no_remote_pins_the_base_and_leaves_the_source_untouched(tmp_path):
    from infermatrix_copilot.engine.steps.review.repo_tools import review_repo_tools

    repo, base_tip, head, fork = _repo_with_advanced_base(tmp_path)
    remotes_before = _git(repo, "remote", "-v")
    worktrees_before = _git(repo, "worktree", "list")
    dest = tmp_path / "shadow" / "demo-1-abc"
    ok, note = shadow.make_shadow_clone(repo, dest, head_sha=head, base_tip=base_tip, base_ref_name="develop")
    assert ok, note
    assert _git(dest, "remote") == ""                                   # no remote at all
    assert _git(dest, "rev-parse", "HEAD") == head
    assert _git(dest, "rev-parse", "refs/improve/base") == base_tip
    assert _git(dest, "rev-parse", "refs/remotes/origin/develop") == base_tip
    assert _git(dest, "rev-parse", "refs/heads/develop") == base_tip
    assert (dest / ".git" / "objects" / "info" / "alternates").exists()  # objects are borrowed, not copied
    assert _git(repo, "remote", "-v") == remotes_before                 # source untouched
    assert _git(repo, "worktree", "list") == worktrees_before
    assert not shadow.make_shadow_clone(repo, dest, head_sha=head, base_tip=base_tip)[0]  # never overwrite

    # the tools bind to the pinned base tip: merge-base(base_tip, HEAD) == the fork
    # point, so the advanced base's `c.txt` is NOT in the PR's diff
    extra = review_repo_tools(dest, base_tip)
    stat = extra["diff_stat"].handler()
    assert f"merge-base {fork[:12]}" in stat and "b.txt" in stat and "c.txt" not in stat
    expected = _git(dest, "diff", "--stat", _git(dest, "merge-base", base_tip, head), head)
    assert stat.split("\n", 1)[1].strip() == expected.strip()
    assert extra["file_at_base"].handler(path="a.txt") == "one\ntwo"         # the merge-base version (git strips)
    assert "absent at base" in extra["file_at_base"].handler(path="b.txt")
    # without the explicit base, the legacy guess (origin/main) would pick the
    # wrong base for this non-default-branch PR — the binding is what fixes it
    legacy = review_repo_tools(dest)["diff_stat"].handler()
    assert "a.txt" in legacy                                              # main's diff includes develop's work


def test_harden_scope_resolves_ensemble_lens_names_to_the_declared_step(tmp_path, settings):
    inside = tmp_path / "clone"
    inside.mkdir()
    ctx = SimpleNamespace(settings=settings, state={"playbook": "pr-review", "repo_path": str(inside)},
                          run_dir=tmp_path / "run")
    for name in ("agent.review_diff", "agent.review_diff#contracts", "agent.review_diff#behavior"):
        hardened = shadow.harden_scope(read_only_scope(), ctx, name)
        assert {"diff_stat", "file_at_base", "doc_read", "calc"} <= hardened.allowed_tools, name
        assert hardened.strict_extras and hardened.read_only and hardened.read_roots[0] == str(inside.resolve())
    undeclared = shadow.harden_scope(read_only_scope(), ctx, "agent.assess_pr_quality#x")
    assert undeclared.allowed_tools == READ_TOOLS


def test_ci_checks_keep_pending_output_and_staging_refuses_unavailable_inputs(tmp_path, settings, monkeypatch):
    from infermatrix_copilot.engine.steps.pr import fetch
    from infermatrix_copilot.improve import staging

    pending = json.dumps([{"name": "unit", "state": "PENDING", "bucket": "pending", "link": ""}])
    monkeypatch.setattr(fetch, "_gh", lambda args, cwd=None: (8, pending))        # gh: non-zero, JSON printed
    assert fetch.ci_checks_for(Path("."), 1) == json.loads(pending)
    monkeypatch.setattr(fetch, "_gh", lambda args, cwd=None: (1, "gh: not logged in"))
    assert fetch.ci_checks_for(Path("."), 1) is None
    monkeypatch.setattr(fetch, "_gh", lambda args, cwd=None: (0, "[]"))
    assert fetch.ci_checks_for(Path("."), 1) == []

    repo, base_tip, head, _ = _repo_with_advanced_base(tmp_path)
    monkeypatch.setattr(fetch, "_resolve_pr_head", lambda r, pr: ("develop", head, ""))
    monkeypatch.setattr(fetch, "_fetch_pinned", lambda r, pr, base_ref, head_sha, run_id: (base_tip, ""))
    monkeypatch.setattr(fetch, "_pr_context_bundle", lambda c, r, pr: "ctx")
    st = settings.model_copy(update={"repo_paths": {"demo": repo}})
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = _ctx(st, {"task_spec": {"repo": "demo"}}, run_dir)
    monkeypatch.setattr(fetch, "gate_report_for", lambda r, pr: ("gate check unavailable (gh failed)", "", False))
    monkeypatch.setattr(fetch, "ci_checks_for", lambda r, pr: [])
    with pytest.raises(staging.StagingError, match="gate report unavailable"):
        staging.stage_item(ctx, "demo#1", shadow_root=tmp_path / "s1")
    monkeypatch.setattr(fetch, "gate_report_for", lambda r, pr: ("gates clean", "OPEN", True))
    monkeypatch.setattr(fetch, "ci_checks_for", lambda r, pr: None)
    with pytest.raises(staging.StagingError, match="CI checks unavailable"):
        staging.stage_item(ctx, "demo#1", shadow_root=tmp_path / "s2")
    assert not (tmp_path / "s1").exists() and not (tmp_path / "s2").exists()   # nothing half-staged


def test_shadow_repo_map_never_indexes_through_a_symlink_and_skill_usage_is_not_written(tmp_path, settings, monkeypatch):
    from infermatrix_copilot.engine.agent_runtime import knowledge as k
    from infermatrix_copilot.engine.agent_runtime import runner as r
    from infermatrix_copilot.profiles.repo_map import RepoMap

    clone = tmp_path / "clone"
    (clone / "pkg").mkdir(parents=True)
    (clone / "pkg" / "ok.py").write_text("def inside_symbol():\n    pass\n")
    private = tmp_path / "private.py"
    private.write_text("def leaked_host_symbol():\n    pass\n")
    (clone / "pkg" / "leak.py").symlink_to(private)
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    # unfenced (production) behaviour indexes through the symlink; the shadow fence does not
    assert "pkg/leak.py" in RepoMap(clone, "python").index()
    ctx = SimpleNamespace(settings=settings.model_copy(update={"improve_shadow": True}),
                          state={"repo_path": str(clone), "task_spec": {"repo": "demo"}},
                          run_dir=tmp_path / "run", trace=SimpleNamespace(record=lambda *a, **k: None))
    ctx.agent_policy = shadow_agent_policy()
    tool = k._repo_map_tool(ctx, None)
    rendered = tool["repo_map"].handler(query="symbol")
    assert "inside_symbol" in rendered and "leaked_host_symbol" not in rendered and "leak.py" not in rendered
    cache = list((tmp_path / "run" / "repo_map").glob("index-*-fenced.json"))
    assert cache and "leak.py" not in cache[0].read_text()



def test_shadow_scope_smoke_and_repo_map_cache_stays_out_of_knowledge(tmp_path, settings):
    from infermatrix_copilot.engine.agent_runtime.knowledge import _repo_map_tool
    from infermatrix_copilot.engine.steps.review.repo_tools import review_repo_tools

    repo, base_tip, head, _ = _repo_with_advanced_base(tmp_path)
    dest = tmp_path / "shadow" / "clone"
    assert shadow.make_shadow_clone(repo, dest, head_sha=head, base_tip=base_tip)[0]
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "general.md").write_text("k")
    tree_before = hashlib.sha256(b"".join(sorted(p.read_bytes() for p in knowledge.rglob("*") if p.is_file()))).hexdigest()
    extra = review_repo_tools(dest, base_tip)
    scope = shadow_scope(dest, tools=tuple(extra), extra_read_roots=(str(knowledge),))
    report = shadow.smoke(scope, dest, extra)
    assert report["ok"], report
    assert tools.dispatch("read_file", {"path": str(knowledge / "general.md")}, scope=scope)["result"] == "k"

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    adapter = SimpleNamespace(manifest={"repo": {"language": "python"}}, root=tmp_path / "adapter", repo_path=str(dest))
    ctx = SimpleNamespace(settings=settings.model_copy(update={"improve_shadow": True, "knowledge_dir": knowledge}),
                          state={"repo_path": str(dest), "task_spec": {"repo": "demo"}}, run_dir=run_dir,
                          trace=SimpleNamespace(record=lambda *a, **k: None))
    ctx.agent_policy = shadow_agent_policy()
    tool = _repo_map_tool(ctx, adapter)
    if tool:
        tool["repo_map"].handler(query="a")
        assert (run_dir / "repo_map").exists()
    tree_after = hashlib.sha256(b"".join(sorted(p.read_bytes() for p in knowledge.rglob("*") if p.is_file()))).hexdigest()
    assert tree_after == tree_before and not list(knowledge.rglob("index-*.json"))


# -- snapshot mode + staging -------------------------------------------------------------

def _ctx(settings, state, run_dir):
    from infermatrix_copilot.run_trace import RunTrace

    return SimpleNamespace(settings=settings, state=state, params={}, run_dir=run_dir,
                           trace=RunTrace(run_dir / "run_trace.jsonl"), llm=None, item=None)


def test_snapshot_mode_steps_never_touch_gh(tmp_path, settings, monkeypatch):
    from infermatrix_copilot.engine.steps.pr import debug, fetch

    def boom(*a, **k):
        raise AssertionError("gh must not be called in snapshot mode")

    monkeypatch.setattr(fetch, "_gh", boom)
    monkeypatch.setattr(debug, "_gh", boom)
    shadow_dir = tmp_path / "clone"
    shadow_dir.mkdir()
    snap = {"repo": "demo", "pr": 7, "head_sha": "a" * 40, "base_sha": "b" * 40, "base_ref": "develop", "diff": "diff --git x",
            "context_text": "## PR description", "gate_report": "PR is a DRAFT — provisional.", "pr_state": "OPEN",
            "ci_checks": [{"name": "unit", "state": "FAILURE", "bucket": "fail", "link": ""},
                          {"name": "lint", "state": "SUCCESS", "bucket": "pass", "link": ""}],
            "shadow_dir": str(shadow_dir), "snapshot_sha256": "c" * 64}
    snap_settings = settings.model_copy(update={"pr_context_source": "snapshot"})
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = {"task_spec": {"repo": "demo", "pr": 7}, "pr_snapshot": snap}
    ctx = _ctx(snap_settings, state, run_dir)
    r = asyncio.run(fetch._pr_fetch_diff(ctx))
    assert r.ok, r.summary
    upd = r.outputs["state_updates"]
    assert upd["diff_text"] == "diff --git x" and upd["repo_path"] == str(shadow_dir)
    assert upd["pr_head_sha"] == "a" * 40 and upd["pr_base_sha"] == "b" * 40 and upd["pr_base_ref"] == "develop"
    assert "SHADOW CLONE" in upd["checkout_note"]
    g = asyncio.run(fetch._pr_gate_check(ctx))
    assert g.ok and g.outputs["state_updates"] == {"gate_report": snap["gate_report"], "pr_state": "OPEN"}
    c = asyncio.run(debug._pr_fetch_ci_failures(ctx))
    assert c.ok and c.outputs["failing"] == ["unit"]
    # a pinned head that differs is stale, and a missing snapshot blocks (never a live fallback)
    stale = _ctx(snap_settings, {"task_spec": {"repo": "demo", "pr": 7, "expected_head_sha": "d" * 40},
                                 "pr_snapshot": snap}, run_dir)
    assert not asyncio.run(fetch._pr_fetch_diff(stale)).ok
    missing = _ctx(snap_settings, {"task_spec": {"repo": "demo", "pr": 7}}, run_dir)
    r = asyncio.run(fetch._pr_fetch_diff(missing))
    assert not r.ok and "no staged snapshot" in r.summary
    wrong = _ctx(snap_settings, {"task_spec": {"repo": "demo", "pr": 8}, "pr_snapshot": snap}, run_dir)
    assert "for PR #7" in asyncio.run(fetch._pr_gate_check(wrong)).summary
    other_repo = _ctx(snap_settings, {"task_spec": {"repo": "other", "pr": 7}, "pr_snapshot": snap}, run_dir)
    r = asyncio.run(fetch._pr_fetch_diff(other_repo))
    assert not r.ok and "repository 'demo'" in r.summary            # PR numbers are repository-local
    assert not asyncio.run(debug._pr_fetch_ci_failures(other_repo)).ok


def test_bridge_spec_carries_the_shadow_fences_across_the_process_boundary(tmp_path):
    from infermatrix_copilot.tool_bridge import load_bridge_spec, make_dispatcher, write_bridge_spec
    from infermatrix_copilot.run_trace import RunTrace

    inside = tmp_path / "clone"
    (inside / ".git" / "objects" / "info").mkdir(parents=True)
    (inside / ".git" / "objects" / "info" / "alternates").write_text("/host/objects")
    (inside / "f.txt").write_text("ok")
    scope = shadow_scope(inside, tools=("diff_stat",), executables=("python3", "git", "grep"))
    path = write_bridge_spec(run_dir=tmp_path / "run", step_name="agent.review_diff#contracts", scope=scope, repo="demo")
    restored, raw = load_bridge_spec(path)
    assert restored == scope                                      # a faithful round trip, every field
    assert raw["scope"]["strict_extras"] and raw["scope"]["read_roots"][0] == str(inside.resolve())
    trace = RunTrace(tmp_path / "run" / "t.jsonl")
    extras = {"diff_stat": ToolDef("diff_stat", "d", {"type": "object", "properties": {}}, lambda **_: "stat"),
              "gh_pr_view": ToolDef("gh_pr_view", "u", {"type": "object", "properties": {}}, lambda **_: "gh")}
    call = make_dispatcher(restored, (str(inside),), trace, extra=extras)
    assert call("read_file", {"path": "f.txt"}) == "ok"
    with pytest.raises(RuntimeError, match="refused"):
        call("read_file", {"path": ".git/objects/info/alternates"})
    assert call("diff_stat", {}) == "stat"
    with pytest.raises(RuntimeError, match="refused"):
        call("gh_pr_view", {})
    # an ordinary spec (no fences) still restores to an ordinary scope
    plain = write_bridge_spec(run_dir=tmp_path / "run2", step_name="s", scope=read_only_scope(), repo="demo")
    assert load_bridge_spec(plain)[0] == read_only_scope()


def test_stage_items_builds_snapshots_and_clones_from_pinned_fetches(tmp_path, settings, monkeypatch):
    from infermatrix_copilot.engine.steps import improve as improve_steps
    from infermatrix_copilot.engine.steps.pr import fetch
    from infermatrix_copilot.improve import staging
    from infermatrix_copilot.trace_store import TraceStore, bind_store

    repo, base_tip, head, fork = _repo_with_advanced_base(tmp_path)
    monkeypatch.setattr(fetch, "_resolve_pr_head", lambda r, pr: ("develop", head, ""))
    monkeypatch.setattr(fetch, "_fetch_pinned", lambda r, pr, base_ref, head_sha, run_id: (base_tip, ""))
    monkeypatch.setattr(fetch, "_pr_context_bundle", lambda c, r, pr: f"## PR description (mode={c.settings.pr_context_mode})")
    monkeypatch.setattr(fetch, "gate_report_for", lambda r, pr: ("gates clean", "OPEN", True))
    monkeypatch.setattr(fetch, "ci_checks_for", lambda r, pr: [{"name": "unit", "state": "SUCCESS", "bucket": "pass"}])
    st = settings.model_copy(update={"repo_paths": {"demo": repo}, "pr_context_mode": "full"})
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store = TraceStore(tmp_path / "traces", environ={})
    ctx = _ctx(st, {"task_spec": {"repo": "demo"}}, run_dir)
    ctx.params = {"items": f"demo#1@{head[:12]}"}
    with bind_store(store):
        result = asyncio.run(improve_steps._stage_items(ctx))
    assert result.ok, result.summary
    snap = result.outputs["state_updates"]["pr_snapshot"]
    assert snap["head_sha"] == head and snap["base_sha"] == base_tip and snap["base_ref"] == "develop"
    assert "b.txt" in snap["diff"] and "c.txt" not in snap["diff"]      # merge-base(base_tip, head)..head
    assert snap["context_text"].endswith("(mode=no_discussion)")            # discussion never staged
    assert snap["ci_checks"][0]["name"] == "unit" and snap["gate_report"] == "gates clean"
    assert Path(snap["shadow_dir"]).is_dir() and _git(snap["shadow_dir"], "remote") == ""
    assert _git(snap["shadow_dir"], "rev-parse", "refs/improve/base") == base_tip
    assert snap["snapshot_sha256"] == staging.snapshot_digest(snap)
    assert (run_dir / "snapshots").glob("*.json")
    rec = store.query(kind="decision")[0]
    assert rec["result"]["type"] == "item_staged" and rec["result"]["snapshot_sha256"] == snap["snapshot_sha256"]
    assert json.loads(store.blob(rec["inputs"]["snapshot"]))["head_sha"] == head
    # the pre-registered head must match, and every item must stage
    (tmp_path / "run2").mkdir(exist_ok=True)
    ctx2 = _ctx(st, {"task_spec": {"repo": "demo"}}, tmp_path / "run2")
    ctx2.params = {"items": ["demo#1@" + "f" * 12]}
    bad = asyncio.run(improve_steps._stage_items(ctx2))
    assert not bad.ok and "not the pre-registered" in bad.summary
    with pytest.raises(staging.StagingError):
        staging.parse_item("garbage")
