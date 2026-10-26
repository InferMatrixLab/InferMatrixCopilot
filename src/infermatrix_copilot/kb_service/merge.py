"""The merge flow after a change set is handed to the publisher (design v8).

    gated ─publish→ pr_requested ─ack(pr, head)→ pr_open ─sign→ merge_requested
      ─ack(merged, merge sha)→ merged ─activation→ activated

The service signs a verdict for the PR's exact head and hands it to the
publisher in a ``merge`` item; the publisher, the only thing that merges
knowledge PRs, runs the gate locally on the exact tree that would land and
merges only if it passes. A refused merge is never retried as is: context that
moved or a PR that no longer applies is rebuilt on current main, a real
problem with the change goes to people, and a transient failure (main moving,
GitHub or git trouble) is signed again next pass. At most one merge per
repository is in flight.

Anything else off the happy path goes to people or stops: a head that no
longer matches what was signed (``head_changed`` → human), a PR closed
unmerged (``closed``). A paused repository gets no ``merge`` item, and the
publisher refuses items of an older generation: since it is the only thing
that merges, a pause takes effect at once (nothing to dequeue or draft).

The service only READS GitHub; every write is an outbox item.
"""

from __future__ import annotations

import os
from typing import Any

from ..knowledge_service.signing import sign
from ..knowledge_service.verdict import build_verdict, manifest_from_files

IN_FLIGHT = ("pr_requested", "pr_open", "merge_requested", "rebuild_needed")
# margin for the publisher's clock when deciding an item has certainly expired
CLOCK_SKEW = 5 * 60
# the publisher could not confirm the signed upstream facts: signed again and
# retried each round, to people once it kept failing this long (design v8 §8.2)
FACTS_MERGE = "upstream facts:"
FACTS_ESCALATE_AFTER = 24 * 3600
# local-gate refusals the service answers by rebuilding on current main
REBUILD_GATE = ("context changed since the verdict", "pages changed since the consistency judgement",
                "does not merge cleanly", "signed patch manifest does not match")
# a refused merge caused by how the repository is set up, not by the change:
# refining cannot fix it, people must (it would only loop)
CONFIG_MERGE = ("must allow a direct merge",)
# a refused local-gate merge that is about the moment, not the change: sign again
TRANSIENT_MERGE = ("main kept moving", "not merging:", "head of PR", "head moved", "git ", "gh ",
                   "did not merge", "is not open", "verdict: ")
from .config import KNOWLEDGE_REPO_ENV, DEFAULT_KNOWLEDGE_REPOSITORY, knowledge_repository


# the transition each outbox item kind drives: (state before, state after)
TRANSITIONS = {
    "open_pr": ("pr_requested", "pr_open"),
    "open_companion_pr": ("pr_requested", "companion_open"),
    "merge": ("merge_requested", "merged"),
    "open_revert_pr": ("pr_requested", "revert_open"),
}


def is_paused(ledger, repo: str) -> bool:
    """A repository is paused when it, or everything (``*``), is paused."""
    return bool(ledger.repo_state(repo)["paused"] or ledger.repo_state("*")["paused"])


def issue_once(rt, repo: str, changeset: dict, kind: str, body: dict) -> bool:
    """Issue ``kind`` for this change set unless the same action is already
    pending and unexpired. The pending item's identity is stored so its ack,
    and only its ack, can advance the change set."""
    pending = changeset.get("pending_item")
    if pending and pending.get("kind") == kind and float(pending.get("expires_at", 0)) > rt.clock():
        return False
    item = rt.outbox.issue(repo, kind, body, **({"ttl": 60} if body.get("maintenance_required") else {}))
    rt.ledger.update_changeset(changeset["id"], pending_item={
        "id": item.id, "kind": kind, "expires_at": item.expires_at, "issued_at": item.issued_at})
    return True


def apply_acks(rt, acks: list[dict]) -> None:
    """Advance change sets from the publisher's signed acks. An ack applies
    only to the pending item it answers and only from the matching state: a
    late ack for a superseded duplicate, or one arriving after the change set
    moved on, changes nothing."""
    for ack in acks:
        if ack.get("kind") == "post_findings" and "invalid" not in ack:
            from .external import findings_delivered

            findings_delivered(rt, ack)
            continue
        changeset_id = str(ack.get("changeset_id") or "")
        if not changeset_id or "invalid" in ack:
            continue
        try:
            changeset = rt.ledger.changeset(changeset_id)
        except KeyError:
            continue
        kind = ack.get("kind")
        pending = changeset.get("pending_item") or {}
        if kind == "merge" and ack.get("ok") and changeset["status"] == "merged" \
                and ack.get("merge_sha") and ack.get("merge_sha") == changeset.get("merge_sha"):
            # GitHub showed the merge before its receipt arrived: the receipt's
            # post-merge check still counts
            _record_post_check(rt, rt.ledger.changeset(changeset_id), ack.get("post_check", ""),
                               ack.get("problems") or [])
            continue
        if kind not in TRANSITIONS or pending.get("id") != ack.get("item_id"):
            continue
        before, after = TRANSITIONS[kind]
        if changeset["status"] != before:
            continue
        if kind == "merge":
            _apply_merge_ack(rt, changeset, ack)
            continue
        if not ack.get("ok"):
            rt.ledger.update_changeset(changeset_id, status="failed", pending_item=None)
            rt.ledger.enqueue_human(changeset["repo"], f"publisher refused {kind}: {ack.get('error', '')}",
                                    changeset_id)
            continue
        fields = {"status": after, "pending_item": None}
        if kind in ("open_pr", "open_companion_pr", "open_revert_pr"):
            fields.update(pr_number=int(ack["pr"]), head_sha=str(ack["head_sha"]),
                          branch=str(ack.get("branch", "")))
        rt.ledger.update_changeset(changeset_id, **fields)


def _pause_repo(rt, repo: str, reason: str) -> None:
    """Stop automatic merging for ``repo`` (a new generation voids every item)."""
    rt.outbox.transition(lambda: rt.ledger.bump_generation(repo, pause=True, reason=reason))


def _record_post_check(rt, changeset: dict, result: str, problems: list) -> None:
    """``passed`` / ``failed`` are final; anything else (``unknown``: the
    publisher could not run it, or recovered the merge after a crash) is run
    by the service itself on the next pass (``verify_merged``)."""
    if changeset["detail"].get("post_check") in ("passed", "failed"):
        return
    rt.ledger.update_changeset(changeset["id"], detail={**changeset["detail"], "post_check": result or "unknown"})
    if result == "failed":
        # main moved in the seconds before the merge and what landed does not
        # pass: stop merging this repository; people decide on a revert
        reason = (f"post-merge check failed on {str(changeset.get('merge_sha') or '')[:12]} "
                  f"(PR #{changeset['pr_number']}): " + "; ".join(map(str, problems))[:400])
        _pause_repo(rt, changeset["repo"], reason)
        rt.ledger.enqueue_human(changeset["repo"], reason, changeset["id"])


def verify_merged(rt, lifecycle) -> list[str]:
    """Run the post-merge check for merges whose result is not known yet, on
    the service's own clone. A check that cannot run is simply retried."""
    from pathlib import Path

    from . import local_gate

    events = []
    for changeset in rt.ledger.changesets(lifecycle.repo, ("merged",)):
        detail = changeset["detail"]
        if not detail.get("verdict_issued_at") or detail.get("post_check") in ("passed", "failed"):
            continue  # not a v8 merge, or already settled
        merge_sha = str(changeset.get("merge_sha") or "")
        if not merge_sha:
            try:
                merge_sha = str(_pr_state(rt, int(changeset["pr_number"])).get("merge_commit_sha") or "")
            except Exception:  # noqa: BLE001 - GitHub trouble: next pass
                continue
            if not merge_sha:
                continue
            rt.ledger.update_changeset(changeset["id"], merge_sha=merge_sha)
            changeset = rt.ledger.changeset(changeset["id"])
        try:
            rt.knowledge.fetch()
            verdict = sign_verdict(rt, changeset)["payload"]
            problems = local_gate.post_merge_problems(
                Path(rt.knowledge.path), repository=knowledge_repository(),
                pr={"number": int(changeset["pr_number"]), "state": "open",
                    "head": {"sha": changeset["head_sha"]}, "labels": []},
                head_sha=changeset["head_sha"], merge_sha=merge_sha, verdict=verdict,
                public_key=rt.outbox._key.public_key(), now=rt.clock())
        except Exception as exc:  # noqa: BLE001 - e.g. the clone cannot fetch: next pass
            events.append(f"post_check_retry {changeset['id']}: {exc}")
            continue
        _record_post_check(rt, changeset, "failed" if problems else "passed", problems)
        events.append(f"post_check {'failed' if problems else 'passed'} {changeset['id']}")
    return events


def _apply_merge_ack(rt, changeset: dict, ack: dict) -> None:
    changeset_id, repo = changeset["id"], changeset["repo"]
    if ack.get("ok"):
        merge_sha = str(ack.get("merge_sha") or "")
        rt.ledger.update_changeset(changeset_id, status="merged", merge_sha=merge_sha or None, pending_item=None)
        record_retirements(rt, changeset)
        _outcome(rt, changeset, "merged", merge_sha=merge_sha)
        _record_post_check(rt, rt.ledger.changeset(changeset_id), ack.get("post_check", ""),
                           ack.get("problems") or [])
        return
    text = " ".join([str(ack.get("error") or ""), *map(str, ack.get("problems") or [])])
    detail = {k: v for k, v in changeset["detail"].items() if k != "facts_failing_since"}
    if FACTS_MERGE in text:
        since = float(changeset["detail"].get("facts_failing_since") or rt.clock())
        if rt.clock() - since >= FACTS_ESCALATE_AFTER:
            rt.ledger.update_changeset(changeset_id, status="gate_failed", pending_item=None,
                                       detail={**detail, "gate_problems": [text[:400]]})
            rt.ledger.enqueue_human(repo, f"PR #{changeset['pr_number']}: the publisher could not confirm its "
                                          f"upstream facts for 24 hours: {text[:300]}", changeset_id)
            _outcome(rt, changeset, "gate_failed", problems=[text[:400]])
        else:
            rt.ledger.update_changeset(changeset_id, status="pr_open", pending_item=None,
                                       detail={**detail, "facts_failing_since": since})
        return
    if detail != changeset["detail"]:  # any other answer ends a run of fact failures
        rt.ledger.update_changeset(changeset_id, detail=detail)
        changeset = rt.ledger.changeset(changeset_id)
    if any(m in text for m in REBUILD_GATE):
        rt.ledger.update_changeset(changeset_id, status="rebuild_needed", pending_item=None,
                                   detail={**changeset["detail"], "rebuild_because": text[:500]})
    elif any(m in text for m in TRANSIENT_MERGE) and not ack.get("problems"):
        rt.ledger.update_changeset(changeset_id, status="pr_open", pending_item=None)
    else:  # the local gate refused the change itself: it is not merged
        from .refine import REFINABLE

        problems = (ack.get("problems") or [])[:50]
        if changeset["kind"] in REFINABLE and not any(m in text for m in CONFIG_MERGE):
            # our own change: refined with these reasons and checked again
            rt.ledger.update_changeset(changeset_id, status="refine_needed", pending_item=None,
                                       detail={**changeset["detail"], "gate_problems": problems})
        else:
            rt.ledger.update_changeset(changeset_id, status="gate_failed", pending_item=None,
                                       detail={**changeset["detail"], "gate_problems": problems})
            if changeset["kind"] == "external" and not any(m in text for m in CONFIG_MERGE):
                # someone else's PR: its author is told why (checked again daily)
                from .external import post_findings

                post_findings(rt, rt.registry.get(repo), int(changeset["pr_number"]), str(changeset["head_sha"]),
                              "gate_failed", [str(p) for p in problems] or [text[:400]])
            else:
                rt.ledger.enqueue_human(repo, f"local gate refused PR #{changeset['pr_number']}: {text[:400]}",
                                        changeset_id)
        _outcome(rt, changeset, "gate_failed", problems=problems[:20])


def sign_verdict(rt, changeset: dict) -> dict:
    """The signed verdict for this change set's PR at its recorded head."""
    detail = changeset["detail"]
    data = rt.load_changeset_files(changeset["id"])
    base = rt.knowledge.knowledge_files(detail["base_sha"])
    head = {**base, **data["files"]}
    for rel in data.get("deleted", []):
        head.pop(rel, None)
    touched = set(data["files"]) | set(data.get("deleted", []))
    manifest = manifest_from_files({k: v for k, v in base.items() if k in touched},
                                   {k: v for k, v in head.items() if k in touched})
    # an external PR's manifest comes from git and covers every changed path
    manifest = detail.get("manifest") or manifest
    decision = detail["decision"]
    blocks = [{k: b[k] for k in ("block_id", "kind", "path", "rule_id", "op", "sha256", "verdict", "model")}
              for b in decision["blocks"]]
    consistency = [{"owner_dir": c["owner_dir"], "verdict": c["verdict"], "pages": c["pages"]}
                   for c in decision["consistency"]]
    verdict = build_verdict(
        repository=knowledge_repository(), pr=int(changeset["pr_number"]), head_sha=changeset["head_sha"],
        context_base_sha=detail["base_sha"], manifest=manifest, blocks=blocks, consistency=consistency,
        release=detail["release"], upstream=decision.get("upstream") or {}, facts=decision.get("facts") or [],
        models={"generator": detail["generator"], "judge": detail["judge"]},
        source="auto", issued_at=rt.clock(),
    )
    return sign("kb-gate-verdict", verdict, rt.outbox._key)


def _pr_state(rt, number: int) -> dict:
    return rt.github.get(f"/repos/{knowledge_repository()}/pulls/{number}")


REBUILD_MARK = "rebuild of "


def _rebuild_of(rt, repo: str, old_id: str) -> str | None:
    """A change set already staged as the rebuild of ``old_id`` (its evidence
    carries the mark, written atomically with the staging), so an interrupted
    rebuild is resumed instead of staged twice."""
    for candidate in rt.ledger.changesets(repo, ("gated", "failed", "human", *IN_FLIGHT)):
        if candidate["kind"] != "rebuild":
            continue
        try:
            evidence = rt.load_changeset_files(candidate["id"]).get("evidence") or []
        except (OSError, ValueError):
            continue
        if any(str(item.get("source_reference") or "") == REBUILD_MARK + old_id for item in evidence):
            return candidate["id"]
    return None


def rebuild(rt, lifecycle, changeset: dict, why: str) -> str | None:
    """Rebuild a change set whose verdict can no longer verify on the current
    base (context pages moved, or the PR no longer applies): re-apply its
    operations to current main and gate them again. Only a rebuild that passes
    the gate replaces the PR; the old one then waits in ``superseding`` until
    its close is observed. Anything else goes to people with the old PR left
    open. Needs the scheduler's lease; returns the new change set id or None."""
    from ..knowledge_service.lifecycle import LifecycleError
    from ..knowledge_service.ops import KnowledgeOperation, apply_operations
    if changeset["kind"] in {"correction", "maintenance_calibration"} or changeset["detail"].get("maintenance_run"):
        rt.ledger.update_changeset(changeset["id"], status="human", pending_item=None)
        rt.ledger.enqueue_human(lifecycle.repo, "maintenance correction requires fresh budgeted source review; automatic generic rebuild refused", changeset["id"])
        return None
    from .runtime import gate_and_stage, publish, require_settled_reviews

    owner = getattr(rt, "lease_owner", None)
    if not owner:
        return None
    require_settled_reviews(rt, lifecycle.repo, kinds=('rebuild',), binding={'rebuild_of': changeset['id']})

    def to_people(reason: str) -> None:
        rt.ledger.update_changeset(changeset["id"], status="rebuild_failed")
        rt.ledger.enqueue_human(lifecycle.repo, reason, changeset["id"])

    detail = changeset["detail"]
    raw = detail.get("operations") or []
    if not raw:  # a mechanical change set (index fixes): nothing to re-apply
        to_people(f"the local gate refused it ({why}); no operations to rebuild")
        return None
    new_id = _rebuild_of(rt, lifecycle.repo, changeset["id"])
    if new_id is None:
        base_sha = rt.knowledge.fetch()
        base = rt.knowledge.knowledge_files(base_sha)
        external = rt.knowledge.external_texts(base_sha)
        operations = [KnowledgeOperation.from_dict(item) for item in raw]
        try:
            result = apply_operations(base, operations, release=detail["release"], today=rt.today())
        except LifecycleError as exc:
            to_people(f"cannot rebuild on current main: {exc}")
            return None
        evidence = [*(rt.load_changeset_files(changeset["id"]).get("evidence") or []),
                    {"source_reference": REBUILD_MARK + changeset["id"], "title": why}]
        new_id = gate_and_stage(rt, lifecycle, owner, kind="rebuild", base=base, base_sha=base_sha,
                                external=external, operations=operations, result=result,
                                evidence=evidence, event_ids=[], release=detail["release"],
                                draft_keys=[],
                                # a rebuild continues the change's refine lineage: the
                                # limit never resets because context moved meanwhile
                                extra_detail={"rebuild_of": changeset['id'],
                                              **{k: detail[k] for k in ("refine_round", "refine_history", "generator")
                                                 if k in detail}})
    staged = rt.ledger.changeset(new_id)
    if staged["status"] not in ("gated", *IN_FLIGHT):
        # the rebuild itself did not pass the gate: nothing replaces the PR
        to_people(f"rebuild {new_id} on current main did not pass the gate ({staged['status']})")
        return None
    no_pr = changeset.get("pr_number") is None
    rt.ledger.update_changeset(changeset["id"], status="superseded" if no_pr else "superseding",
                               detail={**detail, "rebuilt_as": new_id, "superseded_because": why})
    _outcome(rt, changeset, "superseded", reason=why, replaced_by=new_id)
    if not no_pr:
        _request_close(rt, lifecycle.repo, rt.ledger.changeset(changeset["id"]))
    publish(rt, lifecycle, new_id)
    return new_id


def _request_close(rt, repo: str, changeset: dict) -> None:
    issue_once(rt, repo, changeset, "close", {
        "changeset_id": changeset["id"], "pr": int(changeset["pr_number"]),
        "reason": (f"Superseded by change set {changeset['detail'].get('rebuilt_as')}, rebuilt on "
                   f"current main ({changeset['detail'].get('superseded_because', '')}).")})


def advance(rt, lifecycle) -> list[str]:
    """One pass over this repository's in-flight change sets. Returns events."""
    events: list[str] = []
    if rt.outbox is None:
        return events
    paused = is_paused(rt.ledger, lifecycle.repo)
    merging = rt.ledger.changesets(lifecycle.repo, ("merge_requested",))
    for changeset in rt.ledger.changesets(lifecycle.repo, (*IN_FLIGHT, "superseding",
                                                           "companion_open", "revert_open")):
        number = changeset["pr_number"]
        if number is None:
            continue
        try:
            pr: dict[str, Any] = _pr_state(rt, int(number))
        except Exception:  # noqa: BLE001 - GitHub down or rate-limited
            events.append(f"observe_failed {changeset['id']}")
            continue
        if changeset["status"] == "companion_open":
            # people review and merge companions; the service only follows them
            waiting = [cs for cs in rt.ledger.changesets(lifecycle.repo, ("companion_pending",))
                       if cs["detail"].get("companion") == changeset["id"]]
            target = changeset["detail"].get("for_changeset")
            if target and not waiting:
                # the companion names its change; a crash between staging the
                # companion and linking the change leaves it "human" and unlinked
                parent = rt.ledger.changeset(target)
                if parent["status"] in ("companion_pending", "human") and not parent["detail"].get("rebuilt_as"):
                    waiting = [parent]
            if pr.get("merged"):
                if waiting and (paused or not getattr(rt, "lease_owner", None)):
                    continue  # rebuild only unpaused and under the scheduler's lease: next pass
                events.append(f"companion_merged {changeset['id']}")
                retry = False
                for knowledge in waiting:
                    try:
                        new_id = rebuild(rt, lifecycle, knowledge, "its companion PR merged")
                    except Exception as exc:  # noqa: BLE001 - e.g. main not fetchable: next pass
                        retry = True
                        events.append(f"rebuild_retry {knowledge['id']}: {exc}")
                        continue
                    events.append(f"rebuilt {knowledge['id']} as {new_id}" if new_id
                                  else f"rebuild_failed {knowledge['id']}")
                if not retry:  # only once every waiting change was handled
                    rt.ledger.update_changeset(changeset["id"], status="merged",
                                               merge_sha=str(pr.get("merge_commit_sha") or ""))
            elif pr.get("state") == "closed":
                rt.ledger.update_changeset(changeset["id"], status="closed")
                for knowledge in waiting:
                    rt.ledger.update_changeset(knowledge["id"], status="human")
                    rt.ledger.enqueue_human(lifecycle.repo, f"companion PR #{number} was closed unmerged",
                                            knowledge["id"])
                events.append(f"companion_closed {changeset['id']}")
            continue
        if changeset["status"] == "revert_open":
            # people merge reverts; an exact one disposes of the unknown change
            from .audit import settle_revert

            if pr.get("merged"):
                try:
                    exact = settle_revert(rt, changeset, pr)
                except Exception as exc:  # noqa: BLE001 - e.g. the clone cannot fetch yet: next pass
                    events.append(f"revert_settle_retry {changeset['id']}: {exc}")
                    continue
                events.append(f"revert_merged {changeset['id']} exact={exact}")
            elif pr.get("state") == "closed":
                rt.ledger.update_changeset(changeset["id"], status="closed")
                rt.ledger.enqueue_human(lifecycle.repo, (
                    f"revert PR #{number} was closed: {changeset['detail'].get('reverts', '')[:12]} stays "
                    "undisposed and activation stays blocked until it is reverted"), changeset["id"])
                events.append(f"revert_closed {changeset['id']}")
            continue
        if changeset["status"] == "superseding":
            # the replaced PR stays tracked until GitHub shows it closed; the
            # close item is re-issued whenever the previous one expired
            if pr.get("state") == "closed" or pr.get("merged"):
                rt.ledger.update_changeset(changeset["id"], status="superseded", pending_item=None)
                events.append(f"superseded {changeset['id']}")
            else:
                _request_close(rt, lifecycle.repo, changeset)
            continue
        if pr.get("merged"):
            rt.ledger.update_changeset(changeset["id"], status="merged",
                                       merge_sha=str(pr.get("merge_commit_sha") or ""))
            record_retirements(rt, changeset)
            _outcome(rt, changeset, "merged", merge_sha=str(pr.get("merge_commit_sha") or ""))
            events.append(f"merged {changeset['id']}")
            continue  # its post-merge check: the receipt, or verify_merged
        if pr.get("state") == "closed":
            rt.ledger.update_changeset(changeset["id"], status="closed")
            _outcome(rt, changeset, "closed_unmerged")  # overturned by people: a negative label
            events.append(f"closed {changeset['id']}")
            continue
        if paused:
            continue  # merges, closes and reverts are still followed above; nothing is signed
        head = str((pr.get("head") or {}).get("sha") or "")
        if head != changeset["head_sha"] and changeset["kind"] == "external":
            # authors push: the new head is simply judged again at the next poll
            rt.ledger.update_changeset(changeset["id"], status="head_changed", pending_item=None)
            events.append(f"head_changed {changeset['id']}")
            continue
        if head != changeset["head_sha"]:
            _outcome(rt, changeset, "head_changed", head_sha=head)
            rt.ledger.update_changeset(changeset["id"], status="head_changed")
            rt.ledger.enqueue_human(lifecycle.repo, f"PR #{number} head changed after signing", changeset["id"])
            events.append(f"head_changed {changeset['id']}")
            continue
        if changeset["status"] == "merge_requested":
            pending = changeset.get("pending_item") or {}
            if pending.get("kind") == "merge" and float(pending.get("expires_at") or 0) + CLOCK_SKEW < rt.clock():
                # expired unanswered: the publisher never ran it (or its record
                # is lost); sign again. A merge it did run still shows as merged.
                rt.ledger.update_changeset(changeset["id"], status="pr_open", pending_item=None)
                events.append(f"merge_expired {changeset['id']}")
            continue
        if changeset["status"] == "rebuild_needed":
            if changeset["kind"] == "correction":
                rt.ledger.update_changeset(changeset["id"], status="human", pending_item=None)
                rt.ledger.enqueue_human(lifecycle.repo, "correction context changed; fresh source audit and budgeted correction required", changeset["id"])
                continue
            if changeset["kind"] == "external":
                # nothing of ours to rebuild: judge the PR again on current main
                rt.ledger.update_changeset(changeset["id"], status="stale_context", pending_item=None)
                events.append(f"stale_context {changeset['id']}")
            elif getattr(rt, "lease_owner", None):  # only the scheduler (holding the lease) rebuilds
                new_id = rebuild(rt, lifecycle, changeset, changeset["detail"].get("rebuild_because", ""))
                events.append(f"rebuilt {changeset['id']} as {new_id}" if new_id
                              else f"rebuild_failed {changeset['id']}")
            continue
        if changeset["status"] == "pr_open":
            if merging:
                continue  # one merge in flight per repository
            maintenance = {}
            if changeset["kind"] == "correction":
                from .maintenance_policy import correction_publishable, required_ci
                from .containment import merge_authorization
                try:
                    reason = correction_publishable(rt, changeset)
                    if reason:
                        events.append(f"correction_held {changeset['id']}: {reason}")
                        continue
                    required_ci(rt, changeset)
                    maintenance = {"maintenance_required": True, "maintenance": merge_authorization(rt, changeset)}
                except (ValueError, RuntimeError, OSError) as exc:
                    events.append(f"correction_held {changeset['id']}: {exc}")
                    continue
            envelope = sign_verdict(rt, changeset)
            if issue_once(rt, lifecycle.repo, changeset, "merge", {
                    "changeset_id": changeset["id"], "pr": int(number), "head_sha": changeset["head_sha"],
                    "verdict": envelope, **maintenance}):
                current = rt.ledger.changeset(changeset["id"])
                rt.ledger.update_changeset(changeset["id"], status="merge_requested", detail={
                    **current["detail"], "verdict_issued_at": envelope["payload"]["issued_at"]})
                merging = [changeset]
                events.append(f"merge issued {changeset['id']}")
    from .refine import refine_pending

    if not paused:
        events += refine_pending(rt, lifecycle)
    return events + verify_merged(rt, lifecycle)


def _rule_ids(changeset: dict) -> list[str]:
    return sorted({op.get("new_rule_id") or op["rule_id"] for op in changeset["detail"].get("operations", [])
                   if op.get("new_rule_id") or op.get("rule_id")})


def _outcome(rt, changeset: dict, outcome: str, **result) -> None:
    """What finally happened to a change set: the gold label its decision is scored against."""
    trace = getattr(rt, "trace", None)
    if trace is not None:
        trace("outcome", context={"repo": changeset["repo"], "changeset_id": changeset["id"],
                                  "pr": changeset.get("pr_number"), "rule_ids": _rule_ids(changeset)},
              result={"outcome": outcome, **result})


def record_retirements(rt, changeset: dict) -> None:
    release = changeset["detail"].get("release", "")
    for op in changeset["detail"].get("operations", []):
        if op["kind"] in ("retire", "replace"):
            rt.ledger.record_retirement(changeset["repo"], op["rule_id"], op["page"], release)
            trace = getattr(rt, "trace", None)
            if trace is not None:  # the rule's earlier admission now has a later-retired label
                trace("outcome", context={"repo": changeset["repo"], "changeset_id": changeset["id"],
                                          "rule_ids": [op["rule_id"]]},
                      result={"outcome": "rule_retired", "rule_id": op["rule_id"], "release": release,
                              "reason": op.get("reason", "")})
        elif op["kind"] == "purge":
            rt.ledger.mark_purged(changeset["repo"], op["rule_id"])
    # external PRs carry no operations: what L1 found them retiring/purging
    for item in changeset["detail"].get("retirements", []):
        rt.ledger.record_retirement(changeset["repo"], item["rule_id"], item["page"], release)
    for rule_id in changeset["detail"].get("purges", []):
        rt.ledger.mark_purged(changeset["repo"], rule_id)
