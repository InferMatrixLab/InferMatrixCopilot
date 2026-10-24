"""`HarnessLLM` — LLM-shaped adapter over a harness transport, TOOL-LESS only.

Callers that today hold an `llm.LLM` (intent classification, ensemble
reducer/merge, output repair, chat) keep their `create()` call sites; under
a harness backend those tool-less calls become one-shot CLI invocations.
Any call that passes tools raises: agent steps must route through
`run_session` (the harness owns its loop), and a loud error here is the
guard against silently running a second, ungoverned tool loop.

Model selection: a harness serves `STRICT_BACKEND_MODEL` (else its
`ProviderSpec.default_model`). Per-call API-tier model ids are ignored — see
`create()`.
"""

from __future__ import annotations

from .base import HarnessTransport


class HarnessLLM:
    """Duck-typed `llm.LLM` replacement for harness backends. `available`
    reflects CLI presence so every existing `ctx.llm.available` degrade path
    keeps working; `for_member` still returns a REAL api-backed client — MoA
    members carry their own endpoint+key and are independent of the run's
    backend selection."""

    def __init__(self, settings, transport: HarnessTransport):
        self.settings = settings
        self._transport = transport

    @property
    def available(self) -> bool:
        """True when the harness CLI is installed (auth is probed by doctor,
        not here — presence is the cheap honest signal)."""
        from .completion_profiles import CompletionPolicyError, completion_profile_config
        provider = getattr(getattr(self._transport, 'spec', None), 'id', '')
        if completion_profile_config(self.settings, provider) is not None:
            try:
                self.validate_completion()
            except (CompletionPolicyError, RuntimeError):
                return False
            return True
        return self._transport.cli_path() is not None

    def for_target(self, target) -> HarnessLLM:
        """Per-target client. Harness targets are served by this adapter; an
        api target (possible when MoA/tier config mixes backends) gets a real
        `llm.LLM` so it never silently rides the harness."""
        if getattr(target, "kind", "api") == "harness":
            return self
        from ..llm import LLM

        return LLM(self.settings).for_target(target)

    def for_member(self, member):
        """MoA members are raw-API endpoints with their own credentials —
        delegate to the real client; the harness never serves members."""
        from ..llm import LLM

        return LLM(self.settings).for_member(member)

    def validate_completion(self) -> None:
        """Attest required completion support without dispatching a model."""
        from .completion_profiles import validate_completion_policy
        from .base import execution_effort
        execution_effort(self._transport)
        validate_completion_policy(self.settings, self._transport, require_trace=True)

    @property
    def _harness_model(self) -> str:
        """The model this harness serves: `STRICT_BACKEND_MODEL`, else the
        provider's registry default (empty for the subscription CLIs, which
        pick their own)."""
        return (self.settings.strict_backend_model
                or self._transport.spec.default_model)

    def create(self, *, system: str, messages: list[dict],
               tools: list[dict] | None = None, model: str | None = None,
               max_tokens: int | None = None, on_text=None, role: str = ""):
        """Tool-less one-shot completion via the harness CLI. `tools` raises
        by design (see module docstring).

        The caller's `model` is IGNORED. Call sites pass an API-tier id
        (`INTENT_MODEL`, `REVIEWER_MODEL`) chosen for the raw-API path; a
        vendor CLI cannot serve it and rejects it **as prose in the ordinary
        reply channel**, which upstream then reports as an unparseable reply
        rather than a model error. Measured on the release matrix: every
        LLM-classified command phrase failed on all three CLI harnesses with
        "I couldn't parse that", while the one harness whose configured id it
        happened to match classified fine. `STRICT_BACKEND_MODEL` (else the
        provider default) is the only model selector under a harness."""
        if tools:
            raise RuntimeError(
                "harness backend supports tool-less create() only — agent "
                "steps run through the provider's run_session "
                "(doc/features/provider-registry.md)")
        from .completion import complete_native

        kwargs = {"system": system, "messages": messages, "model": self._harness_model,
                  "max_tokens": max_tokens, "tools": []}
        from .base import execution_effort
        effort = execution_effort(self._transport)
        if effort:
            kwargs['effort'] = effort
        reply, _, _, _ = complete_native(
            lambda: self._transport,
            request={**{k: v for k, v in kwargs.items() if k != "tools"}, "role": role},
            identity={"provider": self._transport.spec.id}, capture=True, settings=self.settings,
        )
        if on_text is not None and reply.text:
            on_text(reply.text)
        return reply
