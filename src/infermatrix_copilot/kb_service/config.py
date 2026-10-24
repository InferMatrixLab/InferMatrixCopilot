"""Per-repository knowledge-lifecycle configuration and the repository registry.

Every difference between repositories lives in each adapter's human-reviewed
``knowledge_lifecycle`` manifest section; the service code never names a
repository. ``general`` (the cross-repository slice) is configured by the
service itself because it has no adapter.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..adapters import AdapterError, AdapterRegistry, RepoAdapter

KNOWLEDGE_REPO_ENV = "KB_KNOWLEDGE_REPOSITORY"
DEFAULT_KNOWLEDGE_REPOSITORY = "JiusiServe/InferMatrixCopilot"


def knowledge_repository() -> str:
    return os.environ.get(KNOWLEDGE_REPO_ENV, DEFAULT_KNOWLEDGE_REPOSITORY)


MODES = ("shadow", "auto_merge")
TRIGGERS = ("github_release", "tag_pattern", "branch_cut", "none")
VISIBILITIES = ("public", "private")
GENERAL = "general"
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")
_FULL_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_KEYS = {
    "enabled", "mode", "knowledge_dir", "intake", "release", "sweep",
    "upstream_visibility", "circuit_breaker", "protected_rules",
    "calibration_set", "model_dir", "init",
}
_INIT_KEYS = {
    "seeds", "doc_globs", "source_roots", "exclude", "module_depth", "min_module_loc",
    "pr_window", "coverage_target", "budget_usd", "judge_call_usd",
    "generator_call_usd", "harness_overhead_bytes", "pr_history_count",
    "feature_discovery_required",
}
# README* + docs/**/*.md is what profiles/establish.build_doc_corpus reads;
# the contribution and agent guides carry most cross-doc invariants.
DEFAULT_DOC_GLOBS = ("README*", "docs/**/*.md", "CONTRIBUTING.md", "AGENTS.md")


class LifecycleConfigError(ValueError):
    """An adapter's knowledge_lifecycle section is invalid."""


@dataclass(frozen=True)
class IntakeConfig:
    merged_prs: bool = True
    copilot_runs: bool = True
    human_prs: bool = True
    daily_model_budget_usd: float = 0.0


@dataclass(frozen=True)
class ReleaseConfig:
    trigger: str = "none"
    tag_pattern: str = ""
    auditor: str = ""  # plugin path relative to the adapter directory


@dataclass(frozen=True)
class InitConfig:
    """How ``kb init`` bootstraps the repository's knowledge (design kb-init §3)."""
    seeds: tuple[str, ...] = ()            # knowledge-relative: general/... or repos/<other>/...
    doc_globs: tuple[str, ...] = DEFAULT_DOC_GLOBS
    source_roots: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    module_depth: int = 2
    min_module_loc: int = 300
    pr_window_count: int = 200
    pr_window_max_age_days: int = 180
    pr_history_count: int = 1000
    coverage_target: float = 0.85
    budget_usd: float = 30.0
    judge_call_usd: float = 0.50
    # per generator call: the spend threshold handed to the transport, and the
    # bytes its own harness adds to the prompt (for the one-request overshoot bound)
    generator_call_usd: float = 2.0
    harness_overhead_bytes: int = 200_000
    # Additive opt-in: existing adapters and stage records retain their chain.
    feature_discovery_required: bool = False


@dataclass(frozen=True)
class RepoLifecycle:
    repo: str                      # knowledge repo name, or "general"
    full_name: str                 # upstream owner/name ("" for general)
    enabled: bool
    mode: str
    knowledge_dir: str             # "repos/<repo>" or "general"
    intake: IntakeConfig = field(default_factory=IntakeConfig)
    release: ReleaseConfig = field(default_factory=ReleaseConfig)
    fallback_interval_days: int = 30
    upstream_visibility: str = "public"
    retire_ratio: float = 0.10
    max_files: int = 50
    protected_rules: tuple[str, ...] = ()
    calibration_set: str = ""      # directory relative to the adapter (ships in the wheel)
    model_dir: str = ""
    adapter_dir: Path | None = None
    init: InitConfig | None = None

    @property
    def auto_merge(self) -> bool:
        return self.enabled and self.mode == "auto_merge"

    @property
    def publishes(self) -> bool:
        """Whether anything about this repository may appear in the PUBLIC
        knowledge repository (PRs, verdict comments, reports)."""
        return self.upstream_visibility == "public"


def _bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise LifecycleConfigError(f"{where} must be true or false")
    return value


def _mapping(value: object, where: str, allowed: set[str]) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise LifecycleConfigError(f"{where} must be a mapping")
    unknown = set(value) - allowed
    if unknown:
        raise LifecycleConfigError(f"{where} has unknown keys {sorted(unknown)}")
    return value


def _strings(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise LifecycleConfigError(f"{where} must be a list of non-empty strings")
    return tuple(v.strip() for v in value)


def _int(value: object, where: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise LifecycleConfigError(f"{where} must be an integer >= {minimum}")
    return value


def _number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LifecycleConfigError(f"{where} must be a finite number")
    return float(value)


def _seed_problem(seed: str, repo: str) -> str:
    parts = seed.split("/")
    if seed.startswith("/") or "\\" in seed or any(p in ("", ".", "..") for p in parts):
        return "must be a relative knowledge path without empty, . or .. segments"
    if parts[0] == GENERAL:
        return ""
    if parts[0] == "repos" and len(parts) >= 2 and _NAME.fullmatch(parts[1]):
        return "names the repository being initialised" if parts[1] == repo else ""
    return "must start with general/ or repos/<other repo>/"


def parse_init(section: object, where: str, *, repo: str, manifest: dict) -> InitConfig | None:
    """The ``init`` sub-mapping, strictly validated; None when absent."""
    if section is None:
        return None
    data = _mapping(section, where, _INIT_KEYS)
    seeds = _strings(data.get("seeds"), f"{where}.seeds")
    for seed in seeds:
        problem = _seed_problem(seed, repo)
        if problem:
            raise LifecycleConfigError(f"{where}.seeds: {seed!r} {problem}")
    if len(set(seeds)) != len(seeds):
        raise LifecycleConfigError(f"{where}.seeds has duplicates")
    if "source_roots" in data:
        source_roots = _strings(data["source_roots"], f"{where}.source_roots")
    else:
        ut = manifest.get("ut_coverage") if isinstance(manifest.get("ut_coverage"), dict) else {}
        roots = ut.get("source_roots") if isinstance(ut.get("source_roots"), list) else []
        source_roots = tuple(str(r).strip() for r in roots if isinstance(r, str) and r.strip())
    window = _mapping(data.get("pr_window"), f"{where}.pr_window", {"count", "max_age_days"})
    coverage = _number(data.get("coverage_target", 0.85), f"{where}.coverage_target")
    if not 0 < coverage <= 1:
        raise LifecycleConfigError(f"{where}.coverage_target must be in (0, 1]")
    budget = _number(data.get("budget_usd", 30.0), f"{where}.budget_usd")
    if budget <= 0:
        raise LifecycleConfigError(f"{where}.budget_usd must be > 0")
    judge_call = _number(data.get("judge_call_usd", 0.50), f"{where}.judge_call_usd")
    if judge_call < 0:
        raise LifecycleConfigError(f"{where}.judge_call_usd must be >= 0")
    generator_call = _number(data.get("generator_call_usd", 2.0), f"{where}.generator_call_usd")
    if generator_call <= 0:
        raise LifecycleConfigError(f"{where}.generator_call_usd must be > 0")
    return InitConfig(
        seeds=seeds,
        doc_globs=(_strings(data["doc_globs"], f"{where}.doc_globs") if "doc_globs" in data
                   else DEFAULT_DOC_GLOBS),
        source_roots=source_roots,
        exclude=_strings(data.get("exclude"), f"{where}.exclude"),
        module_depth=_int(data.get("module_depth", 2), f"{where}.module_depth", minimum=1),
        min_module_loc=_int(data.get("min_module_loc", 300), f"{where}.min_module_loc", minimum=0),
        pr_window_count=_int(window.get("count", 200), f"{where}.pr_window.count", minimum=1),
        pr_window_max_age_days=_int(window.get("max_age_days", 180),
                                    f"{where}.pr_window.max_age_days", minimum=1),
        pr_history_count=_int(data.get("pr_history_count", 1000), f"{where}.pr_history_count", minimum=1),
        coverage_target=coverage,
        budget_usd=budget,
        judge_call_usd=judge_call,
        generator_call_usd=generator_call,
        harness_overhead_bytes=_int(data.get("harness_overhead_bytes", 200_000),
                                    f"{where}.harness_overhead_bytes", minimum=0),
        feature_discovery_required=_bool(data.get("feature_discovery_required", False),
                                         f"{where}.feature_discovery_required"),
    )


def validate_seeds(init: InitConfig, knowledge_root: Path) -> list[str]:
    """Run-time check: every seed names an existing file or directory under
    the knowledge root. Returns one problem per missing seed."""
    root = Path(knowledge_root).resolve()
    problems = []
    for seed in init.seeds:
        target = (root / seed).resolve()
        if not target.is_relative_to(root):
            problems.append(f"seed {seed} escapes the knowledge root")
        elif not target.exists():
            problems.append(f"seed {seed} does not exist in the knowledge tree")
    return problems


def parse_lifecycle(adapter: RepoAdapter) -> RepoLifecycle | None:
    """The adapter's lifecycle, or None when it has no knowledge_lifecycle."""
    section = adapter.manifest.get("knowledge_lifecycle")
    if section is None:
        return None
    where = f"adapters/{adapter.name}/manifest.yaml knowledge_lifecycle"
    data = _mapping(section, where, _KEYS)
    repo_meta = adapter.manifest.get("repo") or {}
    knowledge = adapter.manifest.get("knowledge") or {}
    repo_subdir = str(knowledge.get("repo_subdir") or "").strip("/")
    if not repo_subdir.startswith("repos/"):
        raise LifecycleConfigError(f"{where}: adapter knowledge.repo_subdir must be repos/<repo>")
    repo = repo_subdir.removeprefix("repos/")
    if not _NAME.fullmatch(repo):
        raise LifecycleConfigError(f"{where}: invalid repo name {repo!r}")
    full_name = str(repo_meta.get("full_name") or "")
    if not _FULL_NAME.fullmatch(full_name):
        raise LifecycleConfigError(f"{where}: adapter repo.full_name must be owner/name")
    enabled = _bool(data.get("enabled", False), f"{where}.enabled")
    mode = str(data.get("mode", "shadow"))
    if mode not in MODES:
        raise LifecycleConfigError(f"{where}.mode must be one of {MODES}")
    knowledge_dir = str(data.get("knowledge_dir", repo_subdir)).strip("/")
    if knowledge_dir != repo_subdir:
        raise LifecycleConfigError(f"{where}.knowledge_dir must equal the adapter repo_subdir {repo_subdir}")
    intake = _mapping(data.get("intake"), f"{where}.intake",
                      {"merged_prs", "copilot_runs", "human_prs", "daily_model_budget_usd"})
    release = _mapping(data.get("release"), f"{where}.release", {"trigger", "tag_pattern", "auditor"})
    sweep = _mapping(data.get("sweep"), f"{where}.sweep", {"fallback_interval_days"})
    breaker = _mapping(data.get("circuit_breaker"), f"{where}.circuit_breaker",
                       {"retire_ratio", "max_files"})
    trigger = str(release.get("trigger", "none"))
    if trigger not in TRIGGERS:
        raise LifecycleConfigError(f"{where}.release.trigger must be one of {TRIGGERS}")
    if trigger == "tag_pattern" and not release.get("tag_pattern"):
        raise LifecycleConfigError(f"{where}.release.tag_pattern is required for tag_pattern")
    auditor = str(release.get("auditor") or "")
    if auditor and not (Path(adapter.root) / auditor).is_file():
        raise LifecycleConfigError(f"{where}.release.auditor does not exist: {auditor}")
    visibility = str(data.get("upstream_visibility", "public"))
    if visibility not in VISIBILITIES:
        raise LifecycleConfigError(f"{where}.upstream_visibility must be one of {VISIBILITIES}")
    if visibility == "private" and mode == "auto_merge":
        # the knowledge repository is public: a private upstream may never publish
        raise LifecycleConfigError(f"{where}: a private upstream can only run in shadow mode")
    retire_ratio = float(breaker.get("retire_ratio", 0.10))
    max_files = int(breaker.get("max_files", 50))
    if not 0 < retire_ratio <= 1 or max_files < 1:
        raise LifecycleConfigError(f"{where}.circuit_breaker values are out of range")
    budget = float(intake.get("daily_model_budget_usd", 0.0))
    if budget < 0:
        raise LifecycleConfigError(f"{where}.intake.daily_model_budget_usd must be >= 0")
    protected = data.get("protected_rules") or []
    if not isinstance(protected, list) or any(not isinstance(item, str) for item in protected):
        raise LifecycleConfigError(f"{where}.protected_rules must be a list of rule IDs")
    if mode == "auto_merge" and not data.get("calibration_set"):
        raise LifecycleConfigError(f"{where}: auto_merge requires a calibration_set")
    return RepoLifecycle(
        repo=repo,
        full_name=full_name,
        enabled=enabled,
        mode=mode,
        knowledge_dir=knowledge_dir,
        intake=IntakeConfig(
            merged_prs=_bool(intake.get("merged_prs", True), f"{where}.intake.merged_prs"),
            copilot_runs=_bool(intake.get("copilot_runs", True), f"{where}.intake.copilot_runs"),
            human_prs=_bool(intake.get("human_prs", True), f"{where}.intake.human_prs"),
            daily_model_budget_usd=budget,
        ),
        release=ReleaseConfig(trigger=trigger, tag_pattern=str(release.get("tag_pattern") or ""),
                              auditor=auditor),
        fallback_interval_days=int(sweep.get("fallback_interval_days", 30)),
        upstream_visibility=visibility,
        retire_ratio=retire_ratio,
        max_files=max_files,
        protected_rules=tuple(protected),
        calibration_set=str(data.get("calibration_set") or ""),
        model_dir=str(data.get("model_dir") or ""),
        adapter_dir=Path(adapter.root),
        init=parse_init(data.get("init"), f"{where}.init", repo=repo, manifest=adapter.manifest),
    )


def general_lifecycle(*, enabled: bool, mode: str = "shadow") -> RepoLifecycle:
    """The cross-repository slice: sources are Copilot runs and human PRs only,
    no release events, monthly full sweep."""
    if mode not in MODES:
        raise LifecycleConfigError(f"general mode must be one of {MODES}")
    return RepoLifecycle(
        repo=GENERAL, full_name="", enabled=enabled, mode=mode, knowledge_dir=GENERAL,
        intake=IntakeConfig(merged_prs=False),
        release=ReleaseConfig(trigger="none"),
    )


def load_registry(adapters_dir: str | Path, *, general: RepoLifecycle | None = None
                  ) -> dict[str, RepoLifecycle]:
    """Every adapter with a knowledge_lifecycle section, keyed by repo name.

    An invalid section raises: a misconfigured repository must never run with
    guessed settings. Adapters without the section are simply not served.
    """
    registry: dict[str, RepoLifecycle] = {}
    try:
        adapters = AdapterRegistry(adapters_dir).all()
    except AdapterError as exc:
        raise LifecycleConfigError(f"adapters cannot be loaded: {exc}") from exc
    for adapter in adapters:
        lifecycle = parse_lifecycle(adapter)
        if lifecycle is None:
            continue
        if lifecycle.repo in registry:
            raise LifecycleConfigError(f"two adapters claim knowledge repo {lifecycle.repo}")
        registry[lifecycle.repo] = lifecycle
    if general is not None:
        registry[GENERAL] = general
    return registry
