"""Shared amount rules and reservation lifetime; storage and policy stay local."""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import math


_CALL_BUDGETS = ContextVar("model_call_budgets", default={})
_TOKEN_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_read_tokens",
                     "cache_creation_input_tokens", "cache_creation_tokens")


def normalized_cost(value):
    """One reported currency amount; missing or invalid is unknown, not zero."""
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def native_outcome_unknown(error):
    """An unresolved original execution cannot authorize a replacement call."""
    return bool(getattr(error, 'native_outcome_pending', False)
                or (getattr(error, 'native_call_sent', False)
                    and not getattr(error, 'native_call_completed', False)))


def copy_native_outcome(target, source, *, assume_sent=False):
    """Preserve dispatch and original-execution facts through error adapters."""
    target.native_call_sent = getattr(source, 'native_call_sent', assume_sent)
    target.native_call_completed = getattr(source, 'native_call_completed', False)
    target.native_outcome_pending = getattr(source, 'native_outcome_pending', False)
    return target


def request_cost_bound(input_bytes, input_price, output_tokens, output_price, *, cache_factor=1.25, threshold=0.0):
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
           for v in (input_bytes, input_price, output_tokens, output_price, cache_factor, threshold)):
        raise ValueError("cost bounds require finite nonnegative amounts")
    return threshold + (input_bytes * cache_factor * input_price + output_tokens * output_price) / 1_000_000


def reservation_fits(limit, settled, reserved, requested, *, tolerance=1e-12):
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
           for v in (settled, reserved, requested)):
        raise ValueError("budget amounts must be finite and nonnegative")
    return limit is None or settled + reserved + requested <= limit + tolerance


def settlement_charge(reserved, actual=None, *, outcome="completed", fixed_accounting=False):
    known = not isinstance(actual, bool) and isinstance(actual, (int, float)) and math.isfinite(actual) and actual >= 0
    if outcome != "completed" or fixed_accounting or not known:
        return max(reserved, actual if known else 0)
    # Domains may account in integer micro-units. Do not round those through
    # binary floating point when carrying a confirmed cost into their journals.
    return actual


def valid_token_counts(*counts):
    return all(type(value) is int and value >= 0 for value in counts)


def usage_observation(usage):
    """Project only pricing facts, retaining unknowns and cache identity safely."""
    values = usage if isinstance(usage, dict) else {
        key: getattr(usage, key) for key in (*_TOKEN_USAGE_KEYS, 'input_tokens_include_cache', 'cost_usd')
        if hasattr(usage, key)}
    result = {key: values[key] if valid_token_counts(values[key]) else None
              for key in _TOKEN_USAGE_KEYS if key in values}
    if 'input_tokens_include_cache' in values:
        flag = values['input_tokens_include_cache']
        result['input_tokens_include_cache'] = flag if type(flag) is bool else None
    if 'cost_usd' in values:
        result['cost_usd'] = normalized_cost(values['cost_usd'])
    return result


def usage_cost(usage, input_price, output_price, *, cache_read_factor=.1, cache_create_factor=1.25):
    """Prefer reported cost, otherwise price complete tokens; missing facts stay unknown."""
    if isinstance(usage, dict) and 'cost_usd' in usage:
        return normalized_cost(usage['cost_usd'])
    if not isinstance(usage, dict) or not valid_token_counts(usage.get("input_tokens"), usage.get("output_tokens")) \
            or not valid_token_counts(*(usage[key] for key in _TOKEN_USAGE_KEYS if key in usage)):
        return None
    read = usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0
    create = usage.get("cache_creation_input_tokens") or usage.get("cache_creation_tokens") or 0
    uncached = usage["input_tokens"]
    if "input_tokens_include_cache" in usage:
        if type(usage["input_tokens_include_cache"]) is not bool:
            return None
        if usage["input_tokens_include_cache"]:
            if not any(key in usage for key in ("cache_read_input_tokens", "cache_read_tokens")) \
                    or not any(key in usage for key in ("cache_creation_input_tokens", "cache_creation_tokens")):
                return None  # an unknown cache split cannot establish a price
            uncached -= read + create
            if uncached < 0:
                return None
    return (input_price * (uncached + read * cache_read_factor + create * cache_create_factor)
            + output_price * usage["output_tokens"]) / 1_000_000


@contextmanager
def reserved_call(acquire, finish):
    """Acquire durably before yielding; always finalize, with execution outside locks.

    Callers mark ``sent`` immediately before dispatch and record a trusted
    ``actual_usd``/``outcome`` as soon as billing is known. The finish callback
    owns atomic, idempotent persistence and its domain's refusal exceptions.
    """
    call = {"reservation": acquire(), "sent": False, "actual_usd": None, "outcome": "unknown"}
    try:
        yield call
    finally:
        finish(call)


@contextmanager
def bind_call_budget(key, acquire, finish, *, prepare=None):
    """Bind one account without changing its storage, prices or refund policy.

    An inner binding replaces the same key instead of charging it twice.
    Passing ``acquire=None`` temporarily removes that account.
    """
    bindings = dict(_CALL_BUDGETS.get())
    if acquire is None:
        bindings.pop(key, None)
    else:
        bindings[key] = (acquire, finish, prepare)
    token = _CALL_BUDGETS.set(bindings)
    try:
        yield
    finally:
        _CALL_BUDGETS.reset(token)


class CallBudgetFacts(dict):
    """Dispatch observations and one mechanical execution-preparation boundary."""

    def __init__(self):
        super().__init__(sent=False, reply=None, usage=None, outcome='unknown')
        self._preparations = []
        self._required = self._prepared = self._attempted = False

    def __setitem__(self, key, value):
        if key == 'sent' and value and self._required and not self._prepared:
            raise RuntimeError('execution budgets require preparation before dispatch')
        super().__setitem__(key, value)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    @property
    def native_archive_root(self):
        roots = {str(ticket.capture_root) for _, ticket, _ in self._preparations
                 if getattr(ticket, 'capture_root', None)}
        if len(roots) > 1:
            raise RuntimeError('execution accounts require an explicit shared trace store')
        return next(iter(roots), None)

    def prepare(self, execution):
        if self._attempted:
            raise RuntimeError('execution budget preparation cannot be repeated')
        self._attempted = True
        updates = {}
        try:
            for prepare, ticket, request in self._preparations:
                for key, value in (prepare(ticket, request, execution) or {}).items():
                    if key in updates and updates[key] != value:
                        raise RuntimeError('execution account policies conflict')
                    updates[key] = value
        except BaseException as exc:
            exc.call_budget_failure = True
            raise
        self._prepared = True
        return updates


@contextmanager
def call_budget(request):
    """Reserve bound accounts before dispatch and report facts to each owner.

    No model selection, billing or recovery policy lives here. Owners receive
    the actual dispatch flag and unmodified usage, including unknown usage.
    Every acquired account is finalized even if another refuses or fails.
    """
    facts = CallBudgetFacts()
    with ExitStack() as stack:
        for acquire, finish, prepare in _CALL_BUDGETS.get().values():
            ticket = acquire(request)
            stack.callback(finish, ticket, facts)
            facts._required |= bool(getattr(ticket, 'prepare_required', False))
            if getattr(ticket, 'prepare_required', False) and prepare is None:
                raise RuntimeError('required execution budget has no preparation callback')
            if prepare is not None:
                facts._preparations.append((prepare, ticket, request))
        yield facts
