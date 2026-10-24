"""Public embedded owner of durable Direct and Strict review runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

from .direct import get_capabilities
from ...knowledge_service.containment import configured, configuration, knowledge_availability_check, with_containment
from .models import (
    Capabilities,
    QualityPollResult,
    QualityReviewResult,
    QualityReviewRequest,
    QualityRunHandle,
    FindingRecheck,
    InvalidRequestError,
    ResultDecodeError,
    StrictPollResult,
    StrictReviewResult,
    StrictReviewRequest,
    StrictRunHandle,
    StrictRuntimeConfig,
    DirectReviewRunRequest,
    ReviewRunRequest,
    ReviewFinding,
    ReviewResult,
)


from ...review.result_decode import (
    _object_rows, _diagnostics, _review_result, _quality_result,
    decode_review_result, strict_finding,
)


class ReviewRuntime:
    """Own the reservation, execution and polling lifecycle for review modes."""

    worker_call_identity = 'agent-step/1'

    def __init__(
        self, *, config: StrictRuntimeConfig | None = None,
        settings_overrides: dict[str, Any] | None = None,
        worker_prefix: tuple[str, ...] | None = None,
    ):
        # Lazy imports keep Direct-only consumers independent of server startup
        # and make this module, not the consumer, the implementation boundary.
        from ...config import Settings
        from ...app.run_service import RunService

        if config is not None and settings_overrides is not None:
            raise InvalidRequestError("choose config or legacy settings_overrides")
        if config is not None:
            checkout = Path(config.checkout_path).resolve()
            root = Path(config.allowed_root).resolve()
            if not checkout.is_relative_to(root):
                raise InvalidRequestError("checkout is outside the allowed root")
            alias = config.repository.alias
            if not config.repository.full_name:
                from ...kb_service.repo_spec import resolve_snapshot_repo
                from ...knowledge_view import KnowledgeView
                if resolve_snapshot_repo(KnowledgeView.current(), alias) is None:
                    raise InvalidRequestError("Strict runtime needs a registered repository or forge full name")
            overrides: dict[str, Any] = {
                "_env_file": None,
                "repo_paths": {alias: str(checkout)},
                "repo_full_names": {alias: config.repository.full_name},
                "mcp_allowed_repo_roots": [str(root)],
                "mcp_repo_allowlist": [alias],
                "default_repo": alias,
            }
            if config.backend:
                overrides["strict_backend"] = config.backend
            if config.run_root:
                overrides["run_root"] = config.run_root
            overrides["strict_max_workers"] = config.max_workers
        else:
            overrides = settings_overrides or {}
        if overrides.get("_env_file", object()) is None:
            # Settings also reads adapter variables through model_config; a
            # constructor-only _env_file override does not cover that path.
            class EmbeddedSettings(Settings):
                model_config = {**Settings.model_config, "env_file": None}

            settings = EmbeddedSettings(**overrides)
        else:
            settings = Settings(**overrides)
        self._core = RunService(settings, **({'worker_prefix': worker_prefix}
                                            if worker_prefix is not None else {}))
        self._core.direct_profiles = config.direct_profiles if config is not None else {}
        self._knowledge_maintenance = config.knowledge_maintenance or None if config is not None else None
        self._core.knowledge_maintenance = self._knowledge_maintenance

    def capabilities(self) -> Capabilities:
        raw = dict(self._core.capabilities())
        return get_capabilities(
            max_strict_workers=int(raw.get("max_strict_workers") or 1),
            supports_file_locking=bool(raw.get("supports_file_locking", True)),
        )

    def readiness(self, repo: str, repo_path: str = "") -> tuple[str, ...]:
        return tuple(self._core.strict_readiness(repo, repo_path))

    @with_containment
    def reserve_review(self, request: ReviewRunRequest) -> StrictRunHandle:
        """Reserve either review strategy through the shared durable service."""
        if isinstance(request, DirectReviewRunRequest):
            run_id, created = self._core.reserve_direct_review(request)
            return StrictRunHandle(run_id=str(run_id), created=bool(created))
        if not isinstance(request, StrictReviewRequest):
            raise InvalidRequestError("review requires a typed Direct or Strict request")
        from .rechecks import validate_carried

        carried = [item.to_dict() for item in request.carried_findings]
        validate_carried(carried)
        payload = {
            "kind": "pr_review",
            "repo": request.repository.alias,
            "pr": request.pr_number,
            "post": False,
            "params": {"review_depth": request.review_depth, "carried_findings": carried},
            "expected_head_sha": request.expected_head_sha,
            "repo_path": request.repo_path,
            "idempotency_key": request.idempotency_key,
        }
        if request.business_context:
            payload["params"]["business_context"] = request.business_context
        run_id, created = self._core.reserve_strict_review(payload)
        return StrictRunHandle(run_id=str(run_id), created=bool(created))

    def start_review(self, request: ReviewRunRequest) -> StrictRunHandle:
        """Alias for hosts that name the reserve-and-enqueue operation start."""
        return self.reserve_review(request)

    def reserve_direct_review(self, request: DirectReviewRunRequest) -> StrictRunHandle:
        """Compatibility spelling; Direct uses the common review lifecycle."""
        return self.reserve_review(request)

    def find_review(self, idempotency_key, *, expected_head_sha):
        run_id = self._core.find_review(idempotency_key, expected_head_sha)
        return StrictRunHandle(run_id=run_id, created=False) if run_id else None

    @staticmethod
    def run_session(prompt, **kwargs):
        """Run a bounded read-only classification or candidate session.

        Reviews use ``reserve_direct_review`` for durable identity and recovery.
        """
        from ...providers.json_session import run_readonly_json

        return run_readonly_json(prompt, **kwargs)

    @staticmethod
    def decode_session_json(text):
        from ...providers.json_session import decode_object

        return decode_object(text)

    def quality_readiness(self, repo: str, repo_path: str = "") -> tuple[str, ...]:
        """Return setup gaps for the dedicated review-readiness workflow."""
        return tuple(self._core.quality_readiness(repo, repo_path))

    def reserve_quality_review(
        self, request: QualityReviewRequest
    ) -> QualityRunHandle:
        """Reserve one exact-head quality workflow without publishing."""
        payload = {
            "kind": "pr_quality",
            "repo": request.repository.alias,
            "pr": request.pr_number,
            "post": False,
            "params": {
                "deterministic_signals": list(request.deterministic_signals),
            },
            "expected_head_sha": request.expected_head_sha,
            "repo_path": request.repo_path,
            "idempotency_key": request.idempotency_key,
        }
        run_id, created = self._core.reserve_quality_review(payload)
        return QualityRunHandle(run_id=str(run_id), created=bool(created))

    def start_quality_review(
        self, request: QualityReviewRequest
    ) -> QualityRunHandle:
        """Alias for the reserve-and-enqueue quality operation."""
        return self.reserve_quality_review(request)

    def get_status(self, run_id: str) -> StrictPollResult:
        payload = dict(self._core.get_status(run_id))
        status = payload.get("status") or {}
        state = str(status.get("state") or payload.get("state") or "unknown")
        return StrictPollResult(run_id=run_id, state=state, payload=payload)

    @with_containment
    def get_result(self, run_id: str, *, offset: int = 0) -> StrictPollResult:
        payload = dict(self._core.get_result(run_id, offset=offset))
        if configuration()["enabled"]:
            from ...app.run_service import read_knowledge_pin
            receipt = read_knowledge_pin(self._core.run_root / run_id).get("knowledge_usage")
            payload["knowledge_usage"] = receipt
            availability = knowledge_availability_check(receipt)
            payload["knowledge_availability"] = availability
            if not availability["allowed"]:
                payload["knowledge_held"] = True
                from ...run_status import TERMINAL
                if payload.get("state") in TERMINAL:
                    payload["state"] = "held"
        # The report content and structured result cross the SDK boundary; the
        # provider's private run-directory path does not.
        payload.pop("report_path", None)
        return StrictPollResult(
            run_id=run_id,
            state=str(payload.get("state") or "unknown"),
            payload=payload,
            review=_review_result(payload.get("result")),
        )

    def get_quality_result(self, run_id: str) -> QualityPollResult:
        """Return the typed envelope for one quality workflow poll."""
        payload = dict(self._core.get_quality_result(run_id))
        return QualityPollResult(
            run_id=run_id,
            state=str(payload.get("state") or "unknown"),
            payload=payload,
            review=_quality_result(payload.get("result")),
        )

    def close(self) -> None:
        self._core.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


StrictRuntime = ReviewRuntime
