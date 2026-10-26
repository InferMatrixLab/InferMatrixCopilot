"""The knowledge service's runtime: one object wiring ledger, sources, models,
outbox and the intake -> gate -> publish flow for one repository at a time.

Change sets live as JSON under ``$KB_STATE_DIR/changesets/<id>.json`` (the
full proposed file texts are too large for ledger rows); the ledger holds
their status, operations and decisions.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import RepoLifecycle, general_lifecycle, load_registry
from .gate import changes_between, run_gate
from .intake import draft_changes, merge_drafts, operations_json
from .ledger import Ledger
from .models import ModelGateway, ModelRole, ModelUnavailable, roles_from_env
from .sources import (
    GitHubReader, KnowledgeRepo, SourceError, bugfix_lesson, load_bugfix_drops, load_lessons, mailbox_records,
    resolve_repository,
)

DEFAULT_BACKFILL_DAYS = 1


@dataclass
class KbRuntime:
    state_dir: Path
    ledger: Ledger
    registry: dict[str, RepoLifecycle]
    gateway: ModelGateway
    generator: ModelRole
    judge: ModelRole
    knowledge: KnowledgeRepo
    github: GitHubReader
    outbox: object | None = None
    clock: Callable[[], float] = time.time
    lease_owner: str | None = None  # set by `kb serve`, which holds the lease for its lifetime
    publisher_public_key: object | None = None
    traces: object | None = None  # trace_store.TraceStore: decisions and outcomes (model calls via the gateway)
    maintenance: object | None = None  # opt-in durable nightly lane
    release_label: Callable[[str], str] = field(default=lambda repo: "")
    # the repository's upstream as the gate observes it (None: no facts attested)
    upstream_facts: Callable[[RepoLifecycle], object | None] = field(default=lambda lifecycle: None)

    @classmethod
    def from_env(cls, settings, *, state_dir: Path | None = None, sync_repos: bool = True) -> "KbRuntime":
        from ..resources import adapters_root
        from .cli import DEFAULT_STATE_DIR

        state_dir = Path(state_dir or os.environ.get("KB_STATE_DIR") or DEFAULT_STATE_DIR).expanduser()
        general = general_lifecycle(enabled=os.environ.get("KB_GENERAL_ENABLED", "0") == "1",
                                    mode=os.environ.get("KB_GENERAL_MODE", "shadow"))
        registry = load_registry(Path(os.environ.get("ADAPTERS_DIR") or adapters_root()), general=general)
        ledger = Ledger(state_dir / "kb.db")
        for lifecycle in registry.values() if sync_repos else ():
            ledger.ensure_repo(lifecycle.repo, lifecycle.mode if lifecycle.enabled else "disabled")
        generator, judge = roles_from_env()
        outbox = None
        if os.environ.get("KB_SIGNING_KEY"):
            from ..knowledge_service.signing import load_private_key
            from .outbox import Outbox

            outbox = Outbox(state_dir, load_private_key(os.environ["KB_SIGNING_KEY"]), ledger, clock=time.time)
        publisher_key = None
        if os.environ.get("KB_PUBLISHER_PUBKEY"):
            from ..knowledge_service.signing import load_public_key

            publisher_key = load_public_key(Path(os.environ["KB_PUBLISHER_PUBKEY"]).read_text(encoding="utf-8"))
        from ..trace_store import TraceStore
        from .maintenance_settings import MaintenanceConfig

        traces = TraceStore(state_dir / "traces")
        github = GitHubReader()
        return cls(
            upstream_facts=lambda lifecycle: service_observer(state_dir, lifecycle, github),
            publisher_public_key=publisher_key, traces=traces,
            state_dir=state_dir, ledger=ledger, registry=registry,
            gateway=ModelGateway(settings, recorder=trace_recorder(traces)),
            generator=generator, judge=judge,
            knowledge=KnowledgeRepo(Path(os.environ.get("KB_KNOWLEDGE_CLONE") or state_dir / "knowledge-repo")),
            github=github, outbox=outbox, maintenance=MaintenanceConfig.from_env(),
        )

    # -- helpers -------------------------------------------------------------

    def trace(self, kind: str, **fields) -> None:
        """Append a trace/1 record; never lets tracing break the service."""
        if self.traces is None:
            return
        try:
            self.traces.append(kind, **fields)
        except Exception:  # noqa: BLE001 - a full disk must not stop the gate
            pass

    def today(self) -> str:
        return dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).date().isoformat()

    def release_for(self, repo: str) -> str:
        return self.ledger.get_cursor(repo, "release") or self.release_label(repo) or f"r{self.today()}"

    def changeset_path(self, changeset_id: str) -> Path:
        return self.state_dir / "changesets" / f"{changeset_id}.json"

    def save_changeset_files(self, changeset_id: str, data: dict) -> None:
        from .outbox import atomic_write_json

        atomic_write_json(self.changeset_path(changeset_id), data)

    def load_changeset_files(self, changeset_id: str) -> dict:
        return json.loads(self.changeset_path(changeset_id).read_text(encoding="utf-8"))


def trace_recorder(traces):
    """Every model call (inputs, outputs, usage, failure) as a trace/1
    ``model_call`` record, under the caller's bound trace context."""

    from ..trace_store import NativeCompletionRecorder
    return NativeCompletionRecorder(traces)


# -- intake ------------------------------------------------------------------------

def collect_events(rt: KbRuntime, lifecycle: RepoLifecycle) -> int:
    """Record new events for one repository; returns how many were new."""
    new = 0
    if lifecycle.intake.merged_prs and lifecycle.full_name:
        cursor = rt.ledger.get_cursor(lifecycle.repo, "merged_since")
        if cursor is None:
            days = int(os.environ.get("KB_INTAKE_BACKFILL_DAYS", DEFAULT_BACKFILL_DAYS))
            cursor = (dt.datetime.fromtimestamp(rt.clock(), dt.timezone.utc)
                      - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ") + "|0"
            rt.ledger.set_cursor(lifecycle.repo, "merged_since", cursor)
        since, _, after = cursor.partition("|")
        # merge-time order; the cursor is (merge time, PR number) of the last
        # PROCESSED PR, so nothing is skipped and ties cannot stall discovery
        for number, merged_at in rt.github.merged_prs_since(
                lifecycle.full_name, since, after_number=int(after or 0)):
            pr = rt.github.pull_request(lifecycle.full_name, number)
            if rt.ledger.record_event(lifecycle.repo, "merged_pr", str(number), pr.evidence()) is not None:
                new += 1
            rt.ledger.set_cursor(lifecycle.repo, "merged_since", f"{merged_at}|{number}")
    if lifecycle.intake.copilot_runs:
        def record(lesson: dict) -> None:
            nonlocal new
            evidence = {"source_reference": f"run {lesson['run_ref']}", "title": "Copilot run lesson",
                        "body": lesson["summary"], "changed_files": [], "diff_excerpt": lesson["diff_excerpt"]}
            if rt.ledger.record_event(lifecycle.repo, "copilot_run", lesson["event_id"], evidence) is not None:
                new += 1

        for lesson in load_lessons(rt.state_dir / "inbox", lifecycle.repo):
            record(lesson)
            Path(lesson["path"]).rename(Path(lesson["path"]).with_suffix(".consumed"))
        # verified pr_debug fixes: dropped by runs on this host ...
        drops = os.environ.get("KB_BUGFIX_DIR") or str(rt.state_dir / "inbox" / "bugfix")
        for lesson in load_bugfix_drops(drops, rt.registry, lifecycle.repo):
            record(lesson)
            Path(lesson["path"]).rename(Path(lesson["path"]).with_suffix(".consumed"))
        # ... and posted to the mailbox issue by runs on other hosts
        mailbox = os.environ.get("KB_BUGFIX_MAILBOX", "")
        authors = {a.strip().lower() for a in os.environ.get("KB_BUGFIX_AUTHORS", "").split(",") if a.strip()}
        if mailbox and authors:
            full_name, _, number = mailbox.partition("#")
            after = int(rt.ledger.get_cursor(lifecycle.repo, "bugfix_mailbox_after") or 0)
            since = rt.ledger.get_cursor(lifecycle.repo, "bugfix_mailbox_since") or ""
            comments = rt.github.issue_comments(full_name, int(number), after_id=after, since=since)
            for _comment_id, raw in mailbox_records(comments, authors):
                lesson = bugfix_lesson(raw)
                if lesson is not None and resolve_repository(lesson["repo_key"], rt.registry) == lifecycle.repo:
                    record(lesson)
            if comments:
                rt.ledger.set_cursor(lifecycle.repo, "bugfix_mailbox_after", str(int(comments[-1]["id"])))
                if comments[-1].get("created_at"):
                    # ``since`` is exclusive and second-grained: step back one
                    # second so an unread comment sharing that second is read
                    # again; the id cursor drops what was already seen
                    last = dt.datetime.fromisoformat(str(comments[-1]["created_at"]).replace("Z", "+00:00"))
                    rt.ledger.set_cursor(lifecycle.repo, "bugfix_mailbox_since",
                                         (last - dt.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return new


def run_intake(rt: KbRuntime, lifecycle: RepoLifecycle, *, max_events: int = 10) -> str | None:
    """Draft, gate and stage one change set from pending events. Returns the
    change set id, or None when nothing was proposed or another process holds
    the single-writer lease (two writers would draft the same events and
    request duplicate PRs)."""
    from .ledger import LeaseError

    from ..trace_store import trace_context

    try:
        with trace_context(playbook="kb-intake", repo=lifecycle.repo, run_id=f"intake-{uuid.uuid4().hex[:12]}"):
            if rt.lease_owner:
                return _run_intake_locked(rt, lifecycle, max_events=max_events, owner=rt.lease_owner)
            with rt.ledger.lease() as owner:
                return _run_intake_locked(rt, lifecycle, max_events=max_events, owner=owner)
    except LeaseError:
        return None


def _run_intake_locked(rt: KbRuntime, lifecycle: RepoLifecycle, *, max_events: int, owner: str) -> str | None:
    events = rt.ledger.events(lifecycle.repo, "pending", limit=max_events)
    if not events:
        return None
    try:
        base_sha = rt.knowledge.fetch()
        base = rt.knowledge.knowledge_files(base_sha)
        external = rt.knowledge.external_texts(base_sha)
    except SourceError:
        return None  # retried next tick; events stay pending
    release, today = rt.release_for(lifecycle.repo), rt.today()
    drafts = []
    from ..trace_store import accepted_key, trace_context

    accepted: dict[int, str] = {}  # event -> the accepted generator call's key
    draft_attempts = {}
    for event in events:
        try:
            previous = json.loads(event['detail'])
        except (ValueError, TypeError):
            previous = {}
        attempt = previous.get('draft_attempt', 0) if isinstance(previous, dict) else 0
        if type(attempt) is not int or attempt < 0:
            raise ValueError('invalid intake draft attempt')
        draft_attempts[event['id']] = attempt
        rt.ledger.heartbeat(owner)  # model calls are slow; keep the lease live
        try:
            # the change set does not exist yet: the draft key and the accepted
            # attempt link exactly the call whose reply became the change to
            # the decision that later stages it
            key, holder = draft_key_for_event(lifecycle.repo, event["id"]), {}
            from ..config import Settings
            from ..improve.artifacts import runtime_settings
            draft_settings = runtime_settings(getattr(rt, "settings", None) or Settings(), "kb-intake.draft")
            from .intake import checkpoint_draft
            from ..improve import objectives
            evolved = bool(objectives.enabled(draft_settings) and draft_settings.improve_enabled
                           and draft_settings.improve_evolve_enabled and rt.traces is not None)
            active_release = None
            if evolved:
                from ..improve.runtime import registry
                registry_path = registry(draft_settings)
                active_release = hashlib.sha256(registry_path.read_bytes()).hexdigest() if registry_path.exists() else None
            def generate_event():
                from ..improve import objectives
                prediction = None
                if evolved:
                    from ..improve.runtime import active, execute
                    payload = {"repo": lifecycle.repo, "repo_dir": lifecycle.knowledge_dir, "event_id": event["id"],
                               "evidence": event["payload"], "files": base, "release": release, "today": today,
                               "external_texts": external, "generator_model": rt.generator.model}
                    try:
                        objectives.capture(rt.traces, "kb-intake.draft", f"{lifecycle.repo}#{event['id']}", payload)
                    except objectives.ReplayCaptureLimit as exc:
                        # Ordinary pinned drafting does not require an evolution
                        # replay. An active (even disabled) evolved workflow does.
                        try:
                            entry = active(draft_settings)
                            workflows = entry.get("workflows", [])
                            if not isinstance(workflows, list) or not all(isinstance(w, str) for w in workflows):
                                raise ValueError("invalid active draft workflows")
                            is_active = "kb-intake.draft" in workflows
                        except Exception as registry_exc:
                            raise ModelUnavailable("cannot verify active draft release: " + str(registry_exc)) from registry_exc
                        if is_active:
                            raise ModelUnavailable("active draft lacks a complete replay: " + str(exc)) from exc
                    else:
                        from ..llm import LLM
                        try:
                            prediction = execute(draft_settings, rt.traces, "kb-intake.draft", payload, LLM(draft_settings))
                        except Exception as exc:
                            error = ModelUnavailable("autonomous draft rolled back: " + str(exc), allow_fallback=False)
                            # The execution boundary may fail after dispatch. Missing
                            # native facts cannot prove that another call is safe.
                            from ..budgeting import copy_native_outcome
                            raise copy_native_outcome(error, exc, assume_sent=True) from exc
                if prediction is not None:
                    from .intake import Draft
                    from ..knowledge_service.ops import KnowledgeOperation
                    return Draft(event_ids=[event["id"]], operations=[KnowledgeOperation.from_dict(o) for o in prediction["operations"]],
                                        result=None, rationale=prediction.get("rationale", ""), rejected=prediction.get("rejected", False))
                else:
                    return draft_changes(
                        repo=lifecycle.repo, repo_dir=lifecycle.knowledge_dir, event_id=event["id"],
                        evidence=event["payload"], files=base, gateway=rt.gateway,
                        generator=rt.generator, release=release, today=today,
                        max_operations=max(1, min(6, draft_settings.kb_draft_max_operations)))
            with trace_context(draft_key=key, _accepted=holder, step="draft"):
                draft, draft_trace = checkpoint_draft(rt.ledger, owner,
                    unit_key=f"intake:{event['id']}:{attempt}", base_sha=base_sha,
                    accepted_trace=lambda: accepted_key(key, holder), generate=generate_event,
                    repo=lifecycle.repo, repo_dir=lifecycle.knowledge_dir, event_id=event['id'],
                    evidence=event['payload'], files=base, external_texts=external,
                    generator=rt.generator, release=release, today=today,
                    max_operations=max(1, min(6, draft_settings.kb_draft_max_operations)),
                    execution_binding={"settings_sha256": hashlib.sha256(draft_settings.model_dump_json().encode()).hexdigest(),
                                       "evolved": evolved, "active_release": active_release})
            drafts.append(draft)
            if draft_trace:
                accepted[event["id"]] = draft_trace
        except ModelUnavailable as exc:
            from ..budgeting import native_outcome_unknown

            # fenced: if this worker lost its lease meanwhile, the new holder may
            # already have staged the event; an unfenced reset would undo that
            status = "draft_unknown" if native_outcome_unknown(exc) else "pending"
            rt.ledger.set_event_statuses(owner, [(event["id"], status,
                json.dumps({'draft_attempt': attempt, 'error': f"generator unavailable: {exc}"}))])
            return None  # no weaker fallback; wait for the pinned model
    operations, result, kept, conflicting = merge_drafts(base, drafts, release=release, today=today)
    kept_ids = {i for d in kept for i in d.event_ids}
    updates = []
    for draft in drafts:
        for event_id in draft.event_ids:
            if event_id in kept_ids:
                continue  # staged below
            if draft in conflicting:
                updates.append((event_id, 'pending', json.dumps({
                    'draft_attempt': draft_attempts[event_id] + 1, 'reason': 'completed draft conflicts with batch'})))
                continue  # known conflict explicitly authorizes the next business attempt
            if draft.rejected:
                updates.append((event_id, "rejected", json.dumps(draft.attempts, ensure_ascii=False)[:2000]))
            else:
                updates.append((event_id, "done", "no rules"))
    rt.ledger.set_event_statuses(owner, updates)
    if not operations or result is None:
        return None
    outside = sorted(p for p in result.files if not p.startswith(lifecycle.knowledge_dir + "/"))
    if outside:  # defence in depth: drafting already refuses these
        rt.ledger.set_event_statuses(owner, [
            (event_id, "rejected", f"writes outside {lifecycle.knowledge_dir}: {outside}")
            for event_id in kept_ids])
        return None
    evidence = [e["payload"] for e in events if e["id"] in kept_ids]
    return gate_and_stage(rt, lifecycle, owner, kind="intake", base=base, base_sha=base_sha,
                          external=external, operations=operations, result=result,
                          evidence=evidence, event_ids=sorted(kept_ids), release=release,
                          draft_keys=[accepted[i] for i in sorted(kept_ids) if i in accepted],
                          extra_detail={"generator": ", ".join(dict.fromkeys(d.generator for d in kept))})


def draft_key_for_event(repo: str, event_id) -> str:
    """Unique per drafting: a redrafted event never shares a key with an earlier try."""
    return f"event:{repo}:{event_id}:{uuid.uuid4().hex[:8]}"


def require_settled_reviews(rt, repo: str, *, kinds, binding=None):
    """Admission only: an unresolved gate cannot authorize replacement work.

    Legacy intents missing a requested binding remain conservative blockers.
    Only an explicit reconciliation of the existing row may clear this state.
    """
    for pending in rt.ledger.changesets(repo, ('review_pending',)):
        if pending['kind'] not in kinds:
            continue
        detail = pending.get('detail') or {}
        if any(key in detail and detail[key] != value for key, value in (binding or {}).items()):
            continue
        raise ModelUnavailable(
            f"review {pending['id']} remains pending; reconcile original dispatch before replacement work",
            allow_fallback=False)


def gate_and_stage(rt: KbRuntime, lifecycle: RepoLifecycle, owner: str, *, kind: str, base: dict,
                   base_sha: str, external: dict, operations, result, evidence: list[dict],
                   event_ids: list[int], release: str, force_human: str = "",
                   hold: bool = False, draft_keys: list[str] = (), extra_detail: dict | None = None) -> str:
    """Run the quality gate on one change set and stage it (files first, then
    every ledger write in one transaction fenced on the lease). Used by intake,
    sweep and purge. ``force_human`` routes a passing change set to people
    (e.g. a sweep-wide circuit breaker)."""
    from ..trace_store import trace_context

    head = {**base, **result.files}
    rt.ledger.heartbeat(owner)
    changeset_id = rt.ledger.new_changeset_id(lifecycle.repo, kind)
    rule_ids = sorted({op.new_rule_id or op.rule_id for op in operations if (op.new_rule_id or op.rule_id)})
    observer = rt.upstream_facts(lifecycle)  # one mirror sync for facts and evidence
    detail = {
        "operations": operations_json(operations), "event_ids": event_ids,
        **(extra_detail or {}), "base_sha": base_sha, "release": release,
        "generator": (extra_detail or {}).get("generator") or rt.generator.label(),
        "judge": rt.judge.label(),
    }
    rt.save_changeset_files(changeset_id, {"base_sha": base_sha, "files": result.files,
                                         "deleted": [], "evidence": evidence})
    # Consume the events and retain the exact draft before any gate dispatch.
    # A crash or lost response keeps this intent; the poller cannot redraft it.
    try:
        rt.ledger.stage_intake(owner, lifecycle.repo, changeset_id, kind=kind,
            status="review_pending", verdicts=[], human_reason="", drafted_events=event_ids,
            detail=detail)
    except Exception:
        try:
            rt.ledger.changeset(changeset_id)
        except KeyError:
            rt.changeset_path(changeset_id).unlink(missing_ok=True)
        raise
    intent = rt.ledger.changeset(changeset_id)
    draft_sha256 = hashlib.sha256(rt.changeset_path(changeset_id).read_bytes()).hexdigest()
    with trace_context(changeset_id=changeset_id, rule_ids=rule_ids, step="gate"):
        decision = run_gate(
            base=base, head=head, changes=changes_between(base, head), external_texts=external,
            evidence=evidence, gateway=rt.gateway, judge=rt.judge, release=release,
            repo_dir=lifecycle.knowledge_dir, protected_rules=lifecycle.protected_rules,
            retire_ratio=lifecycle.retire_ratio, max_files=lifecycle.max_files,
            facts=observer, evidence_for=_evidence_for(observer))
    if force_human and decision.status == "pass":
        decision.status = "human"
        decision.reasons.append(force_human)
    # a held change set (release sweep) is released only once the whole sweep
    # is settled and its aggregate circuit breaker has been evaluated
    status = {"pass": "sweep_held" if hold else "gated", "fail": "failed", "human": "human"}[decision.status]
    verdicts = [{"layer": "L2", "verdict": b.verdict, "block_id": b.block.block_id,
                 "model": b.model, "detail": b.to_dict()} for b in decision.blocks]
    verdicts.append({"layer": "gate", "verdict": decision.status, "detail": {"reasons": decision.reasons}})
    settlement = dict(kind=kind, status=status, verdicts=verdicts,
        human_reason="; ".join(decision.reasons) if decision.status == "human" else "",
        drafted_events=event_ids, expected_status="review_pending",
        detail={**detail, "decision": decision.to_dict()})
    # Persist the completed result before attempting the ledger transaction.
    # A failed transaction must not erase the only copy of paid review work.
    # Keep the original intent pending on failure: workflow cursors and companion
    # work must be reconciled before admitting another execution.
    from ..persistence import fsync_directory, immutable_write_bytes
    from .companion import needs_companion, prepare_companion, publish_companion
    from .gate_receipts import settle_gate_result

    companion_required = bool(needs_companion(decision) and kind not in {"correction", "maintenance_calibration"}
                              and not hold and not force_human and operations)
    companion = (prepare_companion(rt.ledger.new_changeset_id(lifecycle.repo, "companion"),
                                  changeset_id, operations, decision, external, base_sha)
                 if companion_required else None)

    receipt = {"version": 1, "changeset_id": changeset_id,
               "repo": lifecycle.repo, "kind": kind, "generation": intent["generation"],
               "intent_detail": intent["detail"],
               "draft_sha256": draft_sha256,
               "settlement": settlement, "companion_required": companion_required, "companion": companion}
    receipt_bytes = json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode("utf-8")
    receipt_dir = rt.state_dir / "gate-results"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    fsync_directory(rt.state_dir)
    immutable_write_bytes(receipt_dir / f"{changeset_id}.json", receipt_bytes)
    settle_gate_result(rt.ledger, rt.state_dir, owner, changeset_id)
    rt.trace("decision", context={"changeset_id": changeset_id, "rule_ids": rule_ids, "step": "gate",
                                  "draft_keys": list(draft_keys)},
             model={"role": "judge", "model": rt.judge.label()},
             result={"status": decision.status, "staged_as": status, "reasons": decision.reasons,
                     "blocks": [{"block_id": b.block.block_id, "rule_id": b.block.rule_id, "op": b.block.op,
                                 "verdict": b.verdict, "dimensions": b.dimensions} for b in decision.blocks],
                     "consistency": [{"owner_dir": c["owner_dir"], "verdict": c["verdict"]}
                                     for c in decision.consistency],
                     "l1_issues": [i.to_dict() for i in decision.l1.issues]})
    if companion:
        # the change is fine except for citations outside knowledge/: draft the
        # companion PR that updates them; this change waits for it
        publish_companion(rt, lifecycle, companion["id"])
    return changeset_id


def publish(rt: KbRuntime, lifecycle: RepoLifecycle, changeset_id: str) -> str:
    """Hand a gated change set to the publisher, or record it in shadow mode.

    Shadow (and any repository that does not publish) records what WOULD be
    opened and writes nothing to the outbox. The rest of the merge flow
    (verdict signing, the merge item, activation) is driven by the scheduler once the
    publisher reports the PR."""
    changeset = rt.ledger.changeset(changeset_id)
    if changeset["kind"] == "maintenance_calibration":
        return "maintenance_calibration"
    if changeset["status"] != "gated":
        return changeset["status"]
    state = rt.ledger.repo_state(lifecycle.repo)
    globally_paused = rt.ledger.repo_state("*")["paused"]
    if not lifecycle.auto_merge or not lifecycle.publishes or state["paused"] or globally_paused \
            or rt.outbox is None:
        rt.ledger.update_changeset(changeset_id, status="shadow_recorded")
        return "shadow_recorded"
    if changeset["kind"] == "correction":
        from .maintenance_policy import correction_publishable
        reason = correction_publishable(rt, changeset)
        if reason:
            rt.ledger.update_changeset(changeset_id, status="maintenance_held", detail={**changeset["detail"], "maintenance_hold": reason})
            return "maintenance_held"
    if not calibration_current(rt, lifecycle):
        # auto_merge is only as good as the judge: no current passing
        # calibration for THIS judge and THIS case set, no publication
        rt.ledger.update_changeset(changeset_id, status="calibration_required")
        rt.ledger.enqueue_human(lifecycle.repo, "judge calibration missing or outdated", changeset_id)
        return "calibration_required"
    data = rt.load_changeset_files(changeset_id)
    detail = changeset["detail"]
    from .merge import issue_once

    issue_once(rt, lifecycle.repo, changeset, "open_pr", {
        "changeset_id": changeset_id,
        "base_sha": data["base_sha"],
        "branch": f"kb/{lifecycle.repo}/{changeset_id}",
        "files": {f"knowledge/{rel}": text for rel, text in data["files"].items()},
        "deleted": [f"knowledge/{rel}" for rel in data.get("deleted", [])],
        "title": f"knowledge({lifecycle.repo}): {len(detail['operations'])} change(s) from {changeset['kind']}",
        "body": _pr_body(changeset_id, detail),
    })
    rt.ledger.update_changeset(changeset_id, status="pr_requested")
    return "pr_requested"


def _pr_body(changeset_id: str, detail: dict) -> str:
    lines = [
        f"Generated automatically by the Copilot knowledge service (changeset `{changeset_id}`).",
        "",
        "| op | page | rule |", "|---|---|---|",
    ]
    for op in detail["operations"]:
        lines.append(f"| {op['kind']} | `{op['page']}` | {op.get('new_rule_id') or op['rule_id']} |")
    lines += ["", f"Generator: `{detail['generator']}` · judge: `{detail['judge']}`.",
              "The publisher merges it only if its local gate passes on the exact merge result."]
    return "\n".join(lines)


def _evidence_for(observer):
    """Per-rule evidence with full upstream diffs (the judge's view; see kb_service.evidence)."""
    if observer is None:
        return None
    from .evidence import for_rule

    return lambda text, evidence: for_rule(text, evidence, observer)


def service_observer(state_dir: Path, lifecycle: RepoLifecycle, github: GitHubReader):
    """The service's view of a public upstream: its own mirror (shared with the
    release sweep) and the read-only GitHub reader. A repository without an
    upstream, or one that publishes nothing, attests no facts."""
    if not lifecycle.full_name or not lifecycle.publishes:
        return None
    from .upstream_facts import MirrorObserver

    full_name = lifecycle.full_name
    return MirrorObserver(state_dir / "upstream" / f"{lifecycle.repo}.git", full_name,
                          lambda number: github.get(f"/repos/{full_name}/pulls/{number}"))


def calibration_record(rt: KbRuntime, lifecycle: RepoLifecycle) -> dict | None:
    raw = rt.ledger.get_cursor(lifecycle.repo, "calibration")
    return json.loads(raw) if raw else None


def calibration_current(rt: KbRuntime, lifecycle: RepoLifecycle) -> bool:
    from .calibration import case_set_digest

    record = calibration_record(rt, lifecycle)
    if not record or not record.get("passed") or lifecycle.adapter_dir is None:
        return False
    return (record.get("judge") == rt.judge.label()
            and record.get("case_set") == case_set_digest(lifecycle.adapter_dir / lifecycle.calibration_set))


def record_calibration(rt_ledger, repo: str, *, judge: str, case_set: str, passed: bool, at: float) -> None:
    rt_ledger.set_cursor(repo, "calibration", json.dumps(
        {"judge": judge, "case_set": case_set, "passed": passed, "at": at}, sort_keys=True))

