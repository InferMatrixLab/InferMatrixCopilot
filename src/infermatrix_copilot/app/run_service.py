"""Durable, headless run reservation, execution, polling, and knowledge reads.

The MCP protocol server and embedded SDK both call this service. It owns no
transport or CLI command implementation.
"""

from __future__ import annotations

from ..persistence import atomic_write_bytes

import hashlib
import json
import os
import subprocess
import sys
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .. import contract
from .. import idempotency as idem
from .. import run_status as rs
from ..config import Settings
from ..knowledge_docs import KnowledgeDocs, KnowledgeDocsError
from .request_policy import (
    PolicyError, authorize_repo_path, enforce_mcp_policy,
    enforce_quality_review_policy, enforce_strict_review_policy,
)
from ..task_spec import READ_ONLY_KINDS, TaskSpec
from .core import Copilot
from .reservation import RunReservation

KNOWLEDGE_PIN = "knowledge.json"


def read_knowledge_pin(run_dir: str | Path) -> dict:
    """The knowledge a reserved run was pinned to ({} for runs reserved
    before pinning existed, which use the process default)."""
    try:
        data = json.loads((Path(run_dir) / KNOWLEDGE_PIN).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# The conflict key of a run whose request cannot be read or keyed: such runs
# fail at launch anyway, and sharing one key keeps them from overlapping each
# other rather than guessing what they touch.
UNKEYED = ("?", None)


class RunQueue:
    """FIFO run queue for N workers that never hands out two runs with the
    same conflict key at once.

    A run whose key is busy stays queued, in order, and does not hold a
    worker: the worker takes the oldest run whose key is free, so one busy PR
    cannot stall reviews of other PRs behind it. Keys are computed at enqueue
    (the request is persisted before any run is enqueued)."""

    def __init__(self, key_for: Callable[[str], Any]):
        self._key_for = key_for
        self._cond = threading.Condition()
        self._pending: deque[tuple[str, bool, Any]] = deque()
        self._busy: set[Any] = set()

    def put(self, item: tuple[str, bool]) -> None:
        run_id, strict_compat = item
        try:
            key = self._key_for(run_id)
            hash(key)
        except Exception:  # noqa: BLE001 - a reserved run must still enqueue
            key = UNKEYED
        with self._cond:
            self._pending.append((run_id, strict_compat, key))
            self._cond.notify_all()

    def take(self) -> tuple[str, bool, Any]:
        """Block until a run with a free key is queued; claim its key."""
        with self._cond:
            while True:
                for index, item in enumerate(self._pending):
                    if item[2] not in self._busy:
                        del self._pending[index]
                        self._busy.add(item[2])
                        return item
                self._cond.wait()

    def done(self, key: Any) -> None:
        """Release a key claimed by `take`, waking workers blocked on it."""
        with self._cond:
            self._busy.discard(key)
            self._cond.notify_all()

    def snapshot(self) -> tuple[list[str], set[Any]]:
        """Queued run ids and busy keys (tests/diagnostics)."""
        with self._cond:
            return [item[0] for item in self._pending], set(self._busy)


class RunService:
    """The server core: a run queue drained by `STRICT_MAX_WORKERS` workers
    over isolated subprocesses, plus ownership-aware reconciliation.
    Framework-agnostic (no `mcp` import) so it is unit-testable without a live
    protocol connection."""

    def __init__(self, settings: Settings | None = None, *, worker_prefix=None):
        """Wire reservation and workflow services, register this server's
        liveness token, reconcile orphaned runs, and start the worker."""
        # Trusted host composition may wrap Python startup; workflow arguments,
        # process ownership and the execution claim remain owned by this service.
        prefix = (sys.executable,) if worker_prefix is None else worker_prefix
        if (not isinstance(prefix, (tuple, list)) or not prefix
                or any(not isinstance(arg, str) or not arg or '\0' in arg for arg in prefix)
                or not Path(prefix[0]).is_absolute()
                or not Path(prefix[0]).is_file() or not os.access(prefix[0], os.X_OK)):
            raise ValueError('worker prefix requires an absolute executable and argument vector')
        self.worker_prefix = tuple(prefix)
        self.settings = settings or Settings()
        self.reservations = RunReservation(self.settings)
        self.copilot = Copilot(self.settings)
        self.run_root = Path(self.settings.run_root)
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.server_id = uuid.uuid4().hex
        self.pid = os.getpid()
        rs.register_server(self.run_root, self.server_id, self.pid)
        rs.startup_reconcile(self.run_root)
        # One sweep at a time per service: with several workers finishing
        # runs, overlapping sweeps would only race each other for the same
        # entries and refs.
        self._reap_lock = threading.Lock()
        self._reap()
        self.max_strict_workers = int(self.settings.strict_max_workers)
        self._q = RunQueue(self._conflict_key)
        self._workers = [
            threading.Thread(target=self._worker_loop, daemon=True,
                             name=f"imx-mcp-worker-{i}")
            for i in range(self.max_strict_workers)]
        for worker in self._workers:
            worker.start()

    def _reap(self) -> None:
        """Bound what the idempotency index and PR-time worktrees accumulate.

        Best-effort by construction: this runs at startup and after each
        terminal run, and a sweep that cannot complete must never stop a server
        from serving or a run from finishing."""
        if not self._reap_lock.acquire(blocking=False):
            return  # a sweep is already running; the next terminal run retries
        try:
            idem.reap_stale(self.run_root,
                            retention_days=self.settings.idem_retention_days,
                            repo_paths=list(self.settings.repo_paths.values()))
        except Exception:  # noqa: BLE001 — housekeeping, never load-bearing
            pass
        finally:
            self._reap_lock.release()

    def capabilities(self) -> dict:
        """The version/capability handshake — see `contract.capabilities`."""
        from ..engine.lifecycle import fcntl as _fcntl

        return contract.capabilities(
            max_strict_workers=self.max_strict_workers,
            supports_file_locking=_fcntl is not None)

    # -- workers: up to N runs at a time, each an isolated subprocess --------
    def _conflict_key(self, run_id: str) -> tuple[str, Any]:
        """The resource a run must not share with a concurrently executing one:
        its checkout plus its PR (``None`` for issue tasks, which work in the
        live checkout itself).

        Runs on one PR head share one PR-time worktree (engine/worktrees.py
        keys it by repo+PR+sha), and harness sessions write their tool-bridge
        config into their working directory — cursor's `.cursor/mcp.json` has
        one fixed path per tree — so two such runs overlapping would bind one
        run's agent to the other run's tool scope and trace. Different PRs get
        different trees and may run side by side."""
        try:
            spec = json.loads((self.run_root / run_id / "request.json")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            spec = None
        if not isinstance(spec, dict):
            return UNKEYED
        repo = str(spec.get("repo") or self.settings.default_repo)
        try:
            # the resolver execution itself uses (frozen path, REPO_PATHS,
            # then the adapter manifest), so an implicit request and an
            # explicit one naming the same checkout get the same key
            checkout = self.copilot.repository_context.repo_path_for(
                TaskSpec.model_validate(spec))
        except Exception:  # noqa: BLE001 - such a run fails at launch anyway
            checkout = str(spec.get("repo_path") or "")
        where = str(Path(checkout).expanduser().resolve()) if checkout \
            else f"repo:{repo}"
        pr = spec.get("pr")
        return (where, None if pr is None else str(pr))

    def _worker_loop(self) -> None:
        """Drain the queue forever, launching one run subprocess at a time per
        worker. A launch failure marks the run failed but never kills the
        worker, and always frees the run's conflict key."""
        while True:
            run_id, strict_compat, key = self._q.take()
            try:
                self._launch(run_id, strict_compat=strict_compat)
            except Exception as exc:  # noqa: BLE001 - worker must survive
                try:
                    rs.mark(self.run_root / run_id, rs.FAILED,
                            note=f"launch error: {type(exc).__name__}: {exc}")
                except Exception:
                    pass
            finally:
                self._q.done(key)

    def _launch(self, run_id: str, *, strict_compat: bool = False) -> None:
        """Run one reserved run as `python -m infermatrix_copilot --execute-reserved
        <id>`, child stdout+stderr -> console.log. No MCP child may write
        outward, Strict included. After `.wait()` the child is reaped, so we
        reconcile as sole writer."""
        run_dir = self.run_root / run_id
        env = dict(os.environ)
        from ..knowledge_service.containment import configuration
        maintenance = configuration(getattr(self, "knowledge_maintenance", None))
        if maintenance["enabled"]:
            env["KB_CONTAINMENT_CONFIG"] = json.dumps(maintenance)
            from ..knowledge_service.containment import knowledge_availability_check
            pin = read_knowledge_pin(run_dir)
            if not knowledge_availability_check(pin.get("knowledge_usage"), config=maintenance)["allowed"]:
                rs.mark(run_dir, rs.FAILED, note="knowledge containment requires reassessment")
                return
        # Belt to the policy's braces. Strict used to be handed ALLOW_POST=1
        # when this server allowed it, because Strict specs could carry
        # post=True; the policy now refuses that, so leaving the env gate open
        # would only preserve a path to a second publisher on the same PR.
        env["ALLOW_POST"] = "0"
        env["ALLOW_PUSH"] = "0"
        # The Windows Store Python runtime otherwise inherits the machine's
        # legacy console code page (commonly GBK). Reports legitimately contain
        # Unicode markers such as ✓; force the isolated child's stdio to UTF-8
        # so rendering a completed report cannot fail after all work is done.
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # The child builds its own Settings() from env/.env, so make THIS server's
        # effective config authoritative for it: the same run_root (to locate the
        # reserved dir) and the same read-only allowlist (its policy re-check).
        env["RUN_ROOT"] = str(self.run_root)
        env["MCP_REPO_ALLOWLIST"] = json.dumps(self.settings.mcp_allowed_repos)
        env["DEFAULT_REPO"] = self.settings.default_repo
        env["REPO_PATHS"] = json.dumps(self.settings.repo_paths)
        env["REPO_FULL_NAMES"] = json.dumps(self.settings.repo_full_names)
        env["ALLOWED_KNOWLEDGE_REPOSITORIES"] = json.dumps(self.settings.allowed_knowledge_repositories)
        # The child re-authorizes the request's `repo_path`, so it must judge it
        # against the SAME roots and identities this server did — otherwise the
        # re-check silently validates against different config than the parent.
        env["MCP_ALLOWED_REPO_ROOTS"] = json.dumps(
            self.settings.allowed_repo_roots)
        pin = read_knowledge_pin(run_dir)
        if pin.get("knowledge_dir"):
            # the snapshot pinned at reservation, even if another is active now;
            # a snapshot pruned since then fails the run instead of silently
            # reviewing with different knowledge
            if not (Path(pin["knowledge_dir"]) / "AGENTS.md").is_file():
                rs.mark(run_dir, rs.FAILED, note=(
                    f"pinned knowledge snapshot {pin.get('snapshot')} is no longer available"))
                self._reap()
                return
            env["KNOWLEDGE_DIR"] = pin["knowledge_dir"]
            if pin.get("knowledge_root"):
                env["KNOWLEDGE_ROOT"] = pin["knowledge_root"]
            else:
                env.pop("KNOWLEDGE_ROOT", None)  # the packaged tree, whatever is configured now
        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            # Codex/Claude launch the MCP server over stdio.  Without a new
            # Windows process group, host or transport shutdown can propagate a
            # console control event into the long-running review child and turn
            # it into KeyboardInterrupt.  CREATE_NO_WINDOW also prevents a
            # console flash for every MCP run.
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            # Keep the durable child alive and independently reconcilable when
            # its stdio MCP parent is restarted.
            popen_kwargs["start_new_session"] = True
        with open(run_dir / "console.log", "ab") as log:
            execute_arg = (
                "--execute-strict-reserved"
                if strict_compat else "--execute-reserved"
            )
            request_bytes = (run_dir / "request.json").read_bytes()
            request = json.loads(request_bytes)
            status = rs.read_status(run_dir) or {}
            fingerprint = status.get("request_fingerprint")
            if fingerprint and fingerprint != idem.spec_fingerprint(request):
                raise PolicyError("reserved request changed before launch")
            from .completion_execution import backend_environment, completion_settings_for_run
            execution_settings = completion_settings_for_run(self.settings, run_dir)
            env['NATIVE_COMPLETION_PROFILES'] = json.dumps(execution_settings.native_completion_profiles)
            env['NATIVE_SESSION_PROFILES'] = json.dumps(execution_settings.native_session_profiles)
            env.update(backend_environment(execution_settings))
            env['TRACE_STORE_ROOT'] = execution_settings.trace_store_root
            is_direct = status.get("execution_mode") == "direct_review"
            direct = (request.get("params") or {}).get("direct_review") if is_direct else None
            if direct is not None:
                from ..engine.steps.review.direct_run import check_profile
                trusted = check_profile(direct, getattr(self, "direct_profiles", {}))
                entry = idem.read_entry(self.run_root, direct["idempotency_key"])
                if not entry or entry.get("spec_fingerprint") != idem.spec_fingerprint(request):
                    raise PolicyError("Direct request changed after reservation")
                env["COPILOT_DIRECT_PROFILE"] = json.dumps(trusted)
                env["COPILOT_DIRECT_REQUEST_SHA256"] = hashlib.sha256(request_bytes).hexdigest()
            command = ([*self.worker_prefix, "-m", "infermatrix_copilot.engine.steps.review.direct_run", run_id]
                       if is_direct else
                       [*self.worker_prefix, "-m", "infermatrix_copilot", execute_arg, run_id])
            # A changed host wrapper cannot take over an earlier launch. This
            # record survives an unknown Popen/wait outcome, like the run claim.
            from ..persistence import immutable_write_bytes
            try:
                immutable_write_bytes(run_dir / 'worker-launch.json',
                    (json.dumps({'schema': 'run-worker-launch/1', 'command': command},
                                sort_keys=True) + '\n').encode(), exist_ok=True)
            except FileExistsError as exc:
                raise PolicyError('worker launch differs from its original binding') from exc
            proc = subprocess.Popen(
                command,
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                cwd=str(self.run_root), env=env,
                **popen_kwargs,
            )
            proc.wait()
        # BLOCKED_EXIT without a terminal status is the lock-loser signature:
        # a genuinely blocked child writes its terminal state before exiting 3
        rs.reconcile_after_wait(run_dir, child_pid=proc.pid,
                                suspect_lock_loser=(proc.returncode == 3))
        self._reap()  # the run is terminal; sweep what it left behind

    # -- start (reserve + enqueue) -------------------------------------------
    def start(self, spec_dict: dict) -> str:
        """Boundary policy enforcement + reserve + enqueue; returns the run id.

        No idempotency key here by design: `start` also serves `issue_answer`
        and `issue_filter`, which carry no PR and no head, so any spec-derived
        key would collapse every issue task in a repo onto one entry."""
        spec = enforce_mcp_policy(spec_dict, allowed_repos=self.settings.mcp_allowed_repos, settings=self.settings)
        run_id, created = self.reservations.reserve(
            spec, owner_server_id=self.server_id, owner_server_pid=self.pid)
        if created:
            self._q.put((run_id, False))
        return run_id

    def start_strict_review(self, spec_dict: dict) -> str:
        """Reserve a packaged Strict review workflow.

        Enqueues only a run this call actually created. Returning an existing id
        while still enqueueing would deduplicate the *id* and not the
        *execution* — the second child would review the same PR again."""
        run_id, _created = self.reserve_strict_review(spec_dict)
        return run_id

    def reserve_strict_review(self, spec_dict: dict) -> tuple[str, bool]:
        """Reserve Strict work and expose whether this call created the run.

        ``start_strict_review`` retains its string-only compatibility contract;
        the typed SDK uses this operation so an idempotent retry is never
        mislabeled as a newly created run.
        """
        spec = enforce_strict_review_policy(
            spec_dict, allowed_repos=self.settings.mcp_allowed_repos,
            settings=self.settings)
        run_id, created = self.reservations.reserve(
            spec, owner_server_id=self.server_id, owner_server_pid=self.pid,
            idempotency_key=str(spec_dict.get("idempotency_key") or ""))
        if created and self._pin_or_fail(run_id):
            self._q.put((run_id, True))
        return run_id, created

    def reserve_direct_review(self, request) -> tuple[str, bool]:
        """Reserve trusted SDK input using the same identity and worker queue."""
        from ..engine.steps.review.direct_run import check_profile

        raw = request.to_dict()
        check_profile(raw, getattr(self, "direct_profiles", {}))
        review = raw["review"]
        spec = enforce_strict_review_policy({
            "kind": "pr_review", "repo": review["repository"]["alias"], "pr": review["pr_number"],
            "post": False, "expected_head_sha": review["expected_head_sha"], "repo_path": raw["repo_path"],
            "params": {"carried_findings": review["carried_findings"]},
        }, allowed_repos=self.settings.mcp_allowed_repos, settings=self.settings)
        # Only this typed SDK path may add execution input. The MCP allowlist
        # still strips arbitrary params; Direct never grants write authority.
        # Bind the nested execution input to the same authorized canonical path.
        # Otherwise equivalent symlink spellings fail the worker's equality
        # check and produce different business-idempotency fingerprints.
        raw["repo_path"] = spec.repo_path
        spec = spec.model_copy(update={"params": {**spec.params, "direct_review": raw}})
        run_id, created = self.reservations.reserve(spec, owner_server_id=self.server_id,
            owner_server_pid=self.pid, idempotency_key=raw["idempotency_key"])
        if created and self._pin_or_fail(run_id):
            self._freeze_direct_workspace(run_id, spec.model_dump())
            self._q.put((run_id, False))
        return run_id, created

    def _freeze_direct_workspace(self, run_id: str, spec: dict) -> None:
        from ..review.workspace import snapshot_direct_workspace

        raw = spec["params"]["direct_review"]
        try:
            snapshot_direct_workspace(self.run_root / run_id, raw,
                full_name=self.settings.repo_full_names.get(spec["repo"]) or raw["review"]["repository"]["full_name"])
        except Exception as exc:
            rs.mark(self.run_root / run_id, rs.FAILED,
                    note=f"could not freeze Direct workspace: {type(exc).__name__}: {exc}")
            raise

    def find_review(self, key, expected_head_sha):
        """Recover the original frozen request after an uncertain reservation."""
        entry = idem.read_entry(self.run_root, idem.validate_key(key))
        if not entry:
            return None
        run_dir = self.reservations.contained_run_dir(str(entry.get("run_id") or ""))
        spec = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
        if entry.get("spec_fingerprint") != idem.spec_fingerprint(spec):
            raise PolicyError("recovered review request changed after reservation")
        if (spec.get("kind") != "pr_review" or spec.get("repo") != self.settings.default_repo
                or spec.get("expected_head_sha") != expected_head_sha):
            raise PolicyError("recovered review does not match repository and head")
        with idem.key_lock(self.run_root, key):
            rs.reconcile_if_dead(run_dir, self.run_root)
            if idem.relaunchable(run_dir) and rs.reclaim_queued(run_dir,
                    owner_server_id=self.server_id, owner_server_pid=self.pid):
                if self._pin_or_fail(run_dir.name):
                    direct = "direct_review" in (spec.get("params") or {})
                    if direct:
                        self._freeze_direct_workspace(run_dir.name, spec)
                    self._q.put((run_dir.name, not direct))
        return run_dir.name

    def start_quality_review(self, spec_dict: dict) -> str:
        """Reserve the dedicated, idempotent PR quality workflow."""
        run_id, _created = self.reserve_quality_review(spec_dict)
        return run_id

    def reserve_quality_review(self, spec_dict: dict) -> tuple[str, bool]:
        """Reserve quality work and report whether this call created it."""
        spec = enforce_quality_review_policy(
            spec_dict, allowed_repos=self.settings.mcp_allowed_repos,
            settings=self.settings)
        run_id, created = self.reservations.reserve(
            spec, owner_server_id=self.server_id, owner_server_pid=self.pid,
            idempotency_key=str(spec_dict.get("idempotency_key") or ""))
        if created and self._pin_or_fail(run_id):
            self._q.put((run_id, False))
        return run_id, created

    # -- knowledge pinning ---------------------------------------------------------
    def _pin_or_fail(self, run_id: str) -> bool:
        """Pin, or make the reservation durably terminal: a reservation that
        exists but is never enqueued would stay queued forever (retries see
        created=False), so a pinning failure fails the run with its reason."""
        try:
            self._pin_knowledge(run_id)
            return True
        except Exception as exc:  # noqa: BLE001 - any failure must end the run visibly
            rs.mark(self.run_root / run_id, rs.FAILED,
                    note=f"could not pin the knowledge snapshot: {type(exc).__name__}: {exc}")
            return False

    def _pin_knowledge(self, run_id: str) -> dict:
        """Record, at reservation, the knowledge this run must review with.

        The knowledge service swaps the active snapshot at runtime; a Strict
        run that starts later (queued, or re-launched after a restart) must
        still read the snapshot that was active when it was reserved, and its
        result must say which one that was. The resolved real paths are
        recorded, never the ``active`` symlink."""
        from ..knowledge_view import KnowledgeView

        existing = read_knowledge_pin(self.run_root / run_id)
        if existing:
            # reclaiming an interrupted, never-started reservation also reports
            # created=True: its reservation-time pin stands
            return existing
        view = KnowledgeView.current()
        if view.snapshot == "packaged":
            env_root = ""
        elif view.verified:
            env_root = str(view.root.parent)  # the snapshot dir (MANIFEST.json + knowledge/)
        else:
            env_root = str(view.root)
        snapshot, knowledge_dir = view.public_snapshot, str(view.root)
        if view.snapshot == "packaged":
            # no snapshot root selected: the run reviews with this server's
            # EFFECTIVE knowledge_dir (a deployment may configure its own), and
            # KNOWLEDGE_ROOT is cleared at launch, so a relaunch under a newly
            # configured root cannot review with a snapshot while reporting this
            effective_root = Path(self.settings.knowledge_dir).resolve()
            if effective_root != view.root.resolve():
                view = KnowledgeView(effective_root, f"unverified:{effective_root}")
                snapshot = view.public_snapshot
            knowledge_dir = str(effective_root)
        pin = {"snapshot": snapshot, "tree_sha256": view.tree_sha256,
               "knowledge_dir": knowledge_dir, "knowledge_root": env_root}
        from ..knowledge_service.containment import configured, configuration, _issue_usage
        maintenance = getattr(self, "knowledge_maintenance", None)
        if configuration(maintenance)["enabled"]:
            spec = json.loads((self.run_root / run_id / "request.json").read_text())
            selector = str(spec.get("repo") or "")
            from ..kb_service.repo_spec import resolve_snapshot_repo
            binding = resolve_snapshot_repo(view, selector)
            if binding is None:
                raise RuntimeError("containment requires a registered pinned repository")
            with configured(maintenance):
                usage = _issue_usage({"scope": "snapshot", "repository": binding.repo_id,
                    "review_id": run_id, "knowledge_snapshot": view.public_snapshot,
                    "knowledge_tree_sha256": view.tree_sha256}, mode="strict", view=view)
            pin["knowledge_usage"] = usage
        path = self.run_root / run_id / KNOWLEDGE_PIN
        atomic_write_bytes(path, json.dumps(pin, sort_keys=True).encode("utf-8"))
        return pin

    def strict_readiness(self, repo: str, repo_path: str = "") -> list[str]:
        """Return actionable setup gaps before reserving a Strict run.

        `repo_path` validates THAT explicit checkout. The deleted
        `configure_strict_repo` used to mutate `settings.repo_paths` so this
        method would see it — process-global state written per call, so two
        concurrent Strict requests for different checkouts could each preflight
        against the other's."""
        missing = []
        # Backend selection is EXPLICIT for Strict (doc/RFC-provider-registry
        # .md): unset refuses with the exact fix, never falls back silently.
        backend = self.settings.strict_backend
        if not backend:
            missing.append(
                "STRICT_BACKEND not set; add STRICT_BACKEND=api (or cursor / "
                "claude-code / codex) to ~/.infermatrix-copilot/.env")
        elif backend == "api":
            if not self.settings.shared_api_key:
                missing.append(
                    "model credential missing; set ANTHROPIC_API_KEY or "
                    "OPENAI_API_KEY in "
                    "~/.infermatrix-copilot/.env"
                )
        else:
            from ..providers import transport_for

            try:
                transport = transport_for(self.settings)
            except NotImplementedError as exc:
                missing.append(str(exc))
            else:
                # Packaged Strict/quality workflows use tool-free completion
                # for reduction and repair as well as native agent sessions.
                # CLI/auth readiness alone cannot attest that second contract.
                try:
                    from ..providers.completion_profiles import validate_completion_policy
                    validate_completion_policy(self.settings, transport, require_trace=True)
                except (ValueError, RuntimeError) as exc:
                    missing.append(
                        f"STRICT_BACKEND={backend} cannot provide required "
                        f"tool-free completion: {exc}")
                from ..providers.completion_profiles import completion_profile_config
                if completion_profile_config(self.settings, backend, session=True) is not None:
                    try:
                        validate_completion_policy(self.settings, transport, session=True)
                    except (ValueError, RuntimeError) as exc:
                        missing.append(f'STRICT_BACKEND={backend} agent sandbox unavailable: {exc}')
                elif not transport.cli_path():
                    missing.append(
                        f"STRICT_BACKEND={backend} selected but its CLI is "
                        "not installed; install it or set STRICT_BACKEND_CLI "
                        "in ~/.infermatrix-copilot/.env")
                else:
                    gap = transport.auth_gap()
                    if gap:
                        missing.append(gap)
        if repo_path:
            try:
                repo_path = authorize_repo_path(repo, repo_path, self.settings)
            except PolicyError as exc:
                missing.append(str(exc))
                repo_path = ""
        else:
            repo_path = self.copilot._resolve_repo_path(repo)
        if not repo_path or not Path(repo_path).is_dir():
            missing.append(
                f"checkout for {repo!r} missing; run the installer with "
                "--repo-path <path> or set REPO_PATHS in "
                "~/.infermatrix-copilot/.env"
            )
        if self.copilot.store.get("pr-review") is None:
            missing.append(
                "packaged pr-review playbook missing; reinstall "
                "InferMatrixCopilot"
            )
        return missing

    def quality_readiness(self, repo: str, repo_path: str = "") -> list[str]:
        """Return setup gaps for the quality workflow and its model backend."""
        missing = [
            item for item in self.strict_readiness(repo, repo_path)
            if "packaged pr-review playbook" not in item
        ]
        if self.copilot.store.get("pr-quality") is None:
            missing.append(
                "packaged pr-quality playbook missing; reinstall "
                "InferMatrixCopilot")
        return missing

    # -- poll -----------------------------------------------------------------
    def get_status(self, run_id: str) -> dict:
        """Lazy-reconcile then return `run_status.json` + `progress.json` (when
        present — queued/planning runs have none)."""
        run_dir = self.reservations.contained_run_dir(run_id)
        rs.reconcile_if_dead(run_dir, self.run_root)
        status = rs.read_status(run_dir) or {}
        progress = None
        pf = run_dir / "progress.json"
        if pf.exists():
            try:
                progress = json.loads(pf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                progress = None
        return {"run_id": run_id, "status": status, "progress": progress}

    def get_result(self, run_id: str, offset: int = 0) -> dict:
        """Lazy-reconcile then return the run state; once terminal, attach a
        size-capped page of the report (RUN_REPORT.md, or ESCALATION.md when
        blocked) with `next_offset` + `report_path` for paging — never an
        unbounded dump over the protocol.

        Terminal responses also carry `result`: the structured form from
        `contract.build_review_result`, so a machine consumer reads the verdict
        and findings as data instead of scraping them back out of the Markdown.
        The `report` paging stays for back-compat with existing hosts."""
        # A well-formed id this server has never heard of is answered, not
        # raised, so a bot holding a stale id can tell "lost" from "still
        # running". A malformed or escaping id is still an error.
        run_dir = self.reservations.contained_run_dir(run_id, must_exist=False)
        if not run_dir.exists():
            return {"run_id": run_id, "state": "unknown", "note": "",
                    "report": None, "report_path": None, "next_offset": None,
                    "result": contract.unknown_run_result(run_id)}
        rs.reconcile_if_dead(run_dir, self.run_root)
        status = rs.read_status(run_dir) or {}
        state = status.get("state")
        out: dict[str, Any] = {"run_id": run_id, "state": state,
                               "note": status.get("note", "")}
        if state not in rs.TERMINAL:
            return out  # still queued/planning/running — poll again
        failure = run_dir / "direct-failure.json"
        if failure.is_file():
            out["execution_failure"] = json.loads(failure.read_text(encoding="utf-8"))
        direct = run_dir / "direct-result.json"
        out["result"] = (json.loads(direct.read_text(encoding="utf-8")) if direct.is_file()
                         else contract.build_review_result(run_dir))
        report_path = run_dir / "RUN_REPORT.md"
        if state == rs.BLOCKED and (run_dir / "ESCALATION.md").exists():
            report_path = run_dir / "ESCALATION.md"
        if report_path.exists():
            text = report_path.read_text(encoding="utf-8", errors="replace")
            offset = max(0, int(offset))
            cap = self.settings.mcp_report_max_bytes
            out["report"] = text[offset:offset + cap]
            out["report_path"] = str(report_path)
            nxt = offset + cap
            out["next_offset"] = nxt if nxt < len(text) else None
        else:
            out.update(report=None, report_path=None, next_offset=None)
        return out

    def get_quality_result(self, run_id: str) -> dict:
        """Poll one quality run, returning its dedicated typed contract."""
        run_dir = self.reservations.contained_run_dir(run_id, must_exist=False)
        if not run_dir.exists():
            return {
                "run_id": run_id,
                "state": "unknown",
                "note": "",
                "result": contract.unknown_quality_result(run_id),
            }
        rs.reconcile_if_dead(run_dir, self.run_root)
        status = rs.read_status(run_dir) or {}
        state = status.get("state")
        out: dict[str, Any] = {
            "run_id": run_id,
            "state": state,
            "note": status.get("note", ""),
        }
        if state in rs.TERMINAL:
            out["result"] = contract.build_quality_result(run_dir)
        return out

    def list_playbooks(self) -> dict:
        """The read-only V1 surface: the exposed kinds and the vetted playbooks
        backing them (read-only introspection; no run started)."""
        pbs = [line for line in self.copilot.playbooks().splitlines()
               if any(k in line for k in READ_ONLY_KINDS)]
        return {"read_only_kinds": sorted(READ_ONLY_KINDS), "playbooks": pbs}

    def _docs(self, repo: str) -> KnowledgeDocs:
        """Build the same repo-scoped knowledge view used by workflow agents."""
        from ..adapters.base import AdapterRegistry
        from ..knowledge_view import KnowledgeView, _load_view
        from ..kb_service.repo_spec import resolve_snapshot_repo

        repo = repo or self.settings.default_repo
        current = KnowledgeView.current()
        kdir = Path(self.settings.knowledge_dir).resolve()
        if current.verified or current.root.resolve() == kdir:
            view = current
        elif (kdir.parent / "MANIFEST.json").is_file():
            view = _load_view(str(kdir.parent))
        else:
            view = KnowledgeView(kdir, f"unverified:{kdir}")
        binding = resolve_snapshot_repo(view, repo)
        if repo not in self.settings.mcp_allowed_repos and (binding is None or binding.repo_id not in self.settings.mcp_allowed_repos):
            raise PolicyError(f"repo {repo!r} is not permitted")
        if binding is not None:
            return KnowledgeDocs(view.root, binding.knowledge_slice, verify=view.path)
        try:
            adapter = AdapterRegistry(self.settings.adapters_dir).resolve(
                name=repo.replace("-", "_"))
        except Exception as exc:
            raise PolicyError(f"no knowledge adapter for repo {repo!r}") from exc
        kn = adapter.manifest.get("knowledge") or {}
        return KnowledgeDocs(view.root, kn.get("repo_subdir"), verify=view.path)

    def doc_search(self, query: str, repo: str = "", limit: int = 40) -> dict:
        """Search general + the selected repo's curated Markdown knowledge."""
        selected = repo or self.settings.default_repo
        hits = self._docs(selected).search(query, limit=limit)
        return {"query": query, "repo": selected, "matches": hits,
                "truncated": len(hits) >= max(1, min(int(limit), 100))}

    def doc_read(self, path: str, repo: str = "", offset: int = 0) -> dict:
        """Read one document from general + the selected repo's slice."""
        selected = repo or self.settings.default_repo
        try:
            page = self._docs(selected).read(path, offset=offset)
        except FileNotFoundError as exc:
            raise KnowledgeDocsError(f"no such document: {path}") from exc
        return {"repo": selected, **page}

    def close(self) -> None:
        """Deregister this server's liveness token (best-effort, on shutdown)."""
        rs.unregister_server(self.run_root, self.server_id)
