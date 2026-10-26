"""Reserved Direct reviews on the same durable executor used by Strict."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from .... import run_status as rs
from ....app.request_policy import enforce_strict_review_policy
from ....app.reservation import RunReservation
from ....app.workflow_execution import WorkflowExecution
from ....config import Settings
from ....persistence import atomic_write_bytes, immutable_write_bytes
from ....playbooks.store import parse_playbook
from ....review.direct import DirectClient
from ....contracts.models import (CarriedFinding, ChangedPath, DirectCompletionRequest,
    DirectReviewRequest, FindingRecheck, InvalidRequestError, RepositoryRef)
from ....review.review_result import (normalize_direct_evidence, parse_direct_result,
                                     review_summary_errors)
from ....review.direct_prompt import build_direct_review_prompt, direct_review_schema
from ....review.publication_policy import feedback_alignment_error
from ....review.frozen_context import validate_business_context
from ....providers.descriptors import provider_descriptor
from ....knowledge_service.containment import knowledge_usage_record, knowledge_availability_check
from ...lifecycle import RunLock, RunLockHeld
from ...registry import StepRegistry
from ...step import FailureKind, StepResult, StepSpec


def validate_request(raw):
    profile = raw.get("profile") or {}
    if "direct" not in provider_descriptor(profile.get("provider")).execution_modes:
        raise InvalidRequestError("Direct execution requires a Codex or Cursor read-only profile")
    if (not isinstance(profile.get("command"), list) or not profile["command"]
            or any(not isinstance(arg, str) or not arg for arg in profile["command"])):
        raise InvalidRequestError("Direct execution command must be a nonempty string array")
    if not isinstance(raw.get("prompt", ""), str) or not isinstance(raw.get("output_schema", {}), dict):
        raise InvalidRequestError("Direct execution prompt/schema overrides have invalid types")
    context = raw.get("business_context") or {}
    review = raw.get("review") or {}
    validate_business_context(context, review.get("expected_head_sha", ""), review.get("pr_number"))
    if not raw.get("prompt"):
        pull = context.get("pull") or {}
        if not isinstance(pull, dict) or not pull.get("base_sha") or not pull.get("html_url"):
            raise InvalidRequestError("Direct execution requires frozen pull metadata")
        if pull.get("head_sha") != review.get("expected_head_sha") or pull.get("number") != review.get("pr_number"):
            raise InvalidRequestError("Direct pull metadata differs from the frozen review target")
    idle, maximum = profile.get("idle_timeout_s", 900), profile.get("absolute_timeout_s", 900)
    if profile.get('execution_config') is not None:
        from ....providers.completion_profiles import normalize_completion_profiles
        normalize_completion_profiles({profile['provider']: profile['execution_config']})
        if profile.get('wrap_up_after_s') is not None:
            raise InvalidRequestError('sandbox Direct does not support host-session wrap-up')
    if (isinstance(idle, bool) or isinstance(maximum, bool)
            or not isinstance(idle, (int, float)) or not isinstance(maximum, (int, float))
            or not math.isfinite(idle) or not math.isfinite(maximum) or not 0 < idle <= maximum):
        raise InvalidRequestError("Direct execution requires bounded positive timeouts")


PROFILE_KEYS = ("provider", "command", "model", "idle_timeout_s", "absolute_timeout_s", "wrap_up_after_s", "execution_config", "effort")

def check_profile(raw, profiles):
    """Authorize transport only from the independent deployment registry."""
    validate_request(raw)
    profile = raw["profile"]
    expected = profiles.get(profile["provider"])
    if not expected or any(profile.get(key) != expected.get(key) for key in PROFILE_KEYS):
        raise InvalidRequestError("Direct execution profile does not match the trusted deployment")
    return expected

def _review_request(raw):
    return DirectReviewRequest(**{**raw, "repository": RepositoryRef(**raw["repository"]),
        "changed_paths": tuple(ChangedPath(**row) for row in raw["changed_paths"]),
        "carried_findings": tuple(CarriedFinding(**row) for row in raw.get("carried_findings", ()))})


async def execute(settings, run_dir, *, held_lock=None):
    request_bytes = (run_dir / "request.json").read_bytes()
    digest = os.environ.get("COPILOT_DIRECT_REQUEST_SHA256", "")
    if not digest or not hmac.compare_digest(hashlib.sha256(request_bytes).hexdigest(), digest):
        raise InvalidRequestError("Direct request changed after its authorized launch")
    spec_raw = json.loads(request_bytes)
    workspace = run_dir / "workspace"
    if not (run_dir / "workspace.json").is_file() or workspace.is_symlink():
        raise InvalidRequestError("Direct run has no provider-owned frozen workspace")
    execution_settings = settings.model_copy(update={
        "mcp_allowed_repo_roots": [*settings.allowed_repo_roots, str(workspace)],
    })
    spec = enforce_strict_review_policy({**spec_raw, "repo_path": str(workspace)},
        allowed_repos=settings.mcp_allowed_repos, settings=execution_settings)
    raw = spec_raw["params"]["direct_review"]
    trusted = json.loads(os.environ.get("COPILOT_DIRECT_PROFILE", "{}"))
    check_profile(raw, {trusted.get("provider"): trusted})
    request, profile = _review_request(raw["review"]), raw["profile"]
    if (request.repository.alias != spec.repo or request.pr_number != spec.pr
            or request.expected_head_sha != spec.expected_head_sha or raw["repo_path"] != spec_raw["repo_path"]):
        raise InvalidRequestError("Direct input does not match its authorized reservation")
    client, runtime = DirectClient(), {}
    canonical_prompt = not raw.get("prompt")
    schema = direct_review_schema() if canonical_prompt else raw["output_schema"]
    business_context = raw.get("business_context") or {}
    threads = frozenset(profile.get("thread_references") or (
        row.get("thread_ref") for row in business_context.get("pr_review_context", ())
        if isinstance(row, dict) and row.get("thread_ref")))

    def head():
        current = subprocess.run(["git", "-C", spec.repo_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout.strip()
        if current != request.expected_head_sha:
            raise InvalidRequestError("Direct checkout head changed after reservation")

    def save_usage(delivery):
        pin_path = run_dir / "knowledge.json"
        pin = json.loads(pin_path.read_text()) if pin_path.is_file() else {}
        atomic_write_bytes(pin_path, json.dumps({**pin, "knowledge_usage": runtime["usage"],
            "injection_status": delivery}).encode())

    async def prepare(ctx):
        head()
        plan = client.plan(request)
        if raw.get("resource_revision") and plan.resource_revision != raw["resource_revision"]:
            raise InvalidRequestError("Direct knowledge resources changed after planning")
        runtime["plan"] = plan
        runtime['knowledge_root'] = client._issued_context(plan.review_context_id).knowledge_view.root
        context = plan.to_dict()
        immutable_write_bytes(run_dir / "direct-context.json", json.dumps(context,
            ensure_ascii=False, sort_keys=True).encode(), exist_ok=True)
        runtime["usage"] = knowledge_usage_record(context, injected=False)
        save_usage("not_dispatched")
        if not knowledge_availability_check(runtime["usage"])["allowed"]:
            raise InvalidRequestError("Direct knowledge requires reassessment")
        return StepResult(True)

    async def model(ctx):
        from ....providers.json_session import JSONSessionError, run_readonly_json

        checks = (schema.get("properties", {}).get("review_checks", {}).get("required") or [])
        def validate(payload):
            try:
                candidate = parse_direct_result(payload, required_checks=checks,
                    normalize_reference=lambda value: value if value in threads else "")
                feedback_error = feedback_alignment_error(candidate,
                    business_context.get("existing_feedback_status", candidate.existing_feedback_status))
                if feedback_error:
                    return feedback_error
                if canonical_prompt or profile.get("summary_policy") == "change_flow_v1":
                    return "; ".join(review_summary_errors(candidate,
                        changed_statuses=[item.status for item in request.changed_paths])) or None
            except ValueError as exc:
                return str(exc)
        save_usage("unknown")
        prompt = raw.get("prompt") or build_direct_review_prompt(business_context["pull"],
            {**business_context, **runtime["plan"].to_dict()}, request.repository.full_name,
            cursor_read_only=profile["provider"] == "cursor", provider_issued_context=True)
        if not canonical_prompt:
            # Custom host text cannot supply the provider's issued context.
            # The canonical prompt already includes it in the Direct context.
            prompt += "\nProvider-issued knowledge context (authoritative for this run):\n" + \
                json.dumps(runtime["plan"].to_dict(), ensure_ascii=False)
        try:
            sandbox = {}
            if profile.get('execution_config') is not None:
                from dataclasses import replace
                from ....providers.base import AgentSessionRequest
                from ....scopes import read_only_scope
                from ....tool_bridge import write_bridge_spec, READ_ONLY_BRIDGE_TOOLS
                frame = run_dir / 'readonly' / 'session'
                frame.mkdir(parents=True, exist_ok=True)
                knowledge = runtime['knowledge_root']
                scope = replace(read_only_scope('direct-review'), root=str(workspace),
                    allowed_tools=READ_ONLY_BRIDGE_TOOLS, strict_extras=True,
                    read_roots=(str(workspace), str(frame), str(knowledge)))
                bridge = write_bridge_spec(run_dir=frame, step_name='direct-review', scope=scope,
                                           repo=request.repository.alias, knowledge_dir=knowledge)
                session = AgentSessionRequest(system='Read-only patch review through the scoped tool bridge.',
                    prompt=prompt, scope=scope, model=profile.get('model', ''), max_iters=1,
                    timeout_s=profile.get('absolute_timeout_s', 900), run_dir=frame,
                    step_name='direct-review', bridge_spec_path=bridge)
                sandbox = {'execution_config': profile['execution_config'], 'execution_root': run_dir / 'readonly',
                           'session_request': session,
                           'settings': execution_settings.model_copy(update={'knowledge_dir': knowledge}),
                           'effort': profile.get('effort', '')}
            reply = await asyncio.to_thread(run_readonly_json,
                prompt, cwd=spec.repo_path,
                command=profile["command"], provider=profile["provider"], model=profile.get("model", ""),
                output_schema=schema, idle_timeout_s=profile.get("idle_timeout_s", 900),
                absolute_timeout_s=profile.get("absolute_timeout_s", 900),
                wrap_up_after_s=profile.get("wrap_up_after_s"), result_validator=validate, **sandbox)
        except Exception as exc:
            from ....providers.outcomes import requires_reconciliation

            if isinstance(exc, JSONSessionError) and not requires_reconciliation(exc):
                atomic_write_bytes(run_dir / "direct-failure.json", json.dumps({
                    "failure_class": exc.failure_class, "note": str(exc)}).encode())
            raise
        runtime["usage"] = knowledge_usage_record(runtime["plan"].to_dict(), injected=True)
        save_usage("confirmed")
        runtime["reply"] = reply
        immutable_write_bytes(run_dir / "direct-model.json", json.dumps(reply,
            ensure_ascii=False, sort_keys=True).encode(), exist_ok=True)
        return StepResult(True)

    async def complete(ctx):
        head()
        reply = runtime["reply"]
        candidate = normalize_direct_evidence(parse_direct_result(reply["payload"],
            required_checks=(schema.get("properties", {}).get("review_checks", {}).get("required") or []),
            normalize_reference=lambda value: value if value in threads else ""))
        decision = client.validate(DirectCompletionRequest(
            review_context_id=runtime["plan"].review_context_id, expected_head_sha=request.expected_head_sha,
            evidence_head_sha=candidate.reviewed_head_sha, subtraction_signal=candidate.subtraction_signal,
            subtraction=candidate.subtraction, minimality_proof=candidate.minimality_proof,
            final_comment_count=1, existing_feedback_status=candidate.existing_feedback_status,
            finding_dispositions=candidate.finding_dispositions,
            finding_rechecks=tuple(FindingRecheck(**row) for row in candidate.finding_rechecks)))
        value = candidate.to_dict()
        wire = {"contract_version": "review.v1", "reviewed_head_sha": candidate.reviewed_head_sha,
            "verdict": "COMMENT", "summary_markdown": candidate.summary, "comments": [], "findings": [],
            "finding_dispositions": [], "finding_rechecks": list(candidate.finding_rechecks),
            "rechecks_complete": True, "recheck_missing": [], "stale": False,
            "direct_result": value, "diagnostics": {"served_model": reply.get("served_model"),
                "usage": reply.get("usage"), "knowledge_plan": runtime["plan"].to_dict(),
                "business_context": raw.get("business_context", {}),
                "required_checks": (schema.get("properties", {}).get("review_checks", {}).get("required") or []),
                "thread_references": list(threads),
                "carried_findings": [row.to_dict() for row in request.carried_findings]},
            "expected_head_sha": request.expected_head_sha, "actual_head_sha": candidate.reviewed_head_sha}
        atomic_write_bytes(run_dir / "direct-result.json", json.dumps(wire, ensure_ascii=False).encode())
        atomic_write_bytes(run_dir / "RUN_REPORT.md", candidate.summary.encode())
        if not decision.review_complete:
            return StepResult(False, FailureKind.BLOCKED, "; ".join(decision.missing))
        return StepResult(True)

    registry = StepRegistry()
    for name, handler in (("prepare", prepare), ("model", model), ("complete", complete)):
        registry.register(StepSpec("review.direct." + name, "agent" if name == "model" else "validation",
                                   "read", handler, checkpoint=False))
    playbook = parse_playbook({"name": "direct-review", "version": 1, "status": "locked",
        "task_kinds": ["pr_review"], "repos": [], "steps": [
            {"id": name, "step": "review.direct." + name} for name in ("prepare", "model", "complete")]})
    return await WorkflowExecution(settings, registry).execute(playbook, run_dir=run_dir,
        state={"task_spec": spec.model_dump(), "repo_path": spec.repo_path}, runtime=runtime, held_lock=held_lock)


def main(argv=None):
    settings = Settings()
    run_id = (sys.argv[1:] if argv is None else argv)[0]
    run_dir = RunReservation(settings).contained_run_dir(run_id)
    try:
        with RunLock(run_dir) as lock:
            if not rs.claim_for_execution(run_dir, child_pid=os.getpid()):
                return 3
            try:
                rs.mark(run_dir, rs.RUNNING)
                outcome = asyncio.run(execute(settings, run_dir, held_lock=lock))
                rs.mark(run_dir, outcome.status, note=outcome.blocked_reason)
                return 0 if outcome.status == "done" else 3
            except Exception as exc:
                rs.mark(run_dir, rs.FAILED, note=f"{type(exc).__name__}: {exc}")
                return 1
    except RunLockHeld:
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
