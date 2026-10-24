"""Single-test executor — port of test_runner.sh's `run_ci_cmd_with_watchdog`
(local mode; remote-exec was retired with the shell layer).

Timeout layering (invariant, pinned by tests): the **primary** timer fires at
`job.timeout_sec` and kills the whole process group (the shell's `timeout(1)`
only killed the direct child, leaving pytest workers holding GPU memory — the
outer Python killpg existed to reap them; here one timer does both). The
watchdog may kill earlier on log patterns. The **safety** timer at
`timeout_sec + PY_TIMEOUT_MARGIN_SEC` fires strictly later and SIGKILLs the
group — it only matters if the primary path itself wedged.

One deliberate divergence from the shell, by design (plan §7): a hardware
gate produces an explicit `skipped` outcome instead of the shell's rc=0,
which inflated pass counts and marked never-run tests complete for resume
(`phase3._shell_skipped` existed to undo that).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .gpu_lock import (GpuLock, cleanup_orphan_gpu_procs, visible_devices,
                       wait_gpu_memory_idle)
from .watchdog import LogWatchdog, WatchdogPatterns

PY_TIMEOUT_MARGIN_SEC = 900  # safety margin; must fire strictly after primary
TIMEOUT_RC = 124  # bash `timeout` parity

_COV_STRIP = [  # exact sed-equivalents from test_runner.sh
    (re.compile(r"\s+--cov=\S+"), ""),
    (re.compile(r"\s+--cov-branch"), ""),
    (re.compile(r"\s+--cov-report=\S+"), ""),
]

# "usable failure signal already captured" — the shell's footer no-op grep
_SIGNAL_RE = re.compile(
    r"(=+ .* (passed|failed|error|skipped)|Traceback \(most recent call last\)"
    r"|^FAILED |^ERROR |short test summary info|pytest\.ExitCode|SystemExit:)",
    re.M)
_COLLECTION_RE = re.compile(
    r"(ERROR: file or directory not found:|ERROR: not found:"
    r"|collected 0 items|no tests ran)")


@dataclass
class TestJob:
    """One manifest job: `key` (slug), the shell `command`, optional `setup`
    (best-effort, output appended), per-job `env` pairs, `timeout_sec`,
    `min_gpus`, and the display `index` used in the log filename."""
    __test__ = False  # "Test" prefix is domain naming, not a pytest class
    key: str
    command: str
    timeout_sec: float
    min_gpus: int = 1
    # None: hold the GPU lock iff min_gpus > 0 (historical). Explicit True
    # serializes on the lock WITHOUT making GPUs a hardware requirement —
    # a mutex-holding wrapper must not "skip rc=0" on a GPU-less box.
    gpu_lock: bool | None = None
    env: dict[str, str] = field(default_factory=dict)
    setup: str = ""
    index: int = 0
    hw: str = ""  # informational only — never enforced (shell parity)


@dataclass
class RunPlan:
    """What would run — returned as-is under dry_run for command-echo parity."""
    argv: list[str]
    env_overlay: dict[str, str]
    timeout_sec: float
    needs_gpu_lock: bool
    log_file: str
    cwd: str


@dataclass
class TestOutcome:
    rc: int
    skipped: bool = False
    skip_reason: str = ""
    watchdog_triggered: bool = False
    timed_out: bool = False
    log_file: str = ""
    plan: RunPlan | None = None


def artifact_suffix(baseline: bool) -> str:
    return "_main_baseline" if baseline else ""


# ── model-download notification (parent `_ci_test_notify_model_download`) ────

_HF_DOWNLOAD_RX = re.compile(
    r"(?:huggingface-cli|hf)\s+download\s+(\S+)")


def hf_repo_from_setup(setup: str) -> str:
    """First org/repo from a `huggingface-cli download ...` / `hf download`
    line in the job's setup command, or "" (parent regex, verbatim)."""
    m = _HF_DOWNLOAD_RX.search(setup or "")
    return m.group(1) if m else ""


def hf_repo_cached(repo_id: str, hf_home: str) -> bool:
    """True when the repo has at least one snapshot under `HF_HOME/hub`
    (parent `_hf_hub_repo_cached`)."""
    if not repo_id or not hf_home:
        return False
    d = Path(hf_home) / "hub" / f"models--{repo_id.replace('/', '--')}" \
        / "snapshots"
    try:
        return d.is_dir() and any(d.iterdir())
    except OSError:
        return False


def log_file_for(tests_dir: Path, job: TestJob, *, baseline: bool = False) -> Path:
    return Path(tests_dir) / (
        f"{job.index:02d}_{job.key}{artifact_suffix(baseline)}.log")


def pass_marker_for(tests_dir: Path, job: TestJob, *, baseline: bool = False) -> Path:
    return Path(tests_dir) / f".passed_{job.key}{artifact_suffix(baseline)}"


def backup_prev_log(log_file: Path) -> None:
    """Preserve the previous attempt's traceback as `.prev`, truncate the log.
    Streaming copy (the shell's `cp -p`) — logs can be huge."""
    import shutil

    log_file = Path(log_file)
    try:
        if log_file.is_file() and log_file.stat().st_size > 0:
            shutil.copy2(log_file, log_file.with_name(log_file.name + ".prev"))
        log_file.write_text("")
    except OSError:
        pass


def append_silent_log_footer(log_file: Path, rc: int, context: str = "primary"
                             ) -> None:
    """Postmortem footer when a command failed but the log carries no pytest
    signal at all. rc=4/5 or collection markers get the actionable
    COLLECTION/PATH footer instead of the OOM one — sending the debug agent
    after a hardware ghost for a renamed test path burned real retries."""
    log_file = Path(log_file)
    if rc == 0 or not log_file.is_file():
        return
    # single streaming pass (the shell's grep): logs can be huge, and every
    # pattern here is single-line, so nothing needs the whole file in memory
    has_signal = has_collection = False
    hits: list[str] = []
    size = 0
    try:
        with open(log_file, encoding="utf-8", errors="replace") as f:
            for ln in f:
                size += len(ln.encode("utf-8", errors="replace"))
                line = ln.rstrip("\n")
                if _SIGNAL_RE.search(line):
                    has_signal = True
                if _COLLECTION_RE.search(line):
                    has_collection = True
                if len(hits) < 5 and (
                        "ERROR: file or directory not found:" in line
                        or "ERROR: not found:" in line):
                    hits.append(line)
    except OSError:
        return
    if size and has_signal:
        return
    bar = "=" * 74
    p = f"[postmortem/{context}]"
    if rc in (4, 5) or has_collection:
        body = "\n".join([
            "", bar,
            f"{p} COLLECTION/PATH ERROR — pytest collected no tests (rc={rc})",
            f"{p}   NOT an OOM/SIGKILL: the test path or marker in the",
            f"{p}   manifest command matches no file on disk.",
            f"{p}   Most likely an upstream test rename (e.g. *_expansion.py).",
            *(f"{p}   {ln}" for ln in hits),
            f"{p}   FIX: correct the manifest/test path;",
            f"{p}   do NOT retry on GPU — the outcome will not change.",
            bar, ""])
    else:
        prev = log_file.with_name(log_file.name + ".prev")
        body = "\n".join([
            "", bar,
            f"{p} SILENT EXIT — no pytest/traceback signal captured",
            f"{p}   rc={rc}  captured_bytes={size}",
            f"{p}   log_file={log_file}",
            f"{p} Likely causes: child SIGKILL (OOM / external kill),",
            f"{p}   stream truncation, or pytest worker crash before",
            f"{p}   buffered stdout was flushed.",
            *([f"{p} Previous attempt's log preserved at: {prev}"]
              if prev.exists() else []),
            bar, ""])
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(body)
    except OSError:
        pass


def strip_cov_flags(command: str) -> str:
    for pat, repl in _COV_STRIP:
        command = pat.sub(repl, command)
    return command


def _record_walk(idmap: dict[int, int], walked: "list[int]", *,
                 root: int, root_parent: int, root_birth: int | None = None,
                 stat_ids: "Callable[[int], tuple[int, int] | None]" = None,
                 ) -> None:
    """Fold one descendant walk into the accumulate-only snapshot map.

    Identity is bound to DISCOVERY: a pid can exit and be reused between the
    pgrep walk and the /proc read, and recording the unrelated new holder's
    (perfectly valid) starttime would launder it past the kill-time identity
    check. One stat read yields (ppid, starttime) atomically, and a pid is
    recorded — or an existing record overwritten — only when it sits on a
    fully VALIDATED ancestry chain: the leader must be parented by the
    runner itself AND (once captured) match its immutable `root_birth` —
    the runner spawns many short-lived children (pgrep, git, the next job),
    so a recycled leader pid can wear the right ppid and only the birth
    time distinguishes the stranger. Every other node's parent must itself
    be validated: merely appearing in the walked set is not enough — when
    an intermediate pid is reused, its stranger children still name a
    "walked" parent, and accepting them would launder the stranger's
    subtree. Unverifiable pids (died mid-walk, reparented in the gap) are
    skipped; earlier records for them survive."""
    from .process_tree import _proc_stat_ids as _default_stat
    stat_ids = stat_ids or _default_stat
    info = {p: stat_ids(p) for p in dict.fromkeys(walked)}
    validated: set[int] = set()
    if (info.get(root) is not None and info[root][0] == root_parent
            and (root_birth is None or info[root][1] == root_birth)):
        validated.add(root)
    # fixpoint over parent links (walk order is not topological)
    changed = True
    while changed:
        changed = False
        for p, ids in info.items():
            if p in validated or ids is None:
                continue
            if ids[0] in validated:
                validated.add(p)
                changed = True
    for p in validated:
        idmap[p] = info[p][1]


def _exec_wrap(command: str) -> str:
    return command if "set +e" in command else f"set -e\n{command}"


def cleanup_test_artifacts(repo_root: Path, globs: list[str]) -> int:
    """Delete well-known per-test artifact files at depth 1 of the repo root
    (never recursive — real fixtures under tests/data must never be touched).
    Patterns are basenames only, like the shell's `find -maxdepth 1 -name`:
    a pattern containing a separator, `..`, or `**` is refused, and every
    match's parent must be the repo root itself."""
    removed = 0
    root = Path(repo_root).resolve()
    if not root.is_dir():
        return 0
    for pattern in globs:
        if os.sep in pattern or "/" in pattern or ".." in pattern \
                or "**" in pattern:
            continue  # adapter data, but never a path expression
        for f in root.glob(pattern):
            if f.is_file() and not f.is_symlink() \
                    and f.resolve().parent == root:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


class TestRunner:
    """Runs `TestJob`s with GPU lock, watchdog, layered timeouts, silent-exit
    postmortems, the cov-strip fallback, artifact cleanup, and pass markers.

    Collaborators are injectable (`available_gpus`, `patterns`, `review_fn`,
    `artifact_globs`) so everything tests offline; `dry_run=True` returns the
    exact `RunPlan` without spawning anything (command-echo parity)."""

    __test__ = False  # "Test" prefix is domain naming, not a pytest class

    # descendant-snapshot cadence; also the bound on the residual window in
    # which a child that spawns and is orphaned can escape the final scan
    SNAPSHOT_INTERVAL = 0.5

    def __init__(self, *, repo_root: Path, tests_dir: Path,
                 patterns: WatchdogPatterns | None = None,
                 review_fn: Callable[[str, str], str] | None = None,
                 record_fn: Callable[[str, str, str], None] | None = None,
                 report_fn: Callable[[str, str, str], None] | None = None,
                 artifact_globs: list[str] | None = None,
                 gpu_lock_dir: Path | None = None,
                 cuda_visible_devices: str = "",
                 available_gpus: Callable[[], int] | None = None,
                 watchdog_interval: float = 10.0,
                 notify_download: Callable[[str, str], None] | None = None):
        self.repo_root = Path(repo_root)
        self.tests_dir = Path(tests_dir)
        self.patterns = patterns
        self.review_fn = review_fn
        self.record_fn = record_fn
        self.report_fn = report_fn
        self.artifact_globs = artifact_globs or []
        self.gpu_lock_dir = gpu_lock_dir
        self.cuda = cuda_visible_devices
        self.available_gpus = available_gpus or (
            lambda: len(visible_devices(self.cuda)))
        self.watchdog_interval = watchdog_interval
        self.notify_download = notify_download

    def run(self, job: TestJob, env: dict[str, str], *, baseline: bool = False,
            dry_run: bool = False) -> TestOutcome:
        log_file = log_file_for(self.tests_dir, job, baseline=baseline)
        plan = RunPlan(
            argv=["bash", "--norc", "-c", _exec_wrap(job.command)],
            env_overlay=dict(job.env), timeout_sec=job.timeout_sec,
            needs_gpu_lock=(job.gpu_lock if job.gpu_lock is not None
                            else job.min_gpus > 0),
            log_file=str(log_file),
            cwd=str(self.repo_root))
        if dry_run:
            return TestOutcome(rc=0, log_file=str(log_file), plan=plan)

        self.tests_dir.mkdir(parents=True, exist_ok=True)
        # a stale marker from an earlier pass must never survive a rerun OR a
        # skip: resume logic reads markers as "this run completed it", so the
        # unlink happens before the hardware gate can return
        marker = pass_marker_for(self.tests_dir, job, baseline=baseline)
        marker.unlink(missing_ok=True)

        # gate on and clean up the devices the child will ACTUALLY see: the
        # effective env is {**env, **job.env}, and either layer may redirect
        # CUDA_VISIBLE_DEVICES away from the runner's own view
        run_env = {**env, **job.env}
        if "CUDA_VISIBLE_DEVICES" in run_env:
            job_cuda = run_env["CUDA_VISIBLE_DEVICES"]
            avail = len(visible_devices(job_cuda))
        else:
            job_cuda = self.cuda
            avail = self.available_gpus()
            if self.cuda:
                # the runner's device selection must reach the child too —
                # gating/cleanup on GPU 0 while the child sees every host
                # GPU would run and leak on devices we never inspect
                run_env["CUDA_VISIBLE_DEVICES"] = self.cuda

        # hw gate: explicit skip, never a silent rc=0 pass
        if avail < job.min_gpus:
            return TestOutcome(
                rc=0, skipped=True, log_file=str(log_file), plan=plan,
                skip_reason=f"needs {job.min_gpus} GPU(s), {avail} available")

        # model-download heads-up (parent parity): a setup that will pull an
        # uncached HF repo can stall a job for an hour — notify ONCE per job
        # (durable marker, parent's filename) before spawning
        if self.notify_download and job.setup:
            repo_id = hf_repo_from_setup(job.setup)
            if repo_id and not hf_repo_cached(repo_id,
                                              run_env.get("HF_HOME", "")):
                sent = self.tests_dir / \
                    f".model_download_email_sent_{job.key}"
                if not sent.exists():
                    try:
                        self.notify_download(job.key, repo_id)
                        sent.touch()
                    except Exception:  # noqa: BLE001 - a broken notifier
                        pass           # must never block the test itself

        backup_prev_log(log_file)

        if job.setup:
            self._run_setup(job, run_env, log_file)

        # The GPU lock spans the primary attempt AND the cov fallback: a
        # missing pytest-cov is exactly the case where the primary exits
        # before touching the GPU and the fallback does the real workload.
        lock: GpuLock | None = None
        if plan.needs_gpu_lock and self.gpu_lock_dir is not None:
            lock = GpuLock(self.gpu_lock_dir).acquire()
        try:
            # append: backup_prev_log already truncated this attempt's log,
            # and truncating again here (shell parity: main command used `>`)
            # would discard the setup output and any setup-failure diagnostic
            try:
                primary_offset = log_file.stat().st_size
            except OSError:
                primary_offset = 0
            rc, wd_hit, timed_out = self._spawn(
                _exec_wrap(job.command), job, run_env, log_file, append=True)
            append_silent_log_footer(log_file, rc, "primary")
            if wd_hit:
                rc = rc or 1

            # cov-strip fallback: only for the specific argparse failure, and
            # only when the PRIMARY attempt printed it — setup output must
            # not turn an unrelated failure into a cov retry and false pass.
            # A fatal primary (watchdog kill, timeout) never qualifies: those
            # outcomes must not be overwritten by a passing fallback.
            if rc != 0 and not wd_hit and not timed_out \
                    and self._log_has(log_file,
                                      r"unrecognized arguments: .*--cov",
                                      from_offset=primary_offset):
                fallback = strip_cov_flags(job.command)
                if fallback != job.command:
                    with open(log_file, "a", encoding="utf-8") as f:
                        f.write("\n[coverage-fallback] Retrying without --cov "
                                f"flags:\n{fallback}\n")
                    rc, wd_hit, timed_out = self._spawn(
                        _exec_wrap(fallback), job, run_env, log_file,
                        append=True)
                    append_silent_log_footer(log_file, rc, "cov-fallback")
                    if wd_hit:
                        rc = rc or 1
        finally:
            if lock is not None:
                cleanup_orphan_gpu_procs(job_cuda)
                wait_gpu_memory_idle(job_cuda)
                lock.release()

        cleanup_test_artifacts(self.repo_root, self.artifact_globs)
        if rc == 0:
            marker.touch()
        return TestOutcome(rc=rc, watchdog_triggered=wd_hit,
                           timed_out=timed_out, log_file=str(log_file),
                           plan=plan)

    # -- internals ------------------------------------------------------------
    def _run_setup(self, job: TestJob, env: dict[str, str],
                   log_file: Path) -> None:
        """Setup is best-effort but every failure mode leaves a diagnostic:
        nonzero rc is logged, a timeout kills the setup's whole process group
        (its own session, so background children can't outlive it and race
        the main test), and none of it ever aborts the job."""
        note = ""
        try:
            with open(log_file, "a", encoding="utf-8") as lf:
                proc = subprocess.Popen(["bash", "--norc", "-c", job.setup],
                                        cwd=self.repo_root, env=env,
                                        stdout=lf, stderr=lf,
                                        start_new_session=True)
                # pgid is captured while the leader is certainly alive: the
                # group must be killable even after the leader exits, or a
                # TERM-ignoring background child survives into the main test
                try:
                    pgid = os.getpgid(proc.pid)
                except ProcessLookupError:
                    pgid = proc.pid
                try:
                    rc = proc.wait(timeout=job.timeout_sec)
                    if rc != 0:
                        note = f"[setup] ignored failure: rc={rc}"
                except subprocess.TimeoutExpired:
                    _kill_group(pgid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                    note = (f"[setup] ignored failure: timeout after "
                            f"{job.timeout_sec}s (process group killed)")
                finally:
                    # unconditional group KILL: the leader exiting promptly
                    # must not spare children that ignored the TERM
                    _kill_group(pgid, signal.SIGKILL)
        except OSError as exc:
            note = f"[setup] ignored failure: {type(exc).__name__}: {exc}"
        if note:
            try:
                with open(log_file, "a", encoding="utf-8") as lf:
                    lf.write(f"\n{note}\n")
            except OSError:
                pass

    def _spawn(self, exec_cmd: str, job: TestJob, env: dict[str, str],
               log_file: Path, *, append: bool) -> tuple[int, bool, bool]:
        timed_out = threading.Event()
        # scope this attempt's watchdog to bytes IT produces — setup output
        # and previous-attempt tails are not this attempt's evidence
        try:
            attempt_offset = log_file.stat().st_size if append else 0
        except OSError:
            attempt_offset = 0
        with open(log_file, "a" if append else "w", encoding="utf-8") as lf:
            proc = subprocess.Popen(["bash", "--norc", "-c", exec_cmd],
                                    cwd=self.repo_root, env=env,
                                    stdout=lf, stderr=lf,
                                    start_new_session=True)
            # capture the pgid while the leader is certainly alive: the final
            # watchdog scan runs after proc.wait(), and a short command that
            # logs a critical line, backgrounds a child, and exits must still
            # have its group killable through the reaped leader's pgid
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = proc.pid
            # a dedicated thread snapshots the live descendant tree on a fast
            # fixed cadence (independent of the 10 s watchdog poll): after
            # the leader is reaped, setsid'd children are reparented to init
            # and only the snapshot still names them. Each walk records
            # (pid, /proc starttime) and the map only ever GROWS: a walk that
            # races the leader's exit sees an already-reparented (empty) tree,
            # and letting it shrink the map is exactly how a setsid'd child
            # escapes the final kill. Reused pids are safe to accumulate —
            # kill_tree drops any pid whose live starttime no longer matches
            # the recorded one. A child that both spawns and is orphaned
            # inside one cadence window is the documented residual (bounded
            # by SNAPSHOT_INTERVAL).
            from .process_tree import collect_descendants
            tree: dict[str, dict[int, int]] = {"idmap": {}}
            own_pid = os.getpid()
            _record_walk(tree["idmap"], [proc.pid],
                         root=proc.pid, root_parent=own_pid)
            # the leader's identity is captured ONCE and immutable from here:
            # later walks must match it exactly or record nothing
            root_birth = tree["idmap"].get(proc.pid)
            stop_snap = threading.Event()

            def _snapshot():
                if proc.poll() is None:
                    _record_walk(tree["idmap"], collect_descendants(proc.pid),
                                 root=proc.pid, root_parent=own_pid,
                                 root_birth=root_birth)

            def _snap_loop():
                while not stop_snap.is_set() and proc.poll() is None:
                    _snapshot()
                    stop_snap.wait(self.SNAPSHOT_INTERVAL)

            snap_thread = threading.Thread(target=_snap_loop, daemon=True)
            snap_thread.start()

            def _kill_snapshot():
                self._terminate_tree(proc.pid, pgid, snapshot=tree["idmap"])

            watchdog = None
            if self.patterns is not None:
                watchdog = LogWatchdog(
                    self.patterns, log_file, proc.pid, job.key,
                    check_interval=self.watchdog_interval,
                    review_fn=self.review_fn, record_fn=self.record_fn,
                    report_fn=self.report_fn, start_offset=attempt_offset,
                    kill_fn=lambda pid: _kill_snapshot()).start()

            def primary():
                timed_out.set()
                _snapshot()  # leader still alive here: refresh before killing
                self._terminate_tree(proc.pid, pgid, snapshot=tree["idmap"])

            def safety():  # only matters if the primary path wedged
                _kill_group(pgid, signal.SIGKILL)

            t_primary = threading.Timer(job.timeout_sec, primary)
            t_safety = threading.Timer(job.timeout_sec + PY_TIMEOUT_MARGIN_SEC,
                                       safety)
            t_primary.start()
            t_safety.start()
            try:
                rc = proc.wait()
            finally:
                stop_snap.set()
                snap_thread.join(timeout=10)
                # cancel then JOIN: a fired primary timer is mid-escalation in
                # its own thread — returning before it finishes would let the
                # next job start while own-pgroup descendants still hold GPUs
                t_primary.cancel()
                t_safety.cancel()
                t_primary.join()
                t_safety.join()
                if watchdog is not None:
                    watchdog.stop()
                    # final scan: a job shorter than the poll interval (or a
                    # line written after the last poll) must still be seen —
                    # "CUDA out of memory" + exit 0 is not a pass
                    watchdog.check_once()
        wd_hit = bool(watchdog is not None and watchdog.result.triggered)
        if timed_out.is_set():
            rc = TIMEOUT_RC
        return rc, wd_hit, timed_out.is_set()

    @staticmethod
    def _terminate_tree(pid: int, pgid: int,
                        snapshot: dict[int, int | None] | None = None) -> None:
        """Kill the leader's process group AND every descendant individually:
        spawn-mode multiprocessing children create their own process groups —
        killpg alone never reaches them, and the leader exiting must not end
        the escalation while they hold GPU memory. `kill_tree` owns
        TERM → grace → KILL per pid, survivors logged. `pgid` is pre-captured
        and `snapshot` carries the accumulated pid → starttime descendant
        map, so this also works after the leader was reaped and its setsid'd
        children were reparented away; the starttimes let kill_tree drop
        pids the kernel has since reused.

        No expansion happens HERE: pre-walking and passing the results to
        kill_tree would promote identity-less walked pids to trusted legacy
        roots, bypassing the ancestry validation. Instead only the
        identity-carrying snapshot pids are handed over as roots, and
        kill_tree performs the single, validated expansion (pre- and
        post-walk root identity checks, ancestry-chained acceptance). With
        no recorded identity at all, escalation is killpg-only."""
        from .process_tree import kill_tree

        snapshot = dict(snapshot or {})
        _kill_group(pgid, signal.SIGTERM)
        if snapshot:
            kill_tree(sorted(snapshot), identity=snapshot)
        _kill_group(pgid, signal.SIGKILL)

    @staticmethod
    def _log_has(log_file: Path, pattern: str, *,
                 from_offset: int = 0) -> bool:
        """Streaming line search (the shell's grep) — never whole-file.
        `from_offset` restricts the scan to one attempt's own region."""
        rx = re.compile(pattern)
        try:
            with open(log_file, encoding="utf-8", errors="replace") as f:
                if from_offset:
                    f.seek(from_offset)
                return any(rx.search(line) for line in f)
        except OSError:
            return False


def _kill_group(pgid: int, sig: int) -> None:
    """killpg by a pre-captured pgid — usable after the leader has exited."""
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass
