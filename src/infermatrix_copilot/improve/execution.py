"""Compose improvement policies around the task-agnostic executor."""
from contextlib import ExitStack, contextmanager
from copy import copy
from dataclasses import replace
from functools import wraps
import json
import os

from ..engine.registry import StepRegistry


def shadow_agent_policy(base=None):
    """Keep shadow restrictions in their domain; inject them into each step."""
    from ..engine.agent_policy import AgentExecutionPolicy
    from ..scopes import shadow_scope
    from .shadow import harden_scope
    from ..review.context_policy import related_knowledge_briefing

    def accept(root, resolved):
        return shadow_scope(root).check_read(resolved).allowed

    base = base or AgentExecutionPolicy()
    return replace(base,
        related_context=base.related_context or related_knowledge_briefing,
        transform_scope=harden_scope, record_skill_usage=False,
        repo_map_cache=lambda ctx: ctx.run_dir / "repo_map",
        repo_map_accept=accept,
    )


def _handler(spec):
    @wraps(spec.handler)
    async def run(ctx):
        from .artifacts import runtime_settings
        from . import objectives

        settings = runtime_settings(ctx.settings, f"{ctx.state.get('playbook', '')}.{spec.name}")
        llm = ctx.llm
        if settings is not ctx.settings and hasattr(llm, "settings"):
            llm = copy(llm)
            llm.settings = settings
        ctx = replace(ctx, settings=settings, llm=llm)
        if getattr(settings, "improve_shadow", False):
            ctx = replace(ctx, agent_policy=shadow_agent_policy(ctx.agent_policy))
        if spec.name == "agent.review_diff" and objectives.enabled(settings) and settings.improve_enabled and settings.improve_evolve_enabled:
            from .runtime import review_step
            result = await review_step(ctx)
            if result is not None:
                return result
        return await spec.handler(ctx)
    return run


@contextmanager
def bind_execution(settings, registry):
    """Bind per-run policies without modifying shared registry or settings."""
    from .enroll import declarations_for, item_for, lookup
    from .fingerprint import compute
    from .artifacts import runtime_settings

    declarations = None

    def context_for(step, state, item):
        nonlocal declarations
        if declarations is None:
            declarations = declarations_for(settings)
        context = {}
        tag = os.environ.get("IMPROVE_UNIT_TAG", "")
        if tag:
            context["unit_tag"] = tag
        decl = lookup(declarations, str(state.get("playbook") or ""), step)
        if decl is not None:
            context.update(workflow=decl.workflow, item=item_for(decl, state))
            digest, manifest = compute(decl, runtime_settings(settings, decl.workflow), state=state)
            context["_trace_inputs"] = {"fingerprint_manifest": json.dumps(manifest)}
            context.update({"fingerprint": digest} if digest else
                           {"fingerprint_missing": list(manifest.get("missing") or [])})
        return context

    def authorize(spec, step_id, state):
        if getattr(settings, "improve_shadow", False) and spec.risk not in ("read", "report"):
            reason = f"shadow run refused step '{step_id}' ({spec.name}, risk={spec.risk}): only read/report steps may run"
            state["shadow_violation"] = reason
            return reason
        return ""

    bound = StepRegistry()
    for name in registry.names():
        spec = registry.get(name)
        bound.register(replace(spec, handler=_handler(spec)))
    with ExitStack() as stack:
        if getattr(settings, "improve_governed", False):
            from .budget import governed
            from .cycle import governor_for, ledger_dir_for
            stack.enter_context(governed(governor_for(settings, ledger_dir_for(settings))))
        yield bound, context_for, authorize
