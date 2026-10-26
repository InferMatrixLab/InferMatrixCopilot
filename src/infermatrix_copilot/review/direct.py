"""Typed Direct-review client backed by immutable wheel resources."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any
from ..knowledge_service.containment import (
    ContainmentError, configured, configuration, _issue_usage, knowledge_availability_check, with_containment,
)

from .. import __version__
from ..resources import adapters_root
from ..contracts.models import (
    DIRECT_API_VERSION,
    DURABLE_REVIEW_API_VERSION,
    KNOWLEDGE_API_VERSION,
    QUALITY_API_VERSION,
    SDK_API_VERSION,
    STRICT_API_VERSION,
    Capabilities,
    DirectCompletionDecision,
    DirectCompletionRequest,
    DirectReviewPlan,
    DirectReviewRequest,
    DocumentNotFoundError,
    DocumentPage,
    DocumentRef,
    InvalidRequestError,
    KnowledgeRoute,
    UnsupportedRepositoryError,
)

_EXCERPT_BYTES = 64 * 1024
_DEFAULT_CONTEXT_LIMIT = 256
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
_CONTEXT_ID = re.compile(r"sha256:[0-9a-f]{64}")


def _distribution_version() -> str:
    try:
        installed = importlib.metadata.version("infermatrix-copilot")
    except importlib.metadata.PackageNotFoundError:
        return __version__
    # Editable environments can retain stale dist-info while importing newer
    # source.  The built-wheel smoke separately asserts metadata equality; at
    # runtime the loaded provider code must never advertise an older artifact.
    return installed if installed == __version__ else __version__


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@lru_cache(maxsize=8)
def _resource_revision(knowledge_dir: str, adapters_dir: str) -> str:
    """Hash shipped source data, excluding interpreter-generated artifacts."""
    digest = hashlib.sha256()
    for namespace, root_value in (
        ("knowledge", knowledge_dir),
        ("adapters", adapters_dir),
    ):
        root = Path(root_value)
        digest.update(namespace.encode("utf-8"))
        digest.update(b"\0")
        paths = (
            path for path in root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
        for path in sorted(paths):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def get_capabilities(
    *,
    max_strict_workers: int = 1,
    supports_file_locking: bool = True,
) -> Capabilities:
    """Return the SDK/distribution/resource handshake without starting a server."""
    from ..knowledge_service.curation import _apply_supported

    from ..knowledge_view import KnowledgeView

    view = KnowledgeView.current()
    knowledge = view.root
    adapters = adapters_root()
    from ..kb_service.repo_spec import list_snapshot_repos
    repositories = tuple(binding.repo_id for binding in list_snapshot_repos(view))
    locking = bool(supports_file_locking)
    return Capabilities(
        distribution_version=_distribution_version(),
        sdk_api_version=SDK_API_VERSION,
        direct_api_version=DIRECT_API_VERSION,
        strict_api_version=STRICT_API_VERSION,
        quality_api_version=QUALITY_API_VERSION,
        knowledge_api_version=KNOWLEDGE_API_VERSION,
        resource_revision=_resource_revision(str(knowledge), str(adapters)),
        supported_repositories=repositories,
        supports_expected_head=True,
        supports_structured_result=True,
        supports_post_false=True,
        supports_file_locking=locking,
        supports_idempotent_strict_start=locking,
        supports_quality_review=True,
        supports_knowledge_curation=locking and _apply_supported(),
        max_strict_workers=int(max_strict_workers),
        supports_durable_direct_execution=True,
        durable_review_api_version=DURABLE_REVIEW_API_VERSION,
        supports_knowledge_generation_checkpoint=True,
        knowledge_generation_checkpoint_version=1,
    )


@dataclass(frozen=True)
class _IssuedContext:
    expected_head_sha: str
    resource_revision: str
    carried_findings: tuple[dict, ...] = ()
    knowledge_view: Any = None
    knowledge_usage: dict | None = None


class DirectClient:
    """The supported embedded Direct-review API.

    Completion validation is intentionally bound to plans issued by this
    client instance.  The registry is bounded so a long-running review host
    cannot grow memory without limit.
    """

    def __init__(self, *, max_issued_contexts: int = _DEFAULT_CONTEXT_LIMIT,
                 knowledge_context_path: str | Path | None = None,
                 allowed_knowledge_repositories: tuple[str, ...] = (),
                 knowledge_maintenance: dict | None = None) -> None:
        if max_issued_contexts < 1:
            raise InvalidRequestError("max_issued_contexts must be >= 1")
        self._adapters = adapters_root()
        self._max_issued_contexts = int(max_issued_contexts)
        self._issued_contexts: OrderedDict[str, _IssuedContext] = OrderedDict()
        self._context_lock = threading.Lock()
        self._knowledge_context_path = Path(knowledge_context_path) if knowledge_context_path else \
            Path.home() / ".infermatrix-copilot" / "knowledge-context.sqlite"
        self._knowledge_context_services: dict[str, Any] = {}
        self._allowed_knowledge_repositories = tuple(allowed_knowledge_repositories)
        self._knowledge_maintenance = knowledge_maintenance

    @staticmethod
    def _view():
        # Resolved per request: an activated knowledge snapshot takes effect on
        # the next request, and one request never mixes two trees.
        from ..knowledge_view import KnowledgeView

        return KnowledgeView.current()

    def _issued_context(self, context_id, *, touch=False, required=True):
        with self._context_lock:
            issued = self._issued_contexts.get(context_id)
            if issued is not None and touch:
                self._issued_contexts.move_to_end(context_id)
        if issued is None and required:
            raise InvalidRequestError('review_context_id was not issued by this DirectClient')
        return issued

    @property
    def resource_revision(self) -> str:
        return self._revision(self._view().root)

    def _revision(self, knowledge: Path) -> str:
        return _resource_revision(str(knowledge), str(self._adapters))

    def capabilities(self) -> Capabilities:
        return get_capabilities()

    def _document_path(self, document_id: str, view=None) -> Path:
        view = view if view is not None else self._view()
        value = str(document_id).strip().replace("\\", "/")
        pure = PurePosixPath(value)
        if not value or pure.is_absolute() or ".." in pure.parts:
            raise DocumentNotFoundError(f"invalid document_id: {document_id!r}")
        path = (view.root / pure).resolve()
        try:
            path.relative_to(view.root)
        except ValueError as exc:
            raise DocumentNotFoundError(
                f"document_id escapes the knowledge bundle: {document_id!r}"
            ) from exc
        if not path.is_file():
            raise DocumentNotFoundError(f"unknown document_id: {document_id!r}")
        return view.path(value)

    @staticmethod
    def _served_bytes(path: Path) -> bytes:
        """Document bytes as served: retired rules are withheld (their text
        stays on disk for one release cycle but is no longer knowledge)."""
        from ..knowledge_service.lifecycle import visible_text

        data = path.read_bytes()
        if path.suffix != ".md" or b"kb:rule" not in data:
            return data
        return visible_text(data.decode("utf-8")).encode("utf-8")

    def _document_id(self, path_value: str | Path, view=None) -> str:
        view = view if view is not None else self._view()
        path = Path(path_value).expanduser().resolve()
        try:
            return path.relative_to(view.root).as_posix()
        except ValueError as exc:
            raise DocumentNotFoundError(
                "provider returned a document outside the packaged knowledge bundle"
            ) from exc

    def _document_ref(self, path_value: str | Path, view=None) -> DocumentRef:
        view = view if view is not None else self._view()
        document_id = self._document_id(path_value, view)
        data = self._served_bytes(self._document_path(document_id, view))
        excerpt_data = data[:_EXCERPT_BYTES]
        return DocumentRef(
            document_id=document_id,
            sha256=_sha256(data),
            excerpt=excerpt_data.decode("utf-8", errors="replace"),
            truncated=len(data) > len(excerpt_data),
        )

    @with_containment
    def read_document(
        self,
        document_id: str,
        *,
        offset: int = 0,
        max_bytes: int = _EXCERPT_BYTES,
        review_context_id: str = "",
    ) -> DocumentPage:
        """Read one knowledge document page.

        With ``review_context_id`` the read (and every later page of it) comes
        from the knowledge tree that review's plan was issued from, so a snapshot
        activated mid-review cannot change what the reviewer reads. Without it,
        the currently active tree is used.
        """
        if offset < 0 or max_bytes < 1 or max_bytes > _EXCERPT_BYTES:
            raise InvalidRequestError(
                "offset must be >= 0 and max_bytes must be within 1..65536"
            )
        view = None
        context_id = str(review_context_id).strip().casefold()
        if configuration()["enabled"] and not context_id:
            raise ContainmentError("enforced Direct document reads require a provider-issued review_context_id")
        if context_id:
            issued = self._issued_context(context_id)
            view = issued.knowledge_view
        else:
            view = self._view()
        data = self._served_bytes(self._document_path(document_id, view))
        page = data[offset:offset + max_bytes]
        next_offset = offset + len(page) if offset + len(page) < len(data) else None
        result = DocumentPage(
            document_id=document_id,
            sha256=_sha256(data),
            offset=offset,
            content=page.decode("utf-8", errors="replace"),
            next_offset=next_offset,
            review_context_id=context_id if configuration()["enabled"] else "",
        )
        session = issued.knowledge_usage.get("delivery_session") if context_id and issued.knowledge_usage else None
        if configuration()["enabled"] and not session:
            raise ContainmentError("enforced Direct document reads require a bound review delivery session")
        _issue_usage(result.to_dict(), view=view, session_id=session)
        return result

    def _request_values(self, request: DirectReviewRequest, *, allow_sha256: bool = False) -> tuple[str, list[str]]:
        alias = request.repository.alias.strip()
        if not request.review_id.strip():
            raise InvalidRequestError("review_id must not be empty")
        if request.pr_number < 0:
            raise InvalidRequestError("pr_number must be >= 0")
        if not alias:
            raise InvalidRequestError("repository alias must not be empty")
        from ..kb_service.repo_spec import resolve_snapshot_repo
        binding = resolve_snapshot_repo(self._view(), alias)
        if binding is None and alias.replace("_", "-").casefold() not in {
            item.casefold() for item in self.capabilities().supported_repositories
        }:
            raise UnsupportedRepositoryError(f"unsupported repository: {alias!r}")
        head = request.expected_head_sha.strip()
        if not _FULL_SHA.fullmatch(head) and not (allow_sha256 and re.fullmatch(r"[0-9a-fA-F]{64}", head)):
            raise InvalidRequestError(
                "expected_head_sha must be exactly 40 hexadecimal characters"
            )

        changed_files: list[str] = []
        for item in request.changed_paths:
            value = item.path.strip().replace("\\", "/")
            path = PurePosixPath(value)
            if not value or path.is_absolute() or ".." in path.parts:
                raise InvalidRequestError(
                    f"changed path must be repository-relative: {item.path!r}"
                )
            changed_files.append(path.as_posix())
        return alias, changed_files

    @with_containment
    def open_knowledge_context(self, request: DirectReviewRequest, *, budget=None) -> dict:
        """Opt into the cumulative adaptive protocol; the v1 plan stays unchanged."""
        from ..knowledge_context import KnowledgeContextService
        alias, _ = self._request_values(request, allow_sha256=True)
        view = self._view()
        service = KnowledgeContextService(view, self._knowledge_context_path,
                                          allowed_repositories=self._allowed_knowledge_repositories,
                                          knowledge_maintenance=self._knowledge_maintenance)
        status = service.open_session(alias, source_pin=request.expected_head_sha.casefold(),
                                      review_id=request.review_id, budget=budget)
        self._knowledge_context_services[status["session_id"]] = service
        return status

    def _knowledge_context(self, session_id: str):
        service = self._knowledge_context_services.get(session_id)
        if service is None:
            import sqlite3
            from ..knowledge_context import KnowledgeContextService
            from ..knowledge_view import KnowledgeView, _load_view
            if not re.fullmatch(r"[0-9a-f]{64}", session_id) or not self._knowledge_context_path.is_file():
                raise InvalidRequestError("unknown knowledge session")
            with sqlite3.connect(self._knowledge_context_path) as db:
                row = db.execute("SELECT state FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise InvalidRequestError("unknown knowledge session")
            identity = json.loads(row[0])["identity"]["view"]
            root = Path(identity["root"])
            if (root.parent / "MANIFEST.json").is_file():
                view = _load_view(str(root.parent))
            else:
                view = KnowledgeView(root, identity["snapshot"])
            service = KnowledgeContextService(view, self._knowledge_context_path,
                                              allowed_repositories=self._allowed_knowledge_repositories,
                                              knowledge_maintenance=self._knowledge_maintenance)
            service.status(session_id)  # validate pinned bytes before resuming
            self._knowledge_context_services[session_id] = service
        return service

    @with_containment
    def plan_adaptive(self, request: DirectReviewRequest, *, budget=None) -> dict:
        """Return one budgeted model packet, with references instead of SDK excerpts."""
        from ..direct_routing import direct_review_plan
        alias, changed = self._request_values(request, allow_sha256=True)
        status = self.open_knowledge_context(request, budget=budget)
        service, session_id = self._knowledge_context(status["session_id"]), status["session_id"]
        raw = direct_review_plan(alias, title=request.title, body=request.body,
                                 changed_files=changed, diff=request.diff, view=service.view)
        routes = [{"path": service.view.relative(row["path"]), "kind": "direct_map"}
                  for row in raw["knowledge_routes"]]
        procedures = [{"path": service.view.relative(path)} for path in raw["mandatory_review_guides"]]
        guide_packet = service.inject(session_id, procedures + routes, purpose="review-procedures-and-owner-routes")
        related = service.related(session_id, changed, query="\n".join((request.title, request.body, request.diff)))
        # There is exactly one injectable copy of every returned content fragment.
        content = guide_packet["model_content"] + related["model_content"]
        context_id = _sha256(json.dumps({"session_id": session_id, "request": request.to_dict()},
                                       sort_keys=True).encode())
        packet = {"protocol_version": "knowledge-context-1", "session_id": session_id,
                "review_context_id": context_id,
                "expected_head_sha": request.expected_head_sha.casefold(),
                "model_content": content, "model_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "documents": guide_packet["documents"] + related["documents"],
                "budget": service.status(session_id), "routing": raw["routing"],
                "execution_budget": raw["execution_budget"],
                "first_review_checklist": raw["first_review_checklist"],
                "progress_update": raw["progress_update"], "completion_gate": raw["completion_gate"],
                "guidance": related.get("guidance", ""),
                "truncated": guide_packet["truncated"] or related["truncated"]}
        usage = _issue_usage(packet, view=service.view, session_id=session_id)
        self._remember_context(context_id, request.expected_head_sha.casefold(),
                               tuple(row.to_dict() for row in request.carried_findings), service.view, usage)
        return packet

    def read_knowledge_context(self, session_id: str, document_id: str, *, offset: int = 0,
                               repository: str | None = None) -> dict:
        return self._knowledge_context(session_id).read(session_id, document_id, offset=offset, repository=repository)

    def search_knowledge_context(self, session_id: str, query: str, *, repository: str | None = None) -> dict:
        return self._knowledge_context(session_id).search(session_id, query, repository=repository)

    def related_knowledge_context(self, session_id: str, changed_files: list[str], *, query: str = "",
                                  repository: str | None = None) -> dict:
        return self._knowledge_context(session_id).related(session_id, changed_files, query=query, repository=repository)

    def expand_knowledge_context(self, session_id: str, *, target_tokens: int, reason: str) -> dict:
        return self._knowledge_context(session_id).expand(session_id, target_tokens=target_tokens, reason=reason)

    def _remember_context(
        self, context_id: str, expected_head_sha: str, carried=(), view=None, usage=None,
    ) -> None:
        view = view if view is not None else self._view()
        with self._context_lock:
            self._issued_contexts[context_id] = _IssuedContext(
                expected_head_sha=expected_head_sha,
                resource_revision=self._revision(view.root),
                carried_findings=tuple(carried),
                knowledge_view=view,
                knowledge_usage=usage,
            )
            self._issued_contexts.move_to_end(context_id)
            while len(self._issued_contexts) > self._max_issued_contexts:
                self._issued_contexts.popitem(last=False)

    @with_containment
    def plan(self, request: DirectReviewRequest) -> DirectReviewPlan:
        # Lazy provider import is load-bearing: importing the public SDK models
        # must not import server/config modules or initialize provider runtime.
        from ..direct_routing import direct_review_plan

        from .rechecks import validate_carried

        carried = tuple(item.to_dict() for item in request.carried_findings)
        validate_carried(carried)
        alias, changed_files = self._request_values(request)
        expected_head = request.expected_head_sha.strip().casefold()
        view = self._view()
        revision = self._revision(view.root)
        raw = direct_review_plan(
            alias,
            title=request.title,
            body=request.body,
            changed_files=changed_files,
            view=view,
            diff=request.diff,
        )
        routes = tuple(
            KnowledgeRoute(
                owner=str(item.get("owner") or "unknown"),
                document=self._document_ref(str(item["path"]), view),
                reason=str(item.get("reason") or ""),
                quick_map=str(item.get("quick_map") or ""),
                quick_map_status=str(item.get("quick_map_status") or "unavailable"),
                read_required=bool(item.get("read_required")),
            )
            for item in raw.get("knowledge_routes") or []
        )
        from ..kb_service.repo_spec import resolve_snapshot_repo
        binding = resolve_snapshot_repo(view, alias)
        repo_name = binding.knowledge_slice.removeprefix("repos/") if binding else alias.replace("_", "-")
        map_candidates = (
            view.root / "README.md",
            view.root / "general" / "_index.md",
            view.root / "repos" / "_index.md",
            view.root / "repos" / repo_name / "_index.md",
        )
        context_payload = {
            "review_id": request.review_id,
            "repository": request.repository.to_dict(),
            "pr_number": request.pr_number,
            "expected_head_sha": expected_head,
            "title": request.title,
            "body": request.body,
            "changed_paths": [item.to_dict() for item in request.changed_paths],
            "carried_findings": carried,
            "resource_revision": revision,
        }
        if configuration()["enabled"]:
            from ..knowledge_service.containment import knowledge_maintenance_status
            current = knowledge_maintenance_status()
            context_payload["containment"] = {k:current[k] for k in ("generation", "policy_digest")}
        review_context_id = _sha256(json.dumps(
            context_payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8"))
        navigation_policy = dict(raw.get("navigation_policy") or {})
        fallback = navigation_policy.pop("fallback_entry", "")
        if fallback:
            navigation_policy["fallback_document_id"] = self._document_id(fallback, view)
        completion_gate = dict(raw.get("completion_gate") or {})
        completion_gate["operation"] = str(
            completion_gate.pop("tool", "") or "validate_direct_review"
        )
        related = dict(raw.get("related_knowledge") or {})
        related["documents"] = [{**item, "document": self._document_ref(view.path(item["path"]), view).to_dict()}
                                for item in related.get("documents") or []]
        plan = DirectReviewPlan(
            protocol_version=DIRECT_API_VERSION,
            review_context_id=review_context_id,
            resource_revision=revision,
            repository=request.repository,
            expected_head_sha=expected_head,
            knowledge_entry=self._document_ref(raw["knowledge_entry"], view),
            knowledge_routes=routes,
            mandatory_review_guides=tuple(
                self._document_ref(path, view)
                for path in raw.get("mandatory_review_guides") or []
            ),
            document_maps=tuple(
                self._document_ref(path, view)
                for path in map_candidates if path.is_file()
            ),
            routing=dict(raw.get("routing") or {}),
            navigation_policy=navigation_policy,
            execution_budget=dict(raw.get("execution_budget") or {}),
            first_review_checklist=tuple(raw.get("first_review_checklist") or ()),
            progress_update=dict(raw.get("progress_update") or {}),
            completion_gate=completion_gate,
            diagnostics=dict(raw.get("diagnostics") or {}),
            carried_findings=request.carried_findings,
            knowledge_snapshot=view.public_snapshot,
            knowledge_tree_sha256=view.tree_sha256,
            untested_public_api=dict(raw.get("untested_public_api") or {}),
            related_knowledge=related,
        )
        usage = _issue_usage(plan.to_dict(), view=view, session_id=review_context_id.removeprefix("sha256:"))
        self._remember_context(review_context_id, expected_head, carried, view, usage)
        return plan

    @with_containment
    def validate(
        self, request: DirectCompletionRequest
    ) -> DirectCompletionDecision:
        from ..direct_routing import direct_completion_result

        missing: list[str] = []
        context_id = request.review_context_id.strip().casefold()
        expected_head = request.expected_head_sha.strip().casefold()
        evidence_head = request.evidence_head_sha.strip().casefold()
        if not _CONTEXT_ID.fullmatch(context_id):
            missing.append("review_context_id must be a provider-issued sha256 id")
        issued = self._issued_context(context_id, touch=True, required=False)
        if issued is None:
            missing.append("review_context_id was not issued by this DirectClient")
        else:
            availability = knowledge_availability_check(issued.knowledge_usage)
            if not availability["allowed"]:
                raise ContainmentError("knowledge containment requires reassessment")
            if expected_head != issued.expected_head_sha:
                missing.append(
                    "expected_head_sha does not match the issued review context"
                )
            # Compare against the knowledge tree the plan was issued from, not
            # whatever is active now: activating a newer snapshot mid-review must
            # not invalidate a review pinned to the previous one.
            # Recompute WITHOUT the digest cache: the cached value was filled
            # at plan time and would hide a file changed or deleted since.
            pinned = issued.knowledge_view.root
            if (
                not pinned.is_dir()
                or _resource_revision.__wrapped__(str(pinned), str(self._adapters))
                != issued.resource_revision
            ):
                missing.append(
                    "provider resources changed after the review context was issued"
                )
        if not _FULL_SHA.fullmatch(expected_head):
            missing.append(
                "expected_head_sha must be exactly 40 hexadecimal characters"
            )
        if expected_head != evidence_head:
            missing.append("evidence_head_sha must equal expected_head_sha")
        details: dict[str, Any] = direct_completion_result(
            subtraction_signal=request.subtraction_signal,
            subtraction=list(request.subtraction),
            minimality_proof=request.minimality_proof,
            final_comment_count=request.final_comment_count,
            evidence_head_sha=evidence_head,
            existing_feedback_status=request.existing_feedback_status,
            finding_dispositions=list(request.finding_dispositions),
            knowledge_usage=issued.knowledge_usage if issued else None,
        )
        from .rechecks import checked_rechecks

        rechecks, recheck_missing = checked_rechecks(
            issued.carried_findings if issued else (),
            [item.to_dict() for item in request.finding_rechecks], evidence_head,
        )
        details["finding_rechecks"] = rechecks
        missing.extend(recheck_missing)
        missing.extend(str(item) for item in details.get("missing") or [])
        # Preserve order while avoiding duplicate explanations from overlapping
        # provider and SDK guards.
        unique_missing = tuple(dict.fromkeys(missing))
        complete = not unique_missing and bool(details.get("publish_ready"))
        return DirectCompletionDecision(
            protocol_version=DIRECT_API_VERSION,
            review_context_id=context_id,
            review_complete=complete,
            status="complete" if complete else "partial_review",
            missing=unique_missing,
            details={**details, "publish_ready": complete},
        )
