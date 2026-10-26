"""Recovery must not combine candidate content with another execution's provenance."""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service import init_execution
from infermatrix_copilot.kb_service.init_stages import _Stage, run_stage
from infermatrix_copilot.kb_service.init_support import InitError, InitRecord, InitRuntime
from test_kb_init_skeleton import FakeGateway, FakeGh, _commit, _lifecycle, _runtime, _tree, world  # noqa: F401


def test_returning_to_an_old_execution_refuses_a_replaced_business_record(world, monkeypatch):
    gateway = FakeGateway()
    rt = _runtime(world, gateway)
    validate = init_execution.validate
    monkeypatch.setattr(init_execution, "policy_digest", lambda: "policy-A")
    monkeypatch.setattr(init_execution, "validate", lambda *_: (_ for _ in ()).throw(RuntimeError("interrupted")))
    first = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    assert first.status == "blocked" and first.evidence

    monkeypatch.setattr(init_execution, "policy_digest", lambda: "policy-B")
    gateway.doc_rules, gateway.seed_rules = [], False
    second = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    assert second.status == "blocked" and not second.evidence

    monkeypatch.setattr(init_execution, "policy_digest", lambda: "policy-A")
    monkeypatch.setattr(init_execution, "validate", validate)
    calls = len(gateway.calls)
    recovered = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    assert len(gateway.calls) == calls
    # A saved candidate cannot borrow B's provenance merely because source
    # and configuration match. Refuse rather than publish an unbound record.
    assert recovered.status == "blocked", _tree(recovered) if recovered.status == "dry_run" else recovered.problems


def test_cli_reports_initial_runtime_failure_without_an_unhandled_exception(tmp_path, monkeypatch, capsys):
    from infermatrix_copilot.kb_service import cli

    def unavailable(*args, **kwargs):
        raise InitError("knowledge clone unavailable")

    monkeypatch.setattr("infermatrix_copilot.config.Settings", lambda: SimpleNamespace())
    monkeypatch.setattr(InitRuntime, "from_env", unavailable)
    assert cli.main(["--state-dir", str(tmp_path), "init", "toy", "--stage", "skeleton", "--dry-run"]) == 1
    assert "knowledge clone unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--pr-count", "--budget-usd"])
def test_cli_rejects_zero_before_binding_a_runtime(tmp_path, monkeypatch, capsys, option):
    from infermatrix_copilot.kb_service import cli

    monkeypatch.setattr(InitRuntime, "from_env", lambda *a, **kw: pytest.fail("invalid request bound a runtime"))
    assert cli.main(["--state-dir", str(tmp_path), "init", "toy", "--stage", "pr-history", option, "0"]) == 2
    assert "positive" in capsys.readouterr().err


def test_initialization_policy_binds_the_execution_graph(tmp_path, monkeypatch):
    from infermatrix_copilot.sdk import _resources

    graph = tmp_path / "kb-init-stage.yaml"
    graph.write_text("steps: [prepare, draft, validate, prepare_publication, publish]")
    monkeypatch.setattr(_resources, "resource_dir", lambda _: tmp_path)
    original = init_execution.policy_digest()
    graph.write_text("steps: [prepare, draft, publish]")
    assert init_execution.policy_digest() != original


def test_compatibility_step_resume_rechecks_the_inner_initialization_gate(settings, tmp_path, monkeypatch):
    from infermatrix_copilot.app.workflow_execution import WorkflowExecution
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.steps import register_builtin_steps
    from infermatrix_copilot.playbooks.store import Playbook, PlaybookStep

    calls = []

    async def gated(*args, **kwargs):
        calls.append(True)
        return InitRecord(stage="skeleton", repo="toy", status="dry_run" if len(calls) == 1 else "blocked",
                          problems=[] if len(calls) == 1 else ["source receipt unavailable"])

    monkeypatch.setattr("infermatrix_copilot.kb_service.init_stages.run_stage_async", gated)
    execution = WorkflowExecution(settings, register_builtin_steps(StepRegistry()))
    playbook = Playbook("kb-init", 1, "candidate", [], [], [
        PlaybookStep("init", "knowledge.init", {"repo": "toy", "stage": "skeleton"})])
    runtime = SimpleNamespace(registry={"toy": object()})
    first = asyncio.run(execution.execute(playbook, run_dir=tmp_path, state={}, runtime=runtime))
    assert first.status == "done"
    resumed = asyncio.run(execution.execute(playbook, run_dir=tmp_path, state={}, runtime=runtime))
    assert resumed.status == "blocked" and "source receipt unavailable" in resumed.blocked_reason
    assert len(calls) == 2


@pytest.mark.parametrize("receipt_key", ["foundation", "foundation_publication"])
def test_publication_receipt_does_not_invalidate_the_candidate_on_preparation_resume(world, monkeypatch, receipt_key):
    gateway = FakeGateway()
    rt = _runtime(world, gateway)
    original = _Stage._prepare_publication
    prepared = []

    def interrupted(work, files):
        # Knowledge's publication hook adds this derived coverage receipt
        # after draft/validate; interruption can follow the pending save.
        work.record.coverage[receipt_key] = {"derived_publication_receipt": True}
        result = original(work, files)
        if not prepared:
            prepared.append(True)
            raise RuntimeError("publication proof interrupted")
        return result

    monkeypatch.setattr(_Stage, "_prepare_publication", interrupted)
    first = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    assert first.status == "blocked" and first.pr["validation_pending"]
    calls = len(gateway.calls)
    recovered = run_stage(rt, _lifecycle(), "skeleton", dry_run=True)
    assert recovered.status == "dry_run", recovered.problems
    assert len(gateway.calls) == calls


@pytest.mark.parametrize("damage", [None, "hash", "binding"])
def test_prepared_publication_retains_its_frozen_binding_after_main_advances(world, damage):
    gh = FakeGh(fail_create=1)
    environ = {"ALLOW_PUSH": "1", "ALLOW_POST": "1",
               "KB_INIT_GIT_AUTHOR": "Tester <tester@example.invalid>"}
    rt = _runtime(world, environ=environ, gh_run=gh)
    first = run_stage(rt, _lifecycle(), "skeleton", dry_run=False)
    assert first.status == "blocked" and len(gh.pushed) == 1 and not gh.created
    prepared_bytes = Path(first.pr["prepared"]).read_bytes()
    _commit(world["origin"], {"unrelated.md": "another merged change\n"}, "unrelated advance")

    if damage:
        proof = first.pr["validation"]
        ref = proof["artifact"]
        root = InitRecord.path(rt.state_dir, "toy", "skeleton").parent
        artifact = root / "executions" / "skeleton" / proof["execution"] / ref["path"]
        if damage == "hash":
            artifact.write_bytes(artifact.read_bytes() + b" ")
        else:
            payload = json.loads(artifact.read_bytes())
            payload["binding"]["kb_base_sha"] = "f" * 40
            raw = json.dumps(payload).encode()
            artifact.write_bytes(raw)
            ref["sha256"] = hashlib.sha256(raw).hexdigest()
            first.save(rt.state_dir)

    gateway = FakeGateway()
    later = _runtime(world, gateway, environ=environ, gh_run=gh)
    checkpoint = InitRecord.path(rt.state_dir, "toy", "skeleton")
    before = checkpoint.read_bytes()
    if damage:
        with pytest.raises(InitError, match="initialization artifact"):
            run_stage(later, _lifecycle(), "skeleton", dry_run=False)
        assert checkpoint.read_bytes() == before and not gh.created
    else:
        resumed = run_stage(later, _lifecycle(), "skeleton", dry_run=False)
        assert resumed.status == "published" and resumed.pr["head_sha"] == gh.pushed[0]
        assert resumed.kb_base_sha == first.kb_base_sha
        assert Path(resumed.pr["prepared"]).read_bytes() == prepared_bytes
    assert len(gh.pushed) == 1 and gateway.calls == []
