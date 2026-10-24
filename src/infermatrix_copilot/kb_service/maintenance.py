"""One durable nightly challenge/correction lane in the existing knowledge service."""

from __future__ import annotations

from .model_identity import model_family

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path, PurePosixPath

from . import merge
from .gate import check_consistency
from .maintenance_audit import BudgetGateway, audit_unit, digest
from .maintenance_calibration import calibrate, candidate, current
from .maintenance_correction import propose_correction
from .maintenance_policy import (
    settings, policy_digest, eligible_repos as _eligible, readiness,
    correction_publishable, required_ci,
)

from .maintenance_store import BudgetExceeded, MaintenanceStore
from .maintenance_units import enumerate_units, select_units
from .models import ModelUnavailable
from .outbox import atomic_write_json

# Compatibility for existing internal callers; new callers use maintenance_policy.
_required_ci = required_ci


def _units(rt, snapshot, repos=None):
    files = rt.knowledge.knowledge_files(snapshot)
    units = []
    for lifecycle in rt.registry.values():
        if lifecycle.enabled and (repos is None or lifecycle.repo in repos):
            units.extend(enumerate_units(files, lifecycle, snapshot))
    return files, units


def plan(rt, repos=None):
    config = settings(rt)
    snapshot = rt.ledger.active_snapshot()
    if not snapshot:
        return {"status": "no_active_knowledge", "selected": [], "enabled": config.enabled}
    _, units = _units(rt, snapshot, repos)
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    policy = policy_digest(rt, config)
    report = store.report(units=units, now=rt.clock(), policy_sha256=policy, eligible_repos=_eligible(rt))
    selected = select_units(units, store.history(), config.max_units, report["day"], store.usage(), now=rt.clock())
    return {"enabled": config.enabled, "snapshot": snapshot, "policy_sha256": policy,
            "report": report, "selected": [{k: u[k] for k in ("unit_id", "repo", "owner", "page", "block_id", "lane", "selection_reason")} for u in selected],
            "unpriced_roles": [role.label() for role in (rt.generator, rt.judge) if role.label() not in config.costs]}


def status(rt, repos=None):
    """Read the authoritative maintenance report without dispatching work."""
    result = plan(rt, repos=repos)
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    snapshot = result.get("snapshot")
    _, units = _units(rt, snapshot, repos) if snapshot else ({}, [])
    findings = [{"id": row["id"], "run_id": row["run_id"], "unit_id": row["unit_id"],
                 "repo": row["unit"]["repo"], "outcome": row["outcome"], "created_at": row["created_at"],
                 "reason": row["detail"].get("reason", "")}
                for row in store.findings() if repos is None or row["unit"]["repo"] in repos]
    return {**result, "requests": [r for r in store.pending_requests()
                                  if repos is None or r["repo"] in {*repos, "*"}],
            "runs": store.runs(), "findings": findings,
            "operational": operational_status(rt, store, units)}


def request(rt, request_id, repos=None, *, options=None):
    """Enqueue one repository or all; never claim the lease or call a model."""
    if repos is not None:
        if len(repos) != 1:
            raise ValueError("maintenance requests require one repository or all")
        repo = repos[0]
        resolution = bool((options or {}).get("resolution"))
        if repo not in rt.registry and not resolution:
            raise ValueError(f"unknown maintenance repository: {repo}")
        if not resolution and not rt.registry[repo].enabled:
            raise ValueError(f"maintenance repository is disabled: {repo}")
    else:
        repo = "*"
    detail = dict(options or {})
    if not detail.get("resolution"):
        detail = {"kind": "maintenance", "calibrate": False, "drill": False, **detail}
        if detail["calibrate"] and detail["drill"]:
            raise ValueError("maintenance calibration and drill are mutually exclusive")
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    return store.enqueue_request(request_id, repo, detail, now=rt.clock())


async def run_due_async(rt, *, on_correction=None):
    """Govern correction then audit; SQLite remains the domain checkpoint."""
    from ..app.workflow_execution import WorkflowExecution
    from ..config import Settings
    from ..engine.registry import StepRegistry
    from ..engine.step import StepResult, StepSpec
    from ..playbooks.store import Playbook, PlaybookStep
    from .maintenance_store import cycle_date

    if rt.lease_owner is None:
        raise ValueError("maintenance execution requires the scheduler lease")
    with rt.ledger.fenced(rt.lease_owner):
        pass
    identity = {"cycle_date": cycle_date(rt.clock()), "policy_sha256": policy_digest(rt)}
    fingerprint = digest(json.dumps(identity, sort_keys=True))
    run_dir = rt.state_dir / "runs" / "knowledge-maintenance" / fingerprint
    resources = {"rt": rt, "on_correction": on_correction, "results": {}}

    async def handle(ctx):
        active = ctx.runtime["rt"]
        phase = ctx.params["phase"]
        try:
            with active.ledger.fenced(active.lease_owner):
                pass  # every phase revalidates authority; no outer cached success
            result = advance_corrections(active) if phase == "corrections" else tick(active)
            if phase == "corrections" and ctx.runtime["on_correction"] is not None:
                for event in result:
                    ctx.runtime["on_correction"](event)
            ctx.runtime["results"][phase] = result
            return StepResult(True, summary=f"maintenance {phase}", checkpoint=False)
        except Exception as exc:
            ctx.runtime["error"] = exc
            raise

    registry, steps = StepRegistry(), []
    for phase, operation in (("corrections", "correction"), ("maintenance", "nightly_audit")):
        name = f"knowledge.maintain.{operation}"
        registry.register(StepSpec(name, "deterministic", "knowledge", handle, checkpoint=False))
        steps.append(PlaybookStep(phase, name, params={"phase": phase}))
    playbook = Playbook("knowledge-maintain", 1, "locked", [], [], steps)
    execution = WorkflowExecution(getattr(rt, "settings", None) or Settings(_env_file=None), registry)
    outcome = await execution.execute(playbook, run_dir=run_dir, state=identity,
                                      runtime=resources, fingerprint=fingerprint)
    if "error" in resources:
        raise resources["error"]
    if outcome.status != "done":
        raise RuntimeError(outcome.blocked_reason or "maintenance workflow failed")
    return resources["results"]


def run_due(rt, *, on_correction=None):
    """Synchronous compatibility entry; async schedulers await run_due_async."""
    return asyncio.run(run_due_async(rt, on_correction=on_correction))


def _human(rt, unit, reason, finding=None):
    identity = digest(json.dumps([unit["unit_id"], unit["content_sha256"], reason], sort_keys=True))
    if not rt.ledger.get_cursor(unit["repo"], "maintenance-human:" + identity):
        rt.ledger.enqueue_human(unit["repo"], f"maintenance owner={settings(rt).owner}; {unit['owner']}: {reason}")
        rt.ledger.set_cursor(unit["repo"], "maintenance-human:" + identity, str(rt.clock()))
    if finding:
        candidate(rt, unit, finding)


def _correction(rt, lifecycle, store, run, unit, finding, base):
    from .containment import pending_holds, record_hold

    previous_correction = None
    for previous in rt.ledger.changesets_of_kind(unit["repo"], "correction"):
        old = previous["detail"]
        if old.get("correction_unit") == unit["unit_id"] and old.get("corrected_hash") == unit["content_sha256"] \
                and old.get("original_unit", {}).get("upstream_pin") == unit.get("upstream_pin") \
                and old.get("maintenance_policy") == run["policy_sha256"] \
                and previous["status"] not in {"failed", "closed", "human", "gate_failed", "head_changed", "superseded", "shadow_recorded"}:
            previous_correction = previous
            break
    ready = readiness(rt, store, run["policy_sha256"]) and current(rt, run["policy_sha256"], observed_model=finding.get("reviewer"))
    enforce = ready and lifecycle.auto_merge and lifecycle.publishes
    hold = record_hold(rt, unit, finding, enforce=enforce)
    candidate(rt, unit, finding)
    impacts = impact(rt, store, unit, base)
    if previous_correction:
        return {"changeset_id": previous_correction["id"], "status": previous_correction["status"], "hold": hold, "impacts": impacts}
    if unit.get("protected"):
        _human(rt, unit, "protected claim contradicted; corresponding operation requires owner review", finding)
        return {"status": "human", "hold": hold, "impacts": impacts}
    try:
        proposed = propose_correction(rt, lifecycle, store, settings(rt), run_id=run["id"], unit=unit,
                                      finding=finding, base=base, base_sha=run["snapshot"])
    except (ValueError, ModelUnavailable) as exc:
        _human(rt, unit, str(exc), finding)
        proposed = {"status": "human", "reason": str(exc)}
    return {**proposed, "hold": hold, "impacts": impacts}


def tick(rt):
    """Called under kb serve's existing lease. No second scheduler or model loop."""
    config = settings(rt)
    if rt.lease_owner is None:
        if not config.enabled:
            return None
        raise ValueError("nightly maintenance must run under the scheduler lease")
    snapshot = rt.ledger.active_snapshot()
    if not snapshot:
        return {"status": "no_active_knowledge"}
    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
    policy = policy_digest(rt, config)
    requests = store.pending_requests()
    paused = merge.is_paused(rt.ledger, "*")
    requests = [r for r in requests if r["detail"].get("resolution") or r["detail"].get("drill") or
                (config.enabled and not paused and (r["repo"] == "*" or not merge.is_paused(rt.ledger, r["repo"])))]
    request = requests[0] if requests else None
    operator_request = request and (request["detail"].get("resolution") or request["detail"].get("drill"))
    if (not config.enabled or merge.is_paused(rt.ledger, "*")) and not operator_request:
        return None
    if not operator_request:
        # A waiver in another workflow cannot authorize semantic self-review.
        if model_family(rt.generator.model) == model_family(rt.judge.model):
            return {"status": "independent_reviewer_required"}
    selected_repos = None if request is None or request["repo"] == "*" else [request["repo"]]
    resumable = [r for r in store.runs() if r["status"] in {"running", "failed", "limited", "paused"}
                 and not r.get("request_id") and any(not i["outcome"] and not merge.is_paused(rt.ledger, i["unit"]["repo"]) for i in store.items(r["id"]))]
    run = resumable[0] if resumable and not request else store.begin_cycle(snapshot=snapshot, policy_sha256=policy, now=rt.clock(),
                            request_id=request["id"] if request else None, eligible_repos=_eligible(rt),
                            detail={"selected_repos": selected_repos})
    if run["status"] == "complete":
        if request:
            store.ack_request(request["id"], run["id"], now=rt.clock())
        return None
    if run["policy_sha256"] != policy:
        store.mark_run(run["id"], "cancelled", {"reason": "maintenance policy changed; next cycle requires new evidence"})
        if request:
            store.ack_request(request["id"], run["id"], now=rt.clock())
            rt.ledger.enqueue_human(request["repo"], "maintenance request cancelled after policy change; resubmit with a new request ID")
        return {"status": "policy_changed", "run_id": run["id"]}
    base, units = _units(rt, run["snapshot"], run["detail"].get("selected_repos"))
    selected = select_units(units, store.history(), config.max_units, run["cycle_date"], store.usage(), now=rt.clock())
    if not store.items(run["id"]):
        store.add_items(run["id"], selected, now=rt.clock())
    errors, limited = [], False
    if operator_request or request and request["detail"].get("calibrate"):
        try:
            if request["detail"].get("resolution"):
                from .maintenance_resolution import apply_resolution
                result = apply_resolution(rt, store, request)
            elif request["detail"].get("drill"):
                from .containment_drill import run_revocation_drill
                result = run_revocation_drill(rt, policy)
            else:
                result = calibrate(rt, store, config, run_id=run["id"], policy_sha256=policy)
        except (ValueError, OSError, RuntimeError) as exc:
            status = "limited" if isinstance(exc, BudgetExceeded) else "failed"
            store.mark_run(run["id"], status, {"operator_request_error": str(exc)}, now=rt.clock())
            if status == "failed":
                store.ack_request(request["id"], run["id"], now=rt.clock())
                rt.ledger.enqueue_human(request["repo"], "maintenance request failed: " + str(exc))
            return {"run_id": run["id"], "status": status, "reason": str(exc)}
        store.mark_run(run["id"], "complete", {"operator_result": result}, now=rt.clock())
        store.ack_request(request["id"], run["id"], now=rt.clock())
        return result
    checked_owners = set()
    if not any(i["unit"].get("lane") == "fair" and not i["outcome"] for i in store.items(run["id"])):
        store.release_fair_reserve(now=rt.clock(), reason="no pending fair rotation")
    for item in store.items(run["id"]):
        if item["outcome"]:
            continue
        unit = item["unit"]
        lifecycle = rt.registry[unit["repo"]]
        if merge.is_paused(rt.ledger, unit["repo"]):
            errors.append("repository paused: " + unit["repo"])
            continue
        rt.ledger.heartbeat(rt.lease_owner)
        gateway = BudgetGateway(rt, store, config, run_id=run["id"], unit=unit, phase="audit")
        try:
            finding = audit_unit(rt, lifecycle, unit, gateway)
            try:
                if finding["outcome"] == "contradicted":
                    finding["correction"] = _correction(rt, lifecycle, store, run, unit, finding, base)
                elif finding["outcome"] == "unknown":
                    _human(rt, unit, finding["reason"])
            except (BudgetExceeded, ModelUnavailable, ValueError, RuntimeError) as exc:
                # The original audit verdict is immutable even if a later
                # drafting/gating/consistency step cannot complete.
                finding["followup"] = {"status": "incomplete", "reason": str(exc)}
                if isinstance(exc, BudgetExceeded):
                    limited = True
                else:
                    errors.append(str(exc))
                _human(rt, unit, "audit follow-up incomplete: " + str(exc))
        except BudgetExceeded:
            limited = True
            continue  # the other lane may still have its reserved allowance
        except ModelUnavailable as exc:
            finding = {"outcome": "execution_error", "reason": str(exc), "original_source_checked": False}
            errors.append(str(exc))
        except ValueError as exc:
            finding = {"outcome": "unknown", "reason": str(exc), "original_source_checked": False}
            _human(rt, unit, str(exc))
        if unit["kind"] == "rule" and unit["owner"] not in checked_owners:
            checked_owners.add(unit["owner"])
            try:
                consistency = check_consistency(base, [unit["owner"]], gateway=gateway, judge=rt.judge, changed=None)
                finding["existing_consistency"] = consistency
                if any(check["verdict"] != "consistent" for check in consistency):
                    _human(rt, unit, "pre-existing owner knowledge conflict or uncertainty: " + json.dumps(consistency, ensure_ascii=False))
            except (BudgetExceeded, ModelUnavailable, ValueError) as exc:
                finding["consistency_followup"] = {"status": "incomplete", "reason": str(exc)}
                if isinstance(exc, BudgetExceeded):
                    limited = True
                else:
                    errors.append(str(exc))
        store.record_outcome(run["id"], unit["unit_id"], finding["outcome"], detail=finding, now=rt.clock())
    items = store.items(run["id"])
    if not any(i["unit"].get("lane") == "fair" and not i["outcome"] for i in items):
        store.release_fair_reserve(now=rt.clock(), reason="selected fair rotation completed")
    pending = any(not i["outcome"] for i in items)
    errors += [i["detail"].get("reason", "execution error") for i in items if i["outcome"] == "execution_error"]
    status = "limited" if limited else "failed" if pending or errors else "complete"
    store.mark_run(run["id"], status, {"budget_limited": limited, "errors": sorted(set(errors)),
                                      "eligible_denominator": len(units)}, now=rt.clock())
    report = {"run": store.run(run["id"]), "maintenance": store.report(units=units, now=rt.clock(),
               policy_sha256=policy, eligible_repos=_eligible(rt)), "results": items,
               **operational_status(rt, store, units)}
    atomic_write_json(rt.state_dir / "reports" / f"maintenance-{digest(run['id'])}.json", report)
    if request and not pending:
        store.ack_request(request["id"], run["id"], now=rt.clock())
    if errors and not pending:
        attempt = int((request or {}).get("detail", {}).get("retry_attempt", 0))
        if attempt < 2:
            root = (request or {}).get("detail", {}).get("retry_root", run["id"])
            store.enqueue_request(f"retry:{root}:{attempt + 1}", "*", detail={"retry_root": root, "retry_attempt": attempt + 1,
                "selected_repos": selected_repos}, now=rt.clock())
    return {"run_id": run["id"], "status": status, "reviewed": sum(i["outcome"] in {"verified", "contradicted"} for i in items),
            "attempted": sum(bool(i["outcome"]) for i in items),
            "budget_limited": limited}


def advance_corrections(rt):
    """Resume held publication and restore only verified newly active bytes."""
    from .containment import pending_holds, record_restoration
    from .runtime import publish
    events = []
    for lifecycle in rt.registry.values():
        if not lifecycle.enabled or merge.is_paused(rt.ledger, lifecycle.repo):
            continue
        for changeset in rt.ledger.changesets(lifecycle.repo, ("maintenance_held", "calibration_required", "merged")):
            if changeset["kind"] != "correction":
                continue
            detail = changeset["detail"]
            if changeset["status"] in {"maintenance_held", "calibration_required"}:
                if changeset["status"] == "calibration_required":
                    from .runtime import calibration_current
                    if not calibration_current(rt, lifecycle):
                        continue
                reason = correction_publishable(rt, changeset)
                if not reason:
                    rt.ledger.update_changeset(changeset["id"], status="gated")
                    events.append({"changeset_id": changeset["id"], "status": publish(rt, lifecycle, changeset["id"])})
                continue
            if detail.get("restoration_generation"):
                barrier = pending_holds(rt)
                if barrier.get("ready") and barrier.get("content_current"):
                    store = MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner)
                    for finding in store.findings(run_id=detail["maintenance_run"]):
                        if finding["unit_id"] == detail["correction_unit"] and not rt.ledger._conn.execute(
                                "SELECT 1 FROM maintenance_resolutions WHERE id=?", ("restored:" + changeset["id"],)).fetchone():
                            store.record_resolution(finding["id"], "corrected_and_consumers_confirmed", "kb-maintenance-service",
                                {"changeset_id": changeset["id"], "merge_sha": changeset["merge_sha"], "restoration_generation": detail["restoration_generation"],
                                 "acknowledged_generation": barrier["generation"]},
                                resolution_id="restored:" + changeset["id"], now=rt.clock())
                continue
            if detail.get("post_check") != "passed" or not pending_holds(rt).get("ready"):
                continue
            active = rt.ledger.active_snapshot()
            data = rt.load_changeset_files(changeset["id"])
            files = rt.knowledge.knowledge_files(active)
            if not active or any(files.get(page) != text for page, text in data["files"].items()):
                continue
            try:
                ci = required_ci(rt, changeset)
                original = detail["original_unit"]
                from .maintenance_audit import source_evidence
                evidence = source_evidence(rt, lifecycle, original)
                if evidence != detail["source_audit"]["evidence"]:
                    raise ValueError("correction original-source receipt changed")
                new_units = enumerate_units(files, lifecycle, active)
                approved = [u["content_sha256"] for u in new_units if u["page"] in data["files"] and u["content_sha256"] != original["content_sha256"]]
                result = record_restoration(rt, original["unit_id"], approved_hashes=approved,
                    approved_page_hashes=[digest(text) for text in data["files"].values()],
                    proof={"changeset_id": changeset["id"], "maintenance_run": detail["maintenance_run"],
                           "original_unit": original, "source_evidence": evidence, "source_audit": detail["source_audit"],
                           "decision": detail["decision"], "ci": ci,
                           "correction_merge_sha": changeset["merge_sha"], "active_snapshot": active,
                           "page_hashes": {p: digest(t) for p, t in data["files"].items()}})
                rt.ledger.update_changeset(changeset["id"], detail={**detail, "restoration_generation": result["generation"]})
                events.append({"changeset_id": changeset["id"], "status": "new_content_restored_pending_ack", **result})
            except (ValueError, KeyError, OSError, RuntimeError) as exc:
                events.append({"changeset_id": changeset["id"], "status": "restoration_pending", "reason": str(exc)})
    return events


def operational_status(rt, store, units):
    """Keep source changes, semantic work, merges and consumer ACKs distinct."""
    from .containment import pending_holds
    from .maintenance_store import budget_date
    now = rt.clock()
    repos, corrections = [], []
    for lifecycle in rt.registry.values():
        if not lifecycle.enabled:
            continue
        last = rt.ledger.get_cursor(lifecycle.repo, "last_sweep_at")
        baseline = rt.ledger.get_cursor(lifecycle.repo, "sweep_baseline")
        observed_today = bool(last and budget_date(float(last)) == budget_date(now))
        observation = rt.ledger.get_cursor(lifecycle.repo, "maintenance_source_observation")
        observation = json.loads(observation) if observation else None
        source_status = observation["status"] if observation and budget_date(observation["checked_at"]) == budget_date(now) \
            else "not_observed_this_day"
        repos.append({"repo": lifecycle.repo, "release": rt.ledger.get_cursor(lifecycle.repo, "release"),
                      "upstream_baseline": baseline, "last_release_sweep_at": float(last) if last else None,
                      "code_update_status": source_status, "source_observation": observation,
                      "release_sweep_completed_today": observed_today,
                      "source_update_budget": "existing_configuration"})
        for cs in rt.ledger.changesets_of_kind(lifecycle.repo, "correction"):
            corrections.append({"changeset_id": cs["id"], "repo": cs["repo"], "status": cs["status"],
                "merged": cs["status"] == "merged", "merge_sha": cs.get("merge_sha"),
                "active_bytes_verified": bool(cs["detail"].get("restoration_generation")),
                "restoration_generation": cs["detail"].get("restoration_generation")})
    queue = [{**row, "owner": settings(rt).owner, "age_seconds": max(0, now - row["created_at"])}
             for row in rt.ledger.human_queue()]
    usages = store.usage()
    usage_status = {u["unit_id"]: usages.get(u["unit_id"], {"retrieved": None, "injected": None, "observed": False}) for u in units}
    propagation = pending_holds(rt)
    # ACKs are separately authenticated on import; a file never expands the
    # configured roster or counts an unregistered reader as isolated.
    consumers = []
    raw_dir = getattr(rt, "containment_policy_dir", None) or os.environ.get("KB_CONTAINMENT_POLICY_DIR")
    if raw_dir:
        for consumer in settings(rt).consumers:
            path = Path(raw_dir) / "acks" / f"{consumer}.json"
            ack = json.loads(path.read_text()) if path.is_file() and not path.is_symlink() else None
            consumers.append({"consumer_id": consumer, "ack": ack,
                              "current": consumer not in propagation.get("missing_consumers", [])})
    return {"source_updates": repos, "corrections": corrections, "revocation_propagation": propagation,
            "consumer_versions": consumers, "usage": usage_status, "owner_todos": queue,
            "unregistered_readers": "outside_isolation_coverage"}


def impact(rt, store, unit, files):
    """Trace explicit reverse citations and previously injected consumer tasks.

    Unobserved readers are never represented as isolated. Derived claims are
    owner work until their own pinned-source semantic review is complete.
    """
    references = []
    tokens = [unit["page"], "knowledge/" + unit["page"]]
    if unit["kind"] == "rule":
        tokens.append(unit["block_id"])
    for path, text in sorted(files.items()):
        if path != unit["page"] and any(token in text for token in tokens):
            references.append(path)
    for other in _units(rt, unit["snapshot"])[1]:
        if other["page"] in references:
            _human(rt, other, f"depends on contradicted knowledge {unit['unit_id']}; original-source re-review required")
    rows = store.ledger._conn.execute("SELECT detail FROM maintenance_usage WHERE unit_id=? AND kind='injected' ORDER BY created_at", (unit["unit_id"],))
    tasks = [json.loads(row[0]) for row in rows]
    return {"reverse_citations": references, "injected_tasks_requiring_reassessment": tasks,
            "unregistered_or_unobserved_readers": "unknown"}
