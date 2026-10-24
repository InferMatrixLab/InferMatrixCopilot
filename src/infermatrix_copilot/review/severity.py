"""Review severity semantics and wire identities, independent of execution.

Prompts guide judgments; these tables translate labels, never infer severity
from a finding's prose or rewrite a completed review's classification.
"""
from types import MappingProxyType


_LEVELS = (
    ("blocker", "P0", "Critical breakage or data loss requiring immediate action. "
     "Demonstrate the current trigger and critical impact on supported use; "
     "an ordinary correctness defect or the fact that a merge should wait "
     "does not alone justify this level."),
    ("major", "P1", "A high-priority defect in the changed code or a required "
     "consumer/doc/test update the change lacks. Explain the affected "
     "supported scenario and why its impact requires priority treatment."),
    ("minor", "P2", "An actionable issue with bounded impact or a concrete "
     "improvement that belongs in this PR, including a scoped correctness "
     "defect, stale documentation, a missing assertion or verification."),
    ("nit", "P3", "Optional polish; does not block approval."),
)

STRICT_TO_PRIORITY = MappingProxyType({label: priority for label, priority, _ in _LEVELS})
STRICT_SEVERITY_ORDER = MappingProxyType({label: rank for rank, (label, _, _) in enumerate(_LEVELS)})
STRICT_SEVERITY_DEMOTION = MappingProxyType({
    label: _LEVELS[min(rank + 1, len(_LEVELS) - 1)][0]
    for rank, (label, _, _) in enumerate(_LEVELS)
})

SEVERITY_GUIDANCE = (
    "Severity semantics (use the labels required by this step's output schema):\n"
    + "\n".join(f"- {label} / {priority}: {meaning}" for label, priority, meaning in _LEVELS)
    + "\nSeverity measures demonstrated impact and urgency, not confidence, "
    "review-task completion or the overall approval verdict. A verified "
    "defect need not be critical. Do not invent production exposure, "
    "callers, lost data or CI outcomes to justify escalation. Preserve "
    "explicit evidence limits. A check's blocking state alone is not a "
    "severity classification. A required domain check may specify a level; "
    "otherwise justify the chosen level from frozen-head evidence."
)
