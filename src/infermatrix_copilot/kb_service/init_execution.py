"""Initialization plans on Copilot's executor; domain records retain business facts."""
from __future__ import annotations

import hashlib
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ..app.workflow_execution import WorkflowExecution
from ..engine.registry import StepRegistry
from ..engine.step import FailureKind, StepResult, StepSpec
from ..knowledge_service.facts import FactsError
from ..knowledge_service.pinned_claims import Evidence
from ..persistence import immutable_write_bytes
from ..playbooks.store import parse_playbook
from ..trace_store import file_lock
from .init_budget import Budget, PriceError
from .init_support import (InitError, InitRecord, inputs_digest, load_prepared,
                           run_knowledge_validators, checkpoint_budget)
from .models import ModelUnavailable

PHASES = ("prepare", "draft", "validate", "prepare_publication", "publish")
WORKFLOW_VERSION = 2


def policy_digest():
    from ..resources import resource_dir

    root = Path(__file__).parent
    files = {path for pattern in ("init_*.py", "foundation_*.py", "feature_discovery*.py",
             "knowledge_*.py", "depth_*.py") for path in root.glob(pattern)}
    files.update(root / name for name in ("models.py", "model_dispatch.py", "gate.py", "quality_review.py",
        "../container_worker.py", "../execution_environment.py", "../source_fingerprint.py",
        "../budgeting.py", "../persistence.py", "../git_objects.py", "../trace_store.py", "../providers/completion.py",
        "../engine/executor.py", "../engine/step.py", "../app/workflow_execution.py", "../improve/execution.py",
        "../knowledge_service/l1.py", "../knowledge_service/lifecycle.py", "../knowledge_service/ops.py",
        "../knowledge_service/facts.py", "../knowledge_service/pinned_claims.py", "preview.py"))
    files.update((root / "../providers").glob("*.py"))
    implementation = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in sorted(files)}
    implementation["playbooks/kb-init-stage.yaml"] = hashlib.sha256(
        (resource_dir("playbooks") / "kb-init-stage.yaml").read_bytes()).hexdigest()
    return inputs_digest(implementation=implementation)


@contextmanager
def batch_lock(work):
    path = InitRecord.path(work.rt.state_dir, work.lifecycle.repo, work.STAGE).with_suffix(".lock")
    with file_lock(path, blocking=False) as held:
        if not held:
            name = "discovery" if work.STAGE == "feature-discovery" else work.STAGE
            raise InitError(f"this {name} batch is already running; checkpoint left unchanged")
        yield


def bind(work):
    """Resolve immutable business inputs while holding the cross-execution batch lock."""
    from .init_stages import _init_branch_suffix, adapter_missing

    from .init_quick_maps import QUICK_MAP_POLICY_VERSION

    rt, lc = work.rt, work.lifecycle
    previous = InitRecord.load(rt.state_dir, lc.repo, work.STAGE)
    # Old records have no navigation-policy field and must retain their bytes.
    work.quick_map_policy_version = (previous.coverage.get("quick_map_policy_version", 1)
                                     if previous else QUICK_MAP_POLICY_VERSION)
    if type(work.quick_map_policy_version) is not int or work.quick_map_policy_version not in (1, 2):
        raise InitError("unsupported quick-map policy; preserve the original checkpoint")
    hook = getattr(work, "_bind_options", None)
    if hook:
        hook()
    from .init_generation import preflight as generation_preflight
    generation_preflight(work)
    work._branch_suffix = _init_branch_suffix(rt)
    work.repo_dir = lc.knowledge_dir
    work._base_sha = work._base_for_run(rt.knowledge.fetch())
    main = rt.knowledge.knowledge_files(work._base_sha)
    work.chain = work._chain()
    work.overlay = dict(work.chain.repo_files)
    work.adapter_problem = adapter_missing(work._manifest_path()) if work._manifest_text() is None else ""
    work.upstream = rt.upstream(lc.repo, lc.full_name)
    work.upstream.sync()
    work.pin = work.upstream.resolve(work.pin or work.chain.pin or "HEAD")
    if getattr(rt, "portable_spec", None) and work.pin != rt.portable_spec.source_pin:
        raise InitError("portable source pin differs from the accepted batch; create a new batch")
    work._discovery_gate(work.chain, work.pin)
    work.overlay = dict(work.chain.repo_files)
    options = dict(work._input_options())
    if work.quick_map_policy_version >= 2:
        options["quick_map_policy_version"] = work.quick_map_policy_version
    profiles = getattr(rt.gateway, 'native_completion_profiles', {})
    if profiles:
        options['native_completion_profiles'] = profiles
    if work._branch_suffix:
        options["publication_branch_suffix"] = work._branch_suffix
    work.digest = inputs_digest(stage=work.STAGE, repo=lc.repo, pin=work.pin, kb=work._base_sha,
        init=work._init_identity(), generator=rt.generator.label(), judge=rt.judge.label(),
        dry_run=work._mode_identity(), chain=work.chain.key, **options)
    work.base = {**main, **work.chain.knowledge}
    work.previous = InitRecord.load(rt.state_dir, lc.repo, work.STAGE)


def _pending_quality_reviews(record):
    return any(state.get("review_dispatched") and not state.get("checked")
               for state in record.coverage.get("quality_reviews", {}).values())


def _pending_generation(record):
    from .init_generation import pending
    return pending(record)


def prepare(work):
    from .init_stages import BRANCH_SUFFIX_ENV, _schema_tags

    rt, lc, previous = work.rt, work.lifecycle, work.previous
    profiles = getattr(rt.gateway, 'native_completion_profiles', {})
    if previous and profiles != previous.native_completion_profiles:
        raise InitError('completion execution profile differs from the original batch; '
                        'restore its configuration or use a new state directory')
    work.action = "draft"
    if previous and previous.inputs_digest != work.digest and (
            any(entry.get("status") == "reserved" for entry in previous.budget_calls.values())
            or _pending_quality_reviews(previous) or _pending_generation(previous)):
        raise InitError("stage inputs changed with an unresolved model call; reconcile the original dispatch before new work")
    if previous and previous.pr.get("prepared") and previous.status in ("publishing", "blocked") and not previous.pr.get("validation_pending"):
        if work.dry_run != previous.dry_run:
            raise InitError("a publication of this stage is pending (pushed, PR not confirmed); re-run "
                            "with its original preview/publication mode to finish it")
        problems = work.chain.problems + work._resume_input_problems(previous, work.digest)
        if load_prepared(previous.pr["prepared"]).get("branch") != work._publication_branch():
            problems.append(f"prepared publication belongs to a different branch suffix; restore {BRANCH_SUFFIX_ENV} to its original value to resume")
        if work._frozen_discovery and previous.discovery.get("catalog_binding") != work._discovery_binding():
            problems.append("prepared publication belongs to a different discovery catalog; preserve it and start a new batch")
        work.record = previous
        if problems:
            work._blocked(problems)
            return
        verify_prepared(work)
        work.budget = checkpoint_budget(None if rt.unlimited_subscription else lc.init.budget_usd, work.record, rt.state_dir)
        if work.dry_run:
            problems = work._precheck()
            if problems:
                work._blocked(problems)
                return
        work.record.status, work.record.problems = "publishing", []
        work.action = "publish"
        return
    if previous and previous.status == "dry_run" and previous.inputs_digest == work.digest:
        from .preview import read_preview
        read_preview(previous)
    if previous and previous.inputs_digest == work.digest and not work.adapter_problem \
            and not work.chain.problems and previous.status in ("dry_run", "published", "empty") \
            and (previous.dry_run == work.dry_run or previous.status == "published") and work._cache_reusable(previous):
        work.record, work.action = previous, "done"
        return
    if previous and previous.pr.get("number") and previous.inputs_digest != work.digest:
        raise InitError(f"a published {work.STAGE} record exists (PR #{previous.pr['number']}); remove "
                        f"{InitRecord.path(rt.state_dir, lc.repo, work.STAGE)} to start over")
    work.record = InitRecord(stage=work.STAGE, repo=lc.repo, pin=work.pin, kb_base_sha=work._base_sha,
        inputs_digest=work.digest, started_at=float(int(rt.clock())), dry_run=work.dry_run, notes=list(work.notes))
    if work.quick_map_policy_version >= 2:
        work.record.coverage["quick_map_policy_version"] = work.quick_map_policy_version
    if profiles:
        work.record.native_completion_profiles = profiles
    if work._frozen_discovery:
        work.record.discovery["catalog_binding"] = work._discovery_binding()
    if work.chain.pin and work.pin != work.chain.pin:
        work.record.notes.append(f"pinned at {work.pin[:12]}, not at the earlier stages' {work.chain.pin[:12]}")
    work.budget = checkpoint_budget(None if rt.unlimited_subscription else lc.init.budget_usd, work.record, rt.state_dir)
    # Legacy stages own accepted-unit recovery. Preserve all accumulated spend,
    # including failures before the first immutable draft was completed.
    if previous and previous.inputs_digest == work.digest:
        work.record.spent_usd = work.budget.spent_usd = previous.spent_usd
        from copy import deepcopy
        work.record.budget_calls.update(deepcopy(previous.budget_calls))
        # Revalidate restored identities before allowing any new dispatch.
        work.budget = checkpoint_budget(work.budget.limit_usd, work.record, rt.state_dir)
        if "generation_execution" in previous.coverage:
            work.record.coverage["generation_execution"] = deepcopy(previous.coverage["generation_execution"])
        if previous.coverage.get("quality_reviews"):
            from copy import deepcopy
            work.record.coverage["quality_reviews"] = deepcopy(previous.coverage["quality_reviews"])
    restored = work._restore_progress(previous)
    work.budget.spent_usd = max(work.budget.spent_usd, work.record.spent_usd)
    problems = work.chain.problems + restored + work._precheck()
    if _pending_quality_reviews(work.record):
        problems.append("quality review outcome unknown; reconcile original dispatch before resuming generation")
    if work.adapter_problem:
        problems.append(work.adapter_problem)
    if lc.repo not in _schema_tags(rt.knowledge.show(work._base_sha, "doc/knowledge/SCHEMA.md")):
        problems.append(f"tag {lc.repo!r} is not in the doc/knowledge/SCHEMA.md taxonomy; add it (with the adapter PR) before running kb init")
    if problems:
        work._blocked(problems)
        return
    work.tags, work.today, work.release = [lc.repo], rt.today(), f"init-{work.pin[:12]}"
    work.observer = work.upstream.observer(work.pin, pull=rt.pull)
    work.tree = work.upstream.export(work.pin, work.scratch / "tree")
    work._inputs(work.tree)
    if work.route_problem:
        work._blocked([work.route_problem])


def artifact(work, phase, payload):
    """Immutable, exact-byte sidecars; progress contains only a bounded reference."""
    body = json.dumps({"binding": {"repo": work.lifecycle.repo, "stage": work.STAGE,
        "pin": work.pin, "kb_base_sha": work._base_sha, "inputs_digest": work.digest,
        "execution": work.fingerprint}, "phase": phase, "payload": payload},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    sha = hashlib.sha256(body).hexdigest()
    path = work.run_dir / f"{phase}-{sha}.json"
    try:
        immutable_write_bytes(path, body, exist_ok=True)
    except FileExistsError as exc:
        raise InitError("immutable initialization artifact differs") from exc
    return {"path": path.name, "sha256": sha}


def read_artifact(work, ref, phase, *, directory=None, execution=None, record=None):
    directory = directory or work.run_dir
    path = directory / ref["path"]
    if path.is_symlink() or path.parent.resolve() != directory.resolve():
        raise InitError("initialization artifact is outside this execution")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise InitError("initialization artifact hash differs")
    data = json.loads(raw)
    if data["phase"] != phase or data["binding"] != {
            "repo": record.repo if record else work.lifecycle.repo,
            "stage": record.stage if record else work.STAGE,
            "pin": record.pin if record else work.pin,
            "kb_base_sha": record.kb_base_sha if record else work._base_sha,
            "inputs_digest": record.inputs_digest if record else work.digest,
            "execution": execution or work.fingerprint}:
        raise InitError("initialization artifact belongs to different inputs or policy")
    return data["payload"]


def verify_prepared(work):
    """New prepared publications retain their validation binding across controls."""
    proof = work.record.pr.get("validation")
    if work.record.pr.get("validation_pending"):
        raise InitError("publication validation binding is incomplete; resume preparation")
    if not proof:
        return  # Legacy prepared format retains its original recovery protocol.
    execution = proof["execution"]
    if len(execution) != 64 or any(c not in "0123456789abcdef" for c in execution):
        raise InitError("prepared publication execution identity is invalid")
    directory = work.run_dir.parent / execution
    payload = read_artifact(work, proof["artifact"], "prepare_publication", directory=directory,
                            execution=execution, record=work.record)
    prepared = load_prepared(work.record.pr["prepared"])
    if payload["prepared"] != work.record.pr["prepared"] or inputs_digest(**prepared) != payload["publication_digest"]:
        raise InitError("prepared publication differs from validated artifact")


def business_digest(record):
    """Bind a candidate to the domain facts that produced it, excluding run state."""
    facts = {name: getattr(record, name) for name in (
        "seeds", "evidence", "verdicts", "dropped", "checklist", "coverage",
        "history", "review", "depth", "discovery")}
    # Publication adds these receipts after validation; the original
    # foundation gates and prepared proof validate them independently.
    facts["coverage"] = {key: value for key, value in record.coverage.items()
                         if key not in ("foundation", "foundation_publication")}
    return inputs_digest(**facts)


def replay(work, step_id, outputs):
    if step_id not in ("draft", "validate", "prepare_publication"):
        return
    if step_id == "draft":
        # Preflight can mutate incremental recovery state in memory. Compare
        # the durable facts that were actually saved by the candidate's run.
        work.previous = InitRecord.load(work.rt.state_dir, work.lifecycle.repo, work.STAGE)
    payload = read_artifact(work, outputs["artifact"], step_id)
    if step_id == "draft":
        if work.previous is None or work.previous.inputs_digest != work.digest:
            raise InitError("draft has no matching business record")
        if business_digest(work.previous) != payload["business_digest"]:
            raise InitError("draft belongs to different business facts")
        work._draft = payload["draft"]
        work.head = work._draft["head"]
        if work.STAGE == "harvest-calibration":
            work._case_paths = set(work._draft["other"]) - {work._manifest_path()}
        work.record = work.previous
        work.record.status, work.record.problems = "started", []
        work.record.spent_usd = max(work.record.spent_usd, work.budget.spent_usd)
        work.budget = checkpoint_budget(work.budget.limit_usd, work.record, work.rt.state_dir)
        if getattr(work, "foundation_mode", "strict") == "partial" and work.STAGE == "knowledge":
            from .feature_discovery_index import build_for_stage
            work.source_index = build_for_stage(work.tree, work)
    elif step_id == "validate":
        if payload["draft"] != work.draft_ref:
            raise InitError("validation is bound to a different draft")
        work.changed = payload["files"]
    else:
        if payload["validated"] != work.validated_ref:
            raise InitError("publication is bound to different validation")
        prepared = load_prepared(payload["prepared"])
        if inputs_digest(**prepared) != payload["publication_digest"]:
            raise InitError("prepared publication differs from validated artifact")
        work.record.pr = {"prepared": payload["prepared"],
            "validation": {"execution": work.fingerprint, "artifact": outputs["artifact"]}}
        work.record.status = "publishing"
        work.prepared_ref = outputs["artifact"]
    if step_id == "draft":
        work.draft_ref = outputs["artifact"]
    elif step_id == "validate":
        work.validated_ref = outputs["artifact"]


def validate(work):
    from .init_quick_maps import owner_pages
    from .init_stages import KNOWLEDGE_PREFIX, ROUTES_NAME, validate_change
    import yaml

    draft = work._draft
    try:
        routed = owner_pages(work.head.get(f"{work.repo_dir}/{ROUTES_NAME}"))
    except (ValueError, yaml.YAMLError):
        routed = []
    problems = draft["problems"] + validate_change(work.base, work.head, observer=work.observer,
        rules=draft["rules"], evidence=[Evidence.from_dict(e) for e in draft["evidence"]],
        other=draft["other"], check_other=getattr(work, draft["check_other"], None), quick_map_pages=routed)
    work.changed = {KNOWLEDGE_PREFIX + p: text for p, text in work.head.items() if work.base.get(p) != text}
    work.changed.update({p: after for p, (before, after) in draft["other"].items() if after is not None and after != before})
    work.record.files = sorted(work.changed)
    if not work.changed and not problems:
        depth = work.record.coverage.get("semantic_depth", {})
        if work.STAGE == "knowledge-deepen" and getattr(work.coverage_policy, "semantic_depth_per_facet_gt", None) is not None and not depth.get("target_met"):
            problems.append("semantic depth target is unmet; retained checkpoint is incomplete")
        else:
            work.record.status = "empty"
            work.record.notes.append(f"the {work.STAGE} stage found nothing to change")
            work.action = "done"
    if not problems and work.changed:
        problems = run_knowledge_validators(work.rt.knowledge, work._base_sha, {**work.overlay, **work.changed})
    if problems:
        work._blocked(problems)


def result(work, *, outputs=None):
    record = work.record
    ok = record.status not in ("blocked", "partial")
    return StepResult(ok, None if ok else FailureKind.BLOCKED,
        "; ".join(record.problems)[:2000] if not ok else f"kb init {work.STAGE}: {record.status}",
        outputs=outputs or {}, checkpoint=not record.unfinished)


async def phase(ctx):
    work, name = ctx.runtime, ctx.params["phase"]
    try:
        if name == "prepare":
            prepare(work)
        elif work.action == "done":
            return result(work)
        elif name == "draft":
            work._build(work.tree)
            if _pending_generation(work.record) or _pending_quality_reviews(work.record):
                work._blocked(["model outcome unknown; reconcile original dispatch before publication"])
            if work.record.status == "blocked":
                return result(work)
            if work.record.status == "empty":
                work.action = "done"
                work.record.save(work.rt.state_dir)
                return result(work)
            if not hasattr(work, "_draft"):
                raise InitError("stage returned without a candidate artifact")
            work.record.save(work.rt.state_dir)
            work.draft_ref = artifact(work, name, {"draft": work._draft, "business_digest": business_digest(work.record)})
            return result(work, outputs={"artifact": work.draft_ref})
        elif name == "validate":
            validate(work)
            if work.record.status == "blocked":
                return result(work)
            work.record.save(work.rt.state_dir)
            work.validated_ref = artifact(work, name, {"draft": work.draft_ref, "files": work.changed})
            return result(work, outputs={"artifact": work.validated_ref})
        elif name == "prepare_publication":
            work._prepare_publication(work.changed)
            if work.record.status == "blocked":
                return result(work)
            ref = artifact(work, name, {"validated": work.validated_ref, "prepared": work.record.pr["prepared"],
                "publication_digest": inputs_digest(**load_prepared(work.record.pr["prepared"]))})
            work.prepared_ref = ref
            work.record.pr.pop("validation_pending", None)
            work.record.pr["validation"] = {"execution": work.fingerprint, "artifact": ref}
            work.record.save(work.rt.state_dir)
            work.action = "publish"
            return result(work, outputs={"artifact": ref})
        elif name == "publish":
            verify_prepared(work)
            if hasattr(work, "prepared_ref"):
                replay(work, "prepare_publication", {"artifact": work.prepared_ref})
            work._publish()
        return result(work, outputs={"state_updates": {"init_draft": work.action == "draft", "init_publish": work.action != "done"}})
    except InitError as exc:
        # Existing preflight refusals preserve the durable record and raise
        # through the Python API after the executor has recorded and cleaned up.
        if name == "prepare":
            work.domain_error, work.preserve_record = exc, True
            raise
        work._blocked([str(exc)])
        return result(work)
    except (ModelUnavailable, PriceError, FactsError) as exc:
        if not hasattr(work, "record"):
            raise
        work._blocked([f"{type(exc).__name__}: {exc}"])
        return result(work)


def step_specs():
    return [StepSpec(name=f"knowledge.init.{name}", kind="agent" if name == "draft" else "deterministic",
            risk="knowledge", handler=phase, checkpoint=name not in ("prepare", "publish")) for name in PHASES]


async def execute_init(work):
    """All entry points share the same plan, locks, cache validation and cleanup."""
    from ..config import Settings

    with batch_lock(work):
        bind(work)
        work.execution_policy = policy_digest()
        work.fingerprint = inputs_digest(workflow=WORKFLOW_VERSION, inputs=work.digest,
            policy=work.execution_policy, dry_run=work.dry_run, budget=work.lifecycle.init.budget_usd,
            unlimited=work.rt.unlimited_subscription,
            controls={name: getattr(work, name, None) for name in ("retry_unfinished", "feature_ids",
                "acceptance_mode", "depth_index_path", "stop_file", "foundation_mode", "foundation_record_path")})
        work.run_dir = InitRecord.path(work.rt.state_dir, work.lifecycle.repo, work.STAGE).parent / "executions" / work.STAGE / work.fingerprint
        registry = StepRegistry()
        for spec in step_specs():
            registry.register(spec)
        from ..resources import resource_dir
        import yaml
        plan = parse_playbook(yaml.safe_load((resource_dir("playbooks") / "kb-init-stage.yaml").read_text()))
        with tempfile.TemporaryDirectory(prefix="kb-init-") as scratch:
            work.scratch = Path(scratch)
            try:
                outcome = await WorkflowExecution(work.rt.settings or Settings(), registry).execute(plan,
                    run_dir=work.run_dir, state={}, runtime=work, fingerprint=work.fingerprint,
                    validate_cached=lambda name, outputs: replay(work, name, outputs))
                if hasattr(work, "domain_error"):
                    raise work.domain_error
                if "cached result refused" in outcome.blocked_reason and work.previous:
                    from copy import deepcopy
                    work.record = deepcopy(work.previous)
                    work._blocked([outcome.blocked_reason])
                if outcome.status != "done" and hasattr(work, "record") and work.record.status not in ("blocked", "partial"):
                    work._blocked([outcome.blocked_reason or "initialization execution failed"])
                if not hasattr(work, "record"):
                    raise InitError(outcome.blocked_reason or "initialization failed before preparation")
                return outcome, work.run_dir
            finally:
                if hasattr(work, "record") and not getattr(work, "preserve_record", False):
                    if hasattr(work, "budget"):
                        work.record.spent_usd = round(work.budget.spent_usd, 6)
                    work.record.save(work.rt.state_dir)
