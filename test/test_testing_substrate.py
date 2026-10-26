"""PR1 guardrails: the testing substrate (gpu_lock, process_tree, watchdog,
watchdog_learn, env_plan, runner) ported from the rebase agent's shell layer.
Everything runs offline via injected collaborators."""

import os
import signal
import time
from pathlib import Path

import pytest

from infermatrix_copilot.testing import env_plan, process_tree, watchdog_learn
from infermatrix_copilot.testing.gpu_lock import (
    GpuLock, cleanup_orphan_gpu_procs, wait_gpu_memory_idle)
from infermatrix_copilot.testing.runner import (
    PY_TIMEOUT_MARGIN_SEC, TIMEOUT_RC, TestJob, TestRunner,
    append_silent_log_footer, backup_prev_log, strip_cov_flags)
from infermatrix_copilot.testing.watchdog import LogWatchdog, WatchdogPatterns

PATTERNS_YAML = (Path(__file__).resolve().parent.parent
                 / "adapters" / "vllm_omni" / "testing"
                 / "watchdog_patterns.yaml")


@pytest.fixture()
def patterns() -> WatchdogPatterns:
    return WatchdogPatterns.from_yaml(PATTERNS_YAML)


# -- watchdog pattern translation (bash ERE -> re) -----------------------------

def test_tier1_positive_and_negative(patterns):
    assert patterns.last_match(["torch.cuda.OutOfMemoryError: boom"], "critical")
    assert patterns.last_match(["  Killed  "], "critical")  # bare OOM Killed
    # benign teardown line must NOT match the whole-line Killed pattern
    assert patterns.last_match(
        ["WARNING StageDiffusionProc was killed by signal 15"],
        "critical") is None
    assert patterns.last_match(["all good here"], "critical") is None


def test_tier1_case_insensitive_like_grep_i(patterns):
    assert patterns.last_match(["fatal: something broke"], "critical")


def test_simulation_allowlist_suppresses_kills(patterns):
    line = "raised EngineDeadError (simulated)"  # allowlisted substring
    assert patterns.is_simulated(line)
    # non_fatal test names match Tier-1 FATAL case-insensitively — allowlisted
    assert patterns.is_simulated(
        "tests/x.py::test_non_fatal_client_error_preserves_non_400_status")
    # error-passthrough test NAMES are simulated even when the line is not
    assert patterns.is_simulated("ordinary line", "test_dead_engine_recovers")


def test_noise_is_case_sensitive_and_stripped(patterns):
    noisy = "PytestUnknownMarkWarning: unknown mark"
    assert patterns.is_noise(noisy)
    assert not patterns.is_noise("pytestunknownmarkwarning lowercased")
    kept = patterns.strip_noise([noisy, "RuntimeError: real"])
    assert kept == ["RuntimeError: real"]


def test_last_match_is_last_line(patterns):
    lines = ["ValueError: first", "middle", "RuntimeError: second"]
    assert patterns.last_match(lines, "review") == "RuntimeError: second"


# -- watchdog behavior ---------------------------------------------------------

def _wd(tmp_path, patterns, *, review=None, test_name="tests/x.py"):
    log = tmp_path / "t.log"
    log.write_text("")
    kills, records = [], []
    wd = LogWatchdog(patterns, log, pid=99999, test_name=test_name,
                     review_fn=review,
                     kill_fn=lambda pid: kills.append(pid),
                     record_fn=lambda p, v, t: records.append((p, v)),
                     pid_alive=lambda pid: True)
    return wd, log, kills, records


def test_tier1_kills_and_marks_log(tmp_path, patterns):
    wd, log, kills, _ = _wd(tmp_path, patterns)
    log.write_text("something\nCUDA out of memory\n")
    assert wd.check_once() is True
    assert kills == [99999]
    assert wd.result.tier == 1
    assert "[watchdog/kill] tier=1" in log.read_text()


def test_tier2_kill_needs_reviewer_confirmation(tmp_path, patterns):
    wd, log, kills, records = _wd(tmp_path, patterns,
                                  review=lambda t, s: "verdict: KILL")
    log.write_text("RuntimeError: engine wedged\n")
    assert wd.check_once() is True
    assert kills == [99999] and wd.result.tier == 2
    assert records == [("RuntimeError: engine wedged", "KILL")]


def test_tier2_defaults_to_continue_on_garbage_or_exception(tmp_path, patterns):
    for review in (lambda t, s: "no verdict here",
                   lambda t, s: (_ for _ in ()).throw(TimeoutError())):
        wd, log, kills, records = _wd(tmp_path, patterns, review=review)
        log.write_text("RuntimeError: maybe fine\n")
        assert wd.check_once() is False
        assert kills == []
        assert records[-1][1] == "CONTINUE"


def test_tier2_without_reviewer_never_kills(tmp_path, patterns):
    wd, log, kills, _ = _wd(tmp_path, patterns, review=None)
    log.write_text("RuntimeError: whatever\n")
    assert wd.check_once() is False and kills == []


def test_size_delta_gate_skips_unchanged_log(tmp_path, patterns):
    calls = []
    wd, log, _, _ = _wd(tmp_path, patterns,
                        review=lambda t, s: calls.append(1) or "CONTINUE")
    log.write_text("RuntimeError: x\n")
    wd.check_once()
    wd.check_once()  # unchanged size: no second review
    assert len(calls) == 1


def test_noise_never_reaches_the_reviewer(tmp_path, patterns):
    calls = []
    wd, log, _, _ = _wd(tmp_path, patterns,
                        review=lambda t, s: calls.append(1) or "CONTINUE")
    log.write_text("DeprecationWarning: Error: not really\n")
    assert wd.check_once() is False and calls == []


# -- watchdog_learn ------------------------------------------------------------

def test_learn_promotion_rules(tmp_path):
    logf = tmp_path / "decisions.jsonl"
    import json
    rows = []
    # qualifies: 3 CONTINUEs spanning 6 days
    for day in (1, 4, 7):
        rows.append({"ts": f"2026-07-0{day} 10:00:00",
                     "pattern": "loky KeyError teardown", "verdict": "CONTINUE"})
    # disqualified: one KILL
    for day in (1, 7):
        rows.append({"ts": f"2026-07-0{day} 10:00:00",
                     "pattern": "engine dead maybe", "verdict": "CONTINUE"})
    rows.append({"ts": "2026-07-08 10:00:00",
                 "pattern": "engine dead maybe", "verdict": "KILL"})
    # disqualified: same-day burst
    for h in (1, 2, 3):
        rows.append({"ts": f"2026-07-01 0{h}:00:00",
                     "pattern": "burst warning", "verdict": "CONTINUE"})
    logf.write_text("\n".join(json.dumps(r) for r in rows))

    got = watchdog_learn.eligible_patterns(
        watchdog_learn.read_decisions(logf), existing=set())
    assert got == ["loky KeyError teardown"]
    # already covered by seed (substring either direction) -> not promoted
    assert watchdog_learn.eligible_patterns(
        watchdog_learn.read_decisions(logf),
        existing={"KeyError teardown"}) == []


def test_learn_promote_appends_escaped_to_overlay(tmp_path):
    import json

    import yaml
    logf = tmp_path / "d.jsonl"
    logf.write_text("\n".join(json.dumps(
        {"ts": f"2026-07-0{d} 10:00:00", "pattern": "odd (x) warning",
         "verdict": "CONTINUE"}) for d in (1, 7, 9)))
    overlay = tmp_path / "overlay.yaml"
    new = watchdog_learn.promote(logf, overlay, seed_noise=["UserWarning"])
    assert new == ["odd (x) warning"]
    doc = yaml.safe_load(overlay.read_text())
    assert doc["noise"] == [r"odd\ \(x\)\ warning"] or doc["noise"] == [
        "odd\\ \\(x\\)\\ warning"]
    # overlay feeds back into pattern loading
    p = WatchdogPatterns.from_yaml(PATTERNS_YAML, overlay=overlay)
    assert p.is_noise("odd (x) warning")


def test_tier1_fatal_does_not_match_nonfatal_prose(patterns):
    """DRIFT_TRIAGE #8 (live full run 2026-08-23): critical patterns
    compile with re.I, so the parent's bare FATAL entry tier-1-killed a
    HEALTHY job on the benign "non-fatal RPC error" WARNING. Tier 1 sees
    the raw tail (noise cannot save it), so the seed itself must not
    match "non-fatal" - while real FATAL lines must still kill."""
    benign = ("WARNING 08-23 08:08:42 [rpc_result_router.py:79] Dropping "
              "uncorrelated non-fatal RPC error request_id=other "
              "stage_id=None: request failed")
    assert patterns.last_match([benign], "critical") is None
    assert patterns.last_match(["FATAL: engine core died"], "critical")
    # re.I is the point: lower-case fatal prose still triggers
    assert patterns.last_match(["fatal NCCL watchdog error"], "critical")


def test_learn_promote_is_idempotent(tmp_path):
    import json
    logf = tmp_path / "d.jsonl"
    logf.write_text("\n".join(json.dumps(
        {"ts": f"2026-07-0{d} 10:00:00", "pattern": "odd (y) warning",
         "verdict": "CONTINUE"}) for d in (1, 7, 9)))
    overlay = tmp_path / "overlay.yaml"
    assert watchdog_learn.promote(logf, overlay, seed_noise=[]) == \
        ["odd (y) warning"]
    before = overlay.read_text()
    assert watchdog_learn.promote(logf, overlay, seed_noise=[]) == []
    assert overlay.read_text() == before  # second call appends nothing


def test_learn_record_normalizes_pid_prefix(tmp_path):
    logf = tmp_path / "d.jsonl"
    watchdog_learn.record(logf, pattern="(StageEngineCoreProc pid=123) boom",
                          verdict="continue")
    row = watchdog_learn.read_decisions(logf)[0]
    assert row["pattern"] == "boom" and row["verdict"] == "CONTINUE"


# -- env_plan ------------------------------------------------------------------

def test_env_overlay_order_and_no_mutation(tmp_path):
    before = dict(os.environ)
    env = env_plan.build_subprocess_env(
        venv=tmp_path / "v", cuda_visible_devices="0,1", hf_home="/hf",
        pythonpath_prepend="/wt", job_env={"CUDA_VISIBLE_DEVICES": "2"})
    assert env["CUDA_VISIBLE_DEVICES"] == "2"  # per-job pair wins
    assert env["VIRTUAL_ENV"] == str(tmp_path / "v")
    assert env["PATH"].startswith(str(tmp_path / "v" / "bin"))
    assert env["PYTHONPATH"].split(os.pathsep)[0] == "/wt"
    assert dict(os.environ) == before  # our own env never mutated


def test_agent_shell_scrub_is_an_allowlist():
    env = {"ANTHROPIC_API_KEY": "k", "GITHUB_TOKEN": "t", "HF_TOKEN": "h",
           "PATH": "/bin", "OPENAI_API_KEY": "o", "GIT_ASKPASS": "a",
           "ECO_API_KEY": "e", "PERFORMANCE_API_KEY": "p",
           "BUILDKITE_API_TOKEN": "b", "MY_SERVICE_SECRET": "s",
           "DB_PASSWORD": "d", "HUGGING_FACE_HUB_TOKEN": "h2",
           # denylist escapees the review found — allowlist drops unknowns
           "AWS_SECRET_ACCESS_KEY": "aws", "GOOGLE_APPLICATION_CREDENTIALS": "g",
           "TOTALLY_NOVEL_CRED": "x",
           # runtime knobs that must survive
           "NCCL_DEBUG": "WARN", "CUDA_VISIBLE_DEVICES": "0",
           "HTTP_PROXY": "proxy", "GIT_CONFIG_COUNT": "1",
           "VLLM_USE_V1": "1", "HF_HOME": "/hf"}
    out = env_plan.scrub_agent_shell_env(env)  # default: fail-closed
    assert set(out) == {"PATH", "NCCL_DEBUG", "CUDA_VISIBLE_DEVICES",
                        "HTTP_PROXY", "GIT_CONFIG_COUNT", "VLLM_USE_V1",
                        "HF_HOME"}
    # explicit opt-in re-adds ONLY the HF tokens (gated-model adapters)
    opted = env_plan.scrub_agent_shell_env(env, keep_hf_token=True)
    assert "HF_TOKEN" in opted and "HUGGING_FACE_HUB_TOKEN" in opted
    assert "AWS_SECRET_ACCESS_KEY" not in opted
    # adapters widen via manifest data, never by weakening the default
    extra = env_plan.scrub_agent_shell_env(
        env, extra_safe_prefixes=("TOTALLY_NOVEL_",))
    assert "TOTALLY_NOVEL_CRED" in extra
    assert env["ANTHROPIC_API_KEY"] == "k"  # pure function


# -- gpu_lock ------------------------------------------------------------------

def test_gpu_lock_protocol_and_dead_owner_steal(tmp_path):
    d = tmp_path / "gpu_lock"
    lock = GpuLock(d, poll_sec=0.01, timeout_sec=5).acquire()
    assert (d / "lock").read_text() == str(os.getpid())
    lock.release()
    assert not (d / "lock").exists()
    # dead-owner steal: plant a lock held by a pid that cannot exist
    d.mkdir(exist_ok=True)
    (d / "lock").write_text("999999")
    (d / "owner").write_text("999999")
    GpuLock(d, poll_sec=0.01, timeout_sec=5).acquire().release()


def test_gpu_cleanup_parses_pmon_pid_column_never_kills_zero():
    """pmon rows are `<gpu> <pid> <type> ...`; the shell awk'd field 1 (the
    gpu index) — on GPU 0 that meant `kill 0`, signalling its own process
    group. The port must take field 2 and refuse non-positive pids."""
    pmon = ("# gpu   pid  type  sm  mem  enc  dec  command\n"
            "# Idx     #   C/G   %    %    %    %  name\n"
            "    0  7777     C  42   10    0    0  python\n")
    outs = iter(["", pmon, "", pmon])  # compute-apps empty; pmon has the row
    kills = []
    cleanup_orphan_gpu_procs("0", run=lambda cmd: next(outs, ""),
                             kill=lambda pid, sig: kills.append((pid, sig)),
                             sleep=lambda s: None)
    assert (7777, 15) in kills
    assert all(pid > 0 for pid, _ in kills)  # never 0, never the gpu index


def test_gpu_cleanup_excludes_own_tree_and_escalates():
    kills = []
    outs = iter([
        "4242\n", "",          # first pass: compute-apps, pmon
        "4242\n", "",          # second pass (post-TERM survivors)
    ])
    killed = cleanup_orphan_gpu_procs(
        "0", run=lambda cmd: next(outs, ""),
        kill=lambda pid, sig: kills.append((pid, sig)),
        sleep=lambda s: None)
    assert killed == 1
    assert (4242, 15) in kills and (4242, 9) in kills
    assert all(pid != os.getpid() for pid, _ in kills)


def test_gpu_wait_memory_idle_parses_and_times_out():
    assert wait_gpu_memory_idle("0", run=lambda c: "500, 80000\n",
                                sleep=lambda s: None) is True
    assert wait_gpu_memory_idle("0", run=lambda c: "79000, 80000\n",
                                timeout_sec=0.1, poll_sec=0.05,
                                sleep=lambda s: None) is False


# -- process_tree --------------------------------------------------------------

def test_collect_descendants_bfs_dedup():
    children = {1: [2, 3], 2: [4], 3: [4], 4: []}
    assert process_tree.collect_descendants(
        1, children_of=lambda p: children.get(p, [])) == [1, 2, 3, 4]


def test_kill_tree_term_then_kill_survivors():
    events = []
    alive = {10: 2}  # survives one TERM round

    def kill(pid, sig):
        events.append((pid, sig))
        if sig == signal.SIGTERM and alive.get(pid):
            alive[pid] -= 1

    import infermatrix_copilot.testing.process_tree as pt
    orig = pt._alive
    pt._alive = lambda pid: alive.get(pid, 0) > 0
    try:
        survivors = process_tree.kill_tree(
            [10], kill=kill, sleep=lambda s: None)
    finally:
        pt._alive = orig
    assert (10, signal.SIGTERM) in events and (10, signal.SIGKILL) in events
    assert survivors == [10]  # our fake never really dies


def test_kill_tree_pid_reuse_never_gets_sigkill():
    """After SIGTERM the target dies and the kernel reuses its pid for an
    unrelated process during the grace sleep: the escalation must compare
    /proc start times and leave the impostor alone."""
    import infermatrix_copilot.testing.process_tree as pt
    events = []
    phase = {"reused": False}

    def kill(pid, sig):
        events.append((pid, sig))
        phase["reused"] = True  # TERM delivered; pid recycled during grace

    orig_alive, orig_start = pt._alive, pt._start_time
    pt._alive = lambda pid: True  # the *recycled* pid is alive
    pt._start_time = lambda pid: 999 if phase["reused"] else 111
    try:
        survivors = process_tree.kill_tree([10], kill=kill,
                                           sleep=lambda s: None)
    finally:
        pt._alive, pt._start_time = orig_alive, orig_start
    assert (10, signal.SIGTERM) in events
    assert (10, signal.SIGKILL) not in events   # impostor spared
    assert survivors == []                       # original target counted dead


def test_kill_tree_recorded_identity_drops_reused_pid():
    """Accumulate-only snapshots hand kill_tree pids recorded seconds ago; a
    pid whose recorded starttime no longer matches the live one was reused by
    an unrelated process and must receive NO signal at all."""
    import infermatrix_copilot.testing.process_tree as pt
    events = []
    orig_alive, orig_start = pt._alive, pt._start_time
    pt._alive = lambda pid: True
    pt._start_time = lambda pid: {10: 111, 11: 222}[pid]
    try:
        survivors = process_tree.kill_tree(
            [10, 11], kill=lambda p, s: events.append((p, s)),
            sleep=lambda s: None,
            identity={10: 999, 11: 222})   # 10 was reused; 11 is still ours
    finally:
        pt._alive, pt._start_time = orig_alive, orig_start
    assert all(p != 10 for p, _ in events)  # reused pid untouched
    assert (11, signal.SIGTERM) in events


def test_kill_tree_reused_root_children_never_walked():
    """A snapshotted pid reused by an unrelated process WITH children: the
    root is rejected by identity, and its (unrelated, identity-less) children
    must not be collected or signalled either."""
    import infermatrix_copilot.testing.process_tree as pt
    events, walked = [], []
    children = {10: [30, 31], 11: []}
    orig_alive, orig_start, orig_cd = (pt._alive, pt._start_time,
                                      pt.collect_descendants)
    pt._alive = lambda pid: True
    pt._start_time = lambda pid: {10: 999, 11: 222, 30: 5, 31: 6}[pid]

    def cd(pid, children_of=None):
        walked.append(pid)
        return sorted({pid, *children.get(pid, [])})
    pt.collect_descendants = cd
    try:
        process_tree.kill_tree([10, 11],
                               kill=lambda p, s: events.append((p, s)),
                               sleep=lambda s: None,
                               identity={10: 111, 11: 222})  # 10 reused
    finally:
        pt._alive, pt._start_time = orig_alive, orig_start
        pt.collect_descendants = orig_cd
    assert 10 not in walked                       # stale root not traversed
    assert not any(p in (10, 30, 31) for p, _ in events)
    assert (11, signal.SIGTERM) in events


def test_kill_tree_root_reused_mid_walk_children_dropped():
    """A root can pass the pre-walk identity check, exit, and be reused
    DURING the descendant walk: its stranger children arrive identity-less
    and must be rejected by the post-walk re-validation of the root."""
    import infermatrix_copilot.testing.process_tree as pt
    events = []
    births = {10: 111}  # pre-walk read matches the recorded identity

    def cd(pid, children_of=None):
        births[10] = 999  # the root is recycled while we walk it
        return [pid, 30, 31]

    orig = (pt._alive, pt._start_time, pt._proc_stat_ids,
            pt.collect_descendants)
    pt._alive = lambda pid: True
    pt._start_time = lambda pid: births.get(pid, 500)
    pt._proc_stat_ids = lambda pid: {10: (1, births[10]), 30: (10, 300),
                                     31: (10, 310)}.get(pid)
    pt.collect_descendants = cd
    try:
        pt.kill_tree([10], kill=lambda p, s: events.append((p, s)),
                     sleep=lambda s: None, identity={10: 111})
    finally:
        (pt._alive, pt._start_time, pt._proc_stat_ids,
         pt.collect_descendants) = orig
    # 30/31 belong to the recycled stranger: no chain to a re-verified root
    assert not any(p in (30, 31) for p, _ in events)


def test_snapshot_record_walk_binds_identity_to_ancestry():
    """The accumulate-only snapshot must never retain a pid it cannot verify:
    no birth time (died mid-walk), or a ppid outside the walked tree (pid
    reused by a stranger between pgrep and the /proc read — the laundering
    race). Overwriting an existing record requires re-established ancestry."""
    from infermatrix_copilot.testing.runner import _record_walk
    ROOT, PARENT = 1, 99
    idmap: dict[int, int] = {}
    stats = {1: (PARENT, 100),   # leader, parented by the runner
             2: None,            # died mid-walk
             3: (1, 300),        # genuine child of the leader
             4: (777, 400)}      # pid reused: parent outside the tree
    _record_walk(idmap, [1, 2, 3, 4], root=ROOT, root_parent=PARENT,
                 stat_ids=lambda p: stats[p])
    assert idmap == {1: 100, 3: 300}
    # overwrite is allowed only with ancestry re-established...
    _record_walk(idmap, [1, 3], root=ROOT, root_parent=PARENT,
                 stat_ids=lambda p: {1: (PARENT, 100), 3: (1, 350)}[p])
    assert idmap[3] == 350
    # ...and refused when the new holder's parent is a stranger (old kept)
    _record_walk(idmap, [1, 3], root=ROOT, root_parent=PARENT,
                 stat_ids=lambda p: {1: (PARENT, 100), 3: (777, 999)}[p])
    assert idmap[3] == 350
    # a reused LEADER pid is refused too
    _record_walk(idmap, [1], root=ROOT, root_parent=PARENT,
                 stat_ids=lambda p: (777, 111))
    assert idmap[1] == 100


def test_snapshot_record_walk_requires_validated_chain():
    """Naming a walked pid as parent is not enough: when an INTERMEDIATE pid
    is reused, its stranger children still point at a 'walked' parent — only
    a chain of validated parents anchored at a validated leader is accepted."""
    from infermatrix_copilot.testing.runner import _record_walk
    ROOT, PARENT = 1, 99
    idmap: dict[int, int] = {}
    # 5 is a reused intermediate (parented by a stranger); 6 is its child and
    # must be rejected transitively even though its ppid (5) was walked.
    # 3→7 is a genuine grandchild chain and must be accepted transitively.
    stats = {1: (PARENT, 100), 3: (1, 300), 7: (3, 700),
             5: (777, 500), 6: (5, 600)}
    _record_walk(idmap, [1, 3, 5, 6, 7], root=ROOT, root_parent=PARENT,
                 stat_ids=lambda p: stats[p])
    assert idmap == {1: 100, 3: 300, 7: 700}
    # a dead leader invalidates the whole round: nothing recorded, priors kept
    _record_walk(idmap, [1, 3], root=ROOT, root_parent=PARENT,
                 stat_ids=lambda p: {1: None, 3: (1, 999)}[p])
    assert idmap == {1: 100, 3: 300, 7: 700}


def test_snapshot_leader_identity_immutable():
    """A recycled leader pid can wear the RIGHT ppid (the runner spawns many
    short-lived children) — once captured, the leader's birth is immutable
    and a same-parent stranger records nothing."""
    from infermatrix_copilot.testing.runner import _record_walk
    ROOT, PARENT = 1, 99
    idmap: dict[int, int] = {}
    _record_walk(idmap, [1], root=ROOT, root_parent=PARENT,
                 stat_ids=lambda p: (PARENT, 100))
    root_birth = idmap[ROOT]
    # stranger: correct parent, different birth — leader NOT validated, and
    # its 'child' is rejected transitively
    _record_walk(idmap, [1, 3], root=ROOT, root_parent=PARENT,
                 root_birth=root_birth,
                 stat_ids=lambda p: {1: (PARENT, 555), 3: (1, 300)}[p])
    assert idmap == {1: 100}
    # the true leader still validates and can accrue children
    _record_walk(idmap, [1, 3], root=ROOT, root_parent=PARENT,
                 root_birth=root_birth,
                 stat_ids=lambda p: {1: (PARENT, 100), 3: (1, 300)}[p])
    assert idmap == {1: 100, 3: 300}


def test_terminate_tree_without_identity_is_killpg_only(tmp_path, monkeypatch):
    """With no recorded identity at all, per-pid escalation is skipped
    entirely (killpg only): an unverifiable leader pid may belong to anyone."""
    import infermatrix_copilot.testing.runner as R
    import infermatrix_copilot.testing.process_tree as pt
    groups, calls = [], []
    monkeypatch.setattr(R, "_kill_group",
                        lambda pgid, sig: groups.append((pgid, sig)))
    monkeypatch.setattr(pt, "kill_tree",
                        lambda targets, identity=None:
                        calls.append((targets, identity)))
    R.TestRunner._terminate_tree(10, 77, snapshot=None)
    assert calls == []                       # nothing identifiable to escalate
    assert [g[0] for g in groups] == [77, 77]  # TERM then KILL on the pgroup


def test_terminate_tree_delegates_expansion_to_kill_tree(tmp_path, monkeypatch):
    """_terminate_tree must never expand descendants itself: pre-walked pids
    would reach kill_tree as identity-less LEGACY roots and bypass ancestry
    validation. Contract: exactly the identity-carrying snapshot pids are
    handed over as roots (identity attached); the single validated expansion
    happens inside kill_tree."""
    import infermatrix_copilot.testing.runner as R
    import infermatrix_copilot.testing.process_tree as pt
    walked, calls = [], []
    monkeypatch.setattr(R, "_kill_group", lambda pgid, sig: None)
    monkeypatch.setattr(pt, "collect_descendants",
                        lambda pid, children_of=None: walked.append(pid) or [pid])
    monkeypatch.setattr(pt, "kill_tree",
                        lambda targets, identity=None:
                        calls.append((targets, identity)))
    R.TestRunner._terminate_tree(10, 10, snapshot={10: 111, 20: 222})
    assert walked == []                              # no expansion here, ever
    assert calls == [([10, 20], {10: 111, 20: 222})]  # roots ≡ snapshot + ids

@pytest.fixture()
def runner(tmp_path, patterns) -> TestRunner:
    (tmp_path / "repo").mkdir()
    return TestRunner(repo_root=tmp_path / "repo", tests_dir=tmp_path / "tests",
                      patterns=patterns, artifact_globs=["test_*.wav"],
                      cuda_visible_devices="", available_gpus=lambda: 0,
                      watchdog_interval=0.05)


def test_dry_run_returns_exact_plan(runner):
    job = TestJob(key="k", command="pytest tests/x", timeout_sec=60,
                  min_gpus=2, env={"A": "1"}, index=3)
    out = runner.run(job, {}, dry_run=True)
    assert out.plan.argv == ["bash", "--norc", "-c", "set -e\npytest tests/x"]
    assert out.plan.env_overlay == {"A": "1"}
    assert out.plan.timeout_sec == 60 and out.plan.needs_gpu_lock
    assert out.plan.log_file.endswith("03_k.log")


def test_hw_gate_is_an_explicit_skip_not_a_pass(runner, tmp_path):
    job = TestJob(key="gpu8", command="echo hi", timeout_sec=5, min_gpus=8)
    out = runner.run(job, dict(os.environ))
    assert out.skipped and "8 GPU(s)" in out.skip_reason
    assert not (tmp_path / "tests" / ".passed_gpu8").exists()  # no false pass


def test_pass_marker_and_prev_backup(runner, tmp_path):
    job = TestJob(key="ok", command="echo one", timeout_sec=10, min_gpus=0,
                  index=1)
    out = runner.run(job, dict(os.environ))
    assert out.rc == 0
    assert (tmp_path / "tests" / ".passed_ok").exists()
    log = tmp_path / "tests" / "01_ok.log"
    first = log.read_text()
    runner.run(TestJob(key="ok", command="echo two", timeout_sec=10,
                       min_gpus=0, index=1), dict(os.environ))
    assert (tmp_path / "tests" / "01_ok.log.prev").read_text() == first


def test_baseline_suffix(runner, tmp_path):
    job = TestJob(key="b", command="echo x", timeout_sec=10, min_gpus=0, index=2)
    runner.run(job, dict(os.environ), baseline=True)
    assert (tmp_path / "tests" / "02_b_main_baseline.log").exists()
    assert (tmp_path / "tests" / ".passed_b_main_baseline").exists()


def test_silent_footer_variants(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("no signal at all\n")
    append_silent_log_footer(log, 1)
    assert "SILENT EXIT" in log.read_text()
    log2 = tmp_path / "b.log"
    log2.write_text("ERROR: file or directory not found: tests/gone.py\n")
    append_silent_log_footer(log2, 4)
    text = log2.read_text()
    assert "COLLECTION/PATH ERROR" in text and "do NOT retry on GPU" in text
    log3 = tmp_path / "c.log"  # real pytest signal: footer must no-op
    log3.write_text("=== 1 failed in 2.2s ===\n")
    append_silent_log_footer(log3, 1)
    assert "postmortem" not in log3.read_text()


def test_cov_strip_sed_parity():
    cmd = ("pytest -q tests/x --cov=vllm_omni --cov-branch "
           "--cov-report=term-missing tests/y")
    assert strip_cov_flags(cmd) == "pytest -q tests/x tests/y"


def test_cov_fallback_retries_once(runner, tmp_path):
    probe = tmp_path / "repo" / "probe.sh"
    probe.write_text("#!/bin/bash\n"
                     'if [[ "$*" == *--cov* ]]; then\n'
                     '  echo "ERROR: unrecognized arguments: --cov=x"; exit 2\n'
                     "fi\necho fine\n")
    job = TestJob(key="cov", command="bash probe.sh --cov=x", timeout_sec=20,
                  min_gpus=0, index=4)
    out = runner.run(job, dict(os.environ))
    assert out.rc == 0
    assert "[coverage-fallback]" in (tmp_path / "tests" / "04_cov.log").read_text()


def test_timeout_kills_group_and_reports_124(runner):
    job = TestJob(key="slow", command="sleep 30", timeout_sec=0.3, min_gpus=0,
                  index=5)
    t0 = time.monotonic()
    out = runner.run(job, dict(os.environ))
    assert out.rc == TIMEOUT_RC and out.timed_out
    assert time.monotonic() - t0 < 90  # primary fired, not the safety timer


def test_watchdog_kill_during_run(runner, tmp_path):
    log = tmp_path / "tests" / "06_wd.log"
    job = TestJob(
        key="wd", timeout_sec=30, min_gpus=0, index=6,
        command=f"echo 'CUDA out of memory' >> {log}; sleep 30")
    out = runner.run(job, dict(os.environ))
    assert out.watchdog_triggered and out.rc != 0
    assert "[watchdog/kill] tier=1" in log.read_text()


def test_artifact_cleanup_depth_one_only(runner, tmp_path):
    repo = tmp_path / "repo"
    (repo / "test_deadbeef.wav").write_text("x")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_fixture.wav").write_text("keep")
    runner.run(TestJob(key="clean", command="true", timeout_sec=10,
                       min_gpus=0, index=7), dict(os.environ))
    assert not (repo / "test_deadbeef.wav").exists()
    assert (repo / "tests" / "test_fixture.wav").exists()  # never recursive


def test_timeout_layering_constants():
    # the safety margin is the load-bearing 900 s from phase3; the safety
    # timer fires strictly after the primary by construction (timeout + margin)
    assert PY_TIMEOUT_MARGIN_SEC == 900


def test_timeout_kills_own_pgroup_descendants(runner, tmp_path):
    """A spawn-mode child in its OWN process group must die too: killpg never
    reaches it, and the leader exiting must not end the escalation."""
    pidfile = tmp_path / "child.pid"
    job = TestJob(
        key="orphan", timeout_sec=0.5, min_gpus=0, index=8,
        command=(f"setsid bash -c 'echo $$ > {pidfile}; exec sleep 60' &\n"
                 f"sleep 60"))
    out = runner.run(job, dict(os.environ))
    assert out.timed_out
    deadline = time.monotonic() + 10
    child = int(pidfile.read_text().strip())
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(child, signal.SIGKILL)
        pytest.fail(f"own-pgroup descendant {child} survived the tree kill")


def test_cov_fallback_runs_under_the_gpu_lock(patterns, tmp_path):
    """Missing pytest-cov is exactly when the fallback does the real GPU
    workload — it must run while the lock is still held."""
    (tmp_path / "repo").mkdir()
    lock_dir = tmp_path / "gpu_lock"
    r = TestRunner(repo_root=tmp_path / "repo", tests_dir=tmp_path / "tests",
                   patterns=patterns, gpu_lock_dir=lock_dir,
                   cuda_visible_devices="", available_gpus=lambda: 1,
                   watchdog_interval=0.05)
    probe = tmp_path / "repo" / "probe.sh"
    probe.write_text(
        "#!/bin/bash\n"
        'if [[ "$*" == *--cov* ]]; then\n'
        '  echo "ERROR: unrecognized arguments: --cov=x"; exit 2\nfi\n'
        f'test -f {lock_dir / "lock"} && echo LOCKED-DURING-FALLBACK\n')
    out = r.run(TestJob(key="covlock", command="bash probe.sh --cov=x",
                        timeout_sec=20, min_gpus=1, index=9),
                dict(os.environ))
    assert out.rc == 0
    log = (tmp_path / "tests" / "09_covlock.log").read_text()
    assert "LOCKED-DURING-FALLBACK" in log
    assert not (lock_dir / "lock").exists()  # released afterwards


def test_setup_timeout_is_best_effort(runner, tmp_path):
    job = TestJob(key="setup", command="echo main-ran", timeout_sec=0.4,
                  min_gpus=0, index=10, setup="sleep 60")
    out = runner.run(job, dict(os.environ))
    assert out.rc == 0  # the job itself still ran and passed
    log = (tmp_path / "tests" / "10_setup.log").read_text()
    assert "[setup] ignored failure" in log and "main-ran" in log


def test_artifact_globs_cannot_escape_or_recurse(runner, tmp_path):
    from infermatrix_copilot.testing.runner import cleanup_test_artifacts
    repo = tmp_path / "repo"
    (repo / "sub").mkdir()
    (repo / "sub" / "nested.wav").write_text("keep")
    outside = tmp_path / "outside.wav"
    outside.write_text("keep")
    removed = cleanup_test_artifacts(
        repo, ["**/*.wav", "../outside.wav", "sub/nested.wav"])
    assert removed == 0
    assert (repo / "sub" / "nested.wav").exists() and outside.exists()


def test_final_scan_catches_fast_failures(runner, tmp_path):
    """A job shorter than the watchdog poll interval that prints a critical
    line and exits 0 must NOT earn a pass marker — the final scan sees it."""
    job = TestJob(key="fast", timeout_sec=30, min_gpus=0, index=12,
                  command="echo 'CUDA out of memory'; true")
    out = runner.run(job, dict(os.environ))
    assert out.watchdog_triggered and out.rc != 0
    assert not (tmp_path / "tests" / ".passed_fast").exists()


def test_timeout_return_waits_for_tree_kill(runner, tmp_path):
    """_spawn must not return while the fired primary timer is still
    escalating: by return time the own-pgroup child is already dead."""
    pidfile = tmp_path / "child2.pid"
    job = TestJob(
        key="join", timeout_sec=0.5, min_gpus=0, index=13,
        command=(f"setsid bash -c 'echo $$ > {pidfile}; exec sleep 60' &\n"
                 f"sleep 0.2; wait"))
    out = runner.run(job, dict(os.environ))
    assert out.timed_out
    child = int(pidfile.read_text().strip())
    with pytest.raises(ProcessLookupError):  # dead already, not eventually
        os.kill(child, 0)


def test_setup_nonzero_rc_is_logged(runner, tmp_path):
    job = TestJob(key="setuprc", command="echo main-ran", timeout_sec=10,
                  min_gpus=0, index=14, setup="false")
    out = runner.run(job, dict(os.environ))
    assert out.rc == 0
    log = (tmp_path / "tests" / "14_setuprc.log").read_text()
    assert "[setup] ignored failure: rc=1" in log


def test_setup_timeout_kills_its_session_children(runner, tmp_path):
    pidfile = tmp_path / "setup_child.pid"
    job = TestJob(
        key="setupkill", command="echo main-ran", timeout_sec=1, min_gpus=0,
        index=15,
        setup=f"bash -c 'echo $$ > {pidfile}; exec sleep 60' &\nsleep 60")
    out = runner.run(job, dict(os.environ))
    assert out.rc == 0
    child = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            break
    else:
        os.kill(child, signal.SIGKILL)
        pytest.fail("setup's background child survived the group kill")


def test_job_cuda_override_governs_gate_and_cleanup(patterns, tmp_path):
    """A job redirecting itself to other GPUs is gated on ITS devices and
    cleaned up on ITS devices, not the runner's."""
    (tmp_path / "repo").mkdir()
    cleaned = []
    import infermatrix_copilot.testing.runner as rmod
    orig_clean, orig_wait = rmod.cleanup_orphan_gpu_procs, rmod.wait_gpu_memory_idle
    rmod.cleanup_orphan_gpu_procs = lambda dev, **k: cleaned.append(dev) or 0
    rmod.wait_gpu_memory_idle = lambda dev, **k: True
    try:
        r = TestRunner(repo_root=tmp_path / "repo",
                       tests_dir=tmp_path / "tests", patterns=patterns,
                       gpu_lock_dir=tmp_path / "gl",
                       cuda_visible_devices="0",
                       available_gpus=lambda: 1, watchdog_interval=0.05)
        # runner has 1 GPU, but the job redirects to 2 — gate on the job's
        out = r.run(TestJob(key="redir", command="true", timeout_sec=10,
                            min_gpus=2, index=16,
                            env={"CUDA_VISIBLE_DEVICES": "2,3"}),
                    dict(os.environ))
        assert not out.skipped and out.rc == 0
        assert cleaned == ["2,3"]  # cleanup targeted the job's devices
        # and the reverse: runner has GPUs, job pins itself to none
        out = r.run(TestJob(key="none", command="true", timeout_sec=10,
                            min_gpus=1, index=17,
                            env={"CUDA_VISIBLE_DEVICES": ""}),
                    dict(os.environ))
        assert out.skipped
    finally:
        rmod.cleanup_orphan_gpu_procs = orig_clean
        rmod.wait_gpu_memory_idle = orig_wait


def test_gpu_lock_steal_never_removes_live_lock_under_contention(tmp_path):
    """The steal path re-verifies staleness inside its flock'd critical
    section: a live lock created in the race window survives every steal."""
    d = tmp_path / "gl"
    live = GpuLock(d, poll_sec=0.01, timeout_sec=5).acquire()  # us, alive
    thief = GpuLock(d, poll_sec=0.01, timeout_sec=0.05)
    assert thief._steal_if_stale() is False
    assert (d / "lock").read_text() == str(os.getpid())  # untouched
    live.release()


def test_watchdog_never_fires_twice(tmp_path, patterns):
    """The kill marker grows the log; a later scan must not rediscover the
    same error and kill/record/report a second time."""
    kills, reports = [], []
    log = tmp_path / "t.log"
    log.write_text("CUDA out of memory\n")
    wd = LogWatchdog(patterns, log, pid=99999, test_name="t",
                     kill_fn=lambda pid: kills.append(pid),
                     report_fn=lambda *a: reports.append(a),
                     pid_alive=lambda pid: True)
    assert wd.check_once() is True
    assert wd.check_once() is True  # final scan: no re-fire
    assert len(kills) == 1 and len(reports) == 1


def test_reconcile_after_wait_is_child_scoped(tmp_path):
    """A duplicate child's parent reaping its loser must not mark the
    winner's live run interrupted (mcp_server passes the reaped pid)."""
    from infermatrix_copilot import run_status as rs
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    rs.init_queued(run_dir, run_id="r", owner_server_id="s", owner_server_pid=1)
    rs.mark_child_started(run_dir, child_pid=4242, state=rs.RUNNING)
    # the reaped pid differs from the recorded (winning) child: no-op
    # (_locked_update returns the untouched current record on opt-out)
    out = rs.reconcile_after_wait(run_dir, child_pid=9999)
    assert out["state"] == rs.RUNNING
    assert rs.read_status(run_dir)["state"] == rs.RUNNING
    # the recorded child's own parent may reconcile
    out = rs.reconcile_after_wait(run_dir, child_pid=4242)
    assert out["state"] == rs.INTERRUPTED


def test_setup_group_kill_reaps_term_ignoring_child(runner, tmp_path):
    """The setup leader exiting promptly must not spare a background child
    that ignores SIGTERM — the unconditional group KILL reaps it."""
    pidfile = tmp_path / "stubborn.pid"
    job = TestJob(
        key="stubborn", command="echo main-ran", timeout_sec=1, min_gpus=0,
        index=18,
        setup=(f"bash -c 'trap \"\" TERM; echo $$ > {pidfile}; "
               f"exec sleep 60' &\nsleep 60"))
    out = runner.run(job, dict(os.environ))
    assert out.rc == 0
    child = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            break
    else:
        os.kill(child, signal.SIGKILL)
        pytest.fail("TERM-ignoring setup child survived the group KILL")


def test_base_env_cuda_override_also_governs_gate(patterns, tmp_path):
    """CUDA redirection in the BASE env (not job.env) must govern gating too:
    the child sees {**env, **job.env}."""
    (tmp_path / "repo").mkdir()
    r = TestRunner(repo_root=tmp_path / "repo", tests_dir=tmp_path / "tests",
                   patterns=patterns, cuda_visible_devices="0,1",
                   available_gpus=lambda: 2, watchdog_interval=0.05)
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = ""  # base env pins the child to no GPUs
    out = r.run(TestJob(key="basecuda", command="true", timeout_sec=10,
                        min_gpus=1, index=19), env)
    assert out.skipped  # gated on the child's effective devices, not ours


def test_reconcile_unclaimed_status_spared_for_lock_loser(tmp_path):
    """Winner holds the run lock but hasn't published its pid yet; the
    loser's parent (exit code 3, no terminal status) must not reconcile."""
    from infermatrix_copilot import run_status as rs
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    rs.init_queued(run_dir, run_id="r", owner_server_id="s", owner_server_pid=1)
    out = rs.reconcile_after_wait(run_dir, child_pid=9999,
                                  suspect_lock_loser=True)
    assert out["state"] == rs.QUEUED  # untouched
    # a genuine pre-publish crash (non-3 exit) still reconciles
    out = rs.reconcile_after_wait(run_dir, child_pid=9999,
                                  suspect_lock_loser=False)
    assert out["state"] == rs.INTERRUPTED


def test_final_scan_kill_works_after_leader_reaped(runner, tmp_path):
    """Short command logs a critical line, backgrounds a child, exits: the
    final scan fires after proc.wait(), and the kill must still reach the
    group via the pre-captured pgid."""
    pidfile = tmp_path / "bg.pid"
    log = tmp_path / "tests" / "20_fastbg.log"
    job = TestJob(
        key="fastbg", timeout_sec=30, min_gpus=0, index=20,
        command=(f"bash -c 'echo $$ > {pidfile}; exec sleep 60' &\n"
                 f"echo 'CUDA out of memory' >> {log}; true"))
    out = runner.run(job, dict(os.environ))
    assert out.watchdog_triggered and out.rc != 0
    child = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            break
    else:
        os.kill(child, signal.SIGKILL)
        pytest.fail("background child survived the post-reap final-scan kill")


def test_watchdog_tail_reads_bounded_window(tmp_path, patterns):
    """The tail must not slurp the whole log: a huge file is read through a
    bounded window and still yields the trailing lines."""
    log = tmp_path / "big.log"
    pad = "x" * 60
    with open(log, "w") as f:
        for i in range(200_000):
            f.write(f"line {i} {pad}\n")
        f.write("RuntimeError: at the end\n")
    wd = LogWatchdog(patterns, log, pid=1, test_name="t",
                     pid_alive=lambda pid: True)
    tail = wd._tail(150)
    assert len(tail) == 150 and tail[-1] == "RuntimeError: at the end"
    # the backward-chunked read is bounded: its hard cap is far below this
    # file's size, so correctness here proves it never slurped the whole log
    assert wd._TAIL_MAX_BYTES < log.stat().st_size


def test_gpu_lock_rolls_back_on_owner_write_failure(tmp_path):
    d = tmp_path / "gl"
    lock = GpuLock(d, poll_sec=0.01, timeout_sec=5)
    lock.owner_file = d / "no-such-dir" / "owner"  # injected write failure
    with pytest.raises(OSError):
        lock.acquire()
    assert not (d / "lock").exists()  # rolled back, not wedged for an hour


def test_learn_truncated_patterns_match_their_source(tmp_path):
    import json

    import yaml
    long_line = "W0101 worker error: " + "x" * 150 + " tail-differs"
    logf = tmp_path / "d.jsonl"
    logf.write_text("\n".join(json.dumps(
        {"ts": f"2026-07-0{d} 10:00:00",
         "pattern": watchdog_learn.normalize_pattern(long_line),
         "verdict": "CONTINUE"}) for d in (1, 7, 9)))
    overlay = tmp_path / "overlay.yaml"
    assert len(watchdog_learn.promote(logf, overlay, seed_noise=[])) == 1
    regex = yaml.safe_load(overlay.read_text())["noise"][0]
    assert __import__("re").search(regex, long_line)  # matches the original
    # and stays idempotent in regex space
    assert watchdog_learn.promote(logf, overlay, seed_noise=[]) == []


def test_final_scan_kill_reaches_reparented_setsid_child(runner, tmp_path):
    """A setsid'd child is reparented to init once the leader is reaped —
    only the snapshot thread's last live walk can still name it. The
    watchdog interval is set far above the command's lifetime, proving the
    snapshot cadence (not a watchdog poll) preserved the tree."""
    runner.SNAPSHOT_INTERVAL = 0.05
    runner.watchdog_interval = 5.0  # no poll happens while the job lives
    pidfile = tmp_path / "detached.pid"
    log = tmp_path / "tests" / "21_detached.log"
    job = TestJob(
        key="detached", timeout_sec=30, min_gpus=0, index=21,
        command=(f"setsid bash -c 'echo $$ > {pidfile}; exec sleep 60' &\n"
                 f"sleep 0.3\n"  # a couple of snapshot cycles
                 f"echo 'CUDA out of memory' >> {log}; true"))
    out = runner.run(job, dict(os.environ))
    assert out.watchdog_triggered
    child = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            break
    else:
        os.kill(child, signal.SIGKILL)
        pytest.fail("reparented setsid child survived the snapshot kill")


def test_scrub_prefix_collisions_and_cred_suffix_veto():
    env = {"HOME": "/root", "HOME_TOKEN": "x", "USER": "u",
           "USER_PASSWORD": "x", "HOST_API_KEY": "x", "PATHS_SECRET": "x",
           "PYTHON_API_KEY": "x", "PYTHONPATH": "/p",
           "GIT_CONFIG_COUNT": "1"}
    out = env_plan.scrub_agent_shell_env(env)
    assert set(out) == {"HOME", "USER", "PYTHONPATH", "GIT_CONFIG_COUNT"}


def test_watchdog_scoped_to_attempt_ignores_setup_output(runner, tmp_path):
    """A best-effort setup printing a critical line must not get a passing
    main run killed: each attempt's watchdog starts at its own log offset."""
    job = TestJob(key="setupnoise", command="echo fine; true", timeout_sec=15,
                  min_gpus=0, index=22,
                  setup="echo 'CUDA out of memory (from setup, harmless)'")
    out = runner.run(job, dict(os.environ))
    assert out.rc == 0 and not out.watchdog_triggered
    assert (tmp_path / "tests" / ".passed_setupnoise").exists()


def test_failing_report_writer_never_breaks_the_outcome(runner, tmp_path):
    runner.report_fn = lambda *a: (_ for _ in ()).throw(OSError("disk full"))
    log = tmp_path / "tests" / "23_rep.log"
    job = TestJob(key="rep", timeout_sec=30, min_gpus=0, index=23,
                  command=f"echo 'CUDA out of memory' >> {log}; sleep 30")
    out = runner.run(job, dict(os.environ))  # no exception escapes
    assert out.watchdog_triggered and out.rc != 0


def test_runner_cuda_selection_reaches_the_child(patterns, tmp_path):
    (tmp_path / "repo").mkdir()
    r = TestRunner(repo_root=tmp_path / "repo", tests_dir=tmp_path / "tests",
                   patterns=patterns, cuda_visible_devices="0",
                   available_gpus=lambda: 1, watchdog_interval=0.05)
    env = dict(os.environ)
    env.pop("CUDA_VISIBLE_DEVICES", None)  # base env silent on devices
    out = r.run(TestJob(key="vis", timeout_sec=10, min_gpus=0, index=24,
                        command="echo \"child sees: $CUDA_VISIBLE_DEVICES\""),
                env)
    assert out.rc == 0
    log = (tmp_path / "tests" / "24_vis.log").read_text()
    assert "child sees: 0" in log  # not every host GPU


def test_setup_cov_noise_never_triggers_fallback(runner, tmp_path):
    """Setup printing the cov-argparse line + a primary failing for another
    reason must NOT retry without coverage (and possibly false-pass)."""
    job = TestJob(
        key="covnoise", timeout_sec=15, min_gpus=0, index=25,
        command="echo 'real failure'; exit 7",
        setup="echo 'ERROR: unrecognized arguments: --cov=x (setup noise)'")
    out = runner.run(job, dict(os.environ))
    assert out.rc == 7  # the real failure, not a fallback result
    log = (tmp_path / "tests" / "25_covnoise.log").read_text()
    assert "[coverage-fallback]" not in log


def test_cuda_minus_one_hides_all_devices(runner, tmp_path):
    from infermatrix_copilot.testing.gpu_lock import visible_devices
    assert visible_devices("-1") == []
    assert visible_devices("0,1") == ["0", "1"]
    assert visible_devices("") == []
    out = runner.run(TestJob(key="hidden", command="true", timeout_sec=10,
                             min_gpus=1, index=26,
                             env={"CUDA_VISIBLE_DEVICES": "-1"}),
                     dict(os.environ))
    assert out.skipped  # -1 is zero devices, not one


def test_runner_default_gpu_count_honors_minus_one(tmp_path, patterns):
    """The default available_gpus derives from the runner's own CUDA spec —
    "-1" hides every device and must count as zero, not one (a GPU-gated job
    would otherwise run with all GPUs masked)."""
    (tmp_path / "repo").mkdir()
    r = TestRunner(repo_root=tmp_path / "repo", tests_dir=tmp_path / "tests",
                   patterns=patterns, cuda_visible_devices="-1")
    assert r.available_gpus() == 0
    out = r.run(TestJob(key="masked", command="true", timeout_sec=10,
                        min_gpus=1, index=28), dict(os.environ))
    assert out.skipped


def test_fatal_primary_never_enters_cov_fallback(runner, tmp_path):
    """A watchdog-killed primary must keep its fatal outcome even when the
    log happens to contain the cov-argparse line — a passing fallback must
    not mint a pass marker over a CUDA OOM."""
    log = tmp_path / "tests" / "27_fatal.log"
    job = TestJob(
        key="fatal", timeout_sec=30, min_gpus=0, index=27,
        command=(f"echo 'ERROR: unrecognized arguments: --cov=x' >> {log}\n"
                 f"echo 'CUDA out of memory' >> {log}\n"
                 f"sleep 30"))
    out = runner.run(job, dict(os.environ))
    assert out.watchdog_triggered and out.rc != 0
    assert "[coverage-fallback]" not in log.read_text()
    assert not (tmp_path / "tests" / ".passed_fatal").exists()


def test_failing_recorder_never_suppresses_a_kill(tmp_path, patterns):
    """Telemetry is best-effort: a recorder raising (full disk) must not
    leave a reviewer-confirmed KILL unexecuted."""
    kills = []
    log = tmp_path / "t.log"
    log.write_text("RuntimeError: engine wedged\n")

    def bad_recorder(p, v, t):
        raise OSError("disk full")

    wd = LogWatchdog(patterns, log, pid=99999, test_name="t",
                     review_fn=lambda t, s: "KILL",
                     kill_fn=lambda pid: kills.append(pid),
                     record_fn=bad_recorder, pid_alive=lambda pid: True)
    assert wd.check_once() is True
    assert kills == [99999]  # the kill happened despite the recorder


def test_tail_survives_one_huge_final_line(tmp_path, patterns):
    """A final line far larger than any per-line byte estimate must still be
    seen in full enough form to match its leading critical text."""
    log = tmp_path / "huge.log"
    log.write_text("ok line\n" + "CUDA out of memory " + "x" * 100_000 + "\n")
    wd = LogWatchdog(patterns, log, pid=1, test_name="t",
                     pid_alive=lambda pid: True)
    tail = wd._tail(150)
    assert any(ln.startswith("CUDA out of memory") for ln in tail)


def test_dir_fsync_swallow_is_scoped_to_unsupported(settings, trace, tmp_path,
                                                    monkeypatch):
    import errno

    from infermatrix_copilot.engine.executor import Executor
    from infermatrix_copilot.engine.registry import StepRegistry
    ex = Executor(StepRegistry(), settings, run_dir=tmp_path / "r", trace=trace)
    import infermatrix_copilot.engine.executor as exmod
    real_open = exmod.os.open

    def raising_open(path, flags, *a, **k):
        if str(path) == str(ex.run_dir):
            raise OSError(errno.EIO, "I/O error")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(exmod.os, "open", raising_open)
    with pytest.raises(OSError):  # real storage failure propagates
        ex._save_progress({"completed": {}})

    def unsupported_open(path, flags, *a, **k):
        if str(path) == str(ex.run_dir):
            raise OSError(errno.EINVAL, "not supported")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(exmod.os, "open", unsupported_open)
    ex._save_progress({"completed": {}})  # degraded, not fatal


def test_gpu_lock_steal_grace_for_unparseable_lock(tmp_path):
    """An empty lock younger than the grace window is a writer mid-create,
    not a crash artifact — it must not be stolen."""
    d = tmp_path / "gl"
    d.mkdir()
    (d / "lock").write_text("")
    lock = GpuLock(d, poll_sec=0.01, timeout_sec=0.05)
    with pytest.raises(Exception):
        lock.acquire()  # fresh empty lock: honored, so acquire times out


def test_failure_and_skip_clear_stale_pass_marker(runner, tmp_path):
    ok = TestJob(key="flip", command="true", timeout_sec=10, min_gpus=0,
                 index=11)
    runner.run(ok, dict(os.environ))
    marker = tmp_path / "tests" / ".passed_flip"
    assert marker.exists()
    runner.run(TestJob(key="flip", command="false", timeout_sec=10,
                       min_gpus=0, index=11), dict(os.environ))
    assert not marker.exists()  # failure removed the stale marker
    runner.run(ok, dict(os.environ))
    assert marker.exists()
    runner.run(TestJob(key="flip", command="true", timeout_sec=10,
                       min_gpus=9, index=11), dict(os.environ))  # hw skip
    assert not marker.exists()  # a skip must not retain a misleading marker
