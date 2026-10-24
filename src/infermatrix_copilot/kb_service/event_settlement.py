"""Complete source intake after explicit owner review, merge and activation.

These receipts confer no knowledge-history trust and invent no model verdict.
They record the operator's dispositions of immutable, selected source events.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from ..knowledge_service.lifecycle import CITATION, Page, safe_source_path
from ..knowledge_service.signing import canonical_json, sign, verify
from ..persistence import immutable_write_bytes
from .activate import activation_lock, verify_snapshot
from .reconcile import (
    ReconciliationError, _digest, _merge_evidence, _repository, _validate_target, trusted_commits,
)

PLAN_PURPOSE = "kb-reviewed-event-plan"
RECEIPT_PURPOSE = "kb-reviewed-event-settlement"


def _source(event: dict) -> dict:
    return {k: event[k] for k in ("id", "repo", "source", "external_id", "created_at", "status", "detail")} | {
        "payload_sha256": _digest(event["payload"])}


def _merged_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ReconciliationError("upstream merge time must be an explicit timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReconciliationError("upstream merge time is invalid") from exc
    if parsed.tzinfo is None:
        raise ReconciliationError("upstream merge time must have a timezone")
    return parsed


def _historical_merge(rt, event: dict, item: dict) -> dict:
    """Attest missing historical metadata without rewriting the source payload."""
    payload = event["payload"]
    expected = item.get("source_merge_sha")
    if "merge_commit_sha" in payload:
        sha = payload["merge_commit_sha"]
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ReconciliationError("original source merge SHA is invalid")
        if "source_merge_sha" in item:
            if expected != sha:
                raise ReconciliationError("supplied source merge SHA conflicts with the immutable payload")
            raise ReconciliationError("source_merge_sha is only for an original payload missing its merge SHA")
        return {}
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise ReconciliationError("historical source requires an explicit full source_merge_sha")
    lifecycle = rt.registry.get(event["repo"])
    upstream = getattr(lifecycle, "full_name", "")
    if not isinstance(upstream, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", upstream):
        raise ReconciliationError("historical source requires the configured upstream repository")
    number = int(event["external_id"])
    pr = rt.github.get(f"/repos/{upstream}/pulls/{number}")
    if not isinstance(pr, dict):
        raise ReconciliationError("upstream PR evidence must be an object")
    base, head_data = pr.get("base"), pr.get("head")
    if not isinstance(base, dict) or not isinstance(base.get("repo"), dict) or not isinstance(head_data, dict):
        raise ReconciliationError("upstream PR repository and head evidence are malformed")
    base, head = base["repo"], head_data.get("sha")
    if type(pr.get("number")) is not int or pr["number"] != number \
            or not isinstance(base.get("full_name"), str) or base["full_name"].lower() != upstream.lower() \
            or pr.get("html_url") != f"https://github.com/{upstream}/pull/{number}" \
            or pr.get("merged") is not True or pr.get("merge_commit_sha") != expected \
            or not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ReconciliationError("historical source has unmatched upstream PR merge evidence")
    merged_at = pr.get("merged_at")
    merged_time = _merged_time(merged_at)
    if "merged_at" in payload and _merged_time(payload["merged_at"]) != merged_time:
        raise ReconciliationError("upstream merge time conflicts with the immutable payload")
    return {"source_merge_sha": expected, "upstream_merge": {
        "repository": upstream, "number": number, "url": pr["html_url"],
        "merge_sha": expected, "head_sha": head, "merged_at": merged_at}}


def _active(rt, target: str, manifest: str) -> None:
    snapshot = rt.state_dir / "snapshots" / target
    link = rt.state_dir / "active"
    if rt.ledger.active_snapshot() != target or not link.is_symlink() or link.resolve() != snapshot.resolve():
        raise ReconciliationError("event settlement requires the exact target to be active")
    verify_snapshot(snapshot)
    if _digest(json.loads((snapshot / "MANIFEST.json").read_text())) != manifest:
        raise ReconciliationError("active snapshot differs from the verified target tree")


def prepare(rt, *, target: str, allow_mergers: list[str], required_checks: list[str], reason: str,
            coverage: dict, settled: dict | None = None, receipt: str = "") -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", target) or not reason.strip() or not allow_mergers or not required_checks:
        raise ReconciliationError("target SHA, owner allowlist, required checks and reason are mandatory")
    if not isinstance(coverage, dict) or set(coverage) != {"commits", "events"} \
            or not isinstance(coverage["commits"], list) or not isinstance(coverage["events"], list) \
            or not coverage["commits"] or not coverage["events"]:
        raise ReconciliationError("coverage requires explicit merged knowledge commits and selected events")
    if any(not isinstance(sha, str) for sha in coverage["commits"]):
        raise ReconciliationError("coverage commits must be immutable merge SHA strings")
    if rt.knowledge.fetch() != target:
        raise ReconciliationError("main differs from the explicitly pinned target")
    repository, allowed = _repository(rt.knowledge), {a.lower() for a in allow_mergers}
    actor = rt.github.get("/user").get("login", "")
    if actor.lower() not in allowed:
        raise ReconciliationError("authenticated operator is not an allowlisted owner")
    trusted = trusted_commits(rt)
    first_parent = set(rt.knowledge._git("rev-list", "--first-parent", target).decode().split())
    commits = []
    for sha in sorted(set(coverage["commits"])):
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or sha not in trusted or sha not in first_parent:
            raise ReconciliationError("coverage merge must already have supervised admission on the target history")
        parents = rt.knowledge._git("show", "-s", "--format=%P", sha).decode().split()
        manifest = rt.knowledge.raw_manifest(parents[0], sha)
        if not any(row["path"].startswith("knowledge/") for row in manifest):
            raise ReconciliationError("coverage commit must merge actual knowledge content")
        commit = {"sha": sha, "parents": parents, "manifest_sha256": _digest(manifest)}
        commit["pr"] = _merge_evidence(rt.github, repository, commit, allowed, set(required_checks))
        commits.append(commit)
    validation = _validate_target(rt.knowledge, target)
    _active(rt, target, validation["snapshot_manifest_sha256"])
    files, rows, seen = rt.knowledge.knowledge_files(target), [], set()
    previous = {r["source_event"]["id"]: r for r in (settled or {}).get("events", [])}
    for item in coverage["events"]:
        if not isinstance(item, dict) or set(item) - {"id", "outcome", "rules", "reason", "source_merge_sha"}:
            raise ReconciliationError("invalid selected event coverage")
        event_id, outcome, reason_text = item.get("id"), item.get("outcome"), item.get("reason", "")
        if type(event_id) is not int or event_id in seen or outcome not in {"covered", "already_covered", "no_rule"}:
            raise ReconciliationError("event IDs must be unique integers with explicit dispositions")
        if not isinstance(reason_text, str) or not reason_text.strip():
            raise ReconciliationError("every event disposition needs the operator's review reason")
        seen.add(event_id)
        event = rt.ledger.event(event_id)
        if event["source"] != "merged_pr" or not re.fullmatch(r"[1-9][0-9]*", event["external_id"]) \
                or not isinstance(event["payload"], dict):
            raise ReconciliationError("settlement requires an immutable merged upstream PR event")
        source = _source(event)
        prior = previous.get(event_id)
        if prior and event["status"] == "done":
            detail = json.loads(event["detail"])
            if detail.get("receipt") == receipt and detail.get("outcome") == "manual_reviewed_merged":
                source["status"], source["detail"] = prior["source_event"]["status"], prior["source_event"]["detail"]
        premature_discard = source["status"] == "done" and source["detail"] == "no rules"
        if source["status"] not in {"pending", "rejected", "drafted"} and not premature_discard:
            raise ReconciliationError("only pending, rejected, drafted or original done/no rules events can be settled")
        historical = _historical_merge(rt, event, item)
        rules = item.get("rules", [])
        if not isinstance(rules, list) or (outcome == "no_rule" and rules) or (outcome != "no_rule" and not rules):
            raise ReconciliationError("covered events need exact rule references; no-rule events cannot claim rules")
        proved = []
        for reference in rules:
            if not isinstance(reference, dict) or set(reference) != {"path", "rule_id"}:
                raise ReconciliationError("rules require exact knowledge-relative path and rule_id")
            path, rule_id = reference["path"], reference["rule_id"]
            if not safe_source_path(path) or not path.startswith(f"repos/{event['repo']}/") or path not in files:
                raise ReconciliationError("covered rule must be on a canonical page in the event's repository")
            matches = [section for section in Page.parse(files[path]).rules() if section.rule_id == rule_id]
            if len(matches) != 1 or matches[0].footer.status != "active":
                raise ReconciliationError("covered rule is missing, duplicated or retired")
            if f"PR #{event['external_id']}" not in {m.group("ref") for m in CITATION.finditer(matches[0].text)}:
                raise ReconciliationError("covered rule must cite the original upstream PR")
            proved.append({**reference, "section_sha256": _digest(matches[0].text)})
        rows.append({"source_event": source, "outcome": outcome, "reason": reason_text.strip(),
                     "rules": sorted(proved, key=lambda r: (r["path"], r["rule_id"])), **historical})
    changesets = rt.ledger.changesets_for_events(seen)
    previous_changesets = {row["id"]: row for row in (settled or {}).get("changesets", [])}
    for change in changesets:
        if change["pr_number"] is not None or change["pending_item"]:
            raise ReconciliationError("source changeset has a PR or pending publication; settle it first")
        if change["status"] == "manual_reviewed_merged" and change["detail"].get("manual_reviewed_event_settlement") == receipt:
            prior = previous_changesets.get(change["id"])
            if not prior:
                raise ReconciliationError("settled changeset is not covered by this receipt")
            change["status"] = prior["status"]
            change["detail"].pop("manual_reviewed_event_settlement")
    if rt.knowledge.fetch() != target:
        raise ReconciliationError("main changed during event review")
    _active(rt, target, validation["snapshot_manifest_sha256"])
    return {"schema_version": 1, "repository": repository, "target": target,
            "target_tree": rt.knowledge._git("rev-parse", f"{target}^{{tree}}").decode().strip(),
            "actor": actor, "reason": reason.strip(), "allow_mergers": sorted(allowed),
            "required_checks": sorted(set(required_checks)), "commits": commits,
            "events": sorted(rows, key=lambda r: r["source_event"]["id"]),
            "changesets": [{k: row[k] for k in ("id", "repo", "status", "detail")} for row in changesets],
            **validation, "prepared_at": rt.clock()}


def make_plan(rt, key, **options) -> dict:
    return sign(PLAN_PURPOSE, prepare(rt, **options), key)


def _receipts(rt, public) -> list[tuple]:
    directory = rt.state_dir / "event-settlements"
    if directory.is_symlink():
        raise ReconciliationError("event settlement directory must not be a symlink")
    result = []
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise ReconciliationError("event settlement receipts must be regular immutable files")
        envelope = json.loads(path.read_text())
        if path.stem != _digest(envelope):
            raise ReconciliationError("event settlement receipt was modified")
        payload = verify(RECEIPT_PURPOSE, envelope, public)
        if payload.get("schema_version") != 1 or payload.get("repository") != _repository(rt.knowledge):
            raise ReconciliationError("event settlement receipt identifies another repository")
        result.append((path, payload))
    return result


def apply_plan(rt, envelope: dict, key):
    planned = verify(PLAN_PURPOSE, envelope, key.public_key())
    with rt.ledger.lease() as owner, activation_lock(rt.state_dir):
        existing = next(((path, data) for path, data in _receipts(rt, key.public_key())
                         if data.get("plan_sha256") == _digest(envelope)), None)
        coverage = {"commits": [row["sha"] for row in planned["commits"]],
                    "events": [{"id": row["source_event"]["id"], "outcome": row["outcome"], "reason": row["reason"],
                                "rules": [{k: ref[k] for k in ("path", "rule_id")} for ref in row["rules"]],
                                **({"source_merge_sha": row["source_merge_sha"]} if "source_merge_sha" in row else {})}
                               for row in planned["events"]]}
        fresh = prepare(rt, target=planned["target"], allow_mergers=planned["allow_mergers"],
                        required_checks=planned["required_checks"], reason=planned["reason"], coverage=coverage,
                        settled=existing[1] if existing else None, receipt=existing[0].stem if existing else "")
        if {k: v for k, v in fresh.items() if k != "prepared_at"} != {k: v for k, v in planned.items() if k != "prepared_at"}:
            raise ReconciliationError("source events or reviewed evidence changed; prepare a new plan")
        if existing:
            path = existing[0]
        else:
            receipt = sign(RECEIPT_PURPOSE, {**fresh, "settled_at": rt.clock(), "plan_sha256": _digest(envelope)}, key)
            directory = rt.state_dir / "event-settlements"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{_digest(receipt)}.json"
            immutable_write_bytes(path, canonical_json(receipt))
        rt.ledger.settle_reviewed_events(owner, fresh["events"], fresh["changesets"], path.stem)
        rt.trace("outcome", context={"playbook": "kb-reconcile-reviewed", "repo": fresh["repository"]},
                 result={"outcome": "manual_reviewed_merged", "target": fresh["target"], "receipt": path.name,
                         "actor": fresh["actor"], "events": len(fresh["events"])})
        return path
