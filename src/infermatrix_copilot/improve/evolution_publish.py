"""Human-reviewed publication and adoption tracking; no credentials in the engine."""
from __future__ import annotations
import json
from pathlib import Path
import time
from .artifacts import atomic_json, digest, verify

PROTOCOL = "evolve-outbox/1"

def draft(settings, c):
    from .evolution import directory
    root = directory(settings) / "candidates" / c["id"]
    evaluation = c.get("evaluation", {})
    body = (f"Improve `{c['workflow']}`: {c['claim']}\n\n"
            f"Evidence records: {', '.join(c['evidence'])}.\n\n"
            f"Base: `{c['base_revision']}`. Candidate source: `{c['artifact']['tree_sha']}`.\n\n"
            f"Affected workflows: {', '.join(c.get('affected_workflows', []))}.\n\n"
            "Validation\n\n```json\n" + json.dumps({"tests": c.get("verified"), "experiment": evaluation}, indent=2) + "\n```\n\n"
            + ("Deployment: verified local sandbox release with automatic canary and rollback; this PR is an optional audit record.\n\n" if getattr(settings, "improve_promotion_mode", "pr") == "automatic" and getattr(settings, "improve_evaluation_mode", "gold") == "objective" else "Rollback: revert this PR and restore the preceding configuration. Human approval is required.\n\n")
            + f"<!-- evolve:candidate:{c['id']} -->\n")
    (root / "PR.md").write_text(body, encoding="utf-8")
    atomic_json(root / "bundle.json", {"protocol": PROTOCOL, "candidate": c["id"], "workflow": c["workflow"],
                "base_revision": c["base_revision"], "source_sha": c["artifact"]["tree_sha"],
                "patch_sha": c["patch_sha"], "overrides": c["generated"].get("overrides", {}),
                "evaluation": evaluation, "tests": c.get("verified"), "body_sha": digest(body.encode())})

def publish(settings, store, c):
    from .evolution import directory, save
    from .ledger import Ledger
    from .cycle import ledger_dir_for
    if c["state"] not in ("pr-ready", "canary", "retained") or not settings.allow_post or not settings.allow_push:
        return {"published": False, "reason": "requires evaluated candidate, ALLOW_POST=1 and ALLOW_PUSH=1"}
    if Ledger(ledger_dir_for(settings)).load(c["workflow"]).hold:
        return {"published": False, "reason": "workflow publication hold"}
    if not settings.improve_evolve_outbox_dir or not settings.improve_proposal_repo:
        return {"published": False, "reason": "missing evolution outbox or proposal repository"}
    root = directory(settings) / "candidates" / c["id"]
    verify(root / "arm")
    bundle = json.loads((root / "bundle.json").read_text())
    patch = (root / "candidate.patch").read_text()
    if digest(patch.encode()) != bundle["patch_sha"]:
        raise ValueError("evaluated patch changed before publication")
    body = (root / "PR.md").read_text()
    if digest(body.encode()) != bundle["body_sha"]:
        raise ValueError("PR report changed before publication")
    action = {**bundle, "id": c["id"], "action": "prepare_pr", "repo": settings.improve_proposal_repo,
              "patch": patch, "body": body, "title": f"Improve {c['workflow']}: {c['claim']}"[:180],
              "branch": f"evolve/{c['id']}", "issued_at": time.time()}
    outbox = Path(settings.improve_evolve_outbox_dir)
    path = outbox / "actions" / f"{c['id']}.json"
    if not path.exists(): atomic_json(path, action)
    c["pending_action"] = c["id"]
    save(settings, c, store)
    return {"published": True, "action": str(path)}

def sync(settings, store):
    from .evolution import candidates, save
    if not settings.improve_evolve_outbox_dir: return
    root = Path(settings.improve_evolve_outbox_dir)
    for c in candidates(settings):
        ack_path = root / "acks" / f"{c['id']}.json"
        if ack_path.is_file():
            ack = json.loads(ack_path.read_text())
            if ack.get("candidate") == c["id"] and ack.get("source_sha") == c.get("artifact", {}).get("tree_sha"):
                if ack.get("ok") and not ack.get("dry_run"):
                    if c["state"] == "pr-ready": c.update(state="published", pr=ack["url"], reason="")
                    elif c["state"] in ("canary", "retained", "rolled-back"):
                        c["pr"] = ack["url"]  # audit never changes runtime adoption
        obs_path = root / "inbox" / f"{c['id']}.json"
        if obs_path.is_file():
            obs = json.loads(obs_path.read_text())
            if obs.get("candidate") == c["id"] and obs.get("url") == c.get("pr"):
                if obs.get("merged") and c["state"] == "published":
                    c.update(state="merged", merge_sha=obs["merge_sha"], merged_at=obs.get("at", time.time()))
                elif obs.get("closed") and not obs.get("merged") and c["state"] == "published":
                    c.update(state="closed", reason="human closed the PR")
        if c["state"] == "merged" and c.get("merge_sha"):
            # Compare manifests, not PR merge alone. The source digest attests
            # actual running bytes; the record must postdate the merge observation.
            records = store.query(workflow=c["workflow"], limit=10000)
            for r in records:
                if float(r.get("at", 0)) < c.get("merged_at", 0): continue
                if (r.get("result") or {}).get("type") not in ("step_result", "draft_result"): continue
                if (r.get("context") or {}).get("unit_id", "").startswith("exp-"): continue
                if (r.get("env") or {}).get("source_tree_sha") == c["artifact"]["tree_sha"]:
                    expected = c.get("generated", {}).get("overrides", {})
                    if expected:
                        ref = (r.get("inputs") or {}).get("fingerprint_manifest")
                        if not ref: continue
                        covers = json.loads(store.blob(ref)).get("covers", {})
                        from .experiments import _coerce
                        if any(covers.get("settings", {}).get(k.lower(), covers.get("routing", {}).get(k)) != _coerce(v) for k, v in expected.items()):
                            continue
                    c.update(state="deployed", deployed_record=r["id"], deployed_at=r["at"])
                    from .ledger import Ledger
                    from .cycle import ledger_dir_for
                    ledger = Ledger(ledger_dir_for(settings))
                    with ledger.locked(c["workflow"]):
                        row = ledger.load(c["workflow"])
                        row.baseline["deployed_source"] = {"candidate": c["id"], "tree_sha": c["artifact"]["tree_sha"], "record": r["id"], "at": r["at"]}
                        ledger.save(row)
                    break
        if c["state"] == "deployed": observe(settings, store, c)
        save(settings, c)


def observe(settings, store, c):
    """Read-only production monitoring; recommendations never perform rollbacks."""
    from .reader import units_between
    from .lints import Baseline, run_lints, unit_usd
    from .enroll import declarations_for
    from . import stats
    from dataclasses import asdict
    decl = declarations_for(settings).get(c["workflow"])
    baseline_sha = c.get("incumbent_sha")
    if not baseline_sha:
        from .evolution import directory
        baseline_sha = verify(directory(settings) / "candidates" / c["id"] / "incumbent")["tree_sha"]
    units = units_between(store, c["created_at"] - 7 * 86400, time.time())
    sides = {"before": [], "after": []}
    for u in units.values():
        if u.workflow != c["workflow"] or u.unit_id.startswith("exp-"): continue
        source = (u.records[-1].get("env") or {}).get("source_tree_sha")
        if source == c["artifact"]["tree_sha"] and u.ended >= c["deployed_at"]: sides["after"].append(u)
        elif source == baseline_sha and u.ended < c.get("merged_at", 0): sides["before"].append(u)
    report = {"state": "waiting-for-production-samples", "before_units": len(sides["before"]), "after_units": len(sides["after"]), "checked_at": time.time(), "metrics": {}}
    if min(map(len, sides.values())) < 8:
        c["observation"] = report; return
    adapter = None
    if decl and decl.outcome_adapter:
        from .adapters import load_adapter, scores_from
        from .experiments import _adapter_kwargs
        adapter = load_adapter(decl.outcome_adapter, **_adapter_kwargs(settings, decl))
    values = {"before": {}, "after": {}}
    unknown_cost_items = {'before': set(), 'after': set()}
    for side, group in sides.items():
        for u in group:
            usd = unit_usd(u, settings)
            metrics = {'usd_review': usd} if usd is not None else {}
            if usd is None:
                unknown_cost_items[side].add(u.item)
            if c.get("mechanism", "").startswith("L"):
                metrics["target_defect"] = float(any(f.lint == c["mechanism"] for f in run_lints(u, store, Baseline(settings=settings))))
            if adapter:
                outcome = adapter.fetch(u, store)
                if outcome:
                    metrics.update(scores_from([], [], adapter.review_scores(u, outcome)).values)
            for metric, value in metrics.items():
                values[side].setdefault(metric, {}).setdefault(u.item, []).append(value)
        for item in unknown_cost_items[side]:
            values[side].get('usd_review', {}).pop(item, None)
    report['unknown_cost_items'] = {side: sorted(items) for side, items in unknown_cost_items.items()}
    directions = {**(decl.evolution.get("guards", {}) if decl else {}), "target_defect": "lower"}
    if decl and decl.evolution.get("metric"): directions[decl.evolution["metric"]] = decl.evolution.get("direction", "higher")
    import statistics
    regressions = []
    for metric, direction in directions.items():
        before, after = values["before"].get(metric, {}), values["after"].get(metric, {})
        paired = {item: [statistics.mean(after[item]) - statistics.mean(before[item])] for item in before.keys() & after.keys()}
        if len(paired) < 8: continue
        result = stats.paired(paired, metric)
        regressed = result.hi < 0 if direction == "higher" else result.lo > 0
        report["metrics"][metric] = {**asdict(result), "regressed": regressed}
        if regressed: regressions.append(metric)
    cost_pending = 'usd_review' in directions and any(unknown_cost_items.values())
    report["state"] = "regressed" if regressions else "waiting-for-cost-evidence" if cost_pending \
        else "observed" if report["metrics"] else "waiting-for-matched-items-and-outcomes"
    if regressions:
        report["rollback_recommendation"] = {"pr": c.get("pr"), "candidate": c["id"], "reason": regressions, "action": "Human review: revert this PR and restore the preceding configuration."}
    c["observation"] = report
