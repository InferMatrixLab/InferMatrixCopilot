"""Trusted staging/scoring drivers and the candidate's prediction-only entry points.

dataset.json: {workflow, items: [{item, split: development|holdout,
input: relative.json, sha256, gold: {...}, labels: {...}, label_source: human}]}.
Labels and gold stay in the parent; only the pinned input is sent to a worker.
"""
from __future__ import annotations
from dataclasses import asdict
import json
import time
from pathlib import Path
from types import SimpleNamespace

from .artifacts import ArtifactError, digest, safe_path

def input_path(raw):
    """Read-only replay files can include .github; never overwrite Git metadata."""
    from pathlib import PurePosixPath
    p = PurePosixPath(raw)
    if not raw or p.is_absolute() or ".." in p.parts or ".git" in p.parts or "\\" in raw or any(ord(c) < 32 for c in raw):
        raise ArtifactError("unsafe replay path")
    return p.as_posix()

def dataset(settings, workflow: str) -> list[dict]:
    root = Path(settings.improve_evolve_data_dir).expanduser() if settings.improve_evolve_data_dir else None
    if root is None:
        return []
    path = root / workflow / "dataset.json"
    if not path.is_file():
        return []
    doc = json.loads(path.read_text())
    if doc.get("workflow") != workflow:
        raise ArtifactError("dataset workflow mismatch")
    rows, seen = [], set()
    for raw in doc.get("items", []):
        if raw["item"] in seen or raw.get("split") not in ("development", "holdout"):
            raise ArtifactError("duplicate item or invalid dataset split")
        seen.add(raw["item"])
        input_path = path.parent / safe_path(raw["input"])
        if input_path.is_symlink() or not input_path.resolve().is_relative_to(path.parent.resolve()):
            raise ArtifactError("dataset input escapes its root")
        content = input_path.read_bytes()
        if digest(content) != raw["sha256"]:
            raise ArtifactError("dataset input hash mismatch")
        payload = json.loads(content)
        if any(k in payload for k in ("labels", "gold", "scores", "judgments", "report")):
            raise ArtifactError("input carries evaluator-only material")
        version = digest(json.dumps(raw, sort_keys=True).encode())
        rows.append({**raw, "payload": payload, "version": version})
    return rows

def export_meta(settings, dest: Path) -> dict:
    """Import traces and existing HUMAN labels. Unsplit cases remain development."""
    from .meta import load_cases
    from .artifacts import atomic_json
    workflow = "workflow-improve.improve.forensics"
    root = dest / workflow
    rows = []
    for case in load_cases(Path(settings.improve_meta_dir)):
        info = json.loads((Path(settings.improve_meta_dir) / "cases" / case.name / "case.json").read_text())
        blobs = {}
        for r in case.unit.records:
            for ref in [*(r.get("inputs") or {}).values(), *(r.get("outputs") or {}).values()]:
                blobs[ref] = case.store.blob(ref)
        payload = {"records": case.unit.records, "blobs": blobs, "concerns": [asdict(g) for g in case.gold.entries],
                   "cells": sorted(case.labels)}
        atomic_json(root / f"{case.name}.json", payload)
        rows.append({"item": f"meta:{case.name}", "split": info.get("split", "development"),
                     "input": f"{case.name}.json", "sha256": digest((root / f"{case.name}.json").read_bytes()),
                     "labels": case.labels, "label_source": "human"})
    atomic_json(root / "dataset.json", {"workflow": workflow, "items": rows})
    return {"cases": len(rows), "path": str(root / "dataset.json"), "annotation_required": not bool(rows)}

class Gateway:
    """The production knowledge gate using the existing governed judge entrypoint."""
    def __init__(self, llm, spec, governor):
        self.llm, self.spec, self.governor = llm, spec, governor

    def call_json(self, role, *, system, prompt, validate=None, **kwargs):
        from ..kb_service.models import ModelReply
        from .judges import run_judge
        started = time.monotonic()
        data = run_judge(self.spec, system=system, prompt=prompt, llm=self.llm, governor=self.governor)
        if validate:
            validate(data)
        # run_judge owns the governed call and its trace. It exposes only the
        # decoded verdict here, so native usage and archive identity are unknown.
        return ModelReply(role, data, json.dumps(data), self.spec.model, {},
                          time.monotonic() - started)

def make_unit(store, workflow, item, version, result, uid):
    from .reader import Unit
    rec = store.append("decision", context={"workflow": workflow, "playbook": workflow.split(".", 1)[0],
                       "unit_id": uid, "item": item, "fingerprint": version},
                       result={**{k: v for k, v in result.items() if k in ("operations", "review_text", "review_comments", "attributions", "rejected", "rationale")}, "type": "draft_result" if workflow == "kb-intake.draft" else "step_result"},
                       outputs={"review": result.get("review_text", json.dumps(result))})
    return Unit(unit_id=uid, workflow=workflow, playbook=workflow.split(".", 1)[0], step=workflow.split(".", 1)[1],
                declared=True, item=item, fingerprint=version, records=[rec])

def gold_of(row):
    from .adapters import Gold, GoldEntry
    data = row.get("gold") or {}
    return Gold(row["item"], tuple(GoldEntry(**g) for g in data.get("entries", [])), row["version"])

def score_meta(row: dict, prediction: dict) -> dict:
    labels = row.get("labels") or {}
    if not labels or row.get("label_source") != "human":
        raise ArtifactError("meta scoring requires human labels")
    ids = {r["id"] for r in row["payload"]["records"]}
    found = prediction.get("attributions") or []
    by_id = {a["gold_id"]: a for a in found}
    correct = 0
    for gid, expected in labels.items():
        a = by_id.get(gid, {})
        evidence = a.get("evidence") or []
        if a.get("stage") != "S0" and evidence and set(evidence).issubset(ids) and not a.get("disputed") and a.get("stage") == expected:
            correct += 1
    return {"accuracy_review": correct / len(labels)}

def score_kb(row, result, unit, store, settings, llm, governor):
    from ..knowledge_service.ops import KnowledgeOperation, apply_operations
    from ..kb_service.gate import run_gate, changes_between
    from ..kb_service.models import ModelRole
    from .judges import judge_spec_from
    from .adapters.kb_intake import KbIntakeAdapter
    from .adapters import scores_from
    spec = judge_spec_from(settings)
    if spec is None:
        raise ArtifactError("knowledge gate judge is not configured")
    data = row["payload"]
    operations = [KnowledgeOperation.from_dict(o) for o in result.get("operations", [])]
    if len(operations) > 6 or any(o.allow_protected or not safe_path(o.page).startswith(data["repo_dir"].rstrip("/") + "/") for o in operations):
        raise ArtifactError("worker requested protected knowledge mutation")
    files = data["files"]
    passed_rules = set()
    summary = {"type": "gate_summary", "pass": 0, "fail": 0, "human": 0, "empty": not operations,
               "gate_status": "pass", "gate_version": 2}
    ctx = {"unit_id": unit.unit_id, "workflow": unit.workflow, "of": unit.unit_id}
    if operations:
        applied = apply_operations(files, operations, release=data["release"], today=data["today"])
        head = {**files, **applied.files}
        gateway = Gateway(llm, spec, governor)
        gate = run_gate(base=files, head=head, changes=changes_between(files, head),
                        external_texts=data.get("external_texts", {}), evidence=[data["evidence"]],
                        gateway=gateway, judge=ModelRole("judge", spec.provider or "api", spec.model),
                        release=data["release"], repo_dir=data["repo_dir"])
        summary["gate_status"] = gate.status
        for b in gate.blocks:
            if b.block.kind == "rule":
                summary[b.verdict] += 1
                if b.verdict == "pass": passed_rules.add(b.block.rule_id)
                store.append("outcome", context=ctx, result={"type": "gate_block", "kind": "rule",
                             "rule_id": b.block.rule_id, "verdict": b.verdict, "gate_version": 2})
        if gate.status == "fail":
            passed_rules.clear()
            summary.update({"fail": max(len(operations), sum(summary[k] for k in ("pass", "fail", "human"))), "pass": 0, "human": 0})
    store.append("outcome", context=ctx, result=summary)
    adapter = KbIntakeAdapter(judge=spec, llm=llm, governor=governor)
    gold = gold_of(row)
    if gold.entries:
        # Coverage measures valid rules; rejected rules cannot buy recall.
        from dataclasses import replace
        records = [{**r, "result": {**r["result"], "operations": [o for o in result.get("operations", []) if (o.get("new_rule_id") or o.get("rule_id")) in passed_rules]}} for r in unit.records]
        adapter.gold_match(replace(unit, records=records), gold, store)
    outcome = adapter.fetch(unit, store)
    matches = adapter.match(unit, gold, outcome)
    if any(m.status == "unlabeled" for m in matches):
        raise ArtifactError("coverage judge failed to score the complete gold denominator")
    return scores_from(matches, adapter.findings(unit, outcome), adapter.review_scores(unit, outcome)).values

def score_pair(driver, row, predictions, units, store, settings, llm, governor):
    if driver == "meta":
        return {side: score_meta(row, p) for side, p in predictions.items()}
    if driver == "kb-intake":
        return {side: score_kb(row, p, units[side], store, settings, llm, governor) for side, p in predictions.items()}
    if driver == "pr-review":
        from .adapters.review_eval import ReviewEvalAdapter
        from .judges import judge_spec_from
        adapter = ReviewEvalAdapter(gt_dir=Path("/unused-pinned-gold"), judge=judge_spec_from(settings), llm=llm, governor=governor)
        gold = gold_of(row)
        if not gold.entries:
            raise ArtifactError("PR evaluation needs curated concerns")
        adapter.paired_verdict(units["arm"], units["incumbent"], gold, store, experiment_id="evolve", replicate=int(predictions["arm"].get("replicate", 1)))
        from .adapters import scores_from
        return {side: scores_from([], [], adapter.review_scores(unit, adapter.fetch(unit, store))).values for side, unit in units.items()}
    raise ArtifactError(f"unknown trusted experiment driver: {driver}")

def worker_run(payload, settings, llm):
    """Executes the actual candidate implementation, never a candidate-supplied score."""
    import asyncio
    from ..run_trace import RunTrace
    data, driver = payload["input"], payload["driver"]
    if driver == "meta":
        from .forensics import Cell, attribute
        from .meta import _unit_from
        from .adapters import Gold, GoldEntry
        from ..trace_store import TraceStore
        from ..agent_loop import run_agent
        store = TraceStore(Path("/tmp/traces"))
        for ref, text in data["blobs"].items():
            if store.put_blob(text) != ref:
                raise ArtifactError("input blob checksum mismatch")
        unit = _unit_from(data["records"])
        gold = Gold(unit.item, tuple(GoldEntry(**g) for g in data["concerns"]))
        agents = {}
        for mode in ("eco", "performance"):
            target = settings.tier_target(mode)
            client = llm.for_target(target)
            def agent(system, prompt, scope, extra_tools, max_iters, client=client, model=target.model):
                return run_agent(client, system=system, prompt=prompt, scope=scope,
                                 trace=RunTrace(Path("/tmp/agent.jsonl")), model=model,
                                 max_iters=max_iters, extra_tools=extra_tools).text
            agents[f"{target.provider_id}:{target.model}"] = agent
        attrs = attribute(store, {unit.unit_id: unit}, {unit.item: gold},
                          [Cell(g, unit.unit_id, "miss") for g in data["cells"]], agents=agents, max_cells=len(data["cells"]))
        return {"attributions": [asdict(a) for a in attrs]}
    if driver == "kb-intake":
        from ..kb_service.intake import draft_changes, operations_json
        from ..kb_service.models import ModelRole, parse_json_object
        target = settings.tier_target("eco")
        if data["generator_model"] != target.model: raise ArtifactError("pinned KB generator model changed")
        class WorkerGateway:
            def call_json(self, role, *, system, prompt, validate=None, **kwargs):
                reply = llm.create(system=system, messages=[{"role": "user", "content": prompt}], model=target.model,
                                   max_tokens=settings.llm_max_tokens, role="evolve.kb-draft")
                parsed = parse_json_object(reply.text)
                if validate: validate(parsed)
                return SimpleNamespace(data=parsed)
        draft = draft_changes(repo=data["repo"], repo_dir=data["repo_dir"], event_id=int(data.get("event_id", 1)),
                              evidence=data["evidence"], files=data["files"], gateway=WorkerGateway(),
                              generator=ModelRole("generator", "api", target.model), release=data["release"], today=data["today"],
                              max_operations=max(1, min(6, settings.kb_draft_max_operations)))
        return {"operations": operations_json(draft.operations), "rejected": draft.rejected, "rationale": draft.rationale}
    if driver == "pr-review":
        from ..engine.step import StepContext
        from ..engine.steps.review.steps import _review_diff
        from .artifacts import safe_path
        import subprocess
        repo = Path("/tmp/repository"); repo.mkdir()
        def git(*args):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        git("init", "-q"); git("config", "user.name", "shadow"); git("config", "user.email", "shadow@invalid")
        revisions = []
        for files in (data["base_files"], data["head_files"]):
            for p in repo.rglob("*"):
                if p.is_file() and ".git" not in p.parts: p.unlink()
            for name, content in files.items():
                p = repo / input_path(name); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content)
            git("add", "-A"); git("commit", "--allow-empty", "-qm", "frozen input")
            revisions.append(subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip())
        state = {"playbook": "pr-review", "repo_path": str(repo), "diff_text": data["diff"],
                 "pr_base_sha": revisions[0], "pr_head_sha": revisions[1],
                 "pr_context": data.get("context_text", ""), "gate_report": data.get("gate_report", ""),
                 "pr_state": data.get("pr_state", ""),
                 "task_spec": {"kind": "pr_review", "repo": data["repo"], "pr": data["pr"], "params": data.get("task_params", {})}}
        settings = settings.model_copy(update={"improve_shadow": True, "run_root": Path("/tmp/runs"),
                    "knowledge_dir": Path("/tmp/knowledge"), "skills_dir": Path("/candidate/skills"),
                    "playbooks_dir": Path("/candidate/playbooks"), "adapters_dir": Path("/candidate/adapters"), "memory_db": Path("/tmp/memory.db")})
        for path, content in data.get("knowledge_files", {}).items():
            p = Path("/tmp/knowledge") / safe_path(path); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content)
        from .execution import shadow_agent_policy

        ctx = StepContext(settings, state, {}, Path("/tmp/run"), RunTrace(Path("/tmp/run.jsonl")), llm,
                          agent_policy=shadow_agent_policy())
        ctx.run_dir.mkdir()
        result = asyncio.run(_review_diff(ctx))
        if not result.ok: raise ArtifactError(result.summary)
        return {"review_text": result.outputs.get("review_text") or state.get("review_text", ""),
                "review_comments": result.outputs.get("review_comments", []),
                **{k: state.get(k) for k in ("review_summary", "review_verdict", "review_finding_dispositions", "review_carried_findings", "review_finding_rechecks", "review_recheck_missing")}}
    raise ArtifactError(f"worker has no driver {driver}")
