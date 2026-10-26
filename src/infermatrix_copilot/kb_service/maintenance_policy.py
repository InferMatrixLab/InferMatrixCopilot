"""Maintenance policy identity and correction admission, separate from orchestration.

Every final execution boundary rereads current evidence. These functions never
schedule work or change a maintenance finding.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import knowledge_repository
from .maintenance_audit import AUDIT_SYSTEM, digest
from .maintenance_calibration import current
from .maintenance_correction import CORRECTION_SYSTEM
from .maintenance_settings import MaintenanceConfig
from .maintenance_store import MaintenanceStore

IMPLEMENTATION_FILES = (
    '../budgeting.py',
    '../persistence.py',
    '../git_objects.py',
    '../app/workflow_execution.py',
    '../engine/executor.py',
    '../engine/step.py',
    '../providers/base.py',
    '../providers/completion.py',
    '../providers/json_session.py',
    '../providers/claude_code.py',
    '../providers/codex.py',
    '../providers/cursor.py',
    '../providers/deepseek.py',
    '../providers/zcode.py',
    'models.py',
    'model_identity.py',
    'model_dispatch.py',
    'intake.py',
    'sources.py',
    'sweep.py',
    'upstream_facts.py',
    '../knowledge_service/ops.py',
    '../knowledge_service/lifecycle.py',
    '../knowledge_service/drafting.py',
    '../improve/execution.py',
    'maintenance.py',
    'maintenance_policy.py',
    'maintenance_resolution.py',
    'config.py',
    'maintenance_audit.py',
    'maintenance_correction.py',
    'maintenance_calibration.py',
    'gate.py',
    'maintenance_units.py',
    'maintenance_store.py',
    'containment.py',
    'containment_drill.py',
    'containment_transport.py',
    'merge.py',
    'runtime.py',
    'gate_receipts.py',
    'ledger.py',
    'companion.py',
    'outbox.py',
    'publisher.py',
    'judge_tuning.py',
    'maintenance_settings.py',
    '../knowledge_service/l1.py',
    '../knowledge_service/gate_verifier.py',
    '../knowledge_service/containment.py',
)


def settings(rt):
    return getattr(rt, "maintenance", None) or MaintenanceConfig()


def policy_digest(rt, config=None):
    config = config or settings(rt)
    return config.digest(judge=rt.judge.label(), generator=rt.generator.label(),
                         prompts={"audit": AUDIT_SYSTEM, "correction": CORRECTION_SYSTEM,
                                 "required_checks": os.environ.get("KB_MAINTENANCE_REQUIRED_CHECKS", '["suite"]'),
                                 "implementation": {name: digest((Path(__file__).parent / name).read_text()) for name in
                                     IMPLEMENTATION_FILES}})


def eligible_repos(rt):
    return sorted(lifecycle.repo for lifecycle in rt.registry.values()
                  if lifecycle.enabled and lifecycle.publishes and lifecycle.full_name)


def readiness(rt, store, policy):
    report = store.report(now=rt.clock(), policy_sha256=policy, eligible_repos=eligible_repos(rt))
    from .containment_drill import acceptance_current
    return report["seven_valid_nights_ready"] and current(rt, policy) and bool(settings(rt).consumers) and acceptance_current(rt, policy)


def correction_publishable(rt, changeset):
    """Additional admission requirements never replace the existing full gate."""
    from .containment import pending_holds
    config = settings(rt)
    policy = policy_digest(rt, config)
    if not config.enabled or changeset["detail"].get("maintenance_policy") != policy:
        return "maintenance policy changed or disabled"
    if not readiness(rt, MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner), policy):
        return "seven valid shadow nights or current maintenance calibration missing"
    if not current(rt, policy, observed_model=changeset["detail"].get("source_audit", {}).get("reviewer"),
                   observed_generator=changeset["detail"].get("served_generator")):
        return "observed maintenance model changed; recalibration required"
    if not pending_holds(rt).get("ready"):
        return "containment propagation incomplete"
    return ""


def required_ci(rt, changeset):
    """Read exact-head statuses. Unknown or missing required checks keep the hold."""
    required = json.loads(os.environ.get("KB_MAINTENANCE_REQUIRED_CHECKS", '["suite"]'))
    if not isinstance(required, list) or not required or any(not isinstance(v, str) or not v for v in required):
        raise ValueError("maintenance required checks must be a nonempty JSON list")
    head = changeset.get("head_sha")
    if not head:
        raise ValueError("correction has no reviewed PR head")
    repository = knowledge_repository()
    checks = rt.github.get(f"/repos/{repository}/commits/{head}/check-runs")
    states = rt.github.get(f"/repos/{repository}/commits/{head}/status")
    observed = {}
    for check in checks.get("check_runs", []):
        if check.get("head_sha") == head:
            passed = check.get("status") == "completed" and check.get("conclusion") == "success"
            observed[check["name"]] = observed.get(check["name"], True) and passed
    for check in states.get("statuses", []):
        observed[check["context"]] = observed.get(check["context"], True) and check.get("state") == "success"
    if not all(observed.get(name) is True for name in required):
        raise ValueError("correction exact-head CI is incomplete")
    return {"repository": repository, "head_sha": head, "required": required, "checks": checks, "statuses": states}
