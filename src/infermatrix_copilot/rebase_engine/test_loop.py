"""THE local test loop — port of the parent's `run_manifest_test_loop`
(phase 3; phase 4's local-CI fallback reuses it).

Control-flow parity, pinned by tests: per-test resume from checkpointed
progress (this run only — substate is run-stamped); GPU-count skips; a
failure re-runs the SAME test on the main-baseline worktree and requires
matching failure nodes and causes to establish pre-existing failures
(an unavailable worktree must
not mask a regression behind a git error — it is treated as a regression);
regressions go to the debug loop; every transition checkpoints.

One recorded divergence stands (plan §6.8): the PR1 runner produces TYPED
skip outcomes, so the parent's `_shell_skipped` rc=0 heuristic is not
ported — a skip can never masquerade as a pass here by construction.

The three ACTIONS are injected (run-on-rebase, run-on-baseline, debug) —
the assembly step supplies real implementations (TestRunner, worktree,
module agents); the loop owns only the decisions.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence

from .substate import Substate


from .ci_loop import extract_exception_lines, extract_failed_test_ids

log = logging.getLogger(__name__)

def _log_text(output: str) -> str:
    """Return the log CONTENTS for `TestRunResult.output`.

    `output` carries either the log text or a PATH to the log file — the
    production caller (`engine/steps/rebase_v3._to_result`) passes
    `outcome.log_file`. Parsing the pathname finds nothing, so read the file
    when it is one. A read failure must never change the structural
    classification, so it degrades to "no ids found".
    """
    if not output:
        return ""
    if "\n" not in output and len(output) < 4096:
        try:
            candidate = Path(output)
            if candidate.is_file():
                return candidate.read_text(errors="replace")
        except OSError:
            return ""
    return output


def _baseline_reproduces(current: str, baseline: str) -> bool:
    """Require baseline evidence for every current failure and its cause.

    A baseline that cannot import the old source against the new runtime
    has not executed the failing tests and cannot establish old failures.
    """
    current_log, baseline_log = _log_text(current), _log_text(baseline)
    current_tests = extract_failed_test_ids(current_log)
    current_errors = extract_exception_lines(current_log)
    return bool(
        current_tests
        and current_tests <= extract_failed_test_ids(baseline_log)
        and current_errors
        and current_errors <= extract_exception_lines(baseline_log)
    )




@dataclass(frozen=True)
class TestRunResult:
    __test__ = False
    rc: int
    output: str = ""
    skipped: bool = False
    skip_reason: str = ""
    # non-empty = INFRASTRUCTURE failure (timeout, watchdog kill, harness
    # crash) — Rev 8 §2.3 classifies these STRUCTURAL, never as ordinary
    # test-assertion failures, so the loop must keep them out of
    # `failed_tests` (which the push gate passes through flagged)
    infra: str = ""


# run_fn(slug) -> TestRunResult; baseline_fn(slug) -> TestRunResult | None
# (None = baseline worktree unavailable); debug_fn(slug, label, rc, output)
# -> True (fixed) | False (still failing = assertion) | str (STRUCTURAL
# reason: debug backend unavailable/crashed, unverifiable re-run — recorded
# under infra_failures so the push gate blocks instead of passing it
# through as an ordinary test failure)
RunFn = Callable[[str], TestRunResult]
BaselineFn = Callable[[str], "TestRunResult | None"]
DebugFn = Callable[[str, str, int, str], "Awaitable[bool | str]"]


async def run_test_loop(
    jobs: Sequence[Mapping],
    *,
    substate: Substate,
    run_fn: RunFn,
    baseline_fn: BaselineFn,
    debug_fn: DebugFn,
    visible_gpus: int = 0,
    phase_label: str = "Phase 3",
    retry_infra: bool = False,
) -> dict:
    """Returns the parent's shape plus the structural split:
    ``{"passed", "failed", "failed_tests", "skipped_tests",
    "completed_slugs", "infra_failures"}`` — `failed_tests` holds ONLY
    assertion failures; infrastructure failures live in `infra_failures`. Progress lives under the
    substate's ``phase3_progress`` (run-stamped, so a phase-4 fallback in
    the same run resumes past everything phase 3 already ran).
    `retry_infra` (an operator-requested rerun only) runs recorded
    infrastructure failures again instead of keeping them structural."""
    prev = substate.get("phase3_progress", {}) or {}
    prev_completed = set(prev.get("completed", []))
    prev_failed = set(prev.get("failed", []))
    prev_skipped = set(prev.get("skipped", []))
    prev_infra = list(prev.get("infra", []))
    if retry_infra and prev_infra:
        log.info("%s rerun: retrying %d infrastructure failure(s): %s",
                 phase_label, len(prev_infra), "; ".join(prev_infra[:5]))
        prev_infra = []
    if prev_completed or prev_failed or prev_skipped:
        log.info("%s resume: %d passed, %d failed, %d skipped from previous "
                 "attempt", phase_label, len(prev_completed),
                 len(prev_failed), len(prev_skipped))

    passed = failed = 0
    skipped_tests: list[str] = []
    failed_tests: list[str] = []
    completed_slugs: list[str] = []
    infra_failures: list[str] = list(prev_infra)
    infra_slugs = {i.split(":", 1)[0] for i in prev_infra}

    def checkpoint(current: str = "") -> None:
        substate.update({"phase3_progress": {
            "completed": completed_slugs, "failed": failed_tests,
            "skipped": skipped_tests, "infra": infra_failures,
            "current": current}})

    for job in jobs:
        slug = job.get("slug", "unknown")
        label = job.get("label", slug)
        min_gpus = int(job.get("min_gpus", 1))

        if slug in prev_completed:
            log.info("  [RESUME] %s — already passed, skipping", label)
            passed += 1
            completed_slugs.append(slug)
            continue
        if slug in prev_skipped:
            skipped_tests.append(slug)
            continue
        if slug in infra_slugs:
            # a recorded infrastructure failure stays structural on resume —
            # re-running could flip a timeout into a pass and un-block the gate
            failed += 1
            continue

        if visible_gpus > 0 and min_gpus > visible_gpus:
            log.warning("  SKIP: %s needs %d GPUs, only %d visible",
                        label, min_gpus, visible_gpus)
            skipped_tests.append(slug)
            checkpoint(slug)
            continue

        log.info("Running: %s", label)
        checkpoint(slug)
        result = run_fn(slug)
        if result.skipped:
            # typed skip from the runner (hw gate): never a pass, never
            # "completed" — a completed skip would survive resume and
            # inflate the report with tests that never ran (parent-documented)
            log.warning("  SKIPPED (%s): %s", result.skip_reason, label)
            skipped_tests.append(slug)
            checkpoint(slug)
            continue
        if result.rc == 0:
            log.info("  PASSED: %s", label)
            passed += 1
            completed_slugs.append(slug)
            checkpoint()
            continue
        if result.infra:
            # STRUCTURAL (Rev 8 §2.3): timeouts / watchdog kills / harness
            # crashes never enter the baseline-vs-regression split or the
            # assertion pass-through — the push gate blocks on these
            #
            # A structural end does NOT mean the run produced no findings: a
            # suite can report assertion failures and THEN be killed. Those
            # ids were previously discarded with the output, so the report
            # read "1 infra failure" while the log held real regressions
            # (observed 2026-09-19: simple_diffusion_test timed out after 10
            # quantization assertions, all invisible until CI re-found them).
            # Surface them in the record; the classification is unchanged.
            found = sorted(extract_failed_test_ids(_log_text(result.output)))
            detail = f"{slug}: {result.infra}"
            if found:
                shown = ", ".join(found[:5])
                more = f" (+{len(found) - 5} more)" if len(found) > 5 else ""
                detail += (f" — {len(found)} assertion failure(s) reported "
                           f"before the structural end: {shown}{more}")
                log.warning("  INFRA FAILURE (%s) with %d assertion "
                            "failure(s) already reported: %s", result.infra,
                            len(found), shown)
            log.warning("  INFRA FAILURE (%s): %s (rc=%d)", result.infra,
                        label, result.rc)
            infra_failures.append(detail)
            infra_slugs.add(slug)
            failed += 1
            checkpoint()
            continue

        baseline = baseline_fn(slug)
        if baseline is None:
            # infra failure preparing the worktree — do not mask a possible
            # regression behind a git error; treat as a regression
            log.warning("  FAILED on rebase: %s (rc=%d); baseline "
                        "unavailable — treating as REGRESSION", label,
                        result.rc)
        elif baseline.infra:
            # a baseline timeout/kill is NOT evidence the test fails on
            # main — classifying it pre-existing would false-pass a real
            # regression; regression-preserving, like an absent worktree
            log.warning("  baseline INFRA FAILURE (%s) on %s — treating as "
                        "REGRESSION", baseline.infra, label)
        elif baseline.rc != 0 and _baseline_reproduces(
                result.output, baseline.output):
            log.info("  [PRE-EXISTING] %s fails on main too (rebase rc=%d, "
                     "main rc=%d) — skipping, not a regression", label,
                     result.rc, baseline.rc)
            skipped_tests.append(slug)
            checkpoint(slug)
            continue
        elif baseline.rc != 0:
            log.warning("  baseline failed without reproducing every current "
                        "failure and cause on %s — treating as REGRESSION",
                        label)

        log.info("  [REGRESSION] %s has no matching baseline failure — "
                 "debugging", label)
        verdict = await debug_fn(slug, label, result.rc, result.output)
        if verdict is True:
            passed += 1
            completed_slugs.append(slug)
        elif isinstance(verdict, str) and verdict:
            # STRUCTURAL: the debug machinery itself failed (no backend,
            # agent crash, unverifiable re-run) — never an assertion failure
            log.warning("  DEBUG INFRA FAILURE on %s: %s", label, verdict)
            infra_failures.append(f"{slug}: {verdict}")
            infra_slugs.add(slug)
            failed += 1
        else:
            failed_tests.append(slug)
            failed += 1
        checkpoint()

    if failed_tests:
        log.warning("%s: %d test(s) still failing: %s", phase_label,
                    len(failed_tests), ", ".join(failed_tests[:5]))
    return {"passed": passed, "failed": failed,
            "failed_tests": failed_tests, "skipped_tests": skipped_tests,
            "completed_slugs": completed_slugs,
            "infra_failures": infra_failures}


# ── attempt-scoped snapshot/restore (parent `_snapshot_worktree`) ────────────

def _git_out(repo: Path, args: list[str], timeout: int = 60) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:  # noqa: BLE001 - snapshot machinery is best-effort
        return ""


def _remove_path(path: Path) -> None:
    """Remove whatever occupies `path` now (file, symlink, or a directory
    a rejected attempt put where a file used to be)."""
    import shutil
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        try:
            path.unlink()
        except OSError:
            pass


def snapshot_worktree(repo: Path) -> tuple[str, dict[str, dict]]:
    """Snapshot the working tree before a debug attempt.

    Returns ``(snapshot_commit, untracked)``. The snapshot commit is a
    dangling commit from ``git stash create`` capturing all TRACKED files
    (it does NOT modify the worktree or index); a clean tree falls back to
    HEAD. Untracked files map path → {oid, mode, link}: `stash create`
    skips untracked files entirely, so their CONTENT (blob via
    ``hash-object -w``), permission bits, and symlink targets are
    snapshotted here — without them a rejected attempt's edit, chmod, or
    type change to a pre-existing untracked file (e.g. one an earlier
    ACCEPTED fix created) would survive restoration and leak into the
    next commit (2026-08-01 CI-wiring review, rounds 2-3)."""
    import os
    untracked: dict[str, dict] = {}
    for f in _git_out(repo, ["ls-files", "--others",
                             "--exclude-standard"]).splitlines():
        f = f.strip()
        if not f:
            continue
        p = Path(repo) / f
        entry = {"oid": "", "mode": 0, "link": ""}
        try:
            if p.is_symlink():
                entry["link"] = os.readlink(p)
            elif p.is_file():
                entry["oid"] = _git_out(repo, ["hash-object", "-w", "--",
                                               f]).strip()
                entry["mode"] = p.stat().st_mode & 0o777
        except OSError:
            pass
        untracked[f] = entry
    # ``stash create`` must be authoritative for a dirty tracked tree.  A
    # failed command also writes no stdout, which used to look identical to a
    # clean tree and silently fell back to HEAD.  Restoring that fallback
    # erased every accepted edit that predated the rejected debug attempt.
    tracked_status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1",
         "--untracked-files=no"],
        capture_output=True, text=True, timeout=60)
    if tracked_status.returncode != 0:
        raise RuntimeError(
            "cannot inspect tracked worktree before debug snapshot: "
            f"{tracked_status.stderr.strip() or 'git status failed'}")

    stash = subprocess.run(
        ["git", "-C", str(repo), "stash", "create",
         "pre-debug-attempt snapshot"],
        capture_output=True, text=True, timeout=120)
    snap = stash.stdout.strip()
    if tracked_status.stdout.strip():
        if stash.returncode != 0 or not snap:
            raise RuntimeError(
                "cannot snapshot dirty tracked worktree before debug attempt: "
                f"{stash.stderr.strip() or 'git stash create returned no commit'}")
    elif stash.returncode != 0:
        raise RuntimeError(
            "cannot snapshot clean worktree before debug attempt: "
            f"{stash.stderr.strip() or 'git stash create failed'}")
    elif not snap:
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=60)
        if head.returncode != 0 or not head.stdout.strip():
            raise RuntimeError(
                "cannot resolve HEAD for clean debug snapshot: "
                f"{head.stderr.strip() or 'git rev-parse returned no commit'}")
        snap = head.stdout.strip()
    return snap, untracked


def restore_worktree(repo: Path, snap: str,
                     untracked_before: "dict[str, str] | set[str]") -> None:
    """Revert everything a debug attempt changed, back to the snapshot.
    Rejected/unverified fixes must NOT stay in the tree: they pollute
    every later test and debug attempt and eventually get swept into a
    commit by the push staging. Pre-existing untracked files are restored
    by CONTENT from the snapshot blobs (dict form; a legacy set restores
    by presence only). Best-effort — never raises into the test loop."""
    if not snap:
        return
    blobs = untracked_before if isinstance(untracked_before, dict) else {}
    untracked_before = set(untracked_before)
    try:
        changed = [f for f in _git_out(
            repo, ["diff", "--name-only", snap]).splitlines() if f.strip()]
        # split: files the snapshot KNOWS get checked out back; files the
        # attempt CREATED-AND-STAGED do not exist in the snapshot — a single
        # bulk `git checkout <snap> -- <all>` would fail outright (fix over
        # the parent, whose bulk call silently left every tracked edit in
        # place once one staged-new path was present)
        restorable, created = [], []
        for f in changed:
            probe = subprocess.run(
                ["git", "-C", str(repo), "cat-file", "-e", f"{snap}:{f}"],
                capture_output=True, timeout=30)
            (restorable if probe.returncode == 0 else created).append(f)
        if restorable:
            subprocess.run(["git", "-C", str(repo), "checkout", snap,
                            "--", *restorable],
                           capture_output=True, text=True, timeout=120)
            log.info("  Reverted %d file(s) from rejected debug attempt: %s",
                     len(restorable), ", ".join(restorable[:5]))
        for f in created:
            # unstage, then delete unless it predated the attempt
            subprocess.run(["git", "-C", str(repo), "rm", "--cached",
                            "-f", "-q", "--", f],
                           capture_output=True, text=True, timeout=30)
            if f not in untracked_before:
                try:
                    (Path(repo) / f).unlink()
                    log.info("  Removed staged file created by rejected "
                             "debug attempt: %s", f)
                except OSError:
                    pass
        untracked_now = {f for f in _git_out(
            repo, ["ls-files", "--others", "--exclude-standard"])
            .splitlines() if f.strip()}
        for f in sorted(untracked_now - untracked_before):
            try:
                (Path(repo) / f).unlink()
                log.info("  Removed file created by rejected debug "
                         "attempt: %s", f)
            except OSError:
                pass
        # pre-existing untracked files: restore CONTENT, MODE, and TYPE
        # from the snapshot (edited, chmod-ed, deleted, or replaced by a
        # directory/symlink alike)
        import os
        for f, meta in blobs.items():
            if isinstance(meta, str):        # legacy oid-only form
                meta = {"oid": meta, "mode": 0, "link": ""}
            path = Path(repo) / f
            try:
                if meta.get("link"):
                    cur = os.readlink(path) if path.is_symlink() else None
                    if cur == meta["link"]:
                        continue
                    _remove_path(path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(meta["link"], path)
                    log.info("  Restored pre-existing untracked symlink "
                             "after rejected debug attempt: %s", f)
                    continue
                oid = meta.get("oid", "")
                if not oid:
                    continue
                plain_file = path.is_file() and not path.is_symlink()
                cur = _git_out(repo, ["hash-object", "--", f]).strip() \
                    if plain_file else ""
                mode = int(meta.get("mode") or 0)
                mode_now = (path.stat().st_mode & 0o777) if plain_file \
                    else -1
                if cur == oid and (not mode or mode_now == mode):
                    continue
                blob = subprocess.run(
                    ["git", "-C", str(repo), "cat-file", "blob", oid],
                    capture_output=True, timeout=60)
                if blob.returncode != 0:
                    continue
                _remove_path(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(blob.stdout)
                if mode:
                    os.chmod(path, mode)
                log.info("  Restored pre-existing untracked file after "
                         "rejected debug attempt: %s", f)
            except OSError:
                continue
    except Exception as exc:  # noqa: BLE001
        log.warning("  Could not fully restore worktree after rejected "
                    "attempt: %s", exc)


# ── main-baseline worktree helpers ───────────────────────────────────────────

def ensure_main_worktree(repo: Path, worktree_path: Path,
                         base_ref: str = "origin/main") -> Path | None:
    """A detached worktree at the baseline ref for pre-existing-vs-regression
    comparison. Returns None on failure (the loop treats that as
    regression-preserving, never as a silent pass)."""
    repo, worktree_path = Path(repo), Path(worktree_path)
    if (worktree_path / ".git").exists():
        return worktree_path
    r = subprocess.run(["git", "-C", str(repo), "worktree", "add",
                        "--detach", str(worktree_path), base_ref],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        log.warning("main worktree creation failed: %s", r.stderr.strip())
        return None
    return worktree_path


def remove_main_worktree(repo: Path, worktree_path: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force",
                    str(worktree_path)], capture_output=True, text=True,
                   timeout=120)
