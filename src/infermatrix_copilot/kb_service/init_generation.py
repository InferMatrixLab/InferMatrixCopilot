"""Application binding for unowned init generation; execution stays shared.

Task-journal workflows keep their existing dispatch authority. Serial stage
callers supply their business record and delegate to the same checkpoint runner.
"""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json

from .models import ModelUnavailable
from .quality_review import (GENERATION_CHECKPOINT_KEYS, checkpoint_pending, checkpoint_result,
                             checkpoint_review, validate_checkpoint_state)

KEY = 'generation_execution'
OPTIONS = GENERATION_CHECKPOINT_KEYS


def journal(record, *, create=False):
    value = record.coverage.get(KEY)
    if value is None and KEY not in record.coverage:
        if not create:
            return {}
        value = record.coverage[KEY] = {}
    if not isinstance(value, dict):
        raise ModelUnavailable('invalid generation journal; reconcile original dispatch', allow_fallback=False)
    for state in value.values():
        validate_checkpoint_state(state, **OPTIONS)
        if state.get('generation_dispatched') and not state.get('generation_dispatched_binding'):
            raise ModelUnavailable('generation intent has no binding; reconcile original dispatch', allow_fallback=False)
    return value


def pending(record):
    return any(checkpoint_pending(state, **OPTIONS) for state in journal(record).values())


def preflight(work):
    from .init_support import InitError, InitRecord
    previous = InitRecord.load(work.rt.state_dir, work.lifecycle.repo, work.STAGE)
    if previous is None:
        return
    try:
        states = journal(previous)
        if previous.status not in ('dry_run', 'published', 'empty') and any(
                (state.get('generation_dispatched') or 'generated' in state)
                and 'execution_policy' not in state.get('generation_dispatched_binding', {}).get('inputs', {})
                for state in states.values()):
            raise ModelUnavailable('legacy generation has no workflow policy binding; reconcile original dispatch '
                                   'in its original environment', allow_fallback=False)
    except ModelUnavailable as exc:
        raise InitError(str(exc)) from exc
    if (getattr(work, 'GENERATION_TASK_JOURNALED', False) or KEY in previous.coverage
            or previous.status in ('dry_run', 'published', 'empty')):
        return
    reserved = any(row.get('status') == 'reserved' for row in previous.budget_calls.values())
    if reserved or work.rt.subscription_generator or work.rt.unlimited_subscription:
        raise InitError('legacy generation has no durable intent; reconcile original dispatch '
                        'in its original environment before resuming')


def generate_data(work, *, system, prompt, validate=None, record_payload=True, unit=None):
    from .init_support import generate
    from .init_execution import policy_digest
    policy = getattr(work, 'execution_policy', None)
    if policy is None:
        policy = policy_digest()  # Compatibility for direct private stage calls.
    binding = {'batch_inputs_sha256': work.record.inputs_digest,
               'generator': asdict(work.rt.generator),
               'execution_policy': policy,
               'system_sha256': hashlib.sha256(system.encode()).hexdigest(),
               'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
               'record_payload': record_payload}
    key = unit or hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    states = journal(work.record, create=True)
    state = states.get(key, {})
    cached = checkpoint_result(state, binding=binding, **OPTIONS)
    if cached is not None:
        if validate is not None:
            validate(deepcopy(cached['data']))
        return deepcopy(cached['data'])
    # Changed inputs cannot escape a different unit's original unknown call.
    if pending(work.record):
        raise ModelUnavailable('generation outcome unknown; reconcile original dispatch', allow_fallback=False)
    states.setdefault(key, state)

    def dispatch():
        reply = generate(work.rt, work.budget, work.lifecycle.init, system=system,
                         prompt=prompt, validate=validate, record_payload=record_payload)
        # Parsed domain data is sufficient; omission never gains a raw reply cache.
        return {'data': deepcopy(reply.data)}

    result = checkpoint_review(state, dispatch, lambda: work.record.save(work.rt.state_dir),
                               binding=binding, **OPTIONS)
    return deepcopy(result['data'])


def replace_coverage(record, values):
    """Business metrics cannot discard execution journals or bound rendering policy."""
    retained = {key: record.coverage[key] for key in (KEY, 'quality_reviews', 'quick_map_policy_version')
                if key in record.coverage}
    record.coverage = {**values, **retained}
