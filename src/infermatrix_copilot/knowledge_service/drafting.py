"""A bounded candidate attempt sequence; callers own model and repair policy."""

from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class AttemptPolicy:
    """Domain-selected ceiling, including the initial attempt."""
    maximum: int

    def __post_init__(self):
        if type(self.maximum) is not int or self.maximum < 1:
            raise ValueError("attempt maximum must be a positive integer")

    def allows(self, completed: int) -> bool:
        if type(completed) is not int or completed < 0:
            raise ValueError("completed attempts must be a nonnegative integer")
        return completed < self.maximum


def run_attempts(attempt, *, policy: AttemptPolicy, completed=0, feedback=""):
    """Bound generation/repair while keeping domain acceptance in a callback."""
    policy.allows(completed)
    observations = []
    for index in range(completed, policy.maximum):
        reply, accepted, feedback, error = attempt(feedback, index)
        if accepted is not None:
            return reply, accepted, observations
        observations.append({"attempt": index, "error": error})
        if feedback is None:
            break
    return None, None, observations


def bounded_attempts(attempt, *, max_repairs: int = 2):
    """Run ``attempt(feedback, index)`` until it returns an accepted candidate.

    Each attempt returns reply, accepted, feedback and a diagnostic. Exceptions
    propagate; an empty accepted value is distinct from the ``None`` refusal.
    A refused attempt with ``feedback=None`` ends without another dispatch.
    """
    if type(max_repairs) is not int or not 0 <= max_repairs <= 2:
        raise ValueError("max_repairs must be within 0..2")
    return run_attempts(attempt, policy=AttemptPolicy(max_repairs + 1))
