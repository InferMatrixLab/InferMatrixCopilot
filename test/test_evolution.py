"""Evolution contracts, trusted scoring, resumption and three driver integrations.

Workers/models are scripted here. Real namespace execution is separately
probed and skipped when unavailable, never replaced by unsandboxed execution.
"""
from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import pytest
import yaml

from infermatrix_copilot.config import Settings
from infermatrix_copilot.improve import artifacts, drivers, evolution, experiments
from infermatrix_copilot.improve.isolation import Sandbox, SandboxUnavailable
from infermatrix_copilot.improve.ledger import Ledger
from infermatrix_copilot.llm import Reply, Block
from infermatrix_copilot.trace_store import TraceStore


def init_repo(root, path):
    target = root / path; target.parent.mkdir(parents=True); target.write_text("# baseline\n")
    (root / "playbooks").mkdir()
    (root / "playbooks" / "pr-review.yaml").write_text("name: pr-review\n")
    (root / "test").mkdir(); (root / "test" / "test_trusted.py").write_text("def test_contract(): assert True\n")
    for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-qm", "baseline"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True, capture_output=True)


class Worker:
    def __init__(self): self.calls = []
    def check(self): return {"ready": True, "reason": ""}
    def run(self, source, payload, work, **kwargs):
        self.calls.append((source, payload))
        assert "labels" not in json.dumps(payload) and "gold" not in payload.get("input", {})
        meta = artifacts.verify(source)
        arm = "# improved" in next((source / "src").rglob("*.py")).read_text()
        result = {"source_sha": meta["tree_sha"], "package_path": "/candidate/src/infermatrix_copilot/__init__.py"}
        if payload["mode"] == "tests": return {**result, "rc": 0}
        if payload["driver"] == "meta":
            result["attributions"] = [{"gold_id": "g", "stage": "S2" if arm else "S1", "evidence": ["record"], "disputed": False}]
        elif payload["driver"] == "pr-review": result["review_text"] = "improved" if arm else "baseline"
        else: result["operations"] = [{"kind": "add", "page": "repos/demo/rules.md", "rule_id": "NEW", "section_markdown": "improved"}] if arm else []
        return result


class Model:
    available = True
    def __init__(self, proposal): self.proposal, self.calls = proposal, []
    def for_target(self, target): return self
    def create(self, **kwargs):
        self.calls.append(kwargs)
        return Reply([Block("text", text=json.dumps(self.proposal))])


@pytest.fixture
def bench(tmp_path):
    def build(driver="meta", workflow="workflow-improve.improve.forensics"):
        path = "src/infermatrix_copilot/improve/forensics.py" if driver == "meta" else \
               "src/infermatrix_copilot/kb_service/intake.py" if driver == "kb-intake" else \
               "src/infermatrix_copilot/engine/steps/review/prompts.py"
        source = tmp_path / driver / "source"; source.mkdir(parents=True); init_repo(source, path)
        playbook = workflow.split(".")[0]
        pb = source / "playbooks" / f"{playbook}.yaml"
        if not pb.exists():
            pb.write_text("name: " + playbook)
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-qm", "playbook"], check=True, capture_output=True)
        defs = tmp_path / driver / "declarations"; defs.mkdir()
        adapter = "infermatrix_copilot.improve.adapters.meta_bench:MetaBenchAdapter" if driver == "meta" else \
                  "infermatrix_copilot.improve.adapters.kb_intake:KbIntakeAdapter" if driver == "kb-intake" else \
                  "infermatrix_copilot.improve.adapters.review_eval:ReviewEvalAdapter"
        metric = {"meta": "accuracy_review", "kb-intake": "net_pass_review", "pr-review": "recall_review"}[driver]
        doc = {"workflow": workflow, "kind": "dynamic", "unit": "agent_loop", "item_key": "{item}",
               "fingerprint": {"covers": [{"prompts": [path.removeprefix("src/infermatrix_copilot/")]}]},
               "outcome_adapter": adapter, "experiment_driver": driver,
               "evolution": {"paths": [path], "settings": ["LLM_MAX_TOKENS"], "tests": ["test/test_trusted.py"],
                             "metric": metric, "min_effect": .5, "cost_per_unit_usd": .1, "guards": {"usd_review": "lower"}}}
        (defs / "workflow.yaml").write_text(yaml.safe_dump(doc))
        data = tmp_path / driver / "data" / workflow; data.mkdir(parents=True)
        rows = []
        for n in range(9):
            payload = {"records": [{"id": "record"}], "cells": ["g"], "blobs": {}, "concerns": [],
                       "repo": "demo", "pr": n, "base_sha": "a"*40, "head_sha": "b"*40, "base_files": {}, "head_files": {}, "diff": "frozen", "knowledge_files": {},
                       "generator_model": "claude-sonnet-5", "repo_dir": "repos/demo", "evidence": "PR #1", "files": {}, "release": "v1", "today": "2026-10-04"}
            artifacts.atomic_json(data / f"{n}.json", payload)
            rows.append({"item": f"demo#{n}", "split": "development" if n == 0 else "holdout", "input": f"{n}.json",
                         "sha256": artifacts.digest((data / f"{n}.json").read_bytes()), "labels": {"g": "S2"},
                         "label_source": "human", "gold": {"entries": [{"gold_id": "g", "path": "a.py", "concern": "concern"}]}})
        artifacts.atomic_json(data / "dataset.json", {"workflow": workflow, "items": rows})
        st = Settings(_env_file=None, improve_enabled=True, improve_evolve_enabled=True, improve_evaluation_mode="gold",
                      improve_evolve_source_dir=str(source), improve_evolve_data_dir=str(data.parent),
                      improve_workflows_dirs=str(defs), improve_ledger_dir=str(tmp_path / driver / "ledger"),
                      improve_budget_usd_week=20, improve_judge="api:claude-sonnet-5", eco_model="claude-sonnet-5",
                      performance_model="claude-sonnet-5", llm_max_tokens=1000, playbooks_dir=source / "playbooks")
        store = TraceStore(tmp_path / driver / "traces")
        record = store.append("decision", context={"workflow": workflow, "unit_id": "unit", "fingerprint": "frozen"}, inputs={"fingerprint_manifest": json.dumps({"complete": True, "covers": {}})}, result={"status": "ok"})
        Ledger(Path(st.improve_ledger_dir), store).open_proposal(workflow, tier=2, stage="S2", claim="Repair missing evidence",
                                                             evidence=[record["id"]], loss=1)
        proposal = {"files": [{"path": path, "before_sha": artifacts.digest((source / path).read_bytes()), "content": "# improved\n"}], "overrides": {}}
        return st, store, Worker(), Model(proposal)
    return build


@pytest.mark.parametrize("driver,workflow", [("meta", "workflow-improve.improve.forensics"),
                            ("pr-review", "pr-review.agent.review_diff"), ("kb-intake", "kb-intake.draft")])
def test_three_evolution_drivers_generate_verify_evaluate_and_prepare_pr(bench, driver, workflow, monkeypatch):
    st, store, worker, model = bench(driver, workflow)
    if driver != "meta":
        # Script the production judge boundary, not candidate-reported scores.
        original = drivers.score_pair
        def score(d, row, predictions, units, sink, settings, llm, governor):
            if d == "meta": return original(d, row, predictions, units, sink, settings, llm, governor)
            key = "recall_review" if d == "pr-review" else "net_pass_review"
            return {side: {key: 1.0 if (p.get("review_text") == "improved" or p.get("operations")) else 0.0} for side, p in predictions.items()}
        monkeypatch.setattr(drivers, "score_pair", score)
    result = evolution.run(st, store, workflow=workflow, llm=model, sandbox=worker)
    assert result["state"] == "pr-ready", result
    assert result["evaluation"]["n_retained"] == 8 and result["evaluation"]["promotable"]
    work = evolution.directory(st) / "candidates" / result["id"]
    assert (work / "candidate.patch").read_text() and (work / "PR.md").is_file()
    assert len(model.calls) == 1 and len(worker.calls) == 49
    versions = {artifacts.verify(p)["tree_sha"] for p, payload in worker.calls if payload["mode"] == "predict"}
    assert len(versions) == 2
    # A retry neither generates another candidate nor repeats model requests.
    again = evolution.run(st, store, workflow=workflow, llm=model, sandbox=worker)
    assert again["id"] == result["id"] and len(model.calls) == 1 and len(worker.calls) == 49
    assert evolution.run(st, store, workflow="other", llm=model, sandbox=worker)["reason"] == "weekly-candidate-limit"


def test_full_denominator_and_evidence_validation():
    row = {"labels": {"a": "S2", "b": "S2", "c": "S2"}, "label_source": "human", "payload": {"records": [{"id": "r"}]}}
    pred = {"scores": {"accuracy_review": 1}, "attributions": [{"gold_id": "a", "stage": "S2", "evidence": ["r"]},
            {"gold_id": "b", "stage": "S2", "evidence": ["fake"]}]}
    assert drivers.score_meta(row, pred)["accuracy_review"] == 1/3
    pred["attributions"][0]["disputed"] = True
    assert drivers.score_meta(row, pred)["accuracy_review"] == 0


@pytest.mark.parametrize("path", ["../escape.py", "/tmp/escape.py", "src/infermatrix_copilot/improve/stats.py", "eval/dataset/meta/case.json"])
def test_protected_mutation_policy(path):
    with pytest.raises(artifacts.ArtifactError): artifacts.validate_policy({"paths": [path]})


def test_artifact_hash_preimage_and_dependency_identity(bench, tmp_path):
    st, _, _, model = bench()
    base = tmp_path / "base"; artifacts.baseline(Path(st.improve_evolve_source_dir), base)
    proposal = model.proposal
    proposal["files"][0]["before_sha"] = "bad"
    with pytest.raises(artifacts.ArtifactError, match="preimage"):
        artifacts.apply_candidate(base, tmp_path / "arm", proposal, {"paths": [proposal["files"][0]["path"]]})
    (base / proposal["files"][0]["path"]).write_text("tampered")
    with pytest.raises(artifacts.ArtifactError, match="changed"): artifacts.verify(base)


def test_sandbox_refusal_never_starts_candidate(bench, monkeypatch, tmp_path):
    st, _, _, _ = bench()
    base = tmp_path / "base"; artifacts.baseline(Path(st.improve_evolve_source_dir), base)
    sandbox = Sandbox()
    monkeypatch.setattr(sandbox, "check", lambda: {"ready": False, "reason": "disabled"})
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("candidate launched without isolation"))
    with pytest.raises(SandboxUnavailable, match="disabled"): sandbox.run(base, {}, tmp_path / "work")


def test_missing_adapter_is_tier1_and_disabled_mode_does_not_generate(bench):
    st, store, worker, model = bench()
    check = evolution.check(st, store, "unregistered.step", sandbox=worker)
    assert check["tier"] == 1 and not check["ready"]
    result = evolution.run(st.model_copy(update={"improve_evolve_enabled": False}), store, llm=model, sandbox=worker)
    assert result["state"] == "disabled" and not model.calls


def test_no_duplicate_paid_generation_after_interruption(bench):
    st, store, worker, model = bench()
    c = {"id": "cand-interrupted", "workflow": "workflow-improve.improve.forensics", "state": "generating", "created_at": 1}
    evolution.save(st, c)
    for _ in range(2):
        result = evolution.run(st, store, workflow=c["workflow"], llm=model, sandbox=worker)
        assert result["generation_interrupted"]
    assert not model.calls


def test_changed_dataset_and_label_leak_are_refused(bench):
    st, _, _, _ = bench()
    path = Path(st.improve_evolve_data_dir) / "workflow-improve.improve.forensics" / "1.json"
    path.write_text('{"labels":{"g":"S2"}}')
    with pytest.raises(artifacts.ArtifactError, match="hash mismatch"): drivers.dataset(st, "workflow-improve.improve.forensics")


def test_config_candidate_is_a_reviewable_patch_and_runtime_scoped(bench, tmp_path):
    st, _, _, _ = bench()
    base = tmp_path / "base"; artifacts.baseline(Path(st.improve_evolve_source_dir), base)
    arm = tmp_path / "arm"
    artifacts.apply_candidate(base, arm, {"overrides": {"LLM_MAX_TOKENS": "700"}},
                              {"paths": [], "settings": ["LLM_MAX_TOKENS"]}, workflow="workflow-improve.improve.forensics")
    assert "evolution-overrides.json" in artifacts.patch(base, arm)
    settings = st.model_copy(update={"playbooks_dir": arm / "playbooks"})
    assert artifacts.runtime_settings(settings, "workflow-improve.improve.forensics").llm_max_tokens == 700
    assert artifacts.runtime_settings(settings, "pr-review.agent.review_diff").llm_max_tokens == 1000


def test_trusted_pr_driver_ignores_worker_scores(bench, monkeypatch):
    st, store, _, model = bench("pr-review", "pr-review.agent.review_diff")
    from infermatrix_copilot.improve.adapters import review_eval
    monkeypatch.setattr(review_eval, "run_judge", lambda *a, **k: {"x": {"recall": .75, "precision": .8, "actionability": .9},
                       "y": {"recall": .75, "precision": .8, "actionability": .9}, "winner": "tie"})
    row = drivers.dataset(st, "pr-review.agent.review_diff")[1]
    preds = {side: {"review_text": "finding", "scores": {"recall_review": 1}, "replicate": 1} for side in ("arm", "incumbent")}
    units = {side: drivers.make_unit(store, "pr-review.agent.review_diff", row["item"], "f", p, side) for side, p in preds.items()}
    scores = drivers.score_pair("pr-review", row, preds, units, store, st, model, None)
    assert all(s["recall_review"] == .75 and s["precision_review"] == .8 for s in scores.values())


def test_kb_empty_output_is_scored_by_parent_not_worker(bench):
    st, store, _, model = bench("kb-intake", "kb-intake.draft")
    row = drivers.dataset(st, "kb-intake.draft")[1]
    pred = {"operations": [], "net_pass_review": 900, "type": "gate_summary"}
    unit = drivers.make_unit(store, "kb-intake.draft", row["item"], "f", pred, "u")
    assert unit.records[0]["result"]["type"] == "draft_result"
    score = drivers.score_kb(row, pred, unit, store, st, model, None)
    assert score["net_pass_review"] == 0 and score["recall_gold"] == 0
    assert not model.calls


def test_fingerprint_violation_invalidates_whole_experiment(bench):
    st, store, worker, model = bench()
    original = worker.run
    def forged(source, payload, work, **kwargs):
        result = original(source, payload, work, **kwargs)
        if payload["mode"] == "predict": result["source_sha"] = "forged"
        return result
    worker.run = forged
    result = evolution.run(st, store, workflow="workflow-improve.improve.forensics", llm=model, sandbox=worker)
    assert result["state"] == "rejected" and not result["evaluation"].get("promotable")
    assert "fingerprint mismatch" in result["evaluation"]["error"]


def test_interrupted_pair_is_not_replayed_and_needs_three_replicates(bench):
    st, store, worker, model = bench()
    original = worker.run
    def interrupted(source, payload, work, **kwargs):
        if len(worker.calls) == 2: raise KeyboardInterrupt()
        return original(source, payload, work, **kwargs)
    worker.run = interrupted
    with pytest.raises(KeyboardInterrupt):
        evolution.run(st, store, workflow="workflow-improve.improve.forensics", llm=model, sandbox=worker)
    worker.run = original
    result = evolution.run(st, store, workflow="workflow-improve.improve.forensics", llm=model, sandbox=worker)
    assert len(model.calls) == 1 and result["state"] == "deferred"
    assert result["evaluation"]["n_retained"] == 7
    assert any("interrupted" in v for v in result["evaluation"]["excluded"].values())


def test_repair_is_attempted_only_once(bench):
    st, store, worker, model = bench()
    original = worker.run
    def fail(source, payload, work, **kwargs):
        result = original(source, payload, work, **kwargs)
        if payload["mode"] == "tests": result["rc"] = 1
        return result
    worker.run = fail
    result = evolution.run(st, store, workflow="workflow-improve.improve.forensics", llm=model, sandbox=worker)
    assert result["state"] == "rejected" and result["repairs"] == 1 and len(model.calls) == 2
    assert all(payload["mode"] == "tests" for _, payload in worker.calls)
    assert len(result["verification_attempts"]) == 2
    root = evolution.directory(st) / "candidates" / result["id"]
    for attempt in ("0", "1"):
        assert (root / "attempts" / attempt / "candidate.patch").is_file()
        assert json.loads((root / "attempts" / attempt / "tests.json").read_text())["rc"] == 1
        artifacts.verify(root / "attempts" / attempt / "arm")


def test_global_lock_and_budget_block_prevent_generation(bench):
    st, store, worker, model = bench()
    from infermatrix_copilot.trace_store import file_lock
    root = evolution.directory(st); root.mkdir(parents=True)
    with file_lock(root / "evolution.lock", blocking=False):
        assert evolution.run(st, store, llm=model, sandbox=worker)["state"] == "deferred"
    st = st.model_copy(update={"improve_budget_usd_week": .01})
    assert evolution.run(st, store, llm=model, sandbox=worker)["state"] == "deferred"
    assert not model.calls and not worker.calls


def test_merge_without_production_fingerprint_does_not_advance_baseline(bench, tmp_path, monkeypatch):
    st, store, worker, model = bench()
    from infermatrix_copilot.improve import evolution_publish
    result = evolution.run(st, store, workflow="workflow-improve.improve.forensics", llm=model, sandbox=worker)
    outbox = tmp_path / "outbox"
    st = st.model_copy(update={"improve_evolve_outbox_dir": str(outbox)})
    artifacts.atomic_json(outbox / "acks" / (result["id"] + ".json"), {"candidate": result["id"], "source_sha": result["artifact"]["tree_sha"], "ok": True, "dry_run": False, "url": "https://example.invalid/pr/1"})
    artifacts.atomic_json(outbox / "inbox" / (result["id"] + ".json"), {"candidate": result["id"], "url": "https://example.invalid/pr/1", "merged": True, "merge_sha": "a"*40, "at": 0})
    evolution_publish.sync(st, store)
    assert evolution.candidate(st, result["id"])["state"] == "merged"
    assert "deployed_source" not in Ledger(Path(st.improve_ledger_dir)).load(result["workflow"]).baseline
    import infermatrix_copilot.trace_store as trace
    monkeypatch.setattr(trace, "environment_fingerprint", lambda: {"source_tree_sha": result["artifact"]["tree_sha"]})
    store.append("decision", context={"workflow": result["workflow"], "unit_id": "production"}, result={"type": "step_result"})
    evolution_publish.sync(st, store)
    assert evolution.candidate(st, result["id"])["state"] == "deployed"
    assert Ledger(Path(st.improve_ledger_dir)).load(result["workflow"]).baseline["deployed_source"]["candidate"] == result["id"]


def test_cli_cycle_uses_shared_coordinator(tmp_path, monkeypatch, capsys):
    from infermatrix_copilot.improve import cli, coordinator
    monkeypatch.setenv("IMPROVE_ENABLED", "true"); monkeypatch.setenv("IMPROVE_EVOLVE_ENABLED", "true")
    called = []
    monkeypatch.setattr(coordinator, "run", lambda *a, **k: called.append(a) or {"state": "complete"})
    assert cli.main(["cycle", "--trace-root", str(tmp_path / "traces"), "--ledger-dir", str(tmp_path / "ledger")]) == 0
    assert len(called) == 1 and called[0][0].improve_ledger_dir == str(tmp_path / "ledger")


def test_real_sandbox_hides_credentials_labels_and_host_files(tmp_path, monkeypatch):
    from infermatrix_copilot.improve.isolation import argv, probe
    status = probe()
    if not status["ready"]: pytest.skip(status["reason"])
    secret = tmp_path / "credential"; secret.write_text("sensitive")
    candidate = tmp_path / "candidate"; candidate.mkdir()
    payload = tmp_path / "input"; payload.write_text("{}")
    worker = tmp_path / "worker.py"
    worker.write_text("import os, pathlib, socket\nassert 'EVOLUTION_TEST_TOKEN' not in os.environ\nassert not pathlib.Path(" + repr(str(secret)) + ").exists()\nassert not pathlib.Path('/candidate/eval').exists()\nassert not pathlib.Path('/candidate/ledger').exists()\nprint('isolated')\n")
    monkeypatch.setenv("EVOLUTION_TEST_TOKEN", "sensitive")
    completed = subprocess.run(argv(candidate, payload, worker), capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0 and completed.stdout.strip() == "isolated", completed.stderr


def test_mechanical_workflow_reproduces_defect_without_quality_claim(bench):
    st, store, worker, model = bench()
    doc_path = Path(st.improve_workflows_dirs) / "workflow.yaml"
    declaration = yaml.safe_load(doc_path.read_text()); declaration.pop("outcome_adapter"); declaration["experiment_driver"] = "mechanical"
    doc_path.write_text(yaml.safe_dump(declaration))
    root = Path(st.improve_evolve_data_dir) / declaration["workflow"]
    fixture = root / "test_reproducer.py"; fixture.write_text("def test_defect(): assert True\n")
    dataset = json.loads((root / "dataset.json").read_text())
    dataset.update(reproducer=fixture.name, reproducer_sha256=artifacts.digest(fixture.read_bytes()))
    artifacts.atomic_json(root / "dataset.json", dataset)
    original = worker.run
    def run(source, payload, work, **kwargs):
        result = original(source, payload, work, **kwargs)
        if "reproducer" in work.parts: result["rc"] = int(source.name == "incumbent")
        return result
    worker.run = run
    result = evolution.run(st, store, workflow=declaration["workflow"], llm=model, sandbox=worker)
    assert result["state"] == "pr-ready"
    assert result["evaluation"]["kind"] == "mechanical-defect" and result["evaluation"]["quality_claim"] is False
    assert "experiment" not in result


def test_coordinator_resume_keeps_completed_paid_stages(bench, monkeypatch):
    st, store, worker, model = bench()
    from infermatrix_copilot.improve import coordinator
    path = Path(st.improve_ledger_dir) / "coordinator.json"
    artifacts.atomic_json(path, {"state": "running", "at": 1, "stages": {"lint": {"units": 3}, "experiments": [], "forensics": {"ok": True}}})
    monkeypatch.setattr(evolution, "run", lambda *a, **k: {"state": "deferred", "reason": "fixture"})
    result = coordinator.run(st, store)
    assert result["state"] == "complete" and result["units"] == 3
    disabled = st.model_copy(update={"improve_enabled": False})
    assert coordinator.run(disabled, store)["state"] == "disabled"


def test_coordinator_shared_execution_does_not_replay_unknown_paid_phase(bench, monkeypatch):
    import asyncio
    st, store, _, _ = bench()
    from infermatrix_copilot.improve import coordinator, cycle
    from infermatrix_copilot.engine.steps import improve
    monkeypatch.setattr(cycle, "run_cycle", lambda *a, **k: {"units": 0})
    sent = []

    async def interrupted(ctx):
        sent.append(True)
        raise RuntimeError("dispatched without a result")

    monkeypatch.setattr(improve, "_forensics", interrupted)
    with pytest.raises(RuntimeError, match="dispatched without a result"):
        coordinator.run(st, store)
    checkpoint = json.loads((Path(st.improve_ledger_dir) / "coordinator.json").read_text())
    assert checkpoint["stages"]["forensics"]["ok"] is False
    monkeypatch.setattr(evolution, "run", lambda *a, **k: {"state": "deferred"})
    result = asyncio.run(coordinator.run_async(st, store))
    assert result["state"] == "complete" and sent == [True]


def test_human_annotation_tools_freeze_real_unit_and_require_explicit_labels(tmp_path):
    from infermatrix_copilot.improve.annotations import operate
    store = TraceStore(tmp_path / "traces")
    store.append("decision", context={"workflow": "demo.step", "unit_id": "historical", "item": "demo#1"}, outputs={"reply": "recorded evidence"}, result={"type": "step_result"})
    gold = tmp_path / "gold.json"
    from infermatrix_copilot.improve.gold import gold_id
    gid = gold_id("demo#1", "a.py", "missing error")
    gold.write_text(json.dumps({"item": "demo#1", "status": "curated", "entries": [{"gold_id": gid, "path": "a.py", "concern": "missing error"}]}))
    args = SimpleNamespace(action="export", case="case", meta_dir=str(tmp_path / "meta"), unit_id="historical", gold_file=str(gold))
    result = operate(args, store)
    assert result["annotation_required"] and (Path(result["case"]) / "records.jsonl").exists()
    args.action = "annotate"; args.labels_file = ""; args.human_verified = False; args.split = "holdout"
    with pytest.raises(artifacts.ArtifactError, match="human-verified"): operate(args, store)
    labels = tmp_path / "labels.json"; labels.write_text(json.dumps({gid: "S2"}))
    args.labels_file = str(labels); args.human_verified = True
    assert operate(args, store)["labelled_cells"] == 1
    case = json.loads((Path(result["case"]) / "case.json").read_text())
    assert case["label_source"] == "human" and case["split"] == "holdout"


def test_coordinator_forensics_uses_selected_store_and_records_fingerprint(bench, monkeypatch):
    st, store, _, _ = bench()
    from infermatrix_copilot.improve import coordinator, cycle
    from infermatrix_copilot.engine.steps import improve
    from infermatrix_copilot.engine.step import StepResult
    from infermatrix_copilot.trace_store import current_store
    monkeypatch.setattr(cycle, "run_cycle", lambda *a, **k: {"since": 0, "until": 1, "units": 0})
    async def forensics(ctx):
        assert current_store() is store and ctx.settings.trace_store_root == str(store.root)
        return StepResult(True, summary="fixture")
    monkeypatch.setattr(improve, "_forensics", forensics)
    monkeypatch.setattr(evolution, "run", lambda *a, **k: {"state": "deferred"})
    result = coordinator.run(st, store)
    assert result["state"] == "complete"
    record = store.query(workflow="workflow-improve.improve.forensics")[-1]
    assert record["context"]["fingerprint"] and record["result"]["type"] == "step_result"
    assert json.loads(store.blob(record["inputs"]["fingerprint_manifest"]))["complete"]
    # Same week retains forensic checkpoint.
    monkeypatch.setattr(improve, "_forensics", lambda ctx: pytest.fail("completed paid phase repeated"))
    assert coordinator.run(st, store)["state"] == "complete"


def test_tier2_workflow_can_fix_mechanical_defect_without_quality_gold(bench):
    st, store, worker, model = bench()
    workflow = "workflow-improve.improve.forensics"
    ledger = Ledger(Path(st.improve_ledger_dir)); row = ledger.load(workflow)
    row.proposals[0].tier = 1; row.proposals[0].lint = "L01"; ledger.save(row)
    root = Path(st.improve_evolve_data_dir) / workflow
    fixture = root / "test_reproducer.py"; fixture.write_text("def test_defect(): assert True\n")
    dataset = json.loads((root / "dataset.json").read_text())
    dataset["items"] = [dataset["items"][0]]
    dataset.update(reproducer=fixture.name, reproducer_sha256=artifacts.digest(fixture.read_bytes()))
    artifacts.atomic_json(root / "dataset.json", dataset)
    assert not evolution.check(st, store, workflow, sandbox=worker)["ready"]
    assert evolution.check(st, store, workflow, sandbox=worker, mechanical=True)["ready"]
    original = worker.run
    def run(source, payload, work, **kwargs):
        result = original(source, payload, work, **kwargs)
        if "reproducer" in work.parts: result["rc"] = int(source.name == "incumbent")
        return result
    worker.run = run
    result = evolution.run(st, store, workflow=workflow, llm=model, sandbox=worker)
    assert result["state"] == "pr-ready" and result["evaluation"]["quality_claim"] is False


@pytest.mark.parametrize("verdict,net,coverage", [("yes", 1, 1), ("no", -1, 0)])
def test_kb_driver_uses_production_gate_and_only_valid_rule_coverage(bench, monkeypatch, verdict, net, coverage):
    st, store, _, model = bench("kb-intake", "kb-intake.draft")
    from test_kb_intake_gate import _tree, _rule, _judge_all, ScriptedGateway, PAGE
    from infermatrix_copilot.improve.adapters import kb_intake
    gateway = ScriptedGateway(_judge_all(verdict))
    monkeypatch.setattr(drivers.Gateway, "call_json", lambda self, *a, **k: gateway.call_json(*a, **k))
    section = _rule("DEMO-2a", "PR #11")
    monkeypatch.setattr(kb_intake, "run_judge", lambda *a, **k: {"status": "hit", "quote": "队列满时拒绝新请求并返回明确错误"})
    row = {"item": "demo#11", "version": "frozen", "payload": {"files": _tree(), "release": "v1", "today": "2026-09-28",
           "evidence": {"source_reference": "PR #11"}, "repo_dir": "repos/demo"},
           "gold": {"entries": [{"gold_id": "g", "path": PAGE, "concern": section}]}}
    result = {"operations": [{"kind": "add", "page": PAGE, "rule_id": "DEMO-2a", "section_markdown": section}], "scores": {"net_pass_review": 900}}
    before = dict(row["payload"]["files"])
    unit = drivers.make_unit(store, "kb-intake.draft", row["item"], "f", result, "unit")
    score = drivers.score_kb(row, result, unit, store, st, model, None)
    assert score["net_pass_review"] == net and score["recall_gold"] == coverage
    assert row["payload"]["files"] == before


def test_adopted_configuration_reaches_model_client_without_mutating_other_steps(bench, tmp_path):
    st, _, _, _ = bench()
    import asyncio
    from infermatrix_copilot.app.workflow_execution import WorkflowExecution
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepResult, StepSpec
    from infermatrix_copilot.playbooks.store import Playbook, PlaybookStep
    artifacts.atomic_json(st.playbooks_dir / "evolution-overrides.json", {"workflow-improve.improve.forensics": {"LLM_MAX_TOKENS": "700"}})
    llm = SimpleNamespace(settings=st)
    async def handler(ctx):
        assert ctx.llm.settings.llm_max_tokens == ctx.settings.llm_max_tokens == 700
        return StepResult(True)
    registry = StepRegistry()
    registry.register(StepSpec("improve.forensics", "agent", "read", handler))
    plan = Playbook("workflow-improve", 1, "active", [], [], [PlaybookStep("forensics", "improve.forensics")])
    result = asyncio.run(WorkflowExecution(st, registry).execute(plan, run_dir=tmp_path / "run", state={}, llm=llm))
    assert result.status == "done" and llm.settings.llm_max_tokens == 1000


def test_existing_baseline_refuses_new_revision_before_paid_generation(bench, tmp_path):
    st, _, _, _ = bench()
    source = Path(st.improve_evolve_source_dir)
    baseline = tmp_path / "baseline"; artifacts.baseline(source, baseline)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "--allow-empty", "-qm", "advanced"], check=True)
    with pytest.raises(artifacts.ArtifactError, match="advanced"):
        artifacts.baseline(source, baseline)


def test_patch_without_final_newline_applies_to_exact_baseline(bench, tmp_path):
    st, _, _, model = bench()
    source = Path(st.improve_evolve_source_dir)
    base = tmp_path / "baseline"; artifacts.baseline(source, base)
    model.proposal["files"][0]["content"] = "# improved"
    arm = tmp_path / "arm"
    artifacts.apply_candidate(base, arm, model.proposal, {"paths": [model.proposal["files"][0]["path"]]})
    patch = tmp_path / "candidate.patch"; patch.write_text(artifacts.patch(base, arm))
    assert "No newline at end of file" in patch.read_text()
    artifacts.git(source, "apply", "--check", str(patch))


def test_coordinator_scopes_forensics_and_resumes_each_paid_scope(bench, monkeypatch):
    st, store, _, _ = bench()
    from infermatrix_copilot.improve import coordinator, cycle
    from infermatrix_copilot.engine.steps import improve
    from infermatrix_copilot.engine.step import StepResult
    seen = []
    monkeypatch.setattr(cycle, "run_cycle", lambda *a, **k: {"since": 0, "until": 1, "units": 0})
    async def forensics(ctx):
        seen.append(ctx.params)
        return StepResult(True, summary="fixture")
    monkeypatch.setattr(improve, "_forensics", forensics)
    monkeypatch.setattr(evolution, "run", lambda *a, **k: {"state": "deferred"})
    for workflow in ("pr-review.agent.review_diff", "pr-review.agent.review_diff", "kb-intake.draft"):
        assert coordinator.run(st, store, workflow=workflow, repo="demo")["state"] == "complete"
    assert seen == [{"workflow": "pr-review.agent.review_diff", "repo": "demo"}, {"workflow": "kb-intake.draft", "repo": "demo"}]

    records = store.query(workflow="workflow-improve.improve.forensics")
    unit_ids = {record["context"]["unit_id"] for record in records
                if record.get("result", {}).get("type") == "step_result"}
    assert len(unit_ids) == 2
    assert any("forensics:pr-review.agent.review_diff:demo" in unit for unit in unit_ids)
    assert any("forensics:kb-intake.draft:demo" in unit for unit in unit_ids)


def test_unconfigured_model_tier_is_a_readiness_reason(bench):
    st, store, worker, _ = bench()
    st = st.model_copy(update={"performance_model": "", "strict_backend": "", "strict_backend_model": ""})
    result = evolution.check(st, store, "workflow-improve.improve.forensics", sandbox=worker)
    assert not result["ready"] and "model-tier-unconfigured: performance" in result["reasons"]


def test_evolution_and_experiment_cli_share_the_same_execution_lock(bench, monkeypatch):
    st, store, worker, model = bench()
    from infermatrix_copilot.trace_store import file_lock
    original = experiments.run
    def concurrently_locked(sink, settings, root, eid, **kwargs):
        with file_lock(Path(root) / "experiments" / (eid + ".lock"), blocking=False):
            return original(sink, settings, root, eid, **kwargs)
    monkeypatch.setattr(experiments, "run", concurrently_locked)
    result = evolution.run(st, store, workflow="workflow-improve.improve.forensics", llm=model, sandbox=worker)
    assert result["state"] == "deferred" and "already running" in result["reason"]
    assert len(worker.calls) == 1
    monkeypatch.setattr(experiments, "run", original)
    result = evolution.run(st, store, workflow="workflow-improve.improve.forensics", llm=model, sandbox=worker)
    assert result["state"] == "pr-ready" and len(model.calls) == 1 and len(worker.calls) == 49


def test_isolation_loss_defers_without_success_or_replaying_paid_generation(bench):
    st, store, worker, model = bench()
    original = worker.run
    def unavailable(source, payload, work, **kwargs):
        if payload["mode"] == "predict": raise SandboxUnavailable("namespace disappeared")
        return original(source, payload, work, **kwargs)
    worker.run = unavailable
    first = evolution.run(st, store, workflow="workflow-improve.improve.forensics", llm=model, sandbox=worker)
    assert first["state"] == "deferred" and first["evaluation"]["blocking"] == "isolation"
    assert not first["evaluation"].get("promotable")
    worker.run = original
    again = evolution.run(st, store, workflow=first["workflow"], llm=model, sandbox=worker)
    assert again["state"] == "deferred" and len(model.calls) == 1
    assert "fresh-holdout" in again["reason"]


def test_direct_versioned_experiment_cannot_bypass_kill_switch(bench, monkeypatch):
    st, store, worker, model = bench()
    original = experiments.run
    def locked(*args, **kwargs): raise experiments.ExperimentError("already running")
    monkeypatch.setattr(experiments, "run", locked)
    c = evolution.run(st, store, workflow="workflow-improve.improve.forensics", llm=model, sandbox=worker)
    monkeypatch.setattr(experiments, "run", original)
    disabled = st.model_copy(update={"improve_enabled": False})
    with pytest.raises(experiments.ExperimentError, match="IMPROVE_ENABLED"):
        experiments.run(store, disabled, Path(st.improve_ledger_dir), c["experiment"], judge_llm=model, sandbox=worker)
    assert experiments.load(Path(st.improve_ledger_dir), c["experiment"]).state == "registered"
    assert len(worker.calls) == 1
