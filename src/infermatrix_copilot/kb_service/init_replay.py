"""Read-only replay of retained task calls through existing init workflows.

This adapter has no transport and no ledger writer. The application must verify
that its archive inventory is complete and independently reconcile original
accounts/results before clearing an intent. Consuming the supplied records is
not evidence that no other unfinished native attempts exist.
"""
from contextlib import contextmanager
from copy import deepcopy
import json

from ..trace_store import current_context
from .init_budget import Reservation
from .models import ModelUnavailable
from .quality_review import _dispatch_key, recover_native_reply


def original_task_records(store, *, binding, attempt):
    """Inventory original calls while the caller holds the offline writer guard.

    JSONL is authoritative for receipts, but an attempt can precede its receipt.
    Refuse unreadable archives since their task ownership cannot be established.
    This function performs no repair, dispatch, accounting or index rebuild.
    """
    scope = {"dispatch_sha256": _dispatch_key(binding), "attempt": attempt}
    records = [record for record, _ in store.iter_records(kind="model_call")
               if record.get("context", {}).get("knowledge_dispatch") == scope]
    attempts = {}
    root = store.root / "attempts"
    if root.is_symlink() or not root.is_dir():
        raise ModelUnavailable("original task archive inventory is unavailable", allow_fallback=False)
    for directory in root.iterdir():
        path = directory / "attempt.json"
        try:
            if directory.is_symlink() or not directory.is_dir() or path.is_symlink():
                raise ValueError("nonlocal attempt")
            metadata = json.loads(path.read_text())
            if (metadata.get("schema") != "native-attempt/1" or metadata.get("id") != directory.name
                    or not isinstance(metadata.get("context"), dict)):
                raise ValueError("invalid attempt identity")
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ModelUnavailable("original task archive inventory is unreadable", allow_fallback=False) from exc
        if metadata["context"].get("knowledge_dispatch") == scope:
            attempts[directory.name] = metadata
    referenced = [record.get("result", {}).get("native_attempt_id") for record in records]
    if (not records or len(set(referenced)) != len(referenced) or set(referenced) != set(attempts)):
        raise ModelUnavailable("original task has missing or ambiguous native outcomes", allow_fallback=False)
    for record in records:
        try:
            store.validate_native_attempt(record)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ModelUnavailable("original task has an unresolved native attempt", allow_fallback=False) from exc
    return records


class OriginalTaskReplay:
    def __init__(self, store, records, *, binding, attempt, subscription_roles=(), zcode_reasoning_level="max"):
        self.store = store
        self.scope = {"dispatch_sha256": _dispatch_key(binding), "attempt": attempt}
        self.records = [deepcopy(record) for record in records
                        if record.get("context", {}).get("knowledge_dispatch") == self.scope]
        ids = [record.get("id") for record in self.records]
        if (not ids or any(not identity for identity in ids) or len(set(ids)) != len(ids)
                or any(record.get("kind") != "model_call" for record in self.records)):
            raise ModelUnavailable("original task calls are missing or ambiguous", allow_fallback=False)
        self.subscription_roles = frozenset(subscription_roles)
        self.zcode_reasoning_level = zcode_reasoning_level
        self._used, self._consumed = set(), []
        self._reservation = None
        self._failed = False

    def _refuse(self, message):
        self._failed = True
        raise ModelUnavailable(message, allow_fallback=False)

    def subscription_billing(self, role):
        # This is the frozen task's admission, not a new authentication probe.
        return role.label() in self.subscription_roles

    @contextmanager
    def reserve(self, amount, *, accounting="actual"):
        """Check an original reservation; never reserve or charge a ledger."""
        if self._reservation is not None or accounting not in ("actual", "fixed"):
            self._refuse("invalid original reservation replay")
        reservation = Reservation(amount, accounting=accounting)
        self._reservation = reservation
        try:
            yield reservation
            if not reservation.identity or reservation.charged is None:
                self._refuse("original reservation replay is incomplete")
        finally:
            self._reservation = None

    def call_json(self, role, *, system, prompt, validate=None, max_budget_usd=None, record_payload=True):
        context = current_context()
        if context.get("knowledge_dispatch") != self.scope or not record_payload:
            self._refuse("replay requires the original task scope and retained inputs")
        candidates = []
        for record in self.records:
            if record['id'] in self._used:
                continue
            model = record.get('model', {})
            if any(model.get(key, '') != value for key, value in (
                    ('role', role.name), ('provider', role.provider), ('model', role.model), ('effort', role.effort))):
                continue
            if record.get('context', {}).get('quality_review') != context.get('quality_review'):
                continue
            if (self.store.blob(record['inputs']['system']) == system
                    and self.store.blob(record['inputs']['prompt']) == prompt):
                candidates.append(record)
        if len(candidates) != 1:
            self._refuse("original task call is missing, changed or ambiguous")
        record = candidates[0]
        reservation = self._reservation
        account = record.get('context', {}).get('init_budget')
        if reservation is None:
            if account is not None:
                self._refuse("original task call requires its reservation scope")
        elif (not isinstance(account, dict) or not account.get('reservation_id')
                or reservation.identity or account.get('reserved_usd') != reservation.amount
                or account.get('accounting') != reservation.accounting):
            self._refuse("original task reservation differs from replay")
        try:
            reply = recover_native_reply(self.store, record, role=role, system=system, prompt=prompt,
                                         validate=validate, zcode_reasoning_level=self.zcode_reasoning_level)
        except Exception:
            self._failed = True
            raise
        if reservation is not None:
            reservation.identity = account['reservation_id']
        self._used.add(record['id'])
        self._consumed.append(record)
        return reply

    def consumed_records(self):
        """Return original facts only after every supplied call was consumed."""
        if self._failed or self._reservation is not None or len(self._used) != len(self.records):
            self._refuse("original task replay is incomplete")
        return deepcopy(self._consumed)
