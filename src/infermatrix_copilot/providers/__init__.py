"""Provider registry — one table of ways to reach a model
(doc/features/provider-registry.md).

Public surface: the registry (`PROVIDERS`, `resolve_provider`,
`transport_for`), the contracts (`ProviderSpec`, `AgentSessionRequest`,
`HarnessTransport`), and the two integration factories the core calls:
`llm_for` (LLM construction under the selected backend) and
`run_harness_step` (the agent-runtime branch for harness targets).
"""

from __future__ import annotations

from .base import AgentSessionRequest, HarnessTransport, ProviderSpec, native_budget_facts
from .harness_llm import HarnessLLM
from .registry import PROVIDERS, resolve_provider, transport_for

__all__ = [
    "PROVIDERS", "AgentSessionRequest", "HarnessLLM", "HarnessTransport",
    "ProviderSpec", "llm_for", "resolve_provider", "run_harness_step",
    "transport_for", "run_harness_session",
]


class _UnshippedTransport(HarnessTransport):
    """Placeholder for declared-but-unshipped backends (claude-code/codex):
    construction must degrade — `available` False, the milestone message on
    any use — while `strict_readiness`/doctor report the gap with its fix."""

    def __init__(self, settings, reason: str):
        super().__init__(settings)
        self._reason = reason

    def cli_path(self):
        return None

    def run_session(self, req):
        raise RuntimeError(self._reason)

    def complete(self, **kwargs):
        raise RuntimeError(self._reason)


def llm_for(settings):
    """The LLM-shaped client for the selected backend: the real `llm.LLM`
    under `api` (parity: byte-identical construction), a `HarnessLLM`
    adapter under a harness. Construction never requires the CLI (or the
    transport) to exist — absence surfaces as `available == False`, the same
    degrade signal an unset API key produces today."""
    if resolve_provider(settings).kind == "harness":
        try:
            return HarnessLLM(settings, transport_for(settings))
        except NotImplementedError as exc:
            return HarnessLLM(settings, _UnshippedTransport(settings, str(exc)))
    from ..llm import LLM

    return LLM(settings)


def run_harness_step(ctx, target, *, step_name: str, system: str, prompt: str,
                     scope, max_iters: int, provider_id: str = "",
                     model: str = ""):
    """Run one agent step through a harness: write the bridge spec for this
    scope, then delegate the whole step to the transport. Returns
    `agent_loop.AgentOutcome` so the runner's downstream (output coercion,
    traces, salvage) is shared with the in-process loop. Default transport is
    the run's selected backend; `provider_id`/`model` pin an explicit harness
    instead (the MoA harness-member path, independent of `STRICT_BACKEND`)."""
    from ..tool_bridge import write_bridge_spec
    from .registry import transport_for_id

    transport = (transport_for_id(ctx.settings, provider_id) if provider_id
                 else transport_for(ctx.settings))
    spec = ctx.state.get("task_spec") or {}
    bridge_spec = write_bridge_spec(
        run_dir=ctx.run_dir, step_name=step_name, scope=scope,
        repo=str(spec.get("repo") or ctx.settings.default_repo),
        knowledge_dir=getattr(ctx.settings, "knowledge_dir", None))
    request = AgentSessionRequest(
        system=system, prompt=prompt, scope=scope, model=model or target.model,
        max_iters=max_iters, timeout_s=ctx.settings.strict_backend_timeout_s,
        run_dir=ctx.run_dir, step_name=step_name,
        bridge_spec_path=bridge_spec, trace=ctx.trace)
    return run_harness_session(transport, request)


def run_harness_session(transport, request: AgentSessionRequest):
    """One native call boundary for sessions, completions and their receipts."""
    from .completion import complete_native
    from .base import execution_effort
    from dataclasses import replace
    requested_effort = getattr(request, 'effort', '')
    effort = execution_effort(transport, requested_effort)
    if effort != requested_effort:
        request = replace(request, effort=effort)
    settings = getattr(transport, 'settings', None)
    if getattr(settings, 'native_session_profiles', {}).get(transport.spec.id):
        from .session_execution import execute_session
        return execute_session(transport, request)
    _, outcome, _, _ = complete_native(lambda: transport,
        request={'system': request.system, 'messages': [{'role': 'user', 'content': request.prompt}],
                 'model': request.model, 'max_tokens': None, 'role': request.step_name,
                 **({'effort': effort} if effort else {})},
        identity={'provider': transport.spec.id}, session_request=request, settings=settings,
        capture={'provider': 'harness:' + transport.spec.id, 'extra_result': {'session': True}})
    return outcome
