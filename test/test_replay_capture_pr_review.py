"""Oversize replay capture preserves the PR review release boundary."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.engine.step import FailureKind
from infermatrix_copilot.improve import evolution, objectives, runtime
from infermatrix_copilot.trace_store import TraceStore, bind_store


WORKFLOW = 'pr-review.agent.review_diff'


@pytest.mark.parametrize('entry,blocked', [
    ({}, False),
    ({'workflows': ['other.workflow'], 'candidate': 'other', 'disabled': True}, False),
    ({'workflows': [WORKFLOW], 'candidate': 'review-candidate'}, True),
    ({'workflows': [WORKFLOW], 'candidate': 'review-candidate', 'disabled': True}, True),
], ids=['baseline', 'unrelated-disabled-release', 'active-review', 'disabled-review'])
def test_oversize_review_replay_never_executes_or_becomes_dataset_input(tmp_path, monkeypatch, entry, blocked):
    store = TraceStore(tmp_path / 'traces', environ={})
    settings = Settings(_env_file=None, improve_ledger_dir=str(tmp_path / 'ledger'),
                        improve_evolve_data_dir=str(tmp_path / 'inputs'))
    frozen = {'repo': 'demo', 'pr': 1, 'base_sha': 'b' * 40, 'head_sha': 'a' * 40,
              'base_files': {'x.py': 'old\n'}, 'head_files': {'x.py': 'new\n'},
              'diff': 'unabridged evidence\n' * 30, 'knowledge_files': {}}
    candidate = {'id': 'review-candidate'}
    rollbacks = []
    monkeypatch.setattr(objectives, 'REPLAY_INPUT_MAX_BYTES', 128)
    monkeypatch.setattr(runtime, 'active', lambda _: entry)
    monkeypatch.setattr(runtime, 'freeze_review', lambda *_: frozen)
    monkeypatch.setattr(evolution, 'candidate', lambda _, identifier: candidate if identifier == candidate['id'] else pytest.fail('wrong release'))
    monkeypatch.setattr(runtime, 'rollback', lambda *args: rollbacks.append(args))
    monkeypatch.setattr(runtime, 'execute', lambda *args, **kwargs: pytest.fail('uncaptured input reached evolved review'))
    ctx = SimpleNamespace(settings=settings, state={}, llm=None)

    with bind_store(store):
        result = asyncio.run(runtime.review_step(ctx))

    if blocked:
        assert result.ok is False and result.failure == FailureKind.BLOCKED
        assert result.summary == 'active review lacks a safe frozen replay input'
        assert len(rollbacks) == 1 and rollbacks[0][2] is candidate
    else:
        assert result is None  # the ordinary review handler continues
        assert rollbacks == []
    records = store.query(limit=20)
    assert not any(r['result'].get('type') == 'replay_input' for r in records)
    deferred = [r for r in records if r['result'].get('type') == 'replay_capture_deferred']
    assert len(deferred) == 1 and deferred[0]['inputs'] == {}
    assert deferred[0]['context']['item'] == 'demo#1@' + 'a' * 40
    assert deferred[0]['result']['input_bytes'] > deferred[0]['result']['limit_bytes'] == 128
    assert any(r['result'].get('type') == 'replay_capture_missing' for r in records)
    assert objectives.collect(settings, store, WORKFLOW) == 0
    assert objectives.rows(settings, WORKFLOW) == []


@pytest.mark.parametrize("carried", [False, True])
def test_incompatible_worker_uses_native_before_model_spend(tmp_path, settings, monkeypatch, carried):
    root = Path(__file__).resolve().parents[1] if carried else tmp_path
    store = TraceStore(tmp_path / "traces", environ={})
    frozen = {"repo": "demo", "pr": 1, "head_sha": "a" * 40,
              "task_params": {"carried_findings": [{"finding_id": "previous"}]} if carried else {}}
    monkeypatch.setattr(runtime, "active", lambda _: {"workflows": [WORKFLOW], "artifact": str(root)})
    monkeypatch.setattr(runtime, "freeze_review", lambda *_: frozen)
    monkeypatch.setattr(runtime, "execute", lambda *_: pytest.fail("incompatible worker reached model execution"))
    with bind_store(store):
        assert asyncio.run(runtime.review_step(SimpleNamespace(settings=settings, state={}))) is None
