"""Replay limits must neither truncate knowledge input nor bypass an active arm."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.improve import artifacts, objectives, runtime as improve_runtime
from infermatrix_copilot.kb_service import runtime as kb_runtime
from infermatrix_copilot.kb_service.config import RepoLifecycle
from infermatrix_copilot.kb_service.intake import Draft
from infermatrix_copilot.kb_service.ledger import Ledger
from infermatrix_copilot.kb_service.models import ModelRole
from infermatrix_copilot.trace_store import TraceStore


WORKFLOW = "kb-intake.draft"


@pytest.fixture
def intake(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        improve_enabled=True,
        improve_evolve_enabled=True,
        improve_evaluation_mode="objective",
        improve_ledger_dir=str(tmp_path / "improve"),
        playbooks_dir=tmp_path / "playbooks",
        eco_model="fixture-model",
    )
    ledger = Ledger(tmp_path / "kb.db")
    ledger.ensure_repo("demo", "shadow")
    evidence = {"source_reference": "PR #17", "body": "Keep the complete evidence.",
                "changed_files": ["owner/module.py"], "diff_excerpt": "+complete source\n"}
    event_id = ledger.record_event("demo", "merged_pr", "17", evidence)
    # Ledger serializes evidence with sorted keys; pin the bytes the runtime
    # actually reads rather than a semantically equal, differently ordered dict.
    evidence = ledger.event(event_id)["payload"]
    files = {
        "repos/demo/rules.md": "owner start\n" + "x" * 4096 + "\nowner end 完整\n",
        "repos/other/rules.md": "unrelated owner is also retained\n",
    }
    external = {"README.md": "complete external context\n"}
    generator = ModelRole.parse("generator", "codex:pinned-fixture")
    rt = SimpleNamespace(
        settings=settings,
        ledger=ledger,
        traces=TraceStore(tmp_path / "trace", environ={}),
        knowledge=SimpleNamespace(fetch=lambda: "a" * 40,
                                  knowledge_files=lambda _: files,
                                  external_texts=lambda _: external),
        release_for=lambda _: "v1",
        today=lambda: "2026-10-09",
        lease_owner=None,
        generator=generator,
        gateway=object(),
    )
    lifecycle = RepoLifecycle(repo="demo", full_name="org/demo", enabled=True,
                              mode="shadow", knowledge_dir="repos/demo")
    drafts = []

    def pinned_draft(**kwargs):
        drafts.append(kwargs)
        return Draft(event_ids=[kwargs["event_id"]], operations=[], result=None)

    def forbidden_execute(*args, **kwargs):
        pytest.fail("an uncaptured input must never execute an autonomous artifact")

    monkeypatch.setattr(kb_runtime, "draft_changes", pinned_draft)
    monkeypatch.setattr(improve_runtime, "execute", forbidden_execute)
    # Constructing a model is also unnecessary on the capture-overflow path.
    monkeypatch.setattr("infermatrix_copilot.llm.LLM", lambda _: object())
    payload = {"repo": "demo", "repo_dir": "repos/demo", "event_id": event_id,
               "evidence": evidence, "files": files, "release": "v1", "today": "2026-10-09",
               "external_texts": external, "generator_model": generator.model}
    return SimpleNamespace(rt=rt, lifecycle=lifecycle, files=files, payload=payload,
                           event_id=event_id, drafts=drafts, settings=settings)


def assert_deferred_capture_only(world):
    records = world.rt.traces.query(workflow=WORKFLOW)
    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "decision"
    assert record["context"]["item"] == f"demo#{world.event_id}"
    assert record["inputs"] == record["outputs"] == {}
    encoded = json.dumps(world.payload, ensure_ascii=False).encode()
    assert record["result"] == {
        "type": "replay_capture_deferred", "reason": "size_limit",
        "input_bytes": len(encoded), "limit_bytes": objectives.REPLAY_INPUT_MAX_BYTES,
        "input_sha": artifacts.digest(encoded),
    }
    assert not list((world.rt.traces.root / "blobs").glob("**/*.gz"))
    assert objectives.collect(world.settings, world.rt.traces, WORKFLOW) == 0
    assert objectives.rows(world.settings, WORKFLOW) == []


@pytest.mark.parametrize('fault', ['crash', 'unknown_exception'])
def test_evolved_dispatch_is_not_repeated_after_interruption(intake, monkeypatch, fault):
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(1)
        if fault == 'crash':
            raise KeyboardInterrupt('evolved worker interrupted')
        raise OSError('evolved worker response lost')

    monkeypatch.setattr(improve_runtime, 'execute', interrupted)
    if fault == 'crash':
        with pytest.raises(KeyboardInterrupt):
            kb_runtime.run_intake(intake.rt, intake.lifecycle)
    else:
        assert kb_runtime.run_intake(intake.rt, intake.lifecycle) is None
    assert calls == [1] and not intake.drafts
    assert kb_runtime.run_intake(intake.rt, intake.lifecycle) is None
    assert calls == [1] and not intake.drafts


def test_completed_evolved_result_survives_interruption_before_event_settlement(intake, monkeypatch):
    calls = []

    def completed(*args, **kwargs):
        calls.append(1)
        return {'operations': [], 'rationale': 'no rule needed'}

    settle = intake.rt.ledger.set_event_statuses
    monkeypatch.setattr(improve_runtime, 'execute', completed)

    def interrupted(*args, **kwargs):
        raise OSError('event settlement interrupted')

    monkeypatch.setattr(intake.rt.ledger, 'set_event_statuses', interrupted)
    with pytest.raises(OSError, match='event settlement interrupted'):
        kb_runtime.run_intake(intake.rt, intake.lifecycle)
    monkeypatch.setattr(intake.rt.ledger, 'set_event_statuses', settle)
    assert kb_runtime.run_intake(intake.rt, intake.lifecycle) is None
    assert calls == [1] and not intake.drafts
    assert intake.rt.ledger.event(intake.event_id)['status'] == 'done'


@pytest.mark.parametrize("entry", [
    {},
    {"workflows": ["pr-review.agent.review_diff"]},
    {"workflows": ["pr-review.agent.review_diff"], "disabled": True},
])
def test_oversize_capture_keeps_full_pinned_baseline_without_partial_replay(intake, monkeypatch, entry):
    monkeypatch.setattr(objectives, "REPLAY_INPUT_MAX_BYTES", 1024)
    artifacts.atomic_json(improve_runtime.registry(intake.settings), entry)

    assert kb_runtime.run_intake(intake.rt, intake.lifecycle) is None

    assert len(intake.drafts) == 1
    draft = intake.drafts[0]
    assert draft["files"] is intake.files
    assert draft["files"] == intake.payload["files"]
    assert draft["evidence"] == intake.payload["evidence"]
    assert draft["generator"] is intake.rt.generator
    assert draft["gateway"] is intake.rt.gateway
    assert draft["release"] == intake.payload["release"]
    assert draft["today"] == intake.payload["today"]
    assert intake.rt.ledger.event(intake.event_id)["status"] == "done"
    assert_deferred_capture_only(intake)


@pytest.mark.parametrize("disabled", [False, True])
def test_oversize_active_knowledge_arm_stays_pending_even_when_disabled(intake, monkeypatch, disabled):
    monkeypatch.setattr(objectives, "REPLAY_INPUT_MAX_BYTES", 1024)
    artifacts.atomic_json(improve_runtime.registry(intake.settings),
                          {"workflows": [WORKFLOW], "disabled": disabled})

    assert kb_runtime.run_intake(intake.rt, intake.lifecycle) is None

    assert intake.drafts == []
    event = intake.rt.ledger.event(intake.event_id)
    assert event["status"] == "pending"
    assert event["payload"] == intake.payload["evidence"]
    assert "generator unavailable" in event["detail"]
    assert_deferred_capture_only(intake)


@pytest.mark.parametrize("raw_registry", [
    "{broken json",
    "null",
    "[]",
    '{"workflows": null}',
    '{"workflows": 7}',
    '{"workflows": "kb-intake.draft"}',
])
def test_oversize_capture_with_unverifiable_active_registry_stays_pending(intake, monkeypatch, raw_registry):
    monkeypatch.setattr(objectives, "REPLAY_INPUT_MAX_BYTES", 1024)
    path = improve_runtime.registry(intake.settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw_registry)

    assert kb_runtime.run_intake(intake.rt, intake.lifecycle) is None

    assert intake.drafts == []
    event = intake.rt.ledger.event(intake.event_id)
    assert event["status"] == "pending"
    assert "cannot verify active draft release" in event["detail"]
    assert_deferred_capture_only(intake)


def test_snapshot_above_old_limit_is_captured_and_collected_without_omissions(intake, monkeypatch):
    target_bytes = 16_080_000
    original_size = len(json.dumps(intake.payload, ensure_ascii=False).encode())
    intake.files["repos/demo/rules.md"] += "y" * (target_bytes - original_size)
    encoded = json.dumps(intake.payload, ensure_ascii=False).encode()
    assert len(encoded) == target_bytes
    assert 16_000_000 < len(encoded) < objectives.REPLAY_INPUT_MAX_BYTES
    executions = []

    def baseline_execute(settings, store, workflow, payload, llm):
        executions.append(payload)
        assert improve_runtime.active(settings) == {}
        return None

    monkeypatch.setattr(improve_runtime, "execute", baseline_execute)
    assert kb_runtime.run_intake(intake.rt, intake.lifecycle) is None

    assert executions == [intake.payload]
    assert len(intake.drafts) == 1
    assert intake.drafts[0]["files"] == intake.files
    records = intake.rt.traces.query(workflow=WORKFLOW)
    assert len(records) == 1
    record = records[0]
    assert record["result"] == {"type": "replay_input", "input_sha": artifacts.digest(encoded)}
    assert intake.rt.traces.blob(record["inputs"]["evolution_input"]).encode() == encoded
    assert objectives.collect(intake.settings, intake.rt.traces, WORKFLOW) == 1
    rows = objectives.rows(intake.settings, WORKFLOW)
    assert len(rows) == 1
    assert rows[0]["item"] == f"demo#{intake.event_id}"
    assert rows[0]["payload"] == intake.payload
    assert rows[0]["origin"] == {"kind": "trace-replay", "record": record["id"]}
    assert intake.rt.ledger.event(intake.event_id)["status"] == "done"


@pytest.mark.parametrize("failure", ["evaluator_material", "trace_io"])
def test_capture_failures_other_than_typed_size_limit_remain_hard(intake, monkeypatch, failure):
    if failure == "evaluator_material":
        original = objectives.capture

        def forbidden_capture(store, workflow, item, payload):
            return original(store, workflow, item, {**payload, "labels": {"must_not_replay": True}})

        monkeypatch.setattr(objectives, "capture", forbidden_capture)
        error, message = artifacts.ArtifactError, "evaluator material"
    else:
        def failed_append(*args, **kwargs):
            raise OSError("trace disk unavailable")

        monkeypatch.setattr(intake.rt.traces, "append", failed_append)
        error, message = OSError, "trace disk unavailable"

    with pytest.raises(error, match=message):
        kb_runtime.run_intake(intake.rt, intake.lifecycle)

    assert intake.drafts == []
    assert intake.rt.ledger.event(intake.event_id)["status"] == "pending"
    assert intake.rt.traces.query(workflow=WORKFLOW) == []
