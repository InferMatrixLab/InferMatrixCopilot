"""Opt-in, external containment for immutable knowledge and issued contexts.

The authority signs policy, never consumer usage. Usage is issued into a
private, durable consumer registry; a caller cannot fabricate a publication
receipt by hashing its own JSON. Neither registry lives in a knowledge tree.
"""
from __future__ import annotations

from ..persistence import atomic_write_bytes

import hashlib
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path, PurePosixPath

PROTOCOL = 1
POLICY_PURPOSE = "kb-containment-policy"
MAX_AGE = 600
MAX_RUNTIME_AGE = 180
_CONFIG = ContextVar("knowledge_containment_config", default=None)
_HASH = re.compile(r"[0-9a-f]{64}")
_CONSUMER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")


class ContainmentError(RuntimeError):
    """Knowledge cannot safely be used under the current containment policy."""


class HeldKnowledgeError(ContainmentError):
    """A valid policy contains held evidence in this specific document."""


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _boolean(value):
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.lower() in {"true", "false", "1", "0"}:
        return value.lower() in {"true", "1"}
    raise ContainmentError("containment enabled flag is invalid")


def configuration(config=None):
    if config is None:
        config = _CONFIG.get()
    if config is None and os.environ.get("KB_CONTAINMENT_CONFIG"):
        config = json.loads(os.environ["KB_CONTAINMENT_CONFIG"])
    if config is None:
        directory = os.environ.get("KB_CONTAINMENT_POLICY_DIR", "")
        config = {"enabled": _boolean(os.environ.get("KB_CONTAINMENT_ENFORCE", "false")),
                  "policy_path": str(Path(directory) / "policy.json") if directory else "",
                  "public_key_path": os.environ.get("KB_CONTAINMENT_PUBLIC_KEY", ""),
                  "consumer_id": os.environ.get("KB_CONTAINMENT_CONSUMER_ID", "native"),
                  "state_dir": os.environ.get("KB_CONTAINMENT_STATE_DIR", "")}
    if not isinstance(config, dict):
        raise ContainmentError("containment configuration must be an object")
    enabled = _boolean(config.get("enabled", False))
    if not enabled:
        # Enrollment survives missing environment flags and release rollback.
        consumer = config.get("consumer_id") or os.environ.get("KB_CONTAINMENT_CONSUMER_ID", "native")
        durable = config.get("state_dir") or os.environ.get("KB_CONTAINMENT_STATE_DIR") or str(Path.home()/".infermatrix-copilot/containment"/consumer)
        if (Path(durable)/"highwater.json").exists() or (Path(durable)/"required.json").exists():
            raise ContainmentError("an enrolled consumer cannot disable containment by dropping configuration")
        return {"enabled": False}
    consumer = config.get("consumer_id", "")
    if not isinstance(consumer, str) or not _CONSUMER.fullmatch(consumer):
        raise ContainmentError("containment requires an explicit consumer identity")
    path = config.get("policy_path", "")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ContainmentError("containment requires an absolute signed policy path")
    state = config.get("state_dir") or str(Path.home() / ".infermatrix-copilot/containment" / consumer)
    if not isinstance(state, str) or not Path(state).is_absolute():
        raise ContainmentError("containment requires an absolute durable state directory")
    keys = config.get("public_keys")
    if keys is None and config.get("public_keys_file"):
        keys = json.loads(_regular(Path(config["public_keys_file"])).read_text())
    if keys is None and config.get("public_key_path"):
        keys = [_regular(Path(config["public_key_path"])).read_text()]
    if not isinstance(keys, list) or not keys or any(not isinstance(k, str) for k in keys):
        raise ContainmentError("containment requires pinned public keys")
    return {"enabled": True, "consumer_id": consumer, "policy_path": path,
            "state_dir": state, "public_keys": keys}


@contextmanager
def configured(config=None):
    token = _CONFIG.set(config) if config is not None else None
    try:
        yield
    finally:
        if token is not None:
            _CONFIG.reset(token)


def with_containment(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with configured(getattr(self, "_knowledge_maintenance", None)):
            return method(self, *args, **kwargs)
    return wrapped


def _regular(path):
    if path.is_symlink() or not path.is_file():
        raise ContainmentError("containment artifact must be a regular file")
    return path


def atomic_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ContainmentError("containment artifacts cannot be symlinks")
    atomic_write_bytes(path, canonical_json(value), mode=0o600)


@contextmanager
def state_lock(directory):
    # State is deliberately outside release/snapshot directories. Files are
    # private to the authenticated operator, not supplied by a model result.
    import fcntl
    directory = Path(directory)
    if directory.is_symlink():
        raise ContainmentError("containment state cannot be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o022:
        raise ContainmentError("containment state must be owner-controlled")
    lock = directory / ".lock"
    if lock.is_symlink():
        raise ContainmentError("containment lock cannot be redirected")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def policy_identity(payload):
    return digest({key: payload[key] for key in ("schema_version", "generation", "consumers", "decisions")})


def _time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContainmentError("containment timestamp is invalid")
    return value


def _page(value):
    if not isinstance(value, str):
        raise ContainmentError("containment page is invalid")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ContainmentError("containment page escapes knowledge")
    return value


def load_policy(config=None, *, now=None):
    cfg = configuration(config)
    if not cfg["enabled"]:
        return cfg, None
    from .signing import load_public_key, verify
    now = time.time() if now is None else now
    envelope = json.loads(_regular(Path(cfg["policy_path"])).read_text())
    payload = None
    for key in cfg["public_keys"]:
        try:
            payload = verify(POLICY_PURPOSE, envelope, load_public_key(key))
            break
        except ValueError:
            continue
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "generation", "issued_at", "expires_at", "consumers", "decisions"}:
        raise ContainmentError("containment policy signature/schema is invalid")
    generation = payload["generation"]
    if payload["schema_version"] != PROTOCOL or type(generation) is not int or generation < 1:
        raise ContainmentError("containment protocol/generation is invalid")
    issued, expires = _time(payload["issued_at"]), _time(payload["expires_at"])
    if not issued <= now < expires or not 0 < expires - issued <= MAX_AGE or now - issued > MAX_AGE:
        raise ContainmentError("containment policy is stale or not yet valid")
    consumers, decisions = payload["consumers"], payload["decisions"]
    if not isinstance(consumers, list) or len(set(consumers)) != len(consumers) \
            or any(not isinstance(c, str) or not _CONSUMER.fullmatch(c) for c in consumers) \
            or cfg["consumer_id"] not in consumers:
        raise ContainmentError("containment consumer is not enrolled")
    if not isinstance(decisions, list):
        raise ContainmentError("containment decisions are invalid")
    seen = set()
    for row in decisions:
        if not isinstance(row, dict) or not isinstance(row.get("unit_id"), str) or not _HASH.fullmatch(row["unit_id"]) \
                or row["unit_id"] in seen or row.get("state") not in {"held", "corrected"}:
            raise ContainmentError("containment decision identity/state is invalid")
        seen.add(row["unit_id"])
        _page(row.get("page"))
        if not isinstance(row.get("repo"), str) or not isinstance(row.get("block_id"), str):
            raise ContainmentError("containment decision scope is invalid")
        for field in ("denied_hashes", "approved_hashes", "approved_page_hashes"):
            if not isinstance(row.get(field), list) or any(not isinstance(h, str) or not _HASH.fullmatch(h) for h in row[field]):
                raise ContainmentError("containment decision hashes are invalid")
        if not row["denied_hashes"]:
            raise ContainmentError("containment hold needs original denied hashes")
    identity = policy_identity(payload)
    with state_lock(cfg["state_dir"]) as state:
        highwater = state / "highwater.json"
        if highwater.exists():
            old = json.loads(_regular(highwater).read_text())
            if generation < old["generation"] or (generation == old["generation"] and (
                    identity != old["policy_digest"] or issued < old["issued_at"])):
                raise ContainmentError("containment policy replay or generation conflict")
        denied = {row["unit_id"]: sorted(set(row["denied_hashes"])) for row in decisions}
        if highwater.exists() and any(not set(hashes).issubset(denied.get(unit, []))
                                     for unit, hashes in old.get("denied", {}).items()):
            raise ContainmentError("containment policy attempted to forget original bad hashes")
        current = {"generation": generation, "policy_digest": identity, "issued_at": issued, "denied": denied}
        if not highwater.exists() or old != current:
            atomic_json(highwater, current)
    return cfg, {**payload, "policy_digest": identity}


def knowledge_maintenance_status(*, config=None):
    try:
        cfg, policy = load_policy(config)
        if policy is None:
            return {"protocol": PROTOCOL, "enabled": False, "ready": True, "generation": 0,
                    "policy_digest": "", "issued_at": None, "expires_at": None, "reason": "disabled"}
        return {"protocol": PROTOCOL, "enabled": True, "ready": True,
                **{k: policy[k] for k in ("generation", "policy_digest", "issued_at", "expires_at")}, "reason": "ready"}
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        return {"protocol": PROTOCOL, "enabled": True, "ready": False, "generation": None,
                "policy_digest": "", "issued_at": None, "expires_at": None, "reason": type(exc).__name__}


def page_admissibility(view, relative, *, config=None, policy=None):
    cfg, loaded = load_policy(config) if policy is None else (configuration(config), policy)
    if not cfg["enabled"]:
        return []
    rows = [row for row in loaded["decisions"] if row["page"] == relative]
    if not rows:
        return []
    path = view.root / relative
    raw = path.read_text(encoding="utf-8")
    page_hash = text_hash(raw)
    from ..kb_service.maintenance_units import page_units
    units = {row["unit_id"]: row for row in page_units(relative, raw, rows[0]["repo"], view.public_snapshot)}
    affected = []
    for row in rows:
        unit = units.get(row["unit_id"])
        actual = unit["content_sha256"] if unit else None
        # An exact old bad hash stays denied even after restoration. Unknown
        # edits/removals in this lineage require an approved correction proof.
        if actual in row["denied_hashes"] or (actual not in row["approved_hashes"]
                and page_hash not in row["approved_page_hashes"]):
            affected.append({"unit_id": row["unit_id"], "page": relative, "block_id": row["block_id"],
                             "content_sha256": actual, "reason": "held_knowledge"})
    return affected


def assert_page_available(view, relative, *, config=None):
    affected = page_admissibility(view, relative, config=config)
    if affected:
        raise HeldKnowledgeError("knowledge page contains held or unverified corrected evidence")


def _units(view, paths):
    from ..kb_service.maintenance_units import page_units
    rows = []
    for relative in sorted(set(paths)):
        _page(relative)
        path = view.path(relative)
        text = path.read_text(encoding="utf-8")
        parts = PurePosixPath(relative).parts
        repo = parts[1] if len(parts) > 2 and parts[0] == "repos" else "general"
        units = page_units(relative, text, repo, view.public_snapshot) if path.suffix == ".md" else []
        rows.extend({**{k: row[k] for k in ("unit_id", "repo", "page", "block_id", "kind", "content_sha256")},
                     "_text":row["text"]} for row in units)
    return rows


def _context_paths(context):
    paths = set()
    def walk(value):
        if isinstance(value, dict):
            document = value.get("document_id")
            if isinstance(document, str):
                paths.add(document)
            # Adaptive packets carry knowledge-relative page references.
            if isinstance(value.get("path"), str) and value["path"].startswith(("repos/", "general/")):
                paths.add(value["path"])
            for child in value.values():
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)
    walk(context)
    return paths


def _verify_context_documents(context, view):
    """Check issued resource hashes and excerpts against the pinned real files."""
    from .lifecycle import visible_text
    def walk(value):
        if isinstance(value,dict):
            document = value.get("document_id")
            if isinstance(document,str):
                path = view.path(document)
                data = path.read_bytes()
                if path.suffix==".md" and b"kb:rule" in data:
                    data = visible_text(data.decode("utf-8")).encode("utf-8")
                if "sha256" in value and value["sha256"]!="sha256:"+hashlib.sha256(data).hexdigest():
                    raise ContainmentError("issued document digest does not match actual provider bytes")
                if "excerpt" in value and value["excerpt"]!=data[:65536].decode("utf-8",errors="replace"):
                    raise ContainmentError("issued document excerpt does not match actual provider bytes")
                if "content" in value:
                    offset = value.get("offset",0)
                    if type(offset) is not int or offset<0:
                        raise ContainmentError("issued document offset is invalid")
                    end = value.get("next_offset") or len(data)
                    if value["content"]!=data[offset:end].decode("utf-8",errors="replace"):
                        raise ContainmentError("issued document window does not match actual provider bytes")
            if "model_content" in value and value.get("model_content_sha256")!=text_hash(value["model_content"]):
                raise ContainmentError("issued adaptive content digest is invalid")
            for child in value.values():
                walk(child)
        elif isinstance(value,(list,tuple)):
            for child in value:
                walk(child)
    walk(context)


def _injected_units(context, units):
    """Attribute only bytes present in the issued injectable fragments.

    Page dependencies remain conservative, while a truncated excerpt never
    claims that its omitted sibling units were injected. Partial delivery is
    explicit and still counts as actual use of that source unit.
    """
    from .lifecycle import visible_text
    fragments, global_fragments = {}, []
    def normalize(text):
        return " ".join(re.sub(r"<!--.*?-->","",text,flags=re.S).split())
    def walk(value):
        if isinstance(value,dict):
            document = value.get("document_id")
            if not isinstance(document,str) and isinstance(value.get("path"),str) and value["path"].startswith(("repos/","general/")):
                document = value["path"]
            if isinstance(document,str):
                for field in ("excerpt","content","text"):
                    if isinstance(value.get(field),str):
                        fragments.setdefault(document,[]).append(normalize(value[field]))
            if isinstance(value.get("model_content"),str):
                rendered = value["model_content"]
                global_fragments.append(normalize(rendered))
                # Adaptive delivery serializes source text inside JSON fences.
                # Decode those actual rendered fragments before attribution.
                for block in re.findall(r"<untrusted_data>\s*(.*?)\s*</untrusted_data>", rendered, re.S):
                    try:
                        fragment = json.loads(block)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(fragment,dict) and isinstance(fragment.get("path"),str) and isinstance(fragment.get("content"),str):
                        fragments.setdefault(fragment["path"],[]).append(normalize(fragment["content"]))
            for child in value.values():
                walk(child)
        elif isinstance(value,(list,tuple)):
            for child in value:
                walk(child)
    walk(context)
    result = []
    for unit in units:
        text = visible_text(unit["_text"])
        body = normalize(text)
        delivered = fragments.get(unit["page"],[])+global_fragments
        complete = bool(body) and any(body in part for part in delivered)
        # A meaningful source line establishes partial delivery without
        # attributing a common heading/navigation word to every page unit.
        partial = not complete and any(len(line)>=32 and any(line in part for part in delivered)
            for line in (normalize(row) for row in text.splitlines()))
        if complete or partial:
            result.append({**{k:v for k,v in unit.items() if k!="_text"},"partial":partial})
    return result


def _issue_usage(context, *, injected=False, mode="direct", consumer=None, config=None, view=None, session_id=None):
    cfg, policy = load_policy(config)
    if not cfg["enabled"]:
        return {"protocol": PROTOCOL, "enabled": False, "generation": 0, "retrieved_units": [], "injected_units": []}
    if not isinstance(context, dict) or type(injected) is not bool or mode not in {"direct", "strict"}:
        raise ContainmentError("knowledge usage context/mode is invalid")
    if session_id is not None and (not isinstance(session_id, str) or not _HASH.fullmatch(session_id)):
        raise ContainmentError("usage requires the provider's issued delivery session identity")
    if consumer and (not isinstance(consumer, dict) or consumer.get("consumer_id", cfg["consumer_id"]) != cfg["consumer_id"]):
        raise ContainmentError("knowledge usage identifies another consumer")
    if view is None:
        from ..knowledge_view import KnowledgeView
        view = KnowledgeView.current()
    with configured(cfg):
        paths = _context_paths(context)
        scope = context.get("scope", "documents")
        if scope == "snapshot":
            repository = context.get("repository", "")
            if not isinstance(repository, str) or not repository:
                raise ContainmentError("snapshot usage requires a repository scope")
            paths = {p.relative_to(view.root).as_posix() for p in view.root.rglob("*.md")
                     if p.relative_to(view.root).as_posix().startswith(("general/", f"repos/{repository}/"))}
        elif not paths:
            raise ContainmentError("knowledge usage has no provider documents")
        _verify_context_documents(context,view)
        source_units = _units(view, paths)
        units = [{k:v for k,v in unit.items() if k!="_text"} for unit in source_units]
    supplied_snapshot = context.get("knowledge_snapshot", context.get("snapshot", view.public_snapshot))
    if supplied_snapshot != view.public_snapshot or context.get("knowledge_tree_sha256", view.tree_sha256) != view.tree_sha256:
        raise ContainmentError("usage context does not bind the provider snapshot")
    body = {"protocol": PROTOCOL, "enabled": True, "consumer_id": cfg["consumer_id"],
            "mode": mode, "generation": policy["generation"], "policy_digest": policy["policy_digest"],
            "snapshot": view.public_snapshot, "tree_sha256": view.tree_sha256,
            "context_sha256": digest(context), "scope": scope,
            "retrieved_units": units if scope == "documents" else [],
            "injected_units": _injected_units(context,source_units) if injected and scope == "documents" else [],
            "scope_units": units,
            "pages": [{"path": p, "sha256": hashlib.sha256((view.root / p).read_bytes()).hexdigest()} for p in sorted(paths)],
            **({"delivery_session": session_id} if session_id else {})}
    receipt = {**body, "receipt_id": digest(body)}
    # Keep private physical provenance out of the public receipt/prompt.
    stored = {"receipt": receipt, "knowledge_root": str(view.root.resolve()),
              "snapshot_root": str(view.root.parent) if view.verified else "", "snapshot": view.public_snapshot, "context": json.loads(canonical_json(context)),
              **({"delivery_session": session_id} if session_id else {})}
    with state_lock(cfg["state_dir"]) as state:
        contexts = state / "contexts"
        contexts.mkdir(mode=0o700, exist_ok=True)
        provenance = {k: stored[k] for k in ("knowledge_root", "snapshot_root", "snapshot", "context")}
        if session_id:
            provenance["delivery_session"] = session_id
        context_path = contexts / f"{body['context_sha256']}.json"
        if context_path.exists() and json.loads(_regular(context_path).read_text()) != provenance:
            raise ContainmentError("knowledge context issuance conflicts")
        atomic_json(context_path, provenance)
        directory = state / "usage"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / f"{receipt['receipt_id']}.json"
        if path.exists() and json.loads(_regular(path).read_text()) != stored:
            raise ContainmentError("knowledge usage issuance conflicts")
        atomic_json(path, stored)
        if session_id:
            # A flat, bounded private journal binds all actual deliveries to
            # earlier publication receipts, including after SDK restart.
            sessions = state / "delivery-sessions"
            sessions.mkdir(mode=0o700, exist_ok=True)
            session_path = sessions / f"{session_id}.json"
            binding = {"session_id": session_id, "knowledge_root": stored["knowledge_root"],
                       "snapshot_root": stored["snapshot_root"], "snapshot": stored["snapshot"],
                       "tree_sha256": body["tree_sha256"]}
            journal = json.loads(_regular(session_path).read_text()) if session_path.exists() else {**binding, "receipts": []}
            if any(journal.get(k) != v for k, v in binding.items()) or not isinstance(journal.get("receipts"), list):
                raise ContainmentError("delivery session issuance conflicts")
            identities = journal["receipts"]
            if len(identities) >= 4096 and receipt["receipt_id"] not in identities:
                raise ContainmentError("delivery session receipt limit exceeded")
            if receipt["receipt_id"] not in identities:
                identities.append(receipt["receipt_id"])
            atomic_json(session_path, journal)
    return receipt


def _issued_view(stored):
    from ..knowledge_view import KnowledgeView, _load_view
    return (_load_view(stored["snapshot_root"]) if stored["snapshot_root"] else
            KnowledgeView(Path(stored["knowledge_root"]), stored.get("snapshot", "unverified")))


def knowledge_usage_record(context, *, injected=False, mode="direct", consumer=None, config=None):
    """Record only an exact context previously issued by provider retrieval.

    A digest supplied by the caller cannot create provenance. Issuance roots and
    the original context are private, and current policy is checked again.
    """
    cfg, policy = load_policy(config)
    if not cfg["enabled"]:
        return {"protocol": PROTOCOL, "enabled": False, "generation": 0,
                "retrieved_units": [], "injected_units": []}
    if not isinstance(context, dict):
        raise ContainmentError("knowledge context must be a provider-issued object")
    with state_lock(cfg["state_dir"]) as state:
        stored = json.loads(_regular(state / "contexts" / f"{digest(context)}.json").read_text())
    if canonical_json(stored["context"]) != canonical_json(context):
        raise ContainmentError("knowledge context differs from provider issuance")
    return _issue_usage(context, injected=injected, mode=mode, consumer=consumer,
                        config=cfg, view=_issued_view(stored), session_id=stored.get("delivery_session"))


def knowledge_usage_export(*, config=None, limit=100):
    """Owner-transport-only metadata export; never returns prompts or source text."""
    cfg, policy = load_policy(config)
    if not cfg["enabled"] or type(limit) is not int or not 1 <= limit <= 100:
        raise ContainmentError("usage export requires an enrolled, fresh consumer")
    receipts, events = [], []
    with state_lock(cfg["state_dir"]) as state:
        folder, delivered = state / "usage", state / "usage-delivered"
        for path in sorted(folder.glob("*.json")):
            if (delivered / path.name).exists():
                continue
            record = json.loads(_regular(path).read_text())["receipt"]
            if digest({k:v for k,v in record.items() if k != "receipt_id"}) != record.get("receipt_id") \
                    or record.get("consumer_id") != cfg["consumer_id"]:
                raise ContainmentError("usage export registry is invalid")
            receipts.append(record["receipt_id"])
            # Snapshot scope is a conservative publication dependency, not a
            # claim that every scoped unit was retrieved or injected.
            for kind, field in (("retrieved", "retrieved_units"), ("injected", "injected_units")):
                for unit in record[field]:
                    identity = digest([record["context_sha256"], unit["unit_id"], kind])
                    events.append({"id": identity, "receipt_id": record["receipt_id"], "unit_id": unit["unit_id"], "repo": unit["repo"],
                                   "kind": kind, "count": 1,
                                   "detail": {"context_sha256": record["context_sha256"], "mode": record["mode"],
                                              "snapshot": record["snapshot"], "partial": unit.get("partial",False)}})
            if len(receipts) >= limit:
                break
    return {"consumer_id": cfg["consumer_id"], "receipts": receipts,
            "events": list({event["id"]:event for event in events}.values())}


def knowledge_usage_export_ack(receipt_ids, *, config=None):
    cfg = configuration(config)
    if not cfg["enabled"] or not isinstance(receipt_ids, list) or len(receipt_ids) > 100:
        raise ContainmentError("usage export acknowledgement is invalid")
    with state_lock(cfg["state_dir"]) as state:
        delivered = state / "usage-delivered"
        delivered.mkdir(mode=0o700, exist_ok=True)
        for identity in receipt_ids:
            if not isinstance(identity, str) or not _HASH.fullmatch(identity):
                raise ContainmentError("usage export acknowledgement identity is invalid")
            _regular(state / "usage" / f"{identity}.json")
            atomic_json(delivered / f"{identity}.json", {"receipt_id": identity})
    return {"acknowledged": len(receipt_ids)}


def _session_dependencies(state, stored):
    """Resolve a private flat journal; caller-provided dependency graphs cannot enter."""
    session = stored.get("delivery_session")
    if session is None:
        if "delivery_session" in stored["receipt"]:
            raise ContainmentError("usage has no private delivery session binding")
        return [stored]
    if not isinstance(session,str) or not _HASH.fullmatch(session):
        raise ContainmentError("delivery session identity is invalid")
    journal = json.loads(_regular(state / "delivery-sessions" / f"{session}.json").read_text())
    binding = {"session_id": session, **{k:stored[k] for k in ("knowledge_root","snapshot_root","snapshot")},
               "tree_sha256":stored["receipt"]["tree_sha256"]}
    identities = journal.get("receipts")
    if any(journal.get(k)!=v for k,v in binding.items()) or not isinstance(identities,list) \
            or not 1<=len(identities)<=4096 or any(not isinstance(i,str) or not _HASH.fullmatch(i) for i in identities) \
            or len(set(identities))!=len(identities) or stored["receipt"]["receipt_id"] not in identities:
        raise ContainmentError("delivery session journal is invalid")
    records = []
    for identity in identities:
        record = json.loads(_regular(state / "usage" / f"{identity}.json").read_text())
        receipt = record["receipt"]
        # No nested/cyclic graph traversal: every member must be issued into
        # this exact session and immutable view, with its own valid digest.
        if record.get("delivery_session")!=session or receipt.get("delivery_session")!=session \
                or any(record[k]!=stored[k] for k in ("knowledge_root","snapshot_root","snapshot")) \
                or receipt["tree_sha256"]!=binding["tree_sha256"] or receipt["consumer_id"]!=stored["receipt"]["consumer_id"] \
                or receipt.get("receipt_id")!=identity or digest({k:v for k,v in receipt.items() if k!="receipt_id"})!=identity \
                or digest(record["context"])!=receipt["context_sha256"]:
            raise ContainmentError("delivery session member provenance is invalid")
        records.append(record)
    return records


def knowledge_availability_check(receipt, *, config=None):
    try:
        cfg, policy = load_policy(config)
        if not cfg["enabled"]:
            return {"allowed": True, "enabled": False, "generation": 0, "policy_digest": "", "reason": "disabled", "affected_units": [], "reassessment_required": False}
        if not isinstance(receipt, dict) or receipt.get("enabled") is not True or receipt.get("consumer_id") != cfg["consumer_id"]:
            raise ContainmentError("enforcement requires provider-issued usage provenance")
        identity = receipt.get("receipt_id", "")
        if not isinstance(identity, str) or not _HASH.fullmatch(identity) or digest({k: v for k, v in receipt.items() if k != "receipt_id"}) != identity:
            raise ContainmentError("knowledge usage receipt digest is invalid")
        with state_lock(cfg["state_dir"]) as state:
            stored = json.loads(_regular(state / "usage" / f"{identity}.json").read_text())
            dependencies = _session_dependencies(state, stored)
        if stored["receipt"] != receipt or digest(stored["context"]) != receipt["context_sha256"]:
            raise ContainmentError("knowledge usage was not issued by this provider")
        from ..knowledge_view import KnowledgeView, _load_view
        view = _load_view(stored["snapshot_root"]) if stored["snapshot_root"] else KnowledgeView(Path(stored["knowledge_root"]), receipt["snapshot"])
        if str(view.root.resolve()) != stored["knowledge_root"] or view.tree_sha256 != receipt["tree_sha256"]:
            raise ContainmentError("issued knowledge snapshot changed")
        affected = []
        pages = {}
        for dependency in dependencies:
            for page in dependency["receipt"]["pages"]:
                if page["path"] in pages and pages[page["path"]]!=page["sha256"]:
                    raise ContainmentError("delivery session resources conflict")
                pages[page["path"]] = page["sha256"]
        with configured(cfg):
            for relative, expected in pages.items():
                path = (view.root / _page(relative)).resolve()
                if not path.is_relative_to(view.root.resolve()):
                    raise ContainmentError("issued knowledge path was redirected")
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ContainmentError("issued knowledge bytes changed")
                affected.extend(page_admissibility(view, relative, config=cfg, policy=policy))
        return {"allowed": not affected, "enabled": True, "generation": policy["generation"],
                "policy_digest": policy["policy_digest"], "reason": "held_knowledge" if affected else "available",
                "affected_units": affected, "reassessment_required": bool(affected)}
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        return {"allowed": False, "enabled": True, "generation": None, "policy_digest": "",
                "reason": type(exc).__name__, "affected_units": [], "reassessment_required": True}


def _usage_session_view(receipt, *, config=None):
    """Resolve an existing protected issuance for a raw MCP follow-up."""
    cfg = configuration(config)
    if not cfg["enabled"]:
        return None, None
    if not knowledge_availability_check(receipt, config=cfg)["allowed"]:
        raise ContainmentError("knowledge follow-up requires admissible provider-issued provenance")
    with state_lock(cfg["state_dir"]) as state:
        stored = json.loads(_regular(state / "usage" / f"{receipt['receipt_id']}.json").read_text())
    session = stored.get("delivery_session")
    if not isinstance(session,str) or not _HASH.fullmatch(session):
        raise ContainmentError("knowledge follow-up requires a provider-bound review session")
    return session, _issued_view(stored)


@contextmanager
def _publication_lock(cfg):
    import fcntl
    with state_lock(cfg["state_dir"]) as state:
        path = state / ".publication.lock"
        if path.is_symlink():
            raise ContainmentError("publication lock cannot be redirected")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic()+15
        while True:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic()>=deadline:
                    raise ContainmentError("publication drain exceeded its bounded wait")
                time.sleep(0.02)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def knowledge_publication_guard(*, config=None):
    """Fence the final admissibility check AND outward write against installs."""
    cfg = configuration(config)
    if not cfg["enabled"]:
        yield
        return
    with _publication_lock(cfg), configured(cfg):
        load_policy(cfg)
        yield


def _observed_content(policy):
    """Derive corrected-content readiness from this consumer's current bytes."""
    from ..knowledge_view import KnowledgeView
    from ..kb_service.maintenance_units import page_units
    view = KnowledgeView.current()
    approved = {}
    for row in policy["decisions"]:
        if row["state"] != "corrected":
            continue
        ready = False
        try:
            path = (view.root / row["page"]).resolve()
            if not path.is_relative_to(view.root.resolve()):
                raise ContainmentError("consumer corrected path escapes knowledge")
            raw = path.read_text()
            page_hash = text_hash(raw)
            if view.files is not None and view.files.get(row["page"]) != page_hash:
                raise ContainmentError("consumer corrected bytes differ from manifest")
            units = {u["unit_id"]:u for u in page_units(row["page"],raw,row["repo"],view.public_snapshot)}
            unit = units.get(row["unit_id"],{})
            actual = unit.get("content_sha256")
            ready = actual not in row["denied_hashes"] and (page_hash in row["approved_page_hashes"]
                       or actual in row["approved_hashes"])
        except (ContainmentError,OSError,ValueError,KeyError,TypeError):
            pass
        approved[row["unit_id"]] = ready
    return {"knowledge_snapshot":view.public_snapshot,"knowledge_tree_sha256":view.tree_sha256,
            "approved_content":approved,"content_current":all(approved.values())}


def knowledge_policy_install(envelope, *, config=None, release_id=""):
    """Authenticated owner transport installs a signed policy, then ACKs.

    Waiting for the publication lock drains earlier-generation writes. Policy
    validation/highwater occurs before publication of the candidate bytes.
    """
    cfg = configuration(config)
    if not cfg["enabled"]:
        raise ContainmentError("disabled consumer cannot acknowledge enforcement readiness")
    if not isinstance(release_id, str) or not release_id:
        raise ContainmentError("consumer ACK requires an explicit release identity")
    with _publication_lock(cfg):
        policy_path = Path(cfg["policy_path"])
        policy_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".policy-verify-", dir=policy_path.parent)
        os.close(fd)
        try:
            Path(temporary).write_bytes(canonical_json(envelope))
            checked = {**cfg, "policy_path": temporary}
            _, policy = load_policy(checked)
            atomic_json(policy_path, envelope)
            with state_lock(cfg["state_dir"]) as state:
                atomic_json(state / "required.json", {"protocol": PROTOCOL, "consumer_id": cfg["consumer_id"]})
        finally:
            Path(temporary).unlink(missing_ok=True)
        ack = {"protocol": PROTOCOL, "consumer_id": cfg["consumer_id"], "generation": policy["generation"],
               "policy_digest": policy["policy_digest"], "ready": True,
               "release_id": release_id, "checked_at": time.time(), **_observed_content(policy)}
        with state_lock(cfg["state_dir"]) as state:
            atomic_json(state / "ack.json", ack)
        return ack
