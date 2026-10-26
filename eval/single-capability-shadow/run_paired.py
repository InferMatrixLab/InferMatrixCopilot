"""Run a frozen paired shadow plan with durable stage admission.

No plan is inferred here. Callers must freeze commands, inputs, profile, case
allowances and arm order first. An interrupted stage needs explicit reconciliation;
restarting this coordinator never launches that stage again automatically.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import shlex
import sqlite3
import time

from infermatrix_copilot.persistence import immutable_write_bytes
from infermatrix_copilot.process_execution import run_process

from ledger import Ledger, Refused, plan_call_limit
from stage_selection import select_stages


def verify_native(output, plan, stage):
    path = output / 'calls.sqlite'
    if not path.exists():
        raise Refused('stage returned without a native receipt')
    ledger = Ledger(path, plan)
    try:
        if ledger.db.execute("SELECT 1 FROM calls WHERE status!='complete'").fetchone():
            raise Refused('stage returned with an unresolved native call')
        rows = ledger.db.execute('SELECT result,result_digest FROM calls WHERE case_id=? AND arm=?',
                                 (stage['case_id'],stage['arm'])).fetchall()
        if not rows:
            raise Refused('stage returned without its own native receipt')
        for result, expected in rows:
            if hashlib.sha256(result.encode()).hexdigest() != expected or json.loads(result).get('native_complete') is not True:
                raise Refused('native receipt changed or does not prove completion')
    finally:
        ledger.close()


def hashed(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def terminal_failure(receipt):
    """A local exit alone cannot settle an uncertain provider response."""
    from infermatrix_copilot.providers.codex import CodexTransport
    return receipt.get('native_complete') is False and CodexTransport.terminal_failure(receipt)


def workflow_failure_proof(output, stage, position, *, allow_captured=False):
    """Bind a normal exit; captured native failure may explicitly allow zero."""
    directory = Path(output) / f'{position:02d}-{stage["arm"]}'
    process_path = directory / 'stage-process.json'
    try:
        process = json.loads(process_path.read_text())
        with sqlite3.connect('file:' + str(Path(output) / 'stages.sqlite') + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT returncode FROM stages WHERE position=?', (position,)).fetchone()
        code = process.get('returncode')
        if (type(code) is not int or code < (0 if allow_captured else 1)
                or (allow_captured and 'reason' not in process) or process.get('reason') is not None
                or row != (code,)):
            raise ValueError('stage did not exit normally with its recorded failure')
        return {name: hashed(directory / name) for name in
                ('stage-process.json', 'stdout.txt', 'stderr.txt')}
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise Refused('workflow failure needs its original terminal process and output proof') from exc


def verify_reconciled_failure(output, plan, stage, position):
    evidence = json.loads((output / f'reconciled-failure-{position}.json').read_text())
    if evidence['plan_digest'] != digest(plan) or evidence['stage'] != stage:
        raise Refused('failure reconciliation belongs to another frozen stage')
    ledger = Ledger(output / 'calls.sqlite', plan)
    try:
        if ledger.db.execute("SELECT 1 FROM calls WHERE status!='complete'").fetchone():
            raise Refused('failure reconciliation left an unresolved call')
        rows = ledger.db.execute('SELECT id,result,result_digest FROM calls WHERE case_id=? AND arm=?',
                                 (stage['case_id'], stage['arm'])).fetchall()
        actual = {call_id: expected for call_id, _, expected in rows}
        if not rows or actual != evidence['receipts']:
            raise Refused('reconciled receipt set changed')
        failures = 0
        for _, raw, expected in rows:
            if hashlib.sha256(raw.encode()).hexdigest() != expected:
                raise Refused('reconciled receipt changed')
            receipt = json.loads(raw)
            if not receipt.get('native_complete'):
                if not terminal_failure(receipt):
                    raise Refused('reconciliation does not prove a terminal failure')
                failures += 1
        if not failures:
            if (evidence.get('outcome') != 'workflow_failure'
                    or evidence.get('workflow_proof') != workflow_failure_proof(output, stage, position)):
                raise Refused('no failed call or verified workflow failure in reconciled stage')
        elif evidence.get('outcome') != 'terminal_failure':
            raise Refused('native failure cannot be relabeled as a workflow failure')
        with sqlite3.connect('file:' + str(Path(output) / 'stages.sqlite') + '?mode=ro', uri=True) as stages:
            captured = stages.execute('SELECT returncode FROM stages WHERE position=?', (position,)).fetchone() == (0,)
        if captured and (not failures or evidence.get('captured_stage_proof') !=
                workflow_failure_proof(output, stage, position, allow_captured=True)):
            raise Refused('captured stage needs its original process and terminal native failure proof')
    finally:
        ledger.close()


def reconcile_failed_stage(manifest, output, position):
    """Explicit operator action; never dispatches or substitutes a new call."""
    validate(manifest, for_dispatch=False)
    output = Path(output).resolve()
    if type(position) is not int or not 0 <= position < len(manifest['stages']):
        raise Refused('reconciliation position is outside the frozen plan')
    stage = manifest['stages'][position]
    directory = output / f'{position:02d}-{stage["arm"]}'
    db = sqlite3.connect(output / 'stages.sqlite', isolation_level=None)
    try:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT digest FROM plan').fetchone() != (digest(manifest),):
            raise Refused('original coordinator manifest changed')
        row = db.execute('SELECT status,returncode FROM stages WHERE position=?', (position,)).fetchone()
        if not row or row[0] != 'needs_reconciliation' or type(row[1]) is not int or row[1] < 0:
            raise Refused('only an exited failed stage may be reconciled')
        captured = row[1] == 0
        capture_proof = None
        if captured:
            if not (output / 'calls.sqlite').is_file():
                raise Refused('only an exited failed stage or a proven captured native failure may be reconciled')
            capture_proof = workflow_failure_proof(output, stage, position, allow_captured=True)
        ledger = Ledger(output / 'calls.sqlite', manifest['plan'])
        try:
            calls = ledger.db.execute('SELECT id,request_digest,status,result,result_digest FROM calls WHERE case_id=? AND arm=?',
                                      (stage['case_id'], stage['arm'])).fetchall()
            prepared = []
            try:
                if not calls:
                    raise Refused('failed stage has no native evidence')
                # Validate every receipt before settling any reserved row.
                for call_id, request, status, raw, expected in calls:
                    if status == 'complete':
                        if hashlib.sha256(raw.encode()).hexdigest() != expected:
                            raise Refused('completed native receipt changed')
                        receipt = json.loads(raw)
                        if not receipt.get('native_complete') and not terminal_failure(receipt):
                            raise Refused('completed row does not prove a terminal native outcome')
                    else:
                        receipt = json.loads((output / f'{call_id}.partial.json').read_text())
                        if receipt.get('request_sha256') != request or not terminal_failure(receipt):
                            raise Refused('partial output does not prove this call failed terminally')
                    prepared.append((call_id, status, receipt))
                if captured and not any(terminal_failure(receipt) for _, _, receipt in prepared):
                    raise Refused('captured stage has no terminal native failure')
            except (Refused, OSError, ValueError, TypeError) as exc:
                if captured:
                    raise Refused('only an exited failed stage or a proven captured native failure may be reconciled') from exc
                raise
            for call_id, status, receipt in prepared:
                if status != 'complete':
                    ledger.complete(call_id, receipt)
            receipts = dict(ledger.db.execute('SELECT id,result_digest FROM calls WHERE case_id=? AND arm=?',
                                             (stage['case_id'], stage['arm'])).fetchall())
            workflow_proof = workflow_failure_proof(output, stage, position) if all(
                receipt.get('native_complete') is True for _, _, receipt in prepared) else None
        finally:
            ledger.close()
        evidence = {'plan_digest':digest(manifest['plan']), 'stage':stage, 'receipts':receipts,
                    'outcome':'terminal_failure', 'rerun_permitted':False,
                    'controller_sha256':hashed(__file__)}
        if workflow_proof is not None:
            evidence.update(outcome='workflow_failure', workflow_proof=workflow_proof)
        if capture_proof is not None:
            evidence['captured_stage_proof'] = capture_proof
        path = output / f'reconciled-failure-{position}.json'
        if path.exists() and json.loads(path.read_text()) != evidence:
            raise Refused('original reconciliation evidence changed')
        if not path.exists():
            with path.open('x') as stream:
                json.dump(evidence, stream, indent=2)
        verify_reconciled_failure(output, manifest['plan'], stage, position)
        db.execute("UPDATE stages SET status='reconciled_failed',stdout_sha256=?,stderr_sha256=? WHERE position=?",
                   (hashed(directory/'stdout.txt'), hashed(directory/'stderr.txt'), position))
        db.execute('COMMIT')
    finally:
        if db.in_transaction:
            db.execute('ROLLBACK')
        db.close()


def _completion_proof(output, manifest, stage, position, returncode):
    directory = Path(output) / f'{position:02d}-{stage["arm"]}'
    process = directory / 'stage-process.json'
    if process.exists():
        value = json.loads(process.read_text())
        if (not isinstance(value, dict) or value != {'returncode': 0, 'reason': None}
                or type(value.get('returncode')) is not int or returncode not in (None, 0)):
            raise Refused('original stage did not exit normally')
        return {'source': 'stage_process', 'receipt_sha256': hashed(process)}
    if returncode is not None:
        raise Refused('recorded stage exit has no original process receipt')
    from stage_completion import quality_completion_proof
    return quality_completion_proof(output, manifest, stage, position)


def _completed_stage_calls(output, manifest, stage):
    verify_native(output, manifest['plan'], stage)
    with sqlite3.connect((Path(output) / 'calls.sqlite').as_uri() + '?mode=ro', uri=True) as calls:
        return dict(calls.execute('SELECT id,result_digest FROM calls WHERE case_id=? AND arm=?',
                                  (stage['case_id'], stage['arm'])).fetchall())


def _completion_evidence(output, manifest, stage, position, returncode):
    directory = Path(output) / f'{position:02d}-{stage["arm"]}'
    return {'schema': 'reconciled-stage-completion/1', 'manifest_digest': digest(manifest),
        'stage': stage, 'original_returncode': returncode,
        'completion_proof': _completion_proof(output, manifest, stage, position, returncode),
        'native_receipts': _completed_stage_calls(output, manifest, stage),
        'controller_sha256': hashed(__file__), 'rerun_permitted': False,
        'outputs': {name: hashed(directory / name) for name in ('stdout.txt', 'stderr.txt')}}


def verify_reconciled_completion(output, manifest, stage, position):
    """Recheck original evidence; completion never implies quality acceptance."""
    output = Path(output)
    try:
        evidence = json.loads((output / f'reconciled-complete-{position}.json').read_text())
        with sqlite3.connect((output / 'stages.sqlite').as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT case_id,arm,returncode FROM stages WHERE position=?', (position,)).fetchone()
        if not row or row[:2] != (stage['case_id'], stage['arm']):
            raise Refused('original completion stage identity changed')
        actual = _completion_evidence(output, manifest, stage, position, row[2])
        if evidence != actual:
            raise Refused('original stage completion evidence changed')
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        raise Refused('missing or invalid original stage completion evidence') from exc


def reconcile_completed_stage(manifest, output, position):
    """Explicitly settle verified original completion, without running a command.

    Unknown outer exit codes remain NULL even when a new quality-workflow
    receipt proves its frozen application phases completed successfully.
    """
    from stage_completion import execution_lock
    validate(manifest, for_dispatch=False)
    if type(position) is not int or not 0 <= position < len(manifest['stages']):
        raise Refused('reconciliation position is outside the frozen plan')
    output = Path(output).resolve(strict=True)
    stage = manifest['stages'][position]
    directory = output / f'{position:02d}-{stage["arm"]}'
    with execution_lock(output / 'coordinator.lock'):
        db = sqlite3.connect((output / 'stages.sqlite').as_uri() + '?mode=rw', uri=True, isolation_level=None)
        try:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT digest FROM plan').fetchone() != (digest(manifest),):
                raise Refused('original coordinator manifest changed')
            row = db.execute('SELECT case_id,arm,status,returncode FROM stages WHERE position=?', (position,)).fetchone()
            if (not row or row[:2] != (stage['case_id'], stage['arm'])
                    or row[2] not in ('running', 'needs_reconciliation', 'reconciled_complete')
                    or row[3] not in (None, 0)):
                raise Refused('only an original pending successful stage may be reconciled')
            evidence = _completion_evidence(output, manifest, stage, position, row[3])
            path = output / f'reconciled-complete-{position}.json'
            raw = json.dumps(evidence, sort_keys=True).encode()
            if path.exists():
                if path.read_bytes() != raw:
                    raise Refused('original stage completion evidence changed')
            else:
                immutable_write_bytes(path, raw)
            verify_reconciled_completion(output, manifest, stage, position)
            db.execute("UPDATE stages SET status='reconciled_complete',stdout_sha256=?,stderr_sha256=? WHERE position=?",
                       (hashed(directory / 'stdout.txt'), hashed(directory / 'stderr.txt'), position))
            db.execute('COMMIT')
        finally:
            if db.in_transaction:
                db.execute('ROLLBACK')
            db.close()


def uses_shared_init_driver(path):
    """Recognize the maintained thin init entries without executing them."""
    try:
        tree = ast.parse(Path(path).read_bytes(), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise Refused('stage driver is unavailable or invalid') from exc
    return any(
        isinstance(node, ast.ImportFrom) and node.module == 'init_native_arm'
        or isinstance(node, ast.Import) and any(alias.name == 'init_native_arm' for alias in node.names)
        for node in ast.walk(tree))


def validate(manifest, *, for_dispatch=True):
    select_stages(manifest)
    from quality_plan import validate_quality
    validate_quality(manifest, for_dispatch=for_dispatch)
    plan = manifest['plan']
    limits = plan['native_call_limits']
    if not limits or any(type(n) is not int or n < 1 for n in limits.values()):
        raise Refused('freeze positive per-case allowances')
    ceiling = plan_call_limit(plan)
    if 2 * sum(limits.values()) > ceiling:
        raise Refused(f'paired case allowances exceed the frozen {ceiling}-native-call ceiling')
    stages = manifest['stages']
    expected = {(case, arm) for case in limits for arm in ('baseline', 'candidate')}
    identities = [(stage['case_id'], stage['arm']) for stage in stages]
    if len(identities) != len(set(identities)) or set(identities) != expected:
        raise Refused('freeze exactly one stage per case and arm')
    if plan.get('execution_kind') != 'live' or not plan.get('execution_profile'):
        raise Refused('coordinator requires the explicit live admission profile')
    if for_dispatch:
        from shared_execution import validate_shared, plan_for_case
        for stage in stages:
            validate_shared(plan_for_case(plan, stage['case_id']), stage['arm'])
    if type(manifest.get('order_seed')) is not int:
        raise Refused('record the arm-order seed')
    rng = random.Random(manifest['order_seed'])
    order = []
    for case in sorted(limits):
        arms = ['baseline', 'candidate']
        rng.shuffle(arms)
        order.extend((case, arm) for arm in arms)
    if identities != order:
        raise Refused('stage order differs from the recorded seed')
    if manifest.get('publication') != 'disabled':
        raise Refused('shadow publication must be disabled')
    retention = plan.get('payload_retention')
    if retention is not None:
        from arm_runtime import validate_payload_retention
        root_value = retention.get('root') if isinstance(retention, dict) else None
        retention_root = Path(root_value) if isinstance(root_value, str) else Path('/')
        for stage in stages:
            validate_payload_retention({'plan': plan, 'case_id': stage['case_id'],
                'ledger_path': str(retention_root / 'calls.sqlite')},
                retention_root / 'preflight' / 'config.json')
    bindings = manifest['bindings']
    required = {manifest['binary'], manifest['guard_python'], manifest['guard_script'],
                str(Path(manifest['guard_script']).with_name('ledger.py'))}
    if manifest.get('stage_selection') is not None:
        required.add(str(Path(select_stages.__code__.co_filename).resolve()))
    if manifest.get('quality_evaluation') is not None:
        required.add(str(Path(validate_quality.__code__.co_filename).resolve()))
        required.add(str(Path(importlib.util.find_spec('init_native_arm').origin).resolve()))
    if manifest.get('arm_entry_script'):
        entry = Path(manifest['arm_entry_script'])
        required.update(str(entry.with_name(name)) for name in
                        ('arm_entry.py', 'arm_runtime.py', 'shared_execution.py', 'ledger.py'))
    if 'roles' in plan.get('execution_profile', {}):
        required.update(str(Path(manifest['guard_script']).with_name(name)) for name in
                        ('role_profiles.py', 'arm_runtime.py', 'shared_execution.py', 'ledger.py'))
    if 'legacy_provider_host' in plan:
        from legacy_provider_host import host_binding
        host = host_binding(plan)
        required.update((host['python'], host['script']))
        required.update(str(Path(host['script']).with_name(name)) for name in
                        ('arm_runtime.py', 'shared_execution.py', 'role_profiles.py', 'ledger.py'))
    mixed_binaries = None
    if plan.get('shared_execution', {}).get('schema') == 'shared-sandbox/2':
        from shared_execution import shared_profiles
        mixed_binaries = manifest.get('provider_binaries')
        if (not isinstance(mixed_binaries, dict) or set(mixed_binaries) != set(shared_profiles(plan))
                or any(not isinstance(path, str) or not Path(path).is_absolute() for path in mixed_binaries.values())
                or manifest['binary'] not in mixed_binaries.values()):
            raise Refused('freeze each mixed native executable in provider_binaries')
        required.update(mixed_binaries.values())
    if retention is not None:
        required.add(str(Path(validate_payload_retention.__code__.co_filename).resolve()))
        required.add(retention['input_manifest'])
    if plan.get('worker_execution') == 'durable':
        if plan.get('call_identity') != 'agent-step/1':
            raise Refused('freeze logical agent call identity in the evaluation plan')
        required.update(str(Path(importlib.util.find_spec(name.removesuffix('.py')).origin).resolve()) for name in
                        ('durable_worker.py', 'strict_host.py', 'arm_runtime.py',
                         'shared_execution.py', 'ledger.py'))
    for stage in stages:
        command = stage['command']
        if not isinstance(command, list) or len(command) < 3 or command[1] != '-I':
            raise Refused('stage must use an isolated installed interpreter and a bound driver')
        if not all(isinstance(arg, str) and arg for arg in command):
            raise Refused('stage command must be a nonempty argument vector')
        required.update((command[0], command[2]))
        if type(stage.get('timeout_s')) is not int or not 0 < stage['timeout_s'] <= 7200:
            raise Refused('freeze a bounded stage timeout including repository preparation')
        if for_dispatch and Path(command[2]).name == 'quality_stage_arm.py':
            from quality_stage_arm import load_phases
            try:
                phase_path = Path(command[command.index('--phases') + 1])
                phase_sha = command[command.index('--sha256') + 1]
            except (ValueError, IndexError) as exc:
                raise Refused('freeze the complete quality phase packet') from exc
            packet = load_phases(phase_path, phase_sha)
            if stage['timeout_s'] < sum(phase['timeout_s'] for phase in packet['phases']) + 30:
                raise Refused('outer stage deadline must cover all serial quality phases and final receipt')
            for phase in packet['phases']:
                if uses_shared_init_driver(phase['driver']):
                    from arm_runtime import _workflow_timeout, _host_timeout
                    timeout = _workflow_timeout(plan, case_id=stage['case_id'], require_explicit=True)
                    if phase['timeout_s'] < timeout + _host_timeout(plan, case_id=stage['case_id']):
                        raise Refused('init phase deadline must cover workflow wait and host preparation')
            if stage['case_id'] in {'direct', 'strict'}:
                from arm_runtime import _workflow_timeout, _host_timeout
                timeout = _workflow_timeout(plan, case_id=stage['case_id'], require_explicit=True)
                for phase in packet['phases']:
                    if (Path(phase['driver']).name in {'strict_review_case_arm.py', 'review_case_arm.py'}
                            and phase['timeout_s'] < timeout + _host_timeout(plan, case_id=stage['case_id'])):
                        raise Refused('review phase deadline must cover workflow wait and host preparation')
        # Standalone legacy drivers keep their original manifest shape. Thin
        # shared-init entries must bind their implementation before dispatch.
        uses_shared_init = uses_shared_init_driver(command[2])
        if uses_shared_init and for_dispatch and manifest.get('quality_evaluation') is not None:
            from arm_runtime import _workflow_timeout, _host_timeout
            timeout = _workflow_timeout(plan, case_id=stage['case_id'], require_explicit=True)
            if stage['timeout_s'] < timeout + _host_timeout(plan, case_id=stage['case_id']):
                raise Refused('init stage deadline must cover workflow wait and host preparation')
        if uses_shared_init:
            helper = Path(command[2]).with_name('init_native_arm.py')
            required.add(str(helper))
            if str(helper) in bindings:
                try:
                    implementation = ast.parse(helper.read_bytes(), filename=str(helper))
                except (OSError, SyntaxError, UnicodeError) as exc:
                    raise Refused('shared init implementation is unavailable or invalid') from exc
                if any(isinstance(node, ast.ImportFrom) and node.module == 'init_fixture_policies'
                       or isinstance(node, ast.Import) and any(alias.name == 'init_fixture_policies' for alias in node.names)
                       for node in ast.walk(implementation)):
                    required.add(str(helper.with_name('init_fixture_policies.py')))
        if set(stage.get('environment', {})) - {'KNOWLEDGE_ROOT'}:
            raise Refused('only the frozen knowledge root may extend stage environment')
    if not required.issubset(bindings):
        raise Refused('interpreter, provider, guard, ledger or driver is not hash-bound')
    for path, expected_hash in bindings.items():
        if not Path(path).is_absolute() or hashed(path) != expected_hash:
            raise Refused(f'frozen file changed: {path}')
    expected_binaries = ([(manifest['binary'], plan['provider_binary_sha256'])] if mixed_binaries is None else
                         [(path, plan['provider_binary_sha256_by_provider'][provider])
                          for provider, path in mixed_binaries.items()])
    if any(bindings[path] != sha for path, sha in expected_binaries):
        raise Refused('provider hash differs between bindings and native plan')


def run(manifest, output):
    validate(manifest, for_dispatch=not (Path(output) / 'stages.sqlite').exists())
    selected, retained = select_stages(manifest)
    output = Path(output).resolve()
    from quality_plan import validate_quality
    validate_quality(manifest, output=output, for_dispatch=False)
    retention = manifest['plan'].get('payload_retention')
    if retention is not None and Path(retention['root']) != output:
        raise Refused('payload-retention root differs from the original evaluation output')
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    from stage_completion import execution_lock
    with execution_lock(output / 'coordinator.lock'):
        return _run_locked(manifest, output, selected, retained)


def _run_locked(manifest, output, selected, retained):
    db = sqlite3.connect(output / 'stages.sqlite', isolation_level=None)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''CREATE TABLE IF NOT EXISTS plan (id INTEGER PRIMARY KEY CHECK(id=1), digest TEXT);
        CREATE TABLE IF NOT EXISTS stages (position INTEGER PRIMARY KEY, case_id TEXT, arm TEXT,
            status TEXT, returncode INTEGER, stdout_sha256 TEXT, stderr_sha256 TEXT);''')
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR IGNORE INTO plan VALUES(1,?)', (digest(manifest),))
        if db.execute('SELECT digest FROM plan').fetchone()[0] != digest(manifest):
            raise Refused('coordinator plan changed; retain the original execution environment')
        if any(position not in selected for (position,) in db.execute('SELECT position FROM stages')):
            raise Refused('stage ledger contains an unselected original intent; reconcile without dispatch')
        db.execute('COMMIT')
        saved = output / 'manifest.json'
        if saved.exists():
            if json.loads(saved.read_text()) != manifest:
                raise Refused('saved manifest changed')
        else:
            with saved.open('x') as stream:
                json.dump(manifest,stream,indent=2)
        for position, stage in enumerate(manifest['stages']):
            if position not in selected:
                continue
            directory = output / f'{position:02d}-{stage["arm"]}'
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT status,stdout_sha256,stderr_sha256 FROM stages WHERE position=?', (position,)).fetchone()
            if prior:
                if prior[0] not in {'complete', 'reconciled_failed', 'reconciled_complete'}:
                    raise Refused('original stage needs reconciliation; automatic redispatch is forbidden')
                if (hashed(directory / 'stdout.txt'), hashed(directory / 'stderr.txt')) != prior[1:]:
                    raise Refused('completed stage output changed')
                if prior[0] == 'reconciled_complete':
                    verify_reconciled_completion(output, manifest, stage, position)
                elif prior[0] == 'reconciled_failed':
                    verify_reconciled_failure(output, manifest['plan'], stage, position)
                else:
                    verify_native(output, manifest['plan'], stage)
                db.execute('COMMIT')
                continue
            if db.execute("SELECT 1 FROM stages WHERE status NOT IN ('complete','reconciled_failed','reconciled_complete')").fetchone():
                raise Refused('another original stage needs reconciliation')
            # Initial admission already checked all frozen bytes. Revalidate
            # before new dispatch, not once per historical receipt on resume.
            validate(manifest)
            directory.mkdir(mode=0o700)
            config = directory / 'guard-config.json'
            config.write_text(json.dumps({'plan':manifest['plan'], 'case_id':stage['case_id'],
                'arm':stage['arm'], 'binary':manifest['binary'], 'ledger_path':str(output/'calls.sqlite')}, indent=2))
            shim = directory / 'codex'
            shim.write_text('#!/bin/sh\nexec ' + shlex.join([manifest['guard_python'], manifest['guard_script'],
                '--config', str(config), '--']) + ' "$@"\n')
            shim.chmod(0o700)
            command = [str(shim) if arg == '{shim}' else str(directory) if arg == '{output}' else arg
                       for arg in stage['command']]
            db.execute('INSERT INTO stages VALUES(?,?,?,\'running\',NULL,NULL,NULL)',
                       (position,stage['case_id'],stage['arm']))
            db.execute('COMMIT')  # Durable intent before any stage/provider execution.
            env = {**os.environ, **stage.get('environment', {})}
            env.pop('PYTHONPATH', None)
            env['SHADOW_ARM_CONFIG'] = str(config)
            if manifest.get('arm_entry_script'):
                if len(command) < 3 or command[1] != '-I':
                    raise Refused('composed arm commands require an isolated Python interpreter')
                command = [command[0], '-I', '-B', manifest['arm_entry_script'], '--config', str(config), '--', *command[2:]]
            with (directory/'stdout.txt').open('wb') as stdout, (directory/'stderr.txt').open('wb') as stderr:
                try:
                    process = run_process(command, cwd=directory, env=env,
                        deadline=time.monotonic() + stage['timeout_s'], output='spool',
                        on_chunk=lambda channel, data: (stdout if channel == 'stdout' else stderr).write(data))
                except BaseException:
                    # Preserve intent and files. A durable child can outlive this host.
                    raise
            immutable_write_bytes(directory / 'stage-process.json', json.dumps({
                'returncode': process.returncode, 'reason': process.reason}, sort_keys=True).encode())
            # The child has exited, but its exit code does not settle native calls.
            # Keep that distinction durable even if receipt verification crashes.
            db.execute("UPDATE stages SET status='needs_reconciliation',returncode=? WHERE position=?",
                       (process.returncode,position))
            if process.returncode or process.reason is not None:
                raise Refused('stage failed; inspect native ledger and durable run before reconciliation')
            verify_native(output, manifest['plan'], stage)
            db.execute("UPDATE stages SET status='complete',returncode=0,stdout_sha256=?,stderr_sha256=? WHERE position=?",
                       (hashed(directory/'stdout.txt'),hashed(directory/'stderr.txt'),position))
        failures = db.execute("SELECT count(*) FROM stages WHERE status='reconciled_failed'").fetchone()[0]
        result = {'status':'executed_with_failures' if failures else 'executed', 'failed_stages':failures,
                  'stages':len(selected), 'adjudication':'pending', 'publication':'disabled'}
        if retained:
            result.update(status='executed_partial_with_failures' if failures else 'executed_partial',
                          planned_stages=len(manifest['stages']), selected_stage_positions=list(selected),
                          retained_stages=retained, full_quality_acceptance=False)
        quality = manifest.get('quality_evaluation', {})
        if quality.get('schema') == 'native-quality/2':
            result['external_cases'] = quality['external_cases']
            result['full_quality_acceptance'] = False
        return result
    finally:
        if db.in_transaction:
            db.execute('ROLLBACK')
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--validate-only', action='store_true')
    reconciliation = parser.add_mutually_exclusive_group()
    reconciliation.add_argument('--reconcile-failed-stage', type=int)
    reconciliation.add_argument('--reconcile-completed-stage', type=int)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if args.reconcile_failed_stage is not None:
        reconcile_failed_stage(manifest, args.output, args.reconcile_failed_stage)
        print(json.dumps({'status':'reconciled_failed', 'position':args.reconcile_failed_stage, 'dispatched':False}))
    elif args.reconcile_completed_stage is not None:
        reconcile_completed_stage(manifest, args.output, args.reconcile_completed_stage)
        print(json.dumps({'status':'reconciled_complete', 'position':args.reconcile_completed_stage, 'dispatched':False}))
    elif args.validate_only:
        validate(manifest)
        print(json.dumps({'status':'validated', 'dispatched':False}))
    else:
        print(json.dumps(run(manifest,args.output)))


if __name__ == '__main__':
    main()
