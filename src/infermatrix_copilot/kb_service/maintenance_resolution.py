"""Validate signed owner dispositions and append evidence under the scheduler lease."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import PurePosixPath

from ..knowledge_service.signing import canonical_json, sign, verify
from .maintenance_audit import source_evidence
from .maintenance_calibration import cases_directory
from .outbox import atomic_write_json

RESOLUTION_PURPOSE = "kb-maintenance-resolution-request"
OUTCOMES = {"verified", "contradicted", "unknown"}
PROOF_FIELDS = ("repository", "sha", "path", "start_line", "end_line", "content_sha256")

def _digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()

def _owners():
    owners = {name.casefold() for name in re.split(r"[,\s]+", os.environ.get("KB_MAINTENANCE_OWNERS", "")) if name}
    if not owners:
        raise ValueError("KB_MAINTENANCE_OWNERS must explicitly allow the authenticated owner")
    return owners

def _oracle(evidence):
    kind = evidence.get("calibration_kind", "audit")
    if kind not in {"audit", "correction"}:
        raise ValueError("calibration_kind must be audit or correction")
    oracle = evidence.get("correction_oracle")
    if kind == "correction":
        if not isinstance(oracle, dict) or oracle.get("expected_gate") not in {"pass", "fail", "human"}:
            raise ValueError("a correction case requires an explicit correction_oracle.expected_gate")
        if oracle["expected_gate"] == "pass" and not re.fullmatch(r"[a-f0-9]{64}", str(oracle.get("expected_page_sha256", ""))):
            raise ValueError("a passing correction oracle requires the normalized corrected page SHA-256")
    elif oracle is not None:
        raise ValueError("correction_oracle requires calibration_kind=correction")
    return kind, oracle

def _confirmation(evidence):
    if evidence.get("expected") not in OUTCOMES:
        raise ValueError("confirm requires explicit expected=verified|contradicted|unknown")
    witnesses = evidence.get("witnesses")
    if not isinstance(witnesses, list) or not witnesses or any(
        not isinstance(row, dict) or any(field not in row for field in PROOF_FIELDS) for row in witnesses
    ):
        raise ValueError("confirm requires immutable original-source witnesses with repository, SHA, path, span and SHA-256")
    if len(witnesses) > 64:
        raise ValueError("confirm requires a bounded original-source witness packet")
    for row in witnesses:
        path = row["path"]
        if (not isinstance(row["repository"], str) or not row["repository"]
                or not isinstance(path, str) or not path or PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts or "\\" in path
                or not isinstance(row["sha"], str) or not re.fullmatch(r"[a-f0-9]{40}(?:[a-f0-9]{24})?", row["sha"])
                or not isinstance(row["content_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", row["content_sha256"])
                or type(row["start_line"]) is not int or type(row["end_line"]) is not int
                or not 1 <= row["start_line"] <= row["end_line"]
                or ("quote" in row and (not isinstance(row["quote"], str) or not row["quote"].strip()))):
            raise ValueError("original-source witness identity, span or hash is malformed")
    _oracle(evidence)

def apply_resolution(rt, store, request):
    """Revalidate an owner request under the scheduler lease; no model calls."""
    if not rt.lease_owner or store.lease_owner != rt.lease_owner:
        raise ValueError("owner resolution requires the live scheduler lease")
    if rt.outbox is None:
        raise ValueError("owner resolution requires the service signing key")
    payload = verify(RESOLUTION_PURPOSE, request["detail"]["resolution"], rt.outbox._key.public_key())
    if payload.get("schema_version") != 1 or payload.get("request_id") != request["id"]:
        raise ValueError("resolution request identity mismatch")
    actor, decision = payload.get("actor"), payload.get("decision")
    if not isinstance(actor, str) or actor.casefold() not in _owners() or decision not in {"confirm", "dismiss"}:
        raise ValueError("resolution is not authorized by a current maintenance owner")
    finding = store.finding(payload["finding_id"])
    if finding is None or payload.get("finding_sha256") != _digest(finding) or request["repo"] != finding["unit"]["repo"]:
        raise ValueError("resolution does not bind the immutable original finding")
    evidence = payload["evidence"]
    if not isinstance(evidence, dict) or not str(evidence.get("reason", "")).strip():
        raise ValueError("resolution evidence requires a reason")
    recorded = dict(evidence)
    case_path = None
    if decision == "confirm":
        _confirmation(evidence)
        unit = finding["unit"]
        witnesses = source_evidence(rt, rt.registry[unit["repo"]], unit)
        available = {tuple(row[field] for field in PROOF_FIELDS): row for row in witnesses}
        verified = []
        for row in evidence["witnesses"]:
            proof = available.get(tuple(row[field] for field in PROOF_FIELDS))
            if proof is None or (row.get("quote") and row["quote"] not in proof["excerpt"]):
                raise ValueError("owner witness does not match the immutable original source")
            verified.append(proof)
        kind, oracle = _oracle(evidence)
        case_id = _digest({"request": request["id"], "finding": finding["id"]})
        case = {"id": case_id, "actor": actor, "expected": evidence["expected"],
                "calibration_kind": kind, "unit": unit, "finding_id": finding["id"],
                "original_outcome": finding["outcome"], "reason": evidence["reason"],
                "source_evidence": verified}
        if oracle is not None:
            case["correction_oracle"] = oracle
        # Repo adapter IDs are path components, never caller-supplied paths.
        repo = str(unit["repo"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", repo) or repo in {".", ".."}:
            raise ValueError("repository ID is not safe for a calibration case directory")
        case_path = cases_directory() / repo / "cases" / f"{case_id}.json"
        if case_path.exists():
            existing = verify("kb-maintenance-human-case", json.loads(case_path.read_text()), rt.outbox._key.public_key())
            if existing != case:
                raise ValueError("human calibration case identity changed")
        else:
            # No unlabeled candidate is promoted implicitly; every label and
            # correction oracle above came from this signed owner request.
            with rt.ledger.fenced(rt.lease_owner):
                atomic_write_json(case_path, sign("kb-maintenance-human-case", case, rt.outbox._key))
                case_path.chmod(0o600)
        recorded["verified_original_sources"] = verified
        recorded["calibration_case"] = str(case_path)
    result = store.record_resolution(finding["id"], decision, actor, recorded,
                                     resolution_id=request["id"], now=rt.clock())
    return {**result, "calibration_case": str(case_path) if case_path else None}
