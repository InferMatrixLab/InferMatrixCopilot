"""Spend accounting for ``kb init`` (design kb-init §10 "Budget").

A stage's ``budget_usd`` is a hard ceiling. A generator call is handed a spend
threshold (``generator_call_usd``) that its transport honours, but the claude
CLI checks it only after each API request completes, so a call can overshoot by
one request. Every call therefore RESERVES, before it is dispatched,

    threshold + worst_request
    worst_request = input_bytes * 1.25 * in_price + max_output_tokens * out_price

where input bytes upper-bound input tokens (bytes, never chars/4) and 1.25
covers cache-write pricing. The call runs only if the reservation fits in
what is left; afterwards the reported cost replaces it (the whole reservation
when the cost is unknown or the call failed). A generator model without a
price is refused (fail closed).

Judge calls go through a subscription transport with no per-call price; they
count a fixed ``judge_call_usd`` each. That part is an accounting convention,
not a spend bound.

Explicit unlimited-subscription depth uses a None ceiling after authenticating
both subscription roles. Reservations still record fixed accounted amounts;
they neither invent API prices nor report unknown subscription costs as zero.
"""

from __future__ import annotations

import json
import math
import os
import threading
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Callable, Iterator, Mapping

from ..budgeting import request_cost_bound, reservation_fits, reserved_call, settlement_charge

PRICES_ENV = "KB_INIT_PRICES"
CACHE_WRITE_FACTOR = 1.25


@dataclass(frozen=True)
class Price:
    in_usd_per_mtok: float
    out_usd_per_mtok: float
    max_output_tokens: int


# Anthropic list prices, cached 2026-09-25. Override or extend with
# KB_INIT_PRICES='{"<model>": [in_usd_per_mtok, out_usd_per_mtok, max_output_tokens]}'.
DEFAULT_PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00, 20.00, 128_000),
}


class BudgetExhausted(RuntimeError):
    """A reservation does not fit in what is left of the stage's budget."""


class PriceError(ValueError):
    """The price table is malformed, or has no entry for a model."""


def _price(model: str, raw: object) -> Price:
    if not isinstance(raw, list) or len(raw) != 3:
        raise PriceError(f"{PRICES_ENV}[{model}] must be [in, out, max_output_tokens]")
    in_price, out_price, max_out = raw
    for value in (in_price, out_price):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                or value < 0:
            raise PriceError(f"{PRICES_ENV}[{model}] prices must be finite numbers >= 0")
    if isinstance(max_out, bool) or not isinstance(max_out, int) or max_out < 1:
        raise PriceError(f"{PRICES_ENV}[{model}] max_output_tokens must be an integer >= 1")
    return Price(float(in_price), float(out_price), max_out)


def load_prices(environ: Mapping[str, str] | None = None) -> dict[str, Price]:
    """The default table, with ``KB_INIT_PRICES`` entries added or overriding."""
    environ = os.environ if environ is None else environ
    prices = dict(DEFAULT_PRICES)
    raw = environ.get(PRICES_ENV, "").strip()
    if not raw:
        return prices
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PriceError(f"{PRICES_ENV} is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PriceError(f"{PRICES_ENV} must be a JSON object")
    for model, value in data.items():
        prices[str(model)] = _price(str(model), value)
    return prices


def price_for(prices: Mapping[str, Price], model: str) -> Price:
    try:
        return prices[model]
    except KeyError:
        raise PriceError(f"no price for generator model {model!r}; add it to {PRICES_ENV}") from None


def worst_request_usd(price: Price, input_bytes: int) -> float:
    """The most one request can cost: every input byte a token (at the
    cache-write rate) plus a full-length reply."""
    if input_bytes < 0:
        raise ValueError("input_bytes must be >= 0")
    return request_cost_bound(input_bytes, price.in_usd_per_mtok, price.max_output_tokens,
                              price.out_usd_per_mtok, cache_factor=CACHE_WRITE_FACTOR)


def generator_reservation(price: Price, threshold_usd: float, input_bytes: int) -> float:
    return threshold_usd + worst_request_usd(price, input_bytes)


class Reservation:
    """One reserved amount; ``charge`` records what the call actually cost."""

    def __init__(self, amount: float, identity: str = "", accounting: str = "actual"):
        self.amount = amount
        self.identity = identity
        self.accounting = accounting
        self.charged: float | None = None

    def charge(self, actual: float | None) -> None:
        """Record the call's cost; None (unknown) charges the full reservation.
        A reported cost above the reservation is charged as reported."""
        # Fixed judge accounting is an allowance, not the provider's reported
        # API cost. Native cost and unknown usage remain in the trace receipt.
        self.charged = settlement_charge(self.amount, self.amount if self.accounting == "fixed" else actual)


class Budget:
    """A finite stage ceiling, or explicit None for uncapped accounting.
    Reservations and optional checkpoints share one lock; model calls do not.

    The checkpoint receives settled spend and outstanding reservations before
    dispatch and after settlement. A checkpoint failure propagates and charges
    the affected reservation conservatively, so an outer record save cannot
    erase a reservation that the failing callback may already have persisted.
    Further dispatch requires recovery into a new budget instance.
    """

    def __init__(self, limit_usd: float | None, *, spent_usd: float = 0.0,
                 checkpoint: Callable[[float, float], None] | None = None,
                 calls: dict | None = None):
        if limit_usd is not None and (not math.isfinite(limit_usd) or limit_usd <= 0):
            raise ValueError("limit_usd must be a finite number > 0")
        if not math.isfinite(spent_usd) or spent_usd < 0:
            raise ValueError("spent_usd must be a finite number >= 0")
        self.limit_usd = float(limit_usd) if limit_usd is not None else None
        self.spent_usd = float(spent_usd)
        self._reserved = 0.0
        self._lock = threading.RLock()
        self._checkpoint = checkpoint
        self._checkpoint_failed = False
        self.calls = calls
        if calls is not None:
            if not isinstance(calls, dict):
                raise ValueError("invalid init budget call journal")
            for identity, entry in calls.items():
                if (not isinstance(identity, str) or len(identity) != 32
                        or any(c not in '0123456789abcdef' for c in identity)
                        or not isinstance(entry, dict) or entry.get('status') not in ('reserved', 'settled')
                        or ('accounting' in entry and entry['accounting'] not in ('actual', 'fixed'))
                        or any(isinstance(entry.get(key), bool) or not isinstance(entry.get(key), (int, float))
                               or not math.isfinite(entry[key]) or entry[key] < 0
                               for key in ('reserved_usd', 'accounted_usd'))):
                    raise ValueError("invalid init budget call journal")
                if entry['status'] == 'reserved' and entry['accounted_usd'] < entry['reserved_usd']:
                    raise ValueError("unresolved init budget call lost its reservation")
            # Some legacy stage saves round their public total to six decimals.
            if math.fsum(entry['accounted_usd'] for entry in calls.values()) > spent_usd + 1e-6:
                raise ValueError("init budget total is below its recorded calls")

    @property
    def remaining_usd(self) -> float | None:
        with self._lock:
            return None if self.limit_usd is None else self.limit_usd - self.spent_usd - self._reserved

    def can_reserve(self, amount: float) -> bool:
        """An explicit None ceiling keeps accounting without limiting calls."""
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("a reservation must be a finite number >= 0")
        with self._lock:
            return reservation_fits(self.limit_usd, self.spent_usd, max(0, self._reserved), amount)

    def checkpoint_now(self, update: Callable[[], None] | None = None) -> None:
        """Apply and save domain state under the reservation checkpoint lock."""
        with self._lock:
            if self._checkpoint_failed:
                raise RuntimeError("budget checkpoint failed; recover durable state before dispatch")
            try:
                if update is not None:
                    update()
                if self._checkpoint is not None:
                    self._checkpoint(self.spent_usd, self._reserved)
            except BaseException:
                self._checkpoint_failed = True
                raise

    @contextmanager
    def reserve(self, amount: float, *, accounting: str = "actual") -> Iterator[Reservation]:
        """Hold ``amount`` for one call. Raises ``BudgetExhausted`` before the
        call when it does not fit. On exit the charge (or, when the block did
        not charge, e.g. because the call raised, the whole reservation) is
        added to what was spent."""
        if accounting not in ("actual", "fixed"):
            raise ValueError("unknown init budget accounting policy")

        def acquire():
            with self._lock:
                if self._checkpoint_failed:
                    raise RuntimeError("budget checkpoint failed; recover durable state before dispatch")
                if not self.can_reserve(amount):
                    raise BudgetExhausted(
                        f"reserving ${amount:.4f} would exceed the budget "
                        f"(spent ${self.spent_usd:.4f} of ${self.limit_usd:.2f})")
                self._reserved += amount
                reservation = Reservation(amount, uuid.uuid4().hex if self.calls is not None else "", accounting)
                if self.calls is not None:
                    self.calls[reservation.identity] = {"status": "reserved", "reserved_usd": amount,
                                                        "accounted_usd": amount, "accounting": accounting}
                try:
                    if self._checkpoint is not None:
                        self._checkpoint(self.spent_usd, self._reserved)
                except BaseException:
                    self._reserved -= amount
                    self.spent_usd += amount
                    self._checkpoint_failed = True
                    raise
                return reservation

        def finish(call):
            with self._lock:
                self._reserved -= amount
                charged = settlement_charge(amount, call["reservation"].charged)
                self.spent_usd += charged
                entry = self.calls[call["reservation"].identity] if self.calls is not None else None
                if entry is not None:
                    entry.update(status="settled" if call["reservation"].charged is not None else "reserved",
                                 accounted_usd=charged)
                try:
                    if self._checkpoint is not None:
                        self._checkpoint(self.spent_usd, self._reserved)
                except BaseException:
                    self.spent_usd += max(amount - charged, 0.0)
                    if entry is not None:
                        entry.update(status="reserved", accounted_usd=max(amount, charged))
                    self._checkpoint_failed = True
                    raise

        with reserved_call(acquire, finish) as call:
            from ..trace_store import trace_context
            reservation = call["reservation"]
            with trace_context(init_budget={"reservation_id": reservation.identity,
                                            "reserved_usd": amount, "accounting": accounting}) if self.calls is not None else nullcontext():
                yield reservation

    def reconcile(self, identity: str, *, actual_usd, receipt: dict) -> dict:
        """Settle a proven original outcome; application code validates evidence.

        This operates on a resumed, idle account and never reserves another call.
        Unreported cost retains the original conservative accounted amount.
        """
        from copy import deepcopy
        with self._lock:
            if self._reserved or self._checkpoint_failed:
                raise ValueError("reload the original idle budget before reconciliation")
            if self.calls is None or identity not in self.calls:
                raise ValueError("original budget reservation is missing")
            entry = self.calls[identity]
            if entry.get("accounting") not in ("actual", "fixed"):
                raise ValueError("original accounting policy is missing")
            charged = (entry["reserved_usd"] if entry["accounting"] == "fixed" else
                       settlement_charge(entry["accounted_usd"], actual_usd))
            if entry["status"] == "settled":
                if entry["accounted_usd"] != charged or entry.get("receipt", receipt) != receipt:
                    raise ValueError("original budget settlement is immutable")
                return deepcopy(entry)
            before, spent = deepcopy(entry), self.spent_usd
            self.spent_usd += charged - entry["accounted_usd"]
            entry.update(status="settled", accounted_usd=charged, receipt=deepcopy(receipt))
            try:
                self.checkpoint_now()
            except BaseException:
                self.spent_usd = spent
                entry.clear()
                entry.update(before)
                raise
            return deepcopy(entry)
