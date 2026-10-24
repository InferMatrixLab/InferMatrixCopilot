"""Durable run identity, reservation, and idempotent retry policy.

This service owns the on-disk reservation contract. It has no planner,
executor, transport, or long-lived worker state.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

from .. import idempotency as idem
from .. import run_status as rs
from ..config import Settings
from ..task_spec import TaskSpec
from .request_policy import authorize_repo_path


class RunReservation:
    _RUN_ID_RE = re.compile(r"^run-\d{8}-\d{6}-[0-9a-f]{6}$")

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def new_run_id() -> str:
        """Create a unique id in the persisted run-directory format."""
        return f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    def contained_run_dir(self, run_id: str, *, must_exist: bool = True) -> Path:
        """Resolve an untrusted id strictly inside the configured run root.

        A well-formed missing id may be returned for polling so callers can
        distinguish an unknown run from an invalid or escaping id.
        """
        if not self._RUN_ID_RE.match(run_id or ""):
            raise ValueError(f"invalid run_id: {run_id!r}")
        root = self.settings.run_root.resolve()
        run_dir = (self.settings.run_root / run_id).resolve()
        if run_dir.parent != root:
            raise ValueError(f"run_id escapes run_root: {run_id!r}")
        if must_exist and not run_dir.exists():
            raise ValueError(f"no such run: {run_id!r}")
        return run_dir

    def reserve(
        self, spec: TaskSpec, *, owner_server_id: str,
        owner_server_pid: int, idempotency_key: str = "",
    ) -> tuple[str, bool]:
        """Persist a queued run and return ``(run_id, newly_created)``.

        An explicit checkout is authorized and frozen before persistence.
        Keyed retries re-use a resolvable run, or re-arm a queued run whose
        owner died before it launched, under the idempotency-key lock.
        """
        if spec.repo_path:
            spec = spec.model_copy(update={"repo_path": authorize_repo_path(
                spec.repo, spec.repo_path, self.settings)})
        key = idem.validate_key(idempotency_key)
        if not key:
            return self._reserve_new(spec, owner_server_id, owner_server_pid), True
        with idem.key_lock(self.settings.run_root, key):
            return self._reserve_keyed(spec, key, owner_server_id,
                                       owner_server_pid)

    def _reserve_keyed(
        self, spec: TaskSpec, key: str, owner_server_id: str,
        owner_server_pid: int,
    ) -> tuple[str, bool]:
        """Resolve or create the run for a key while its lock is held."""
        fingerprint = idem.spec_fingerprint(spec.model_dump())
        entry = idem.read_entry(self.settings.run_root, key)
        if entry:
            run_dir = self.contained_run_dir(str(entry.get("run_id") or ""), must_exist=False)
            # The index is a cache; the persisted request is the authority.
            # A reused key for a different spec must not serve the old run.
            recorded = str(entry.get("spec_fingerprint") or "")
            if not recorded:
                recorded = self._request_fingerprint(run_dir)
            if recorded != fingerprint:
                raise idem.IdempotencyError(
                    f"idempotency_key {key!r} was already used for a different "
                    "request; use a new key for a new attempt")
            if run_dir.exists() and idem.resolvable(run_dir):
                return run_dir.name, False
            if run_dir.exists() and idem.relaunchable(run_dir):
                if self._request_fingerprint(run_dir) != fingerprint:
                    raise idem.IdempotencyError("reserved request changed; reconcile the original run")
                # The owner died after publishing the key but before launch.
                # Re-stamp the queued run so only this retry enqueues it.
                if rs.reclaim_queued(run_dir, owner_server_id=owner_server_id,
                                     owner_server_pid=owner_server_pid):
                    return run_dir.name, True
                return run_dir.name, False
            # A missing directory or damaged status cannot prove that no paid
            # work occurred. Keep the original binding pending reconciliation.
            return run_dir.name, False
        run_id = self._reserve_new(spec, owner_server_id, owner_server_pid)
        idem.write_entry(self.settings.run_root, key, run_id, fingerprint)
        return run_id, True

    @staticmethod
    def _request_fingerprint(run_dir: Path) -> str:
        try:
            request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request is not an object")
        except (OSError, ValueError) as exc:
            raise idem.IdempotencyError("reserved request is unreadable; reconcile the original run") from exc
        return idem.spec_fingerprint(request)

    def _reserve_new(
        self, spec: TaskSpec, owner_server_id: str, owner_server_pid: int,
    ) -> str:
        """Create the run directory, request, and initial queued status."""
        run_id = self.new_run_id()
        run_dir = self.settings.run_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        req = run_dir / "request.json"
        req.write_text(json.dumps(spec.model_dump(), indent=2), encoding="utf-8")
        try:  # least-privilege perms; advisory only against same-user tampering
            os.chmod(req, 0o600)
        except OSError:
            pass
        from .completion_execution import create_completion_pin
        completion_pin = create_completion_pin(self.settings, run_dir)
        rs.init_queued(run_dir, run_id=run_id, owner_server_id=owner_server_id,
                       owner_server_pid=owner_server_pid,
                       request_fingerprint=idem.spec_fingerprint(spec.model_dump()),
                       execution_mode="direct_review" if "direct_review" in spec.params else "workflow",
                       native_completion_sha256=completion_pin)
        return run_id
