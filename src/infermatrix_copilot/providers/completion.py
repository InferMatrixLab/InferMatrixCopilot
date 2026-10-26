"""One native invocation, including partial events and its final receipt.

Callers supply reply validation and policy errors; this layer does not know
knowledge schemas, model roles, budgets, or application runtimes.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

from ..budgeting import call_budget, normalized_cost


def native_capture_result(entry, extra_result=None, *, preserve_legacy_account=False):
    """Project native observations separately from a caller's legacy account.

    USD/subscription aliases may describe an existing domain convention. Keep
    them compatible; cost_usd/native_subscription are authoritative native
    facts and cannot be overridden by those aliases.
    """
    result = dict(extra_result or {})
    aliases = {'usd': entry['cost_usd'], 'subscription': entry.get('subscription')}
    if preserve_legacy_account:
        for key, value in aliases.items():
            result.setdefault(key, value)
    else:
        result.update(aliases)
    result.update(cost_usd=entry['cost_usd'], native_subscription=entry.get('subscription'))
    result.update({key: entry[key] for key in ('native_attempt_id', 'native_session_ids') if key in entry})
    result.update({key: entry[key] for key in ('native_call_sent', 'native_call_completed', 'native_outcome_pending') if key in entry})
    from ..trace_store import native_process_fields
    result.update(native_process_fields(entry)[1])
    result.update(entry.get('session_result', {}))
    return result


def _captured_native_recorder(store, request, options, completion, *, preserve_legacy_account=False):
    """One durable conversation capture for configured and account-required calls."""
    from ..llm import capture_model_call
    from ..trace_store import native_process_fields
    provider = options.get('provider', 'harness:' + request.get('provider_id', ''))

    def record(entry):
        reply, complete, usage, served_model = completion()
        return capture_model_call({**request, 'tools': []}, request.get('role', ''), provider,
            reply if complete else None, entry['seconds'], error=entry['error'],
            reported_usage=usage, served_model=served_model,
            extra_result=native_capture_result(entry, options.get('extra_result'),
                                               preserve_legacy_account=preserve_legacy_account),
            extra_outputs={'native_events': entry.get('native_events', ''), **native_process_fields(entry)[0]}, store=store, strict=True)

    record.begin_call = lambda entry: store.begin_call({**entry, 'provider': provider})
    return record


@contextmanager
def _completion_boundary():
    """Retain dispatch facts when recording or account settlement itself fails."""
    state = {"sent": False, "completed": False, "pending": False}
    try:
        yield state
    except BaseException as exc:
        # Ordinary provider failures are classified below. Infrastructure errors
        # escaping that path must not authorize a second paid call.
        classified = state.get("classified_error") is exc
        exc.native_call_sent = state["sent"]
        exc.native_call_completed = state["completed"]
        exc.native_outcome_pending = (state["pending"] or bool(getattr(exc, "native_outcome_pending", False))
                                      or state["sent"] and not classified)
        if (exc.native_outcome_pending or state["sent"] and not classified) and hasattr(exc, "allow_fallback"):
            exc.allow_fallback = False
        raise


def complete_native(
    transport_factory, *, request: dict, identity: dict | None = None,
    payload: dict | None = None, recorder=None, validate=None,
    record_payload=True, pacer=None, stop_file=None,
    cancelled_errors=(), passthrough_errors=(), make_error=None,
    capture=False, settings=None, session_request=None, native_observer=None,
):
    """Return ``(reply, validated_value, record, receipt)`` without retrying.

    The receipt is finalized once, after validation. Unknown native usage is
    retained on failures and interruption. A stop threshold is accepted only
    from a transport that attests it; it never becomes a token approximation.
    """
    session = session_request is not None
    with _completion_boundary() as dispatch, call_budget({**request, "kind": "harness_session" if session else "harness",
                      "provider_id": (identity or {}).get("provider", "")}) as budget:
        started = time.time()
        capture_started = time.monotonic()
        identity = {**{key: request.get(key, '') for key in ('role', 'model', 'effort')}, **(identity or {})}
        payload = dict(payload if payload is not None else
                       {k: request[k] for k in ("system", "messages") if k in request})
        if not record_payload:
            payload = {key: "" for key in payload}
        from .completion_profiles import CompletionPolicyError, completion_profile_config
        policy = completion_profile_config(settings, identity.get('provider', ''), session=session)
        if (policy is not None or budget.native_archive_root) and not record_payload:
            raise CompletionPolicyError('durable native accounting requires retained request/result evidence')
        if policy is not None:
            options = dict(capture) if isinstance(capture, dict) else {}
            if recorder is not None:
                durable = getattr(recorder, 'durable', None)
                if not callable(durable):
                    raise CompletionPolicyError('configured completion requires a durable native recorder')
                recorder = durable()
            else:
                from pathlib import Path
                from ..trace_store import NativeCompletionRecorder, TraceStore, current_store
                store = current_store()
                if store is None:
                    root = getattr(settings, 'trace_store_root', '')
                    if not root or not Path(root).expanduser().is_absolute():
                        raise CompletionPolicyError('configured completion requires an absolute TRACE_STORE_ROOT')
                    store = TraceStore(Path(root).expanduser())
                if 'prompt' in payload:
                    recorder = NativeCompletionRecorder(store, strict=True)
                else:
                    recorder = _captured_native_recorder(store, {**request, 'provider_id': identity.get('provider', '')},
                        options, lambda: (reply, native_complete, usage, served_model))
            # The selected recorder emits the one canonical model_call row.
            capture = False
        begin = getattr(recorder, "begin_call", None)
        if not callable(begin) and budget.native_archive_root:
            from pathlib import Path
            from ..trace_store import NativeCompletionRecorder, TraceStore, current_store
            store = current_store() or TraceStore(Path(budget.native_archive_root))
            options = dict(capture) if isinstance(capture, dict) else {}
            durable = NativeCompletionRecorder(store, strict=True) if 'prompt' in payload else \
                _captured_native_recorder(store, {**request, 'provider_id': identity.get('provider', '')}, options,
                    lambda: (reply, native_complete, usage, served_model), preserve_legacy_account=True)
            observer = recorder
            def combined(entry):
                receipt = durable(entry)
                if observer is not None:
                    observer(entry)
                return receipt
            recorder, begin, capture = combined, durable.begin_call, False
        archive = begin({**identity, **payload}) if callable(begin) else None
        native_events, transport, ticket = [], None, None
        label = identity.get("requested") or request.get("model") or identity.get("provider", "native")
        threshold = request.get("max_budget_usd")

        def failure(message, allow_fallback=False):
            return make_error(message, allow_fallback) if make_error else RuntimeError(message)

        def canceled(message):
            error = failure(message)
            error.pre_dispatch_cancelled = True
            return error

        def event_sink(event):
            native_events.append(event)
            if archive is not None and record_payload:
                archive.event(event)
            if pacer is not None and ticket is not None:
                pacer.observe(ticket, event, event_sink)
            if native_observer is not None:
                try:
                    native_observer(event)
                except BaseException as exc:
                    exc.native_observer_failure = True
                    raise

        reply, value, error, native_complete = None, None, None, False
        outcome, session_result = None, {'session': True} if session else {}
        try:
            if stop_file and stop_file.exists():
                event_sink({"type": "native.throttle.stopped", "payload": {"at": time.time(), "before_dispatch": True}})
                raise canceled("campaign stop requested before native dispatch")
            transport = transport_factory()
            if policy is not None:
                from .completion_profiles import prepare_completion_transport
                try:
                    transport = prepare_completion_transport(settings, transport, archive, request=request,
                                                             session_request=session_request)
                except CompletionPolicyError:
                    raise
                except Exception as exc:
                    problem = CompletionPolicyError('configured native preparation failed: ' + type(exc).__name__)
                    problem.native_outcome_pending = bool(getattr(exc, 'native_outcome_pending', False))
                    raise problem from exc
            preflight = getattr(transport, "validate_session" if session else "validate_completion", None)
            if callable(preflight):
                preflight(session_request) if session else preflight()
            from ..sandbox.base import SandboxCompletionProfile
            profile = getattr(transport, 'session_profile' if session else 'completion_profile', None)
            if isinstance(profile, SandboxCompletionProfile):
                profile.sandbox.require_inputs(session_request.frozen_inputs if session else request.get('frozen_inputs'))
            validate_request = None if session else getattr(transport, 'validate_completion_request', None)
            if callable(validate_request):
                validate_request(request)
            kwargs = dict(request)
            from .base import native_budget_facts
            execution = native_budget_facts(transport, session=session, provider_id=identity.get('provider', ''))
            from ..trace_store import NativeCallArchive
            if isinstance(archive, NativeCallArchive):
                execution['native_binding'] = {'schema': 'native-budget-binding/1',
                    'trace_root': str(archive.store.root.resolve()), 'attempt_id': archive.id,
                    **{key: archive.meta[key] for key in ('model', 'context', 'env', 'inputs')}}
            updates = budget.prepare(execution)
            if session and updates:
                raise ValueError('native sessions cannot apply completion budget arguments')
            kwargs.update(updates)
            if updates and callable(validate_request):
                validate_request(kwargs)
            threshold = kwargs.get('max_budget_usd')
            if threshold is not None:
                if not threshold > 0:
                    raise failure(f"{label}: max_budget_usd must be positive")
                if not getattr(transport, "stops_at_spend", False):
                    raise failure(f"{identity.get('provider', '')} cannot stop a call at a spend threshold")
            # Partial usage is an accounting fact even when no archive or
            # pacing policy is configured. Recording remains independently
            # controlled by record_payload and the recorder above.
            if getattr(transport, "supports_native_events", False):
                kwargs["native_event_sink"] = event_sink
            if pacer is not None:
                if not getattr(transport, "supports_native_events", False):
                    raise failure("paced Zcode dispatch requires native error events")
                try:
                    ticket = pacer.acquire(event_sink)
                except cancelled_errors as exc:
                    raise canceled(str(exc)) from exc
                if pacer.stop_file and pacer.stop_file.exists():
                    event_sink({"type": "native.throttle.stopped", "payload": {"at": time.time(), "before_dispatch": True}})
                    raise canceled("campaign stop requested before Zcode dispatch")
            budget["sent"] = True
            dispatch["sent"] = True
            if session:
                from copy import copy
                from ..llm import Block, Reply
                observed = copy(session_request)
                def observe(event):
                    event_sink(event)
                    if getattr(session_request, 'native_event_sink', None) is not None:
                        session_request.native_event_sink(event)
                observed.native_event_sink = observe
                outcome = transport.run_session(observed)
                reply = Reply(blocks=[Block(type='text', text=outcome.text or '')],
                    stop_reason='truncated' if outcome.truncated else 'end_turn')
                session_result.update(iterations=outcome.iterations, tool_calls=outcome.tool_calls,
                    truncated=outcome.truncated, refusals=list(outcome.refusals), tools_used=list(outcome.tools_used))
            else:
                reply = transport.complete(**kwargs)
            dispatch["completed"] = True
            native_complete = True
            seconds = time.time() - started
            if pacer is not None:
                try:
                    pacer.finish(ticket, success=True, sink=event_sink)
                except BaseException as exc:
                    if hasattr(exc, "allow_fallback"):
                        exc.allow_fallback = False
                    raise
            value = validate(reply) if validate is not None else outcome
        except BaseException as exc:
            error = exc
            from .outcomes import requires_reconciliation
            dispatch["pending"] = requires_reconciliation(exc)

        if native_complete:
            text = "".join(getattr(block, "text", "") or "" for block in getattr(reply, "blocks", []))
            budget.update(reply=reply, usage=getattr(reply, "usage", None))
            usage = dict(getattr(reply, "usage", {}) or {})
            served_model, stop_reason = getattr(reply, "model", "") or "", getattr(reply, "stop_reason", "")
            if session:
                budget.update(reply=outcome, usage=outcome.usage)
                if native_events:
                    snapshot = getattr(transport, 'native_snapshot', None)
                    partial = snapshot(native_events) if callable(snapshot) else {}
                    if not outcome.usage:
                        budget['usage'] = partial.get('usage')
                    served_model = partial.get('served_model', '')
                usage = dict(budget.get('usage') or {})
                # Legacy display counters do not attest native usage.
                reply.usage = dict(usage)
        else:
            snapshot = getattr(transport, "native_snapshot", None)
            partial = snapshot(native_events) if callable(snapshot) else {}
            text, usage = partial.get("text", ""), partial.get("usage") or {}
            budget["usage"] = partial.get("usage")
            served_model = partial.get("served_model", "")
            if session and native_events:
                session_result.update(reported_usage=partial.get('usage'), served_model=served_model)
            stop_reason = "canceled_before_dispatch" if getattr(error, "pre_dispatch_cancelled", False) else ""
            seconds = round(time.time() - started, 3)
        budget["outcome"] = "completed" if error is None and not (session and outcome.refusals) else "failed"
        budget.native_outcome_pending = dispatch['pending']
        reported = usage.get("cost_usd")
        record = {**identity, "served_model": served_model, "stop_reason": stop_reason, "usage": usage,
                  "cost_usd": normalized_cost(reported),
                  "max_budget_usd": threshold, "seconds": round(seconds, 3),
                  **payload, "reply": text if record_payload else ""}
        process = getattr(transport, 'native_process_outcome', None)
        if record_payload and process is not None:
            from dataclasses import asdict
            record['native_process'] = asdict(process)
        if record_payload and error is not None:
            from ..process_execution import process_cleanup_facts
            record.update(process_cleanup_facts(error))
        if getattr(transport, 'configured_subscription_admission', None) is not None:
            record['subscription'] = getattr(transport, 'subscription_billing', False) is True
        error_text = (str(error) or type(error).__name__) if error is not None else ""
        entry = {**record,
                 "error": error_text if native_complete else error_text[:2000] if record_payload else "transport failed (payload omitted)"}
        if session:
            entry['session_result'] = session_result
            if outcome is not None:
                from dataclasses import asdict
                entry['session_outcome'] = {key: value for key, value in asdict(outcome).items() if key != 'text'}
        if archive is not None:
            entry.update(native_call_sent=dispatch['sent'], native_call_completed=dispatch['completed'],
                         native_outcome_pending=dispatch['pending'])
        if archive is not None:
            entry.update(archive.payload())
        receipt = recorder(entry) if recorder else None
        receipt = receipt if isinstance(receipt, dict) else {}
        if archive is not None:
            status = "complete" if error is None else "canceled_before_dispatch" if getattr(error, "pre_dispatch_cancelled", False) \
                else "failed" if isinstance(error, Exception) else "interrupted"
            archive.finish(receipt, entry, status=status)
            budget.native_receipt_complete = True
        if capture:
            from ..llm import capture_model_call
            from ..trace_store import native_process_fields
            options = capture if isinstance(capture, dict) else {}
            capture_model_call({**request, "tools": []}, request.get("role", ""),
                               options.get("provider", f"harness:{identity.get('provider', '')}"),
                               reply if native_complete else None,
                               time.monotonic() - capture_started, error=error_text,
                               extra_result=native_capture_result(entry, options.get("extra_result"), preserve_legacy_account=True),
                               reported_usage=usage, served_model=served_model,
                               extra_outputs=native_process_fields(entry)[0])
        if error is not None:
            if native_complete or not isinstance(error, Exception) or isinstance(error, passthrough_errors) or make_error is None \
                    or getattr(error, 'call_budget_failure', False):
                dispatch["classified_error"] = error
                raise error
            translated = failure(f"{label} failed: {error}",
                                 not dispatch["pending"] and getattr(error, 'allow_fallback', True))
            translated.native_outcome_pending = dispatch["pending"]
            dispatch["classified_error"] = translated
            raise translated from error
        return reply, value, record, receipt
