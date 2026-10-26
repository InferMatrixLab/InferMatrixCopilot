"""``kb serve``: the knowledge service's scheduler.

One process holds the ledger lease for its whole life (a keeper thread renews
it). Every tick:

1. re-sign the control record and the public hold list (the publisher refuses
   to act on a control record older than 10 minutes, so a dead service stops
   all publication within that window);
2. apply the publisher's signed acks and advance each repository's merge flow;
3. per enabled, unpaused repository: intake on its interval, release sweep when
   a release (or the fallback interval) is due;
4. activate a new knowledge snapshot when the knowledge repository's main moved.

Repositories are isolated: an exception in one is recorded and the others go
on. The overturn breaker pauses a repository (and dequeues its open knowledge
PRs) when people close two of its auto-generated PRs unmerged within 24 hours.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from dataclasses import dataclass, field

from . import merge
from .activate import ActivationError, activate, activation_lock
from .archive import make_archive
from .accept import accept_pending
from .audit import audit_main, open_unknown, provenance_problems
from .report import flush_reports
from .companion import publish_companion
from .external import poll_external
from .sweep_audit import audit_sweep, stage_baseline_companion
from .runtime import collect_events, publish, run_intake
from .sweep import UpstreamRepo, detect_release, run_sweep

OVERTURN_WINDOW = 24 * 3600
OVERTURN_LIMIT = 2


@dataclass
class Scheduler:
    rt: object
    tick_seconds: float = 60.0
    intake_every: float = 900.0
    release_every: float = 3600.0
    audit_every: float = 0.0  # v8: provenance is checked every tick
    archive_every: float = 7 * 24 * 3600.0
    log: list[dict] = field(default_factory=list)
    _last: dict[str, float] = field(default_factory=dict)

    def _due(self, key: str, every: float) -> bool:
        now = self.rt.clock()
        if now - self._last.get(key, 0.0) >= every:
            self._last[key] = now
            return True
        return False

    def _improve_cycle(self):
        """Run the weekly improvement cycle when its slot has passed; the
        settings come from the runtime when it carries them, else the env."""
        rt = self.rt
        if rt.traces is None:
            return None
        settings = getattr(rt, "settings", None)
        if settings is None:
            from ..config import Settings

            settings = Settings()
        if not getattr(settings, "improve_enabled", False):
            return None
        from ..improve.cycle import ledger_dir_for, maybe_run_weekly

        if getattr(settings, "improve_evolve_enabled", False):
            from ..improve.cycle import is_due, _cursor
            from ..improve.coordinator import run
            cursor = _cursor(ledger_dir_for(settings))
            checkpoint = ledger_dir_for(settings) / "coordinator.json"
            incomplete = checkpoint.is_file() and json.loads(checkpoint.read_text()).get("state") == "running"
            if incomplete or is_due(rt.clock(), float(cursor.get("last_run_at") or 0), weekday=settings.improve_cycle_weekday,
                      hour=settings.improve_cycle_hour):
                return run(settings, rt.traces, now=rt.clock())
            return None

        return maybe_run_weekly(rt.traces, settings, ledger_dir_for(settings), now=rt.clock())

    def _record(self, repo: str, event: str, **detail) -> None:
        entry = {"at": self.rt.clock(), "repo": repo, "event": event, **detail}
        self.log.append(entry)
        path = self.rt.state_dir / "traces" / "scheduler.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def tick(self) -> None:
        rt = self.rt
        if rt.outbox is not None:
            rt.outbox.transition(lambda: None)
            if rt.publisher_public_key is not None:
                merge.apply_acks(rt, rt.outbox.collect_acks(rt.publisher_public_key))
        try:
            from .containment import collect_consumer_updates, refresh_policy, native_ack
            from .maintenance_store import MaintenanceStore
            collect_consumer_updates(rt, store=MaintenanceStore(rt.ledger, lease_owner=rt.lease_owner))
            refresh_policy(rt)  # freshness continues during an intake pause
            native_ack(rt)  # readiness of this lease-owning consumer, not an installer
        except Exception as exc:
            self._record("*", "containment_error", error=repr(exc))
        for lifecycle in rt.registry.values():
            if not lifecycle.enabled:
                continue
            try:
                self._repo_tick(lifecycle)
            except Exception as exc:  # isolate repositories from each other
                self._record(lifecycle.repo, "error", error=repr(exc), trace=traceback.format_exc()[-2000:])
        try:  # the meta-improvement engine's weekly cycle (design §4): same
              # lease, its own slot, isolated like every other tick item
            report = self._improve_cycle()
            if report is not None:
                self._record("*", "improve_cycle", units=report.get("units"),
                             proposals=len(report.get("proposals_opened") or []))
        except Exception as exc:
            self._record("*", "error", error=repr(exc), trace=traceback.format_exc()[-2000:])
        if self._due("archive", self.archive_every):
            try:  # the weekly off-machine copy of the traces (pulled by the publisher)
                archive = make_archive(rt)
                if archive is not None:
                    self._record("*", "archive", archive=archive.name)
            except Exception as exc:
                self._record("*", "error", error=repr(exc), trace=traceback.format_exc()[-2000:])
        if self._due("audit", self.audit_every):
            try:  # every knowledge change on main must have been recorded
                for finding in audit_main(rt, self._pause):
                    self._record("*", "audit", finding=finding)
            except Exception as exc:
                self._record("*", "error", error=repr(exc), trace=traceback.format_exc()[-2000:])
        try:  # `kb accept-unknown` requests: an unknown commit judged through the gate
            for event in accept_pending(rt):
                self._record("*", "accept", detail=event)
        except Exception as exc:
            self._record("*", "error", error=repr(exc), trace=traceback.format_exc()[-2000:])
        if self._due("external", self.intake_every) and not merge.is_paused(rt.ledger, "*"):
            try:  # knowledge PRs the service did not open (source 4)
                for event in poll_external(rt):
                    self._record("*", "external", detail=event)
            except Exception as exc:
                self._record("*", "error", error=repr(exc), trace=traceback.format_exc()[-2000:])
        try:
            sha = rt.knowledge.fetch()
            with activation_lock(rt.state_dir):  # the pin cannot change under this decision
                pin = rt.ledger.get_cursor("*", "rollback_pin")
                pinned_main = json.loads(pin)["main"] if pin else None
                if pinned_main == sha:
                    pass  # rolled back away from this main: stay there until main moves on
                elif sha != rt.ledger.active_snapshot():
                    blocked = open_unknown(rt) or provenance_problems(rt, sha)
                    if blocked:
                        # a knowledge change that passed no gate is on main: nothing
                        # new is served until it is reverted (design v8, G5 exception)
                        self._record("*", "activation_blocked", unknown=[s[:12] for s in blocked])
                    else:
                        activate(rt, sha, locked=True)
                        self._record("*", "activated", snapshot=sha)
        except ActivationError as exc:
            rt.ledger.enqueue_human("*", f"snapshot activation refused: {exc}")
            self._record("*", "activation_refused", error=str(exc))
        except Exception as exc:  # a fetch failure etc. must not stop the service
            self._record("*", "activation_error", error=repr(exc))

        try:
            from .reconcile import settle_reconciled_holds
            from .external import settle_closed_holds
            resolved = settle_reconciled_holds(rt) + settle_closed_holds(rt)
            if resolved:
                self._record("*", "holds_resolved", ids=resolved)
        except Exception as exc:
            self._record("*", "hold_settlement_error", error=repr(exc))

        try:
            from .maintenance import run_due
            result = run_due(rt, on_correction=lambda event: self._record("*", "correction", **event))
            report = result["maintenance"]  # persisted Shanghai slot; independent of releases
            if report is not None:
                self._record("*", "maintenance", **report)
        except Exception as exc:
            self._record("*", "maintenance_error", error=repr(exc), trace=traceback.format_exc()[-2000:])

    def _repo_tick(self, lifecycle) -> None:
        rt = self.rt
        for event in merge.advance(rt, lifecycle):
            self._record(lifecycle.repo, "merge", detail=event)
        self._overturn_breaker(lifecycle)
        if merge.is_paused(rt.ledger, lifecycle.repo):
            return
        # durable recovery: anything gated but not yet handed over (a crash or an
        # exception between staging and publishing) is published now; publish
        # is idempotent per change set
        for changeset in rt.ledger.changesets(lifecycle.repo, ("gated",)):
            status = publish(rt, lifecycle, changeset["id"])
            self._record(lifecycle.repo, "published", changeset=changeset["id"], status=status)
        for changeset in rt.ledger.changesets(lifecycle.repo, ("companion_staged",)):
            try:  # a companion that cannot be published yet never blocks the rest of the tick
                status = publish_companion(rt, lifecycle, changeset["id"])
            except Exception as exc:
                self._record(lifecycle.repo, "error", error=f"companion {changeset['id']}: {exc!r}")
                continue
            self._record(lifecycle.repo, "published", changeset=changeset["id"], status=status)
        if self._due(f"intake:{lifecycle.repo}", self.intake_every):
            new = collect_events(rt, lifecycle)
            changeset_id = run_intake(rt, lifecycle)
            if changeset_id:
                status = publish(rt, lifecycle, changeset_id)
                self._record(lifecycle.repo, "intake", new_events=new, changeset=changeset_id, status=status)
        if lifecycle.full_name and self._due(f"release:{lifecycle.repo}", self.release_every):
            upstream = UpstreamRepo(rt.state_dir / "upstream" / f"{lifecycle.repo}.git", lifecycle.full_name)
            previous_baseline = rt.ledger.get_cursor(lifecycle.repo, "sweep_baseline") or ""
            observation = {}
            sweep = detect_release(rt, lifecycle, upstream, observation=observation)
            target_baseline = (sweep or {}).get("to_sha") or rt.ledger.get_cursor(lifecycle.repo, "sweep_baseline") or ""
            source_status = "not_observed"
            if observation.get("status") == "observed" and previous_baseline and target_baseline:
                source_status = "no_code_update" if previous_baseline == target_baseline else "code_update_detected"
            elif observation.get("status") == "observed" and target_baseline and not previous_baseline:
                source_status = "baseline_initialized"
            rt.ledger.set_cursor(lifecycle.repo, "maintenance_source_observation", json.dumps({
                "checked_at": rt.clock(), "scope": "release_baseline", "trigger": lifecycle.release.trigger,
                "status": source_status, "from_sha": previous_baseline, "to_sha": target_baseline,
                "observation_status": observation.get("status", "not_observed"),
                "observed_tag": observation.get("tag", ""),
            }, sort_keys=True))
            if sweep is not None:
                # the release audit feeds the sweep hints; baseline drift becomes a
                # companion PR that runs in parallel and never blocks the sweep
                base_sha = rt.knowledge.fetch()  # one revision for the audit and the sweep
                hints, audit = audit_sweep(rt, lifecycle, sweep, upstream, base_sha)
                try:  # the baseline companion never blocks the sweep
                    stage_baseline_companion(rt, lifecycle, rt.lease_owner, sweep, audit, base_sha)
                except Exception as exc:
                    self._record(lifecycle.repo, "error", error=f"baseline companion: {exc!r}",
                                 trace=traceback.format_exc()[-2000:])
                    key = f"baseline_companion_failed:{sweep.get('tag')}:{sweep.get('to_sha')}"
                    if not rt.ledger.get_cursor(lifecycle.repo, key):
                        rt.ledger.set_cursor(lifecycle.repo, key, "1")
                        rt.ledger.enqueue_human(lifecycle.repo,
                                                f"could not draft the adapter baseline update: {exc}")
                report = run_sweep(rt, lifecycle, rt.lease_owner, sweep, upstream, audit_hints=hints,
                                   reconciliation=len(audit.reconciliation) if audit is not None else None,
                                   base_sha=base_sha)
                for changeset_id in report["changesets"]:
                    publish(rt, lifecycle, changeset_id)

                self._record(lifecycle.repo, "sweep", tag=sweep["tag"], reason=sweep["reason"],
                             changesets=report["changesets"], breaker=report["breaker"])
        for run_id in flush_reports(rt, lifecycle):  # queued by settled sweeps; retried until done
            self._record(lifecycle.repo, "sweep_report", run_id=run_id)

    def _overturn_breaker(self, lifecycle) -> None:
        rt = self.rt
        # only the service's own PRs: an author closing their own PR is no overturn
        recent = [cs for cs in rt.ledger.changesets(lifecycle.repo, ("closed",))
                  if cs["kind"] not in ("external", "companion")
                  and rt.clock() - float(cs["updated_at"]) < OVERTURN_WINDOW]
        state = rt.ledger.repo_state(lifecycle.repo)
        if len(recent) >= OVERTURN_LIMIT and not state["paused"]:
            self._pause(lifecycle.repo, f"overturn breaker: {len(recent)} knowledge PRs closed unmerged within 24h")

    def _pause(self, repo: str, reason: str) -> None:
        """Pause a repository ("*": all of them): no merge is issued, and the
        publisher refuses items of the older generation."""
        rt = self.rt
        if rt.ledger.repo_state(repo)["paused"]:
            rt.ledger.enqueue_human(repo if repo != "*" else next(iter(rt.registry)), reason)
            return
        # publish the pause in the same locked transition as the state change,
        # so the publisher sees it at once
        if rt.outbox is not None:
            rt.outbox.transition(lambda: rt.ledger.bump_generation(repo, pause=True, reason=reason))
        else:
            rt.ledger.bump_generation(repo, pause=True, reason=reason)
        rt.ledger.enqueue_human(repo if repo != "*" else next(iter(rt.registry)), reason)
        self._record(repo, "paused", reason=reason)

    def _containment_heartbeat(self, stop, owner):
        """A separate SQLite connection keeps freshness independent of models."""
        from types import SimpleNamespace
        from .ledger import Ledger
        from .containment import collect_consumer_updates, refresh_policy, native_ack
        from .maintenance_store import MaintenanceStore
        ledger = Ledger(self.rt.ledger.path, clock=self.rt.clock)
        heartbeat_rt = SimpleNamespace(**{**vars(self.rt), "ledger": ledger, "lease_owner": owner})
        try:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    ledger.heartbeat(owner)  # refuses a lost scheduler lease
                    store = MaintenanceStore(ledger, lease_owner=owner)
                    collect_consumer_updates(heartbeat_rt, store=store)
                    if stop.is_set():
                        break
                    refresh_policy(heartbeat_rt)
                    if stop.is_set():
                        break
                    native_ack(heartbeat_rt)
                except Exception as exc:
                    self._record("*", "containment_heartbeat_error", error=repr(exc))
                stop.wait(max(1, 45 - (time.monotonic() - started)))
        finally:
            ledger.close()

    def serve(self, stop: threading.Event | None = None, *, once: bool = False) -> None:
        stop = stop or threading.Event()
        heartbeat_stop = threading.Event()
        with self.rt.ledger.lease() as owner:
            self.rt.lease_owner = owner
            heartbeat = None
            if getattr(self.rt, "containment_policy_dir", None) or os.environ.get("KB_CONTAINMENT_POLICY_DIR"):
                heartbeat = threading.Thread(target=self._containment_heartbeat, args=(heartbeat_stop, owner),
                                             name="kb-containment-heartbeat", daemon=True)
                heartbeat.start()
            try:
                while not stop.is_set():
                    started = time.monotonic()
                    self.tick()
                    if once:
                        return
                    stop.wait(max(1.0, self.tick_seconds - (time.monotonic() - started)))
            finally:
                heartbeat_stop.set()
                if heartbeat is not None:
                    heartbeat.join(timeout=45)
                self.rt.lease_owner = None
