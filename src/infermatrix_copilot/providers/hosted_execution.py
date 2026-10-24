"""Raw native exchange for translating hosts, over the existing call workflow."""
import hashlib

from ..trace_store import redact
from .completion import complete_native
from .native_result import native_process_result
from .original_execution import canonical, run_original_native


def execute_native_process(transport, *, root, identity, request, session_request=None,
                           native_observer=None):
    """One original exchange; hosts keep their own publication and quota policy.

    This boundary validates provider completion, not a caller's judgment/review
    schema. Failed native exits remain failed receipts for the translating host;
    missing or unfinished original archives cannot authorize another dispatch.
    """
    settings = transport.settings
    payload = {'system': request['system'], 'prompt': identity['prompt']}
    expected_inputs = {key: 'sha256:' + hashlib.sha256(redact(value).encode()).hexdigest()
                       for key, value in payload.items()}
    live = {}

    def project(record, store, saved=None):
        if (record['inputs'] != expected_inputs or record['model']['provider'] != transport.spec.id
                or record['model']['model'] != request['model']
                or record['model'].get('effort', '') != request.get('effort', '')):
            raise ValueError('native exchange receipt differs from its original request')
        entry = live.get('entry')
        if entry is None and saved is not None:
            entry = {'native_process': {key: saved[key] for key in ('stdout', 'stderr', 'returncode', 'reason')}}
        process = native_process_result(store, record, entry=entry)
        result = transport.process_receipt(process)
        result.update(native_complete=result['native_complete'] and not record.get('error'),
            native_attempt_id=record['result']['native_attempt_id'], trace_id=record['id'],
            trace_root=str(store.root), error=record.get('error', ''), seconds=record['seconds'],
            cost_usd=record['result'].get('cost_usd'), served_model=record['model'].get('served_model', ''),
            stop_reason=record['result'].get('stop_reason', ''), schema='native-exchange/1')
        return result

    def dispatch(recorder, store):
        try:
            _, _, _, receipt = complete_native(lambda: transport, request=request,
                identity={'provider': transport.spec.id}, payload=payload,
                recorder=recorder, session_request=session_request, settings=settings,
                native_observer=native_observer)
        except Exception as exc:
            # The canonical workflow has already finalized a known native
            # failure. Preserve it as a raw receipt; preparation/unknown errors
            # without process evidence keep their original exception and intent.
            if (getattr(exc, 'call_budget_failure', False) or getattr(exc, 'native_observer_failure', False)
                    or getattr(exc, 'native_outcome_pending', False) or recorder.receipt is None
                    or not recorder.entry.get('native_process') or not recorder.entry.get('error')
                    or recorder.entry['error'] != (str(exc) or type(exc).__name__)):
                raise
            receipt = recorder.receipt
        live['entry'] = recorder.entry
        return receipt

    bound = {**identity, 'timeout_s': settings.strict_backend_timeout_s,
        'request_sha256': hashlib.sha256(canonical({key: value for key, value in request.items()
        if key not in ('absolute_deadline',)})).hexdigest()}
    return run_original_native(root=root, name='exchange', identity=bound, dispatch=dispatch, project=project)
