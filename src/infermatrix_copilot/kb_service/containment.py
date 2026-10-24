"""Maintenance authority: append-only holds, signed refreshes and ACK barrier.

No original event, verdict, retirement or admission receipt is rewritten.
Shadow findings never alter the serving policy. Transport ACKs are accepted
only by the authenticated owner-channel entry point, never by scanning JSON.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from ..knowledge_service.containment import (
    ContainmentError, MAX_AGE, MAX_RUNTIME_AGE, POLICY_PURPOSE, PROTOCOL, atomic_json,
    digest, policy_identity, state_lock, text_hash,
)
from ..knowledge_service.signing import canonical_json, sign, verify

DECISION_PURPOSE = "kb-containment-decision"


def _option(rt, name, env, default=None):
    value = getattr(rt, name, None)
    return value if value is not None else os.environ.get(env, default)


def _enabled(rt):
    value = _option(rt, "containment_enforce", "KB_CONTAINMENT_ENFORCE", False)
    return value is True or isinstance(value, str) and value.lower() in {"true", "1"}


def _directory(rt):
    raw = _option(rt, "containment_policy_dir", "KB_CONTAINMENT_POLICY_DIR", "")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink():
        raise ContainmentError("authority policy directory must be absolute and not redirected")
    return path


def _consumers(rt):
    value = _option(rt, "containment_consumers", "KB_CONTAINMENT_CONSUMERS", [])
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or len(set(value)) != len(value) or any(
            not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", v) for v in value):
        raise ContainmentError("registered containment consumer roster is invalid")
    return sorted(value)


def _key(rt):
    key = getattr(getattr(rt, "outbox", None), "_key", None)
    if key is None:
        raise ContainmentError("containment authority requires the service signing key")
    return key


def _assert_runtime_lease(rt):
    owner = getattr(rt,"lease_owner",None)
    if owner is not None and rt.ledger.live_lease()!=owner:
        raise ContainmentError("containment authority lost the scheduler lease")


def _now(rt):
    return getattr(rt, "clock", time.time)()


def _read(directory, key):
    path = directory / "authority.json"
    if not path.exists():
        return {"schema_version": PROTOCOL, "generation": 1, "consumers": [], "decisions": []}
    if path.is_symlink() or not path.is_file():
        raise ContainmentError("authority state must be a regular signed artifact")
    payload = verify(DECISION_PURPOSE, json.loads(path.read_text()), key.public_key())
    if payload.get("schema_version") != PROTOCOL or type(payload.get("generation")) is not int:
        raise ContainmentError("authority state protocol is invalid")
    return payload


def _save(directory, state, key):
    atomic_json(directory / "authority.json", sign(DECISION_PURPOSE, state, key))


def _refresh(rt, directory, state, key):
    _assert_runtime_lease(rt)
    roster = _consumers(rt)
    if state["consumers"] != roster:
        state = {**state, "generation": state["generation"] + 1, "consumers": roster}
        _save(directory, state, key)
    now = _now(rt)
    payload = {**state, "issued_at": now, "expires_at": now + MAX_AGE}
    envelope = sign(POLICY_PURPOSE, payload, key)
    # Co-located native writes share the consumer publication fence too; a
    # service hold cannot race a last check followed by an outward write.
    from contextlib import nullcontext
    from ..knowledge_service.containment import configuration, _publication_lock
    native = configuration()
    fence = (_publication_lock(native) if native["enabled"] and
             Path(native["policy_path"]).absolute() == (directory/"policy.json").absolute() else nullcontext())
    with fence:
        _assert_runtime_lease(rt)
        atomic_json(directory / "policy.json", envelope)
    outbox = Path(rt.state_dir) / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    atomic_json(outbox / "containment-policy.json", envelope)
    return {**payload, "policy_digest": policy_identity(payload)}


def refresh_policy(rt):
    """Refresh signed freshness, without changing policy generation/content."""
    directory = _directory(rt)
    if directory is None:
        return {"enabled": False, "reason": "not_configured"}
    key = _key(rt)
    with state_lock(directory):
        state = _read(directory, key)
        if not (directory / "authority.json").exists():
            _save(directory, state, key)
        return _refresh(rt, directory, state, key)


def _immutable(path, value):
    raw = canonical_json(value)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != raw:
            raise ContainmentError("immutable containment decision conflicts")
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())


def record_hold(rt, unit, finding, *, enforce=False):
    if not isinstance(unit, dict) or not isinstance(finding, dict) or finding.get("outcome") != "contradicted" \
            or not finding.get("reason") or not finding.get("evidence") or not finding.get("reviewer"):
        raise ContainmentError("hold requires an independently reviewed, evidenced contradiction")
    if not re.fullmatch(r"[0-9a-f]{64}", unit.get("unit_id", "")) \
            or text_hash(unit.get("text", "")) != unit.get("content_sha256"):
        raise ContainmentError("hold must bind the exact audited unit bytes")
    identity = digest({"unit": unit, "finding": finding})
    if not enforce or not _enabled(rt):
        trace = getattr(rt, "trace", None)
        if trace:
            trace("outcome", context={"repo": unit["repo"], "playbook": "kb-maintenance"},
                  result={"outcome": "would_hold", "decision_id": identity, "unit_id": unit["unit_id"]})
        return {"id": identity, "status": "would_hold", "unit_id": unit["unit_id"]}
    lifecycle = rt.registry.get(unit["repo"])
    if lifecycle is None or not lifecycle.enabled or not lifecycle.auto_merge:
        raise ContainmentError("serving holds require the configured guarded repository mode")
    directory = _directory(rt)
    if directory is None or not _consumers(rt):
        raise ContainmentError("serving holds require an enrolled consumer roster")
    key = _key(rt)
    with state_lock(directory):
        state = _read(directory, key)
        decisions = {row["unit_id"]: row for row in state["decisions"]}
        previous = decisions.get(unit["unit_id"])
        row = previous or {"unit_id": unit["unit_id"], "repo": unit["repo"], "page": unit["page"],
                           "block_id": unit["block_id"], "kind": unit["kind"], "state": "held",
                           "denied_hashes": [], "approved_hashes": [], "approved_page_hashes": [], "decision_ids": []}
        known_bad = previous is not None and unit["content_sha256"] in previous["denied_hashes"]
        row = previous if known_bad else {**row, "state": "held", "denied_hashes": sorted(set(row["denied_hashes"]) | {unit["content_sha256"]}),
               "approved_hashes": [h for h in row["approved_hashes"] if h != unit["content_sha256"]],
               "approved_page_hashes": [], "decision_ids": sorted(set(row["decision_ids"]) | {identity})}
        journal = directory / "decisions"
        journal.mkdir(mode=0o700, exist_ok=True)
        artifact = journal / f"{identity}.json"
        if artifact.exists():
            if artifact.is_symlink():
                raise ContainmentError("immutable hold was redirected")
            previous_finding = verify(DECISION_PURPOSE,json.loads(artifact.read_text()),key.public_key())
            if previous_finding.get("kind")!="hold" or previous_finding.get("unit")!=unit or previous_finding.get("finding")!=finding:
                raise ContainmentError("immutable hold identity conflicts")
        else:
            _immutable(artifact, sign(DECISION_PURPOSE,
                       {"schema_version": PROTOCOL, "kind": "hold", "unit": unit, "finding": finding,
                        "recorded_at": _now(rt)}, key))
        if row != previous:
            decisions[unit["unit_id"]] = row
            state = {**state, "generation": state["generation"] + 1,
                     "decisions": [decisions[k] for k in sorted(decisions)]}
            _save(directory, state, key)
        policy = _refresh(rt, directory, state, key)
    return {"id": identity, "status": "held", "unit_id": unit["unit_id"],
            "generation": policy["generation"], "policy_digest": policy["policy_digest"]}


def accept_consumer_ack(rt, ack, *, authenticated_consumer):
    """Owner SSH transport calls this after installing and fencing a consumer.

    The identity argument comes from the authenticated transport binding, not
    from the JSON being acknowledged. Unregistered/raw inbox JSON is inert.
    """
    directory = _directory(rt)
    if directory is None or authenticated_consumer not in _consumers(rt):
        raise ContainmentError("ACK transport is not bound to an enrolled consumer")
    if not isinstance(ack, dict) or ack.get("consumer_id") != authenticated_consumer or ack.get("protocol") != PROTOCOL \
            or ack.get("ready") is not True or not isinstance(ack.get("release_id"), str) \
            or ack.get("runtime_ready") is not True \
            or not re.fullmatch(r"[0-9a-f]{64}", ack.get("runtime_config_sha256", "")) \
            or not isinstance(ack.get("runtime_checked_at"), (int,float)) \
            or not 0 <= _now(rt)-ack["runtime_checked_at"] < MAX_RUNTIME_AGE:
        raise ContainmentError("consumer ACK is not protocol-ready")
    key = _key(rt)
    with state_lock(directory):
        state = _read(directory, key)
        if ack.get("generation") != state["generation"] or ack.get("policy_digest") != policy_identity(state) \
                or not isinstance(ack.get("checked_at"), (int, float)) \
                or not 0 <= _now(rt) - ack["checked_at"] < MAX_AGE:
            raise ContainmentError("consumer ACK does not identify the current fresh policy")
        acknowledgements = directory / "acks"
        acknowledgements.mkdir(mode=0o700, exist_ok=True)
        atomic_json(acknowledgements / f"{authenticated_consumer}.json", ack)
    return ack


def collect_consumer_updates(rt, store=None, *, limit=100):
    directory = _directory(rt)
    if directory is None:
        return {"accepted":0,"rejected":0,"usage":0}
    # Create/validate the authority before a child mkdir can inherit the umask.
    # Release this lock before ACK import, which may lock the authority again.
    with state_lock(directory):
        pass
    with state_lock(directory/"consumer-import"):
        _assert_runtime_lease(rt)
        return _collect_consumer_updates(rt,store=store,limit=limit)


def _collect_consumer_updates(rt, store=None, *, limit=100):
    """Import only publisher-notarized owner SSH metadata, never raw JSON ACKs.

    Usage IDs include enrolled consumer identity and the provider's issuance
    identity. Retries preserve exactly the same store rows. Unregistered
    consumers cannot influence maintenance selection or restoration readiness.
    """
    from .containment_transport import TRANSPORT_PURPOSE
    directory = _directory(rt)
    if directory is None:
        return {"accepted": 0, "rejected": 0, "usage": 0}
    public = getattr(rt, "publisher_public_key", None)
    counts = {"accepted": 0, "rejected": 0, "usage": 0}
    if store is not None and "native" in _consumers(rt):
        from ..knowledge_service.containment import configuration, knowledge_usage_export, knowledge_usage_export_ack
        try:
            cfg = configuration()
            if cfg["enabled"] and cfg["consumer_id"] == "native":
                owner = getattr(rt,"lease_owner",None)
                if not owner or rt.ledger.live_lease()!=owner:
                    raise ContainmentError("local usage import requires the active scheduler lease")
                usage = knowledge_usage_export(config=cfg)
                for event in usage["events"]:
                    store.record_usage(digest(["native",event["id"]]),event["unit_id"],kind=event["kind"],
                                       repo=event["repo"],count=1,detail={**event["detail"],"consumer_id":"native"})
                    counts["usage"] += 1
                knowledge_usage_export_ack(usage["receipts"],config=cfg)
        except (ContainmentError,OSError):
            counts["native_unavailable"] = 1
    folder = Path(rt.state_dir) / "inbox" / "containment"
    for path in sorted(folder.glob("*.json"))[:limit]:
        try:
            if public is None or path.is_symlink():
                raise ContainmentError("consumer metadata requires a pinned publisher key")
            envelope = json.loads(path.read_text())
            if digest(envelope) != path.stem:
                raise ContainmentError("consumer metadata envelope identity changed")
            payload = verify(TRANSPORT_PURPOSE, envelope, public)
            consumer = payload["consumer_id"]
            if payload.get("schema_version") != PROTOCOL or consumer not in _consumers(rt) \
                    or not isinstance(payload.get("received_at"), (int, float)) \
                    or not 0 <= _now(rt) - payload["received_at"] < MAX_AGE:
                raise ContainmentError("consumer metadata is unregistered or stale")
            usage = payload["usage"]
            if usage.get("consumer_id") != consumer or not isinstance(usage.get("receipts"), list) \
                    or len(usage["receipts"]) > 100 or not isinstance(usage.get("events"), list) \
                    or len(usage["events"]) > 10000:
                raise ContainmentError("consumer usage batch is invalid")
            for event in usage["events"]:
                if not isinstance(event, dict) or event.get("kind") not in {"retrieved", "injected"} \
                        or event.get("count") != 1 or not re.fullmatch(r"[0-9a-f]{64}", event.get("unit_id", "")) \
                        or not re.fullmatch(r"[0-9a-f]{64}", event.get("id", "")) \
                        or event.get("receipt_id") not in usage["receipts"] \
                        or not re.fullmatch(r"[0-9a-f]{64}", event.get("detail",{}).get("context_sha256", "")) \
                        or event["id"] != digest([event["detail"]["context_sha256"], event["unit_id"], event["kind"]]):
                    raise ContainmentError("consumer usage identity/provenance is invalid")
            # An older-generation ACK is harmless and cannot satisfy a barrier;
            # durable usage from that generation still matters for triage.
            try:
                accept_consumer_ack(rt, payload["ack"], authenticated_consumer=consumer)
            except ContainmentError:
                pass
            if usage["events"] and store is None:
                continue  # wait for the lease-owning store; do not drop usage
            for event in usage["events"]:
                store.record_usage(digest([consumer, event["id"]]), event["unit_id"], kind=event["kind"],
                                   repo=event["repo"], count=event["count"],
                                   detail={**event["detail"], "consumer_id": consumer})
                counts["usage"] += 1
            archive = directory / "consumer-receipts"
            archive.mkdir(mode=0o700, exist_ok=True)
            _immutable(archive / path.name, envelope)
            path.unlink()
            counts["accepted"] += 1
        except (ContainmentError, ValueError, OSError, KeyError, TypeError):
            counts["rejected"] += 1
            path.rename(path.with_suffix(".invalid"))
    return counts


def _barrier(rt, directory, state):
    roster, identity, missing = _consumers(rt), policy_identity(state), []
    for consumer in roster:
        path = directory / "acks" / f"{consumer}.json"
        ack = json.loads(path.read_text()) if path.is_file() and not path.is_symlink() else {}
        if ack.get("consumer_id") != consumer or ack.get("generation") != state["generation"] \
                or ack.get("policy_digest") != identity or ack.get("ready") is not True \
                or ack.get("runtime_ready") is not True \
                or not isinstance(ack.get("runtime_checked_at"), (int,float)) \
                or not 0 <= _now(rt)-ack["runtime_checked_at"] < MAX_RUNTIME_AGE \
                or not isinstance(ack.get("checked_at"), (int, float)) \
                or not 0 <= _now(rt) - ack["checked_at"] < MAX_AGE:
            missing.append(consumer)
    return bool(roster) and not missing, missing


def pending_holds(rt):
    directory = _directory(rt)
    if directory is None:
        return {"holds": [], "ready": False, "missing_consumers": []}
    key, roster = _key(rt), _consumers(rt)
    with state_lock(directory):
        state = _read(directory, key)
        ready, missing = _barrier(rt, directory, state)
        corrected = [row["unit_id"] for row in state["decisions"] if row["state"] == "corrected"]
        content_missing = []
        for consumer in roster:
            path = directory / "acks" / f"{consumer}.json"
            ack = json.loads(path.read_text()) if path.is_file() and not path.is_symlink() else {}
            if consumer in missing or any(ack.get("approved_content",{}).get(unit_id) is not True for unit_id in corrected):
                content_missing.append(consumer)
        return {"holds": state["decisions"], "generation": state["generation"], "policy_digest": policy_identity(state),
                "ready": ready, "missing_consumers": missing, "content_current": bool(roster) and not content_missing,
                "missing_content_consumers": content_missing}



def record_restoration(rt, unit_id, *, approved_hashes=(), approved_page_hashes=(), proof):
    """Admit exact corrected bytes; original bad hashes remain denied forever.

    The maintenance correction engine supplies the already completed original
    source, cross-family, normal publication, activation and consumer proof.
    """
    if not isinstance(proof, dict):
        raise ContainmentError("restoration requires bound correction proof")
    view, saved = _verify_restoration(rt, unit_id, proof)
    hashes, pages = list(approved_hashes), list(approved_page_hashes)
    if not hashes and not pages or any(not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes + pages):
        raise ContainmentError("restoration requires exact approved content hashes")
    from .maintenance_units import page_units
    eligible = {u["content_sha256"] for page,text in saved["files"].items()
                for u in page_units(page,text,proof["original_unit"]["repo"],view.public_snapshot)}
    eligible_pages = {text_hash(text) for text in saved["files"].values()}
    if not set(hashes).issubset(eligible) or not set(pages).issubset(eligible_pages):
        raise ContainmentError("restoration approved hashes are not present in exact active gated bytes")
    directory, key = _directory(rt), _key(rt)
    with state_lock(directory):
        state = _read(directory, key)
        decisions = {row["unit_id"]: row for row in state["decisions"]}
        row = decisions[unit_id]
        original = proof["original_unit"]
        if original.get("content_sha256") not in row["denied_hashes"] or any(original.get(k)!=row[k] for k in ("repo","page","block_id","kind")):
            raise ContainmentError("restoration original unit does not match a signed held identity")
        witnessed, already = False, False
        for path in (directory/"decisions").glob("*.json"):
            if path.is_symlink():
                raise ContainmentError("immutable containment decision was redirected")
            packet = verify(DECISION_PURPOSE,json.loads(path.read_text()),key.public_key())
            if packet.get("kind")=="restoration" and packet.get("unit_id")==unit_id \
                    and packet.get("proof",{}).get("changeset_id")==proof["changeset_id"] \
                    and packet.get("proof",{}).get("correction_merge_sha")==proof["correction_merge_sha"]:
                already = True
            if packet.get("kind")=="hold" and packet.get("unit")==original and packet.get("finding")==proof["source_audit"]:
                witnessed = True
        if not witnessed:
            raise ContainmentError("restoration has no matching immutable independent audit decision")
        if set(hashes) & set(row["denied_hashes"]):
            raise ContainmentError("restoration cannot re-admit an original bad hash")
        if already and row["state"]=="corrected" and set(hashes).issubset(row["approved_hashes"]) and set(pages).issubset(row["approved_page_hashes"]):
            return _refresh(rt,directory,state,key)
        if not _barrier(rt,directory,state)[0]:
            raise ContainmentError("current consumer generation is not acknowledged")
        identity = digest({"unit_id": unit_id, "hashes": hashes, "pages": pages, "proof": proof})
        journal = directory / "decisions"
        journal.mkdir(mode=0o700, exist_ok=True)
        _immutable(journal / f"{identity}.json", sign(DECISION_PURPOSE,
                   {"schema_version": PROTOCOL, "kind": "restoration", "unit_id": unit_id,
                    "approved_hashes": hashes, "approved_page_hashes": pages, "proof": proof}, key))
        decisions[unit_id] = {**row, "state": "corrected",
                             "approved_hashes": sorted(set(row["approved_hashes"]) | set(hashes)),
                             "approved_page_hashes": sorted(set(row["approved_page_hashes"]) | set(pages)),
                             "decision_ids": sorted(set(row["decision_ids"]) | {identity})}
        state = {**state, "generation": state["generation"] + 1, "decisions": [decisions[k] for k in sorted(decisions)]}
        _save(directory, state, key)
        return _refresh(rt, directory, state, key)


def _verify_restoration(rt, unit_id, proof):
    from ..knowledge_view import _load_view
    from .maintenance_audit import source_evidence
    from .maintenance_units import page_units
    changeset = rt.ledger.changeset(proof.get("changeset_id", ""))
    detail = changeset["detail"]
    original = detail.get("original_unit", {})
    if changeset.get("kind") != "correction" or changeset.get("status") != "merged" \
            or detail.get("post_check") != "passed" or detail.get("decision", {}).get("status") != "pass" \
            or original.get("unit_id") != unit_id or proof.get("original_unit") != original \
            or proof.get("maintenance_run") != detail.get("maintenance_run") \
            or proof.get("source_audit") != detail.get("source_audit") \
            or proof.get("decision") != detail.get("decision") \
            or not re.fullmatch(r"[0-9a-f]{40}", changeset.get("merge_sha", "")) \
            or proof.get("correction_merge_sha") != changeset["merge_sha"]:
        raise ContainmentError("correction proof does not bind a gated merged ledger changeset")
    audit = detail["source_audit"]
    lifecycle = rt.registry[changeset["repo"]]
    from .maintenance_policy import required_ci
    if proof.get("ci") != required_ci(rt, changeset):
        raise ContainmentError("correction exact-head CI proof changed")
    observed = source_evidence(rt, lifecycle, original)
    if audit.get("outcome") != "contradicted" or audit.get("original_source_checked") is not True \
            or observed != audit.get("evidence") or observed != proof.get("source_evidence"):
        raise ContainmentError("original-source audit no longer matches exact pinned witnesses")
    # The publisher's immutable, signature-verified ACK is the CI/local gate
    # authority; caller flags and a bare ledger post_check are insufficient.
    publisher = getattr(rt, "publisher_public_key", None)
    receipt = None
    for path in (Path(rt.state_dir) / "receipts" / "publisher").glob("*.json"):
        if path.is_symlink() or publisher is None:
            continue
        ack = verify("kb-ack", json.loads(path.read_text()), publisher)
        if ack.get("kind") == "merge" and ack.get("ok") is True \
                and ack.get("changeset_id") == changeset["id"] \
                and ack.get("merge_sha") == changeset["merge_sha"] \
                and ack.get("head_sha") == changeset["head_sha"] and ack.get("post_check") == "passed":
            receipt = ack
            break
    if receipt is None:
        raise ContainmentError("exact signed publisher merge/CI receipt is not available")
    active = rt.ledger.active_snapshot()
    if not active or proof.get("active_snapshot") != active:
        raise ContainmentError("correction active snapshot changed")
    view = _load_view(str(Path(rt.state_dir) / "active"))
    if not view.verified or view.public_snapshot != active:
        raise ContainmentError("active correction bytes are not a verified admitted snapshot")
    saved = rt.load_changeset_files(changeset["id"])
    if saved.get("evidence") != observed:
        raise ContainmentError("correction gate used different source witnesses")
    actual_pages = {}
    for page, text in saved["files"].items():
        # Read verified manifest bytes without a serving hold preventing the
        # authority from checking a proposed restoration.
        if view.files.get(page) != text_hash(text) or (view.root / page).read_text() != text:
            raise ContainmentError("active correction differs from gated proposed bytes")
        actual_pages[page] = text_hash(text)
    if proof.get("page_hashes") != actual_pages:
        raise ContainmentError("restoration page hash proof differs")
    return view, saved


def merge_authorization(rt, changeset):
    """Short-lived signed permission for one correction at one policy generation."""
    directory, key = _directory(rt), _key(rt)
    if directory is None or changeset.get("kind") != "correction":
        raise ContainmentError("maintenance merge authorization requires a correction")
    with state_lock(directory):
        state = _read(directory, key)
        if not _barrier(rt, directory, state)[0]:
            raise ContainmentError("maintenance merge requires all current consumer ACKs")
        now = _now(rt)
        return sign("kb-maintenance-merge", {
            "schema_version": PROTOCOL, "changeset_id": changeset["id"], "head_sha": changeset["head_sha"],
            "policy_sha256": changeset["detail"]["maintenance_policy"],
            "generation": state["generation"], "policy_digest": policy_identity(state),
            "acks": [json.loads((directory / "acks" / f"{c}.json").read_text()) for c in _consumers(rt)],
            "issued_at": now, "expires_at": now + 60,
        }, key)


def native_ack(rt):
    """A running lease owner proves its own actual SDK configuration/readiness."""
    from ..knowledge_service.containment import configuration, knowledge_policy_install
    owner = getattr(rt,"lease_owner",None)
    cfg = configuration()
    if not _enabled(rt) or not cfg["enabled"] or cfg["consumer_id"] != "native" or "native" not in _consumers(rt):
        return {"ready":False,"reason":"native_not_enrolled"}
    if not owner or rt.ledger.live_lease()!=owner:
        raise ContainmentError("native readiness requires the actual running scheduler lease")
    envelope = json.loads((Path(rt.state_dir)/"outbox/containment-policy.json").read_text())
    release = os.environ.get("KB_CONTAINMENT_RELEASE_FILE","")
    if release:
        path = Path(release)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ContainmentError("native release identity must be an actual regular manifest")
        import hashlib
        identity = hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        from .. import __version__
        from ..knowledge_service import containment as consumer_module
        identity = digest({"provider_version":__version__,"protocol":PROTOCOL,
                           "consumer_code_sha256":text_hash(Path(consumer_module.__file__).read_text()),
                           "authority_code_sha256":text_hash(Path(__file__).read_text())})
    # Do not hold the ledger transaction while waiting for outward writes to drain.
    ack = knowledge_policy_install(envelope,config=cfg,release_id="sha256:"+identity)
    if rt.ledger.live_lease()!=owner:
        raise ContainmentError("native scheduler lost its lease while draining publication")
    ack.update(runtime_ready=True,runtime_checked_at=_now(rt),runtime_config_sha256=digest(cfg))
    with state_lock(cfg["state_dir"]) as state:
        atomic_json(state/"heartbeat.json",{**ack,"protocol_version":PROTOCOL,"config_sha256":digest(cfg)})
    return accept_consumer_ack(rt,ack,authenticated_consumer="native")
