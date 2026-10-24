"""Mixture-of-Agents support (design W6): member resolution, the atomic
budget ledger, and eligibility.

MoA runs lens proposers (PR review) or draft proposers (issue answering) on
heterogeneous `LLM_MIXTURE` members while the verify-and-merge reducer /
aggregator stays on the run's tier model. Guarantees, enforced HERE and not
in prompt-land:

- **Cap**: every member request must atomically `reserve()` a conservative
  upper-bound cost first (UTF-8 request bytes plus max_tokens output and
  the cache-creation surcharge). Unknown paid usage retains that reservation;
  a reported overrun is recorded in full and stops further member calls.
  Reservation failure ⇒ the call runs on the tier
  model (PR) or the member is skipped (issue) — never an uncapped request.
- **Deadline**: per-request timeout = min(member timeout, remaining overall
  deadline); an expired deadline fails reservation.
- **Secrets**: member identity is `model@host` everywhere; api keys come from
  `api_key_env` (an env-var NAME) or a raw `api_key` field, and neither is
  ever logged/traced.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ...budgeting import bind_call_budget, request_cost_bound, reservation_fits, settlement_charge, usage_cost
from ...metrics import CACHE_CREATE_FACTOR, CACHE_READ_FACTOR, model_price


@dataclass(frozen=True)
class Member:
    model: str
    base_url: str = ""
    api_key: str = ""      # resolved secret — NEVER rendered; see label()
    provider: str = ""     # "" = raw API; else a harness provider id (cursor)

    def label(self) -> str:
        if self.provider:
            return f"{self.model}@{self.provider}"
        host = urlparse(self.base_url).netloc if self.base_url else "default"
        return f"{self.model}@{host}"


def resolve_members(settings: Any) -> list[Member]:
    """Usable members from `settings.llm_mixture`: schema-checked, secrets
    resolved via `api_key_env`, capped at `moa_max_members`, and REJECTED
    (with a warning) when the model has no price entry — an unpriced member
    would make the budget cap unenforceable. <2 usable members ⇒ MoA off."""
    import logging

    from ...metrics import MODEL_PRICES

    log = logging.getLogger("infermatrix_copilot")
    has_override = (float(getattr(settings, "token_price_in_per_mtok", 0)) > 0
                    or float(getattr(settings, "token_price_out_per_mtok", 0)) > 0)
    out: list[Member] = []
    for raw in (settings.llm_mixture or {}).get("members") or []:
        if not isinstance(raw, dict) or not raw.get("model"):
            continue
        model = str(raw["model"])
        provider = str(raw.get("provider") or "")
        if provider:
            # Harness sessions are reported separately from the API USD cap;
            # kind does not attest zero spend (DeepSeek is API-keyed).
            # Sessions stay bounded by strict_backend_timeout_s.
            # Reject unshipped/unknown ids here so
            # a typo degrades to "member skipped", never a mid-run raise.
            try:
                from ...providers.registry import transport_for_id

                transport_for_id(settings, provider)
            except (ValueError, NotImplementedError) as exc:
                log.warning("MoA member %s@%s rejected: %s",
                            model, provider, exc)
                continue
            out.append(Member(model=model, provider=provider))
            if len(out) >= int(getattr(settings, "moa_max_members", 4)):
                break
            continue
        # unpriced member -> cap unenforceable -> reject at parse (W6)
        if not has_override and not any(k in model.lower()
                                        for k, _ in MODEL_PRICES):
            log.warning("MoA member %s has no MODEL_PRICES entry — rejected "
                        "(cap must stay enforceable)", model)
            continue
        key = ""
        if raw.get("api_key_env"):
            key = os.environ.get(str(raw["api_key_env"]), "")
        elif raw.get("api_key"):
            key = str(raw["api_key"])
        out.append(Member(model=model, base_url=str(raw.get("base_url") or ""),
                          api_key=key))
        if len(out) >= int(getattr(settings, "moa_max_members", 4)):
            break
    return out


def moa_eligible(settings: Any, *, kind: str, mode: str,
                 depth: str = "") -> bool:
    """Per-kind eligibility (design W6): `full` ⇒ PR reviews at full depth or
    performance mode; issues (no depth concept) in performance mode.
    `performance` ⇒ both kinds, performance mode only. `always` ⇒ every run."""
    when = str(getattr(settings, "moa_when", "off"))
    if when == "off":
        return False
    if when == "always":
        return True
    if kind == "pr_review":
        if when == "full":
            return depth == "full" or mode == "performance"
        return mode == "performance"          # when == "performance"
    if kind == "issue_answer":
        return mode == "performance"          # both "full" and "performance"
    return False


@dataclass
class MoaBudget:
    """Thread-safe atomic reservation ledger (W6). `reserve()` is the
    serialization point — concurrent members cannot enter on a stale
    remaining-spend read. Receipts prevent duplicate or conflicting charges."""

    max_usd: float
    deadline: float                      # monotonic absolute deadline
    member_timeout_s: float
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _reserved: dict[int, float] = field(default_factory=dict)
    _settled: float = 0.0
    _receipts: dict[int, tuple[float | None, float]] = field(default_factory=dict)
    _breached: bool = False
    _next_id: int = 0
    tripped: bool = False
    _settings: Any = field(default=None, repr=False)

    @classmethod
    def start(cls, settings: Any) -> "MoaBudget":
        return cls(max_usd=float(settings.moa_max_usd),
                   deadline=time.monotonic() + float(settings.moa_deadline_s),
                   member_timeout_s=float(settings.moa_member_timeout_s), _settings=settings)

    def request_timeout(self) -> float:
        """min(member timeout, remaining overall deadline); <=0 ⇒ expired."""
        return min(self.member_timeout_s, self.deadline - time.monotonic())

    def estimate(self, member: Member, request_chars: int,
                 max_tokens: int) -> float:
        """Conservative input-token bound, including cache creation."""
        p_in, p_out = model_price(member.model, self._settings)
        return request_cost_bound(request_chars, p_in, max_tokens, p_out,
                                  cache_factor=CACHE_CREATE_FACTOR)

    def reserve(self, est_usd: float) -> int | None:
        """Atomically reserve `est_usd`; None when the cap/deadline refuses
        (caller falls back per the per-kind fallback rules)."""
        if self.request_timeout() <= 0:
            return None
        with self._lock:
            if self._breached or not reservation_fits(self.max_usd, self._settled,
                                                     sum(self._reserved.values()), est_usd):
                self.tripped = True
                return None
            rid = self._next_id
            self._next_id += 1
            self._reserved[rid] = est_usd
            return rid

    def settle(self, rid: int, actual_usd: float | None) -> None:
        """Record the full charge once; unknown paid usage retains its bound."""
        with self._lock:
            if rid in self._receipts:
                previous, _ = self._receipts[rid]
                if actual_usd != previous:
                    raise MoaBudgetExceeded("different settlement for MoA reservation")
                return
            if rid not in self._reserved:
                raise MoaBudgetExceeded("unknown MoA reservation")
            reserved = self._reserved.pop(rid)
            charged = settlement_charge(reserved, actual_usd)
            self._receipts[rid] = (actual_usd, charged)
            self._settled += charged
            if charged > reserved:
                self.tripped = self._breached = True

    def release(self, rid: int) -> None:
        """Only a request that was never dispatched can release its bound."""
        self.settle(rid, 0.0)

    def bind(self, member: Member):
        """Bind this domain's account to real API dispatch, without wrapping LLMs."""
        def acquire(request):
            if request.get("kind") != "api" or request.get("model") != member.model:
                return None
            payload = {key: request.get(key) for key in ("system", "messages", "tools")}
            size = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
            rid = self.reserve(self.estimate(member, size, int(request["max_tokens"])))
            if rid is None:
                raise MoaBudgetExceeded(f"MoA budget/deadline refused {member.label()}")
            return rid

        def finish(rid, facts):
            if rid is None:
                return
            if not facts["sent"]:
                self.release(rid)
            else:
                pin, pout = model_price(member.model, self._settings)
                self.settle(rid, usage_cost(facts["usage"], pin, pout,
                            cache_read_factor=float(getattr(self._settings, "cache_read_price_factor", 0) or 0)
                            or CACHE_READ_FACTOR, cache_create_factor=CACHE_CREATE_FACTOR))

        return bind_call_budget(("moa", id(self)), acquire, finish)

    def spent(self) -> float:
        with self._lock:
            return self._settled


class MoaBudgetExceeded(RuntimeError):
    """The member's account refused a dispatch or conflicting settlement."""
