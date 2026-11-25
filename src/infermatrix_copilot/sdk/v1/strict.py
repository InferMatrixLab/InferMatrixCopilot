"""Public embedded facade for the durable Strict run lifecycle."""

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
)


def _object_rows(raw: Any, field: str) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise ResultDecodeError(f"{field} must be an array of objects")
    return tuple(raw)


def _diagnostics(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ResultDecodeError("diagnostics must be an object")
    return raw


def _review_result(raw: Any) -> StrictReviewResult | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ResultDecodeError("Strict result must be an object")
    rechecks = []
    for row in _object_rows(raw.get("finding_rechecks"), "finding_rechecks"):
        outcome = row.get("outcome")
        if not isinstance(outcome, str) or outcome not in {"fixed", "still_affected", "unverified"}:
            raise ResultDecodeError("finding recheck outcome is invalid")
        try:
            rechecks.append(FindingRecheck(**row))
        except (TypeError, ValueError) as exc:
            raise ResultDecodeError(f"invalid finding recheck: {exc}") from exc
    missing = raw.get("recheck_missing") or []
    if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
        raise ResultDecodeError("recheck_missing must be an array of strings")
    complete = raw.get("rechecks_complete", False)
    if not isinstance(complete, bool):
        raise ResultDecodeError("rechecks_complete must be a boolean")
    score = raw.get("review_score")
    if score is not None and not isinstance(score, dict):
        raise ResultDecodeError("review_score must be an object")
    return StrictReviewResult(
        contract_version=str(raw.get("contract_version") or ""),
        reviewed_head_sha=str(raw.get("reviewed_head_sha") or ""),
        verdict=str(raw.get("verdict") or ""),
        summary_markdown=str(raw.get("summary_markdown") or ""),
        comments=_object_rows(raw.get("comments"), "comments"),
        findings=_object_rows(raw.get("findings"), "findings"),
        finding_rechecks=tuple(rechecks),
        rechecks_complete=complete,
        recheck_missing=tuple(missing),
        review_claim_evidence=_object_rows(raw.get("review_claim_evidence"), "review_claim_evidence"),
        review_score=score,
        review_feedback_status=str(raw.get("review_feedback_status") or ""),
        finding_dispositions=_object_rows(raw.get("finding_dispositions"), "finding_dispositions"),
        stale=bool(raw.get("stale", False)),
        diagnostics=_diagnostics(raw.get("diagnostics")),
    )


def _quality_result(raw: Any) -> QualityReviewResult | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ResultDecodeError("quality result must be an object")
    return QualityReviewResult(
        contract_version=str(raw.get("contract_version") or ""),
        reviewed_head_sha=str(raw.get("reviewed_head_sha") or ""),
        verdict=str(raw.get("verdict") or ""),
        confidence=str(raw.get("confidence") or ""),
        summary=str(raw.get("summary") or ""),
        reasons=_object_rows(raw.get("reasons"), "reasons"),
        stale=bool(raw.get("stale", False)),
        diagnostics=_diagnostics(raw.get("diagnostics")),
    )


class StrictRuntime:
    """Own a durable local Strict runtime without exposing MCP internals."""

    def __init__(
        self, *, config: StrictRuntimeConfig | None = None,
        settings_overrides: dict[str, Any] | None = None,
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
        self._core = RunService(settings)
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
    def reserve_review(self, request: StrictReviewRequest) -> StrictRunHandle:
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
        if request.include_score:
            payload["params"]["include_score"] = True
        run_id, created = self._core.reserve_strict_review(payload)
        return StrictRunHandle(run_id=str(run_id), created=bool(created))

    def start_review(self, request: StrictReviewRequest) -> StrictRunHandle:
        """Alias for hosts that name the reserve-and-enqueue operation start."""
        return self.reserve_review(request)

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
