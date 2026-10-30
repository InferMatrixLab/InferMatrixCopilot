"""Snapshot-pinned, cumulative knowledge context shared by review consumers.

The default meter is a conservative UTF-8 byte upper bound, not reported model
usage. Hosts may supply a tokenizer with a stable identifier. Only model_content
is intended for injection; references and diagnostics contain no extra excerpts.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping

from .knowledge_docs import KnowledgeDocs
from .knowledge_view import KnowledgeView


class ContextError(ValueError):
    """Invalid identity, authorization, or context budget."""


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ContextBudget:
    initial_tokens: int = 24000
    max_tokens: int = 64000
    model_context_tokens: int = 131072
    source_reserve_tokens: int = 32000
    output_reserve_tokens: int = 8192
    other_prompt_tokens: int = 0

    def __post_init__(self):
        values = asdict(self)
        if any(type(v) is not int or v < 0 for v in values.values()):
            raise ContextError("budget fields must be non-negative integers")
        if not 0 < self.initial_tokens <= self.max_tokens <= 64000:
            raise ContextError("knowledge budget must satisfy 0 < initial <= maximum <= 64000")
        if self.capacity < 1:
            raise ContextError("model context has no capacity after source/output reservations")

    @property
    def capacity(self) -> int:
        return min(self.max_tokens, self.model_context_tokens - self.source_reserve_tokens
                   - self.output_reserve_tokens - self.other_prompt_tokens)


class KnowledgeContextService:
    """Persistent review sessions; each mutation is one SQLite transaction.

    The resolver returns a portable binding with repo_id, knowledge_slice,
    source_pin, catalog_hash and policy_hash. It must resolve from this view.
    Cross-repository reads require both a confirmed dependency and an explicit
    host-supplied allowlist; model tool arguments cannot grant either permission.
    """

    def __init__(self, view: KnowledgeView, state_path: str | Path, *,
                 resolver: Callable | None = None,
                 tokenizer: Callable[[str], int] | None = None,
                 tokenizer_id: str = "utf8-byte-upper-bound-v1",
                 allowed_repositories: tuple[str, ...] = (),
                 knowledge_maintenance: dict | None = None):
        self.view = view
        self._knowledge_maintenance = knowledge_maintenance
        if resolver is None:
            from .kb_service.repo_spec import resolve_snapshot_repo
            resolver = resolve_snapshot_repo
        self.resolver = resolver
        self.tokenizer = tokenizer or (lambda value: len(value.encode("utf-8")))
        if not tokenizer_id.strip():
            raise ContextError("tokenizer_id is required")
        self.tokenizer_id = tokenizer_id
        self.allowed_repositories = frozenset(allowed_repositories)
        self.state_path = Path(state_path)
        if self.state_path.resolve().is_relative_to(view.root.resolve()):
            raise ContextError("runtime context ledger must live outside the knowledge tree")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL)")

    def _db(self):
        db = sqlite3.connect(self.state_path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @staticmethod
    def _field(binding, key: str, default=""):
        return binding.get(key, default) if isinstance(binding, Mapping) else getattr(binding, key, default)

    def _binding(self, selector: str) -> dict:
        value = self.resolver(self.view, selector)
        if value is None:
            raise ContextError(f"unknown knowledge repository: {selector}")
        fields = {key: self._field(value, key) for key in
                  ("repo_id", "knowledge_slice", "source_pin", "catalog_hash", "policy_hash", "registry_hash")}
        if not fields["knowledge_slice"]:
            fields["knowledge_slice"] = self._field(value, "repo_subdir")
        if not fields["repo_id"] or not fields["knowledge_slice"]:
            raise ContextError("registry binding lacks repository identity or slice")
        # Resolving the slice through KnowledgeDocs also rejects escapes.
        KnowledgeDocs(self.view.root, fields["knowledge_slice"], verify=self.view.path)
        return fields

    def _view_identity(self) -> dict:
        # Even an explicitly configured development tree is bound to its bytes;
        # verified snapshots additionally enforce their manifest on every read.
        from .knowledge_service.containment import load_policy
        _, policy = load_policy(self._knowledge_maintenance)
        containment = {"generation": policy["generation"], "policy_digest": policy["policy_digest"]} if policy else None
        if self.view.verified:
            return {"snapshot": self.view.public_snapshot, "tree_sha256": self.view.tree_sha256,
                    "root": str(self.view.root.resolve()), **({"containment": containment} if containment else {})}
        files = {p.relative_to(self.view.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(self.view.root.rglob("*")) if p.is_file()}
        return {"snapshot": self.view.public_snapshot,
                "tree_sha256": self.view.tree_sha256 or _digest(files),
                "root": str(self.view.root.resolve()), **({"containment": containment} if containment else {})}

    def open_session(self, repository: str, *, source_pin: str, review_id: str,
                     budget: ContextBudget | None = None) -> dict:
        if not review_id.strip() or len(source_pin) not in (40, 64) or any(c not in "0123456789abcdef" for c in source_pin):
            raise ContextError("review_id and a full lowercase Git source pin are required")
        binding, budget = self._binding(repository), budget or ContextBudget()
        identity = {"view": self._view_identity(), "repository": binding,
                    "source_pin": source_pin, "review_id": review_id,
                    "tokenizer_id": self.tokenizer_id, "budget": asdict(budget),
                    "authorized_repositories": sorted(self.allowed_repositories)}
        session_id = _digest(identity)
        state = {"identity": identity, "active_tokens": min(budget.initial_tokens, budget.capacity),
                 "capacity_tokens": budget.capacity, "consumed_tokens": 0,
                 "units": {}, "requests": {}, "expansions": []}
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO sessions VALUES (?, ?)", (session_id, json.dumps(state)))
        return self.status(session_id)

    def _state(self, db, session_id: str) -> dict:
        row = db.execute("SELECT state FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ContextError("context session was not issued by this service")
        state = json.loads(row[0])
        identity = state["identity"]
        if identity["tokenizer_id"] != self.tokenizer_id or identity["view"] != self._view_identity() \
                or identity["authorized_repositories"] != sorted(self.allowed_repositories):
            raise ContextError("context snapshot, tokenizer, or authorization changed")
        if self._binding(identity["repository"]["repo_id"]) != identity["repository"]:
            raise ContextError("context repository/catalog/source binding changed")
        return state

    def _save(self, db, session_id: str, state: dict):
        db.execute("UPDATE sessions SET state=? WHERE id=?", (json.dumps(state, ensure_ascii=False), session_id))

    def status(self, session_id: str) -> dict:
        with self._db() as db:
            state = self._state(db, session_id)
        identity = {**state["identity"], "view": {key: value for key, value in state["identity"]["view"].items() if key != "root"}}
        return {"session_id": session_id, "identity": identity,
                "active_tokens": state["active_tokens"], "capacity_tokens": state["capacity_tokens"],
                "consumed_tokens": state["consumed_tokens"],
                "remaining_tokens": state["active_tokens"] - state["consumed_tokens"],
                "token_accounting": self.tokenizer_id, "actual_provider_usage": "unknown",
                "expansions": state["expansions"]}

    def expand(self, session_id: str, *, target_tokens: int, reason: str) -> dict:
        if type(target_tokens) is not int or not reason.strip():
            raise ContextError("expansion requires an integer target and a concrete reason")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db, session_id)
            duplicate = any(row["to_tokens"] == target_tokens and row["reason"] == reason[:1000]
                            for row in state["expansions"])
            if target_tokens == state["active_tokens"] and duplicate:
                pass
            elif not state["active_tokens"] < target_tokens <= state["capacity_tokens"]:
                raise ContextError("expansion exceeds reserved model capacity or does not increase the budget")
            else:
                state["expansions"].append({"from_tokens": state["active_tokens"],
                                            "to_tokens": target_tokens, "reason": reason[:1000]})
                state["active_tokens"] = target_tokens
                self._save(db, session_id, state)
        return self.status(session_id)

    def _repo_for(self, state: dict, repository: str | None) -> dict:
        own = state["identity"]["repository"]
        if not repository or repository == own["repo_id"]:
            return own
        target = self._binding(repository)
        if target["repo_id"] == own["repo_id"]:
            return own
        if target["repo_id"] not in self.allowed_repositories:
            raise ContextError("cross-repository context is not authorized by the host")
        origin = self.resolver(self.view, own["repo_id"])
        links = self._field(origin, "dependencies", ())
        confirmed = any(isinstance(link, Mapping) and link.get("status") == "confirmed"
                        and link.get("repo_id") == target["repo_id"]
                        and link.get("source_pin") == target["source_pin"]
                        and link.get("catalog_hash") == target["catalog_hash"] for link in links)
        if not confirmed or not target["source_pin"] or not target["catalog_hash"]:
            raise ContextError("cross-repository dependency lacks a confirmed source/catalog binding")
        return target

    def _count(self, content: str) -> int:
        cost = self.tokenizer(content)
        if type(cost) is not int or cost < 0 or (content and not cost):
            raise ContextError("tokenizer returned an invalid count")
        return cost

    def _deliver(self, session_id: str, request: dict, build: Callable) -> dict:
        from .knowledge_service.containment import configured, _issue_usage
        with configured(self._knowledge_maintenance):
            result = self._deliver_current(session_id, request, build)
            if result.get("documents"):
                _issue_usage(result, view=self.view, session_id=session_id)
            return result

    def _feedback_receipt(self, task_id: str, repository: str, documents: list[dict]) -> dict:
        from .knowledge_service.agreement import AgreementStore
        from .knowledge_service.feedback import KnowledgeFeedbackStore

        if AgreementStore.from_env().status()["status"] != "accepted":
            return {"status": "agreement_required", "protocol": "knowledge-feedback/1", "targets": []}
        try:
            return KnowledgeFeedbackStore.from_env().issue_delivery(
                task_id=task_id, repository=repository, view=self.view, documents=documents)
        except (OSError, sqlite3.Error):
            return {"status": "unavailable", "error": "feedback_storage_unavailable"}

    def _deliver_current(self, session_id: str, request: dict, build: Callable) -> dict:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db, session_id)
            binding = self._repo_for(state, request.get("repository"))
            docs = KnowledgeDocs(self.view.root, binding["knowledge_slice"], verify=self.view.path)
            if request["op"] == "read":
                docs._resolve_doc(request["path"])
            key = _digest({"request": request, "active_tokens": state["active_tokens"]})
            if key in state["requests"]:
                stored = state["requests"][key]
                result = stored.get("result", stored)
                expected = _digest({"session_id": session_id, "request": request,
                                    "active_tokens": state["active_tokens"], "result": result})
                content = result.get("model_content")
                if not isinstance(content, str) or hashlib.sha256(content.encode()).hexdigest() != result.get("model_content_sha256") \
                        or stored.get("receipt_sha256") != expected \
                        or self._count(content) != result.get("delivery_tokens") \
                        or not self._count(content) <= result.get("consumed_tokens", -1) <= state["consumed_tokens"]:
                    raise ContextError("cached context receipt/content integrity check failed")
                for reference in result["documents"]:
                    self.view.path(reference["path"])
                from .knowledge_service.agreement import AgreementStore

                feedback = result.get("knowledge_feedback", {})
                if AgreementStore.from_env().status()["status"] != "accepted":
                    feedback = {"status": "agreement_required", "protocol": "knowledge-feedback/1", "targets": []}
                elif feedback.get("status") in {"unavailable", "agreement_required"}:
                    from .knowledge_service.feedback import model_content_documents

                    # Repair only delivery metadata after the original packet
                    # passes integrity checks. Replaying retrieval could spend
                    # budget again or grant targets that were never delivered.
                    feedback = self._feedback_receipt(
                        state["identity"]["review_id"], binding["repo_id"],
                        model_content_documents(content))
                if feedback != result.get("knowledge_feedback", {}):
                    result = {**result, "knowledge_feedback": feedback}
                    state["requests"][key] = {"request": request, "result": result,
                        "receipt_sha256": _digest({"session_id": session_id, "request": request,
                                                  "active_tokens": state["active_tokens"], "result": result})}
                    self._save(db, session_id, state)
                return result
            units, extra = build(docs)
            rendered, references, truncated = [], [], False
            feedback_documents = []
            for unit in units:
                budget_partial = False
                content = unit.pop("content")
                unit_hash = _digest({"repository": binding["repo_id"], "path": unit["path"], "content": content})
                if unit_hash in state["units"]:
                    previous = state["units"][unit_hash]
                    if previous["complete"]:
                        continue
                    content = content[previous["delivered_chars"]:]
                    unit["continuation"] = True
                else:
                    previous = {"delivered_chars": 0}
                pins = unit.get("source_pins") or [binding["source_pin"]]
                def render(value):
                    injected = {**unit, "repo_id": binding["repo_id"], "source_pins": pins, "content": value}
                    serialized = json.dumps(injected, ensure_ascii=False).replace("<", "\\u003c")
                    return "\n<untrusted_data>\n" + serialized + "\n</untrusted_data>\n"
                # Headers and rendered delimiters consume the same budget.
                remaining = state["active_tokens"] - state["consumed_tokens"]
                full = render(content)
                if self._count(full) > remaining:
                    budget_partial = True
                    unit["partial"] = True
                    # A sliced facet is not an intact injected facet.
                    unit["included_facets"] = []
                    for metadata in ("included_facet_acceptance_modes", "included_validation_kinds", "included_facet_basis"):
                        if metadata in unit:
                            unit[metadata] = {}
                    if "available_facets" in unit:
                        unit["not_injected_facets"] = list(unit["available_facets"])
                    low, high = 0, len(content)
                    while low < high:
                        mid = (low + high + 1) // 2
                        if self._count(render(content[:mid])) <= remaining:
                            low = mid
                        else:
                            high = mid - 1
                    if not low:
                        truncated = True
                        break
                    content, full, truncated = content[:low], render(content[:low]), True
                state["consumed_tokens"] += self._count(full)
                state["units"][unit_hash] = {"delivered_chars": previous["delivered_chars"] + len(content),
                                             "complete": not budget_partial}
                references.append({**unit, "repo_id": binding["repo_id"], "content_sha256": hashlib.sha256(content.encode()).hexdigest()})
                feedback_documents.append({"path": unit["path"], "content": content})
                rendered.append(full)
                if state["consumed_tokens"] >= state["active_tokens"]:
                    break
            result = {"status": "ready" if rendered else "budget_exhausted" if truncated or state["consumed_tokens"] >= state["active_tokens"] else "no_new_context",
                      "session_id": session_id, "documents": references, "model_content": "".join(rendered),
                      "content_chars": sum(len(s) for s in rendered), "truncated": truncated,
                      "delivery_tokens": self._count("".join(rendered)),
                      "consumed_tokens": state["consumed_tokens"],
                      "remaining_tokens": state["active_tokens"] - state["consumed_tokens"],
                      "token_accounting": self.tokenizer_id, "actual_provider_usage": "unknown", **extra}
            result["model_content_sha256"] = hashlib.sha256(result["model_content"].encode()).hexdigest()
            result["knowledge_feedback"] = self._feedback_receipt(
                state["identity"]["review_id"], binding["repo_id"], feedback_documents)
            if request["op"] == "read" and references and references[-1].get("partial"):
                result["next_offset"] = request["offset"] + state["units"][unit_hash]["delivered_chars"]
            state["requests"][key] = {"request": request, "result": result,
                "receipt_sha256": _digest({"session_id": session_id, "request": request,
                                          "active_tokens": state["active_tokens"], "result": result})}
            self._save(db, session_id, state)
            return result

    def related(self, session_id: str, changed_files: list[str], *, query: str = "",
                repository: str | None = None) -> dict:
        def build(docs):
            result = docs.related(changed_files, query=query, max_documents=100,
                                  max_page_chars=65536, max_content_chars=262144, max_changed_files=None)
            return result["documents"], {"retrieval_status": result["status"], "guidance": result["guidance"]}
        return self._deliver(session_id, {"op": "related", "changed_files": changed_files,
                                         "query": query, "repository": repository}, build)

    def search(self, session_id: str, query: str, *, repository: str | None = None, limit: int = 40) -> dict:
        def build(docs):
            matches = docs.search(query, limit=limit)
            return [{"path": h["path"], "line": h["line"], "content": h["text"]} for h in matches], {}
        return self._deliver(session_id, {"op": "search", "query": query,
                                         "repository": repository, "limit": limit}, build)

    def read(self, session_id: str, path: str, *, offset: int = 0,
             limit: int = 65536, repository: str | None = None) -> dict:
        def build(docs):
            page = docs.read(path, offset=offset, limit=limit)
            return [{"path": page["path"], "offset": offset, "content": page["content"]}], {"next_offset": page["next_offset"]}
        return self._deliver(session_id, {"op": "read", "path": path, "offset": offset,
                                         "limit": limit, "repository": repository}, build)

    def inject(self, session_id: str, documents: list[dict], *, purpose: str) -> dict:
        """Budget provider-selected procedures/rules through the same ledger.

        The host passes only document IDs, never arbitrary unverified prose.
        """
        paths = [row["path"] for row in documents]
        def build(docs):
            units = []
            for row in documents:
                path = row["path"]
                page = docs.read(path, limit=65536)
                content = page["content"]
                if row.get("kind") == "direct_map":
                    from .direct_routing import _direct_quick_map_text
                    content, status = _direct_quick_map_text(content)
                    if status == "unavailable":
                        continue
                units.append({"path": path, "content": content, "purpose": purpose})
            return units, {}
        return self._deliver(session_id, {"op": "inject", "documents": documents, "purpose": purpose}, build)
