"""Metadata-only projection of the existing model-call capture boundary.

This observer neither dispatches nor reserves/settles anything. Full request
retention remains optional; per-call token facts need no request/reply payload.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
from uuid import uuid4

from .budgeting import usage_observation

_observer = ContextVar('model_usage_observer', default=None)


def model_usage_fact(record):
    identity = record.get('model') or {}
    result = record.get('result') or {}
    return {'id': record['id'], 'context': {'run_id': (record.get('context') or {}).get('run_id')},
            'model': {k: identity.get(k) or '' for k in ('model', 'served_model', 'role', 'provider')},
            'usage': usage_observation(record.get('usage') or {}),
            'result': {k: result[k] for k in ('native_attempt_id', 'cost_usd', 'native_call_sent',
                        'native_outcome_pending') if k in result}}


def observe_model_call(record):
    observer = _observer.get()
    if observer is None or (record.get('context') or {}).get('run_id') != observer['run_id']:
        return
    with observer['lock']:
        observer['observed'] += 1
        try:
            observer['trace'].record('model_call_usage', capture_id=observer['id'],
                                     call=model_usage_fact(record))
            observer['saved'] += 1
        except Exception:
            # A diagnostic write must not turn a paid result into another call.
            # The closing marker makes a partial projection explicit.
            observer['errors'] += 1


@contextmanager
def bind_model_usage(trace, run_id):
    observer = {'trace': trace, 'run_id': run_id, 'id': uuid4().hex,
                'lock': threading.Lock(), 'observed': 0, 'saved': 0, 'errors': 0}
    def marker(phase):
        try:
            trace.record('model_usage_capture', capture_id=observer['id'], phase=phase,
                         observed=observer['observed'], saved=observer['saved'], errors=observer['errors'])
        except Exception:
            observer['errors'] += 1
    marker('start')
    token = _observer.set(observer)
    try:
        yield
    finally:
        with observer['lock']:
            marker('end')
        _observer.reset(token)
