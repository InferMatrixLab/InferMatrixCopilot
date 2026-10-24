"""Trusted local deployment of evaluated artifacts, with automatic rollback.

Active bytes execute only in the existing credential-free sandbox. The host
keeps publishing/knowledge writes and the model broker. A PR is optional audit.
"""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import time
import uuid

from . import artifacts, objectives
from .cycle import ledger_dir_for, governor_for
from .isolation import Sandbox, SandboxUnavailable
from ..trace_store import bind_store, trace_context, file_lock
from ..git_objects import tree_entries


def registry(settings):
    return ledger_dir_for(settings) / 'evolution' / 'active.json'


def active(settings):
    path = registry(settings)
    return json.loads(path.read_text()) if path.exists() else {}


def baseline(settings, workflow, dest):
    current = active(settings)
    if current.get('artifact'):
        source = Path(current['artifact'])
        meta = artifacts.verify(source)
        if meta['revision'] != artifacts.git(Path(settings.improve_evolve_source_dir or artifacts.source_root()), 'rev-parse', 'HEAD'):
            raise artifacts.ArtifactError('active release belongs to an outdated source baseline')
        if dest.exists():
            if artifacts.verify(dest)['tree_sha'] != meta['tree_sha']: raise artifacts.ArtifactError('active baseline changed while candidate was generated')
        else: shutil.copytree(source, dest)
        return meta
    return artifacts.baseline(Path(settings.improve_evolve_source_dir or artifacts.source_root()), dest)


def promote(settings, store, c, *, sandbox=None):
    with file_lock(registry(settings).with_name('execution.lock'), blocking=True) as held:
        if not held: raise SandboxUnavailable('release execution is busy')
        return _promote(settings, store, c, sandbox=sandbox)


def _promote(settings, store, c, *, sandbox=None):
    from .evolution import directory, save
    if not settings.improve_enabled or not settings.improve_evolve_enabled or not objectives.enabled(settings) or settings.improve_promotion_mode != 'automatic':
        return c
    if c['workflow'] not in ('pr-review.agent.review_diff', 'kb-intake.draft', objectives.ENGINE):
        raise artifacts.ArtifactError('workflow lacks a sandboxed production driver')
    status = (sandbox or Sandbox()).check()
    if not status['ready']:
        c.update(state='deferred', reason='automatic deployment requires isolation: ' + status['reason']); save(settings, c, store); return c
    work = directory(settings) / 'candidates' / c['id']
    if artifacts.git(Path(settings.improve_evolve_source_dir or artifacts.source_root()), 'rev-parse', 'HEAD') != c['base_revision']:
        raise artifacts.ArtifactError('source baseline advanced before activation')
    proof = json.loads((work / 'objective.json').read_text())
    verdict = proof.get('result', {})
    if verdict != c.get('evaluation') or verdict.get('protocol') != objectives.PROTOCOL or not verdict.get('promotable'):
        raise artifacts.ArtifactError('automatic promotion requires the persisted controller verdict')
    arm, incumbent = artifacts.verify(work / 'arm'), artifacts.verify(work / 'incumbent')
    if arm['tree_sha'] != c['artifact']['tree_sha'] or proof.get('sources') != {'arm': arm['tree_sha'], 'incumbent': incumbent['tree_sha']}:
        raise artifacts.ArtifactError('promotion source differs from evaluated bytes')
    if arm['dependency_sha'] != artifacts.dependency_hash() or c.get('verified', {}).get('rc') != 0:
        raise artifacts.ArtifactError('promotion dependency/regression verification failed')
    if artifacts.digest((work / 'candidate.patch').read_bytes()) != c['patch_sha']:
        raise artifacts.ArtifactError('evaluated patch changed')
    # Reapply in a separate tree. A forged arm manifest cannot expand the
    # mutation scope or modify the protected evaluator/runtime.
    from .enroll import declarations_for
    decl = declarations_for(settings)[c['workflow']]
    with file_lock(registry(settings).with_suffix('.lock'), blocking=True) as held:
        if not held: raise SandboxUnavailable('deployment registry locked')
        current = active(settings)
        if current.get('candidate') == c['id']:
            c.update(state='canary', activated_at=current['at']); save(settings, c, store); return c
        expected = artifacts.verify(Path(current['artifact']))['tree_sha'] if current.get('artifact') else incumbent['tree_sha']
        if incumbent['tree_sha'] != expected: raise artifacts.ArtifactError('deployment baseline advanced')
        check_path = work / ('reapply-' + uuid.uuid4().hex)
        try:
            reconstructed = artifacts.apply_candidate(work / 'incumbent', check_path, c['generated'], decl.evolution, workflow=c['workflow'])
            if reconstructed['tree_sha'] != arm['tree_sha']: raise artifacts.ArtifactError('release mutation proof mismatch')
        finally: shutil.rmtree(check_path, ignore_errors=True)
        release = ledger_dir_for(settings) / 'evolution' / 'releases' / arm['tree_sha']
        if not release.exists():
            staged = release.with_name('.' + release.name + '-' + uuid.uuid4().hex)
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(work / 'arm', staged)
            artifacts.verify(staged); staged.replace(release)
        artifacts.verify(release)
        previous = current if current else {'artifact': str(work / 'incumbent'), 'source_sha': incumbent['tree_sha'], 'workflows': [], 'candidate': None}
        entry = {'candidate': c['id'], 'artifact': str(release), 'source_sha': arm['tree_sha'], 'at': time.time(),
                 'workflows': sorted(set(previous.get('workflows', [])) | set(c.get('affected_workflows', [])) | {c['workflow']}),
                 'previous': previous, 'evaluation_sha': artifacts.digest(json.dumps(verdict, sort_keys=True).encode())}
        # Persist lineage before activation; recovery accepts exactly this
        # candidate pointer and never deploys an unevaluated replacement.
        c.update(state='canary', previous_release=previous, activated_at=entry['at'], release=str(release), incumbent_sha=incumbent['tree_sha'])
        save(settings, c, store)
        artifacts.atomic_json(registry(settings), entry)
        store.append('decision', result={'type': 'automatic_activation', 'candidate': c['id'], 'source_sha': arm['tree_sha']})
    return c


def rollback(settings, store, c, reason):
    from .evolution import save
    with file_lock(registry(settings).with_suffix('.lock'), blocking=True) as held:
        if not held: raise SandboxUnavailable('rollback registry locked')
        current = active(settings)
        if current.get('candidate') != c['id']:
            c.update(state='rollback-blocked', reason='active release advanced; refusing to overwrite its lineage')
            save(settings, c, store); return
        try:
            previous = current['previous']
            if artifacts.verify(Path(previous['artifact']))['tree_sha'] != previous['source_sha']:
                raise artifacts.ArtifactError('previous release fingerprint mismatch')
            c['rollback_pending'] = reason
            save(settings, c, store)
            artifacts.atomic_json(registry(settings), previous)
            c.pop('rollback_pending', None)
            c.update(state='rolled-back', reason=reason, rolled_back_at=time.time())
            from .ledger import Ledger
            ledger = Ledger(ledger_dir_for(settings))
            with ledger.locked(c['workflow']):
                row = ledger.load(c['workflow'])
                if row.baseline.get('deployed_source', {}).get('candidate') == c['id']:
                    row.baseline.pop('deployed_source', None)
                ledger.save(row)
        except (ValueError, OSError, KeyError) as exc:
            # Disable the affected executable release if exact restoration is
            # unavailable. Subsequent invocations block instead of running it.
            artifacts.atomic_json(registry(settings), {**current, 'disabled': True, 'reason': str(exc)})
            c.update(state='rollback-blocked', reason=str(exc))
        save(settings, c, store)
        store.append('decision', result={'type': 'automatic_rollback', 'candidate': c['id'], 'state': c['state'], 'reason': reason})


def execute(settings, store, workflow, data, llm, *, sandbox=None, driver_override=None):
    if not settings.improve_enabled or not settings.improve_evolve_enabled: return None
    with file_lock(registry(settings).with_name('execution.lock'), blocking=True) as held:
        if not held: raise SandboxUnavailable('release execution is busy')
        return _execute(settings, store, workflow, data, llm, sandbox=sandbox, driver_override=driver_override)


def _execute(settings, store, workflow, data, llm, *, sandbox=None, driver_override=None):
    """Production prediction. Host stamps metrics/bytes and validates outputs."""
    from .evolution import candidate, save, directory, public_settings
    entry = active(settings)
    if not entry or workflow not in entry.get('workflows', []): return None
    if entry.get('disabled'): raise SandboxUnavailable('active runtime disabled: ' + entry.get('reason', ''))
    c = candidate(settings, entry['candidate'])
    sandbox = sandbox or Sandbox()
    root = Path(entry['artifact'])
    try:
        if artifacts.verify(root)['tree_sha'] != entry['source_sha']: raise artifacts.ArtifactError('active release changed')
        st = objectives.settings_for_source(objectives.execution_settings(settings), root, workflow)
        from .enroll import declarations_for
        decl = declarations_for(st)[workflow]
        driver = driver_override or objectives.DRIVERS.get(decl.experiment_driver)
        if driver == 'objective-tests': raise artifacts.ArtifactError('workflow lacks a sandboxed production driver')
        before, started = governor_for(st, ledger_dir_for(st)).remaining()['usd_settled'], time.monotonic()
        context = {'workflow': workflow, 'unit_id': 'live-evolve:' + uuid.uuid4().hex}
        with bind_store(store), trace_context(**context):
            result = sandbox.run(root, {'mode': 'predict', 'driver': driver, 'capture_errors': True, 'input': data, 'settings': public_settings(st), 'trace_context': context},
                                 directory(st) / 'production' / context['unit_id'], llm=llm, settings=st, governor=governor_for(st, ledger_dir_for(st)))
        if result.get('source_sha') != entry['source_sha']: raise artifacts.ArtifactError('production worker fingerprint mismatch')
        if result.get('execution_error'): raise artifacts.ArtifactError(result['execution_error'])
        paired = None
        item = str(data.get('repo', '') + '#' + str(data.get('pr', data.get('event_id', '')))) if decl.experiment_driver != 'meta' else str(data['records'][0].get('context', {}).get('unit_id', ''))
        eligible = workflow == c['workflow'] and (workflow != objectives.ENGINE or driver == ('objective-lints' if set(c['artifact']['paths']) == {'src/infermatrix_copilot/improve/lints.py'} else 'objective-meta'))
        # During canary, compare the actual predecessor on the same frozen
        # input. Both calls share the existing budget; no evaluator model.
        # Continue checking fresh production items after retention, bounded
        # to eight pairs per week and the same shared budget.
        from .budget import iso_week
        week = iso_week(time.time())
        observe = c['state'] == 'canary' or (c['state'] == 'retained' and c.get('monitor_week') != week)
        if observe and c['state'] == 'retained':
            c.update(monitor_week=week, monitor_items=[], monitor_records=[])
        observe = c['state'] == 'canary' or (c['state'] == 'retained' and len(c.get('monitor_items', [])) < 8)
        observed_items = c.get('canary_items', []) if c['state'] == 'canary' else c.get('monitor_items', [])
        if observe and eligible and item not in observed_items:
            previous = entry['previous']
            previous_settings = objectives.settings_for_source(objectives.execution_settings(settings), previous['artifact'], workflow)
            previous_started = time.monotonic()
            settled = governor_for(st, ledger_dir_for(st)).remaining()['usd_settled']
            reference = sandbox.run(Path(previous['artifact']), {'mode': 'predict', 'driver': driver, 'capture_errors': True, 'input': data,
                                    'settings': public_settings(objectives.settings_for_source(objectives.execution_settings(settings), previous['artifact'], workflow)), 'trace_context': {**context, 'unit_id': 'exp-canary:' + context['unit_id']}},
                                   directory(st) / 'production' / (context['unit_id'] + '-previous'), llm=llm, settings=previous_settings, governor=governor_for(st, ledger_dir_for(st)))
            if reference.get('source_sha') != previous['source_sha']: raise artifacts.ArtifactError('canary predecessor changed')
            if decl.experiment_driver in ('pr-review', 'kb-intake') and objectives.canonical(decl.experiment_driver, reference) != objectives.canonical(decl.experiment_driver, result):
                raise artifacts.ArtifactError('canary output differs from predecessor; semantic preservation unverified')
            arm_usd = settled - before
            old_usd = governor_for(st, ledger_dir_for(st)).remaining()['usd_settled'] - settled
            paired = {'item': item, 'arm_usd': arm_usd, 'previous_usd': old_usd,
                      'arm_seconds': previous_started - started, 'previous_seconds': time.monotonic() - previous_started,
                      'previous_calls': reference['_broker_calls']}
            c.setdefault('canary_items' if c['state'] == 'canary' else 'monitor_items', []).append(item)
        if driver == 'objective-meta':
            ids = {r['id'] for r in data['records']}
            found = result.get('attributions', [])
            if len(found) != len(data['cells']) or {a.get('gold_id') for a in found} != set(data['cells']) or any(a.get('stage') not in ('S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8', 'S9', 'S10') or not set(a.get('evidence', [])).issubset(ids) for a in found):
                raise artifacts.ArtifactError('diagnostic output violates the evidence contract')
        if decl.experiment_driver in ('pr-review', 'kb-intake'):
            # Empty KB drafts may be correct on production inputs. Promotion
            # never receives a yield reward for fabricating rules.
            valid = objectives.score(decl.experiment_driver, {'payload': data}, result)
            if decl.experiment_driver == 'kb-intake' and result.get('operations') == [] and not result.get('rejected'): valid = 1
            if not valid: raise artifacts.ArtifactError('production output violates executable contract')
        metrics = {'type': 'runtime_result', 'candidate': c['id'], 'source_sha': entry['source_sha'], 'contract_success': 1,
                   'broker_calls': result['_broker_calls'], 'usd': governor_for(st, ledger_dir_for(st)).remaining()['usd_settled'] - before,
                   'seconds': time.monotonic() - started, 'pair': paired, 'driver': driver}
        record = store.append('decision', context=context, result=metrics)
        if c['state'] in ('canary', 'retained') and eligible:
            if c['state'] == 'canary': c.setdefault('production_records', []).append(record['id'])
            elif paired: c.setdefault('monitor_records', []).append(record['id'])
            c['deployed_at'] = c.get('deployed_at', record['at']); c['deployed_record'] = record['id']
            # Actual sandbox execution, rather than activation/PR merge,
            # attests the deployed baseline.
            from .ledger import Ledger
            ledger = Ledger(ledger_dir_for(st))
            with ledger.locked(workflow):
                row = ledger.load(workflow)
                row.baseline['deployed_source'] = {'candidate': c['id'], 'tree_sha': entry['source_sha'], 'record': record['id'], 'at': record['at']}
                ledger.save(row)
            save(st, c, store)
        return result
    except Exception as exc:
        # Budget/isolation/validation failures are visible and restore the
        # previous pointer; this call blocks instead of replaying a paid call.
        rollback(settings, store, c, f'{type(exc).__name__}: {exc}')
        raise


def sync(settings, store, *, now=None):
    with file_lock(registry(settings).with_name('execution.lock'), blocking=True) as held:
        if not held: return
        return _sync(settings, store, now=now)


def _sync(settings, store, *, now=None):
    from .evolution import candidates, save
    now = time.time() if now is None else now
    for c in candidates(settings):
        if c['state'] not in ('canary', 'retained'): continue
        entry = active(settings)
        if c.get('rollback_pending') and entry.get('source_sha') == c.get('previous_release', {}).get('source_sha'):
            c.update(state='rolled-back', reason=c.pop('rollback_pending'), rolled_back_at=now)
            save(settings, c, store); continue
        if entry.get('candidate') != c['id']:
            # Recover a crash after saving activation intent but before the
            # registry pointer. Revalidation precedes any later activation.
            if c['state'] == 'canary' and not c.get('deployed_record') and (not entry or entry.get('source_sha') == c.get('incumbent_sha')):
                c.update(state='pr-ready', reason='activation interrupted; exact release will be revalidated')
                save(settings, c, store)
            continue
        if now - c['activated_at'] >= 7 * 86400 and c['state'] == 'canary':
            rollback(settings, store, c, 'canary lacks verified production evidence after seven days'); continue
        records = [store.get(rid) for rid in c.get('monitor_records' if c['state'] == 'retained' else 'production_records', [])]
        verified = [r for r in records if r.get('result', {}).get('source_sha') == entry['source_sha'] and r['result'].get('contract_success') == 1]
        c['observation'] = {'state': 'waiting-for-production-samples', 'verified_units': len(verified), 'semantic_quality_claim': False}
        if len(verified) >= 8:
            from . import stats
            pairs = [r['result']['pair'] for r in verified if r['result'].get('pair')]
            deltas = {}
            for pair in pairs:
                a, b = pair['arm_usd'], pair['previous_usd']
                if b <= 0: a, b = pair['arm_seconds'], pair['previous_seconds']
                deltas.setdefault(pair['item'], []).append((b - a) / max(b, 1e-9))
            measured = stats.paired(deltas, 'production_resource_gain')
            c['observation']['resource_gain'] = {'mean': measured.mean, 'lo': measured.lo, 'hi': measured.hi, 'n_items': measured.n_items}
            if measured.n_items >= 8 and measured.hi < 0:
                rollback(settings, store, c, 'statistically significant production resource regression'); continue
            if measured.n_items >= 8:
                c.update(state='retained', reason='', retained_at=now)
                c['observation']['state'] = 'production-contracts-verified'
        save(settings, c, store)


def freeze_review(settings, state):
    """Freeze local Git objects and knowledge; never read PR checkout symlinks."""
    import re
    import subprocess
    repository = Path(state['repo_path'])
    revisions = [state['pr_base_sha'], state['pr_head_sha']]
    if any(not re.fullmatch(r'[a-fA-F0-9]{40}', revision) for revision in revisions): raise artifacts.ArtifactError('review replay needs fixed base/head SHAs')
    trees, total = [], 0
    for revision in revisions:
        listing = subprocess.check_output(['git', '-C', str(repository), 'ls-tree', '-rz', '--full-tree', revision], timeout=60)
        files = {}
        for mode, kind, oid, name in tree_entries(listing):
            name = name.decode()
            # Refuse credential/config payloads and binary/oversize material
            # rather than silently replaying a different repository.
            if mode == '120000' or kind != 'blob': raise artifacts.ArtifactError('review snapshot contains non-regular source')
            from .drivers import input_path
            input_path(name)
            if any(part == '.env' or part.startswith('.env.') for part in Path(name).parts): raise artifacts.ArtifactError('review snapshot contains credential files')
            size = int(subprocess.check_output(['git', '-C', str(repository), 'cat-file', '-s', oid], timeout=20))
            total += size
            if size > 1_000_000 or total > 12_000_000: raise artifacts.ArtifactError('review snapshot exceeds replay bound')
            files[name] = subprocess.check_output(['git', '-C', str(repository), 'cat-file', 'blob', oid], timeout=20).decode('utf-8')
        trees.append(files)
    knowledge = {}
    kroot = Path(settings.knowledge_dir).resolve()
    if kroot.is_dir():
        for p in sorted(kroot.rglob('*.md')):
            if p.is_symlink() or not p.resolve().is_relative_to(kroot): raise artifacts.ArtifactError('knowledge snapshot path escaped')
            total += p.stat().st_size
            if total > 16_000_000: raise artifacts.ArtifactError('knowledge snapshot exceeds replay bound')
            knowledge[p.relative_to(kroot).as_posix()] = p.read_text()
    spec = state['task_spec']
    return {'repo': spec['repo'], 'pr': spec['pr'], 'base_sha': revisions[0], 'head_sha': revisions[1],
            'base_files': trees[0], 'head_files': trees[1], 'diff': state['diff_text'], 'knowledge_files': knowledge,
            'context_text': state.get('pr_context', ''), 'gate_report': state.get('gate_report', ''),
            'task_params': spec.get('params', {}), 'pr_state': state.get('pr_state', '')}


async def review_step(ctx):
    """Use an active sandbox release for the read-only review step."""
    from ..engine.step import StepResult, FailureKind
    from ..trace_store import current_store
    store = current_store()
    if not store: return None
    workflow = 'pr-review.agent.review_diff'
    entry = active(ctx.settings)
    is_active = workflow in entry.get('workflows', [])
    try:
        data = freeze_review(ctx.settings, ctx.state)
        item = f"{data['repo']}#{data['pr']}@{data['head_sha']}"
        objectives.capture(store, workflow, item, data)
    except (ValueError, OSError, KeyError) as exc:
        store.append('decision', result={'type': 'replay_capture_missing', 'workflow': workflow, 'reason': str(exc)})
        if is_active:
            from .evolution import candidate
            rollback(ctx.settings, store, candidate(ctx.settings, entry['candidate']), 'review replay capture failed: ' + str(exc))
            return StepResult(False, FailureKind.BLOCKED, 'active review lacks a safe frozen replay input')
        return None
    if not is_active: return None
    import asyncio
    try:
        prediction = await asyncio.to_thread(execute, ctx.settings, store, workflow, data, ctx.llm)
    except Exception as exc:
        return StepResult(False, FailureKind.BLOCKED, 'autonomous review rolled back: ' + str(exc))
    if prediction is None: return None
    outputs = {k: prediction.get(k) for k in ('review_text', 'review_comments', 'review_summary', 'review_verdict',
                                             'review_finding_dispositions', 'review_carried_findings', 'review_finding_rechecks', 'review_recheck_missing')}
    ctx.state.update(outputs)
    return StepResult(True, summary='review produced by verified autonomous source', outputs={**outputs, 'state_updates': outputs})


def run_lints(unit, store, baseline):
    """Production lint code is loaded from the active release in a sandbox."""
    from .lints import run_lints as original, Finding, catalogue
    settings = baseline.settings
    entry = active(settings) if settings else {}
    workflow = objectives.ENGINE
    if not settings or workflow not in entry.get('workflows', []): return original(unit, store, baseline)
    from .evolution import candidate
    c = candidate(settings, entry['candidate'])
    try:
        blobs = {ref: store.blob(ref) for r in unit.records for ref in [*r.get('inputs', {}).values(), *r.get('outputs', {}).values()]}
        data = {'records': unit.records, 'blobs': blobs, 'cells': [], 'concerns': [], 'baseline': {'usd': baseline.usd, 'seconds': baseline.seconds, 'parse_failure_rate': baseline.parse_failure_rate}}
        output = execute(settings, store, workflow, data, None, driver_override='objective-lints')
        known, ids = {r['id'] for r in catalogue()}, {r['id'] for r in unit.records}
        findings = [Finding(**f) for f in output['findings']]
        if any(f.lint not in known or f.unit_id != unit.unit_id or f.workflow != unit.workflow or not f.evidence or not set(f.evidence).issubset(ids) for f in findings):
            raise artifacts.ArtifactError('active lint returned invalid evidence')
        expected = {(f.lint, tuple(f.evidence)) for f in original(unit, store, baseline)}
        if not expected.issubset({(f.lint, tuple(f.evidence)) for f in findings}): raise artifacts.ArtifactError('active lint lost an existing positive')
        return findings
    except Exception as exc:
        if active(settings).get('candidate') == c['id']: rollback(settings, store, c, 'production lint failed: ' + str(exc))
        # Diagnostics continue using the exact preceding host code; generated
        # code is never executed outside the sandbox.
        store.append('decision', result={'type': 'runtime_lint_restored', 'reason': str(exc)})
        return original(unit, store, baseline)


def diagnose(settings, store, llm=None):
    """One-model hypotheses on observed mechanical defects, without gold labels."""
    if objectives.ENGINE not in active(settings).get('workflows', []): return {'state': 'no-active-diagnostic-release'}
    from .reader import units_between
    from .lints import run_lints as original, Baseline
    from ..llm import LLM
    report = []
    for unit in units_between(store, time.time() - 7 * 86400, time.time()).values():
        if len(report) >= 3: break
        if unit.playbook == 'workflow-improve' or unit.unit_id.startswith(('exp-', 'live-evolve:')): continue
        findings = original(unit, store, Baseline(settings=settings))
        if not findings: continue
        try:
            blobs = {ref: store.blob(ref) for r in unit.records for ref in [*r.get('inputs', {}).values(), *r.get('outputs', {}).values()]}
        except (ValueError, OSError) as exc:
            store.append('decision', result={'type': 'diagnosis_input_missing', 'unit': unit.unit_id, 'reason': str(exc)})
            continue
        concerns = [{'gold_id': 'observed-' + str(i), 'path': 'runtime', 'concern': f'{f.lint}: {f.detail}'} for i, f in enumerate(findings[:3])]
        data = {'records': unit.records, 'blobs': blobs, 'concerns': concerns, 'cells': [g['gold_id'] for g in concerns]}
        result = execute(settings, store, objectives.ENGINE, data, llm or LLM(objectives.execution_settings(settings)))
        store.append('decision', context={'workflow': objectives.ENGINE}, result={'type': 'objective_diagnosis', 'source_unit': unit.unit_id,
                     'hypotheses': result['attributions'], 'semantic_quality_claim': False})
        report.append(unit.unit_id)
    return {'state': 'diagnosed', 'units': report, 'semantic_quality_claim': False}
