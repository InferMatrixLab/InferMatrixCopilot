"""PR4c — assembly: mode governance, push-gate taxonomy, manifest builder,
test loop, CI build ledger + monitor, v1 obligations, and the v3 playbook
partial e2e (report_only end-to-end; full-mode terminal row)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from infermatrix_copilot.rebase_engine import ci_loop, test_loop as tl
from infermatrix_copilot.rebase_engine.modes import (
    ModeConflictError, mode_state_flags, resolve_effective_mode)
from infermatrix_copilot.rebase_engine.push_gate import evaluate_push_gate
from infermatrix_copilot.rebase_engine.substate import Substate
from infermatrix_copilot.rebase_engine.test_manifest import (
    ManifestSpec, build_manifest)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _spec(params=None, report_only=False):
    return SimpleNamespace(params=dict(params or {}),
                           report_only=report_only)


# -- mode governance -----------------------------------------------------------

def test_mode_truth_table():
    """Every cell of the Rev 8 §2.1 table, including the write-back."""
    for raw, expect in [("", "report_only"), ("report_only", "report_only"),
                        ("full", "full"), ("local_ci", "local_ci"),
                        ("remote_ci", "remote_ci")]:
        s = _spec({"rebase_mode": raw} if raw else {})
        assert resolve_effective_mode(s) == expect
        assert s.params["rebase_mode"] == expect          # write-back
        assert s.report_only == (expect == "report_only")  # one truth
    # report_only=True narrows: unset/report_only fine, mutating BLOCKED
    for raw in ("", "report_only"):
        s = _spec({"rebase_mode": raw} if raw else {}, report_only=True)
        assert resolve_effective_mode(s) == "report_only"
    for raw in ("full", "local_ci", "remote_ci"):
        with pytest.raises(ModeConflictError, match="narrowing"):
            resolve_effective_mode(_spec({"rebase_mode": raw},
                                         report_only=True))
    with pytest.raises(ModeConflictError, match="unknown rebase_mode"):
        resolve_effective_mode(_spec({"rebase_mode": "yolo"}))
    # CLI params may coerce to int/bool — documented BLOCKED, never a crash
    with pytest.raises(ModeConflictError, match="unknown rebase_mode"):
        resolve_effective_mode(_spec({"rebase_mode": 1}))
    with pytest.raises(ModeConflictError, match="unknown rebase_mode"):
        resolve_effective_mode(_spec({"rebase_mode": True}))
    assert mode_state_flags("full") == {
        "mode_report_only": False, "mode_full": True,
        "mode_local_ci": False, "mode_remote_ci": False,
        "mode_runs_local_tests": True, "mode_runs_push_gate": True,
        "mode_runs_remote_ci": True}
    # the composites encode the §2.2 or-of-modes (single-key `when:`)
    assert mode_state_flags("local_ci")["mode_runs_local_tests"] is True
    assert mode_state_flags("local_ci")["mode_runs_push_gate"] is False
    assert mode_state_flags("local_ci")["mode_runs_remote_ci"] is False
    assert mode_state_flags("remote_ci")["mode_runs_local_tests"] is False
    assert mode_state_flags("remote_ci")["mode_runs_push_gate"] is True
    assert mode_state_flags("report_only")["mode_runs_push_gate"] is False


def test_locked_playbook_is_not_mode_governed(tmp_path):
    """The LOCKED delegating playbook does not declare mode_aware, so
    resolve_effective_mode never touches its specs — byte-identical
    behavior (recorded refinement of Rev 8 §2.1)."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.playbooks.store import PlaybookStore
    store = PlaybookStore(REPO_ROOT / "playbooks",
                          register_builtin_steps(StepRegistry()))
    assert store.get("repo-rebase-v3").mode_aware is True
    assert store.get("repo-rebase-v3").status == "locked"  # planner-visible


# -- push gate taxonomy --------------------------------------------------------

def test_push_gate_taxonomy():
    base = {"modules": {"a": {"status": "done"}},
            "tests": {"pipeline": {"failed_tests": []},
                      "precommit": {"result": "passed"}}}
    assert evaluate_push_gate(base, {}).allowed

    structural = {"modules": {"a": {"status": "failed"}},
                  "tests": {"pipeline": {"failed_tests": ["t1"]},
                            "precommit": {"result": "passed"}}}
    d = evaluate_push_gate(structural, {})
    assert not d.allowed and any("module a failed" in r for r in d.reasons)
    # explicit push_with_failures: structural passes, loudly
    d = evaluate_push_gate(structural, {"push_with_failures": True})
    assert d.allowed and any("explicit" in r for r in d.reasons)

    # assertion failures pass through FLAGGED
    asserts = {"modules": {}, "tests": {"pipeline": {"failed_tests": ["t1"]},
                                        "precommit": {"result": "passed"}}}
    d = evaluate_push_gate(asserts, {})
    assert d.allowed and d.flagged == ("test failure: t1",)
    # strict makes them blocking
    d = evaluate_push_gate(asserts, {"strict_push_gate": True})
    assert not d.allowed and "strict gate" in d.reasons[0]
    # pre-existing precommit red (baseline red too): flagged, not blocking
    preex = {"modules": {}, "tests": {
        "pipeline": {"failed_tests": []},
        "precommit": {"result": "failed_preexisting", "baseline_rc": 1}}}
    d = evaluate_push_gate(preex, {})
    assert d.allowed and any("pre-exists" in f for f in d.flagged)
    assert not evaluate_push_gate(preex, {"strict_push_gate": True}).allowed
    # infra failures are STRUCTURAL, never ordinary test failures
    infra = {"tests": {"infra_failures": ["harness crash"],
                       "pipeline": {}, "precommit": {}}}
    assert not evaluate_push_gate(infra, {}).allowed
    with pytest.raises(ModeConflictError, match="conflict"):
        evaluate_push_gate(base, {"strict_push_gate": True,
                                  "push_with_failures": True})


# -- test manifest -------------------------------------------------------------

@pytest.fixture()
def ci_repo(tmp_path):
    repo = tmp_path / "repo"
    # the LIVE tree nests the pipelines under .buildkite/cuda/ (the parent's
    # hardcoded `.buildkite` is stale; our yaml_dir is adapter data)
    (repo / ".buildkite" / "cuda").mkdir(parents=True)
    (repo / "tests" / "worker").mkdir(parents=True)
    (repo / "tests" / "worker" / "test_a_expansion.py").write_text("x")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".buildkite" / "cuda" / "test-merge.yml").write_text(yaml.safe_dump({
        # top-level pipeline env (the live pipelines declare shared runtime
        # settings here) — must reach every job, with job exports winning
        "env": {"SHARED_FLAG": "cuda", "FOO": "0"},
        "steps": [
            {"label": "Worker Tests", "timeout_in_minutes": 10,
             "agents": {"queue": "gpu_4_queue"},
             "commands": ["export FOO=1",
                          "pytest tests/worker/test_a.py"]},
            {"label": "Perf sweep", "commands": ["x"]},          # skipped
            {"group": "g", "steps": [
                {"label": "K8s Job", "agents": {"queue": "mithril-h100-pool"},
                 "plugins": [{"kubernetes": {"podSpec": {"containers": [
                     {"resources": {"limits": {"nvidia.com/gpu": "8"}}}]}}}],
                 "commands": ["pytest tests/worker/"]}]},
        ]}))
    (repo / ".buildkite" / "cuda" / "test-ready.yml").write_text(
        yaml.safe_dump({
            "steps": [{"label": "Worker Tests", "timeout_in_minutes": 20,
                       "agents": {"queue": "gpu_1_queue"},
                       "commands": ["pytest tests/worker/test_a.py"]}]}))
    return repo


def test_manifest_build(ci_repo):
    manifest = yaml.safe_load(
        (REPO_ROOT / "adapters/vllm_omni/manifest.yaml").read_text())
    spec = ManifestSpec.from_manifest(manifest)
    built = build_manifest(ci_repo, spec)
    by_slug = {j.slug: j for j in built.jobs}
    assert "perf_sweep" not in by_slug                     # skip pattern
    wt = by_slug["worker_tests"]
    assert wt.source == "ready" and wt.timeout_sec == 1200  # ready wins slug
    assert wt.env == "" or "FOO" not in wt.command          # env split out
    # top-level pipeline env reaches the merge-pipeline job; the job's own
    # export wins the last-key-wins parse
    from infermatrix_copilot.engine.steps.rebase_v3 import _parse_env_pairs
    k8s_env = _parse_env_pairs(by_slug["k8s_job"].env)
    assert k8s_env["SHARED_FLAG"] == "cuda"
    assert k8s_env["FOO"] == "0"                    # no job export here
    # pipeline env comes FIRST, so a job's own export wins the parse
    assert _parse_env_pairs("FOO=0 FOO=1")["FOO"] == "1"
    # rename-family path correction: test_a.py -> test_a_expansion.py
    assert "tests/worker/test_a_expansion.py" in wt.command
    k8s = by_slug["k8s_job"]
    assert k8s.min_gpus == 8                                # pod spec wins
    assert wt.module == "worker_runner"                     # manifest map
    assert "worker_runner" in built.module_plans


# -- test loop -----------------------------------------------------------------

def test_test_loop_decision_matrix(tmp_path):
    sub = Substate(tmp_path, "run-1")
    old_failure = "E AssertionError: existing failure\nFAILED tests/test_old.py::test_old\n"
    jobs = [{"slug": s, "label": s, "min_gpus": 1}
            for s in ("pass", "hwskip", "preexisting", "regression_fixed",
                      "regression_stuck", "infra")]
    runs = {"pass": tl.TestRunResult(0),
            "hwskip": tl.TestRunResult(0, skipped=True, skip_reason="gpu"),
            "preexisting": tl.TestRunResult(1, output=old_failure),
            "regression_fixed": tl.TestRunResult(1),
            "regression_stuck": tl.TestRunResult(1),
            "infra": tl.TestRunResult(1)}
    baselines = {"preexisting": tl.TestRunResult(1, output=old_failure),
                 "regression_fixed": tl.TestRunResult(0),
                 "regression_stuck": tl.TestRunResult(0),
                 "infra": None}                            # worktree broken
    debugs = {"regression_fixed": True, "regression_stuck": False,
              "infra": False}
    debugged = []

    async def debug_fn(slug, label, rc, output):
        debugged.append(slug)
        return debugs[slug]

    result = asyncio.run(tl.run_test_loop(
        jobs, substate=sub, run_fn=lambda s: runs[s],
        baseline_fn=lambda s: baselines[s], debug_fn=debug_fn))
    assert result["passed"] == 2                 # pass + regression_fixed
    assert result["failed_tests"] == ["regression_stuck", "infra"]
    assert sorted(result["skipped_tests"]) == ["hwskip", "preexisting"]
    # baseline-None (infra) went to DEBUG, not to skipped — a git error
    # must never mask a regression
    assert "infra" in debugged and "preexisting" not in debugged
    # resume: a second loop skips everything already settled
    reran = []
    result2 = asyncio.run(tl.run_test_loop(
        jobs, substate=sub,
        run_fn=lambda s: reran.append(s) or runs[s],
        baseline_fn=lambda s: baselines[s], debug_fn=debug_fn))
    assert result2["passed"] >= 2 and "pass" not in reran


@pytest.mark.parametrize("baseline_log, preexisting", [
    ("ImportError: cannot import name 'RoutedExpertsLists'", False),
    ("", False),
    ("E TypeError: new API\nFAILED tests/test_metrics.py::test_register\n", False),
    ("E AssertionError: missing series\nFAILED tests/test_other.py::test_other\n", False),
    ("E AssertionError: missing series\nFAILED tests/test_metrics.py::test_register\n", True),
    (("E AssertionError: missing series\nFAILED tests/test_metrics.py::test_register\n"
      "E TypeError: old extra failure\nFAILED tests/test_other.py::test_other\n"), True),
])
def test_local_baseline_requires_matching_failures_and_causes(
        tmp_path, baseline_log, preexisting):
    current = tmp_path / "current.log"
    current.write_text("E AssertionError: missing series\n"
                       "FAILED tests/test_metrics.py::test_register\n")
    baseline = tmp_path / "baseline.log"
    baseline.write_text(baseline_log)
    debugged = []

    async def debug_fn(slug, label, rc, output):
        debugged.append(slug)
        return False

    result = asyncio.run(tl.run_test_loop(
        [{"slug": "metrics"}], substate=Substate(tmp_path, "baseline-evidence"),
        run_fn=lambda slug: tl.TestRunResult(1, output=str(current)),
        baseline_fn=lambda slug: tl.TestRunResult(1, output=str(baseline)),
        debug_fn=debug_fn))
    assert result["skipped_tests"] == (["metrics"] if preexisting else [])
    assert result["failed_tests"] == ([] if preexisting else ["metrics"])
    assert debugged == ([] if preexisting else ["metrics"])


def test_test_loop_infra_is_structural(tmp_path):
    """Rev 8 §2.3: timeouts/watchdog kills are INFRASTRUCTURE failures —
    kept out of `failed_tests` (assertion pass-through), never sent to the
    baseline split or the debug agent, and sticky across resume."""
    sub = Substate(tmp_path, "run-i")
    jobs = [{"slug": s, "label": s, "min_gpus": 1}
            for s in ("timeout", "assertion")]
    runs = {"timeout": tl.TestRunResult(1, infra="timeout"),
            "assertion": tl.TestRunResult(1)}
    baselined, debugged = [], []

    async def debug_fn(slug, label, rc, output):
        debugged.append(slug)
        return False

    def baseline_fn(slug):
        baselined.append(slug)
        return tl.TestRunResult(0)

    result = asyncio.run(tl.run_test_loop(
        jobs, substate=sub, run_fn=lambda s: runs[s],
        baseline_fn=baseline_fn, debug_fn=debug_fn))
    assert result["infra_failures"] == ["timeout: timeout"]
    assert result["failed_tests"] == ["assertion"]     # infra NOT here
    assert result["failed"] == 2
    assert baselined == ["assertion"] and debugged == ["assertion"]
    # resume: the recorded infra failure is not re-run (a lucky second
    # attempt must not flip a structural failure into a pass)
    reran = []
    result2 = asyncio.run(tl.run_test_loop(
        jobs, substate=sub, run_fn=lambda s: reran.append(s) or runs[s],
        baseline_fn=baseline_fn, debug_fn=debug_fn))
    assert "timeout" not in reran
    assert result2["infra_failures"] == ["timeout: timeout"]
    # an operator-requested rerun retries it; a pass clears the record
    runs["timeout"] = tl.TestRunResult(0)
    reran.clear()
    result3 = asyncio.run(tl.run_test_loop(
        jobs, substate=sub, run_fn=lambda s: reran.append(s) or runs[s],
        baseline_fn=baseline_fn, debug_fn=debug_fn, retry_infra=True))
    assert reran == ["timeout", "assertion"]
    assert result3["infra_failures"] == []
    assert "timeout" in result3["completed_slugs"]


def test_infra_failure_surfaces_assertions_it_already_reported(tmp_path):
    """A structural end does not mean the run found nothing.

    A suite can report assertion failures and THEN be killed. The loop must
    keep the STRUCTURAL classification (out of `failed_tests`, push gate
    blocks) while still naming the ids in the record -- otherwise the report
    reads "1 infra failure" and real regressions stay invisible.

    Regression test for 2026-09-19: simple_diffusion_test timed out after
    reporting 10 quantization assertion failures; all 10 were dropped with
    the output and only CI re-found them.
    """
    sub = Substate(tmp_path, "run-surface")
    jobs = [{"slug": "killed", "label": "killed", "min_gpus": 1}]
    output = (
        "FAILED tests/diffusion/models/glm_image/test_q.py::TestA::test_one\n"
        "FAILED tests/diffusion/models/glm_image/test_q.py::TestB::test_two\n"
        "[watchdog/kill] tier=1 test=killed\n"
    )
    runs = {"killed": tl.TestRunResult(1, output=output, infra="timeout")}

    async def debug_fn(slug, label, rc, out):
        return False

    result = asyncio.run(tl.run_test_loop(
        jobs, substate=sub, run_fn=lambda s: runs[s],
        baseline_fn=lambda s: tl.TestRunResult(0), debug_fn=debug_fn))

    # classification is unchanged: still structural, still not an assertion
    assert result["failed_tests"] == []
    assert result["failed"] == 1
    record = result["infra_failures"][0]
    assert record.startswith("killed: timeout")
    # ...but the findings it DID produce are now named
    assert "2 assertion failure(s)" in record
    assert "TestA::test_one" in record and "TestB::test_two" in record


def test_infra_failure_reads_assertions_from_a_log_PATH(tmp_path):
    """The production caller passes a PATH, not log text.

    `engine/steps/rebase_v3._to_result` sets TestRunResult.output to
    `outcome.log_file`. Parsing the pathname finds nothing, so the loop must
    read the file -- otherwise this surfacing never fires for the real
    timeout/watchdog runs it exists for.
    """
    sub = Substate(tmp_path, "run-path")
    log_file = tmp_path / "simple_diffusion_test.log"
    log_file.write_text(
        "collected 5802 items\n"
        "FAILED tests/diffusion/models/glm_image/test_q.py::TestA::test_one\n"
        "FAILED tests/diffusion/models/minimax_h3/test_c.py::test_two\n"
        "[watchdog/kill] tier=1 test=simple_diffusion_test\n"
    )
    jobs = [{"slug": "killed", "label": "killed", "min_gpus": 1}]
    runs = {"killed": tl.TestRunResult(1, output=str(log_file), infra="timeout")}

    async def debug_fn(slug, label, rc, out):
        return False

    result = asyncio.run(tl.run_test_loop(
        jobs, substate=sub, run_fn=lambda s: runs[s],
        baseline_fn=lambda s: tl.TestRunResult(0), debug_fn=debug_fn))
    record = result["infra_failures"][0]
    assert "2 assertion failure(s)" in record
    assert "TestA::test_one" in record
    assert result["failed_tests"] == []          # still structural


def test_infra_failure_unreadable_log_stays_structural(tmp_path):
    """An unreadable/missing log must not change the classification."""
    sub = Substate(tmp_path, "run-unreadable")
    jobs = [{"slug": "gone", "label": "gone", "min_gpus": 1}]
    missing = str(tmp_path / "does_not_exist.log")
    runs = {"gone": tl.TestRunResult(1, output=missing, infra="timeout")}

    async def debug_fn(slug, label, rc, out):
        return False

    result = asyncio.run(tl.run_test_loop(
        jobs, substate=sub, run_fn=lambda s: runs[s],
        baseline_fn=lambda s: tl.TestRunResult(0), debug_fn=debug_fn))
    assert result["infra_failures"] == ["gone: timeout"]
    assert result["failed_tests"] == []


def test_infra_failure_without_assertions_is_unchanged(tmp_path):
    """A genuine infra-only failure keeps the original terse record."""
    sub = Substate(tmp_path, "run-terse")
    jobs = [{"slug": "oom", "label": "oom", "min_gpus": 1}]
    runs = {"oom": tl.TestRunResult(1, output="Killed\n", infra="timeout")}

    async def debug_fn(slug, label, rc, out):
        return False

    result = asyncio.run(tl.run_test_loop(
        jobs, substate=sub, run_fn=lambda s: runs[s],
        baseline_fn=lambda s: tl.TestRunResult(0), debug_fn=debug_fn))
    assert result["infra_failures"] == ["oom: timeout"]



# -- CI build ledger + monitor -------------------------------------------------

class FakeCI:
    def __init__(self):
        self.builds: dict[str, dict] = {}
        self.created = 0
        self.logs: dict[tuple, str] = {}

    def create_build(self, *, branch, commit, message, meta_data):
        self.created += 1
        bid = f"b{self.created}"
        self.builds[bid] = {"id": bid, "web_url": f"u/{bid}",
                            "state": "running", "branch": branch,
                            "commit": commit,
                            "meta_data": dict(meta_data), "jobs": []}
        return self.builds[bid]

    def get_build(self, build_id):
        return self.builds[build_id]

    def find_builds_by_meta(self, key, value):
        return [b for b in self.builds.values()
                if b["meta_data"].get(key) == value]

    def cancel_build(self, build_id):
        self.builds[build_id]["state"] = "canceled"
        return self.builds[build_id]

    def get_job_log(self, build_id, job_id):
        return self.logs.get((build_id, job_id), "")

    def list_jobs(self, build_id):
        return list(self.builds[build_id].get("jobs") or [])

    def retry_job(self, build_id, job_id):
        return None, True

    def latest_builds(self, branch, states=(), per_page=30):
        return [b for b in self.builds.values()
                if b.get("branch") == branch
                and (not states or b.get("state") in states)]

    def builds_for_commit(self, branch, commit):
        return [b for b in self.builds.values()
                if b.get("branch") == branch and b.get("commit") == commit]


def test_build_op_guarded_create_and_recovery(tmp_path):
    ci = FakeCI()
    op = ci_loop.create_build_guarded(
        ci, tmp_path, op_id="op-1", run_id="r", purpose="initial",
        branch="ci-x", commit="c" * 40, message="m")
    assert op.state == "created" and op.build_id == "b1"
    assert ci.builds["b1"]["meta_data"]["imx_op_id"] == "op-1"
    # idempotent: the created record short-circuits, no second build
    op2 = ci_loop.create_build_guarded(
        ci, tmp_path, op_id="op-1", run_id="r", purpose="initial",
        branch="ci-x", commit="c" * 40, message="m")
    assert op2.build_id == "b1" and ci.created == 1

    # crash window: intent durable, create landed, ack lost — recovery
    # ADOPTS by exact op id instead of re-creating
    intent = ci_loop.BuildOp(op_id="op-2", run_id="r", purpose="retry",
                             branch="ci-x", commit="c" * 40)
    ci_loop._durable_write(intent.path(tmp_path),
                           ci_loop.__dict__["asdict"](intent))
    orphan = ci.create_build(branch="ci-x", commit="c" * 40, message="m",
                             meta_data={"imx_op_id": "op-2"})
    op3 = ci_loop.create_build_guarded(
        ci, tmp_path, op_id="op-2", run_id="r", purpose="retry",
        branch="ci-x", commit="c" * 40, message="m", sleep=lambda s: None)
    assert op3.build_id == orphan["id"] and ci.created == 2  # adopted

    # zero matches after bounded re-poll ⇒ ESCALATE, never re-create
    intent = ci_loop.BuildOp(op_id="op-3", run_id="r", purpose="retry",
                             branch="ci-x", commit="c" * 40)
    ci_loop._durable_write(intent.path(tmp_path),
                           ci_loop.__dict__["asdict"](intent))
    with pytest.raises(ci_loop.CIOpError, match="refusing to re-create"):
        ci_loop.create_build_guarded(
            ci, tmp_path, op_id="op-3", run_id="r", purpose="retry",
            branch="ci-x", commit="c" * 40, message="m",
            sleep=lambda s: None)
    assert ci.created == 2

    # op ids are SINGLE-USE identities: different parameters never adopt
    with pytest.raises(ci_loop.CIOpError, match="single-use"):
        ci_loop.create_build_guarded(
            ci, tmp_path, op_id="op-2", run_id="r", purpose="retry",
            branch="ci-x", commit="d" * 40, message="m",
            sleep=lambda s: None)

    # cancellation only for op-recorded builds
    assert ci_loop.cancel_build_guarded(ci, tmp_path, "op-1") is True
    assert ci.builds["b1"]["state"] == "canceled"
    assert ci_loop.cancel_build_guarded(ci, tmp_path, "op-nope") is False
    # a cancelled op is CONSUMED — recovery must never resurrect it
    with pytest.raises(ci_loop.CIOpError, match="terminal"):
        ci_loop.create_build_guarded(
            ci, tmp_path, op_id="op-1", run_id="r", purpose="initial",
            branch="ci-x", commit="c" * 40, message="m",
            sleep=lambda s: None)
    assert ci.created == 2


def test_monitor_classification(tmp_path):
    ci = FakeCI()
    b = ci.create_build(branch="x", commit="c" * 40, message="m",
                        meta_data={})
    spec = ci_loop.CIClassifySpec(
        ignorable_name_patterns=("(?i)statistics",),
        ignorable_log_patterns=("401 Unauthorized",))
    b["state"] = "failed"
    b["jobs"] = [
        {"name": "Good Job", "id": "j1", "state": "passed", "exit_status": 0},
        {"name": "Testcase Statistics", "id": "j2", "state": "failed",
         "exit_status": 1},
        {"name": "Gated Model", "id": "j3", "state": "failed",
         "exit_status": 1},
        {"name": "Real Failure", "id": "j4", "state": "failed",
         "exit_status": 2},
        {"name": "Budget Kill", "id": "j5", "state": "timed_out",
         "exit_status": 255},
        # a terminal BUILD can still carry non-terminal or torn jobs —
        # these must never read as passed
        {"name": "Still Running", "id": "j6", "state": "running"},
        {"name": "Torn Exit", "id": "j7", "state": "finished"},
        {"name": "Canceled Job", "id": "j8", "state": "canceled",
         "exit_status": 0},
    ]
    ci.logs[(b["id"], "j3")] = "fatal: 401 Unauthorized"
    ci.logs[(b["id"], "j4")] = "FAILED tests/x.py::test_y - boom"
    ci.logs[(b["id"], "j5")] = (
        "\x1b_bk;t=1000000\x07 dozens of tests executed and PASSED with "
        "healthy streaming output right up to the budget wall\n"
        "\x1b_bk;t=1200000\x07 Exceeded maximum job timeout")
    # timeout_sec=0: the monitor now WAITS for pending jobs inside a
    # terminal build (parent parity); an expired deadline pins the same
    # single-snapshot classification this test always asserted
    out = ci_loop.monitor_build(ci, b["id"], spec=spec, poll_sec=0,
                                timeout_sec=0, sleep=lambda s: None)
    cls = {j.name: j.classification for j in out.jobs}
    assert cls == {"Good Job": "passed", "Testcase Statistics": "ignored",
                   "Gated Model": "ignored", "Real Failure": "failed",
                   "Budget Kill": "budget_timeout",
                   "Still Running": "incomplete",
                   "Torn Exit": "incomplete",
                   "Canceled Job": "incomplete"}
    assert [j.name for j in out.failed_jobs] == ["Real Failure"]
    assert {j.name for j in out.incomplete_jobs} == \
        {"Still Running", "Torn Exit", "Canceled Job"}
    # no-run build states are TERMINAL, neither success nor failure
    b["state"] = "not_run"
    out = ci_loop.monitor_build(ci, b["id"], spec=spec, poll_sec=0,
                                sleep=lambda s: None)
    assert out.no_run and out.jobs == []


def test_pure_log_classifiers():
    line = "\x1b_bk;t=123\x07[2026-07-01T10:00:00Z] \x1b[31mFAILED\x1b[0m " \
           "tests/a.py::test_x - took 1.25s"
    n = ci_loop.normalize_log_line(line)
    assert n == "FAILED tests/a.py::test_x - took <N>s"
    ids = ci_loop.extract_failed_test_ids(line)
    assert ids == {"tests/a.py::test_x"}
    sig = ci_loop.extract_error_signature(
        "RuntimeError: boom at 3.14s\nFAILED tests/a.py::test_x - x",
        extra_exception_names=("MyCustomError",))
    assert "RuntimeError: boom at <N>s" in sig
    assert ci_loop.is_budget_timeout(
        "\x1b_bk;t=1000000\x07 many tests ran here and PASSED cleanly "
        "with plenty of output before the kill\n\x1b_bk;t=1100000\x07 "
        "Received cancellation signal")
    assert not ci_loop.is_budget_timeout("FAILED then "
                                         "Received cancellation signal PASSED")


# -- scrub wiring --------------------------------------------------------------

def test_scrubbed_agent_env_wiring(monkeypatch):
    from infermatrix_copilot.rebase_engine.testing_env import \
        scrubbed_agent_env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = scrubbed_agent_env({"EXTRA": "1"})
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/usr/bin" and env["EXTRA"] == "1"
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-secret"   # never mutated


# -- v3 playbook partial e2e ---------------------------------------------------

@pytest.fixture()
def v3_env(settings, tmp_path, monkeypatch):
    import shutil
    from infermatrix_copilot.engine.executor import Executor
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.playbooks.store import PlaybookStore
    # fixture repo with the LIVE nested .buildkite/cuda pipeline layout
    repo = tmp_path / "omni"
    (repo / ".buildkite" / "cuda").mkdir(parents=True)
    (repo / "tests").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".buildkite" / "cuda" / "test-merge.yml").write_text(
        yaml.safe_dump({
            "steps": [{"label": "Quick", "timeout_in_minutes": 1,
                       "commands": ["export QUICK_ENV=1", "true"]}]}))
    (repo / ".buildkite" / "cuda" / "test-nightly.yml").write_text(
        yaml.safe_dump({
            "steps": [{"label": "Nightly Soak", "timeout_in_minutes": 1,
                       "commands": ["true"]}]}))
    # the target venv (manifest repo.venv -> ${VLLM_OMNI_VENV})
    monkeypatch.setenv("VLLM_OMNI_VENV", str(tmp_path / "omni-venv"))
    # committed clean tree: report_only runs the read-only guard_clean
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    # adapter: the real manifest, repointed
    adir = Path(settings.adapters_dir) / "vllm_omni"
    adir.mkdir(parents=True, exist_ok=True)
    manifest = yaml.safe_load(
        (REPO_ROOT / "adapters/vllm_omni/manifest.yaml").read_text())
    manifest["repo"]["path"] = str(repo)
    # the real manifest DECLARES parent read-compat layers (rebase.knowledge)
    # and the prelude fails CLOSED on a declared-but-unreadable layer; this
    # fixture world has no parent checkout, so the fixture must not declare
    # one (knowledge-declaring paths get their own dedicated tests)
    manifest["rebase"].pop("knowledge", None)
    # production setup wiring (manifest -> ManifestSpec -> job -> TestJob):
    # the real map is empty, so the fixture declares one for the pin
    manifest["rebase"]["test_manifest"]["setup_map"] = {
        "quick": "echo huggingface-cli download org/quick x"}
    (adir / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    settings.playbooks_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / "playbooks" / "repo-rebase-v3.yaml",
                settings.playbooks_dir / "repo-rebase-v3.yaml")
    from infermatrix_copilot.run_trace import RunTrace
    registry = register_builtin_steps(StepRegistry())
    store = PlaybookStore(settings.playbooks_dir, registry)
    run_dir = tmp_path / "run-v3"
    run_dir.mkdir()
    executor = Executor(registry, settings, run_dir=run_dir,
                        trace=RunTrace(run_dir / "trace.jsonl"))
    return executor, store.get("repo-rebase-v3"), repo, run_dir


def test_v3_report_only_partial_e2e(v3_env, tmp_path, settings, trace):
    """report_only end to end: prelude seeds mode flags, the mutating guard
    and full-mode steps are when-gated OFF, the scan writes the manifest
    artifact, finalize passes on clean substate. The repo carries a lock
    file a PRIOR mutating run legitimately left under locks/ — report-only
    must not read persistent lock infrastructure as dirt."""
    executor, playbook, repo, run_dir = v3_env
    (repo / "locks").mkdir()
    (repo / "locks" / "omni.lock").write_text("")
    spec = SimpleNamespace(params={"rebase_mode": ""}, report_only=False)
    resolve_effective_mode(spec)
    state = {"task_spec": {"kind": "repo_rebase", "repo": "vllm-omni",
                           "params": spec.params},
             "repo_path": str(repo), "run_id": "run-e2e"}
    outcome = asyncio.run(executor.run(playbook, state))
    assert outcome.status == "done", getattr(outcome, "blocked_reason", "")
    data = json.loads((run_dir / "test_manifest.json").read_text())
    assert data["jobs"] and data["jobs"][0]["slug"] == "quick"
    # the when-gates held: no guard/wheel artifacts in a report_only run
    assert state.get("mode_report_only") is True
    assert state.get("mode_full") is False


def test_v3_finalize_terminal_row(v3_env, settings):
    """Rev 8 §3.1 row 2: all steps ok but substate failures ⇒ BLOCKED (the
    reused needs-human exit-3 semantics)."""
    executor, playbook, repo, _ = v3_env
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.run_trace import RunTrace
    registry = register_builtin_steps(StepRegistry())
    fin = registry.get("rebase.v3_finalize")
    run_dir = Path(settings.run_root) / "run-fin"
    run_dir.mkdir(parents=True, exist_ok=True)
    sub = Substate(run_dir, "run-fin")
    sub.update({"modules": {"worker": {"status": "failed"}}})
    ctx = StepContext(settings=settings, state={"run_id": "run-fin"},
                      params={}, run_dir=run_dir,
                      trace=RunTrace(run_dir / "trace.jsonl"))
    result = asyncio.run(fin.handler(ctx))
    assert not result.ok
    assert "needing a human" in result.summary
    # the honest terminal name — not "done" — is what substate records
    assert Substate(run_dir, "run-fin").get("phase") == "needs_human"
    # ...and a clean substate finalizes ok
    run_dir2 = Path(settings.run_root) / "run-fin2"
    run_dir2.mkdir(parents=True, exist_ok=True)
    Substate(run_dir2, "run-fin2").update({"modules": {"a": {"status": "done"}}})
    ctx2 = StepContext(settings=settings, state={"run_id": "run-fin2"},
                       params={}, run_dir=run_dir2,
                       trace=RunTrace(run_dir2 / "trace.jsonl"))
    assert asyncio.run(fin.handler(ctx2)).ok


# -- v3 per-mode matrix + runtime init -----------------------------------------

def test_v3_per_mode_matrix():
    """The playbook's `when:` gates realize the Rev 8 §2.2 matrix exactly:
    report_only gets the read-only guard + scan; local_ci runs the local
    loop but a VACUOUS push gate; remote_ci pushes without the local loop;
    full runs everything. Report precedes finalize (terminal row must not
    suppress RUN_REPORT); waves are ordered around the gate."""
    from infermatrix_copilot.engine.executor import _eval_when
    doc = yaml.safe_load(
        (REPO_ROOT / "playbooks" / "repo-rebase-v3.yaml").read_text())
    matrix = {
        "report_only": {"prelude", "guard_check", "scan", "report",
                        "finalize"},
        "local_ci": {"prelude", "guard", "knowledge_prep", "tests",
                     "precommit", "report", "finalize"},
        "remote_ci": {"prelude", "guard", "knowledge_prep", "push_gate",
                      "ci", "report", "finalize"},
        "full": {"prelude", "guard", "knowledge_prep", "wheel", "assign",
                 "wave1", "wave_gate", "wave2", "tests", "precommit",
                 "push_gate", "ci", "phase5_report", "curate", "compare",
                 "report", "finalize"},
    }
    for mode, expect in matrix.items():
        state = {"task_spec": {}, **mode_state_flags(mode)}
        ran = {s["id"] for s in doc["steps"]
               if "when" not in s or _eval_when(s["when"], state)}
        assert ran == expect, f"{mode}: {sorted(ran ^ expect)}"
    ids = [s["id"] for s in doc["steps"]]
    assert ids.index("report") < ids.index("finalize")
    # Rev 8 §2.2 tail order (completion design D5): the rebase summary
    # precedes curation, curation precedes the closing attestation, and
    # ALL of it precedes the final report — a curate failure can never
    # hide behind an already-written success report. knowledge_prep must
    # precede the first possible agent write (wave 1).
    assert (ids.index("phase5_report") < ids.index("curate")
            < ids.index("compare") < ids.index("report"))
    assert ids.index("guard") < ids.index("knowledge_prep") \
        < ids.index("wave1")
    # report_only stays store-read-only: knowledge_prep (a WRITE step)
    # must never run there
    ro_state = {"task_spec": {}, **mode_state_flags("report_only")}
    prep = next(s for s in doc["steps"] if s["id"] == "knowledge_prep")
    assert not _eval_when(prep["when"], ro_state)
    assert ids.index("wave1") < ids.index("wave_gate") < ids.index("wave2")


def test_v3_prelude_inits_runtime(v3_env, settings, trace, tmp_path,
                                  monkeypatch):
    """Mutating modes acquire the shared checkout flock(s) (name from
    adapter data), publish `upstream_path`/`last_rebase_upstream_commit`,
    and release the locks via the lifecycle finalizer on EVERY exit path;
    full mode without its preconditions is BLOCKED upfront."""
    from infermatrix_copilot.engine import lifecycle
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.rebase_engine.runctx import CheckoutLock
    _, _, repo, run_dir = v3_env
    monkeypatch.delenv("VLLM_UPSTREAM_REPO", raising=False)
    # "without upstream" means absent from the process env AND the
    # .env fallback (config.expansion_env) - isolate from the
    # developer's file
    monkeypatch.setattr(type(settings), "expansion_env",
                        lambda self: {})
    registry = register_builtin_steps(StepRegistry())
    prelude = registry.get("rebase.v3_prelude")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=upstream, check=True)
    (upstream / "seed.txt").write_text("u")
    subprocess.run(["git", "add", "-A"], cwd=upstream, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "seed"], cwd=upstream, check=True)

    def ctx_for(params, run_dir):
        return StepContext(
            settings=settings, params={}, run_dir=run_dir, trace=trace,
            state={"task_spec": {"kind": "repo_rebase", "repo": "vllm-omni",
                                 "params": params},
                   "repo_path": str(repo), "run_id": run_dir.name})

    # full without upstream/baseline: BLOCKED before any lock is taken
    r = asyncio.run(prelude.handler(
        ctx_for({"rebase_mode": "full"}, run_dir)))
    assert not r.ok and "upstream" in r.summary
    assert not (repo / "locks" / "omni.lock").exists()

    # full with both: locks acquired (adapter lock_name + upstream),
    # world published, and the finalizer releases them — upstream seeded
    # via state instead of the env var
    ctx = ctx_for({"rebase_mode": "full", "last_rebase_commit": "d" * 40},
                  run_dir)
    ctx.state["upstream_path"] = str(upstream)
    r = asyncio.run(prelude.handler(ctx))
    assert r.ok, r.summary
    ups = r.outputs["state_updates"]
    assert ups["upstream_origin_path"] == str(upstream)
    # full mode works on a per-run DISPOSABLE scratch clone, never the
    # canonical upstream tree
    assert ups["upstream_path"] == str(run_dir / "upstream_scratch")
    assert (run_dir / "upstream_scratch" / ".git").exists()
    assert ups["last_rebase_upstream_commit"] == "d" * 40
    assert ups["mode_runs_push_gate"] is True
    # both flocks held: a second taker fails
    probe = CheckoutLock(repo, "omni")
    assert probe.acquire(blocking=False) is False
    probe_up = CheckoutLock(upstream, "upstream")
    assert probe_up.acquire(blocking=False) is False
    # the lifecycle finalizer releases on every exit path — and tears the
    # disposable scratch down
    # (a crash-shaped outcome keeps the scratch: the run is resumable and
    # the target venv's editable vllm resolves into it; only a clean done
    # discards it — the finalizer list is consumed, so re-register for the
    # done pass)
    asyncio.run(lifecycle.finalize(run_dir, None))
    assert probe.acquire(blocking=False) is True
    probe.release()
    assert probe_up.acquire(blocking=False) is True
    probe_up.release()
    assert (run_dir / "upstream_scratch").is_dir()
    from infermatrix_copilot.engine.steps import rebase_v3
    rebase_v3._register_scratch_teardown(
        SimpleNamespace(run_dir=run_dir), run_dir / "upstream_scratch")
    asyncio.run(lifecycle.finalize(run_dir, SimpleNamespace(status="done")))
    assert not (run_dir / "upstream_scratch").exists()

    # report_only never locks
    run_dir2 = run_dir.parent / "run-ro"
    run_dir2.mkdir(exist_ok=True)
    r = asyncio.run(prelude.handler(
        ctx_for({"rebase_mode": "report_only"}, run_dir2)))
    assert r.ok
    ro_probe = CheckoutLock(repo, "omni")
    assert ro_probe.acquire(blocking=False) is True   # nothing was locked
    ro_probe.release()


def test_v3_wave_gate(v3_env, settings, trace):
    """A wave-1 module failure empties wave 2 (dependents never build on a
    broken base); halt_on_module_failure escalates instead."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import FailureKind, StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    _, _, repo, run_dir = v3_env
    registry = register_builtin_steps(StepRegistry())
    gate = registry.get("rebase.v3_wave_gate")
    sub = Substate(run_dir, "run-w")
    sub.update({"modules": {"m1": {"status": "failed"},
                            "m2": {"status": "done"}}})

    def ctx_for(params):
        return StepContext(
            settings=settings, params={}, run_dir=run_dir, trace=trace,
            state={"task_spec": {"params": params}, "run_id": "run-w",
                   "wave1_modules": ["m1", "m2"],
                   "wave2_modules": ["m3"]})

    r = asyncio.run(gate.handler(ctx_for({})))
    assert r.ok and r.outputs["state_updates"]["wave2_modules"] == []
    r = asyncio.run(gate.handler(ctx_for({"halt_on_module_failure": True})))
    assert not r.ok and r.failure is FailureKind.ESCALATE
    # clean wave 1 leaves wave 2 alone
    sub.update({"modules": {"m1": {"status": "done"}}})
    r = asyncio.run(gate.handler(ctx_for({})))
    assert r.ok and "state_updates" not in (r.outputs or {})


def test_v3_assign_publishes_waves(v3_env, settings, trace, monkeypatch):
    """The assign step orders active modules into the manifest's wave lists
    — the playbook's wave1/wave2 fan-outs depend on them."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3, \
        register_builtin_steps
    _, _, repo, run_dir = v3_env
    manifest = yaml.safe_load(
        (Path(settings.adapters_dir) / "vllm_omni" / "manifest.yaml")
        .read_text())
    modules = list(manifest["modules"])
    active = modules[:3]
    monkeypatch.setattr(
        rebase_v3, "run_commit_assignment",
        lambda cfg, paths, sub: SimpleNamespace(
            total_commits=7,
            skip={m: (m not in active) for m in modules}))
    registry = register_builtin_steps(StepRegistry())
    ctx = StepContext(
        settings=settings, params={}, run_dir=run_dir, trace=trace,
        state={"task_spec": {"repo": "vllm-omni", "params": {}},
               "run_id": "run-a", "repo_path": str(repo),
               "upstream_origin_path": str(repo),
               "last_rebase_upstream_commit": "d" * 40})
    r = asyncio.run(registry.get("rebase.v3_assign").handler(ctx))
    assert r.ok, r.summary
    ups = r.outputs["state_updates"]
    exp_w1 = [m for m in modules if m in active
              and (manifest["modules"][m] or {}).get("wave") == 1]
    exp_w2 = [m for m in active if m not in exp_w1]
    assert ups["wave1_modules"] == exp_w1
    assert sorted(ups["wave2_modules"]) == sorted(exp_w2)
    assert ups["active_modules"] == active


# -- v3 test-loop step contract ------------------------------------------------

def test_parse_env_pairs():
    from infermatrix_copilot.engine.steps.rebase_v3 import _parse_env_pairs
    assert _parse_env_pairs("FOO=1 BAR=a=b") == {"FOO": "1", "BAR": "a=b"}
    assert _parse_env_pairs("") == {}
    assert _parse_env_pairs("NOEQ FOO=x") == {"FOO": "x"}


def test_v3_test_loop_step_contract(v3_env, settings, trace, monkeypatch):
    """The local loop excludes nightly jobs, passes each job's declared env
    pairs to the child, INHERITS the process env for tests (the scrub is
    agent-shell-only — Rev 8 §6), and runs the baseline with the worktree
    prepended to PYTHONPATH (main's files execute main's code). A missing
    debug backend is a STRUCTURAL failure, not an assertion."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import FailureKind, StepContext
    from infermatrix_copilot.engine.step import StepResult
    from infermatrix_copilot.engine.steps import rebase_v3, \
        register_builtin_steps
    from infermatrix_copilot.rebase_engine import test_loop as tl_mod
    from infermatrix_copilot.testing import runner as runner_mod
    _, _, repo, run_dir = v3_env
    # no debug backend in this test regardless of the machine env
    monkeypatch.setattr(
        rebase_v3, "_tier_client",
        lambda ctx: StepResult(False, FailureKind.BLOCKED, "no backend"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-inherit")
    calls = []

    def fake_run(self, job, env, *, baseline=False, dry_run=False):
        calls.append({"key": job.key, "job_env": dict(job.env),
                      "env": dict(env), "baseline": baseline,
                      "setup": job.setup})
        return runner_mod.TestOutcome(rc=0 if baseline else 1,
                                      log_file="")
    monkeypatch.setattr(runner_mod.TestRunner, "run", fake_run)
    wt = run_dir / "main_worktree"
    monkeypatch.setattr(tl_mod, "ensure_main_worktree",
                        lambda repo, path, base_ref="origin/main": wt)
    monkeypatch.setattr(tl_mod, "remove_main_worktree",
                        lambda repo, path: None)
    registry = register_builtin_steps(StepRegistry())
    ctx = StepContext(
        settings=settings, params={}, run_dir=run_dir, trace=trace,
        state={"task_spec": {"repo": "vllm-omni", "params": {}},
               "run_id": "run-t", "repo_path": str(repo)})
    r = asyncio.run(registry.get("rebase.v3_test_loop").handler(ctx))
    assert r.ok, r.summary
    # nightly excluded from the local loop
    assert {c["key"] for c in calls} == {"quick"}
    rebase_call = next(c for c in calls if not c["baseline"])
    baseline_call = next(c for c in calls if c["baseline"])
    # the job's declared env pairs reach the child
    assert rebase_call["job_env"] == {"QUICK_ENV": "1"}
    assert baseline_call["job_env"] == {"QUICK_ENV": "1"}
    # the manifest setup_map reaches TestJob.setup THROUGH production
    # wiring (feeds the runner's model-download notification hook)
    assert rebase_call["setup"] == "echo huggingface-cli download org/quick x"
    # tests INHERIT the process env — the agent-shell scrub must not strip
    # a test's required credentials (misclassification hazard)
    assert rebase_call["env"].get("ANTHROPIC_API_KEY") == "sk-test-inherit"
    assert baseline_call["env"].get("ANTHROPIC_API_KEY") == "sk-test-inherit"
    # baseline PYTHONPATH prepends the worktree
    assert baseline_call["env"]["PYTHONPATH"].split(":")[0] == str(wt)
    assert rebase_call["env"].get("PYTHONPATH", "").split(":")[0] != str(wt)
    # rc=1 vs baseline rc=0 = regression; a MISSING debug backend is a
    # structural (infra) failure, never an ordinary assertion failure
    data = Substate(run_dir, "run-t").read()
    assert data["tests"]["pipeline"]["failed_tests"] == []
    assert data["tests"]["infra_failures"] == [
        "quick: debug backend unavailable (capability_gap)"]
    assert not evaluate_push_gate(data, {}).allowed


def test_v3_dropped_steps_are_structural(v3_env, settings, trace, tmp_path,
                                         monkeypatch):
    """A labeled CI step the builder DROPPED for lack of a runnable
    command becomes a structural infra failure — the push gate cannot
    pass over an omitted test (round-2 fail-open finding: other jobs
    passing must not hide the omission)."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext, StepResult
    from infermatrix_copilot.engine.step import FailureKind
    from infermatrix_copilot.engine.steps import rebase_v3, \
        register_builtin_steps
    from infermatrix_copilot.testing import runner as runner_mod
    repo2 = tmp_path / "dropped-repo"
    (repo2 / ".buildkite" / "cuda").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo2, check=True)
    (repo2 / ".buildkite" / "cuda" / "test-merge.yml").write_text(
        yaml.safe_dump({"steps": [
            {"label": "Good", "timeout_in_minutes": 1, "commands": ["true"]},
            {"label": "Broken Stub", "timeout_in_minutes": 1,
             "commands": []}]}))
    monkeypatch.setattr(
        runner_mod.TestRunner, "run",
        lambda self, job, env, *, baseline=False, dry_run=False:
        runner_mod.TestOutcome(rc=0))
    monkeypatch.setattr(
        rebase_v3, "_tier_client",
        lambda ctx: StepResult(False, FailureKind.BLOCKED, "no backend"))
    registry = register_builtin_steps(StepRegistry())
    run_dir = tmp_path / "run-dropped"
    run_dir.mkdir()
    ctx = StepContext(
        settings=settings, params={}, run_dir=run_dir, trace=trace,
        state={"task_spec": {"repo": "vllm-omni", "params": {}},
               "run_id": "run-dr", "repo_path": str(repo2)})
    r = asyncio.run(registry.get("rebase.v3_test_loop").handler(ctx))
    assert r.ok, r.summary
    data = Substate(run_dir, "run-dr").read()
    assert data["tests"]["infra_failures"] == [
        "step 'Broken Stub': no runnable command"]
    assert not evaluate_push_gate(data, {}).allowed     # gate blocks
    assert any(e for e in trace.events("manifest_steps_dropped"))


def test_v3_test_loop_empty_manifest_is_structural(v3_env, settings, trace,
                                                   tmp_path):
    """Zero runnable local jobs marks `manifest_empty` — the push gate
    blocks instead of sailing through a vacuous '0 failed'."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    repo2 = tmp_path / "nightly-only"
    (repo2 / ".buildkite" / "cuda").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo2, check=True)
    (repo2 / ".buildkite" / "cuda" / "test-nightly.yml").write_text(
        yaml.safe_dump({
            "steps": [{"label": "Soak", "timeout_in_minutes": 1,
                       "commands": ["true"]}]}))
    # plus a merge-pipeline step that gets DROPPED (comment-only): the
    # all-dropped/empty path must still carry the labels (round-2 P3)
    (repo2 / ".buildkite" / "cuda" / "test-merge.yml").write_text(
        yaml.safe_dump({
            "steps": [{"label": "Disabled Stub", "timeout_in_minutes": 1,
                       "commands": ["# temporarily disabled"]}]}))
    registry = register_builtin_steps(StepRegistry())
    run_dir = tmp_path / "run-empty"
    run_dir.mkdir()
    ctx = StepContext(
        settings=settings, params={}, run_dir=run_dir, trace=trace,
        state={"task_spec": {"repo": "vllm-omni", "params": {}},
               "run_id": "run-e", "repo_path": str(repo2)})
    r = asyncio.run(registry.get("rebase.v3_test_loop").handler(ctx))
    assert r.ok and "manifest_empty" in r.summary
    sub = Substate(run_dir, "run-e").read()
    assert sub["manifest_empty"] is True
    # the dropped step's label is recorded even on the all-dropped path
    assert sub["tests"]["infra_failures"] == [
        "step 'Disabled Stub': no runnable command"]
    assert any(e for e in trace.events("manifest_steps_dropped"))
    assert not evaluate_push_gate(sub, {}).allowed


# -- v3 agent wiring -----------------------------------------------------------

@pytest.fixture()
def v3_agent_env(v3_env, settings):
    """v3_env plus the real adapter rebase/ data (templates, tool schemas,
    hooks) so agent-assembly paths load for real."""
    import shutil
    adir = Path(settings.adapters_dir) / "vllm_omni"
    shutil.copytree(REPO_ROOT / "adapters" / "vllm_omni" / "rebase",
                    adir / "rebase")
    return v3_env


def test_v3_debug_fn_invokes_agent_and_verifies(v3_agent_env, settings,
                                                trace, monkeypatch):
    """The regression debug_fn actually invokes the configured agent
    backend (require_plan_review off, debug prompt) and only a green
    RE-RUN counts as fixed."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3, \
        register_builtin_steps
    from infermatrix_copilot.rebase_engine import agent_loop as al
    from infermatrix_copilot.rebase_engine import test_loop as tl_mod
    from infermatrix_copilot.testing import runner as runner_mod
    _, _, repo, run_dir = v3_agent_env
    monkeypatch.setattr(
        rebase_v3, "_tier_client",
        lambda ctx: (object(), SimpleNamespace(
            model="m-test", api_key="k", base_url="", source="tier:eco")))
    agent_calls = []

    async def fake_loop(client, prompt, **kw):
        agent_calls.append(kw)
        return {"done": True, "text": "fixed", "turns": 2}
    monkeypatch.setattr(al, "run_agent_loop", fake_loop)

    runs = {"quick": 0}

    def fake_run(self, job, env, *, baseline=False, dry_run=False):
        if baseline:
            return runner_mod.TestOutcome(rc=0)
        runs[job.key] += 1
        # first rebase run fails; the post-debug re-run passes
        return runner_mod.TestOutcome(rc=1 if runs[job.key] == 1 else 0)
    monkeypatch.setattr(runner_mod.TestRunner, "run", fake_run)
    monkeypatch.setattr(tl_mod, "ensure_main_worktree",
                        lambda repo, path, base_ref="origin/main":
                        run_dir / "wt")
    monkeypatch.setattr(tl_mod, "remove_main_worktree",
                        lambda repo, path: None)
    registry = register_builtin_steps(StepRegistry())
    ctx = StepContext(
        settings=settings, params={}, run_dir=run_dir, trace=trace,
        state={"task_spec": {"repo": "vllm-omni", "params": {}},
               "run_id": "run-d", "repo_path": str(repo)})
    r = asyncio.run(registry.get("rebase.v3_test_loop").handler(ctx))
    assert r.ok, r.summary
    assert len(agent_calls) == 1
    assert agent_calls[0]["require_plan_review"] is False
    assert agent_calls[0]["scope"] is not None       # C5: debug is scoped too
    assert runs["quick"] == 2                        # verified by re-run
    data = Substate(run_dir, "run-d").read()
    assert data["tests"]["pipeline"]["failed_tests"] == []
    assert data["tests"]["pipeline"]["passed"] == 1


def test_v3_module_scope_and_serialization(v3_agent_env, settings, trace,
                                           monkeypatch):
    """C5: every module agent gets a ToolScope — repo tree as the writable
    wall, manifest local_paths as primary. Fan-out siblings SERIALIZE (they
    mutate the same checkout)."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3, \
        register_builtin_steps
    from infermatrix_copilot.rebase_engine import module_rebase as mr
    _, _, repo, run_dir = v3_agent_env
    monkeypatch.setattr(
        rebase_v3, "_tier_client",
        lambda ctx: (object(), SimpleNamespace(
            model="m-test", api_key="k", base_url="", source="tier:eco")))
    events, scopes, configs = [], {}, {}

    async def fake_rebase_module(module, **kw):
        scopes[module] = kw.get("scope")
        configs[module] = kw.get("config")
        events.append(("enter", module))
        await asyncio.sleep(0.02)
        events.append(("exit", module))
        return {"status": "done", "exit_code": 0, "debug_attempts": 0,
                "turns": 1, "summary": ""}
    monkeypatch.setattr(mr, "rebase_module", fake_rebase_module)
    # capture the agent-shell env handed to the tool set
    from infermatrix_copilot.rebase_engine import rebase_tools as rt_mod
    agent_envs = {}
    orig_build = rt_mod.build_rebase_tools

    def spy_build(defs, paths, backends):
        agent_envs.update(paths.env)
        return orig_build(defs, paths, backends)
    monkeypatch.setattr(rt_mod, "build_rebase_tools", spy_build)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-agent-secret")
    monkeypatch.setenv("HF_TOKEN", "hf-gated-secret")
    registry = register_builtin_steps(StepRegistry())
    handler = registry.get("rebase.v3_module_rebase").handler

    def ctx_for(item):
        return StepContext(
            settings=settings, params={}, run_dir=run_dir, trace=trace,
            item=item,
            state={"task_spec": {"repo": "vllm-omni", "params": {}},
                   "run_id": "run-m", "repo_path": str(repo)})

    async def both():
        return await asyncio.gather(
            handler(ctx_for("worker_runner")),
            handler(ctx_for("model_executor")))
    r1, r2 = asyncio.run(both())
    assert r1.ok and r2.ok
    # serialized: no interleaving of enter/exit
    assert events[0][0] == "enter" and events[1] == ("exit", events[0][1])
    assert events[2][0] == "enter" and events[3] == ("exit", events[2][1])
    scope = scopes["worker_runner"]
    root = Path(str(repo)).resolve().as_posix()
    rd = Path(str(run_dir)).resolve().as_posix()
    assert scope.name == "rebase-module:worker_runner"
    # writable wall: the repo tree AND the run's artifact dir (the plan
    # gate REQUIRES the decision write under <run_dir>/plans/)
    assert scope.path_scope.writable == (f"{root}/*", f"{rd}/*")
    assert any(p.startswith(f"{root}/vllm_omni/worker")
               for p in scope.path_scope.primary)
    assert f"{rd}/plans/*" in scope.path_scope.primary
    assert scope.root == root
    signal_path = str(Path(configs["worker_runner"].signal_dir)
                      / "module.worker_runner.done")
    assert configs["worker_runner"].signal_dir == str(run_dir / "signals")
    assert scope.path_scope.check_write(signal_path).allowed
    # the decision write is allowed AND in primary (no out-of-scope noise)
    d = scope.path_scope.check_write(
        f"{rd}/plans/module-worker_runner/x.decision.md")
    assert d.allowed and not d.out_of_scope
    # ...and through the REAL dispatch choke point with the REAL write_file
    # tool (the integration the mocks were hiding)
    from infermatrix_copilot import tools as tools_mod
    from infermatrix_copilot.rebase_engine.rebase_tools import (
        RebaseBackends, RebasePaths, build_rebase_tools, load_tool_schemas)
    defs = load_tool_schemas(Path(settings.adapters_dir) / "vllm_omni"
                             / "rebase" / "tool_schemas.json")
    extra = build_rebase_tools(
        defs, RebasePaths(omni_path=str(repo), vllm_path=str(repo),
                          env={}), RebaseBackends())
    plan_file = Path(rd) / "plans" / "module-worker_runner" / "p.decision.md"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    out = tools_mod.dispatch("write_file",
                             {"file_path": str(plan_file), "content": "d"},
                             scope=scope, extra=extra, trace=trace)
    assert out["ok"] and not out["out_of_scope"], out
    assert plan_file.read_text() == "d"
    # a write inside the repo but outside local_paths is out-of-scope
    d = scope.path_scope.check_write(f"{root}/vllm_omni/engine/core.py")
    assert d.allowed and d.out_of_scope
    assert not scope.path_scope.check_write("/etc/passwd").allowed
    # the agent shell gets the imx-omni-pytest contract + TARGET runtime
    # overlay, with credentials scrubbed
    assert agent_envs["IMX_TARGET_REPO"] == str(repo)
    assert agent_envs["IMX_LOG_DIR"] == str(run_dir)
    assert agent_envs["IMX_GPU_MUTEX"] == "1"
    assert agent_envs["IMX_ADAPTER_REBASE"].endswith("vllm_omni/rebase")
    assert agent_envs["VIRTUAL_ENV"].endswith("omni-venv")
    assert "ANTHROPIC_API_KEY" not in agent_envs      # scrub still applies
    # HF token retained under the manifest's EXPLICIT opt-in (Rev 8 §4:
    # validation.requires_hf_token — gated-model verification needs it)...
    assert agent_envs["HF_TOKEN"] == "hf-gated-secret"
    # ...and stripped for an adapter that does NOT declare the opt-in
    from infermatrix_copilot.engine.steps.rebase_v3 import _agent_shell_env
    manifest_no_opt = yaml.safe_load(
        (Path(settings.adapters_dir) / "vllm_omni" / "manifest.yaml")
        .read_text())
    manifest_no_opt["validation"].pop("requires_hf_token")
    plain_ctx = SimpleNamespace(run_dir=run_dir, settings=settings)
    env2 = _agent_shell_env(plain_ctx, manifest_no_opt, str(repo),
                            Path(settings.adapters_dir) / "vllm_omni")
    assert "HF_TOKEN" not in env2
    # the served-model policy travels with the config (aliases + mismatch);
    # CUDA/HF facts describe THIS host, not the dataclass defaults
    cfg = configs["worker_runner"]
    assert cfg.model_mismatch_policy == settings.model_mismatch_policy
    assert cfg.model_aliases == settings.model_aliases
    import os as _os
    assert cfg.cuda_devices == _os.environ.get("CUDA_VISIBLE_DEVICES",
                                               "0,1")
    # an UNKNOWN module (unassigned debug job) records EVERY repo write as
    # out-of-scope instead of silently blessing the whole tree
    manifest = yaml.safe_load(
        (Path(settings.adapters_dir) / "vllm_omni" / "manifest.yaml")
        .read_text())
    from infermatrix_copilot.engine.steps.rebase_v3 import _module_scope
    anon = _module_scope(str(repo), "no_such_module", manifest)
    d = anon.path_scope.check_write(f"{root}/vllm_omni/worker/gpu.py")
    assert d.allowed and d.out_of_scope
    # in-process resume under a NEW event loop must not reuse the old
    # loop's lock: weak loop keys mean a fresh loop gets a fresh lock
    r3 = asyncio.run(handler(ctx_for("worker_runner")))
    assert r3.ok
    from infermatrix_copilot.engine.steps.rebase_v3 import _serial_lock

    async def probe_lock():
        return _serial_lock(run_dir)
    l1 = asyncio.run(probe_lock())
    l2 = asyncio.run(probe_lock())
    assert l1 is not l2                      # per-loop, never resurrected


def test_v3_tier_client_pairs_endpoint_and_credential(trace, tmp_path,
                                                      monkeypatch):
    """The module/debug backend comes from `tier_target()` — model,
    endpoint, and credential resolved atomically (independent eco backend
    honored; no key ⇒ capability_gap BLOCKED)."""
    import anthropic
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.engine.step import StepResult
    from infermatrix_copilot.engine.steps.rebase_v3 import _tier_client
    captured = {}

    class FakeClient:
        def __init__(self, **kw):
            captured.update(kw)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeClient)
    s = Settings(_env_file=None, run_root=tmp_path / "runs",
                 eco_model="eco-m", eco_base_url="https://eco.example",
                 eco_api_key="eco-key", anthropic_api_key="global-key")
    ctx = SimpleNamespace(settings=s, trace=trace,
                          state={"task_spec": {"mode": "eco"}})
    client, target = _tier_client(ctx)
    assert isinstance(client, FakeClient)
    assert captured == {"api_key": "eco-key",
                        "base_url": "https://eco.example"}
    assert target.model == "eco-m" and target.source == "tier:eco"
    # no credential anywhere: BLOCKED with a declared capability_gap
    s2 = Settings(_env_file=None, run_root=tmp_path / "runs2",
                  anthropic_api_key="", eco_model="", eco_base_url="",
                  eco_api_key="")
    ctx2 = SimpleNamespace(settings=s2, trace=trace,
                           state={"task_spec": {"mode": "eco"}})
    r = _tier_client(ctx2)
    assert isinstance(r, StepResult) and not r.ok


# -- round 2: baseline/debug taxonomy, preconditions, precommit, artifacts -----

def test_test_loop_baseline_infra_preserves_regression(tmp_path):
    """A baseline timeout is NOT evidence the test fails on main — it must
    go to the debug/regression path, never be classified pre-existing."""
    sub = Substate(tmp_path, "run-bi")
    jobs = [{"slug": "t", "label": "t", "min_gpus": 1}]
    debugged = []

    async def debug_fn(slug, label, rc, output):
        debugged.append(slug)
        return False

    result = asyncio.run(tl.run_test_loop(
        jobs, substate=sub, run_fn=lambda s: tl.TestRunResult(1),
        baseline_fn=lambda s: tl.TestRunResult(1, infra="timeout"),
        debug_fn=debug_fn))
    assert debugged == ["t"]                      # went to debug
    assert result["skipped_tests"] == []          # NOT pre-existing
    assert result["failed_tests"] == ["t"]


def test_test_loop_debug_structural_verdict(tmp_path):
    """A debug_fn string verdict (backend missing/crashed, unverifiable
    re-run) is recorded under infra_failures — the push gate blocks it —
    never under the assertion pass-through."""
    sub = Substate(tmp_path, "run-dv")
    jobs = [{"slug": s, "label": s, "min_gpus": 1}
            for s in ("nobackend", "gaveup")]
    verdicts = {"nobackend": "debug backend unavailable (capability_gap)",
                "gaveup": False}

    async def debug_fn(slug, label, rc, output):
        return verdicts[slug]

    result = asyncio.run(tl.run_test_loop(
        jobs, substate=sub, run_fn=lambda s: tl.TestRunResult(1),
        baseline_fn=lambda s: tl.TestRunResult(0), debug_fn=debug_fn))
    assert result["infra_failures"] == [
        "nobackend: debug backend unavailable (capability_gap)"]
    assert result["failed_tests"] == ["gaveup"]
    assert not evaluate_push_gate(
        {"tests": {"infra_failures": result["infra_failures"],
                   "pipeline": {"failed_tests": []}}}, {}).allowed


def test_pin_present(tmp_path):
    from infermatrix_copilot.rebase_engine.wheel import PinSpec, pin_present
    pin = PinSpec(dockerfile="docker/Dockerfile.ci",
                  url_pattern=r"wheels\.example\.com/[0-9a-f]{40}",
                  url_template="wheels.example.com/{commit}",
                  commit_env_var="WHEEL_COMMIT")
    repo = tmp_path / "r"
    (repo / "docker").mkdir(parents=True)
    assert pin_present(repo, pin) is False           # no dockerfile
    df = repo / "docker" / "Dockerfile.ci"
    df.write_text("FROM x\nRUN true\n")
    assert pin_present(repo, pin) is False           # no pin
    df.write_text(f"FROM x\nENV WHEEL_COMMIT={'a' * 40}\n")
    assert pin_present(repo, pin) is True            # ENV form
    df.write_text(f"FROM x\nRUN pip install https://wheels.example.com/"
                  f"{'b' * 40}/pkg.whl\n")
    assert pin_present(repo, pin) is True            # URL form


def test_v3_prelude_prepared_tree_preconditions(v3_env, settings, trace,
                                                tmp_path, monkeypatch):
    """local_ci/remote_ci operate on a PREPARED tree: no wheel pin ⇒
    BLOCKED; remote_ci additionally needs the upstream-commit signal."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    _, _, repo, run_dir = v3_env
    monkeypatch.delenv("VLLM_UPSTREAM_REPO", raising=False)
    # "without upstream" means absent from the process env AND the
    # .env fallback (config.expansion_env) - isolate from the
    # developer's file
    monkeypatch.setattr(type(settings), "expansion_env",
                        lambda self: {})
    registry = register_builtin_steps(StepRegistry())
    prelude = registry.get("rebase.v3_prelude")

    def ctx_for(params, rd):
        rd.mkdir(exist_ok=True)
        return StepContext(
            settings=settings, params={}, run_dir=rd, trace=trace,
            state={"task_spec": {"kind": "repo_rebase", "repo": "vllm-omni",
                                 "params": params},
                   "repo_path": str(repo), "run_id": rd.name})

    # no wheel pin in the tree: both prepared-tree modes refuse
    r = asyncio.run(prelude.handler(
        ctx_for({"rebase_mode": "local_ci"}, tmp_path / "p1")))
    assert not r.ok and "wheel pin" in r.summary
    r = asyncio.run(prelude.handler(
        ctx_for({"rebase_mode": "remote_ci"}, tmp_path / "p2")))
    assert not r.ok and "wheel pin" in r.summary

    # pin the tree (manifest pin spec: ENV VLLM_PRECOMPILED_WHEEL_COMMIT)
    (repo / "docker").mkdir(exist_ok=True)
    (repo / "docker" / "Dockerfile.ci").write_text(
        f"FROM base\nENV VLLM_PRECOMPILED_WHEEL_COMMIT={'c' * 40}\n")
    r = asyncio.run(prelude.handler(
        ctx_for({"rebase_mode": "local_ci"}, tmp_path / "p3")))
    assert r.ok, r.summary
    asyncio.run(_finalize_run(tmp_path / "p3"))

    # remote_ci still refuses without the upstream-commit signal...
    r = asyncio.run(prelude.handler(
        ctx_for({"rebase_mode": "remote_ci"}, tmp_path / "p4")))
    assert not r.ok and "upstream-commit" in r.summary
    # ...and publishes it when given
    r = asyncio.run(prelude.handler(
        ctx_for({"rebase_mode": "remote_ci",
                 "upstream_commit": "e" * 40}, tmp_path / "p5")))
    assert r.ok, r.summary
    assert r.outputs["state_updates"]["upstream_commit"] == "e" * 40
    asyncio.run(_finalize_run(tmp_path / "p5"))


async def _finalize_run(run_dir):
    from infermatrix_copilot.engine import lifecycle
    await lifecycle.finalize(run_dir, None)


def test_v3_terminal_report_finalizer(v3_env, settings, trace, tmp_path):
    """Transition-table row 3: a run that blocks before the report step
    still gets RUN_REPORT.md, written by the prelude-registered lifecycle
    finalizer (augments artifacts, never upgrades); an existing report is
    never overwritten."""
    from infermatrix_copilot.engine import lifecycle
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    _, _, repo, _ = v3_env
    registry = register_builtin_steps(StepRegistry())
    prelude = registry.get("rebase.v3_prelude")
    run_dir = tmp_path / "run-blocked"
    run_dir.mkdir()
    ctx = StepContext(
        settings=settings, params={}, run_dir=run_dir, trace=trace,
        state={"task_spec": {"kind": "repo_rebase", "repo": "vllm-omni",
                             "params": {"rebase_mode": "report_only"}},
               "repo_path": str(repo), "run_id": "run-blocked"})
    assert asyncio.run(prelude.handler(ctx)).ok
    outcome = SimpleNamespace(status="blocked")
    asyncio.run(lifecycle.finalize(run_dir, outcome))
    report = (run_dir / "RUN_REPORT.md").read_text()
    assert "status: blocked" in report
    assert "rebase_mode: report_only" in report
    # idempotent + never clobbers the real report
    (run_dir / "RUN_REPORT.md").write_text("REAL REPORT")
    ctx2 = StepContext(
        settings=settings, params={}, run_dir=run_dir, trace=trace,
        state=dict(ctx.state))
    assert asyncio.run(prelude.handler(ctx2)).ok
    asyncio.run(lifecycle.finalize(run_dir, outcome))
    assert (run_dir / "RUN_REPORT.md").read_text() == "REAL REPORT"


def test_v3_module_rebase_idempotent_on_resume(v3_agent_env, settings,
                                               trace, monkeypatch):
    """Crash window: substate says the module is done but the executor
    checkpoint was lost — re-entry must NOT run the agent (and apply its
    edits) twice; the substate short-circuits."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3, \
        register_builtin_steps
    from infermatrix_copilot.rebase_engine import module_rebase as mr
    _, _, repo, run_dir = v3_agent_env
    monkeypatch.setattr(
        rebase_v3, "_tier_client",
        lambda ctx: (object(), SimpleNamespace(
            model="m", api_key="k", base_url="", source="global")))
    called = []

    async def fake_rebase_module(module, **kw):
        called.append(module)
        return {"status": "done", "exit_code": 0, "debug_attempts": 0,
                "turns": 1, "summary": ""}
    monkeypatch.setattr(mr, "rebase_module", fake_rebase_module)
    Substate(run_dir, "run-idem").update(
        {"modules": {"worker_runner": {"status": "done", "exit_code": 0}}})
    registry = register_builtin_steps(StepRegistry())
    ctx = StepContext(
        settings=settings, params={}, run_dir=run_dir, trace=trace,
        item="worker_runner",
        state={"task_spec": {"repo": "vllm-omni", "params": {}},
               "run_id": "run-idem", "repo_path": str(repo)})
    r = asyncio.run(registry.get("rebase.v3_module_rebase").handler(ctx))
    assert r.ok and "short-circuit" in r.summary
    assert called == []                              # agent NOT re-run
    assert r.outputs["state_updates"]["module_worker_runner_status"] == "done"


def test_v3_module_rebase_reruns_interrupted_and_blocks_on_interrupt(
        v3_agent_env, settings, trace, monkeypatch):
    """A SIGINT/SIGTERM leaves a module `interrupted`: on resume that module
    is NOT short-circuited (only done/skipped are) and the agent runs again;
    and an attempt that itself ends `interrupted` BLOCKS the step so the run
    stops instead of advancing to the next sibling."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import FailureKind, StepContext
    from infermatrix_copilot.engine.steps import rebase_v3, \
        register_builtin_steps
    from infermatrix_copilot.rebase_engine import module_rebase as mr
    _, _, repo, run_dir = v3_agent_env
    monkeypatch.setattr(
        rebase_v3, "_tier_client",
        lambda ctx: (object(), SimpleNamespace(
            model="m", api_key="k", base_url="", source="global")))
    called = []
    outcomes = iter([
        {"status": "done", "exit_code": 0, "debug_attempts": 0,
         "turns": 1, "summary": ""},
        {"status": "interrupted", "exit_code": -1, "debug_attempts": 1,
         "turns": 4, "interrupted_by": "SIGTERM", "summary": "cut"},
    ])

    async def fake_rebase_module(module, **kw):
        called.append(module)
        return next(outcomes)
    monkeypatch.setattr(mr, "rebase_module", fake_rebase_module)
    sub = Substate(run_dir, "run-int")
    sub.update({"modules": {"worker_runner": {"status": "interrupted"},
                            "model_executor": {"status": "done"}}})
    registry = register_builtin_steps(StepRegistry())
    handler = registry.get("rebase.v3_module_rebase").handler

    def ctx_for(item):
        return StepContext(
            settings=settings, params={}, run_dir=run_dir, trace=trace,
            item=item,
            state={"task_spec": {"repo": "vllm-omni", "params": {}},
                   "run_id": "run-int", "repo_path": str(repo)})

    # resume: the interrupted module runs again, the done one does not
    r = asyncio.run(handler(ctx_for("worker_runner")))
    assert r.ok and called == ["worker_runner"]
    assert r.outputs["state_updates"]["module_worker_runner_status"] == "done"
    r = asyncio.run(handler(ctx_for("model_executor")))
    assert r.ok and "short-circuit" in r.summary and called == ["worker_runner"]
    # an attempt interrupted mid-flight blocks the step (resumable)
    sub.update({"modules": {"worker_runner": {"status": "interrupted"}}})
    r = asyncio.run(handler(ctx_for("worker_runner")))
    assert not r.ok and r.failure is FailureKind.BLOCKED
    assert "SIGTERM" in r.summary and "--resume" in r.summary
    assert r.outputs["state_updates"]["module_worker_runner_status"] == "interrupted"


@pytest.mark.parametrize("contents, expected", [
    ("Suggestion...Passed\n- hook id: suggestion\n", None),
    (("Migration...(no files to check)Skipped\n"
      "- hook id: migration\n"), None),
    (("Mypy...Failed\n- hook id: mypy\nSuggestion...Passed\n"
      "- hook id: suggestion\n"), frozenset({"mypy"})),
    ("Mypy...\x1b[41mFailed\x1b[0m\n- hook id: mypy\n",
     frozenset({"mypy"})),
    (("Unknown...Failed\nSuggestion...Passed\n"
      "- hook id: suggestion\n"), None),
    ("Unknown...Failed\nMypy...Failed\n- hook id: mypy\n", None),
])
def test_precommit_failed_hooks_requires_failed_status(tmp_path, contents,
                                                       expected):
    from infermatrix_copilot.engine.steps.rebase_v3 import _precommit_failed_hooks

    log = tmp_path / "precommit.log"
    log.write_text(contents)
    assert _precommit_failed_hooks(
        SimpleNamespace(log_file=str(log))) == expected


@pytest.mark.parametrize("retry_once, results, expected", [
    (True, [(1, False, "spdx"), (1, False, "ruff")],
     (1, frozenset({"ruff"}))),
    (True, [(1, False, "spdx"), (0, False, None)], (0, frozenset())),
    (False, [(1, False, "spdx")], (1, frozenset({"spdx"}))),
    (True, [(0, False, None)], (0, frozenset())),
    (True, [(1, True, "spdx")], None),
    (True, [(1, False, "spdx"), (1, True, "ruff")], None),
])
def test_precommit_baseline_retry_parity(
        v3_env, settings, trace, monkeypatch, retry_once, results, expected):
    """Auto-fix baseline retries use the final verdict; timeouts prove nothing."""
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps.rebase_v3 import _precommit_baseline
    from infermatrix_copilot.testing import runner as runner_mod

    _, _, repo, run_dir = v3_env
    calls = []

    def run(self, job, env):
        rc, timed_out, hook = results[len(calls)]
        calls.append((self.repo_root, job.command, job.timeout_sec))
        log = run_dir / f"baseline-{len(calls)}.log"
        log.write_text(f"Hook...Failed\n- hook id: {hook}\n" if hook else "")
        return runner_mod.TestOutcome(rc=rc, timed_out=timed_out,
                                      log_file=str(log))

    monkeypatch.setattr(runner_mod.TestRunner, "run", run)
    ctx = StepContext(settings=settings, params={}, run_dir=run_dir,
                      trace=trace, state={})
    pc = {"retry_once": retry_once, "timeout_sec": 123}
    result = _precommit_baseline(ctx, str(repo), pc, "pre-commit run -a", {})
    assert result == expected
    wt = run_dir / "precommit_baseline_wt"
    assert calls == [(wt, "pre-commit run -a", 123)] * len(results)
    assert not wt.exists()


def test_v3_precommit_step(v3_env, settings, trace, monkeypatch):
    """Phase 3.2: the adapter-declared precommit runs after the local loop,
    retries once (auto-fix hooks), and its substate verdict feeds the push
    gate; an undeclared precommit is recorded, never invented."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.testing import runner as runner_mod
    _, _, repo, run_dir = v3_env
    rcs = [1, 0]                    # first run auto-fixes, retry passes
    ran = []

    def fake_run(self, job, env, *, baseline=False, dry_run=False):
        ran.append(job.command)
        return runner_mod.TestOutcome(rc=rcs[len(ran) - 1])
    monkeypatch.setattr(runner_mod.TestRunner, "run", fake_run)
    registry = register_builtin_steps(StepRegistry())

    def ctx_for(run_id):
        rd = run_dir.parent / f"dir-{run_id}"
        rd.mkdir(exist_ok=True)
        return StepContext(
            settings=settings, params={}, run_dir=rd, trace=trace,
            state={"task_spec": {"repo": "vllm-omni", "params": {}},
                   "run_id": run_id, "repo_path": str(repo)})

    r = asyncio.run(registry.get("rebase.v3_precommit").handler(
        ctx_for("run-pc")))
    assert r.ok and "passed" in r.summary
    assert len(ran) == 2 and all("pre-commit" in c for c in ran)
    data = Substate(run_dir.parent / "dir-run-pc", "run-pc").read()
    pc = data["tests"]["precommit"]
    assert pc["result"] == "passed" and pc["attempt"] == 1
    assert evaluate_push_gate(data, {}).allowed

    # both attempts red, baseline (3rd and 4th runs) ALSO red on SAME hooks:
    # the debt pre-exists the run - recorded distinctly, gate FLAGS
    # instead of blocking
    def hook_outcome(name, rc, hooks, timed_out=False):
        log = run_dir.parent / f"{name}.log"
        log.write_text("".join(
            f"some hook...Failed\n- hook id: {h}\n" for h in hooks))
        return runner_mod.TestOutcome(rc=rc, timed_out=timed_out,
                                      log_file=str(log))

    scripted = []

    def scripted_run(self, job, env, *, baseline=False, dry_run=False):
        ran.append(job.command)
        return scripted[len(ran) - 1]

    monkeypatch.setattr(runner_mod.TestRunner, "run", scripted_run)
    ran.clear()
    scripted[:] = [hook_outcome("cur1", 1, ["check-test-ci-coverage"]),
                   hook_outcome("cur2", 1, ["check-test-ci-coverage"]),
                   hook_outcome("base1", 1, ["check-test-ci-coverage"]),
                   hook_outcome("base1-retry", 1, ["check-test-ci-coverage"])]
    r = asyncio.run(registry.get("rebase.v3_precommit").handler(
        ctx_for("run-pc2")))
    assert r.ok and "PRE-EXISTS" in r.summary        # substate data, step ok
    assert len(ran) == 4                             # same retry for baseline
    data = Substate(run_dir.parent / "dir-run-pc2", "run-pc2").read()
    assert data["tests"]["precommit"]["result"] == "failed_preexisting"
    d = evaluate_push_gate(data, {})
    assert d.allowed and any("pre-exists" in f for f in d.flagged)
    # strict gate still makes the pre-existing red blocking
    assert not evaluate_push_gate(data, {"strict_push_gate": True}).allowed

    # a NEW failure riding on old debt is NOT pre-existing: the current
    # failed-hook set must be a SUBSET of the baseline's
    ran.clear()
    scripted[:] = [hook_outcome("cur3", 1, ["check-test-ci-coverage",
                                            "ruff"]),
                   hook_outcome("cur4", 1, ["check-test-ci-coverage",
                                            "ruff"]),
                   hook_outcome("base2", 1, ["check-test-ci-coverage"]),
                   hook_outcome("base2-retry", 1, ["check-test-ci-coverage"])]
    r = asyncio.run(registry.get("rebase.v3_precommit").handler(
        ctx_for("run-pc-new")))
    assert r.ok and "FAILED" in r.summary
    data = Substate(run_dir.parent / "dir-run-pc-new", "run-pc-new").read()
    assert data["tests"]["precommit"]["result"] == "failed"
    assert not evaluate_push_gate(data, {}).allowed

    # Verbose passing hooks print their IDs too. A hook that passed in the
    # baseline but fails now must still block, even alongside existing debt.
    ran.clear()
    baseline_verbose = hook_outcome(
        "base-verbose", 1, ["check-test-ci-coverage"])
    with Path(baseline_verbose.log_file).open("a") as stream:
        stream.write("Suggestion...Passed\n- hook id: suggestion\n")
    scripted[:] = [hook_outcome("cur-verbose1", 1,
                                ["check-test-ci-coverage", "suggestion"]),
                   hook_outcome("cur-verbose2", 1,
                                ["check-test-ci-coverage", "suggestion"]),
                   baseline_verbose, baseline_verbose]
    r = asyncio.run(registry.get("rebase.v3_precommit").handler(
        ctx_for("run-pc-verbose")))
    assert r.ok and "FAILED" in r.summary
    data = Substate(run_dir.parent / "dir-run-pc-verbose",
                    "run-pc-verbose").read()
    assert data["tests"]["precommit"]["result"] == "failed"
    assert not evaluate_push_gate(data, {}).allowed

    # unparseable logs (no hook ids recorded) stay structural
    ran.clear()
    scripted[:] = [runner_mod.TestOutcome(rc=1),
                   runner_mod.TestOutcome(rc=1),
                   hook_outcome("base3", 1, ["check-test-ci-coverage"]),
                   hook_outcome("base3-retry", 1, ["check-test-ci-coverage"])]
    r = asyncio.run(registry.get("rebase.v3_precommit").handler(
        ctx_for("run-pc-nolog")))
    data = Substate(run_dir.parent / "dir-run-pc-nolog",
                    "run-pc-nolog").read()
    assert data["tests"]["precommit"]["result"] == "failed"
    assert not evaluate_push_gate(data, {}).allowed
    monkeypatch.setattr(runner_mod.TestRunner, "run", fake_run)

    # both attempts red, baseline probe TIMED OUT: proves nothing -
    # unavailable baseline keeps the red STRUCTURAL (fail closed)
    ran.clear()
    scripted[:] = [hook_outcome("cur5", 1, ["check-test-ci-coverage"]),
                   hook_outcome("cur6", 1, ["check-test-ci-coverage"]),
                   hook_outcome("base4", 1, ["check-test-ci-coverage"],
                                timed_out=True)]
    monkeypatch.setattr(runner_mod.TestRunner, "run", scripted_run)
    r = asyncio.run(registry.get("rebase.v3_precommit").handler(
        ctx_for("run-pc-to")))
    assert r.ok and "FAILED" in r.summary
    data = Substate(run_dir.parent / "dir-run-pc-to", "run-pc-to").read()
    assert data["tests"]["precommit"]["result"] == "failed"
    assert data["tests"]["precommit"]["baseline_rc"] is None
    assert not evaluate_push_gate(data, {}).allowed
    monkeypatch.setattr(runner_mod.TestRunner, "run", fake_run)

    # both attempts red, baseline GREEN: the run introduced it - structural
    rcs[:] = [1, 1, 0]
    ran.clear()
    r = asyncio.run(registry.get("rebase.v3_precommit").handler(
        ctx_for("run-pc3")))
    assert r.ok and "FAILED" in r.summary
    data = Substate(run_dir.parent / "dir-run-pc3", "run-pc3").read()
    assert data["tests"]["precommit"]["result"] == "failed"
    assert data["tests"]["precommit"]["baseline_rc"] == 0
    d = evaluate_push_gate(data, {})
    assert not d.allowed and any("precommit red" in x for x in d.reasons)


def test_v3_ci_push_carries_the_settings_github_token(
        v3_env, settings, trace, monkeypatch):
    """The push transport disables credential helpers BY DESIGN (header
    auth only, never the shared credential store), so the ci step MUST
    hand Settings.github_token to commit_and_push - the live remote_ci
    launch (2026-08-23) pushed tokenless and failed against the org
    remote while a manual credential-helper push succeeded."""
    import subprocess
    from types import SimpleNamespace
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.engine.steps import rebase_v3
    from infermatrix_copilot.rebase_engine import ci_loop, push_to_ci
    _, _, repo, run_dir = v3_env
    # the step pushes ONLY from the adapter-declared rebase branch
    subprocess.run(["git", "checkout", "-qb", "dev/vllm-align"], cwd=repo,
                   check=True)
    monkeypatch.setattr(settings, "buildkite_api_token", "bk-tok",
                        raising=False)
    monkeypatch.setattr(settings, "github_token", "gh-push-tok",
                        raising=False)
    captured = {}

    def fake_commit_and_push(*args, **kwargs):
        captured.update(kwargs)
        return push_to_ci.PushOutcome(True, pushed_commit="deadbeef")

    async def fake_rounds(**kwargs):
        kwargs["push_fn"](0)
        return SimpleNamespace(result="failed", reason="stub",
                               fixed_jobs=[], unfixed_jobs=[], rounds=[])

    monkeypatch.setattr(push_to_ci, "commit_and_push", fake_commit_and_push)
    monkeypatch.setattr(ci_loop, "run_ci_rounds", fake_rounds)
    registry = register_builtin_steps(StepRegistry())
    rd = run_dir.parent / "dir-ci-token"
    rd.mkdir(exist_ok=True)
    ctx = StepContext(
        settings=settings, params={}, run_dir=rd, trace=trace,
        state={"task_spec": {"repo": "vllm-omni",
                             "params": {"rebase_mode": "remote_ci",
                                        "upstream_commit": "f" * 40}},
               "run_id": "run-ci-token", "repo_path": str(repo),
               "upstream_commit": "f" * 40})
    r = asyncio.run(registry.get("rebase.v3_ci").handler(ctx))
    assert captured, f"push_fn never reached commit_and_push: {r.summary}"
    assert captured["token"] == "gh-push-tok"


def test_v3_ci_debug_rejects_weakened_assertion(
        v3_agent_env, settings, trace, monkeypatch):
    """The CI-debug acceptance boundary restores an oracle-weakening patch,
    even when the target test cannot be reproduced on the local host."""
    import subprocess
    from types import SimpleNamespace
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.engine.steps import rebase_v3
    from infermatrix_copilot.rebase_engine import ci_loop
    _, _, repo, run_dir = v3_agent_env
    subprocess.run(["git", "checkout", "-qb", "dev/vllm-align"],
                   cwd=repo, check=True)
    helper = repo / "tests" / "audio_helpers.py"
    helper.write_text("def check(score):\n    assert score > 0.8\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "add test helper"], cwd=repo,
                   check=True)
    original = helper.read_text()
    monkeypatch.setattr(settings, "buildkite_api_token", "bk",
                        raising=False)
    monkeypatch.setattr(
        rebase_v3, "_tier_client",
        lambda ctx: (object(), SimpleNamespace(
            model="m", api_key="k", base_url="", source="global")))

    async def fake_agent(*args, **kwargs):
        helper.write_text("def check(score):\n    assert score > 0.7\n")
        return "done"

    accepted = []

    async def fake_rounds(**kwargs):
        accepted.append(await kwargs["debug_fn"](SimpleNamespace(
            name="Quick", log_file="", state="failed", exit_status=1)))
        return SimpleNamespace(result="failed", reason="stub",
                               fixed_jobs=[], unfixed_jobs=["Quick"],
                               rounds=[])

    monkeypatch.setattr(rebase_v3, "_run_debug_agent", fake_agent)
    monkeypatch.setattr(ci_loop, "run_ci_rounds", fake_rounds)
    registry = register_builtin_steps(StepRegistry())
    rd = run_dir.parent / "dir-ci-policy"
    rd.mkdir(exist_ok=True)
    ctx = StepContext(
        settings=settings, params={}, run_dir=rd, trace=trace,
        state={"task_spec": {"repo": "vllm-omni",
                             "params": {"rebase_mode": "remote_ci",
                                        "upstream_commit": "f" * 40}},
               "run_id": "run-ci-policy", "repo_path": str(repo),
               "upstream_commit": "f" * 40})
    result = asyncio.run(registry.get("rebase.v3_ci").handler(ctx))
    assert result.ok and accepted == [False]
    assert helper.read_text() == original
    rejected = list(trace.events("ci_debug_policy_rejected"))
    assert rejected and rejected[-1]["paths"] == ["tests/audio_helpers.py"]


def test_v3_ci_arms_adoption_exemption_only_on_proven_remote_noop(
        v3_env, settings, trace, monkeypatch, tmp_path):
    """The pre-push adoption exemption may only arm when the push is a
    PROVEN ref no-op: remote branch ref == local head. A clean tree with
    an advanced remote must pass head_commit="" (exemption off) - a
    lease-authorized push there would move the ref and could cancel the
    very build the exemption spares (gate review 2026-08-24)."""
    import subprocess
    from types import SimpleNamespace
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.rebase_engine import ci_loop, push_to_ci
    _, _, repo, run_dir = v3_env

    def git(*args, cwd=repo):
        subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True)

    git("checkout", "-qb", "dev/vllm-align")
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)],
                   check=True, capture_output=True)
    git("remote", "add", "origin", str(remote))
    git("push", "-q", "origin", "dev/vllm-align")

    monkeypatch.setattr(settings, "buildkite_api_token", "bk",
                        raising=False)
    monkeypatch.setattr(settings, "github_token", "gh", raising=False)
    monkeypatch.setattr(
        push_to_ci, "commit_and_push",
        lambda *a, **k: push_to_ci.PushOutcome(True,
                                               pushed_commit="d" * 40))
    captured = {}

    async def fake_rounds(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(result="failed", reason="stub",
                               fixed_jobs=[], unfixed_jobs=[], rounds=[])

    monkeypatch.setattr(ci_loop, "run_ci_rounds", fake_rounds)
    registry = register_builtin_steps(StepRegistry())

    def run_step(name):
        rd = run_dir.parent / name
        rd.mkdir(exist_ok=True)
        ctx = StepContext(
            settings=settings, params={}, run_dir=rd, trace=trace,
            state={"task_spec": {"repo": "vllm-omni",
                                 "params": {"rebase_mode": "remote_ci",
                                            "upstream_commit": "f" * 40}},
                   "run_id": name, "repo_path": str(repo),
                   "upstream_commit": "f" * 40})
        return asyncio.run(registry.get("rebase.v3_ci").handler(ctx))

    # remote == local head: the exemption arms with the proven head
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    run_step("dir-ci-noop")
    assert captured["head_commit"] == head

    # remote ADVANCES past the local head (clean local tree): off
    clone = tmp_path / "advancer"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)],
                   check=True, capture_output=True)
    # the bare remote's HEAD names no default branch; land on the real one
    git("checkout", "-q", "dev/vllm-align", cwd=clone)
    (clone / "advance.txt").write_text("x")
    git("add", "-A", cwd=clone)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "advance"], cwd=clone, check=True,
                   capture_output=True)
    git("push", "-q", "origin", "HEAD:dev/vllm-align", cwd=clone)
    captured.clear()
    # the first in-process handler call still holds the checkout flock
    # (no executor lifecycle here); a fresh lock inode flocks independently
    import shutil
    shutil.rmtree(repo / "locks", ignore_errors=True)
    r2 = run_step("dir-ci-diverged")
    assert captured, f"step never reached rounds: {r2.summary}"
    assert captured["head_commit"] == ""


def test_v3_precommit_not_declared(v3_env, settings, trace):
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    _, _, repo, run_dir = v3_env
    adir = Path(settings.adapters_dir) / "vllm_omni"
    manifest = yaml.safe_load((adir / "manifest.yaml").read_text())
    manifest["rebase"].pop("precommit", None)
    (adir / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    registry = register_builtin_steps(StepRegistry())
    ctx = StepContext(
        settings=settings, params={}, run_dir=run_dir, trace=trace,
        state={"task_spec": {"repo": "vllm-omni", "params": {}},
               "run_id": "run-nd", "repo_path": str(repo)})
    r = asyncio.run(registry.get("rebase.v3_precommit").handler(ctx))
    assert r.ok and "not declared" in r.summary
    data = Substate(run_dir, "run-nd").read()
    assert data["tests"]["precommit"]["result"] == "not_declared"
    assert evaluate_push_gate(data, {}).allowed      # recorded, not red
    assert any(e for e in trace.events("capability_gap"))


def test_v3_halt_on_phase3_failures(v3_env, settings, trace, monkeypatch,
                                    tmp_path):
    """The declared safety param actually halts: with phase-3 failures in
    substate and halt_on_phase3_failures=true, the precommit step (end of
    phase 3 — precommit still runs first) ESCALATEs instead of proceeding
    toward push/CI."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import FailureKind, StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.testing import runner as runner_mod
    _, _, repo, _ = v3_env
    monkeypatch.setattr(
        runner_mod.TestRunner, "run",
        lambda self, job, env, *, baseline=False, dry_run=False:
        runner_mod.TestOutcome(rc=0))
    registry = register_builtin_steps(StepRegistry())

    def run_precommit(params, run_id):
        rd = tmp_path / f"halt-{run_id}"
        rd.mkdir()
        Substate(rd, run_id).update(
            {"tests": {"pipeline": {"failed_tests": ["t1"]}}})
        ctx = StepContext(
            settings=settings, params={}, run_dir=rd, trace=trace,
            state={"task_spec": {"repo": "vllm-omni", "params": params},
                   "run_id": run_id, "repo_path": str(repo)})
        return asyncio.run(registry.get("rebase.v3_precommit").handler(ctx))

    r = run_precommit({"halt_on_phase3_failures": True}, "h1")
    assert not r.ok and r.failure is FailureKind.ESCALATE
    assert "halting before any push/CI" in r.summary
    # default: failures pass through to the push gate's ruling
    assert run_precommit({}, "h2").ok


def test_v3_mutating_step_reacquires_locks_on_resume(v3_env, settings,
                                                     trace, monkeypatch,
                                                     tmp_path):
    """A --resume replays the checkpointed prelude WITHOUT executing it —
    the resumed mutating steps must (re)acquire the checkout flocks
    themselves, and the lifecycle finalizer still releases them."""
    from infermatrix_copilot.engine import lifecycle
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.rebase_engine.runctx import CheckoutLock
    from infermatrix_copilot.testing import runner as runner_mod
    _, _, repo, _ = v3_env
    monkeypatch.setattr(
        runner_mod.TestRunner, "run",
        lambda self, job, env, *, baseline=False, dry_run=False:
        runner_mod.TestOutcome(rc=0))
    registry = register_builtin_steps(StepRegistry())
    rd = tmp_path / "resumed-run"
    rd.mkdir()
    # resumed world: mode already resolved into params, NO prelude executed
    ctx = StepContext(
        settings=settings, params={}, run_dir=rd, trace=trace,
        state={"task_spec": {"repo": "vllm-omni",
                             "params": {"rebase_mode": "local_ci"}},
               "run_id": "resumed-run", "repo_path": str(repo)})
    r = asyncio.run(registry.get("rebase.v3_precommit").handler(ctx))
    assert r.ok, r.summary
    probe = CheckoutLock(repo, "omni")
    assert probe.acquire(blocking=False) is False    # step took the lock
    asyncio.run(lifecycle.finalize(rd, None))
    assert probe.acquire(blocking=False) is True
    probe.release()


def test_v3_debug_reject_restores_worktree(v3_agent_env, settings, trace,
                                           monkeypatch):
    """A debug attempt whose verification re-run stays red must NOT leave
    its edits in the tree — assertion failures pass the push gate by
    default, so an unverified patch would eventually be committed and
    pushed. Snapshot before dispatch, restore on rejection."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3, \
        register_builtin_steps
    from infermatrix_copilot.rebase_engine import agent_loop as al
    from infermatrix_copilot.rebase_engine import test_loop as tl_mod
    from infermatrix_copilot.testing import runner as runner_mod
    _, _, repo, run_dir = v3_agent_env
    monkeypatch.setattr(
        rebase_v3, "_tier_client",
        lambda ctx: (object(), SimpleNamespace(
            model="m", api_key="k", base_url="", source="global")))
    tracked = repo / ".buildkite" / "cuda" / "test-merge.yml"
    original = tracked.read_text()

    async def fake_loop(client, prompt, **kw):
        # the "agent" edits a tracked file and creates AND STAGES a new one
        # (the staged case broke the parent's bulk checkout restore)
        tracked.write_text(original + "\n# unverified debug patch\n")
        (repo / "stray_debug_artifact.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "stray_debug_artifact.py"],
                       cwd=repo, check=True)
        return {"done": True, "text": "patched", "turns": 1}
    monkeypatch.setattr(al, "run_agent_loop", fake_loop)
    monkeypatch.setattr(
        runner_mod.TestRunner, "run",
        lambda self, job, env, *, baseline=False, dry_run=False:
        runner_mod.TestOutcome(rc=0 if baseline else 1))   # rerun stays red
    monkeypatch.setattr(tl_mod, "ensure_main_worktree",
                        lambda repo, path, base_ref="origin/main":
                        run_dir / "wt")
    monkeypatch.setattr(tl_mod, "remove_main_worktree",
                        lambda repo, path: None)
    registry = register_builtin_steps(StepRegistry())
    ctx = StepContext(
        settings=settings, params={}, run_dir=run_dir, trace=trace,
        state={"task_spec": {"repo": "vllm-omni", "params": {}},
               "run_id": "run-restore", "repo_path": str(repo)})
    r = asyncio.run(registry.get("rebase.v3_test_loop").handler(ctx))
    assert r.ok, r.summary
    data = Substate(run_dir, "run-restore").read()
    assert data["tests"]["pipeline"]["failed_tests"] == ["quick"]
    # the rejected patch is GONE: tracked file reverted, stray file removed
    assert tracked.read_text() == original
    assert not (repo / "stray_debug_artifact.py").exists()


@pytest.mark.parametrize("mode", [
    "oracle", "product", "test_fixture", "capture_error", "evaluate_error",
])
def test_v3_local_debug_preserves_oracles(v3_env, settings, trace,
                                         monkeypatch, mode):
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3, register_builtin_steps
    from infermatrix_copilot.rebase_engine import test_loop as tl_mod
    from infermatrix_copilot.testing import runner as runner_mod

    _, _, repo, run_dir = v3_env
    test_file = repo / "tests" / "test_value.py"
    product_file = repo / "engine.py"
    original = "value = 0\ndef test_value():\n    assert value == 1\n"
    test_file.write_text(original)
    product_file.write_text("value = 0\n")
    subprocess.run(["git", "add", "tests/test_value.py", "engine.py"],
                   cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "fixtures"], cwd=repo, check=True)
    monkeypatch.setattr(rebase_v3, "_tier_client", lambda ctx: object())
    dispatched = []

    async def fake_debug(*args):
        dispatched.append(mode)
        if mode == "oracle":
            test_file.write_text(original.replace("assert value == 1",
                                                   "assert True"))
        elif mode == "test_fixture":
            test_file.write_text(original.replace("value = 0", "value = 1"))
        else:
            product_file.write_text("value = 1\n")
        return "done"

    monkeypatch.setattr(rebase_v3, "_run_debug_agent", fake_debug)
    if mode in ("capture_error", "evaluate_error"):
        def unavailable(*args, **kwargs):
            raise RuntimeError("policy unavailable")
        name = "capture_patch_policy" if mode == "capture_error" \
            else "evaluate_debug_patch"
        monkeypatch.setattr(rebase_v3, name, unavailable)
    runs = []

    def fake_run(self, job, env, *, baseline=False, dry_run=False):
        if baseline:
            return runner_mod.TestOutcome(rc=0)
        runs.append(job.key)
        if len(runs) == 1:
            return runner_mod.TestOutcome(rc=1)
        marker = runner_mod.pass_marker_for(self.tests_dir, job)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return runner_mod.TestOutcome(rc=0)

    monkeypatch.setattr(runner_mod.TestRunner, "run", fake_run)
    monkeypatch.setattr(tl_mod, "ensure_main_worktree",
                        lambda repo, path, base_ref="origin/main": run_dir / "wt")
    monkeypatch.setattr(tl_mod, "remove_main_worktree", lambda repo, path: None)
    ctx = StepContext(
        settings=settings, params={}, run_dir=run_dir, trace=trace,
        state={"task_spec": {"repo": "vllm-omni", "params": {}},
               "run_id": "local-policy", "repo_path": str(repo)})
    registry = register_builtin_steps(StepRegistry())
    result = asyncio.run(registry.get("rebase.v3_test_loop").handler(ctx))
    assert result.ok
    data = Substate(run_dir, "local-policy").read()
    accepted = mode in ("product", "test_fixture")
    assert data["tests"]["pipeline"]["passed"] == int(accepted)
    assert bool(data["tests"]["infra_failures"]) == mode.endswith("error")
    assert (run_dir / "tests" / ".passed_quick").exists() == accepted
    if not accepted:
        assert test_file.read_text() == original
        assert product_file.read_text() == "value = 0\n"
    if mode == "oracle":
        assert data["tests"]["pipeline"]["failed_tests"] == ["quick"]
    if mode == "capture_error":
        assert dispatched == []


@pytest.mark.parametrize("operation", ["search", "read"])
def test_v3_backend_knowledge_uses_configured_root_and_repo_scope(
        settings, trace, tmp_path, operation):
    from infermatrix_copilot.engine.steps.rebase_v3 import build_backends

    root = tmp_path / "configured-knowledge"
    for relative in ["general/shared.md", "repos/vllm-omni/owned.md",
                     "repos/other/private.md"]:
        page = root / relative
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("# needle\n" + relative)
    settings.knowledge_dir = root
    settings.memory_db = tmp_path / "memory.db"
    backends = build_backends(
        settings=settings, state={"task_spec": {"repo": "vllm-omni"}},
        run_dir=tmp_path, trace=trace,
        manifest={"knowledge": {"repo_subdir": "repos/vllm-omni"}},
        repo=str(tmp_path / "checkout"), model="test-model")

    if operation == "search":
        result = backends.doc_search(query="needle")
        assert "error" not in result, result
        assert {hit["path"] for hit in result["matches"]} == {
            "general/shared.md", "repos/vllm-omni/owned.md"}
    else:
        result = backends.doc_read(path="repos/vllm-omni/owned.md")
        assert "error" not in result, result
        assert "repos/vllm-omni/owned.md" in result["content"]
        refused = backends.doc_read(path="repos/other/private.md")
        assert "outside the selected knowledge slices" in refused["error"]


def test_v3_backends_are_production(v3_env, settings, trace, tmp_path,
                                    monkeypatch):
    """The module/debug agents get REAL backends: plan review runs on the
    resolved tier backend and writes review files; pytest executes in the
    TARGET env; the knowledge tools hit the copilot stores (agents may
    only PROPOSE skills). None of the `_unwired` defaults remain."""
    import anthropic
    from infermatrix_copilot.engine.steps.rebase_v3 import _build_backends
    from infermatrix_copilot.testing import runner as runner_mod
    _, _, repo, run_dir = v3_env
    settings.memory_db = tmp_path / "mem.db"
    manifest = yaml.safe_load(
        (Path(settings.adapters_dir) / "vllm_omni" / "manifest.yaml")
        .read_text())
    ctx = SimpleNamespace(settings=settings, trace=trace, run_dir=run_dir,
                          state={"task_spec": {"repo": "vllm-omni"},
                                 "run_id": "run-b"})
    target = SimpleNamespace(model="m-test", api_key="k", base_url="",
                             source="tier:eco")
    backends = _build_backends(ctx, manifest, str(repo), target)

    # plan review: a real reviewer call, verdict files written
    class FakeMessages:
        def create(self, **kw):
            assert kw["model"] == "m-test"
            return SimpleNamespace(content=[SimpleNamespace(
                text='{"verdict": "approve", "summary": "ok", '
                     '"concerns": []}')])

    class FakeAnthropic:
        def __init__(self, **kw):
            self.messages = FakeMessages()
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    plan_dir = run_dir / "plans" / "module-x"
    plan_dir.mkdir(parents=True)
    (plan_dir / "p.json").write_text('{"plan": "do it"}')
    (plan_dir / "p.md").write_text("# plan")
    out = backends.request_plan_review(plan_json_path=str(plan_dir / "p.json"))
    assert out["verdict"] == "approve"
    assert (plan_dir / "p.review.json").exists()
    # ...and a missing plan is an error, not a crash
    assert "error" in backends.request_plan_review(
        plan_json_path=str(plan_dir / "nope.json"))

    # pytest: runs through the runner in the TARGET env
    envs, cmds = [], []

    def fake_run(self, job, env, *, baseline=False, dry_run=False):
        envs.append(dict(env))
        cmds.append(job.command)
        return runner_mod.TestOutcome(rc=0)
    monkeypatch.setattr(runner_mod.TestRunner, "run", fake_run)
    out = backends.run_pytest(test_paths=["tests/x.py"])
    assert out["passed"] is True
    assert envs[0]["VIRTUAL_ENV"] == str(tmp_path / "omni-venv")
    assert "error" in backends.run_pytest()          # no paths

    # precommit: a file-scoped call DROPS --all-files (mutually exclusive)
    out = backends.run_precommit(files=["a.py", "b.py"])
    assert out["passed"] is True
    assert "--all-files" not in cmds[-1]
    assert "--files a.py b.py" in cmds[-1]
    out = backends.run_precommit()
    assert "--all-files" in cmds[-1]                 # unscoped keeps it

    # debug memory: real store roundtrip
    out = backends.record_debug_memory(
        module="worker", key="k1", symptom="boom", root_cause="rc",
        fix="patch", files="a.py,b.py")
    assert out.get("ok"), out
    found = backends.search_debug_memory(keyword="boom")
    assert found["results"] and found["results"][0]["module"] == "worker"

    # skills: propose-only governance — a candidate in the RUNTIME state
    # dir; the checked-in adapter tree stays untouched (read-only at run
    # time, Rev 8 §1.1 seed/runtime split)
    out = backends.skill_manage(action="create", name="new-trick",
                                description="d", body="b")
    assert out.get("ok") and "CANDIDATE" in out["note"]
    runtime_dir = Path(settings.memory_db).parent / "state" / "vllm-omni" \
        / "skills_runtime"
    assert (runtime_dir / "_candidates.json").exists()
    adapter_skills = Path(settings.adapters_dir) / "vllm_omni" / "skills"
    assert not (adapter_skills / "_candidates.json").exists()
    assert not (runtime_dir / "new-trick" / "SKILL.md").exists()
    assert "error" in backends.skill_manage(action="delete", name="x")

    # retrieval is a REAL seed UNION runtime: a full seed page can never
    # starve distinct runtime skills out of the result
    def write_skill(root, name, desc):
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n"
            "status: active\n---\nbody\n")
    write_skill(adapter_skills, "seed-one", "rebase widget seed")
    write_skill(adapter_skills, "seed-two", "rebase widget seed")
    write_skill(runtime_dir, "runtime-one", "rebase widget learned")
    names = {s["name"] for s in backends.search_skills(
        keyword="rebase widget", max_results=3)["skills"]}
    assert "runtime-one" in names and "seed-one" in names
    # k=1: the learned (runtime) skill is never crowded out by seed
    top = backends.search_skills(keyword="rebase widget",
                                 max_results=1)["skills"]
    assert top[0]["name"] == "runtime-one"


def test_v3_wheel_failed_install_never_pins(v3_env, settings, trace,
                                             tmp_path, monkeypatch):
    """A failed install must leave the guard-checked target tree untouched:
    the pin is the step's only tree mutation and must come AFTER the
    install. Pinning first stranded a dirty Dockerfile on install failure
    and the next run's guard refused to start (run-20260825-095137)."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3, \
        register_builtin_steps
    from infermatrix_copilot.rebase_engine.wheel import WheelInstallError
    _, _, repo, run_dir = v3_env
    upstream = tmp_path / "up"
    upstream.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=upstream, check=True)
    (upstream / "s.txt").write_text("s")
    subprocess.run(["git", "add", "-A"], cwd=upstream, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "s"], cwd=upstream, check=True)
    pinned = []
    monkeypatch.setattr(rebase_v3.wheel_mod, "pick_wheel_commit",
                        lambda *a, **k: "f" * 40)
    monkeypatch.setattr(rebase_v3.wheel_mod, "pin_dockerfile",
                        lambda *a, **k: pinned.append(a) or True)
    monkeypatch.setattr(rebase_v3.wheel_mod, "make_arch_probe",
                        lambda spec: None)

    def failing_install(*a, **k):
        raise WheelInstallError("wheel install failed for commit ffffff")
    monkeypatch.setattr(rebase_v3.wheel_mod, "ensure_wheel_installed",
                        failing_install)
    registry = register_builtin_steps(StepRegistry())
    rd = tmp_path / "wheel-fail"
    rd.mkdir()
    ctx = StepContext(
        settings=settings, params={}, run_dir=rd, trace=trace,
        state={"task_spec": {"repo": "vllm-omni",
                             "params": {"rebase_mode": "full"}},
               "run_id": "wheel-fail", "repo_path": str(repo),
               "upstream_origin_path": str(upstream),
               "last_rebase_upstream_commit": "d" * 40})
    r = asyncio.run(registry.get("rebase.v3_wheel").handler(ctx))
    assert not r.ok and "wheel install failed" in r.summary
    assert pinned == []          # tree untouched on the failure path
    asyncio.run(_finalize_run(rd))


def test_v3_wheel_installs_into_target_venv(v3_env, settings, trace,
                                            tmp_path, monkeypatch):
    """The wheel step ends with the package INSTALLED at the picked commit
    in the TARGET venv (ensure_wheel_installed wired); an unconfigured
    venv is BLOCKED, never a silent install into the copilot's own env."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3, \
        register_builtin_steps
    _, _, repo, run_dir = v3_env
    upstream = tmp_path / "up"
    upstream.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=upstream, check=True)
    (upstream / "s.txt").write_text("s")
    subprocess.run(["git", "add", "-A"], cwd=upstream, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "s"], cwd=upstream, check=True)
    calls = {}
    monkeypatch.setattr(rebase_v3.wheel_mod, "pick_wheel_commit",
                        lambda *a, **k: "f" * 40)
    monkeypatch.setattr(rebase_v3.wheel_mod, "pin_dockerfile",
                        lambda *a, **k: True)
    monkeypatch.setattr(rebase_v3.wheel_mod, "make_arch_probe",
                        lambda spec: None)

    def fake_install(up, commit, spec, *, python, **kw):
        calls.update({"commit": commit, "python": python, "up": str(up)})
        return True
    monkeypatch.setattr(rebase_v3.wheel_mod, "ensure_wheel_installed",
                        fake_install)
    registry = register_builtin_steps(StepRegistry())

    def ctx_for(run_id):
        rd = tmp_path / f"wheel-{run_id}"
        rd.mkdir(exist_ok=True)
        return StepContext(
            settings=settings, params={}, run_dir=rd, trace=trace,
            state={"task_spec": {"repo": "vllm-omni",
                                 "params": {"rebase_mode": "full"}},
                   "run_id": run_id, "repo_path": str(repo),
                   "upstream_origin_path": str(upstream),
                   "last_rebase_upstream_commit": "d" * 40})

    r = asyncio.run(registry.get("rebase.v3_wheel").handler(ctx_for("w1")))
    assert r.ok, r.summary
    assert calls["commit"] == "f" * 40
    assert calls["python"] == str(tmp_path / "omni-venv" / "bin" / "python")
    # the install targets the SCRATCH clone, not the canonical upstream
    assert calls["up"].startswith(str(tmp_path / "wheel-w1"))
    asyncio.run(_finalize_run(tmp_path / "wheel-w1"))

    # unconfigured venv: BLOCKED
    monkeypatch.delenv("VLLM_OMNI_VENV")
    # "unconfigured" now means absent from the process env AND the .env
    # fallback (config.expansion_env) - isolate from the developer's file
    monkeypatch.setattr(type(settings), "expansion_env", lambda self: {})
    r = asyncio.run(registry.get("rebase.v3_wheel").handler(ctx_for("w2")))
    assert not r.ok and "venv" in r.summary
    asyncio.run(_finalize_run(tmp_path / "wheel-w2"))


def test_v3_guard_takes_locks_and_adapter_policy(v3_env, settings, trace,
                                                 tmp_path):
    """rebase.v3_guard is the first mutator: it takes the checkout flocks
    BEFORE delegating to the self-cleaning guard (resume-safe), and it
    injects the adapter's rebase.guard policy — the adapter's
    discard_untracked_patterns actually discard."""
    from infermatrix_copilot.engine import lifecycle
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.rebase_engine.runctx import CheckoutLock
    _, _, repo, _ = v3_env
    # an untracked artifact matching the adapter's discard pattern
    junk = repo / "tests" / "e2e" / "stage_configs" / "cfg_123456.yaml"
    junk.parent.mkdir(parents=True)
    junk.write_text("x")
    registry = register_builtin_steps(StepRegistry())
    rd = tmp_path / "guard-run"
    rd.mkdir()
    ctx = StepContext(
        settings=settings, params={}, run_dir=rd, trace=trace,
        state={"task_spec": {"repo": "vllm-omni",
                             "params": {"rebase_mode": "local_ci"}},
               "run_id": "guard-run", "repo_path": str(repo)})
    r = asyncio.run(registry.get("rebase.v3_guard").handler(ctx))
    assert r.ok, r.summary
    assert not junk.exists()                 # adapter policy applied
    probe = CheckoutLock(repo, "omni")
    assert probe.acquire(blocking=False) is False   # flock held FIRST
    asyncio.run(lifecycle.finalize(rd, None))
    assert probe.acquire(blocking=False) is True
    probe.release()


def test_v3_test_loop_requires_target_venv(v3_env, settings, trace,
                                           monkeypatch, tmp_path):
    """Without a configured target venv the local loop BLOCKS — raw
    manifest commands must never execute against the copilot's own env."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    _, _, repo, _ = v3_env
    monkeypatch.delenv("VLLM_OMNI_VENV")
    # "unconfigured" now means absent from the process env AND the .env
    # fallback (config.expansion_env) - isolate from the developer's file
    monkeypatch.setattr(type(settings), "expansion_env", lambda self: {})
    registry = register_builtin_steps(StepRegistry())
    rd = tmp_path / "novenv-run"
    rd.mkdir()
    ctx = StepContext(
        settings=settings, params={}, run_dir=rd, trace=trace,
        state={"task_spec": {"repo": "vllm-omni", "params": {}},
               "run_id": "novenv-run", "repo_path": str(repo)})
    r = asyncio.run(registry.get("rebase.v3_test_loop").handler(ctx))
    assert not r.ok and "venv" in r.summary


def test_v3_assign_syncs_paths_first(v3_env, settings, trace, tmp_path,
                                     monkeypatch):
    """Path sync runs BEFORE assignment: an upstream path that vanished
    (rename/refactor) is dropped from the module's map instead of silently
    yielding zero commits and a skippable module."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3, \
        register_builtin_steps
    _, _, repo, run_dir = v3_env
    adir = Path(settings.adapters_dir) / "vllm_omni"
    manifest = yaml.safe_load((adir / "manifest.yaml").read_text())
    manifest["modules"]["worker_runner"]["upstream_paths"] = [
        "vllm/v1/worker/", "vllm/vanished/"]
    (adir / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    upstream = tmp_path / "up"
    (upstream / "vllm" / "v1" / "worker").mkdir(parents=True)
    (upstream / "vllm" / "v1" / "worker" / "w.py").write_text("w")
    subprocess.run(["git", "init", "-q"], cwd=upstream, check=True)
    subprocess.run(["git", "add", "-A"], cwd=upstream, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "u"], cwd=upstream, check=True)
    captured = {}
    monkeypatch.setattr(
        rebase_v3, "run_commit_assignment",
        lambda cfg, paths, sub: captured.update(paths) or SimpleNamespace(
            total_commits=1, skip={m: True for m in paths}))
    registry = register_builtin_steps(StepRegistry())
    rd = tmp_path / "sync-run"
    rd.mkdir()
    ctx = StepContext(
        settings=settings, params={}, run_dir=rd, trace=trace,
        state={"task_spec": {"repo": "vllm-omni", "params": {}},
               "run_id": "sync-run", "repo_path": str(repo),
               "upstream_origin_path": str(upstream),
               "last_rebase_upstream_commit": "d" * 40})
    r = asyncio.run(registry.get("rebase.v3_assign").handler(ctx))
    assert r.ok, r.summary
    assert captured["worker_runner"] == ("vllm/v1/worker/",)
    assert any(e for e in trace.events("upstream_path_sync_dropped"))
    asyncio.run(_finalize_run(rd))


def test_v3_module_gets_live_test_plan(v3_agent_env, settings, trace,
                                       monkeypatch):
    """Module agents receive the LIVE test plan (authoritative CI
    obligations) — built from the manifest, passed as module_test_plan."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3, \
        register_builtin_steps
    from infermatrix_copilot.rebase_engine import module_rebase as mr
    _, _, repo, run_dir = v3_agent_env
    monkeypatch.setattr(
        rebase_v3, "_tier_client",
        lambda ctx: (object(), SimpleNamespace(
            model="m", api_key="k", base_url="", source="global")))
    plans = {}

    async def fake_rebase_module(module, **kw):
        plans[module] = kw.get("module_test_plan")
        return {"status": "done", "exit_code": 0, "debug_attempts": 0,
                "turns": 1, "summary": ""}
    monkeypatch.setattr(mr, "rebase_module", fake_rebase_module)
    registry = register_builtin_steps(StepRegistry())
    ctx = StepContext(
        settings=settings, params={}, run_dir=run_dir, trace=trace,
        item="worker_runner",
        state={"task_spec": {"repo": "vllm-omni", "params": {}},
               "run_id": "run-plan", "repo_path": str(repo)})
    r = asyncio.run(registry.get("rebase.v3_module_rebase").handler(ctx))
    assert r.ok, r.summary
    plan = plans["worker_runner"]
    assert plan is not None and set(plan) >= {"ci_tests",
                                              "upstream_changes"}
    # the scan artifact was produced as a side product for later modules
    assert (run_dir / "test_manifest.json").exists()


def test_v3_scratch_fetches_release_branch_from_canonical_remote(
        v3_env, settings, trace, tmp_path, monkeypatch):
    """Borrow local objects while fetching release refs from the real remote."""
    from infermatrix_copilot.engine import lifecycle
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3

    _, _, upstream, _ = v3_env
    def _git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()
    upstream_head = _git("-C", str(upstream), "rev-parse", "HEAD")
    _git("-C", str(upstream), "branch", "releases/v0.31.0", upstream_head)
    canonical = tmp_path / "canonical-clone"
    _git("clone", "--quiet", str(upstream), str(canonical))
    assert subprocess.run(
        ["git", "-C", str(canonical), "show-ref", "--verify",
         "refs/heads/releases/v0.31.0"], capture_output=True,
        check=False).returncode != 0
    rd = tmp_path / "release-scratch-run"
    rd.mkdir()
    monkeypatch.setattr(rebase_v3, "_SCRATCH_REGISTERED", set())
    ctx = StepContext(settings=settings, params={}, run_dir=rd, trace=trace,
                      state={"upstream_origin_path": str(canonical)})
    scratch = rebase_v3._ensure_upstream_scratch(ctx)
    assert isinstance(scratch, str)
    _git("-C", scratch, "fetch", "--quiet", "origin")
    assert _git("-C", scratch, "rev-parse",
                "origin/releases/v0.31.0") == upstream_head
    assert (Path(scratch) / ".git/objects/info/alternates").is_file()
    asyncio.run(lifecycle.finalize(rd, SimpleNamespace(status="done")))
    assert not Path(scratch).exists()


def test_v3_scratch_adoption_reregisters_teardown(v3_env, settings, trace,
                                                  tmp_path, monkeypatch):
    """A resumed process that ADOPTS a surviving scratch checkout must
    re-register its teardown — the disposable clone may not outlive the
    run just because the registering process crashed."""
    from infermatrix_copilot.engine import lifecycle
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3
    _, _, repo, _ = v3_env
    rd = tmp_path / "adopt-run"
    (rd / "upstream_scratch").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=rd / "upstream_scratch",
                   check=True)
    # fresh process: no teardown registered yet for this run dir
    monkeypatch.setattr(rebase_v3, "_SCRATCH_REGISTERED", set())
    ctx = StepContext(
        settings=settings, params={}, run_dir=rd, trace=trace,
        state={"task_spec": {"repo": "vllm-omni", "params": {}},
               "run_id": "adopt-run",
               "upstream_path": str(rd / "upstream_scratch"),
               "upstream_origin_path": str(repo)})
    out = rebase_v3._ensure_upstream_scratch(ctx)
    assert out == str(rd / "upstream_scratch")
    asyncio.run(lifecycle.finalize(rd, SimpleNamespace(status="done")))
    assert not (rd / "upstream_scratch").exists()     # torn down anyway


@pytest.mark.parametrize("surviving", [False, True])
def test_v3_resumed_scratch_restores_recorded_commit(
        v3_env, settings, trace, tmp_path, monkeypatch, surviving):
    from infermatrix_copilot.engine import lifecycle
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3
    _, _, origin, _ = v3_env
    pinned = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=origin, text=True).strip()
    (origin / "new-head.txt").write_text("canonical advanced")
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "advance"], cwd=origin, check=True)
    rd = tmp_path / "resume-pin-run"
    rd.mkdir()
    scratch = rd / "upstream_scratch"
    if surviving:
        subprocess.run(["git", "clone", "-q", str(origin), str(scratch)],
                       check=True)
    monkeypatch.setattr(rebase_v3, "_SCRATCH_REGISTERED", set())
    ctx = StepContext(
        settings=settings, params={}, run_dir=rd, trace=trace,
        state={"upstream_path": str(scratch),
               "upstream_origin_path": str(origin), "upstream_commit": pinned})
    out = rebase_v3._ensure_upstream_scratch(ctx)
    assert out == str(scratch)
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=scratch, text=True).strip() == pinned
    assert not (scratch / "new-head.txt").exists()
    asyncio.run(lifecycle.finalize(rd, SimpleNamespace(status="done")))
    assert not scratch.exists()


def test_v3_module_resume_recreates_pinned_scratch_before_agent(
        v3_agent_env, settings, trace, monkeypatch):
    from infermatrix_copilot.engine import lifecycle
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3
    from infermatrix_copilot.rebase_engine import module_rebase as mr
    _, _, origin, rd = v3_agent_env
    pinned = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=origin, text=True).strip()
    (origin / "new-head.txt").write_text("later canonical commit")
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "advance"], cwd=origin, check=True)
    monkeypatch.setattr(rebase_v3, "_ensure_checkout_locks", lambda *a: None)
    monkeypatch.setattr(rebase_v3, "_tier_client", lambda ctx: (
        object(), SimpleNamespace(model="m", api_key="k", base_url="")))
    seen = []

    async def fake_agent(module, **kw):
        path = kw["config"].vllm_path
        commit = await asyncio.to_thread(
            subprocess.check_output, ["git", "rev-parse", "HEAD"],
            cwd=path, text=True)
        seen.append(commit.strip())
        return {"status": "done", "exit_code": 0, "debug_attempts": 0,
                "turns": 1, "summary": "verified"}

    monkeypatch.setattr(mr, "rebase_module", fake_agent)
    ctx = StepContext(
        settings=settings, params={}, run_dir=rd, trace=trace,
        item="worker_runner",
        state={"task_spec": {"repo": "vllm-omni",
                             "params": {"rebase_mode": "full"}},
               "run_id": "resume-module", "repo_path": str(origin),
               "upstream_origin_path": str(origin),
               "upstream_path": str(rd / "upstream_scratch"),
               "upstream_commit": pinned})
    result = asyncio.run(rebase_v3._v3_module_rebase(ctx))
    assert result.ok, result.summary
    assert seen == [pinned]
    asyncio.run(lifecycle.finalize(rd, None))


def test_v3_scratch_checkout_failure_blocks(settings, trace, tmp_path,
                                           monkeypatch):
    from infermatrix_copilot.engine import lifecycle
    from infermatrix_copilot.engine.step import StepContext, StepResult
    from infermatrix_copilot.engine.steps import rebase_v3
    rd = tmp_path / "bad-pin-run"
    scratch = rd / "upstream_scratch"
    scratch.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=scratch, check=True)
    monkeypatch.setattr(rebase_v3, "_SCRATCH_REGISTERED", set())
    ctx = StepContext(
        settings=settings, params={}, run_dir=rd, trace=trace,
        state={"upstream_path": str(scratch),
               "upstream_commit": "missing-commit"})
    out = rebase_v3._ensure_upstream_scratch(ctx)
    assert isinstance(out, StepResult) and not out.ok
    assert "scratch checkout failed" in out.summary
    # a BLOCKED exit is resumable: the scratch stays for the retry
    asyncio.run(lifecycle.finalize(rd, None))
    assert scratch.exists()


def test_v3_push_gate_override_is_logged(v3_env, settings, trace, tmp_path):
    """push_with_failures is 'explicit and logged': the overridden
    structural failures land in the trace, the summary, and state — never
    indistinguishable from a genuinely clean gate."""
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    registry = register_builtin_steps(StepRegistry())
    rd = tmp_path / "override-run"
    rd.mkdir()
    Substate(rd, "ov").update(
        {"modules": {"m1": {"status": "failed"}},
         "tests": {"pipeline": {"failed_tests": []}}})
    ctx = StepContext(
        settings=settings, params={}, run_dir=rd, trace=trace,
        state={"task_spec": {"repo": "vllm-omni",
                             "params": {"push_with_failures": True}},
               "run_id": "ov"})
    r = asyncio.run(registry.get("rebase.v3_push_gate").handler(ctx))
    assert r.ok
    assert "OVERRIDDEN" in r.summary and "m1" in r.summary
    assert r.outputs["state_updates"]["push_gate_overrides"]
    assert any(e for e in trace.events("push_gate_override"))


def test_runner_model_download_notify(tmp_path):
    """Parent parity: a setup that will pull an uncached HF repo notifies
    ONCE per job (durable marker) before spawning; a cached repo stays
    silent."""
    from infermatrix_copilot.testing.runner import (
        TestJob, TestRunner, hf_repo_cached, hf_repo_from_setup)
    assert hf_repo_from_setup(
        "huggingface-cli download org/model --local-dir x") == "org/model"
    assert hf_repo_from_setup("hf download a/b file.bin") == "a/b"
    assert hf_repo_from_setup("pytest tests/") == ""
    hf_home = tmp_path / "hf"
    assert hf_repo_cached("org/model", str(hf_home)) is False
    snap = hf_home / "hub" / "models--org--model" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    assert hf_repo_cached("org/model", str(hf_home)) is True

    notified = []
    runner = TestRunner(repo_root=tmp_path, tests_dir=tmp_path / "tests",
                        notify_download=lambda k, r: notified.append((k, r)))
    # `echo` keeps the (best-effort, actually executed) setup harmless while
    # still matching the parent's same-line download regex
    job = TestJob(key="dl", command="true", timeout_sec=30, min_gpus=0,
                  gpu_lock=False,
                  setup="echo huggingface-cli download other/model x")
    env = {"HF_HOME": str(hf_home), "PATH": "/usr/bin:/bin"}
    assert runner.run(job, env).rc == 0
    assert notified == [("dl", "other/model")]
    assert (tmp_path / "tests" / ".model_download_email_sent_dl").exists()
    assert runner.run(job, env).rc == 0              # marker: no re-notify
    assert notified == [("dl", "other/model")]
    # cached repo: never notified
    job2 = TestJob(key="dl2", command="true", timeout_sec=30, min_gpus=0,
                   gpu_lock=False,
                   setup="echo huggingface-cli download org/model x")
    assert runner.run(job2, env).rc == 0
    assert [n for n in notified if n[0] == "dl2"] == []


@pytest.mark.parametrize("testing,expected", [
    ({}, 1), ({"local_timeout_multiplier": 3}, 3),
    ({"local_timeout_multiplier": 1.5}, 1.5)])
def test_local_timeout_multiplier_scales_only_valid_factors(testing, expected):
    from infermatrix_copilot.engine.steps.rebase_v3 import _local_timeout_multiplier
    assert _local_timeout_multiplier(testing) == expected


@pytest.mark.parametrize("factor", [0, 0.5, -2, True, "3"])
def test_local_timeout_multiplier_rejects_invalid_factor(factor):
    from infermatrix_copilot.engine.steps.rebase_v3 import _local_timeout_multiplier
    from infermatrix_copilot.engine.step import FailureKind, StepResult
    result = _local_timeout_multiplier({"local_timeout_multiplier": factor})
    assert isinstance(result, StepResult)
    assert result.failure == FailureKind.BLOCKED
