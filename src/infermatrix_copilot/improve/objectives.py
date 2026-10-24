"""Trusted executable objectives. No human labels, model judges or candidate scores.

Fault witnesses stay in the parent. Tests exercise bounded contracts, never claim
semantic recall on arbitrary PRs or correctness of newly generated knowledge.
"""
from __future__ import annotations
from dataclasses import asdict
import copy
import json
from pathlib import Path
import time
import uuid

from . import artifacts, drivers, stats
from .budget import BudgetRefused
from .cycle import governor_for, ledger_dir_for
from .enroll import declarations_for
from .isolation import Sandbox, SandboxUnavailable
from ..trace_store import file_lock, bind_store, trace_context

PROTOCOL = 'objective/1'
ENGINE = 'workflow-improve.improve.forensics'
DRIVERS = {'pr-review': 'pr-review', 'kb-intake': 'kb-intake', 'meta': 'objective-meta', 'mechanical': 'objective-tests'}
FAULTS = {'ceiling': ('S4', 'L01'), 'discard': ('S3', 'L09'), 'fallback': ('S7', 'L07')}
# Complete KB trees and companion texts already exceed 16 MB. TraceStore
# compresses these envelopes; retain a finite uncompressed memory/disk bound.
REPLAY_INPUT_MAX_BYTES = 64_000_000


class ReplayCaptureLimit(artifacts.ArtifactError):
    """A complete replay exceeds the storage bound, before candidate execution."""
    def __init__(self, size_bytes, limit_bytes):
        self.size_bytes, self.limit_bytes = size_bytes, limit_bytes
        super().__init__(f'replay input exceeds capture limit ({size_bytes} > {limit_bytes} bytes)')


def enabled(settings):
    return getattr(settings, 'improve_evaluation_mode', 'gold') == 'objective'


def execution_settings(settings):
    """One API route for generation, inference and diagnosis; no stronger tier."""
    from .experiments import settings_for
    model = settings.improve_evolve_model or settings.tier_target('eco').model
    patched, _ = settings_for(settings, {'ECO_MODEL': model, 'PERFORMANCE_MODEL': model, 'AGENT_MODEL': model})
    return patched.model_copy(update={'review_planner_model': model, 'review_promotion_model': model,
                                     'review_lens_backends': {}, 'llm_mixture': {}})


def data_root(settings):
    return Path(settings.improve_evolve_data_dir).expanduser() if settings.improve_evolve_data_dir else ledger_dir_for(settings) / 'evolution' / 'inputs'


def rows(settings, workflow):
    return drivers.dataset(settings.model_copy(update={'improve_evolve_data_dir': str(data_root(settings))}), workflow)


def fault_case(seed: str, fault: str):
    """Known intervention and a clean control; only the intervention's input travels."""
    uid, rid, gid = 'unit-' + seed, 'event-' + seed, 'concern-' + seed
    reply = json.dumps({'summary': 'completed', 'review_comments': []})
    ref = 'sha256:' + artifacts.digest(reply.encode())
    ctx = {'workflow': ENGINE, 'playbook': 'workflow-improve', 'unit_id': uid, 'item': seed, 'fingerprint': seed}
    model = {'id': rid, 'kind': 'model_call', 'schema': 'trace/1', 'at': 1, 'context': ctx,
             'seconds': 1, 'model': {'model': 'fixed', 'served_model': 'fixed'},
             'usage': {'input_tokens': 20, 'output_tokens': 20}, 'inputs': {}, 'outputs': {'reply': ref},
             'result': {'stop_reason': 'end_turn', 'max_tokens': 100}}
    terminal = {'id': 'terminal-' + seed, 'kind': 'decision', 'schema': 'trace/1', 'at': 2,
                'context': ctx, 'inputs': {}, 'outputs': {}, 'result': {'type': 'step_result', 'status': 'ok'}}
    clean = {'records': [model, terminal], 'blobs': {ref: reply}, 'concerns': [], 'cells': []}
    mutated = copy.deepcopy(clean)
    if fault == 'ceiling':
        mutated['records'][0]['result'].update(stop_reason='max_tokens', max_tokens=20)
        evidence_id = rid
    elif fault == 'discard':
        mutated['records'][1]['result'] = {'type': 'cap', 'dropped': 1 + int(seed[:2], 16) % 12, 'cap': 3}
        evidence_id = terminal['id']
    elif fault == 'fallback':
        mutated['records'][1]['result'] = {'type': 'planner_zero_output', 'reason': 'injected planner failure'}
        evidence_id = terminal['id']
    else:
        raise artifacts.ArtifactError('unknown controlled fault')
    mutated.update(concerns=[{'gold_id': gid, 'path': 'runtime', 'concern': 'The unit failed to retain a complete executable result.'}], cells=[gid])
    stage, lint = FAULTS[fault]
    return mutated, clean, {'cell': gid, 'stage': stage, 'lint': lint, 'evidence': evidence_id}


def capture(store, workflow, item, payload):
    """Record a replay envelope in a trusted host, before invoking candidate code."""
    if store is None: return
    forbidden = {'labels', 'gold', 'scores', 'judgments', 'report'}
    if forbidden & set(payload): raise artifacts.ArtifactError('replay input includes evaluator material')
    raw = json.dumps(payload, ensure_ascii=False)
    encoded = raw.encode()
    context = {'workflow': workflow, 'item': item}
    input_sha = artifacts.digest(encoded)
    if len(encoded) > REPLAY_INPUT_MAX_BYTES:
        # A missing replay is explicit and never eligible for collection. Do
        # not truncate files or references that affect the executable contract.
        store.append('decision', context=context, result={'type': 'replay_capture_deferred', 'reason': 'size_limit',
                     'input_bytes': len(encoded), 'limit_bytes': REPLAY_INPUT_MAX_BYTES, 'input_sha': input_sha})
        raise ReplayCaptureLimit(len(encoded), REPLAY_INPUT_MAX_BYTES)
    store.append('decision', context=context,
                 inputs={'evolution_input': raw}, result={'type': 'replay_input', 'input_sha': input_sha})


def collect(settings, store, workflow):
    """Automatically import immutable trace inputs; never guess missing snapshots."""
    root = data_root(settings) / workflow
    with file_lock(root / 'collection.lock', blocking=True) as held:
        if not held: return 0
        path = root / 'dataset.json'
        doc = json.loads(path.read_text()) if path.exists() else {'workflow': workflow, 'items': []}
        known = {r['item'] for r in doc['items']}
        added = 0
        for r in store.query(workflow=workflow, limit=10000):
            if r.get('result', {}).get('type') != 'replay_input': continue
            ref = r.get('inputs', {}).get('evolution_input')
            if not ref: continue
            payload = json.loads(store.blob(ref))
            source_id = str(r['id'])
            # Cluster all captures of one business item together. A retry or a
            # changed snapshot must not become an independent holdout item.
            item = str(r.get('context', {}).get('item') or source_id)
            if item in known: continue
            name = artifacts.digest(item.encode()) + '.json'
            artifacts.atomic_json(root / name, payload)
            doc['items'].append({'item': item, 'split': 'development' if int(artifacts.digest(item.encode())[:8], 16) % 5 == 0 else 'holdout',
                                 'input': name, 'sha256': artifacts.digest((root / name).read_bytes()),
                                 'origin': {'record': source_id, 'kind': 'trace-replay'}, 'objective': {}})
            known.add(item); added += 1
        artifacts.atomic_json(path, doc)
        return added


def prepare(settings, store, *, workflow='all', repo=''):
    """Generate controlled engine cases and collect available business snapshots."""
    report = {}
    for name, decl in declarations_for(settings).items():
        if workflow != 'all' and name != workflow: continue
        if not decl.evolution: continue
        if name == ENGINE:
            root = data_root(settings) / name
            path = root / 'dataset.json'
            with file_lock(root / 'collection.lock', blocking=True) as held:
                if not held: continue
                doc = json.loads(path.read_text()) if path.exists() else {'workflow': name, 'items': []}
                # A fresh, bounded pool for each candidate. Labels are causal
                # witnesses of the controller's intervention, never model text.
                used_path = ledger_dir_for(settings) / 'evolution' / 'holdouts.json'
                used = json.loads(used_path.read_text()) if used_path.exists() else {}
                fresh = [r for r in doc['items'] if r['split'] == 'holdout' and not any(k.startswith(name + ':' + r['item'] + ':') for k in used)]
                floor = max([80, *[trial.get('n_required', 0) for cpath in (ledger_dir_for(settings) / 'evolution' / 'candidates').glob('*/candidate.json')
                                      for trial in json.loads(cpath.read_text()).get('objective_trials', [])
                                      if json.loads(cpath.read_text()).get('workflow') == name]])
                if len(fresh) < floor:
                    # Grow only the controller's scenarios; business inputs
                    # must arrive from real traces. Budget still gates calls.
                    for i in range(max(100, floor - len(fresh) + 10)):
                        seed = uuid.uuid4().hex
                        fault = tuple(FAULTS)[i % len(FAULTS)]
                        payload, clean, witness = fault_case(seed, fault)
                        if i % 4 == 0:
                            payload, witness = clean, {'negative': True}
                        artifacts.atomic_json(root / (seed + '.json'), payload)
                        doc['items'].append({'item': 'controlled:' + seed, 'split': 'development' if i < 10 else 'holdout',
                                             'input': seed + '.json', 'sha256': artifacts.digest((root / (seed + '.json')).read_bytes()),
                                             'origin': {'kind': 'controlled-intervention', 'version': 1}, 'objective': witness})
                artifacts.atomic_json(path, doc)
                report[name] = {'items': len(doc['items']), 'source': 'controlled-interventions'}
                store.append('decision', context={'workflow': name}, result={'type': 'objective_inputs', 'items': len(doc['items'])})
        else:
            report[name] = {'imported': collect(settings, store, name), 'source': 'trace-replay'}
    return report


def policy(decl, mechanism=''):
    supplied = decl.raw.get('objective') or {}
    goal = 'resource_gain' if mechanism in ('L11', 'L15', 'efficiency') else supplied.get('metric', 'contract_success')
    return {'metric': goal, 'min_effect': float(supplied.get('min_effect', .05)), 'sd': float(supplied.get('prior_sd', .15)),
            'guards': {'contract_success': 'higher'}, 'replicates': 3}


def check(settings, store, workflow, *, repo='', sandbox=None):
    decl = declarations_for(settings).get(workflow)
    if not decl or not decl.evolution:
        return {'workflow': workflow, 'ready': False, 'reasons': ['no-objective-mutation-policy'], 'evaluation_mode': 'objective'}
    reasons = []
    st = None
    try:
        st = execution_settings(settings)
        if st.tier_target('eco').kind != 'api': reasons.append('objective-mode-requires-one-API-route')
        if settings.review_lens_backends or settings.llm_mixture: reasons.append('mixed-review-routes-not-supported-by-objective-proxy')
    except (ValueError, RuntimeError): reasons.append('fixed-model-unconfigured')
    data = [r for r in rows(settings, workflow) if not repo or r['item'].split('#')[0] == repo]
    required_keys = {'pr-review': {'repo', 'pr', 'base_sha', 'head_sha', 'base_files', 'head_files', 'diff', 'knowledge_files'},
                     'kb-intake': {'repo', 'repo_dir', 'generator_model', 'evidence', 'files', 'release', 'today'},
                     'meta': {'records', 'blobs', 'concerns', 'cells'}}.get(decl.experiment_driver, set())
    if any(required_keys - set(r['payload']) for r in data): reasons.append('incomplete-driver-snapshot')
    if st and decl.experiment_driver == 'kb-intake' and any(r['payload'].get('generator_model') != st.tier_target('eco').model for r in data):
        reasons.append('pinned-generator-model-does-not-match-broker')
    dev, hold = [r for r in data if r['split'] == 'development'], [r for r in data if r['split'] == 'holdout']
    used_path = ledger_dir_for(settings) / 'evolution' / 'holdouts.json'
    used = json.loads(used_path.read_text()) if used_path.exists() else {}
    hold = [r for r in hold if f"{workflow}:{r['item']}:{r['version']}" not in used]
    p = policy(decl)
    required = stats.items_required(p['sd'], p['min_effect'])
    if not dev: reasons.append('missing-replay-development-inputs')
    if len(hold) < required: reasons.append(f'insufficient-fresh-objective-items: {len(hold)} < {required}')
    driver = DRIVERS.get(decl.experiment_driver)
    if driver == 'objective-tests':
        reasons = [r for r in reasons if not r.startswith('insufficient-fresh-objective-items') and r != 'missing-replay-development-inputs']
        required = 0
        if settings.improve_promotion_mode == 'automatic': reasons.append('missing-sandboxed-production-driver')
    if not driver: reasons.append('missing-objective-driver')
    source = Path(settings.improve_evolve_source_dir or artifacts.source_root())
    try:
        revision = artifacts.git(source, 'rev-parse', 'HEAD')
        if artifacts.git(source, 'status', '--porcelain', '--untracked-files=no', '--', 'src', 'playbooks', 'adapters', 'skills'): reasons.append('uncommitted-source-baseline')
    except artifacts.ArtifactError:
        revision = ''; reasons.append('source-baseline-not-a-repository')
    for test in decl.evolution['tests']:
        if not (source / test).is_file(): reasons.append('missing-regression-test:' + test)
    isolation = (sandbox or Sandbox()).check()
    if not isolation['ready']: reasons.append('sandbox-unavailable:' + isolation['reason'])
    budget = governor_for(settings, ledger_dir_for(settings)).remaining()
    if budget['usd_remaining'] <= 0: reasons.append('budget-exhausted')
    if not store or not store.query(workflow=workflow, limit=1): reasons.append('missing-workflow-traces')
    return {'workflow': workflow, 'tier': 1, 'evaluation_mode': 'objective', 'ready': not reasons,
            'reasons': reasons, 'driver': driver, 'source_revision': revision, 'development_items': len(dev),
            'holdout_items': len(hold), 'n_required': required, 'sandbox': isolation, 'budget': budget,
            'semantic_quality_claim': False, 'evolution': decl.evolution}


def canonical(driver, output):
    if driver == 'pr-review': return {k: output.get(k, []) for k in ('review_text', 'review_summary', 'review_comments', 'review_verdict', 'review_finding_dispositions', 'review_carried_findings', 'review_finding_rechecks', 'review_recheck_missing')}
    if driver == 'kb-intake': return output.get('operations', [])
    return output.get('attributions', [])


def score(driver, row, output, *, lint=False):
    """All scores are computed here; candidate numeric fields are ignored."""
    if driver == 'meta':
        expected = row.get('objective') or {}
        if row.get('origin', {}).get('kind') != 'controlled-intervention': raise artifacts.ArtifactError('missing causal witness')
        if lint:
            found = output.get('findings')
            if not isinstance(found, list): return 0.0
            if expected.get('negative'): return float(not found)
            ids = {r['id'] for r in row['payload']['records']}
            return float(any(f.get('lint') == expected['lint'] and expected['evidence'] in f.get('evidence', []) for f in found)
                         and all(f.get('evidence') and set(f['evidence']).issubset(ids) and f.get('lint') != 'L00' for f in found))
        found = output.get('attributions')
        if not isinstance(found, list): return 0.0
        if expected.get('negative'): return float(not found)
        matches = [a for a in found if a.get('gold_id') == expected['cell']]
        ids = {r['id'] for r in row['payload']['records']}
        return float(len(found) == len(row['payload']['cells']) and len(matches) == 1 and matches[0].get('stage') == expected['stage'] and not matches[0].get('disputed')
                     and expected['evidence'] in matches[0].get('evidence', []) and set(matches[0]['evidence']).issubset(ids))
    if driver == 'pr-review':
        from ..review.severity import STRICT_TO_PRIORITY
        data = row['payload']; findings = output.get('review_comments')
        if not isinstance(findings, list) or not isinstance(output.get('review_text'), str) or not output['review_text'].strip(): return 0.0
        for f in findings:
            path, line = f.get('file', f.get('path')), f.get('line')
            if path not in data['head_files'] or not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= len(data['head_files'][path].splitlines()): return 0.0
            severity = f.get('severity')
            if not isinstance(severity, str) or severity not in STRICT_TO_PRIORITY or not f.get('comment') or f.get('disposition', 'publish') != 'publish': return 0.0
            anchor = f.get('anchor_snippet')
            if anchor and anchor not in data['head_files'][path]: return 0.0
        return 1.0
    if driver == 'kb-intake':
        from ..knowledge_service.ops import KnowledgeOperation, apply_operations
        data = row['payload']
        raw = output.get('operations')
        if not isinstance(raw, list) or len(raw) > 6 or output.get('rejected'): return 0.0
        try:
            operations = [KnowledgeOperation.from_dict(o) for o in raw]
            if any(o.allow_protected or any(not artifacts.safe_path(p).startswith(data['repo_dir'].rstrip('/') + '/') for p in (o.page, o.new_page) if p) for o in operations): return 0.0
            apply_operations(data['files'], operations, release=data['release'], today=data['today'])
        except (ValueError, KeyError, TypeError): return 0.0
        return 1.0
    raise artifacts.ArtifactError('unknown executable objective')


def evaluate(settings, store, c, sandbox, llm):
    """Pre-register once, checkpoint each paid call, paired item-level inference."""
    from .evolution import directory
    decl = declarations_for(settings)[c['workflow']]
    work = directory(settings) / 'candidates' / c['id']
    path = work / 'objective.json'
    governor = governor_for(settings, ledger_dir_for(settings))
    st = execution_settings(settings)
    if any(k in c['generated'].get('overrides', {}) for k in ('ECO_MODEL', 'PERFORMANCE_MODEL', 'AGENT_MODEL')):
        raise artifacts.ArtifactError('objective candidates cannot change the pinned model')
    from .evolution import public_settings
    source = Path(settings.improve_evolve_source_dir or artifacts.source_root())
    if decl.experiment_driver == 'mechanical':
        # Existing trusted tests are an automatic executable reproducer. A
        # passing incumbent cannot establish a defect repair on this path.
        runs = {}
        for side in ('incumbent', 'arm'):
            runs[side] = sandbox.run(work / side, {'mode': 'tests', 'tests': decl.evolution['tests'], 'settings': public_settings(st)},
                                     work / 'objective-tests' / side, tests=source / 'test')
        verdict = {'protocol': PROTOCOL, 'label': 'supported' if runs['incumbent'].get('rc') == 1 and runs['arm'].get('rc') == 0 else 'neutral',
                'promotable': runs['incumbent'].get('rc') == 1 and runs['arm'].get('rc') == 0,
                'kind': 'existing-contract-repair', 'semantic_quality_claim': False, 'reproducer': runs}
        artifacts.atomic_json(path, {'sources': {side: artifacts.verify(work / side)['tree_sha'] for side in ('incumbent', 'arm')}, 'result': verdict})
        return verdict
    p = policy(decl, c['mechanism'])
    all_rows = {r['item']: r for r in rows(settings, c['workflow'])}
    previous_required = max([0, *[t['n_required'] for t in c.get('objective_trials', [])]])
    if path.exists() and json.loads(path.read_text()).get('result', {}).get('blocking') == 'data':
        old = json.loads(path.read_text())
        previous_required = old['result']['n_required']
        c.setdefault('objective_trials', []).append({'path': str(path.with_name('objective-' + str(len(c.get('objective_trials', []))) + '.json')), 'n_required': previous_required})
        path.replace(Path(c['objective_trials'][-1]['path']))
        from .evolution import save
        save(settings, c, store)
    if path.exists():
        exp = json.loads(path.read_text())
        # Frozen registry + source + execution settings are never refreshed
        # under the same paid experiment, even after a process restart.
        if exp['policy'] != p or exp['settings_sha'] != artifacts.digest(json.dumps(public_settings(st), sort_keys=True).encode()):
            raise artifacts.ArtifactError('objective registration settings changed')
    else:
        required = max(previous_required, stats.items_required(p['sd'], p['min_effect']))
        fresh = [r for r in all_rows.values() if r['split'] == 'holdout' and (not c.get('repo') or r['item'].split('#')[0] == c['repo'])]
        used_path = directory(settings) / 'holdouts.json'
        with file_lock(directory(settings) / 'holdouts.lock', blocking=True) as held:
            if not held: raise SandboxUnavailable('holdout registry locked')
            used = json.loads(used_path.read_text()) if used_path.exists() else {}
            fresh = [r for r in fresh if f"{c['workflow']}:{r['item']}:{r['version']}" not in used]
            selected = sorted(fresh, key=lambda r: r['item'])[:required]
            if len(selected) < required: raise SandboxUnavailable('insufficient fresh objective scenarios')
            exp = {'protocol': PROTOCOL, 'id': c['id'] + '-objective-' + uuid.uuid4().hex[:8], 'state': 'registered', 'policy': p, 'n_required': required,
                   'items': {r['item']: r['version'] for r in selected}, 'sources': {side: artifacts.verify(work / side)['tree_sha'] for side in ('incumbent', 'arm')},
                   'settings_sha': artifacts.digest(json.dumps(public_settings(st), sort_keys=True).encode()), 'progress': {}, 'at': time.time()}
            # Registration is durable before consuming holdouts. Resuming also
            # verifies/repairs this index, so a crash between writes is safe.
            artifacts.atomic_json(path, exp)
            for r in selected: used[f"{c['workflow']}:{r['item']}:{r['version']}"] = exp['id']
            artifacts.atomic_json(used_path, used)
    for item, version in exp['items'].items():
        if item not in all_rows or all_rows[item]['version'] != version: raise artifacts.ArtifactError('objective snapshot changed')
    with file_lock(directory(settings) / 'holdouts.lock', blocking=True) as held:
        if not held: raise SandboxUnavailable('holdout registry locked')
        used_path = directory(settings) / 'holdouts.json'
        used = json.loads(used_path.read_text()) if used_path.exists() else {}
        for item, version in exp['items'].items():
            key = f"{c['workflow']}:{item}:{version}"
            if key in used and used[key] != exp['id']: raise artifacts.ArtifactError('objective holdout reused')
            used[key] = exp['id']
        artifacts.atomic_json(used_path, used)
    for side in ('incumbent', 'arm'):
        if artifacts.verify(work / side)['tree_sha'] != exp['sources'][side]: raise artifacts.ArtifactError('objective source changed')
    if exp.get('result'): return exp['result']
    lint = decl.experiment_driver == 'meta' and set(c['artifact']['paths']) == {'src/infermatrix_copilot/improve/lints.py'}
    driver = 'objective-lints' if lint else DRIVERS[decl.experiment_driver]
    for item in exp['items']:
        for rep in range(3):
            key = item + '/' + str(rep)
            sample = exp['progress'].get(key)
            if sample:
                if sample['state'] == 'running':
                    exp['progress'][key] = {'state': 'excluded', 'reason': 'interrupted paid call; no replay'}
                    artifacts.atomic_json(path, exp)
                continue
            before = governor.remaining()['usd_remaining']
            if before <= 0: raise BudgetRefused('objective experiment budget exhausted')
            exp['progress'][key] = {'state': 'running'}; exp['state'] = 'running'; artifacts.atomic_json(path, exp)
            sample = {'state': 'scored', 'item': item, 'outputs': {}, 'scores': {}, 'seconds': {}, 'usd': {}, 'calls': {}}
            order = ('arm', 'incumbent') if (int(artifacts.digest(item.encode())[:8], 16) + rep) % 2 else ('incumbent', 'arm')
            for side in order:
                started, settled = time.monotonic(), governor.remaining()['usd_settled']
                arm_settings = settings_for_source(st, work / side, c['workflow'])
                context = {'workflow': c['workflow'], 'unit_id': f"exp-objective:{c['id']}:{key}:{side}", 'item': item}
                with bind_store(store), trace_context(**context):
                    output = sandbox.run(work / side, {'mode': 'predict', 'driver': driver, 'capture_errors': True, 'input': all_rows[item]['payload'],
                                          'settings': public_settings(arm_settings), 'trace_context': context},
                                         work / 'objective-runs' / artifacts.digest(key.encode())[:16] / side,
                                         llm=llm, settings=arm_settings, governor=governor)
                if output.get('source_sha') != exp['sources'][side]: raise artifacts.ArtifactError('objective worker fingerprint mismatch')
                sample['seconds'][side] = time.monotonic() - started
                sample['usd'][side] = governor.remaining()['usd_settled'] - settled
                sample['calls'][side] = output.get('_broker_calls', 0)
                sample['outputs'][side] = canonical(decl.experiment_driver, output)
                sample['scores'][side] = score(decl.experiment_driver, all_rows[item], output, lint=lint)
            # A resource optimization cannot shrink artifacts to buy a score.
            # For semantic tasks require exact baseline finding/operation sets.
            equivalent = decl.experiment_driver == 'meta' or sample['outputs']['arm'] == sample['outputs']['incumbent']
            sample['no_regression'] = sample['scores']['arm'] >= sample['scores']['incumbent'] and (equivalent or not sample['scores']['incumbent'])
            a, b = sample['usd']['arm'], sample['usd']['incumbent']
            if b <= 0: a, b = sample['seconds']['arm'], sample['seconds']['incumbent']
            sample['resource_gain'] = (b - a) / max(b, 1e-9)
            sample['outputs'] = {side: artifacts.digest(json.dumps(value, sort_keys=True).encode()) for side, value in sample['outputs'].items()}
            exp['progress'][key] = sample; artifacts.atomic_json(path, exp)
    complete = [item for item in exp['items'] if all(exp['progress'][item + '/' + str(r)]['state'] == 'scored' for r in range(3))]
    deltas = {item: [exp['progress'][item + '/' + str(rep)]['resource_gain'] if p['metric'] == 'resource_gain' else
                     exp['progress'][item + '/' + str(rep)]['scores']['arm'] - exp['progress'][item + '/' + str(rep)]['scores']['incumbent'] for rep in range(3)] for item in complete}
    result = stats.paired(deltas, p['metric'])
    required = max(exp['n_required'], stats.items_required(result.sd, p['min_effect']))
    label = stats.label(result, n_required=required)
    guard = all(s.get('no_regression') for s in exp['progress'].values() if s['state'] == 'scored')
    verdict = {'protocol': PROTOCOL, **asdict(result), 'label': label, 'metric': p['metric'], 'min_effect': p['min_effect'],
               'n_required': required, 'n_registered': len(exp['items']), 'replicates': 3, 'snapshot_versions': exp['items'],
               'no_regression': guard, 'promotable': label == 'supported' and result.mean >= p['min_effect'] and guard,
               'sources': exp['sources'], 'semantic_quality_claim': False,
               'scope': 'controlled-interventions' if decl.experiment_driver == 'meta' else 'replay-contracts-and-output-preservation'}
    if len(complete) < required: verdict['blocking'] = 'data'
    exp['state'] = label; exp['result'] = verdict; artifacts.atomic_json(path, exp)
    store.append('decision', result={'type': 'objective_verdict', 'candidate': c['id'], **verdict})
    return verdict


def worker(payload, settings, llm):
    """Protected driver called by the trusted launcher, not a mutable score function."""
    from .meta import _unit_from
    from ..trace_store import TraceStore
    data = payload['input']
    store = TraceStore(Path('/tmp/objective-traces'))
    for ref, content in data['blobs'].items():
        if store.put_blob(content) != ref: raise artifacts.ArtifactError('objective blob mismatch')
    unit = _unit_from(data['records'])
    if payload['driver'] == 'objective-lints':
        from .lints import run_lints, Baseline
        return {'findings': [asdict(f) for f in run_lints(unit, store, Baseline(settings=settings, **data.get('baseline', {})))]}
    from .forensics import attribute, Cell
    from .adapters import Gold, GoldEntry
    from ..agent_loop import run_agent
    from ..run_trace import RunTrace
    target = settings.tier_target('eco')
    def agent(system, prompt, scope, extra_tools, max_iters):
        return run_agent(llm, system=system, prompt=prompt, scope=scope, extra_tools=extra_tools,
                         max_iters=max_iters, model=target.model, trace=RunTrace(Path('/tmp/objective-agent.jsonl'))).text
    concerns = Gold(unit.item, tuple(GoldEntry(**g) for g in data['concerns']))
    attributed = attribute(store, {unit.unit_id: unit}, {unit.item: concerns},
                           [Cell(g, unit.unit_id, 'miss') for g in data['cells']], agents={'fixed-model': agent}, max_cells=len(data['cells']))
    return {'attributions': [asdict(a) for a in attributed]}


def settings_for_source(settings, source, workflow):
    st = artifacts.runtime_settings(settings.model_copy(update={'playbooks_dir': Path(source) / 'playbooks'}), workflow)
    if st.tier_target('eco').model != settings.tier_target('eco').model or st.tier_target('performance').model != settings.tier_target('eco').model:
        raise artifacts.ArtifactError('adopted source requests a different objective model')
    return st
