"""Recover original completion without turning unknown exits into success codes."""
import copy
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

import arm_entry
import arm_runtime
import ledger
import shared_execution
import quality_stage_arm
import run_paired
import stage_completion
from ledger import Refused
from test_run_paired import fixture


def quality_fixture(tmp_path, *, schema=2):
    manifest = fixture(tmp_path)
    entry = Path(arm_entry.__file__).resolve()
    manifest['arm_entry_script'] = str(entry)
    for module in (arm_entry, arm_runtime, shared_execution, ledger):
        path = str(Path(module.__file__).resolve())
        manifest['bindings'][path] = run_paired.hashed(path)
    driver = Path(manifest['stages'][0]['command'][2])
    helper = Path(stage_completion.__file__).resolve()
    wrapper = Path(quality_stage_arm.__file__).resolve()
    packet = {'schema': f'quality-stage/{schema}', 'arm_entry': str(entry),
        'files': {str(p): run_paired.hashed(p) for p in (entry, driver, Path(sys.executable), helper)},
        'phases': [{'name': 'native-workflow', 'python': sys.executable,
                    'driver': str(driver), 'arguments': ['{shim}'], 'timeout_s': 10}]}
    phases = tmp_path / 'phases.json'; phases.write_text(json.dumps(packet))
    manifest['bindings'].update({str(phases): run_paired.hashed(phases), str(wrapper): run_paired.hashed(wrapper)})
    for stage in manifest['stages']:
        stage.update(timeout_s=40, command=[sys.executable, '-I', str(wrapper), '--phases', str(phases),
                                           '--sha256', run_paired.hashed(phases), '--shim', '{shim}'])
    return manifest, packet


def lose_ack(tmp_path, monkeypatch, *, schema=2):
    manifest, packet = quality_fixture(tmp_path, schema=schema)
    real = run_paired.run_process
    def lost(*args, **kwargs):
        result = real(*args, **kwargs)
        assert result.returncode == 0 and result.reason is None
        raise RuntimeError('coordinator lost after original child')
    with monkeypatch.context() as patch:
        patch.setattr(run_paired, 'run_process', lost)
        with pytest.raises(RuntimeError, match='coordinator lost'):
            run_paired.run(manifest, tmp_path / 'run')
    return manifest, tmp_path / 'run', packet


def rows(output, table='stages'):
    with sqlite3.connect(output / (table + '.sqlite')) as db:
        return db.execute('SELECT * FROM '+table).fetchall()


def test_original_workflow_completion_recovers_without_inventing_outer_exit(tmp_path, monkeypatch):
    manifest, output, _ = lose_ack(tmp_path, monkeypatch)
    directory = output / f'00-{manifest["stages"][0]["arm"]}'
    marker = directory / 'native-workflow/dispatch-marker'; stamp = marker.stat().st_mtime_ns
    calls = rows(output, 'calls')
    assert rows(output)[0][3:5] == ('running', None)
    assert not (directory / 'stage-process.json').exists()
    with pytest.raises(Refused, match='reconciliation'):
        run_paired.run(manifest, output)
    run_paired.reconcile_completed_stage(manifest, output, 0)
    assert rows(output)[0][3:5] == ('reconciled_complete', None)
    assert rows(output, 'calls') == calls
    evidence = output / 'reconciled-complete-0.json'; before = evidence.read_bytes()
    run_paired.reconcile_completed_stage(manifest, output, 0)
    assert evidence.read_bytes() == before
    result = run_paired.run(manifest, output)
    assert result['adjudication'] == 'pending' and result['publication'] == 'disabled'
    assert marker.stat().st_mtime_ns == stamp and len(rows(output, 'calls')) == 2
    assert rows(output)[0][4] is None


@pytest.mark.parametrize('damage', ['receipt', 'phase-output', 'phase-process', 'config',
                                  'driver', 'native', 'coordinator-live', 'worker-live'])
def test_incomplete_or_changed_evidence_never_settles_or_dispatches(tmp_path, monkeypatch, damage):
    from contextlib import nullcontext
    manifest, output, packet = lose_ack(tmp_path, monkeypatch)
    directory = output / f'00-{manifest["stages"][0]["arm"]}'
    if damage == 'receipt': (directory / 'quality-completion.json').unlink()
    elif damage == 'phase-output': (directory / 'native-workflow/stdout.txt').write_text('changed')
    elif damage == 'phase-process': (directory / 'native-workflow/phase-process.json').write_text('{"returncode": 7, "reason": null}')
    elif damage == 'config': (directory / 'guard-config.json').write_text('{}')
    elif damage == 'driver': Path(packet['phases'][0]['driver']).write_text('changed')
    elif damage == 'native':
        with sqlite3.connect(output / 'calls.sqlite') as db:
            db.execute("UPDATE calls SET status='reserved'")
    lock = (stage_completion.execution_lock(output / 'coordinator.lock') if damage == 'coordinator-live'
            else stage_completion.execution_lock(directory / 'quality-stage.lock') if damage == 'worker-live'
            else nullcontext())
    original = rows(output); calls = rows(output, 'calls')
    with lock, pytest.raises(Refused):
        run_paired.reconcile_completed_stage(manifest, output, 0)
    assert rows(output) == original and rows(output, 'calls') == calls
    assert not (output / 'reconciled-complete-0.json').exists()


def test_legacy_inner_phase_receipts_cannot_be_promoted_to_outer_completion(tmp_path, monkeypatch):
    manifest, output, _ = lose_ack(tmp_path, monkeypatch, schema=1)
    original = rows(output); calls = rows(output, 'calls')
    with pytest.raises(Refused, match='legacy quality stage'):
        run_paired.reconcile_completed_stage(manifest, output, 0)
    assert rows(output) == original and rows(output, 'calls') == calls


def test_known_terminal_process_can_recover_verification_crash(tmp_path, monkeypatch):
    manifest = fixture(tmp_path); output = tmp_path / 'run'
    with monkeypatch.context() as patch:
        patch.setattr(run_paired, 'verify_native', lambda *args: (_ for _ in ()).throw(RuntimeError('crash')))
        with pytest.raises(RuntimeError, match='crash'):
            run_paired.run(manifest, output)
    assert rows(output)[0][3:5] == ('needs_reconciliation', 0)
    run_paired.reconcile_completed_stage(manifest, output, 0)
    assert rows(output)[0][3:5] == ('reconciled_complete', 0)
    assert run_paired.run(manifest, output)['status'] == 'executed'


def test_changed_reconciled_receipt_refuses_resume(tmp_path, monkeypatch):
    manifest, output, _ = lose_ack(tmp_path, monkeypatch)
    run_paired.reconcile_completed_stage(manifest, output, 0)
    directory = output / f'00-{manifest["stages"][0]["arm"]}'
    (directory / 'quality-completion.json').write_text('{}')
    with pytest.raises(Refused): run_paired.run(manifest, output)
    assert len(rows(output, 'calls')) == 1


def test_completion_helper_must_be_frozen_before_any_phase(tmp_path):
    manifest, packet = quality_fixture(tmp_path)
    phases = tmp_path / 'phases.json'
    packet['files'].pop(str(Path(stage_completion.__file__).resolve()))
    phases.write_text(json.dumps(packet)); sha = run_paired.hashed(phases)
    manifest['bindings'][str(phases)] = sha
    for stage in manifest['stages']: stage['command'][6] = sha
    with pytest.raises(Refused, match='not hash-bound'):
        run_paired.run(manifest, tmp_path / 'run')
    assert not (tmp_path / 'run').exists()


@pytest.mark.skipif(os.name != 'posix', reason='actual SIGKILL controller-loss probe')
def test_actual_coordinator_death_leaves_recoverable_original_completion(tmp_path):
    manifest, _ = quality_fixture(tmp_path)
    output = tmp_path / 'run'; path = tmp_path / 'manifest.json'; path.write_text(json.dumps(manifest))
    launcher = tmp_path / 'controller.py'
    launcher.write_text('import json,os,signal,sys\nfrom pathlib import Path\n'
        f'sys.path.insert(0,{str(Path(run_paired.__file__).parent)!r})\nimport run_paired\n'
        'original=run_paired.run_process\n'
        'def lose(*args,**kwargs):\n original(*args,**kwargs)\n os.kill(os.getpid(),signal.SIGKILL)\n'
        'run_paired.run_process=lose\n'
        f'run_paired.run(json.loads(Path({str(path)!r}).read_text()),Path({str(output)!r}))\n')
    result = subprocess.run([sys.executable, '-I', str(launcher)], capture_output=True, timeout=30)
    assert result.returncode == -signal.SIGKILL, result.stderr.decode()
    assert rows(output)[0][3:5] == ('running', None)
    calls = rows(output, 'calls')
    run_paired.reconcile_completed_stage(manifest, output, 0)
    assert rows(output)[0][3:5] == ('reconciled_complete', None)
    assert rows(output, 'calls') == calls


def test_reconciliation_crash_after_immutable_proof_reuses_same_evidence(tmp_path, monkeypatch):
    manifest, output, _ = lose_ack(tmp_path, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(run_paired, 'verify_reconciled_completion',
                      lambda *args: (_ for _ in ()).throw(RuntimeError('settlement crash')))
        with pytest.raises(RuntimeError, match='settlement crash'):
            run_paired.reconcile_completed_stage(manifest, output, 0)
    assert rows(output)[0][3:5] == ('running', None)
    evidence = output / 'reconciled-complete-0.json'; before = evidence.read_bytes()
    calls = rows(output, 'calls')
    run_paired.reconcile_completed_stage(manifest, output, 0)
    assert evidence.read_bytes() == before and rows(output, 'calls') == calls
    assert rows(output)[0][3:5] == ('reconciled_complete', None)


def test_outer_output_cannot_be_reacknowledged_after_reconciliation(tmp_path, monkeypatch):
    manifest, output, _ = lose_ack(tmp_path, monkeypatch)
    run_paired.reconcile_completed_stage(manifest, output, 0)
    directory = output / f'00-{manifest["stages"][0]["arm"]}'
    (directory / 'stdout.txt').write_text('forged completion')
    original = rows(output)
    with pytest.raises(Refused, match='evidence changed'):
        run_paired.reconcile_completed_stage(manifest, output, 0)
    assert rows(output) == original and len(rows(output, 'calls')) == 1
