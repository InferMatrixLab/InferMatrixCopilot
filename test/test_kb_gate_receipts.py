"""Recover completed review work from disk without a model runtime."""
import json

import pytest

from infermatrix_copilot.kb_service import cli
from infermatrix_copilot.kb_service.gate_receipts import settle_gate_result
from infermatrix_copilot.kb_service.runtime import collect_events, run_intake
from test_kb_intake_gate import _runtime, _generator_then_judge, ScriptedGateway


@pytest.fixture
def pending(tmp_path, monkeypatch):
    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(_generator_then_judge()))
    original = rt.ledger.stage_intake

    def fail(*args, **kwargs):
        if kwargs.get('expected_status'):
            raise OSError('settlement unavailable')
        return original(*args, **kwargs)

    monkeypatch.setattr(rt.ledger, 'stage_intake', fail)
    collect_events(rt, lifecycle)
    with pytest.raises(OSError, match='settlement unavailable'):
        run_intake(rt, lifecycle)
    monkeypatch.setattr(rt.ledger, 'stage_intake', original)
    row, = rt.ledger.changesets('demo', ('review_pending',))
    return rt, row


def verdict_count(rt):
    return rt.ledger._conn.execute('SELECT count(*) FROM verdicts').fetchone()[0]


def test_cli_recovers_once_without_loading_registry_or_models(pending, monkeypatch):
    rt, row = pending
    calls = list(rt.gateway.calls)
    monkeypatch.setattr(cli, '_registry', lambda: pytest.fail('recovery must not synchronize registry or construct runtime'))
    args = ['--state-dir', str(rt.state_dir), 'reconcile-review', row['id']]
    assert cli.main(args) == 0
    assert rt.ledger.changeset(row['id'])['status'] == 'gated'
    count = verdict_count(rt)
    assert count > 0
    assert cli.main(args) == 0
    assert verdict_count(rt) == count
    assert rt.gateway.calls == calls
    assert not (rt.state_dir / 'outbox').exists()


@pytest.mark.parametrize('fault', ['missing', 'draft', 'intent', 'generation', 'companion', 'malformed'])
def test_invalid_or_unsupported_recovery_keeps_intent_pending(pending, fault):
    rt, row = pending
    receipt = rt.state_dir / 'gate-results' / f"{row['id']}.json"
    if fault == 'missing':
        receipt.unlink()
    elif fault == 'draft':
        rt.changeset_path(row['id']).write_text('{}')
    elif fault == 'intent':
        rt.ledger.update_changeset(row['id'], detail={**row['detail'], 'changed': True})
    elif fault == 'generation':
        rt.ledger.bump_generation('demo', pause=True, reason='owner paused')
    elif fault == 'companion':
        data = json.loads(receipt.read_text())
        data.pop('companion', None)  # a legacy receipt cannot reconstruct the edits
        receipt.write_text(json.dumps({**data, 'companion_required': True}))
    else:
        receipt.write_text('[]')
    calls = list(rt.gateway.calls)
    assert cli.main(['--state-dir', str(rt.state_dir), 'reconcile-review', row['id']]) == 1
    assert rt.ledger.changeset(row['id'])['status'] == 'review_pending'
    assert verdict_count(rt) == 0
    assert rt.gateway.calls == calls


def test_lost_commit_reply_is_idempotent(pending, monkeypatch):
    rt, row = pending
    stage = rt.ledger.stage_intake

    def committed(*args, **kwargs):
        stage(*args, **kwargs)
        raise OSError('commit reply lost')

    owner = rt.ledger.acquire_lease('recovery')
    monkeypatch.setattr(rt.ledger, 'stage_intake', committed)
    with pytest.raises(OSError, match='commit reply lost'):
        settle_gate_result(rt.ledger, rt.state_dir, owner, row['id'])
    count = verdict_count(rt)
    assert count > 0
    assert settle_gate_result(rt.ledger, rt.state_dir, owner, row['id'])['status'] == 'gated'
    assert verdict_count(rt) == count


def test_generation_changes_during_settlement_are_fenced(pending, monkeypatch):
    rt, row = pending
    stage = rt.ledger.stage_intake

    def changed(*args, **kwargs):
        rt.ledger.bump_generation('demo', pause=True, reason='concurrent pause')
        return stage(*args, **kwargs)

    monkeypatch.setattr(rt.ledger, 'stage_intake', changed)
    owner = rt.ledger.acquire_lease('recovery')
    with pytest.raises(ValueError, match='generation changed'):
        settle_gate_result(rt.ledger, rt.state_dir, owner, row['id'])
    assert rt.ledger.changeset(row['id'])['status'] == 'review_pending'
    assert verdict_count(rt) == 0
