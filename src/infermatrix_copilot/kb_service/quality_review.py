"""Application-owned quality judgment execution, independent of stage rubrics.

Policies supply schemas and acceptance; the runner owns dispatch, accounting and
receipts. Provider invocation still goes through ModelGateway. This module does
not impose one acceptance threshold on block, facet or aggregate judgments.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from collections import deque
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import hashlib
import json
import re

from .models import ModelReply, ModelUnavailable
from .init_budget import BudgetExhausted
from ..budgeting import native_outcome_unknown
from ..trace_store import trace_context

_review_scope = ContextVar("knowledge_review_scope", default=None)


@contextmanager
def _attempt_scope(identity, attempt):
    token = _review_scope.set({"dispatch_sha256": identity, "attempt": attempt, "call_index": 0})
    try:
        # Every native call in a task, including generation before its judges,
        # belongs to the original durable dispatch. Review-specific sequencing
        # remains in quality_review and does not redefine the task identity.
        with trace_context(knowledge_dispatch={"dispatch_sha256": identity, "attempt": attempt}):
            yield
    finally:
        _review_scope.reset(token)


class QualityPolicyError(ModelUnavailable):
    """Invalid execution policy, distinct from an unavailable judge response."""


def _dispatch_key(binding):
    if (not isinstance(binding, dict) or type(binding.get('attempt', 1)) is not int
            or binding.get('attempt', 1) < 1):
        raise ModelUnavailable('invalid dispatch binding; reconcile retained state before dispatch',
                               allow_fallback=False)
    try:
        return hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ModelUnavailable('invalid dispatch binding; reconcile retained state before dispatch',
                               allow_fallback=False) from exc


def validate_dispatch_intents(state):
    """Check retained identities before stage policy chooses any new work."""
    if not isinstance(state, dict):
        raise ModelUnavailable('invalid dispatch journal; reconcile retained state before dispatch',
                               allow_fallback=False)
    for key, binding in state.items():
        if key != _dispatch_key(binding):
            raise ModelUnavailable('dispatch binding changed; reconcile retained state before dispatch',
                                   allow_fallback=False)


def validate_dispatch_requests(requests):
    """Validate optional original requests without inventing legacy evidence."""
    if not isinstance(requests, dict):
        raise ModelUnavailable('invalid dispatch requests; reconcile retained state', allow_fallback=False)
    for key, envelope in requests.items():
        if (not isinstance(envelope, dict) or set(envelope) != {'binding', 'request', 'sha256'}
                or not isinstance(envelope['request'], dict)
                or _dispatch_key(envelope['binding']) != key
                or _dispatch_key({k: envelope[k] for k in ('binding', 'request')}) != envelope['sha256']):
            raise ModelUnavailable('original dispatch request changed; reconcile retained state', allow_fallback=False)


class DispatchJournal:
    """Dispatch intents inside the caller's existing knowledge checkpoint.

    ``checkpoint(update)`` applies an update and durably saves the owning record.
    The coordinator alone mutates this mapping; workers never write it.
    """
    def __init__(self, state, identify, checkpoint, *, uncertain=None, requests=None, snapshot=None):
        validate_dispatch_intents(state)
        if requests is not None:
            validate_dispatch_requests(requests)
        if snapshot is not None and requests is None:
            raise ValueError('request snapshots require a durable request mapping')
        self.state, self.identify, self.checkpoint = state, identify, checkpoint
        self.uncertain = uncertain or (lambda value, error: native_outcome_unknown(error))
        self.requests, self.snapshot = requests, snapshot

    def _identity(self, task):
        binding = deepcopy(self.identify(task))
        key = _dispatch_key(binding)
        return key, binding

    def begin(self, task):
        key, binding = self._identity(task)
        if key in self.state:
            raise ModelUnavailable("execution outcome unknown; reconcile original dispatch before retry", allow_fallback=False)
        envelope = None
        if self.requests is not None:
            if self.snapshot is None:
                raise ValueError('read-only request journal cannot dispatch')
            # Match the durable JSON representation, including tuple/list
            # normalization, so an unchanged request compares equal on resume.
            request = json.loads(json.dumps(deepcopy(self.snapshot(task)), ensure_ascii=False))
            envelope = {'binding': binding, 'request': request}
            envelope['sha256'] = _dispatch_key(envelope)
            validate_dispatch_requests({key: envelope})
            if key in self.requests and self.requests[key] != envelope:
                raise ModelUnavailable('original dispatch request is immutable', allow_fallback=False)

        def update():
            self.state[key] = binding
            if envelope is not None:
                self.requests[key] = envelope

        self.checkpoint(update)
        return key, binding.get("attempt", 1)

    def original_request(self, *, scope):
        key, attempt = scope
        if self.requests is None or key not in self.requests:
            raise ModelUnavailable('original dispatch request is unavailable', allow_fallback=False)
        envelope = self.requests[key]
        validate_dispatch_requests({key: envelope})
        if (envelope['binding'].get('attempt', 1) != attempt
                or (key in self.state and self.state[key] != envelope['binding'])):
            raise ModelUnavailable('original dispatch request identity differs', allow_fallback=False)
        return deepcopy(envelope['request'])

    def settle(self, task, value, error, settle, *, scope):
        # The request may have changed while another task settled. Only the
        # identity reserved before this worker started can settle its outcome.
        key, attempt = scope
        binding = deepcopy(self.state.get(key))
        if _dispatch_key(binding) != key or binding.get('attempt', 1) != attempt:
            raise ModelUnavailable('dispatch binding changed; reconcile retained state before settlement',
                                   allow_fallback=False)
        if self.requests is not None and (key in self.requests or self.snapshot is not None):
            self.original_request(scope=scope)
        followups = []

        def update():
            if not self.uncertain(value, error):
                self.state.pop(key, None)
            followups.extend(settle(task, value, error) or ())

        try:
            self.checkpoint(update)
        except BaseException:
            # An outer finally may save the record again. It must retain an
            # unresolved intent if settlement did not return durably.
            self.state[key] = binding
            raise
        return followups


def validate_checkpoint_state(state, *, result_key="checked", dispatch_key="review_dispatched",
                              attempts_key="review_attempts"):
    """Reject corrupt retained state before it can authorize another call."""
    valid = isinstance(state, dict)
    if valid and dispatch_key and dispatch_key in state:
        valid = isinstance(state[dispatch_key], bool)
    if valid and attempts_key in state:
        attempts = state[attempts_key]
        valid = type(attempts) is int and attempts >= 0
    if valid and result_key in state:
        valid = isinstance(state[result_key], dict)
    if not valid:
        raise ModelUnavailable('invalid execution checkpoint; reconcile retained state before dispatch',
                               allow_fallback=False)

    binding_key = dispatch_key + "_binding" if dispatch_key else None
    retained = state.get(binding_key) if binding_key else None
    if binding_key and binding_key in state:
        if (not isinstance(retained, dict) or not isinstance(retained.get("inputs"), dict)
                or type(retained.get("attempt")) is not int or retained["attempt"] < 1
                or retained["attempt"] != state.get(attempts_key)
                or retained.get("sha256") != hashlib.sha256(json.dumps(
                    retained["inputs"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()):
            raise ModelUnavailable("invalid execution binding; reconcile retained state before dispatch",
                                   allow_fallback=False)
    cached = state.get(result_key)
    if retained is not None and isinstance(cached, dict) and any(
            key not in cached or cached[key] != value for key, value in retained["inputs"].items()):
        raise ModelUnavailable("completed result differs from original execution binding; reconcile retained state",
                               allow_fallback=False)


GENERATION_CHECKPOINT_KEYS = dict(result_key="generated", dispatch_key="generation_dispatched",
                                  attempts_key="generation_attempts")


def checkpoint_pending(state, **keys):
    """One pending-intent predicate for generation and quality checkpoints."""
    validate_checkpoint_state(state, **keys)
    return bool(state.get(keys.get("dispatch_key", "review_dispatched"))
                and keys.get("result_key", "checked") not in state)


def checkpoint_result(state, *, binding, result_key="checked", dispatch_key="review_dispatched",
                      attempts_key="review_attempts", reuse=lambda result: True):
    """Read a bound completion or refuse an unresolved intent, without writing."""
    validate_checkpoint_state(state, result_key=result_key, dispatch_key=dispatch_key,
                              attempts_key=attempts_key)
    if not isinstance(binding, dict):
        raise ModelUnavailable("invalid execution binding; reconcile retained state before dispatch",
                               allow_fallback=False)
    cached = state.get(result_key)
    matching = isinstance(cached, dict) and all(cached.get(key) == value for key, value in binding.items())
    if matching and reuse(cached):
        return cached
    if dispatch_key and state.get(dispatch_key) and not matching:
        raise ModelUnavailable("dispatched review has no matching durable receipt; reconcile original dispatch before correction",
                               allow_fallback=False)
    return None


def checkpoint_review(state, run, save, *, binding, result_key="checked",
                      dispatch_key="review_dispatched", attempts_key="review_attempts",
                      reuse=lambda result: True):
    """Persist intent before a judgment and its bound result before returning.

    Adapters retain their existing state keys. Missing legacy intent is not
    invented; an existing intent without a matching result is uncertain and
    cannot silently cause another paid call. A new correction clears its intent
    explicitly when the domain checkpoints the new draft.
    """
    binding = deepcopy(binding)
    cached = checkpoint_result(state, binding=binding, result_key=result_key,
                               dispatch_key=dispatch_key, attempts_key=attempts_key, reuse=reuse)
    if cached is not None:
        return cached
    identity = hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    binding_key = dispatch_key + "_binding" if dispatch_key else None
    # A known rejected result may be retried by explicit policy. Its old receipt
    # must not make the new, unresolved intent look completed after a crash.
    state.pop(result_key, None)
    state[attempts_key] = state.get(attempts_key, 0) + 1
    if dispatch_key:
        state[dispatch_key] = True
        state[binding_key] = {"inputs": deepcopy(binding), "sha256": identity,
                              "attempt": state[attempts_key]}
    save()
    try:
        with _attempt_scope(identity, state[attempts_key]):
            result = run()
    except (BudgetExhausted, QualityPolicyError):
        # These failures precede model dispatch; keep the attempt count for the
        # audit but do not leave an uncertain paid-intent marker.
        if dispatch_key:
            state[dispatch_key] = False
        save()
        raise
    except ModelUnavailable as exc:
        if not native_outcome_unknown(exc):
            if dispatch_key:
                state[dispatch_key] = False
            save()
        raise
    state[result_key] = {**result, **binding}
    if dispatch_key:
        state[dispatch_key] = False
    try:
        save()
    except Exception:
        state.pop(result_key, None)
        if dispatch_key:
            state[dispatch_key] = True
        raise
    return state[result_key]


def drain_review_tasks(tasks, work, settle, *, concurrency, stop_on=(), completion_order=None,
                       followups_first=True, journal=None):
    """Bound dispatch and checkpoint each completion before scheduling follow-ups.

    ``settle(task, value, error)`` owns domain-state translation and persistence;
    it returns follow-up tasks only after the preceding result is durable.
    Budget stops drain already dispatched work without starting more calls.
    A journal persists dispatch intent on the coordinator before submission.
    """
    ready = deque(tasks)
    stopped = False
    completed = completion_order or as_completed

    def invoke(task, scope):
        if scope is None:
            return work(task)
        with _attempt_scope(*scope):
            return work(task)

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="kb-quality") as pool:
        pending = {}
        while (ready and not stopped) or pending:
            while ready and not stopped and len(pending) < concurrency:
                task = ready.popleft()
                scope = journal.begin(task) if journal is not None else None
                pending[pool.submit(invoke, task, scope)] = (task, scope)
            if not pending:
                break
            future = next(completed(pending))
            task, scope = pending.pop(future)
            value, error = None, None
            try:
                value = future.result()
            except Exception as exc:
                error = exc
                stopped |= isinstance(exc, stop_on)
            followups = (journal.settle(task, value, error, settle, scope=scope) if journal is not None
                         else settle(task, value, error)) or ()
            if not stopped:
                if followups_first:
                    ready.extendleft(reversed(tuple(followups)))
                else:
                    ready.extend(followups)
    return not stopped


@dataclass(frozen=True)
class QualityPolicy:
    name: str
    version: int = 1
    subscription: bool = False
    reject_fallback: bool = False
    archive: str = "optional"  # optional | present | verified


def native_receipt(reply, *, required=False):
    """Canonical native reply identity; unknown cost and usage stay unknown."""
    digest = hashlib.sha256(reply.text.encode()).hexdigest()
    if required and (not reply.trace_id or not re.fullmatch(r"[0-9a-f]{64}", reply.reply_sha256 or "")
                     or reply.reply_sha256 != digest):
        raise ModelUnavailable("native archive receipt is missing or inconsistent", allow_fallback=False)
    return {"trace_id": reply.trace_id, "reply_sha256": reply.reply_sha256 or digest,
            "requested": reply.role.label(), "served_model": reply.served_model or None,
            "usage": reply.usage, "seconds": reply.seconds, "cost_usd": reply.cost_usd}


def _quality_input_hash(policy, role, system, prompt):
    identity = {"policy": policy.name, "version": policy.version, "requested": role.label(),
                "system": system, "prompt": prompt}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def recover_native_reply(store, record, *, role, system, prompt, validate=None, zcode_reasoning_level="max"):
    """Decode one original native result; no transport or accounting actions."""
    from ..trace_store import environment_fingerprint
    from .models import parse_json_object
    environment = record.get("env", {})
    if not environment.get("runtime_package_sha256") or environment != environment_fingerprint(refresh=True):
        raise ModelUnavailable("restore the original runtime before quality result recovery", allow_fallback=False)
    if any(store.blob(record["inputs"][key]) != value for key, value in (("system", system), ("prompt", prompt))):
        raise ModelUnavailable("native result differs from original inputs", allow_fallback=False)
    text = store.blob(record["outputs"]["reply"])
    reply = ModelReply(role, parse_json_object(text), text, record.get("model", {}).get("served_model", ""),
                       record.get("usage", {}), record.get("seconds"), record.get("result", {}).get("cost_usd"),
                       record["id"], record["outputs"]["reply"].removeprefix("sha256:"))
    proof = native_receipt(reply, required=True)
    verify_archived_receipt(store, proof, role, text, record=record, zcode_reasoning_level=zcode_reasoning_level)
    if validate is not None:
        validate(reply.data)
    return reply


def recover_quality_reply(store, record, *, policy, role, binding, attempt, validate):
    """Recover a single judgment through the canonical native decoder."""
    system, prompt = (store.blob(record["inputs"][key]) for key in ("system", "prompt"))
    scope = {"policy": policy.name, "version": policy.version,
             "input_sha256": _quality_input_hash(policy, role, system, prompt),
             "dispatch_sha256": _dispatch_key(binding), "attempt": attempt, "call_index": 1}
    if record.get("context", {}).get("quality_review") != scope:
        raise ModelUnavailable("native judgment differs from original checkpoint binding", allow_fallback=False)
    reply = recover_native_reply(store, record, role=role, system=system, prompt=prompt, validate=validate)
    proof = native_receipt(reply, required=True)
    return replace(reply, quality_receipt={**scope, "outcome": "judged", "native": proof})


def review_call(gateway, role, *, policy: QualityPolicy, system, prompt, validate,
                budget=None, charge_usd=None, verify_reply=None):
    """One accounted judgment; never automatically replay an uncertain call.

    The legacy init judge account charges a configured amount per invocation;
    receipts separately retain reported native cost, including unknown usage.
    Other workflows may supply their own enclosing budget reservation.
    """
    if policy.archive not in {"optional", "present", "verified"}:
        raise ValueError("unknown quality archive policy")
    if policy.reject_fallback and role.fallback is not None:
        raise QualityPolicyError("unlimited subscription judgment must not configure a fallback", allow_fallback=False)
    if policy.subscription and not gateway.subscription_billing(role):
        raise QualityPolicyError("subscription judge needs an authenticated subscription backend", allow_fallback=False)
    input_sha = _quality_input_hash(policy, role, system, prompt)
    scope = _review_scope.get()
    if scope is not None:
        scope["call_index"] += 1
    attempt = dict(scope) if scope is not None else {"dispatch_sha256": input_sha, "attempt": 1, "call_index": 1}
    reply = None
    try:
        # Persist application identity in the native archive before dispatch.
        # A crash cannot rely on the final quality receipt being available.
        with trace_context(quality_review={"policy": policy.name, "version": policy.version,
                                          "input_sha256": input_sha, **attempt}), \
                budget.reserve(charge_usd, accounting="fixed") if budget is not None else nullcontext() as reservation:
            reply = gateway.call_json(role, system=system, prompt=prompt, validate=validate)
            if reservation is not None:
                reservation.charge(charge_usd)
            if policy.archive == "present" and (not reply.trace_id or not re.fullmatch(r"[0-9a-f]{64}", reply.reply_sha256 or "")):
                raise ModelUnavailable("native review archive receipt unavailable; draft retained for rejudging", allow_fallback=False)
            proof = native_receipt(reply, required=policy.archive == "verified")
            if verify_reply is not None:
                verify_reply(reply)
    except ModelUnavailable as exc:
        # Archive/evidence rejection happens after a completed model call. Its
        # billing facts remain useful even though this is not an accepted proof.
        if reply is not None:
            exc.native_call_sent = True
            exc.native_call_completed = True
        exc.quality_receipt = {"policy": policy.name, "version": policy.version,
                               "input_sha256": input_sha, **attempt,
                               "outcome": "unknown" if native_outcome_unknown(exc) else "unavailable",
                               "native": native_receipt(reply) if reply is not None else None}
        raise
    receipt = {"policy": policy.name, "version": policy.version, "input_sha256": input_sha,
               **attempt, "outcome": "judged", "native": proof}
    return replace(reply, quality_receipt=receipt) if isinstance(reply, ModelReply) else reply


def verify_archived_receipt(store, proof, expected_role, text=None, *, zcode_reasoning_level="max", record=None):
    import zlib
    try:
        if not re.fullmatch(r"[0-9a-f]{64}", proof.get("reply_sha256", "")):
            raise ValueError("reply content hash is missing")
        if proof.get("requested") != expected_role.label():
            raise ValueError("receipt role differs from the configured discovery role")
        record = store.get(proof["trace_id"]) if record is None else record
        if record.get("id") != proof["trace_id"]:
            raise ValueError("receipt identifies another native call")
        if record.get("kind") != "model_call" or record.get("error") or record.get("outputs", {}).get("reply") != "sha256:" + proof["reply_sha256"]:
            raise ValueError("reply differs from archived successful call")
        archived_reply = store.blob(record["outputs"]["reply"])
        if hashlib.sha256(archived_reply.encode()).hexdigest() != proof["reply_sha256"] or (text is not None and archived_reply != text):
            raise ValueError("reply archive blob differs")
        attempt = store.validate_native_attempt(record)
        model = record.get("model", {})
        if model.get("role") != expected_role.name or model.get("model") != expected_role.model or model.get("provider") != expected_role.provider \
                or model.get("effort", "") != expected_role.effort:
            raise ValueError("archive model differs from requested discovery role")
        if expected_role.provider == "zcode":
            configured_level = zcode_reasoning_level
            if model.get("native_reasoning_level") != configured_level \
                    or attempt.get("model", {}).get("native_reasoning_level") != configured_level:
                raise ValueError("archive effective Zcode reasoning level differs or is unrecorded")
        served = model.get("served_model")
        if expected_role.provider == "zcode" and expected_role.model.casefold() == "glm-5.3" and served \
                and re.sub(r"[^a-z0-9]", "", served.casefold()) != "glm53":
            raise ValueError("served Zcode model differs from GLM-5.3")
    except (KeyError, OSError, ValueError, TypeError, AttributeError, EOFError, zlib.error) as exc:
        error = ModelUnavailable(f"native discovery archive could not be replayed: {exc}", allow_fallback=False)
        error.native_outcome_pending = True
        raise error from exc
