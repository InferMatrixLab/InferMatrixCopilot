"""Select agent context policies at the application boundary."""
from dataclasses import replace
from functools import wraps

from ..engine.agent_policy import agent_policy
from ..engine.registry import StepRegistry
from ..review.context_policy import related_knowledge_briefing


def compose_agent_context(ctx):
    policy = agent_policy(ctx)
    if policy.related_context is None:
        ctx = replace(ctx, agent_policy=replace(policy, related_context=related_knowledge_briefing))
    return ctx


def _handler(handler):
    @wraps(handler)
    async def run(ctx):
        return await handler(compose_agent_context(ctx))
    return run


def bind_agent_context(registry):
    """Clone registrations; never mutate the caller's shared registry or contexts."""
    bound = StepRegistry()
    for name in registry.names():
        spec = registry.get(name)
        bound.register(replace(spec, handler=_handler(spec.handler)))
    return bound
