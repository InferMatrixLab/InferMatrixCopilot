"""Per-step agent policies supplied by application composition."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..scopes import ToolScope


@dataclass(frozen=True)
class AgentExecutionPolicy:
    transform_scope: Callable[[ToolScope, Any, str], ToolScope] | None = None
    record_skill_usage: bool = True
    repo_map_cache: Callable[[Any], Path] | None = None
    repo_map_accept: Callable[[str, Path], bool] | None = None
    related_context: Callable[[Any, str, dict, dict], str] | None = None


def agent_policy(ctx) -> AgentExecutionPolicy:
    """Support lightweight contexts while leaving policy selection to callers."""
    return getattr(ctx, "agent_policy", None) or AgentExecutionPolicy()
