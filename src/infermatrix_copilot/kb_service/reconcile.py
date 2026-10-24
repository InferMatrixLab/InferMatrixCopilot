"""Explicit admission of owner-merged initialization/admin knowledge commits.

Automatic candidates still use the full knowledge gate. This operator path
records independently checked GitHub merge evidence and a verified final tree;
it never calls a merge, disposes of an unknown, or changes publication modes.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

from ..knowledge_service.signing import canonical_json, sign, verify
from ..persistence import immutable_write_bytes
from ..git_objects import tree_entries
from .activate import activation_lock, build_snapshot, verify_snapshot

PLAN_PURPOSE = "kb-reconciliation-plan"
RECEIPT_PURPOSE = "kb-reviewed-reconciliation"


class ReconciliationError(ValueError):
    """The evidence cannot admit the pinned knowledge history."""


def _digest(value) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _repository(knowledge) -> str:
    remote = knowledge._git("remote", "get-url", knowledge.remote).decode().strip()
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?", remote)
    if not match:
        raise ReconciliationError("knowledge origin must identify a GitHub repository")
    return match[1]


def _commits(knowledge, base: str, target: str) -> list[dict]:
    if not base or not knowledge.is_ancestor(base, target):
        raise ReconciliationError("target must descend from the active snapshot")
    records = []
    previous = base
    for sha, parents, _at, _message in knowledge.first_parent_commits(base, target):
        if not parents or parents[0] != previous:
            raise ReconciliationError("active snapshot is not on the target's first-parent chain")
        manifest = knowledge.raw_manifest(parents[0], sha)
        if any(e["path"].startswith("knowledge/") for e in manifest):
            records.append({"sha": sha, "parents": parents, "manifest_sha256": _digest(manifest)})
        previous = sha
    if previous != target:
        raise ReconciliationError("target first-parent history is incomplete")
    return records


def _pages(github, endpoint: str, *, field: str | None = None) -> list[dict]:
    result = []
    for page in range(1, 101):
        response = github.get(endpoint, per_page=100, page=page)
        batch = response.get(field) if field else response
        if not isinstance(batch, list):
            raise ReconciliationError(f"malformed GitHub evidence: {endpoint}")
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise ReconciliationError(f"incomplete GitHub evidence: {endpoint}")


def _merge_evidence(github, repository: str, commit: dict, allowed: set[str], required: set[str]) -> dict:
    sha = commit["sha"]
    root = f"/repos/{repository}"
    prs = [p for p in _pages(github, f"{root}/commits/{sha}/pulls")
           if p.get("merged_at") and p.get("merge_commit_sha") == sha
           and (p.get("base", {}).get("repo") or {}).get("full_name", "").lower() == repository.lower()]
    if len(prs) != 1:
        raise ReconciliationError(f"{sha[:12]} must match exactly one merged PR")
    pr = github.get(f"{root}/pulls/{prs[0]['number']}")
    merger = (pr.get("merged_by") or {}).get("login", "")
    head = (pr.get("head") or {}).get("sha", "")
    if not pr.get("merged_at") or pr.get("merge_commit_sha") != sha or merger.lower() not in allowed:
        raise ReconciliationError(f"PR #{pr.get('number')} has unmatched merge evidence or disallowed merger")
    if len(commit["parents"]) == 2 and commit["parents"][1] != head:
        raise ReconciliationError(f"PR #{pr['number']} head does not match the actual merge parent")
    checks = _pages(github, f"{root}/commits/{head}/check-runs", field="check_runs")
    statuses = _pages(github, f"{root}/commits/{head}/statuses")
    observed = [{"kind": "check", "app_id": (r.get("app") or {}).get("id", 0), "name": r["name"],
                 "state": r.get("conclusion") if r.get("status") == "completed" else "pending",
                 "id": r["id"], "head_sha": r.get("head_sha")} for r in checks]
    observed += [{"kind": "status", "app_id": 0, "name": r["context"], "state": r["state"],
                  "id": r["id"], "head_sha": head}
                 for r in statuses]
    latest = {}
    for row in sorted(observed, key=lambda r: r["id"]):
        latest[row["kind"], row["app_id"], row["name"]] = row
    observed = list(latest.values())
    # Combined commit status is 'pending' even when the statuses list is empty.
    # Require named evidence instead; no absence or aggregate counts as success.
    for name in required:
        found = [r for r in observed if r["name"] == name]
        if not found or any(r["state"] != "success" or r["head_sha"] != head for r in found):
            raise ReconciliationError(f"PR #{pr['number']} required check {name!r} is missing or unsuccessful")
    if any(r["state"] not in {"success", "neutral", "skipped"} for r in observed):
        raise ReconciliationError(f"PR #{pr['number']} has failed or pending checks")
    reviews = _pages(github, f"{root}/pulls/{pr['number']}/reviews")
    return {"number": pr["number"], "url": pr["html_url"], "head_sha": head,
            "merge_sha": sha, "merged_at": pr["merged_at"], "merged_by": merger,
            "checks": sorted(observed, key=lambda r: (r["name"], r["id"])),
            "reviews": [{"id": r["id"], "state": r["state"], "commit_id": r["commit_id"],
                         "user": (r.get("user") or {}).get("login", "")} for r in reviews]}


def _validate_target(knowledge, target: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="kb-reconcile-") as temporary:
        root = Path(temporary)
        source = knowledge.export(target, root / "source")
        # Validators also inspect tracked local/ paths. Give the exported tree
        # an exact temporary index without checking out or modifying the live
        # clone; its object store is referenced read-only through alternates.
        subprocess.run(["git", "init", "-q", str(source)], check=True, capture_output=True)
        objects = knowledge._git("rev-parse", "--path-format=absolute", "--git-path", "objects").decode().strip()
        (source / ".git" / "objects" / "info" / "alternates").write_text(objects + "\n")
        subprocess.run(["git", "-C", str(source), "read-tree", target], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source), "update-ref", "HEAD", target], check=True, capture_output=True)
        # git archive attributes must not silently hide a knowledge file from
        # the validators. Served text must be regular UTF-8, never a symlink.
        listing = knowledge._git("ls-tree", "-r", "-z", target, "--", "knowledge/")
        for mode, kind, oid, raw_path in tree_entries(listing):
            path = raw_path.decode("utf-8")
            exported = source / path
            if kind != "blob" or not exported.is_file() or exported.is_symlink():
                raise ReconciliationError(f"knowledge file was omitted or is not regular: {path}")
            actual = knowledge._git("hash-object", "--no-filters", str(exported)).decode().strip()
            if actual != oid:
                raise ReconciliationError(f"exported knowledge differs from the exact git tree: {path}")
            if path.endswith((".md", ".yaml")):
                if mode != "100644":
                    raise ReconciliationError(f"served knowledge has an unsupported mode: {path}")
                exported.read_text(encoding="utf-8", errors="strict")
        from ..knowledge_service.validation import validate_workspace

        validation = validate_workspace(source, timeout_seconds=180, stop_on_failure=True)
        if not validation.passed:
            raise ReconciliationError("; ".join(validation.problems()))
        reports = [execution.receipt() for execution in validation.executions]
        snapshot = build_snapshot(root / "state", target, knowledge.knowledge_files(target),
                                  extra=knowledge.top_level_knowledge(target))
        verify_snapshot(snapshot)
        return {"validators": reports, "snapshot_manifest_sha256":
                _digest(json.loads((snapshot / "MANIFEST.json").read_text()))}


def prepare(rt, *, target: str, allow_mergers: list[str], required_checks: list[str], reason: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", target) or not reason.strip() or not allow_mergers or not required_checks:
        raise ReconciliationError("a full target SHA, reason, allowlisted mergers and required checks are mandatory")
    if rt.knowledge.fetch() != target:
        raise ReconciliationError("main differs from the explicitly pinned target")
    base = rt.ledger.active_snapshot()
    repository = _repository(rt.knowledge)
    allowed = {a.lower() for a in allow_mergers}
    actor = rt.github.get("/user").get("login", "")
    if actor.lower() not in allowed:
        raise ReconciliationError("authenticated operator is not an allowlisted owner")
    commits = _commits(rt.knowledge, base, target)
    if not commits:
        raise ReconciliationError("no knowledge commits need reconciliation")
    for commit in commits:
        unknown = rt.ledger.get_cursor("*", "unknown:" + commit["sha"])
        record = json.loads(unknown) if unknown else {}
        acceptance = json.loads(rt.ledger.get_cursor("*", "accept_request:" + commit["sha"]) or "{}")
        if acceptance.get("state") == "requested":
            raise ReconciliationError("a pending acceptance must settle before supervised reconciliation")
        if record.get("state") not in {None, "people", "open", "accepted"} or record.get("revert_changeset"):
            raise ReconciliationError("a pending revert/acceptance must settle before supervised reconciliation")
        for lifecycle in rt.registry.values():
            candidate = rt.ledger.event_by_external_id(lifecycle.repo, "unrecorded", commit["sha"])
            if candidate and candidate["status"] == "pending":
                raise ReconciliationError("an unrecorded intake candidate must settle before reconciliation")
        commit["pr"] = _merge_evidence(rt.github, repository, commit, allowed, set(required_checks))
    validation = _validate_target(rt.knowledge, target)
    if rt.knowledge.fetch() != target or rt.ledger.active_snapshot() != base:
        raise ReconciliationError("main or active snapshot changed during validation; prepare a new plan")
    return {"schema_version": 1, "repository": repository, "active_snapshot": base, "target": target,
            "target_tree": rt.knowledge._git("rev-parse", f"{target}^{{tree}}").decode().strip(),
            "actor": actor, "reason": reason.strip(), "allow_mergers": sorted(allowed),
            "required_checks": sorted(set(required_checks)), "commits": commits,
            **validation, "prepared_at": rt.clock()}


def make_plan(rt, key, **options) -> dict:
    return sign(PLAN_PURPOSE, prepare(rt, **options), key)


def apply_plan(rt, envelope: dict, key) -> Path:
    planned = verify(PLAN_PURPOSE, envelope, key.public_key())
    with activation_lock(rt.state_dir):
        fresh = prepare(rt, target=planned["target"], allow_mergers=planned["allow_mergers"],
                        required_checks=planned["required_checks"], reason=planned["reason"])
        if {k: v for k, v in fresh.items() if k != "prepared_at"} != {
                k: v for k, v in planned.items() if k != "prepared_at"}:
            raise ReconciliationError("reconciliation evidence changed; prepare a new plan")
        directory = rt.state_dir / "reconciliations"
        if directory.is_symlink():
            raise ReconciliationError("reconciliation directory must not be a symlink")
        directory.mkdir(parents=True, exist_ok=True)
        for existing in directory.glob("*.json"):
            prior = json.loads(existing.read_text())
            payload = verify(RECEIPT_PURPOSE, prior, key.public_key())
            if existing.stem != _digest(prior):
                raise ReconciliationError("existing reconciliation receipt was modified")
            if payload.get("plan_sha256") == _digest(envelope):
                return existing
        receipt = sign(RECEIPT_PURPOSE, {**fresh, "admitted_at": rt.clock(), "plan_sha256": _digest(envelope)}, key)
        path = directory / f"{_digest(receipt)}.json"
        # Plans have a separate signature purpose and cannot be installed as
        # receipts. A successfully applied receipt is never replaced in place.
        immutable_write_bytes(path, canonical_json(receipt))
        rt.trace("outcome", context={"playbook": "kb-reconcile-reviewed", "repo": fresh["repository"]},
                 result={"outcome": "supervised_admission", "target": fresh["target"], "receipt": path.name,
                         "actor": fresh["actor"], "commits": len(fresh["commits"])})
        return path


def trusted_commits(rt) -> set[str]:
    """Re-verify immutable receipts before any audit, activation or control trust."""
    public = rt.outbox._key.public_key() if rt.outbox is not None else None
    return read_receipts(rt.knowledge, rt.state_dir, public)


def settle_reconciled_holds(rt) -> list[int]:
    """Resolve old provenance alerts only after receipt-backed activation.

    Admission already makes these commits trusted; the old queue alerts must
    also stop asking an operator to revert them. Candidate-rule holds and
    unadmitted commits remain untouched.
    """
    holds = [row for row in rt.ledger.human_queue() if row["changeset_id"] is None]
    active = rt.ledger.active_snapshot()
    if not holds or not active or not getattr(rt, "lease_owner", None):
        return []
    trusted = trusted_commits(rt)  # re-verifies every receipt and its exact history
    receipts = {}
    for path in sorted((rt.state_dir / "reconciliations").glob("*.json")):
        payload = verify(RECEIPT_PURPOSE, json.loads(path.read_text()), rt.outbox._key.public_key())
        for commit in payload["commits"]:
            if commit["sha"] in trusted:
                receipts[commit["sha"]] = path.name
    rows = []
    for hold in holds:
        match = re.match(r"(?:provenance: )?unrecorded knowledge change ([0-9a-f]{12})\b", hold["reason"])
        if match is None:
            continue
        matches = [sha for sha in trusted if sha.startswith(match[1])]
        if len(matches) != 1 or not rt.knowledge.is_ancestor(matches[0], active):
            continue
        sha = matches[0]
        rows.append({"hold": hold, "resolution": {
            "kind": "reconciled_provenance", "commit": sha,
            "receipt": receipts[sha], "active_snapshot": active,
        }})
    if rows:
        rt.ledger.settle_reviewed_holds(rt.lease_owner, rows)
        rt.trace("outcome", context={"playbook": "kb-reconcile-reviewed", "repo": "*"},
                 result={"outcome": "provenance_holds_resolved", "ids": [r["hold"]["id"] for r in rows],
                         "active_snapshot": active})
    return [row["hold"]["id"] for row in rows]


def read_receipts(knowledge, state_dir: Path, public) -> set[str]:
    directory = state_dir / "reconciliations"
    if directory.is_symlink():
        raise ReconciliationError("reconciliation directory must not be a symlink")
    if not directory.exists():
        return set()
    if public is None:
        raise ReconciliationError("supervised receipts require the configured service signing key")
    trusted = set()
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise ReconciliationError("reconciliation receipts must be regular immutable files")
        envelope = json.loads(path.read_text())
        if path.stem != _digest(envelope):
            raise ReconciliationError("reconciliation receipt content differs from its immutable name")
        payload = verify(RECEIPT_PURPOSE, envelope, public)
        if payload.get("schema_version") != 1 or payload["repository"] != _repository(knowledge):
            raise ReconciliationError("reconciliation receipt does not identify this knowledge repository")
        actual = _commits(knowledge, payload["active_snapshot"], payload["target"])
        recorded = [{k: c[k] for k in ("sha", "parents", "manifest_sha256")} for c in payload["commits"]]
        if recorded != actual or payload["target_tree"] != knowledge._git(
                "rev-parse", f"{payload['target']}^{{tree}}").decode().strip():
            raise ReconciliationError("reconciliation receipt does not cover the exact pinned history")
        trusted.update(c["sha"] for c in actual)
    return trusted
