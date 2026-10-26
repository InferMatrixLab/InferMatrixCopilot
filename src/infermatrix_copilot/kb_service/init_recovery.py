"""Offline reconciliation of original init accounting facts, without dispatch."""
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace

from ..budgeting import normalized_cost

from ..trace_store import TraceStore
from .init_support import INIT_DIR, STAGES, INDEPENDENT_STAGES, InitRecord, checkpoint_budget
from .repo_spec import REPO_ID


def _unique_reservation(trace, records):
    identity = trace.get('context', {}).get('init_budget', {}).get('reservation_id')
    if not identity or sum(entry.get('context', {}).get('init_budget', {}).get('reservation_id') == identity
                           for entry in records) != 1:
        raise ValueError('reservation has missing or ambiguous native outcomes')


@contextmanager
def _original_call(state_dir, *, repo, stage, trace_id, offline_confirmed=False, outcome="complete"):
    if not REPO_ID.fullmatch(repo) or stage not in STAGES + INDEPENDENT_STAGES:
        raise ValueError('invalid init account identity')
    state_dir = Path(state_dir)
    traces = TraceStore(state_dir / INIT_DIR / 'traces')
    with traces._migration_lock():
        traces._require_paused(None, offline_confirmed)
        records = [entry for entry, _ in traces.iter_records(kind="model_call")]
        matches = [entry for entry in records if entry.get('id') == trace_id]
        if len(matches) != 1:
            raise ValueError('reconciliation requires one original native trace')
        trace = matches[0]
        traces.validate_native_attempt_outcome(trace, outcome=outcome)
        _unique_reservation(trace, records)
        record = InitRecord.load(state_dir, repo, stage)
        if record is None or record.repo != repo or record.stage != stage:
            raise ValueError('original init account is missing or mismatched')
        yield traces, trace, record, records


def _settle_cost(state_dir, record, trace):
    state_dir = Path(state_dir)
    repo, stage, trace_id = record.repo, record.stage, trace["id"]
    binding = trace["context"]["init_budget"]
    identity = binding["reservation_id"]
    budget = checkpoint_budget(None, record, state_dir)
    if stage == 'feature-discovery':
        from .init_feature_discovery import _discovery_budget
        journal = state_dir / INIT_DIR / repo / 'discovery-budget.json'
        if not journal.is_file() or not record.discovery.get('identity'):
            raise ValueError('original discovery accounting journal is missing')
        budget = _discovery_budget(budget, journal, record.discovery['identity'])
    entry = (budget.calls or {}).get(identity)
    if (entry is None or binding.get('accounting') not in ('actual', 'fixed')
            or entry.get('accounting') != binding['accounting']
            or entry['reserved_usd'] != binding.get('reserved_usd')):
        raise ValueError('native outcome differs from original reservation binding')
    actual = trace.get('result', {}).get('cost_usd')
    if actual is not None and normalized_cost(actual) is None:
        raise ValueError('native cost is invalid')
    receipt = {'trace_id': trace_id, 'reply_blob': trace['outputs']['reply']}
    settled = budget.reconcile(identity, actual_usd=actual, receipt=receipt)
    if stage == 'feature-discovery':
        # The journal is authoritative, so a crash before this mirror save
        # is repaired by the same resume adapter without another charge.
        record.spent_usd = budget.spent_usd
        record.save(state_dir)
    return {'reservation_id': identity, 'trace_id': trace_id, 'accounted_usd': settled['accounted_usd'],
            'native_cost_usd': actual, 'status': settled['status']}


def reconcile_cost(state_dir, *, repo, stage, trace_id, offline_confirmed=False, outcome="complete"):
    with _original_call(state_dir, repo=repo, stage=stage, trace_id=trace_id,
                        offline_confirmed=offline_confirmed, outcome=outcome) as (_, trace, record, _):
        return _settle_cost(state_dir, record, trace)


def _settle_task(state_dir, record, traces, calls, *, pending, requests, scope, apply_result):
    """Settle original accounts before checkpointing a result and its intent.

    Domain adapters validate and prepare the result. This shared boundary owns
    account ordering and the single durable result/intent checkpoint. Partial
    account settlement is idempotent; failed publication of the checkpoint keeps
    the original intent pending through DispatchJournal.
    """
    from .quality_review import DispatchJournal

    records = [entry for entry, _ in traces.iter_records(kind="model_call")]
    accounted = [trace for trace in calls if trace.get('context', {}).get('init_budget') is not None]
    for trace in accounted:
        _unique_reservation(trace, records)
    settlements = [_settle_cost(state_dir, record, trace) for trace in accounted]
    if scope[0] in pending:
        def checkpoint(update):
            update()
            record.save(state_dir)
        journal = DispatchJournal(pending, None, checkpoint, requests=requests)
        journal.settle(None, None, None, lambda *_: apply_result(), scope=scope)
    return settlements


def reconcile_foundation(state_dir, *, repo, scope, offline_confirmed=False):
    """Settle an original foundation task from its retained local inputs.

    Reload durable business state under the existing offline guard. A crash
    during account settlement leaves the intent pending; each original account
    receipt is idempotent. Applying content remains the normal resume's job.
    """
    from .init_knowledge_parallel import original_job_request, replay_original_job
    from .init_knowledge import _Knowledge
    from .init_coverage import Owner
    from .init_support import UpstreamPin, restore_execution
    from .config import RepoLifecycle
    from .knowledge_coverage import load_policy

    if not REPO_ID.fullmatch(repo):
        raise ValueError('invalid init account identity')
    state_dir = Path(state_dir)
    traces = TraceStore(Path(state_dir) / INIT_DIR / 'traces')
    with traces._migration_lock():
        traces._require_paused(None, offline_confirmed)
        record = InitRecord.load(state_dir, repo, 'knowledge')
        if record is None or record.repo != repo or record.stage != 'knowledge':
            raise ValueError('original foundation stage is missing or mismatched')
        request = original_job_request(SimpleNamespace(record=record), scope=scope)
        context = request['context']
        validation = context.get('validation')
        if not isinstance(validation, dict) or not context.get('init'):
            raise ValueError('original foundation validation inputs are unavailable')
        # Retain validation settings without constructing an execution gateway.
        # Replay requires native proofs explicitly and supplies its own reader.
        gateway = SimpleNamespace(zcode_reasoning_level=context['zcode_reasoning_level'])
        runtime, init = restore_execution(SimpleNamespace(state_dir=state_dir, gateway=gateway), context)
        lifecycle = RepoLifecycle(repo=repo, full_name=validation['full_name'], enabled=False,
                                  mode='manual', knowledge_dir=validation['repo_dir'], init=init)
        original = _Knowledge(runtime, lifecycle, dry_run=True, pin=record.pin)
        original.record = record
        original.repo_dir = validation['repo_dir']
        original.tags = context['tags']
        original.owners = [Owner(**owner) for owner in validation['owners']]
        original.coverage_policy = (load_policy(validation['coverage_policy'], original.repo_dir)
                                    if validation.get('coverage_policy') else None)
        upstream = UpstreamPin(state_dir / INIT_DIR / 'upstream' / f'{repo}.git', lifecycle.full_name)
        original.observer = upstream.observer(record.pin)
        result, calls = replay_original_job(original, traces, scope=scope)
        saved = record.coverage['foundation_jobs']
        key = result['owner'] + ':' + result['input_sha256']
        cached = saved['tasks'].get(key)
        if cached is not None and cached != result:
            raise ValueError('original foundation result is immutable')
        if cached is None and scope[0] not in saved.get('inflight', {}):
            raise ValueError('original foundation task is not pending')
        def persist():
            saved['tasks'][key] = result
        settlements = _settle_task(state_dir, record, traces, calls, pending=saved.get('inflight', {}),
                                   requests=saved.get('requests'), scope=scope, apply_result=persist)
        return {'status': 'reconciled', 'task': key, 'result_sha256': result['result_sha256'],
                'settlements': settlements}


def reconcile_discovery(state_dir, *, repo, scope, offline_confirmed=False):
    """Settle one original discovery task without models, source fetch or publish."""
    from .feature_discovery_index import load_index_snapshot
    from .init_feature_discovery import DiscoveryEngine
    from .quality_review import DispatchJournal

    if not REPO_ID.fullmatch(repo):
        raise ValueError('invalid init account identity')
    state_dir = Path(state_dir)
    traces = TraceStore(state_dir / INIT_DIR / 'traces')
    with traces._migration_lock():
        traces._require_paused(None, offline_confirmed)
        record = InitRecord.load(state_dir, repo, 'feature-discovery')
        if record is None or record.repo != repo or record.stage != 'feature-discovery':
            raise ValueError('original discovery stage is missing or mismatched')
        state = record.discovery
        request = DispatchJournal(state.get('dispatch_intents', {}), None, None,
            requests=state.get('dispatch_requests')).original_request(scope=scope)
        context = state.get('dispatch_contexts', {}).get(request.get('context_sha256'), {})
        index = load_index_snapshot(state_dir / 'feature-discovery-index',
                                    sha256=state.get('index_sha256'), pin=record.pin)
        engine = DiscoveryEngine(index, seeds=[], owners=[], state=state, call=None, save=lambda: None,
                                 repository=context.get('repository', ''), pin=record.pin)
        proposed, calls, digest = engine.prepare_original_result(traces, scope=scope)
        def persist():
            # The journal owns its pending mapping. Preserve that reference so
            # its removal and the detached proposal share one record save.
            state.update({key: value for key, value in proposed.items() if key != 'dispatch_intents'})
        settlements = _settle_task(state_dir, record, traces, calls, pending=state.get('dispatch_intents', {}),
            requests=state.get('dispatch_requests'), scope=scope, apply_result=persist)
        return {'status': 'reconciled', 'dispatch': scope[0], 'result_sha256': digest, 'settlements': settlements}


def _review_checkpoint(record, scope):
    """Select the original result owner; never infer missing legacy bindings."""
    from .quality_review import validate_checkpoint_state
    if scope.get('policy') == 'init-block':
        candidates = list(record.coverage.get('quality_reviews', {}).values())
    elif record.stage == 'pr-history' and scope.get('policy') == 'pr-history-aggregate':
        candidates = [record.history.get('aggregate_quality', {})]
    elif record.stage == 'knowledge-deepen' and scope.get('policy') == 'depth-facets':
        candidates = list(record.depth.get('features', {}).values())
    else:
        raise ValueError('quality recovery is not registered for this init stage')
    matches = []
    for state in candidates:
        retained = state.get('review_dispatched_binding', {})
        if retained.get('sha256') == scope.get('dispatch_sha256'):
            validate_checkpoint_state(state)
            matches.append(state)
    if len(matches) != 1:
        raise ValueError('original quality checkpoint binding is missing or ambiguous')
    return matches[0]


def _review_result(traces, trace, record, state):
    from .models import ModelRole
    from .quality_review import QualityPolicy, recover_quality_reply
    retained = state['review_dispatched_binding']
    binding = retained['inputs']
    policy = trace['context']['quality_review']['policy']
    if policy == 'init-block':
        from ..knowledge_service.l1 import Block
        from .gate import DIMENSIONS, validate_block_review, block_review_result
        if not {'block', 'requested', 'role'}.issubset(binding):
            raise ValueError('original block review contract is missing')
        saved = binding['block']
        block = Block(**{key: saved[key] for key in ('kind', 'path', 'rule_id', 'op', 'sha256')})
        if block.to_dict() != saved or block.op not in DIMENSIONS:
            raise ValueError('original block review contract is invalid')
        role = ModelRole.parse(binding['role'], binding['requested'])
        validate = lambda data: validate_block_review(data, block)
        render = lambda reply: block_review_result(reply, block, role).to_dict()
    elif policy == 'pr-history-aggregate':
        from .init_history import _validate_review, aggregate_review_result
        role = ModelRole.parse('pr-reviewer', binding['requested'])
        if role.provider != 'codex':
            raise ValueError('aggregate review requires its original Codex reviewer')
        policy, validate, render = 'pr-history-aggregate', _validate_review, aggregate_review_result
    else:
        from .depth_judge import validate_facet_review, facet_review_result
        required = {'text_sha256', 'requested', 'role', 'acceptance_modes', 'validation_kinds'}
        if not required.issubset(binding):
            raise ValueError('original facet review contract is missing')
        modes, kinds = binding['acceptance_modes'], binding['validation_kinds']
        if (not isinstance(modes, dict) or not modes or any(mode not in ('strict', 'lightweight')
                for mode in modes.values()) or not isinstance(kinds, dict) or set(kinds) - set(modes)):
            raise ValueError('original facet review contract is invalid')
        role = ModelRole.parse(binding['role'], binding['requested'])
        policy = 'depth-facets'
        validate = lambda data: validate_facet_review(data, modes)
        render = lambda reply: facet_review_result(reply, role, modes, kinds)
    reply = recover_quality_reply(traces, trace, policy=QualityPolicy(policy), role=role,
                                  binding=binding, attempt=retained['attempt'], validate=validate)
    return {**render(reply), **binding}


def reconcile_review(state_dir, *, repo, stage, trace_id, offline_confirmed=False):
    """Recover an original judgment using its stage's validator and result owner."""
    with _original_call(state_dir, repo=repo, stage=stage, trace_id=trace_id,
                        offline_confirmed=offline_confirmed) as (traces, trace, record, records):
        scope = trace.get('context', {}).get('quality_review')
        if not scope or sum(entry.get('context', {}).get('quality_review') == scope for entry in records) != 1:
            raise ValueError('quality checkpoint has ambiguous native outcomes')
        state = _review_checkpoint(record, scope)
        result = _review_result(traces, trace, record, state)
        cached = state.get('checked')
        if cached is not None and cached != result:
            raise ValueError('original quality result is immutable')
        if cached is None and not state.get('review_dispatched'):
            raise ValueError('original quality checkpoint is not pending')
        _settle_cost(state_dir, record, trace)
        aggregate = scope['policy'] == 'pr-history-aggregate'
        if cached != result or (aggregate and record.review != result) or state.get('review_dispatched'):
            state['checked'] = result
            state['review_dispatched'] = False
            if aggregate:
                record.review = result
            record.save(state_dir)
        return {'trace_id': trace_id, 'status': 'reconciled',
                **({'verdict': result['verdict'], 'head_sha': result['head_sha']} if aggregate
                   else {'block_id': result['block_id'], 'verdict': result['verdict']}
                   if scope['policy'] == 'init-block'
                   else {'facets': {facet: item['verdict'] for facet, item in result['facets'].items()}})}
