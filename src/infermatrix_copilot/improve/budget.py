"""The weekly budget envelope, enforced per model call (design §10).

Two hard envelopes per ISO week, persisted under the ledger directory and
serialized across processes: dollars (`improve_budget_usd_week`) and judge
CLI calls (`improve_budget_judge_calls_week`). A call is RESERVED before it
is sent and SETTLED after, so ``settled + reserved <= envelope`` holds at
every instant, concurrent workers and processes included; a reservation
that does not fit raises `BudgetRefused` and the call is never made.

Every reservation is pinned to the week it was taken in (its id carries the
week), and its settlement or release goes to THAT week's file: a call that
spans the weekly boundary neither strands its reservation nor charges a
week whose envelope it never checked.

The reservation is a true upper bound: output tokens are the request's
``max_tokens`` (the API enforces it); input tokens — including cache reads
and cache writes, which are prompt tokens billed at their own rates — are
bounded by the request's UTF-8 byte count for the visible payload. Gateways
can inject hidden prompts; configure their exact models in
``improve_input_context_limits`` with the provider's full context ceiling.
The larger bound is priced at the dearest configured input/cache rate.
Settlement charges the cache-aware cost exactly as `metrics.cost_from_spans`
does. Prices come from the metrics price table; a model with no price cannot
be reserved and is refused, never priced at zero. If a settlement ever
exceeds its reservation the governor records a ``budget_breach`` in the
week's file; every governor instance and process refuses from then on
until an operator clears it (fail closed, not "note and continue").

Named reservations (`reserve_named`/`release_named`) hold an experiment's
planning estimate from registration until it runs, so two registrations
cannot claim the same funds and a forensics pass cannot spend them.

`governed(governor)` binds the governor for the choke points
(`LLM.create`, `judges.run_judge`); with nothing bound, calls are unmetered.
A shadow subprocess binds its own governor from the environment
(`IMPROVE_GOVERNED=1` + `IMPROVE_LEDGER_DIR`) against the same files.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..trace_store import current_store, file_lock
from ..budgeting import bind_call_budget, request_cost_bound, reservation_fits, settlement_charge, usage_cost
from ..persistence import atomic_write_bytes

_GOVERNOR: contextvars.ContextVar["Governor | None"] = contextvars.ContextVar("improve_governor", default=None)
_JUDGE_ACCOUNT = contextvars.ContextVar('improve_judge_account', default=None)


class BudgetRefused(RuntimeError):
    """The reservation does not fit the envelope: the call was not made."""


class BudgetBreach(RuntimeError):
    """A settlement exceeded its reservation: the cycle must abort."""


def iso_week(now: float) -> str:
    d = dt.datetime.fromtimestamp(now, dt.timezone.utc).isocalendar()
    return f"{d[0]}-W{d[1]:02d}"


def request_bytes(kwargs: dict) -> int:
    """The UTF-8 size of everything the request carries (system, messages,
    tool definitions): the prompt-token upper bound."""
    payload = {k: kwargs.get(k) for k in ("system", "messages", "tools")}
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def worst_case_usd(model: str, req_bytes: int, max_tokens: int, settings: Any = None) -> float:
    from ..metrics import CACHE_CREATE_FACTOR, CACHE_READ_FACTOR, model_price, model_price_known

    if not model_price_known(model, settings):
        raise BudgetRefused(f"no price for model {model!r}: it cannot be reserved (never priced at zero)")
    pin, pout = model_price(model, settings)
    limits = getattr(settings, "improve_input_context_limits", {}) or {}
    limit = limits.get(model, 0)
    if type(limit) is not int or limit < 0:
        raise BudgetRefused(f"invalid input context ceiling for model {model!r}")
    input_bound = max(req_bytes, limit)
    read_factor = float(getattr(settings, "cache_read_price_factor", 0) or 0) or CACHE_READ_FACTOR
    input_factor = max(1.0, CACHE_CREATE_FACTOR, read_factor)
    return request_cost_bound(input_bound, pin, max(0, int(max_tokens or 0)), pout,
                              cache_factor=input_factor)


def actual_usd(model: str, usage: dict | None, settings: Any = None) -> float | None:
    """The cache-aware cost of a call, as `metrics.cost_from_spans` prices it."""
    if isinstance(usage, dict) and 'cost_usd' in usage:
        return usage_cost(usage, 0, 0)  # reported amounts do not require a price table
    from ..metrics import CACHE_CREATE_FACTOR, CACHE_READ_FACTOR, model_price

    pin, pout = model_price(model, settings)
    read_f = float(getattr(settings, "cache_read_price_factor", 0) or 0) or CACHE_READ_FACTOR
    return usage_cost(usage, pin, pout, cache_read_factor=read_f, cache_create_factor=CACHE_CREATE_FACTOR)


def record_usd(record: dict, settings: Any = None) -> float | None:
    """Read reported cost or price complete API facts; legacy aliases are not bills."""
    from ..budgeting import normalized_cost
    from ..metrics import model_price_known

    result, usage = record.get('result') or {}, record.get('usage') or {}
    if 'cost_usd' in result:
        return normalized_cost(result['cost_usd'])
    if 'cost_usd' in usage:
        return normalized_cost(usage['cost_usd'])
    identity = record.get('model') or {}
    if (str(identity.get('provider') or '').startswith('harness:')
            or any(key in result for key in ('native_subscription', 'subscription', 'native_attempt_id'))
            or result.get('session')):
        return None  # tokens and account conventions cannot price a native invoice
    model = str(identity.get('served_model') or identity.get('model') or '')
    if not model_price_known(model, settings):
        return None
    return normalized_cost(actual_usd(model, usage, settings))


class Governor:
    def __init__(self, ledger_dir: str | Path, *, usd_week: float, judge_calls_week: int,
                 settings: Any = None, clock=time.time):
        self.dir = Path(ledger_dir) / "budget"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.usd_week = float(usd_week)
        self.judge_calls_week = int(judge_calls_week)
        self.settings = settings
        self._clock = clock
        self.breached = False

    # -- state per week ---------------------------------------------------------------
    def week(self) -> str:
        return iso_week(self._clock())

    def _path(self, week: str) -> Path:
        return self.dir / f"{week}.json"

    def _load(self, week: str) -> dict:
        path = self._path(week)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"week": week, "usd_settled": 0.0, "reservations": {}, "named": {}, "judge_calls": 0,
                "judge_reserved": 0, "breaches": [], "refused": 0}

    def _save(self, week: str, state: dict) -> None:
        path = self._path(week)
        atomic_write_bytes(path, json.dumps(state, indent=1).encode("utf-8"))

    @contextmanager
    def _locked(self, week: str, *, write=True) -> Iterator[dict]:
        with file_lock(self.dir / "budget.lock", blocking=True, timeout=30.0) as held:
            if not held:
                raise BudgetRefused("the budget ledger is locked by another process")
            state = self._load(week)
            try:
                yield state
            finally:
                if write:
                    self._save(week, state)   # a refusal counted before the raise is state too

    @staticmethod
    def _reserved(state: dict) -> float:
        return sum(float(v["usd"]) for v in state["reservations"].values()) + \
            sum(float(v) for v in state["named"].values())

    def _stop_if_breached(self, state: dict) -> None:
        if state["breaches"] or self.breached:
            self.breached = True
            raise BudgetRefused("the budget governor is breached (recorded for this week); no further calls until cleared")

    def remaining(self, week: str | None = None) -> dict:
        week = week or self.week()
        state = self._load(week)
        reserved = self._reserved(state)
        return {"week": week, "usd_envelope": self.usd_week, "usd_settled": round(state["usd_settled"], 6),
                "usd_reserved": round(reserved, 6), "named": {k: round(float(v), 6) for k, v in state["named"].items()},
                "usd_remaining": round(self.usd_week - state["usd_settled"] - reserved, 6),
                "judge_calls_envelope": self.judge_calls_week, "judge_calls_used": state["judge_calls"],
                "judge_calls_reserved": state["judge_reserved"],
                "judge_calls_remaining": self.judge_calls_week - state["judge_calls"] - state["judge_reserved"],
                "breached": bool(state["breaches"]) or self.breached, "refused": state["refused"]}

    # -- dollars ------------------------------------------------------------------------
    def reserve_call(self, model: str, req_bytes: int, max_tokens: int, *, purpose: str = "") -> str:
        """Reserve the worst case for one model call in the current week;
        the id pins the week. Raises BudgetRefused."""
        est = worst_case_usd(model, req_bytes, max_tokens, self.settings)
        return self.reserve_amount(model, est, purpose=purpose)

    def reserve_amount(self, model: str, est: float, *, purpose: str = '', native_binding=None) -> str:
        """Reserve a domain-attested bound through the original dollar journal."""
        from ..budgeting import normalized_cost
        est = normalized_cost(est)
        if est is None:
            raise BudgetRefused('invalid dollar reservation bound')
        week = self.week()
        with self._locked(week) as state:
            self._stop_if_breached(state)
            if not reservation_fits(self.usd_week, state["usd_settled"], self._reserved(state), est):
                state["refused"] += 1
                raise BudgetRefused(
                    f"reserving ${est:.4f} for {model} would exceed the weekly envelope "
                    f"(${self.usd_week:.2f}; settled ${state['usd_settled']:.4f}, reserved ${self._reserved(state):.4f})")
            rid = f"{week}|{uuid.uuid4().hex[:10]}"
            state["reservations"][rid] = {"usd": est, "model": model, "at": self._clock(), "purpose": purpose}
            if native_binding is not None:
                state['reservations'][rid]['native_binding'] = dict(native_binding)
        return rid

    @staticmethod
    def _week_of(reservation_id: str) -> str:
        return reservation_id.split("|", 1)[0]

    def _finish_call(self, reservation_id, actual, model, outcome, *, native_binding=None):
        week = self._week_of(reservation_id)
        breached = False
        with self._locked(week) as state:
            receipts = state.setdefault("settlements", {})
            identity = {"actual": actual, "model": model, "outcome": outcome}
            previous = receipts.get(reservation_id)
            if previous is not None:
                if native_binding is not None and previous.get('native_binding') != native_binding:
                    raise BudgetRefused('original settlement binding changed during recovery')
                if any(previous[key] != value for key, value in identity.items()):
                    raise BudgetRefused("reservation already has a different settlement")
                if previous["breached"]:
                    raise BudgetBreach("settlement exceeded its reservation: cycle aborted")
                return previous["charged"]
            reservation = state["reservations"].get(reservation_id)
            if reservation is None:
                raise BudgetRefused("unknown reservation: no settlement authority")
            if native_binding is not None and reservation.get('native_binding') != native_binding:
                raise BudgetRefused('original reservation binding changed during recovery')
            if model and model != reservation["model"]:
                raise BudgetRefused("settlement model differs from reservation")
            charged = 0.0 if outcome == "released" else settlement_charge(reservation["usd"], actual, outcome=outcome)
            breached = charged > float(reservation["usd"]) + 1e-9
            receipts[reservation_id] = {**identity, "charged": charged, "breached": breached}
            if 'native_binding' in reservation:
                receipts[reservation_id]['native_binding'] = reservation['native_binding']
            del state["reservations"][reservation_id]
            state["usd_settled"] += charged
            if breached:
                state["breaches"].append({"reservation": reservation_id, "reserved": reservation["usd"],
                                          "actual": charged, "model": model, "at": self._clock()})
        if breached:
            self.breached = True
            self._record_breach(reservation_id, model, charged)
            raise BudgetBreach(f"settlement ${charged:.4f} exceeded its reservation for {model}: cycle aborted")
        return charged

    def settle_call(self, reservation_id: str, usage: dict | None, model: str) -> float:
        """Settle once in the reserved week; absent usage retains the full amount."""
        return self._finish_call(reservation_id, actual_usd(model, usage, self.settings), model, "completed")

    def release_call(self, reservation_id: str) -> None:
        """Drop a reservation for a call that failed BEFORE it was sent."""
        self._finish_call(reservation_id, None, "", "released")

    def forfeit_call(self, reservation_id: str) -> float:
        """Retain the whole reservation when dispatch may have been billed."""
        return self._finish_call(reservation_id, None, "", "unknown")

    def reconcile_native(self, reservation_id: str, *, offline_confirmed=False):
        """Settle only the original bound terminal record, without model dispatch."""
        from ..budgeting import normalized_cost
        import re
        if not isinstance(reservation_id, str) or not re.fullmatch(r'\d{4}-W\d{2}\|[0-9a-f]{10}', reservation_id):
            raise BudgetRefused('invalid original native reservation identity')
        week = self._week_of(reservation_id)
        with self._locked(week, write=False) as state:
            authority = state['reservations'].get(reservation_id) or state.get('settlements', {}).get(reservation_id)
            binding = (authority or {}).get('native_binding')
            if not isinstance(binding, dict):
                raise BudgetRefused('original reservation has no native binding; it cannot be reinterpreted')
            binding = dict(binding)
        with self._native_outcome(binding, offline_confirmed=offline_confirmed) as (record, attempt):
            result = record.get('result', {})
            sent, amount = result['native_call_sent'], result.get('cost_usd')
            if amount is not None and normalized_cost(amount) is None:
                raise BudgetRefused('original native amount is invalid')
            charged = self._finish_call(reservation_id, amount if sent else None,
                binding['model']['model'] if sent else '', 'completed' if sent else 'released', native_binding=binding)
            return {'reservation_id': reservation_id, 'trace_id': record['id'],
                    'native_attempt_id': attempt['id'], 'native_cost_usd': amount, 'accounted_usd': charged}

    @staticmethod
    @contextmanager
    def _native_outcome(binding, *, offline_confirmed):
        """Validate one original receipt for either domain account policy."""
        from ..trace_store import TraceStore
        if binding.get('schema') != 'native-budget-binding/1':
            raise BudgetRefused('original reservation has an unsupported native binding schema')
        root = Path(binding['trace_root'])
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise BudgetRefused('original native trace store is unavailable')
        store = TraceStore(root)
        with store._migration_lock():
            store._require_paused(None, offline_confirmed)
            records = [record for record, _ in store.iter_records(kind='model_call')
                       if record.get('result', {}).get('native_attempt_id') == binding['attempt_id']]
            if len(records) != 1:
                raise BudgetRefused('original native outcome is missing or ambiguous; keep reservation pending')
            record = records[0]
            attempt = store.validate_native_attempt(record, allow_failed=True)
            if attempt['id'] != binding['attempt_id'] \
                    or any(attempt.get(key) != binding.get(key) for key in ('model', 'context', 'env', 'inputs')):
                raise BudgetRefused('original native outcome differs from reservation binding')
            result = record.get('result', {})
            sent, pending = result.get('native_call_sent'), result.get('native_outcome_pending')
            if type(sent) is not bool or type(pending) is not bool or pending:
                raise BudgetRefused('original native dispatch is unknown or pending; no replacement is authorized')
            yield record, attempt

    # -- named (planned) reservations -----------------------------------------------------
    def reserve_named(self, name: str, usd: float) -> None:
        """Hold ``usd`` under ``name`` (an experiment's planning estimate)
        in the current week until released; refused when it does not fit."""
        week = self.week()
        with self._locked(week) as state:
            self._stop_if_breached(state)
            already = float(state["named"].get(name, 0.0))
            if not reservation_fits(self.usd_week, state["usd_settled"], max(0, self._reserved(state) - already), float(usd)):
                state["refused"] += 1
                raise BudgetRefused(f"holding ${float(usd):.2f} for {name} would exceed the weekly envelope")
            state["named"][name] = float(usd)

    def release_named(self, name: str, week: str | None = None) -> float:
        """Release a named hold (the experiment now reserves per call)."""
        with self._locked(week or self.week()) as state:
            return float(state["named"].pop(name, 0.0))

    # -- judge calls ----------------------------------------------------------------------
    def reserve_judge_call(self) -> str:
        """Reserve one judge call; the token retains its original ISO week."""
        week = self.week()
        token = f"{week}|judge|{uuid.uuid4().hex[:8]}"
        with self._locked(week) as state:
            self._stop_if_breached(state)
            if not reservation_fits(self.judge_calls_week, state["judge_calls"], state["judge_reserved"], 1, tolerance=0):
                state["refused"] += 1
                raise BudgetRefused(f"the weekly judge-call envelope ({self.judge_calls_week}) is exhausted")
            state["judge_reserved"] += 1
            state.setdefault("judge_tokens", {})[token] = "reserved"
        return token

    def bind_judge_call(self, token: str, native_binding: dict) -> None:
        """Bind the existing count reservation before dispatch; never replace it."""
        if not isinstance(native_binding, dict) or native_binding.get('schema') != 'native-budget-binding/1':
            raise BudgetRefused('judge reservation requires an original durable attempt binding')
        with self._locked(self._week_of(token)) as state:
            if state.get('judge_tokens', {}).get(token) != 'reserved':
                raise BudgetRefused('judge reservation is not pending')
            bindings = state.setdefault('judge_bindings', {})
            if token in bindings and bindings[token] != native_binding:
                raise BudgetRefused('original judge reservation already has a different binding')
            bindings[token] = dict(native_binding)

    def settle_judge_call(self, token: str | None = None, *, native_binding=None) -> None:
        week = self._week_of(token) if token else self.week()
        with self._locked(week) as state:
            tokens = state.setdefault("judge_tokens", {})
            if token is None:
                token = next((key for key, value in tokens.items() if value == "reserved"), None)
                if token is None and state["judge_reserved"] > 0:
                    # Legacy files recorded only the count, without token identities.
                    state["judge_reserved"] -= 1
                    state["judge_calls"] += 1
                    return
            if token not in tokens:
                raise BudgetRefused("unknown judge reservation")
            if native_binding is not None and state.get('judge_bindings', {}).get(token) != native_binding:
                raise BudgetRefused('original judge reservation binding changed during recovery')
            if tokens[token] == "settled":
                return
            tokens[token] = "settled"
            state["judge_reserved"] -= 1
            state["judge_calls"] += 1

    def reconcile_judge(self, token: str, *, offline_confirmed=False):
        """Count the original terminal judge attempt once, without another call."""
        import re
        if not isinstance(token, str) or not re.fullmatch(r'\d{4}-W\d{2}\|judge\|[0-9a-f]{8}', token):
            raise BudgetRefused('invalid original judge reservation identity')
        with self._locked(self._week_of(token), write=False) as state:
            binding = state.get('judge_bindings', {}).get(token)
            if state.get('judge_tokens', {}).get(token) not in ('reserved', 'settled') or not isinstance(binding, dict):
                raise BudgetRefused('original judge reservation has no native binding; it cannot be reinterpreted')
            binding = dict(binding)
        with self._native_outcome(binding, offline_confirmed=offline_confirmed) as (record, attempt):
            self.settle_judge_call(token, native_binding=binding)
            return {'reservation_id': token, 'trace_id': record['id'],
                    'native_attempt_id': attempt['id'], 'accounted_judge_calls': 1}

    def clear_breach(self, week: str | None = None) -> None:
        """Operator action: forget the week's breach records."""
        with self._locked(week or self.week()) as state:
            state["breaches"] = []
        self.breached = False

    def _record_breach(self, reservation_id: str, model: str, actual: float) -> None:
        store = current_store()
        if store is None:
            return
        try:
            store.append("decision", context={"playbook": "workflow-improve"},
                         result={"type": "incident", "kind": "budget_breach", "reservation": reservation_id,
                                 "model": model, "actual_usd": actual, "severity": "severe"})
        except Exception:  # noqa: BLE001 - the week's file already holds the breach
            pass


@dataclass
class NativeReservation:
    model: str
    reservation: str | None = None
    prepare_required: bool = True
    capture_root: str = ''


@dataclass
class JudgeReservation:
    token: str
    capture_root: str
    prepare_required: bool = True
    bound: bool = False


@contextmanager
def judge_governed(governor):
    """Declare the existing CLI judge-count policy, without claiming an invoice."""
    token = _JUDGE_ACCOUNT.set(governor)
    def acquire(request):
        reservation = governor.reserve_judge_call()
        if not isinstance(governor, Governor):
            return reservation  # legacy host adapters retain their count-only contract
        store = current_store()
        root = store.root if store else getattr(governor.settings, 'trace_store_root', '') or governor.dir / 'native-traces'
        return JudgeReservation(reservation, str(Path(root).expanduser().resolve()))

    def prepare(ticket, request, execution):
        if isinstance(ticket, JudgeReservation):
            binding = execution.get('native_binding')
            if not isinstance(binding, dict) or not binding.get('attempt_id') or not binding.get('trace_root'):
                raise BudgetRefused('judge reservation requires an original durable attempt binding')
            governor.bind_judge_call(ticket.token, binding)
            ticket.bound = True
        return {}

    def finish(ticket, facts):
        if isinstance(ticket, JudgeReservation):
            if getattr(facts, 'native_outcome_pending', False) or (ticket.bound and facts['sent']
                    and not getattr(facts, 'native_receipt_complete', False)):
                return
            ticket = ticket.token
        governor.settle_judge_call(ticket)
    try:
        with bind_call_budget('improve-judge', acquire if governor else None, finish, prepare=prepare):
            yield
    finally:
        _JUDGE_ACCOUNT.reset(token)


@contextmanager
def governed(governor: "Governor | None") -> Iterator["Governor | None"]:
    def acquire(request):
        if request.get("kind", "api") != "api":
            root = getattr(governor.settings, 'trace_store_root', '') or str(governor.dir / 'native-traces')
            return None if _JUDGE_ACCOUNT.get() is governor else NativeReservation(
                request.get('model') or '', capture_root=str(Path(root).expanduser().resolve()))
        model = request["model"]
        return (governor.reserve_call(model, request_bytes(request),
                                      int(request.get("max_tokens") or 0),
                                      purpose=request.get("role") or ""), model)

    def prepare(ticket, request, execution):
        if not isinstance(ticket, NativeReservation):
            return {}
        if execution.get('subscription_admitted') is True:
            return {}  # domain waiver, not an assertion of zero native cost
        provider = execution.get('provider_id') or ''
        limits = getattr(governor.settings, 'improve_native_call_limits', {}) or {}
        limit = limits.get(f'{provider}:{ticket.model}')
        if not limit:
            raise BudgetRefused('native dollar account requires an exact provider/model stop and overshoot bound')
        if execution.get('stops_at_spend') is not True:
            raise BudgetRefused('native execution cannot enforce the dollar account stop threshold')
        from ..budgeting import normalized_cost
        threshold, overshoot = normalized_cost(limit.get('stop_usd')), normalized_cost(limit.get('overshoot_usd'))
        if threshold is None or threshold <= 0 or overshoot is None or overshoot <= 0:
            raise BudgetRefused('invalid native dollar account bounds')
        if 'max_budget_usd' in request:
            requested = normalized_cost(request['max_budget_usd'])
            if requested is None or requested <= 0:
                raise BudgetRefused('invalid requested native stop threshold')
            threshold = min(threshold, requested)
        binding = execution.get('native_binding')
        if not isinstance(binding, dict) or not binding.get('attempt_id') or not binding.get('trace_root'):
            raise BudgetRefused('native dollar reservation requires an original durable attempt binding')
        ticket.reservation = governor.reserve_amount(ticket.model, threshold + overshoot,
                                                      purpose=request.get('role') or '', native_binding=binding)
        return {'max_budget_usd': threshold}

    def finish(ticket, facts):
        if ticket is None:
            return
        native = isinstance(ticket, NativeReservation)
        if native:
            if getattr(facts, 'native_outcome_pending', False):
                return  # original binding and funds remain pending for offline reconciliation
            reservation, model = ticket.reservation, ticket.model
            if reservation is None:
                return
        else:
            reservation, model = ticket
        usage = facts["usage"]
        if native:
            if not facts['sent']:
                governor.release_call(reservation)
                return
            # Native token counts and missing cost reports cannot establish USD.
            from ..budgeting import normalized_cost
            amount = normalized_cost((usage or {}).get('cost_usd'))
            governor.settle_call(reservation, {'cost_usd': amount}, model)
        elif usage is not None:
            governor.settle_call(reservation, usage, model)
        elif facts["sent"]:
            governor.forfeit_call(reservation)
        else:
            governor.release_call(reservation)

    token = _GOVERNOR.set(governor)
    try:
        with bind_call_budget("improve-weekly", acquire if governor else None, finish, prepare=prepare):
            yield governor
    finally:
        _GOVERNOR.reset(token)


def current_governor() -> "Governor | None":
    return _GOVERNOR.get()
