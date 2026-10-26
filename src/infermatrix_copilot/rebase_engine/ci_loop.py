"""CI build lifecycle — the guarded-creation/recovery contract (Rev 8 §3.2)
plus the parent monitor's deterministic core (poll loop, no-run terminal
states, per-job retry, baseline root-cause comparison, authoritative final
reconciliation, pure log classifiers — ported verbatim where pure) and the
phase-4 round orchestrator (`run_ci_rounds`: push → settle → adopt-or-create
→ monitor → serialized debug dispatch → retry rounds).

Every build CREATION goes through `create_build_guarded`: a durable
``{op_id, state: intent}`` record precedes the API call, the build is
stamped with ``imx_op_id``/``imx_run_id`` metadata, and crash recovery
matches EXACTLY on that op id — zero matches re-polls boundedly (API
eventual consistency) then ESCALATES; multiple matches ESCALATE; a build is
NEVER re-created on uncertainty. Cancellation/rebuild is only ever allowed
for op-recorded builds.

The HTTP client is injected (a `CIClient` protocol) — the adapter's
provider wiring supplies the real one; everything here tests offline.
The agent-driven CI debug dispatch stays external (assembly wiring), same
as the test loop's debug_fn.
"""

from __future__ import annotations

from ..persistence import atomic_write_bytes, fsync_directory

import asyncio
import calendar
import errno
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

log = logging.getLogger(__name__)

BUILD_NO_RUN_STATES = ("skipped", "not_run")

_DIR_FSYNC_TOLERATED = {errno.EINVAL, errno.EOPNOTSUPP,
                        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
                        errno.EACCES, errno.EPERM, errno.EISDIR, errno.EBADF}


class CIOpError(RuntimeError):
    """The build-op ledger is unusable or the recovery contract escalated."""


class CIClient(Protocol):  # pragma: no cover - structural typing only
    def create_build(self, *, branch: str, commit: str, message: str,
                     meta_data: Mapping[str, str]) -> dict: ...
    def get_build(self, build_id: str) -> dict: ...
    def find_builds_by_meta(self, key: str, value: str) -> list[dict]: ...
    def cancel_build(self, build_id: str) -> dict: ...
    def get_job_log(self, build_id: str, job_id: str) -> str: ...
    def list_jobs(self, build_id: str) -> list[dict]: ...
    def retry_job(self, build_id: str,
                  job_id: str) -> tuple[str | None, bool]: ...
    # adoption / baseline lookups (used by `run_ci_rounds`, not the core
    # monitor): active builds on a branch, siblings at a commit
    def latest_builds(self, branch: str, states: Sequence[str] = (),
                      per_page: int = 30) -> list[dict]: ...
    def builds_for_commit(self, branch: str, commit: str) -> list[dict]: ...


# ── build-op ledger ──────────────────────────────────────────────────────────

@dataclass
class BuildOp:
    op_id: str
    run_id: str
    purpose: str            # gate | initial | retry | rebuild
    branch: str
    commit: str
    state: Literal["intent", "created", "cancelled"] = "intent"
    build_id: str = ""
    build_url: str = ""
    created_at: float = field(default_factory=time.time)

    def path(self, ops_dir: Path) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", self.op_id):
            raise CIOpError(f"unsafe op_id for a ledger filename: "
                            f"{self.op_id!r}")
        return Path(ops_dir) / f"{self.op_id}.json"


def _durable_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, json.dumps(payload, indent=1, sort_keys=True).encode("utf-8"),
                       directory_fsync=_sync_directory)


def _sync_directory(directory):
    try:
        fsync_directory(directory)
    except OSError as e:
        if e.errno not in _DIR_FSYNC_TOLERATED:
            raise CIOpError("build-op ledger fsync failed — durability "
                            "cannot be guaranteed") from e


def load_ops(ops_dir: Path) -> list[BuildOp]:
    out: list[BuildOp] = []
    ops_dir = Path(ops_dir)
    if not ops_dir.is_dir():
        return out
    for p in sorted(ops_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            op = BuildOp(**data)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as e:
            raise CIOpError(f"corrupt build-op record {p}: {e}") from e
        if op.state not in ("intent", "created", "cancelled"):
            raise CIOpError(f"corrupt build-op record {p}: "
                            f"unknown state {op.state!r}")
        out.append(op)
    return out


def create_build_guarded(client: CIClient, ops_dir: Path, *,
                         op_id: str, run_id: str, purpose: str,
                         branch: str, commit: str, message: str,
                         repoll_attempts: int = 3,
                         repoll_delay: float = 60.0,
                         sleep: Callable[[float], None] = time.sleep) -> BuildOp:
    """Durable intent → API create (op-id stamped) → durable created.

    Re-entry with an existing intent record RECOVERS instead of re-creating:
    the provider is searched for a build carrying exactly this op id; one
    match adopts it; zero matches re-poll `repoll_attempts` times (eventual
    consistency) then ESCALATE; multiple matches ESCALATE. Uncertainty never
    creates a second build."""
    op = BuildOp(op_id=op_id, run_id=run_id, purpose=purpose,
                 branch=branch, commit=commit)
    path = op.path(ops_dir)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        existing = BuildOp(**data)
        # an op id names ONE operation: re-entry with different parameters
        # is a caller bug — adopting the old build under new intent would
        # silently monitor the wrong world
        mismatch = [f"{k}: {getattr(existing, k)!r} != {v!r}"
                    for k, v in (("run_id", run_id), ("purpose", purpose),
                                 ("branch", branch), ("commit", commit))
                    if getattr(existing, k) != v]
        if mismatch:
            raise CIOpError(
                f"build-op {op_id}: recorded identity differs from this "
                f"request ({'; '.join(mismatch)}) — op ids are single-use, "
                "refusing to adopt")
        if existing.state == "created" and existing.build_id:
            return existing
        if existing.state != "intent":
            # cancelled (or any other terminal) op is CONSUMED — recovery
            # must never resurrect it to "created"
            raise CIOpError(
                f"build-op {op_id}: state {existing.state!r} is terminal — "
                "a new operation needs a new op id")
        # crash between intent and acknowledgment: recover by exact op id
        for attempt in range(1, max(1, repoll_attempts) + 1):
            matches = client.find_builds_by_meta("imx_op_id", op_id)
            if len(matches) == 1:
                existing.state = "created"
                existing.build_id = str(matches[0].get("id", ""))
                existing.build_url = str(matches[0].get("web_url", ""))
                _durable_write(path, asdict(existing))
                return existing
            if len(matches) > 1:
                raise CIOpError(
                    f"build-op {op_id}: {len(matches)} builds carry this op "
                    "id — human review required, refusing to guess")
            if attempt < repoll_attempts:
                sleep(repoll_delay)
        raise CIOpError(
            f"build-op {op_id}: intent recorded but no build carries the op "
            "id after re-polling — the create may have half-happened; "
            "human review required, refusing to re-create")

    _durable_write(path, asdict(op))
    build = client.create_build(
        branch=branch, commit=commit, message=message,
        meta_data={"imx_op_id": op_id, "imx_run_id": run_id})
    op.state = "created"
    op.build_id = str(build.get("id", ""))
    op.build_url = str(build.get("web_url", ""))
    _durable_write(path, asdict(op))
    return op


def cancel_build_guarded(client: CIClient, ops_dir: Path, op_id: str) -> bool:
    """Cancellation is allowed ONLY for op-recorded builds (never a build
    this run cannot prove it created)."""
    for op in load_ops(ops_dir):
        if op.op_id == op_id and op.state == "created" and op.build_id:
            client.cancel_build(op.build_id)
            op.state = "cancelled"
            _durable_write(op.path(ops_dir), asdict(op))
            return True
    return False


# ── monitor core ─────────────────────────────────────────────────────────────

PENDING_JOB_STATES = ("running", "scheduled", "waiting", "assigned",
                      "accepted", "limited")
_TERMINAL_BUILD_STATES = ("passed", "failed", "canceled", "cancelled",
                          "blocked")


@dataclass
class JobResult:
    name: str
    job_id: str
    state: str
    exit_status: int = 0
    classification: str = ""       # passed|failed|ignored|ignored_baseline|
    #                                budget_timeout|incomplete|retrying
    log_file: str = ""
    classification_reason: str = ""
    baseline_build_id: str = ""
    baseline_job_id: str = ""


@dataclass
class MonitorOutcome:
    build_state: str
    jobs: list[JobResult] = field(default_factory=list)
    # False when the authoritative final job list could not be retrieved
    # at all — retrieval failure is NOT an empty job list (round-2 review)
    reconciled: bool = True

    @property
    def no_run(self) -> bool:
        return self.build_state in BUILD_NO_RUN_STATES

    @property
    def failed_jobs(self) -> list[JobResult]:
        return [j for j in self.jobs if j.classification == "failed"]

    @property
    def incomplete_jobs(self) -> list[JobResult]:
        """Jobs that never reached a per-job terminal state inside a
        terminal build (running/waiting/canceled/blocked/unknown) —
        STRUCTURAL, never counted as passed."""
        return [j for j in self.jobs if j.classification == "incomplete"]

    @property
    def budget_timeouts(self) -> list[JobResult]:
        """timed_out jobs killed by their pipeline minutes budget while
        tests were still passing — an operator problem (raise the budget /
        split the job), never a code-debug dispatch, but their remaining
        tests never ran, so they still block a pass."""
        return [j for j in self.jobs if j.classification == "budget_timeout"]

    @property
    def clean_pass(self) -> bool:
        """True only when there is nothing left to act on or wait for.
        Baseline-matched failures still count as clean (`ignored_baseline`);
        unresolved failures, never-finished jobs, budget kills, a failed
        final reconciliation, or a build that produced NO job signal at
        all never do — and only a build that RAN TO COMPLETION
        (`passed`, or `failed` with every failure explained away) can be
        clean: a canceled/blocked/refused build may have prevented later
        dynamic jobs from ever appearing, so its visible green jobs prove
        nothing (round-3 review). At least one job must have actually
        PASSED — a build whose every job was ignored proves nothing either
        (round-4 review: a failed suite-creation step plus ignores must
        not read green)."""
        return (self.build_state in ("passed", "failed")
                and self.reconciled
                and any(j.classification == "passed" for j in self.jobs)
                and not self.failed_jobs
                and not self.incomplete_jobs and not self.budget_timeouts)


@dataclass(frozen=True)
class BaselineFailure:
    """One pre-existing failure on the baseline build. `name` is lowered;
    `exit_status` uses the parent's `or -1` transform (0/None ⇒ -1) so both
    sides of the match compare the same encoding."""

    name: str
    exit_status: int
    job_id: str = ""
    build_id: str = ""


@dataclass(frozen=True)
class CIClassifySpec:
    """Adapter data: which job names/log contents are ignorable, which
    exception names extend the error-signature grammar, and the baseline of
    pre-existing failures on the default branch.

    `structural_name_patterns` name the SUITE-CREATION lanes (pipeline
    upload/load steps): their failure suppresses every dynamic test job,
    so it is STRUCTURAL (incomplete), never ignorable (round-4 review —
    a deliberate divergence from the parent, which ignored them).

    `baseline_log_fn` fetches a BASELINE job's log — the baseline lives on
    a DIFFERENT pipeline than the build under test, so the monitor's own
    client cannot resolve its build ids (round-2 review: asking the wrong
    pipeline 404s into the lenient same-cause path). None = the baseline
    coordinates resolve on the same client (single-pipeline setups)."""

    ignorable_name_patterns: Sequence[str] = ()
    ignorable_log_patterns: Sequence[str] = ()
    structural_name_patterns: Sequence[str] = ()
    extra_exception_names: Sequence[str] = ()
    baseline: Sequence[BaselineFailure] = ()
    baseline_log_fn: Callable[[str, str], str] | None = None


def _save_job_log(log_dir, name: str, text: str) -> str:
    if not log_dir or not text:
        return ""
    safe = re.sub(r"[ /.:&()]", "_", name)
    path = Path(log_dir) / f"{safe}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        return ""
    return str(path)


def _baseline_matches(spec: CIClassifySpec, name: str,
                      exit_status: int) -> tuple[BaselineFailure, ...]:
    """Same-name baseline candidates, preferring the same provider exit code.

    Exit status is supporting evidence, not failure identity: a real pytest
    failure can be followed by a teardown timeout and therefore land as -1 on
    main but 1 on the branch. Root-cause comparison below remains mandatory.
    """
    lower = name.lower()
    coded = exit_status or -1
    matches = [bf for bf in spec.baseline if bf.name == lower]
    return tuple(sorted(matches, key=lambda bf: bf.exit_status != coded))


def _same_root_cause(client: CIClient, entry: BaselineFailure,
                     log_text: str, spec: CIClassifySpec, log_dir) -> str:
    """Root-cause comparison against the baseline job's log: identical
    normalized error signatures, or cause-correlated pytest node-id subsets,
    mean the same pre-existing failure. The returned string is durable
    evidence for the classification; an empty string means no safe match.

    FAIL-CLOSED on missing evidence (round-4 review — a deliberate
    divergence from the parent's lenient defaults): missing baseline
    coordinates, an unavailable baseline log (log retrieval degrades to
    "" during API outages), or an empty current signature all mean the
    comparison CANNOT be made — the failure stays actionable rather than
    being waved through as pre-existing."""
    if not entry.job_id or not entry.build_id:
        return ""
    fetch = spec.baseline_log_fn or client.get_job_log
    main_log = fetch(entry.build_id, entry.job_id)
    if not main_log:
        return ""
    _save_job_log(log_dir,
                  f"baseline_{entry.build_id}_{entry.job_id}_{entry.name}",
                  main_log)
    cur_sig = extract_error_signature(log_text or "",
                                      spec.extra_exception_names)
    if not cur_sig:
        return ""
    main_sig = extract_error_signature(main_log, spec.extra_exception_names)
    if cur_sig == main_sig:
        return "identical normalized error signature"
    cur_tests = extract_failed_test_ids(log_text or "")
    main_tests = extract_failed_test_ids(main_log)
    if not cur_tests or not cur_tests <= main_tests:
        return ""
    cur_errors = extract_exception_lines(log_text, spec.extra_exception_names)
    main_errors = extract_exception_lines(main_log,
                                          spec.extra_exception_names)
    if cur_errors & main_errors:
        return ("current pytest failure/error nodes are a cause-matched "
                "subset of the baseline")
    return ""


def _classify_terminal(client: CIClient, build_id: str, job: dict,
                       spec: CIClassifySpec, log_dir) -> JobResult:
    """One job's deterministic classification (shared by the poll loop and
    the final reconciliation; the poll loop may convert a `failed` verdict
    into `retrying` while retry budget remains)."""
    name = str(job.get("name", "") or "")
    raw_exit = job.get("exit_status")
    jr = JobResult(name=name, job_id=str(job.get("id", "")),
                   state=str(job.get("state", "")),
                   exit_status=int(raw_exit or 0))
    passed_like = jr.state == "passed" or (jr.state == "finished"
                                           and raw_exit == 0)
    if jr.state not in ("passed", "finished", "failed", "timed_out",
                        "broken") \
            or (jr.state == "finished" and raw_exit is None):
        # a terminal BUILD can still carry non-terminal or torn JOBS
        # (running, waiting, canceled, blocked, unknown, or a finished
        # job whose exit_status never landed) — a missing exit code is
        # NOT zero; incomplete is structural, never "passed". Checked
        # FIRST: an UNFINISHED job is unfinished work even when its name
        # would be ignorable once terminal (round-5 review — a pending
        # stats lane at the deadline must not read `ignored`)
        jr.classification = "incomplete"
    elif not passed_like and any(re.search(p, name)
                                 for p in spec.structural_name_patterns):
        # a failed SUITE-CREATION lane (pipeline upload/load) suppressed
        # every dynamic test job — structural, never ignorable (round-4
        # review)
        jr.classification = "incomplete"
    elif any(re.search(p, name) for p in spec.ignorable_name_patterns):
        jr.classification = "ignored"
    elif jr.state == "broken":
        # the provider could not start the job (unmet condition, missing
        # agent, config error) — infra, not a test failure (parent parity)
        jr.classification = "ignored"
    elif jr.state == "passed" or jr.exit_status == 0 and \
            jr.state not in ("failed", "timed_out"):
        jr.classification = "passed"
    elif job.get("soft_failed"):
        # explicitly non-blocking pipeline step (parent parity)
        jr.classification = "ignored"
    else:
        log_text = client.get_job_log(build_id, jr.job_id)
        jr.log_file = _save_job_log(log_dir, name, log_text)
        if any(re.search(p, log_text)
               for p in spec.ignorable_log_patterns):
            jr.classification = "ignored"
        elif jr.state == "timed_out" and is_budget_timeout(log_text):
            jr.classification = "budget_timeout"
        else:
            match = next(((entry, reason)
                          for entry in _baseline_matches(
                              spec, name, jr.exit_status)
                          if (reason := _same_root_cause(
                              client, entry, log_text, spec, log_dir))), None)
            if match is not None:
                entry, reason = match
                jr.classification = "ignored_baseline"
                jr.classification_reason = reason
                jr.baseline_build_id = entry.build_id
                jr.baseline_job_id = entry.job_id
            else:
                jr.classification = "failed"
    return jr


def monitor_build(client: CIClient, build_id: str, *,
                  spec: CIClassifySpec,
                  poll_sec: float = 60.0,
                  timeout_sec: float = 4 * 3600,
                  retry_max: int = 0,
                  log_dir=None,
                  sleep: Callable[[float], None] = time.sleep,
                  now: Callable[[], float] = time.monotonic) -> MonitorOutcome:
    """Poll a build to terminal, classifying jobs as they finish.

    Parent-parity semantics: no-run build states are TERMINAL immediately
    (never treated as failure or success — the caller escalates); a
    terminal build with still-pending jobs keeps being polled until every
    job lands or the deadline passes; a would-be `failed` job is retried up
    to `retry_max` times per job name (flaky recovery) before it counts;
    after the loop, the authoritative job list is re-fetched and anything
    the incremental poller missed (dynamically added steps, stale build
    snapshots, a hung job) is classified — never-finished jobs are
    `incomplete`, so a partially-run build can never read as passed."""
    deadline = now() + timeout_sec
    jobs_out: list[JobResult] = []
    processed: set[str] = set()
    retries_by_name: dict[str, int] = {}
    # retry-attempt job id → the ORIGINAL JobResult marked `retrying`
    expected_retries: dict[str, JobResult] = {}
    build_state = ""
    while True:
        build = client.get_build(build_id) or {}
        state = str(build.get("state", ""))
        if state:
            build_state = state
        if build_state in BUILD_NO_RUN_STATES:
            # the pipeline decided this build will NEVER run (branch
            # filter, skip-intermediate, duplicate) — polling is waste and
            # its jobs prove nothing
            return MonitorOutcome(build_state=build_state)
        jobs = [j for j in (build.get("jobs") or []) if isinstance(j, dict)]
        for job in jobs:
            jid = str(job.get("id", ""))
            name = str(job.get("name", "") or "")
            jstate = str(job.get("state", ""))
            if not name or jid in processed:
                continue
            if job.get("type") not in (None, "script"):
                # waiter/trigger/manual gate steps are pipeline plumbing —
                # skipped here exactly as the final reconciliation skips
                # them (verification round 2: classifying them only in
                # the poll path dispatched debug agents at non-jobs)
                continue
            if jstate in PENDING_JOB_STATES:
                continue
            processed.add(jid)
            expected_retries.pop(jid, None)
            jr = _classify_terminal(client, build_id, job, spec, log_dir)
            if jr.classification == "failed" and \
                    retries_by_name.get(name, 0) < retry_max:
                retries_by_name[name] = retries_by_name.get(name, 0) + 1
                try:
                    new_id, retryable = client.retry_job(build_id, jid)
                except Exception:  # noqa: BLE001 - provider API failure
                    # we could neither retry nor rule flakiness out —
                    # STRUCTURAL (incomplete), never a mutation dispatch
                    # and never a silent pass (round-1 review)
                    jr.classification = "incomplete"
                    new_id, retryable = None, True
                if new_id:
                    jr.classification = "retrying"
                    expected_retries[str(new_id)] = jr
                # a non-retryable TYPE (HTTP 400) changes nothing: the
                # failure stands as `failed` — the parent's ignored_infra
                # here was a false-green vector (round-4 review)
            jobs_out.append(jr)
        pending = sum(1 for j in jobs
                      if str(j.get("state", "")) in PENDING_JOB_STATES
                      and j.get("type") in (None, "script"))
        pending += sum(1 for rid in expected_retries
                       if rid not in processed)
        if build_state in _TERMINAL_BUILD_STATES and pending == 0:
            break
        if now() > deadline:
            if build_state not in _TERMINAL_BUILD_STATES:
                build_state = "monitor_timeout"
            break
        sleep(poll_sec)

    # final reconciliation: the poll loop's get_build snapshots can lag or
    # drop dynamically-added steps — re-fetch the authoritative list and
    # classify everything it missed so a partial build never reads passed
    final = client.get_build(build_id) or {}
    if str(final.get("state", "")) and build_state != "monitor_timeout":
        build_state = str(final.get("state", ""))
    reconciled = True
    try:
        auth = client.list_jobs(build_id)
    except Exception:  # noqa: BLE001 - degrade to the embedded snapshot
        auth = [j for j in (final.get("jobs") or []) if isinstance(j, dict)]
        if not final:
            # neither the jobs endpoint nor the build itself was readable:
            # the reconciliation contract (nothing missed) cannot be
            # honored, so the outcome must not read clean
            reconciled = False
    for job in auth:
        jid = str(job.get("id", ""))
        name = str(job.get("name", "") or "")
        if not name or jid in processed:
            continue
        if job.get("type") not in (None, "script"):
            continue
        if job.get("retried"):
            # the retry attempt is a separate job in this same list
            continue
        processed.add(jid)
        jobs_out.append(_classify_terminal(client, build_id, job, spec,
                                           log_dir))
    # a retry attempt we requested but NEVER observed is unfinished work:
    # its original must not linger as `retrying` (which clean_pass would
    # wave through) — it is structurally incomplete (round-1 review)
    for rid, orig in expected_retries.items():
        if rid not in processed and orig.classification == "retrying":
            orig.classification = "incomplete"
    return MonitorOutcome(build_state=build_state, jobs=jobs_out,
                          reconciled=reconciled)


# ── pure log classifiers (parent-verbatim) ───────────────────────────────────

_BK_TS_RE = re.compile(r"\x1b_bk;t=(\d+)\x07")
_ISO_TS_PARSE_RE = re.compile(
    r"\[(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z?\]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_CANCEL_MARKERS = (
    "Received cancellation signal",
    "Exceeded maximum job timeout",
    "The command was interrupted by a signal",
)
_BK_MARKER_RE = re.compile(r"\x1b?_bk;t=\d+\x07?")
_ISO_TS_RE = re.compile(r"\[\d{4}-\d{2}-\d{2}T[\d:.]+Z?\]\s*")
_FLOAT_RE = re.compile(r"\d+\.\d+")


def _log_timestamps(segment: str) -> list[float]:
    ts = [int(m.group(1)) / 1000.0 for m in _BK_TS_RE.finditer(segment)]
    if ts:
        return ts
    out: list[float] = []
    for m in _ISO_TS_PARSE_RE.finditer(segment):
        y, mo, d, h, mi, s = (int(g) for g in m.groups())
        out.append(float(calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))))
    return out


def is_budget_timeout(log_text: str) -> bool:
    """A timed_out job killed by its minutes budget while tests were still
    PASSING (needs a bigger budget or a job split, never a code-debug agent):
    cancellation banner present, ≥1 PASSED and no FAILED before the kill,
    and output within 5 min of the kill (a hang shows a long silent gap)."""
    if not log_text:
        return False
    cancel_pos = -1
    for marker in _CANCEL_MARKERS:
        cancel_pos = log_text.find(marker)
        if cancel_pos != -1:
            break
    if cancel_pos == -1:
        return False
    body = _ANSI_RE.sub("", log_text[:cancel_pos])
    if not re.search(r"\bPASSED\b", body):
        return False
    if re.search(r"\bFAILED\b", body):
        return False
    kill_ts = _log_timestamps(log_text)
    if not kill_ts:
        return True
    output_ts = _log_timestamps(log_text[:max(0, cancel_pos - 40)])
    if not output_ts:
        return False
    return (max(kill_ts) - output_ts[-1]) <= 300.0


def normalize_log_line(line: str) -> str:
    """Strip run-specific noise so the same failure compares equal."""
    line = _BK_MARKER_RE.sub("", line)
    line = _ANSI_RE.sub("", line)
    line = _ISO_TS_RE.sub("", line)
    line = _FLOAT_RE.sub("<N>", line)
    return line.strip()


def extract_failed_test_ids(log_text: str) -> set[str]:
    """Pytest node ids from terminal FAILED and setup/teardown ERROR lines."""
    ids: set[str] = set()
    for line in log_text.split("\n"):
        if not any(token in line for token in ("FAILED", "ERROR")) \
                or "::" not in line:
            continue
        m = re.search(r"([\w./-]+\.py::[\w:]+(?:\[[^\]]*\])?)",
                      normalize_log_line(line))
        if m:
            ids.add(m.group(1))
    return ids


def extract_exception_lines(
        log_text: str,
        extra_exception_names: Sequence[str] = ()) -> set[str]:
    """All normalized exception lines used to corroborate pytest node matches."""
    base = (r"RuntimeError|AssertionError|TypeError|ValueError"
            r"|ModuleNotFoundError|ImportError|CalledProcessError"
            r"|torch\.OutOfMemoryError|CUDA out of memory"
            r"|exit status|SyntaxError|KeyError|AttributeError")
    names = "|".join([base, *map(re.escape, extra_exception_names)])
    rx = re.compile(rf".*({names}).*")
    return {line for line in (
        normalize_log_line(raw) for raw in log_text.split("\n"))
        if line and rx.match(line)}


def extract_error_signature(log_text: str,
                            extra_exception_names: Sequence[str] = ()) -> str:
    """Key error lines, normalized so two runs of one failure compare equal.
    Repo-specific exception class names arrive as adapter data."""
    lines = log_text.split("\n")
    exception_lines = extract_exception_lines(log_text,
                                               extra_exception_names)
    sig_lines = [n for n in (normalize_log_line(ln) for ln in lines)
                 if n in exception_lines]
    failures = [normalize_log_line(ln) for ln in lines
                if "FAILED" in ln and "::" in ln]
    result = sig_lines[-3:] if sig_lines else []
    if failures:
        result.extend(failures[-3:])
    return "\n".join(result[-8:])


# ── baseline + build selection ───────────────────────────────────────────────

_ACTIVE_BUILD_STATES = ("scheduled", "running", "failing")


def pick_best_build(builds: Sequence[dict]) -> dict | None:
    """Best baseline candidate: scheduled > api > completed > newest
    (parent parity — scheduled/API builds run the full suite; manual and
    webhook builds may be partial retries). Falls back to the newest build
    when nothing scores."""
    scored: list[tuple[int, int, dict]] = []
    for i, b in enumerate(builds):
        source = str(b.get("source", ""))
        state = str(b.get("state", ""))
        score = 0
        if source == "schedule":
            score = 3
        elif source == "api":
            score = 2
        elif state in ("passed", "failed"):
            score = 1
        # index 0 is newest (providers return newest first); ties → newer
        scored.append((score, -i, b))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored[0][2]


def fetch_baseline_failures(client: CIClient, branch: str, *,
                            window: int = 5) -> tuple[BaselineFailure, ...]:
    """Pre-existing failures on the baseline pipeline's `branch`, with
    coordinates so the monitor can root-cause-compare logs. The CLIENT is
    scoped to the BASELINE pipeline (adapter data), not the
    build-under-test's.

    Use a window from the best available trust tier (schedule > api > other).
    A failure from the newest trusted build is current baseline evidence.
    Older failures survive a newer green only when they recur in at least two
    builds, distinguishing an intermittent baseline from one stale red run.
    """
    if window < 1:
        raise ValueError("baseline window must be positive")
    builds = client.latest_builds(branch, states=("passed", "failed"),
                                  per_page=10)
    if not builds:
        return ()
    def trust(build: dict) -> int:
        source = str(build.get("source", ""))
        if source == "schedule":
            return 3
        if source == "api":
            return 2
        return 1

    tier = max(map(trust, builds))
    selected = [b for b in builds if trust(b) == tier][:window]
    failures: dict[str, list[tuple[int, BaselineFailure]]] = {}
    for index, build in enumerate(selected):
        build_id = str(build.get("id", ""))
        for job in client.list_jobs(build_id):
            if str(job.get("state", "")) not in (
                    "failed", "broken", "timed_out"):
                continue
            name = str(job.get("name", "") or "").lower()
            if not name:
                continue
            raw = job.get("exit_status", -1)
            failure = BaselineFailure(
                name=name, exit_status=int(raw or -1),
                job_id=str(job.get("id", "")), build_id=build_id)
            failures.setdefault(name, []).append((index, failure))

    out: list[BaselineFailure] = []
    for entries in failures.values():
        if entries[0][0] == 0 or len(entries) >= 2:
            out.extend(failure for _, failure in entries)
    return tuple(out)


# ── phase-4 round orchestrator ───────────────────────────────────────────────

@dataclass
class CIBuildRound:
    purpose: str            # initial | retry | rebuild | adopted
    op_id: str = ""         # empty for adopted builds (monitor-only)
    build_id: str = ""
    build_url: str = ""
    adopted: bool = False
    build_state: str = ""
    passed: int = 0
    failed: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    budget_timeouts: list[str] = field(default_factory=list)
    ignored: int = 0
    ignored_baseline: int = 0
    jobs: list[dict] = field(default_factory=list)
    monitor_duration_sec: float | None = None


@dataclass
class CIRunResult:
    # passed | failed | no_signal | push_failed | push_dry_run | refused
    result: str
    reason: str = ""
    rounds: list[CIBuildRound] = field(default_factory=list)
    fixed_jobs: list[str] = field(default_factory=list)
    unfixed_jobs: list[str] = field(default_factory=list)


def _record_outcome(rec: CIBuildRound, outcome: MonitorOutcome) -> None:
    rec.build_state = outcome.build_state
    rec.passed = sum(1 for j in outcome.jobs
                     if j.classification == "passed")
    rec.failed = [j.name for j in outcome.failed_jobs]
    rec.incomplete = [j.name for j in outcome.incomplete_jobs]
    rec.budget_timeouts = [j.name for j in outcome.budget_timeouts]
    rec.ignored = sum(1 for j in outcome.jobs
                      if j.classification == "ignored")
    rec.ignored_baseline = sum(1 for j in outcome.jobs
                               if j.classification == "ignored_baseline")
    rec.jobs = [asdict(j) for j in outcome.jobs]


def op_index_base(ops: Sequence[BuildOp], run_id: str) -> int:
    """The next free `-ci-r<N>` index for this run — derived from the
    DURABLE ledger so a resumed invocation never reuses an op id that
    already names a different operation (round-1 review: op ids are
    single-use identities; a crash after round 0 must not restart at r0
    with a new HEAD)."""
    rx = re.compile(re.escape(run_id) + r"-ci-r(\d+)")
    idxs = [int(m.group(1)) for o in ops
            if (m := rx.fullmatch(o.op_id)) is not None]
    return max(idxs) + 1 if idxs else 0


def _foreign_active(active: Sequence[dict], owned_commits: set[str],
                    owned_ids: set[str]) -> dict | None:
    """The first active build this run does NOT own. Owned means:
    op-recorded (`owned_ids`) or a WEBHOOK build at a commit we pushed —
    that one is our own push's artifact. Nothing else is exempt
    (verification round 2): a schedule/api build merely sharing a commit
    we pushed is somebody else's work (those are ADOPTED before this
    check when they qualify), and a same-commit UI/manual/trigger build
    is neither evidence nor ours to cancel."""
    for b in active:
        if str(b.get("state", "")) in BUILD_NO_RUN_STATES:
            continue
        commit = str(b.get("commit", ""))
        if str(b.get("id", "")) in owned_ids:
            continue
        if commit in owned_commits and \
                str(b.get("source", "")) == "webhook":
            continue
        return b
    return None


class BuildCreationRefused(RuntimeError):
    """A provider REFUSED build creation as POLICY (deterministic 4xx -
    e.g. Buildkite 422 "Branches have been disabled for this pipeline"
    when the org runs the pipeline schedule-only). Distinct from
    transport errors: retrying can never succeed, so the round converts
    it into the structured `refused` outcome instead of crashing the
    step as an unhandled error (live remote_ci launch 2026-08-23)."""


def _foreign_refusal(b: dict, branch: str) -> str:
    return (f"an active build (#{b.get('number', '?')}, state "
            f"{b.get('state', '?')}, commit "
            f"{str(b.get('commit', ''))[:12]}) exists on {branch} at a "
            "different commit — a push or new build would cancel it "
            "(cancel-running-branch-builds); refusing to mutate a build "
            "this run does not own")


def _acquire_build(client: CIClient, ops_dir: Path, *, run_id: str,
                   round_idx: int, purpose: str, branch: str, commit: str,
                   message: str, sleep: Callable[[float], None],
                   owned_commits: set[str] | None = None,
                   owned_ids: set[str] | None = None
                   ) -> tuple[CIBuildRound | None, str]:
    """Create (or, on a rebuild round, adopt) with the parent's cancel-race
    safety rules:

    - an ACTIVE build on the branch at a DIFFERENT commit refuses creation
      outright — with cancel-running-branch-builds pipeline semantics our
      new build would cancel a build we do not own;
    - an active FULL-SUITE (schedule/api) build at the SAME commit is
      ADOPTED (monitor-only) on every round purpose instead of duplicated
      — we never cancel or rebuild a build we did not create;
    - after a fresh push (`initial`/`retry`), we deliberately create our
      own build even if the push's WEBHOOK build is active at the commit:
      only OUR build carries the trigger env's opt-in suite, and by the
      settle-then-create ordering ours is the newest build, which the
      pipeline's skip rules keep (the webhook one is skipped as older —
      our own push's artifact either way).

    Creation itself goes through the §3.2 guarded op ledger — and BEFORE
    any new operation, THIS run's unresolved prior ops are settled
    (round-2 review): an `intent` record is recovered under its own op id
    (adopt by metadata / bounded repoll / escalate — never a fresh index
    while a creation is uncertain), and a `created` op whose build is
    still ACTIVE at this commit is re-attached (crash-during-monitor /
    monitor-timeout resume) instead of duplicated."""
    owned_commits = owned_commits if owned_commits is not None else set()
    owned_ids = owned_ids if owned_ids is not None else set()
    for op in load_ops(ops_dir):
        if op.run_id != run_id:
            continue
        if op.state == "intent":
            recovered = create_build_guarded(
                client, ops_dir, op_id=op.op_id, run_id=run_id,
                purpose=op.purpose, branch=op.branch, commit=op.commit,
                message=message, sleep=sleep)
            if op.commit == commit:
                return CIBuildRound(purpose=op.purpose, op_id=op.op_id,
                                    build_id=recovered.build_id,
                                    build_url=recovered.build_url), ""
            # settled, but the checkout ADVANCED since the crash: the
            # recovered build tests a stale commit — its green result
            # must never approve the commit we just pushed (verification
            # round 2). It is op-recorded (ours): cancel it if still
            # active, then fall through to a fresh operation.
            state = str(client.get_build(recovered.build_id)
                        .get("state", ""))
            if state in _ACTIVE_BUILD_STATES:
                cancel_build_guarded(client, ops_dir, op.op_id)
            continue
        if op.state == "created" and op.build_id:
            state = str(client.get_build(op.build_id).get("state", ""))
            if state not in _ACTIVE_BUILD_STATES:
                continue
            if op.commit == commit:
                return CIBuildRound(purpose=op.purpose, op_id=op.op_id,
                                    build_id=op.build_id,
                                    build_url=op.build_url), ""
            # our own still-active build at a STALE commit (crash during
            # monitor, then the tree advanced): cancel it — op-recorded
            # cancellation is ours to perform — before building the
            # current commit, or the pipeline's cancel-running semantics
            # would race it anyway
            cancel_build_guarded(client, ops_dir, op.op_id)
    active = [b for b in client.latest_builds(branch,
                                              states=_ACTIVE_BUILD_STATES)
              if str(b.get("state", "")) not in BUILD_NO_RUN_STATES]
    # same-commit adoption qualifies only FULL-SUITE sources (round-4
    # review): a partial webhook sibling is not evidence — we create our
    # own env-carrying build instead (cancelling our own push's webhook
    # build via pipeline skip semantics is ours to accept). A QUALIFYING
    # same-commit build is adopted on EVERY round purpose (verification
    # round 1) and adoption is decided BEFORE the foreign refusal so the
    # refusal needs no same-commit exemption at all (verification round
    # 2): a same-commit UI/manual/trigger build is neither evidence nor
    # ours to cancel — it refuses like any other unowned active build.
    same = [b for b in active if str(b.get("commit", "")) == commit
            and str(b.get("source", "")) in ("schedule", "api")]
    if same:
        b = same[0]
        return CIBuildRound(purpose="adopted", build_id=str(b.get("id", "")),
                            build_url=str(b.get("web_url", "")),
                            adopted=True), ""
    foreign = _foreign_active(active, owned_commits, owned_ids)
    if foreign is not None:
        return None, _foreign_refusal(foreign, branch)
    try:
        op = create_build_guarded(
            client, ops_dir, op_id=f"{run_id}-ci-r{round_idx}",
            run_id=run_id, purpose=purpose, branch=branch, commit=commit,
            message=message, sleep=sleep)
    except BuildCreationRefused as exc:
        return None, (f"pipeline refused build creation: {exc} - the "
                      "provider accepts no pushed-branch builds (schedule-"
                      "only policy); wait for the next scheduled build at "
                      "this commit (same-commit schedule builds are "
                      "adopted), or have an org admin re-enable branch "
                      "builds")
    return CIBuildRound(purpose=purpose, op_id=op.op_id,
                        build_id=op.build_id, build_url=op.build_url), ""


def _best_sibling(client: CIClient, branch: str, commit: str,
                  exclude_id: str) -> dict | None:
    """The sibling build at (branch, commit) most likely to carry FULL
    test signal after the pipeline refused ours. Only `schedule`/`api`
    sources qualify (round-4 review — a deliberate narrowing of the
    parent): webhook/manual builds run WITHOUT the trigger env's opt-in
    suite, so a passing partial build must never green-light the commit.
    Preference among qualifying siblings: active first, then finished,
    then job count. Adopted builds are monitor-only."""
    def _rank(b: dict) -> tuple:
        state = str(b.get("state", ""))
        return (state in _ACTIVE_BUILD_STATES,
                state in ("passed", "failed", "canceled", "cancelled"),
                len(b.get("jobs") or []), b.get("number") or 0)

    candidates = [b for b in client.builds_for_commit(branch, commit)
                  if str(b.get("state", "")) not in BUILD_NO_RUN_STATES
                  and str(b.get("id", "")) != exclude_id
                  and str(b.get("source", "")) in ("schedule", "api")]
    return max(candidates, key=_rank) if candidates else None


async def run_ci_rounds(*, client: CIClient, ops_dir: Path, run_id: str,
                        branch: str, spec: CIClassifySpec,
                        push_fn: Callable[[int], Any],
                        changes_fn: Callable[[], bool],
                        debug_fn: Callable[[JobResult], Any] | None = None,
                        log_dir=None,
                        max_retries: int = 2,
                        settle_sec: float = 60.0,
                        poll_sec: float = 60.0,
                        timeout_sec: float = 4 * 3600,
                        job_retry_max: int = 2,
                        message: str = "ci build",
                        # the checkout head, passed ONLY when the caller
                        # has PROVEN the remote branch ref equals it (the
                        # push is then a ref no-op) - it arms the narrow
                        # pre-push adoption exemption below; empty keeps
                        # the unconditional refusal
                        head_commit: str = "",
                        op_base: int | None = None,
                        sleep: Callable[[float], None] = time.sleep,
                        asleep: Callable[..., Any] = asyncio.sleep,
                        now: Callable[[], float] = time.monotonic,
                        trace: Callable[..., None] | None = None
                        ) -> CIRunResult:
    """The phase-4 loop (parent `phase4_init_wrapper` parity, §3.2
    ownership rules): push → webhook settle → adopt-or-guarded-create →
    monitor → SERIALIZED debug dispatch → retry rounds.

    `push_fn(op_index)` returns a PushOutcome-shaped object (`pushed`,
    `pushed_commit`, `dry_run`, `reason`) and is the ONLY authority on push
    authorization — this loop never self-grants; `op_index` is the
    ledger-unique index for this round's push op id. `debug_fn(job)` is
    awaited one job at a time by construction (all agents edit the one
    shared worktree with snapshot/restore — concurrency would clobber
    snapshots; do not gather). Retry rounds: code changes present ⇒ push
    fixes and build fresh; none ⇒ a NEW op-recorded build at the same
    commit (incomplete/flaky recovery — the §3.2 ledger owns every build
    we create; adopted builds are monitor-only). Budget-timeout-only
    rounds fail immediately: neither retries nor agents fix an operator
    problem. A refused (no-run) build adopts the best sibling carrying
    signal, else the run has NO CI signal and says so.

    Resume: op indices start AFTER the highest index already in the
    durable ledger (`op_base`, auto-derived from `ops_dir` when omitted;
    the caller may raise it to also clear its push-WAL indices) — a
    re-entered run never reuses an op id for a different operation."""
    def _trace(event: str, **fields) -> None:
        if trace is not None:
            trace(event, **fields)

    rounds: list[CIBuildRound] = []
    fixed_all: list[str] = []
    commit = ""
    try:
        ledger = load_ops(ops_dir)
        base = op_base if op_base is not None \
            else op_index_base(ledger, run_id)
    except CIOpError as exc:
        return CIRunResult("refused", reason=str(exc))
    # ownership sets: op-recorded builds and commits WE pushed (a prior
    # invocation's included) — a push must be refused BEFORE its webhook
    # build can cancel an unowned active build (round-3 review)
    owned_commits = {op.commit for op in ledger if op.run_id == run_id}
    owned_ids = {op.build_id for op in ledger
                 if op.run_id == run_id and op.build_id}
    # an unresolved INTENT carries no build id, but its create may have
    # landed — resolve the orphan by op-id metadata so the pre-push scan
    # sees it as OURS instead of refusing before `_acquire_build` can
    # settle it (extra verification round). Lookup only: the settle
    # itself (adopt / cancel-stale / escalate) stays in `_acquire_build`.
    for op in ledger:
        if op.run_id != run_id or op.state != "intent":
            continue
        try:
            for b in client.find_builds_by_meta("imx_op_id", op.op_id):
                bid = str(b.get("id", ""))
                if bid:
                    owned_ids.add(bid)
        except Exception as exc:  # noqa: BLE001 - cannot prove ownership
            return CIRunResult(
                "refused",
                reason=f"cannot resolve unsettled intent op {op.op_id}: "
                       f"{exc} — refusing to scan ownership blind",
                rounds=rounds)
    for rnd in range(max_retries + 1):
        idx = base + rnd
        if rnd == 0 or changes_fn():
            try:
                active = await asyncio.to_thread(
                    client.latest_builds, branch, _ACTIVE_BUILD_STATES)
            except Exception as exc:  # noqa: BLE001 - cannot prove safety
                return CIRunResult(
                    "refused",
                    reason=f"CI provider lookup failed before push: {exc} "
                           "— cannot prove no unowned build would be "
                           "cancelled",
                    rounds=rounds, fixed_jobs=fixed_all)
            scan = active
            if head_commit and not changes_fn():
                # Pre-push adoption exemption (live nightly-adopt run
                # 2026-08-24): a schedule/api build at the CURRENT head
                # is exactly what _acquire_build ADOPTS (monitor-only),
                # and with nothing left to commit the push is a ref
                # no-op that can neither create a webhook build nor
                # cancel anything - the hazard this refusal protects
                # against does not exist. Non-adoptable sources
                # (ui/manual/trigger) and a dirty tree keep the refusal.
                scan = [b for b in active
                        if not (str(b.get("commit", "")) == head_commit
                                and str(b.get("source", ""))
                                in ("schedule", "api"))]
            foreign = _foreign_active(scan, owned_commits, owned_ids)
            if foreign is not None:
                return CIRunResult("refused",
                                   reason=_foreign_refusal(foreign, branch),
                                   rounds=rounds, fixed_jobs=fixed_all)
            push = await asyncio.to_thread(push_fn, idx)
            if getattr(push, "dry_run", False):
                return CIRunResult("push_dry_run",
                                   reason=getattr(push, "reason", ""),
                                   rounds=rounds, fixed_jobs=fixed_all)
            if not getattr(push, "pushed", False):
                return CIRunResult("push_failed",
                                   reason=getattr(push, "reason", "")
                                   or "push refused", rounds=rounds,
                                   fixed_jobs=fixed_all)
            commit = str(getattr(push, "pushed_commit", ""))
            owned_commits.add(commit)
            purpose = "initial" if rnd == 0 else "retry"
            _trace("ci_push", round=rnd, commit=commit)
            # let the push's webhook build settle first (parity: with
            # skip-queued semantics an API build created before the webhook
            # build becomes the older queued build and gets skipped)
            await asleep(settle_sec)
        else:
            purpose = "rebuild"
            _trace("ci_rebuild_no_changes", round=rnd)
        try:
            rec, refusal = await asyncio.to_thread(
                _acquire_build, client, ops_dir, run_id=run_id,
                round_idx=idx, purpose=purpose, branch=branch,
                commit=commit, message=message, sleep=sleep,
                owned_commits=owned_commits, owned_ids=owned_ids)
        except CIOpError as exc:
            return CIRunResult("refused", reason=str(exc), rounds=rounds,
                               fixed_jobs=fixed_all)
        if rec is None:
            return CIRunResult("refused", reason=refusal, rounds=rounds,
                               fixed_jobs=fixed_all)
        # ONLY op-recorded builds join the ownership set — an adopted
        # build is monitor-only, and exempting its id would let a later
        # fix push cancel it (verification round 1)
        if rec.build_id and not rec.adopted:
            owned_ids.add(rec.build_id)
        rounds.append(rec)
        _trace("ci_build", round=rnd, purpose=rec.purpose,
               build=rec.build_id, adopted=rec.adopted)
        monitor_started = time.monotonic()
        outcome = await asyncio.to_thread(
            monitor_build, client, rec.build_id, spec=spec,
            poll_sec=poll_sec, timeout_sec=timeout_sec,
            # adopted = MONITOR-ONLY (§3.2): never retry_job a build this
            # run cannot prove it created (round-4 review)
            retry_max=0 if rec.adopted else job_retry_max,
            log_dir=log_dir, sleep=sleep, now=now)
        rec.monitor_duration_sec = round(
            max(0.0, time.monotonic() - monitor_started), 3)
        if outcome.no_run:
            rec.build_state = outcome.build_state
            _trace("ci_build_refused", build=rec.build_id,
                   state=outcome.build_state)
            sibling = await asyncio.to_thread(_best_sibling, client, branch,
                                              commit, rec.build_id)
            if sibling is None:
                return CIRunResult(
                    "no_signal",
                    reason=f"the pipeline refused build {rec.build_id} "
                           f"({outcome.build_state}) and no sibling build "
                           f"exists at {commit[:12]} on {branch} — this "
                           "commit is not going to be tested",
                    rounds=rounds, fixed_jobs=fixed_all)
            rec = CIBuildRound(purpose="adopted",
                               build_id=str(sibling.get("id", "")),
                               build_url=str(sibling.get("web_url", "")),
                               adopted=True)
            rounds.append(rec)
            _trace("ci_sibling_adopted", build=rec.build_id)
            monitor_started = time.monotonic()
            outcome = await asyncio.to_thread(
                monitor_build, client, rec.build_id, spec=spec,
                poll_sec=poll_sec, timeout_sec=timeout_sec,
                retry_max=0, log_dir=log_dir, sleep=sleep,
                now=now)
            rec.monitor_duration_sec = round(
                max(0.0, time.monotonic() - monitor_started), 3)
        _record_outcome(rec, outcome)
        if outcome.clean_pass:
            return CIRunResult("passed", rounds=rounds,
                               fixed_jobs=fixed_all)
        fixed: list[str] = []
        unfixed: list[str] = []
        for jr in outcome.failed_jobs:
            if debug_fn is None:
                unfixed.append(jr.name)
                continue
            # SERIALIZED debug dispatch — one agent at a time, by
            # construction (see the docstring; never gather these)
            ok = await debug_fn(jr)
            (fixed if ok else unfixed).append(jr.name)
        fixed_all.extend(fixed)
        if outcome.failed_jobs and not fixed:
            return CIRunResult(
                "failed", reason="no actionable CI failure could be fixed",
                rounds=rounds, fixed_jobs=fixed_all, unfixed_jobs=unfixed)
        if not outcome.failed_jobs and outcome.budget_timeouts:
            return CIRunResult(
                "failed",
                reason="job timeout budgets hit while tests were still "
                       "passing — raise the pipeline job budget or split "
                       "the job (operator fix; neither retries nor debug "
                       "agents can help)",
                rounds=rounds, fixed_jobs=fixed_all,
                unfixed_jobs=[j.name for j in outcome.budget_timeouts])
        # fixed > 0 ⇒ next round pushes the fixes; incomplete-only ⇒ next
        # round rebuilds the unfinished jobs at the same commit
    last = rounds[-1] if rounds else None
    return CIRunResult(
        "failed",
        reason=f"CI still not clean after {max_retries + 1} round(s)",
        rounds=rounds, fixed_jobs=fixed_all,
        unfixed_jobs=(last.failed + last.incomplete) if last else [])
