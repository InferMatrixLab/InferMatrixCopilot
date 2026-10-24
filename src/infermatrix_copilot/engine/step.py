"""Step — the engine's minimal governable execution unit (design §3.X).

A Step is one stable engineering action with declared risk and failure
semantics. Tools live below (inside steps); Playbooks above (graphs of steps).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional, TYPE_CHECKING

from ..run_trace import RunTrace
from .agent_policy import AgentExecutionPolicy

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from ..llm import LLM


class FailureKind(str, enum.Enum):
    """The typed reason a step failed — the value the executor routes on: retry
    bounded (RETRYABLE), hand back to the planner (REPLAN), or halt and escalate
    (TEST_FAILURE / BLOCKED / FORBIDDEN / ESCALATE). A str-enum so it serializes
    into RunTrace and progress checkpoints as its plain string value."""

    RETRYABLE = "retryable"        # transient; executor retries bounded
    REPLAN = "replan"              # plan no longer valid; back to planner
    TEST_FAILURE = "test_failure"  # verification failed
    BLOCKED = "blocked"            # environment/permission blocks progress
    FORBIDDEN = "forbidden"        # step refused by policy (push guard, scope)
    ESCALATE = "escalate"          # needs a human decision


@dataclass
class StepResult:
    """The outcome of one step: `ok` plus, on failure, a typed `failure`
    (drives the executor's retry/replan/escalate branch), a human `summary`, the
    `outputs` a later step consumes (notably `outputs['state_updates']`, the B2
    handoff), and the `changed_files` it touched. Failures are values, not
    exceptions — a handler never raises across the step boundary."""

    ok: bool
    failure: FailureKind | None = None
    summary: str = ""
    outputs: dict = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    checkpoint: bool = True          # False: useful output, unfinished work


@dataclass
class StepContext:
    """Everything a step handler may touch, injected by the executor."""

    settings: "Settings"
    state: dict                      # shared mutable run state
    params: dict                     # this step instance's params (from playbook)
    run_dir: Path
    trace: RunTrace
    llm: Optional["LLM"] = None
    item: Any = None                 # current foreach item, if any
    runtime: Any = None              # injected resources; never checkpointed
    agent_policy: AgentExecutionPolicy = field(default_factory=AgentExecutionPolicy)


Risk = Literal["read", "write_workspace", "push", "knowledge", "report"]
Kind = Literal["deterministic", "script", "agent", "validation", "report"]


@dataclass(frozen=True)
class StepSpec:
    """The registered, immutable definition of a step: its unique `name`, its
    `kind` (descriptive; `agent` implies a governed LLM runtime), its `risk`
    (enforced — the planner bars write/push steps in generate mode), the async
    `handler` invoked with a StepContext, and a `description`. Frozen so the
    registry's specs are shared safely and can't drift after registration."""

    name: str                        # e.g. "workspace.guard_clean"
    kind: Kind                       # descriptive; `agent` ⇒ governed runtime
    risk: Risk                       # enforced: planner bars write/push in generate
    handler: Callable[[StepContext], Awaitable[StepResult]]
    description: str = ""
    checkpoint: bool = True          # False: always execute, including on resume
