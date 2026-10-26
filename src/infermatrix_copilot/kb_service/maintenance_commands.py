"""Operator requests for the single-writer maintenance scheduler.

The CLI only enqueues immutable requests. Owner resolution requests are signed
after authenticating the actual gh user; only the scheduler can append a
resolution or sign a labeled regression case. Original audit rows stay intact.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

from ..knowledge_service.signing import sign
from .maintenance_calibration import cases_directory
from .maintenance_store import MaintenanceStore

from .maintenance_resolution import (
    RESOLUTION_PURPOSE, OUTCOMES, PROOF_FIELDS, _digest, _owners,
    _oracle, _confirmation, apply_resolution,
)


def authenticated_owner():
    result = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                            check=True, capture_output=True, text=True, timeout=30)
    actor = result.stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", actor) or actor.casefold() not in _owners():
        raise ValueError("authenticated gh user is not an allowed maintenance owner")
    return actor


def _evidence(value):
    text = Path(value[1:]).read_text() if value.startswith("@") else value
    evidence = json.loads(text)
    if not isinstance(evidence, dict) or not isinstance(evidence.get("reason"), str) or not evidence["reason"].strip():
        raise ValueError("resolution evidence must be a JSON object with a nonempty reason")
    # Canonical encoding also rejects non-finite numbers before signing.
    json.dumps(evidence, allow_nan=False)
    return evidence


def _selected(rt, args):
    if getattr(args, "all", False):
        return None
    repo = args.repo
    if repo not in rt.registry:
        raise ValueError(f"unknown maintenance repository: {repo}")
    if not rt.registry[repo].enabled:
        raise ValueError(f"maintenance repository is disabled: {repo}")
    return [repo]


def command(args, state_dir):
    from ..config import Settings
    from . import maintenance
    from .runtime import KbRuntime

    rt = KbRuntime.from_env(Settings(), state_dir=state_dir, sync_repos=False)
    try:
        if args.command == "maintain":
            repos = _selected(rt, args)
            if args.maintenance_action in {"plan", "status"}:
                read = maintenance.status if args.maintenance_action == "status" else maintenance.plan
                result = read(rt, repos=repos)
            else:
                detail = {"kind": "maintenance", "calibrate": args.calibrate, "drill": args.drill}
                result = maintenance.request(rt, args.request_id, repos=repos, options=detail)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        actor = authenticated_owner()
        store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
        finding = store.finding(args.id)
        if finding is None:
            raise ValueError("unknown maintenance finding ID")
        if rt.outbox is None:
            raise ValueError("owner resolution requires the existing KB_SIGNING_KEY")
        evidence = _evidence(args.evidence)
        if args.decision == "confirm":
            _confirmation(evidence)
        payload = {"schema_version": 1, "finding_id": args.id,
                   "finding_sha256": _digest(finding), "decision": args.decision,
                   "actor": actor, "evidence": evidence}
        request_id = args.request_id or "resolution-" + _digest(payload)
        payload["request_id"] = request_id
        envelope = sign(RESOLUTION_PURPOSE, payload, rt.outbox._key)
        result = maintenance.request(rt, request_id, repos=[finding["unit"]["repo"]],
                                     options={"resolution": envelope})
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        rt.ledger.close()
