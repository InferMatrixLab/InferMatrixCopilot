"""Immutable, serializable SDK v1 contract models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SDK_API_VERSION = "1.0.0"
DURABLE_REVIEW_API_VERSION = "1.0.0"
DIRECT_API_VERSION = "1.1.0"
STRICT_API_VERSION = "1.4.0"  # StrictRuntimeConfig.max_workers
QUALITY_API_VERSION = "1.0.0"
KNOWLEDGE_API_VERSION = "1.1.0"


class _Serializable:
    """Lossless Python-wire projection shared by all public data models."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[arg-type]


class SDKError(RuntimeError):
    """Base error whose code is stable across implementation releases."""

    code = "sdk_error"


class InvalidRequestError(SDKError):
    code = "invalid_request"


class IncompatibleVersionError(SDKError):
    code = "incompatible_version"


class UnsupportedRepositoryError(SDKError):
    code = "unsupported_repository"


class DocumentNotFoundError(SDKError):
    code = "document_not_found"


class KnowledgeCurationError(SDKError):
    code = "knowledge_curation_error"


class ResultDecodeError(SDKError):
    code = "result_decode_error"


@dataclass(frozen=True)
class RepositoryRef(_Serializable):
    alias: str
    full_name: str = ""


@dataclass(frozen=True)
class ChangedPath(_Serializable):
    path: str
    status: str = ""


@dataclass(frozen=True)
class DocumentRef(_Serializable):
    document_id: str
    sha256: str
    excerpt: str
    truncated: bool


@dataclass(frozen=True)
class DocumentPage(_Serializable):
    document_id: str
    sha256: str
    offset: int
    content: str
    next_offset: int | None
    review_context_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if not self.review_context_id:
            result.pop("review_context_id")
        return result


@dataclass(frozen=True)
class KnowledgeRoute(_Serializable):
    owner: str
    document: DocumentRef
    reason: str
    quick_map: str
    quick_map_status: str
    read_required: bool


@dataclass(frozen=True)
class KnowledgeCatalogEntry(_Serializable):
    """One owner rule page a proposal may target, with its remaining room.

    ``free_bytes`` / ``free_lines`` are what a new section may add before the
    page crosses the tree validator's split gate; a proposal that needs more
    is rejected before validation so the host can reroute it."""

    document_id: str
    size_bytes: int
    free_bytes: int
    free_lines: int


@dataclass(frozen=True)
class KnowledgeEvidenceEvent(_Serializable):
    """One host-collected, untrusted fact bundle offered for distillation."""

    event_id: str
    source_kind: str
    source_reference: str
    title: str
    summary: str = ""
    changed_paths: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    # Host-selected excerpt of the change itself (unified-diff hunks, the
    # most rule-bearing files first). Bounded by the SDK per event and per
    # batch; empty when the host has none.
    diff_excerpt: str = ""


@dataclass(frozen=True)
class KnowledgeEvidenceBatch(_Serializable):
    """One bounded curation request; orchestration remains host-owned."""

    batch_id: str
    repository: RepositoryRef
    events: tuple[KnowledgeEvidenceEvent, ...]
    max_rules: int = 8


@dataclass(frozen=True)
class KnowledgeRuleProposal(_Serializable):
    """A mechanically validated append-only rule proposal."""

    proposal_id: str
    input_index: int
    page_document_id: str
    rule_id: str
    section_markdown: str
    sources: tuple[str, ...]
    page_sha256: str


@dataclass(frozen=True)
class KnowledgeProposalRejection(_Serializable):
    index: int
    reason: str


@dataclass(frozen=True)
class KnowledgeProposalValidation(_Serializable):
    batch_id: str
    repository: RepositoryRef
    accepted: tuple[KnowledgeRuleProposal, ...]
    rejected: tuple[KnowledgeProposalRejection, ...]


@dataclass(frozen=True)
class KnowledgeValidatorResult(_Serializable):
    validator_id: str
    passed: bool
    status: str
    returncode: int | None
    output: str


@dataclass(frozen=True)
class KnowledgeApplyResult(_Serializable):
    batch_id: str
    success: bool
    attempted: int
    applied: int
    accepted_indexes: tuple[int, ...]
    rejected_indexes: tuple[int, ...]
    updated_document_ids: tuple[str, ...]
    updated_on: str
    validators: tuple[KnowledgeValidatorResult, ...]
    rolled_back: bool


@dataclass(frozen=True)
class Capabilities(_Serializable):
    distribution_version: str
    sdk_api_version: str
    direct_api_version: str
    strict_api_version: str
    quality_api_version: str
    knowledge_api_version: str
    resource_revision: str
    supported_repositories: tuple[str, ...]
    supports_expected_head: bool
    supports_structured_result: bool
    supports_post_false: bool
    supports_file_locking: bool
    supports_idempotent_strict_start: bool
    supports_quality_review: bool
    supports_knowledge_curation: bool
    max_strict_workers: int
    knowledge_maintenance_protocol_version: int = 1
    supports_knowledge_containment: bool = True
    supports_durable_direct_execution: bool = False
    durable_review_api_version: str = ""
    supports_knowledge_generation_checkpoint: bool = False
    knowledge_generation_checkpoint_version: int = 0


@dataclass(frozen=True)
class CarriedFinding(_Serializable):
    finding_id: str
    source_head_sha: str
    severity: str
    path: str
    title: str
    body: str = ""
    line: int | None = None
    disputed: bool = False


@dataclass(frozen=True)
class FindingRecheck(_Serializable):
    finding_id: str
    head_sha: str
    outcome: Literal["fixed", "still_affected", "unverified"]
    evidence: str


@dataclass(frozen=True)
class DirectReviewRequest(_Serializable):
    review_id: str
    repository: RepositoryRef
    pr_number: int
    expected_head_sha: str
    title: str
    body: str
    changed_paths: tuple[ChangedPath, ...]
    carried_findings: tuple[CarriedFinding, ...] = ()
    # optional unified diff of the frozen head; enables the provider's
    # untested-public-function candidates (#164)
    diff: str = ""


@dataclass(frozen=True)
class DirectReviewPlan(_Serializable):
    protocol_version: str
    review_context_id: str
    resource_revision: str
    repository: RepositoryRef
    expected_head_sha: str
    knowledge_entry: DocumentRef
    knowledge_routes: tuple[KnowledgeRoute, ...]
    mandatory_review_guides: tuple[DocumentRef, ...]
    document_maps: tuple[DocumentRef, ...]
    routing: dict[str, Any]
    navigation_policy: dict[str, Any]
    execution_budget: dict[str, Any]
    first_review_checklist: tuple[str, ...]
    progress_update: dict[str, Any]
    completion_gate: dict[str, Any]
    diagnostics: dict[str, Any] = field(default_factory=dict)
    carried_findings: tuple[CarriedFinding, ...] = ()
    # which knowledge this plan (and every document read under its
    # review_context_id) was served from: record it with the review
    knowledge_snapshot: str = ""
    knowledge_tree_sha256: str = ""
    # new public functions no test in the request's diff names (#164);
    # {"status": "no_diff"} when the request carried no diff
    untested_public_api: dict[str, Any] = field(default_factory=dict)
    related_knowledge: dict[str, Any] = field(default_factory=dict)

FeedbackStatus = Literal[
    "checked", "disabled", "unavailable", "not_applicable"
]


@dataclass(frozen=True)
class DirectCompletionRequest(_Serializable):
    review_context_id: str
    expected_head_sha: str
    evidence_head_sha: str
    subtraction_signal: str
    subtraction: tuple[dict[str, str], ...] = ()
    minimality_proof: dict[str, str] | None = None
    final_comment_count: int = 1
    existing_feedback_status: FeedbackStatus = "not_applicable"
    finding_dispositions: tuple[dict[str, str], ...] = ()
    finding_rechecks: tuple[FindingRecheck, ...] = ()


@dataclass(frozen=True)
class DirectCompletionDecision(_Serializable):
    protocol_version: str
    review_context_id: str
    review_complete: bool
    status: str
    missing: tuple[str, ...]
    details: dict[str, Any]

@dataclass(frozen=True)
class StrictReviewRequest(_Serializable):
    repository: RepositoryRef
    pr_number: int
    expected_head_sha: str
    repo_path: str
    idempotency_key: str
    review_depth: str = "standard"
    carried_findings: tuple[CarriedFinding, ...] = ()
    business_context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DirectReviewRunRequest(_Serializable):
    """Frozen business input and explicit read-only model profile for one run."""

    review: DirectReviewRequest
    repo_path: str
    idempotency_key: str
    prompt: str = ""
    output_schema: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    resource_revision: str = ""
    business_context: dict[str, Any] = field(default_factory=dict)


# Mode-specific input contracts retain their wire shape under one lifecycle API.
ReviewRunRequest = StrictReviewRequest | DirectReviewRunRequest


@dataclass(frozen=True)
class StrictRunHandle(_Serializable):
    run_id: str
    created: bool


@dataclass(frozen=True)
class StrictRuntimeConfig(_Serializable):
    """Explicit repository and worker binding for an embedded Strict host."""

    repository: RepositoryRef
    checkout_path: str
    allowed_root: str
    backend: str = ""
    run_root: str = ""
    # Runs this host executes at once; reported back as `max_strict_workers`.
    max_workers: int = 1
    knowledge_maintenance: dict[str, Any] = field(default_factory=dict)
    # Deployment-owned transport profiles; repository/request data cannot register commands.
    direct_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if not self.direct_profiles:
            result.pop("direct_profiles")
        return result

    def __post_init__(self) -> None:
        if not self.repository.alias:
            raise InvalidRequestError("Strict runtime needs a repository alias")
        if self.repository.full_name:
            if "/" not in self.repository.full_name or any(not part for part in self.repository.full_name.split("/")):
                raise InvalidRequestError("Strict repository namespace must have non-empty path segments")
        if not self.checkout_path or not self.allowed_root:
            raise InvalidRequestError("Strict runtime needs a checkout and allowed root")
        if (isinstance(self.max_workers, bool)
                or not isinstance(self.max_workers, int)
                or self.max_workers < 1):
            raise InvalidRequestError("Strict runtime max_workers must be a positive integer")


@dataclass(frozen=True)
class StrictReviewResult(_Serializable):
    contract_version: str
    reviewed_head_sha: str
    verdict: str
    summary_markdown: str
    comments: tuple[dict[str, Any], ...]
    findings: tuple[dict[str, Any], ...]
    finding_rechecks: tuple[FindingRecheck, ...]
    rechecks_complete: bool
    recheck_missing: tuple[str, ...]
    stale: bool
    diagnostics: dict[str, Any]
    finding_dispositions: tuple[dict[str, Any], ...] = ()
    expected_head_sha: str = ""
    actual_head_sha: str = ""
    direct_result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        for name, default in (("finding_dispositions", ()), ("expected_head_sha", ""),
                              ("actual_head_sha", ""), ("direct_result", None)):
            if result[name] == default:
                result.pop(name)
        return result


@dataclass(frozen=True)
class StrictPollResult(_Serializable):
    run_id: str
    state: str
    payload: dict[str, Any]
    review: StrictReviewResult | None = None

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if self.review is not None:
            result["review"] = self.review.to_dict()
        return result

    @property
    def terminal(self) -> bool:
        return self.state not in {"queued", "planning", "running"}


@dataclass(frozen=True)
class QualityReviewRequest(_Serializable):
    repository: RepositoryRef
    pr_number: int
    expected_head_sha: str
    repo_path: str
    idempotency_key: str
    deterministic_signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class QualityRunHandle(_Serializable):
    run_id: str
    created: bool


@dataclass(frozen=True)
class QualityReviewResult(_Serializable):
    contract_version: str
    reviewed_head_sha: str
    verdict: str
    confidence: str
    summary: str
    reasons: tuple[dict[str, Any], ...]
    stale: bool
    diagnostics: dict[str, Any]


@dataclass(frozen=True)
class QualityPollResult(_Serializable):
    run_id: str
    state: str
    payload: dict[str, Any]
    review: QualityReviewResult | None = None

    @property
    def terminal(self) -> bool:
        return self.state not in {"queued", "planning", "running"}


ReviewRunHandle = StrictRunHandle
ReviewRuntimeConfig = StrictRuntimeConfig
ReviewPollResult = StrictPollResult


@dataclass(frozen=True)
class ReviewFinding(_Serializable):
    severity: str
    title: str
    body: str
    path: str
    line: int | None = None


@dataclass(frozen=True)
class ReviewResult(_Serializable):
    reviewed_head_sha: str
    summary: str
    findings: tuple[ReviewFinding, ...]
    subtraction_signal: str
    review_checks: dict[str, str]
    subtraction: tuple[dict[str, str], ...] = ()
    minimality_proof: dict[str, Any] | None = None
    existing_feedback_status: str = "not_applicable"
    finding_dispositions: tuple[dict[str, str], ...] = ()
    finding_rechecks: tuple[dict[str, str], ...] = ()
    review_complete: bool = True
