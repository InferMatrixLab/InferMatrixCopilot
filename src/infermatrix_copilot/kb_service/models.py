"""Pinned model roles with an explicitly configured generator fallback.

The generator drafts knowledge changes; the judge (a different model family)
grades them. Both are pinned by (provider, model, reasoning effort) and
recorded on every call. ``KB_GENERATOR_FALLBACK`` may name one pinned model
to try when generation is unavailable. Judges never fall back, and schema
repair and spend limits retain their existing behavior. If both generators
are unavailable, the caller leaves the work queued.

``KB_JUDGE_FAMILY_WAIVER`` (a truthy value) is the explicit operator opt-in
that lets the judge share a model family with a generator — a deployment
where the same backend both drafts and grades (e.g. zcode GLM on both
sides). The waiver is logged on every start; the calibration set is the
compensating control, so it must be re-run for the waivered judge.
"""

from __future__ import annotations

from .model_identity import model_family

from ..budgeting import copy_native_outcome, native_outcome_unknown

import json
import logging
import os
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from typing import Any, Callable

GENERATOR_ENV = "KB_GENERATOR"   # "provider:model[:effort]"
GENERATOR_FALLBACK_ENV = "KB_GENERATOR_FALLBACK"
JUDGE_ENV = "KB_JUDGE"
JUDGE_FAMILY_WAIVER_ENV = "KB_JUDGE_FAMILY_WAIVER"
DEFAULT_GENERATOR = "claude-code:claude-opus-5-5"
DEFAULT_JUDGE = "codex:gpt-6-sol:medium"
logger = logging.getLogger(__name__)


class ModelUnavailable(RuntimeError):
    """The pinned backend could not produce a usable reply; work stays queued."""

    def __init__(self, message: str, *, allow_fallback: bool = True):
        super().__init__(message)
        self.allow_fallback = allow_fallback


@dataclass(frozen=True)
class ModelRole:
    name: str
    provider: str
    model: str
    effort: str = ""
    fallback: ModelRole | None = None

    @classmethod
    def parse(cls, name: str, spec: str) -> "ModelRole":
        parts = spec.split(":")
        if len(parts) not in (2, 3) or not all(parts[:2]):
            raise ValueError(f"{name} must be provider:model[:effort], got {spec!r}")
        return cls(name, parts[0], parts[1], parts[2] if len(parts) == 3 else "")

    def label(self) -> str:
        return f"{self.provider}:{self.model}" + (f":{self.effort}" if self.effort else "")


def roles_from_env() -> tuple[ModelRole, ModelRole]:
    generator = ModelRole.parse("generator", os.environ.get(GENERATOR_ENV, DEFAULT_GENERATOR))
    judge = ModelRole.parse("judge", os.environ.get(JUDGE_ENV, DEFAULT_JUDGE))
    fallback_spec = os.environ.get(GENERATOR_FALLBACK_ENV, "").strip()
    if fallback_spec:
        fallback = ModelRole.parse("generator", fallback_spec)
        if fallback.label().casefold() == generator.label().casefold():
            raise ValueError("the generator fallback must differ from the primary generator")
        generator = replace(generator, fallback=fallback)
    waived = os.environ.get(JUDGE_FAMILY_WAIVER_ENV, "").strip().lower() in ("1", "true", "yes")
    for candidate in (generator, generator.fallback):
        if candidate is not None and candidate.provider == judge.provider \
                and model_family(candidate.model) == model_family(judge.model):
            if waived:
                logger.warning(
                    "KB_JUDGE_FAMILY_WAIVER is set: judge %s grades drafts from the same "
                    "model family as %s; keep the calibration set current",
                    judge.label(), candidate.label())
                break
            raise ValueError("the judge must come from a different model family than every generator")
    return generator, judge


@dataclass(frozen=True)
class ModelReply:
    role: ModelRole
    data: dict
    text: str
    served_model: str
    usage: dict
    seconds: float
    cost_usd: float | None = None  # the transport's reported spend (None: unknown)
    trace_id: str = ""
    reply_sha256: str = ""
    quality_receipt: dict = field(default_factory=dict)


def parse_json_object(text: str) -> dict:
    """The reply's JSON object: a bare object, or the last fenced ```json block.
    Raw control characters inside strings (a newline in a multi-line value,
    e.g. a diagram) are accepted: models write them, and they are data, not
    structure (``strict=False``)."""
    from ..structured_output import decode_json_object

    try:
        return decode_json_object(text, policy="last_fence", strict=False)
    except ValueError as exc:
        raise ModelUnavailable("reply is not a JSON object") from exc


class ModelGateway:
    """One-shot structured calls through the provider registry's harness
    transports. ``transport_factory(provider_id)`` is injectable for tests."""

    def __init__(self, settings, *, transport_factory: Callable[[str], Any] | None = None,
                 recorder: Callable[[dict], None] | None = None, zcode_pacer=None):
        self._settings = settings
        self._factory = transport_factory
        self._recorder = recorder
        self._subscription_transports: dict[str, Any] = {}
        self._zcode_pacer = zcode_pacer
        self._dispatch = None

    def configure_dispatch(self, dispatch):
        """Portable initialization opts into one shared native dispatch cap."""
        self._dispatch = dispatch

    def configure_zcode_pacing(self, pacer):
        """Only the explicit native campaign configures shared dispatch pacing."""
        self._zcode_pacer = pacer

    @property
    def native_completion_profiles(self):
        from ..providers.completion_profiles import normalize_completion_profiles
        return normalize_completion_profiles(getattr(self._settings, 'native_completion_profiles', {}) or {})

    @property
    def zcode_reasoning_level(self):
        """Effective native setting; the Zcode role's effort is not this value."""
        return str(getattr(self._settings, "zcode_reasoning_level", "") or "max")

    def _transport(self, provider: str):
        if self._factory is not None:
            return self._factory(provider)
        from ..providers.registry import transport_for_id

        transport = transport_for_id(self._settings, provider)
        from ..providers.completion_profiles import completion_profile_config
        if completion_profile_config(self._settings, provider) is not None:
            return transport  # completion preflights its own daemon/profile, not a host login
        if transport.cli_path() is None:
            raise ModelUnavailable(f"{provider} CLI is not installed")
        gap = transport.auth_gap()
        if gap:
            raise ModelUnavailable(f"{provider} is not logged in: {gap}")
        return transport

    def call_json(self, role: ModelRole, *, system: str, prompt: str,
                  validate: Callable[[dict], None] | None = None,
                  max_budget_usd: float | None = None, record_payload: bool = True) -> ModelReply:
        """Try the primary, then one configured generator on unavailability.

        Both attempts use the same validation and payload policy. Calls with
        a spend threshold and schema errors never trigger a second model call:
        the caller has reserved for one model, and Zcode cannot enforce a cap.
        """
        kwargs = dict(system=system, prompt=prompt, validate=validate,
                      max_budget_usd=max_budget_usd, record_payload=record_payload)
        try:
            return self._call_json(role, **kwargs)
        except ModelUnavailable as primary_error:
            if role.name != "generator" or role.fallback is None \
                    or max_budget_usd is not None or not primary_error.allow_fallback \
                    or native_outcome_unknown(primary_error):
                raise
            logger.warning("Knowledge generator %s unavailable; trying configured fallback %s",
                           role.label(), role.fallback.label())
            try:
                return self._call_json(role.fallback, fallback_from=role.label(), **kwargs)
            except ModelUnavailable as fallback_error:
                combined = ModelUnavailable(
                    f"{role.label()} unavailable: {primary_error}; "
                    f"fallback {role.fallback.label()} unavailable: {fallback_error}",
                    allow_fallback=False)
                raise copy_native_outcome(combined, fallback_error) from fallback_error

    def _call_json(self, role: ModelRole, *, system: str, prompt: str,
                   validate: Callable[[dict], None] | None = None,
                   max_budget_usd: float | None = None, record_payload: bool = True,
                   fallback_from: str = "") -> ModelReply:
        from ..providers.completion import complete_native

        identity = {"role": role.name, "requested": role.label(), "provider": role.provider,
                    "model": role.model, "effort": role.effort, "fallback_from": fallback_from}
        if role.provider == "zcode":
            identity["native_reasoning_level"] = self.zcode_reasoning_level
        pacer = self._zcode_pacer if role.provider == "zcode" else None
        cancelled_errors = ()
        if pacer is not None:
            from .depth_pacing import PacingStopped
            cancelled_errors = (PacingStopped,)

        def transport():
            found = self._subscription_transports.pop(role.label(), None)
            return found if found is not None else self._transport(role.provider)

        def validate_reply(reply):
            text = "".join(getattr(block, "text", "") or "" for block in getattr(reply, "blocks", []))
            if getattr(reply, "stop_reason", "") == "max_budget":
                raise ModelUnavailable(f"{role.label()} stopped at its spend threshold ${max_budget_usd}",
                                       allow_fallback=False)
            if getattr(reply, "stop_reason", "") == "max_tokens" or not text.strip():
                raise ModelUnavailable(f"{role.label()} timed out or returned nothing")
            data = parse_json_object(text)
            if validate is not None:
                try:
                    validate(data)
                except Exception as exc:
                    raise ModelUnavailable(f"{role.label()} reply failed its schema: {exc!r}",
                                           allow_fallback=False) from exc
            return data, text

        request = {"system": system, "messages": [{"role": "user", "content": prompt}],
                   "model": role.model, "effort": role.effort, "role": role.name}
        if max_budget_usd is not None:
            request["max_budget_usd"] = max_budget_usd
        with self._dispatch.slot() if self._dispatch is not None else nullcontext():
            _, (data, text), record, receipt = complete_native(
                transport, request=request, identity=identity,
                payload={"system": system, "prompt": prompt}, recorder=self._recorder,
                validate=validate_reply, record_payload=record_payload, pacer=pacer,
                stop_file=getattr(self._zcode_pacer, "stop_file", None),
                cancelled_errors=cancelled_errors, passthrough_errors=(ModelUnavailable,),
                make_error=lambda message, fallback: ModelUnavailable(message, allow_fallback=fallback),
                settings=self._settings,
            )
        return ModelReply(role, data, text, record["served_model"], record["usage"],
                          record["seconds"], record["cost_usd"], receipt.get("id", ""),
                          receipt.get("outputs", {}).get("reply", "").removeprefix("sha256:"))

    def subscription_billing(self, role: ModelRole) -> bool:
        """Bind the authenticated subscription backend to the next dispatch."""
        self._subscription_transports.pop(role.label(), None)
        from ..providers.completion_profiles import completion_profile_config
        if completion_profile_config(self._settings, role.provider) is not None:
            from ..providers.subscription_admission import admit_subscription
            transport = admit_subscription(self._settings, self._transport(role.provider), recorder=self._recorder)
            if transport is None:
                return False
            self._subscription_transports[role.label()] = transport
            return True
        transport = self._transport(role.provider)
        if getattr(transport, "subscription_billing", False) is not True:
            return False
        self._subscription_transports[role.label()] = transport
        return True
