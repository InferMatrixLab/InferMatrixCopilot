"""Settings loaded from environment / .env (never committed)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_PACKAGE_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parents[2]
_USER_CONFIG = Path.home() / ".infermatrix-copilot" / ".env"
# Upper bound on concurrently executing runs in one run service; each run is a
# subprocess driving a vendor harness, so this is a sanity cap, not a target.
MAX_STRICT_WORKERS_LIMIT = 32
# Glob patterns, not fixed paths: the adapters probe must not name a specific repo
# (the core stays repo-neutral — DESIGN §V2.2.1), so it asks "is there ANY adapter
# directory here?" rather than naming one. A plain filename globs to itself, so one
# mechanism covers every entry.
_RESOURCE_MARKERS = {
    "knowledge": "AGENTS.md",
    "playbooks": "pr-review.yaml",
    "adapters": "*/manifest.yaml",
    "skills": "code-quality-review/SKILL.md",
}


def _resource_dir(name: str) -> Path:
    """Resolve source-checkout and wheel-packaged runtime data uniformly."""
    marker = _RESOURCE_MARKERS[name]
    candidates = (
        _REPO_ROOT / name,
        _PACKAGE_ROOT / "_runtime" / name,
        _PACKAGE_ROOT / name,
    )
    for candidate in candidates:
        # .is_file() is load-bearing: a glob can match a directory, and accepting one
        # would resolve the resource dir to the wrong candidate
        if any(match.is_file() for match in candidate.glob(marker)):
            return candidate
    return candidates[0]


class TierNotConfiguredError(RuntimeError):
    """A performance-mode run was requested but no performance backend is
    configured. Deliberately NOT a silent fallback to the eco model — that
    fallback is how a run once carried a high-capability label while the
    endpoint silently served the eco-class model."""


@dataclass(frozen=True)
class ResolvedTarget:
    """One immutable LLM destination: which model, on which endpoint, with
    which credential — resolved centrally by `Settings.tier_target` so loose
    (model, client) pairs can never recombine a tier's model with the wrong
    backend. `source` labels the resolution for traces ("tier:eco" /
    "tier:performance" / "global"); the key itself never appears in logs."""

    role: str
    model: str
    base_url: str
    api_key: str
    source: str
    provider: Literal["anthropic", "openai"] = "anthropic"
    # Provider registry (doc/features/provider-registry.md): which registry entry
    # serves this target and how. `api` keeps today's exact semantics;
    # `harness` targets carry NO endpoint/credential here — subscription auth
    # lives inside the vendor CLI and is never resolved by Settings.
    provider_id: str = "api"
    kind: Literal["api", "harness"] = "api"

    @property
    def host(self) -> str:
        """Endpoint hostname for traces/echo lines (`cli:<provider>` for
        harness targets, which have no HTTP endpoint of ours)."""
        if self.kind == "harness":
            return f"cli:{self.provider_id}"
        if self.base_url:
            return urlparse(self.base_url).netloc
        return ("api.openai.com" if self.provider == "openai"
                else "api.anthropic.com")


class Settings(BaseSettings):
    """Process-wide configuration, populated from the environment / `.env` and
    grouped by concern (LLM, repos, engine, push safety, metrics, escalation).
    Values are the defaults; secrets stay empty here and arrive from `.env`."""

    # The source checkout, stable per-user config, and cwd-local override are
    # loaded in that order. Installed wheels have no writable repo-local .env,
    # so the user config is the durable Strict setup location.
    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ROOT / ".env"), str(_USER_CONFIG), ".env"),
        env_file_encoding="utf-8", extra="ignore",
    )

    # Shared LLM backend. `auto` selects the only configured key; when both are
    # present it keeps the historical Anthropic default unless explicitly set.
    llm_provider: Literal["auto", "anthropic", "openai"] = "auto"
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    agent_model: str = "claude-sonnet-5"
    openai_model: str = "gpt-5.6"
    reviewer_model: str = ""  # empty -> agent_model
    intent_model: str = ""  # empty -> agent_model
    # Dual-path (双路径) model tiers for agent reasoning. The tier is chosen from
    # intent — `eco` by default, `performance` only when the user explicitly asks
    # for the high-performance model — and selects the model per run via
    # `model_for`. Both empty -> `agent_model` (so the split is a no-op until
    # `performance_model` is configured, and never changes existing behavior).
    eco_model: str = ""          # empty -> agent_model (the cost-effective default)
    performance_model: str = ""  # empty -> performance mode FAILS UPFRONT (tier_target)
    # Per-tier backends (plan v2): each tier may override the selected shared
    # backend. Atomic rule (validated below): a tier is fully unset (inherit
    # everything), model-only (different model on the shared backend), or
    # model+base_url+api_key all three (an independent backend). Partial
    # URL/key combos are startup errors — a tier URL must never receive the
    # shared credential, nor a tier credential the shared URL.
    eco_base_url: str = ""
    eco_api_key: str = ""
    performance_base_url: str = ""
    performance_api_key: str = ""
    # Served-model guard: "fail" (default) raises when a response names a
    # different model than requested (after alias normalization) — the
    # endpoint is substituting models; "warn" records loudly and continues.
    model_mismatch_policy: Literal["fail", "warn"] = "fail"
    # Audited requested->served equivalences that are NOT substitutions
    # (e.g. a gateway echoing a canonical/dated name). JSON in .env,
    # single-quoted; every application is traced (model_alias_applied).
    model_aliases: Annotated[dict, NoDecode] = {}
    llm_max_tokens: int = 16000  # 8k truncated verbose lens replies mid-JSON

    # Repos
    default_repo: str = "vllm-omni"
    repo_paths: dict[str, str] = {}
    # alias -> "owner/repo" full GitHub identity (REPO_FULL_NAMES env JSON).
    # Used by intent URL routing to validate that a pasted URL really refers to
    # a configured repo — a same-named repo under a different owner must be
    # rejected, not silently run against the local checkout. When an alias is
    # absent here, the checkout's `git remote get-url origin` is the fallback
    # authority (resolved lazily, cached per process).
    repo_full_names: dict[str, str] = {}

    # MCP server (mcp_server.py) — read-only surface for Claude Code / Codex.
    # The allowlist defaults to just `default_repo` (least privilege); extra
    # repos must be named explicitly. mcp_report_max_bytes caps a get_result
    # page so a report is never dumped unbounded over the stdio protocol.
    mcp_repo_allowlist: list[str] = []
    mcp_report_max_bytes: int = 65536
    # Roots under which a caller-supplied `repo_path` may live
    # (MCP_ALLOWED_REPO_ROOTS env JSON). Empty means "the configured checkouts
    # only" — least privilege, so a per-call path cannot point the review at an
    # arbitrary clone. Widening this is an operator decision, not a caller's.
    mcp_allowed_repo_roots: list[str] = []
    # How long an idempotency entry survives after its run finishes. The mapping
    # is deliberately retained across terminal completion (that is what makes a
    # lost-response retry return the finished run instead of re-reviewing), so
    # it has to be bounded explicitly. Far longer than any retry window.
    idem_retention_days: int = 30
    # How many reserved runs the run service executes at once (each is its
    # own subprocess). Reported to consumers as `max_strict_workers`. Runs on
    # the same checkout+PR never overlap regardless (run_service.py), because
    # they share the PR-time worktree that harness sessions write config into.
    strict_max_workers: int = 1

    # Strict execution backend (doc/features/provider-registry.md): which provider
    # powers runs. REQUIRED for Strict — `strict_readiness` names the exact
    # fix when empty (decision: explicit selection, never a silent fallback);
    # the CLI path treats empty as "api" so maintainer setups keep working.
    # Harness ids (cursor / claude-code / codex) need no API key: the vendor
    # CLI holds the subscription auth.
    strict_backend: str = ""             # "" | api | cursor | claude-code | codex | deepseek | zcode
    strict_backend_model: str = ""       # model id INSIDE the harness (optional)
    strict_backend_effort: str = ""      # optional native default; unsupported transports refuse
    # dsh is the one API-keyed harness (providers/deepseek.py): the other
    # three hold subscription auth inside the vendor CLI, so `tier_target`
    # blanks credentials for harness backends and dsh must source its own.
    # Empty falls back to `anthropic_api_key`, which on this machine holds the
    # DeepSeek key. Leave the base URL empty unless dsh must talk to a proxy —
    # its native endpoint is NOT the `/anthropic` gateway the api path uses.
    deepseek_harness_api_key: str = ""
    deepseek_harness_base_url: str = ""
    strict_backend_concurrency: int = 2  # concurrent harness sessions
    strict_backend_cli: str = ""         # binary path override (else PATH)
    strict_backend_timeout_s: float = 1800.0  # per-session wall-clock ceiling
    # Trusted local execution only; not accepted through model/review requests.
    # Each configured completion gets its own durable trace attempt/profile.
    native_completion_profiles: dict[str, dict] = {}
    native_session_profiles: dict[str, dict] = {}
    # zcode pins the served model per run through a personal provider config
    # written into the session dir (providers/zcode.py): the provider the
    # entry names (empty: the host's own configured provider, else the Z.AI
    # individual coding plan) and the reasoning level the entry must carry.
    zcode_provider_id: str = ""
    zcode_reasoning_level: Literal["low", "high", "max"] = "max"

    # Shared, human-curated knowledge base — vendored from the community docs
    # (see doc/architecture/KNOWLEDGE.md), organized as general/ (cross-repo experience) +
    # repos/<repo>/ (repo-specific). Adapters reference only their repos/<repo>/
    # slice (manifest `knowledge.repo_subdir`); general/ is shared across all
    # repos. knowledge_general_docs is the always-on general slice; every deeper
    # doc is reachable on demand via doc_search/doc_read.
    knowledge_dir: Path = _resource_dir("knowledge")
    # Host authorization only: model tools cannot enlarge their repository scope.
    allowed_knowledge_repositories: tuple[str, ...] = ()
    knowledge_general_docs: list[str] = ["general/_index.md"]
    # Drop directory for bugfix-run intake records (KNOWLEDGE_INTAKE_DIR):
    # after a pr_debug run lands a real push, `pr.harvest_debug_knowledge`
    # writes one JSON record here for an external knowledge-intake consumer
    # (e.g. the reviewbot's daily distillation batch). Empty disables the
    # drop; the step then no-ops without failing the run.
    knowledge_intake_dir: str = ""
    # Cross-host publisher for the same record (KNOWLEDGE_INTAKE_ISSUE,
    # "owner/repo#N"): the harvest step also posts it as a marked JSON
    # comment on that locked mailbox issue, so a reviewbot on another host
    # can ingest it. Operator opt-in; empty = off. Fail-open like the drop.
    knowledge_intake_issue: str = ""

    # Engine
    run_root: Path = Path.home() / ".infermatrix-copilot" / "runs"
    max_step_retries: int = 1
    max_agent_iters: int = 40
    playbooks_dir: Path = _resource_dir("playbooks")
    adapters_dir: Path = _resource_dir("adapters")

    # Push safety — dry-run by default; protected branches never force-pushed.
    allow_push: bool = False
    allow_post: bool = False  # outward writes (PR comments / issue replies) dry-run by default
    protected_branches: list[str] = ["main"]

    # PR debug
    buildkite_api_token: str = ""
    # GitHub token for the v3 remote-CI push: the push transport disables
    # credential helpers BY DESIGN (header auth only, never the shared
    # credential store), so without this the phase-4 push runs
    # unauthenticated and fails against any private-write remote
    github_token: str = ""
    pr_debug_max_groups: int = 6

    # Deprecated path expansion for explicitly configured legacy data imports.
    # Rebase execution is internal; no external checkout is discovered by default.
    rebase_agent_root: Path | None = None

    @field_validator("rebase_agent_root", mode="before")
    @classmethod
    def _optional_rebase_agent_root(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    rebase_poll_interval: int = 30
    # v3 module-agent plan reviewer (rebase_engine/plan_review.py): empty =
    # review on the run's own tier model — never silently skipped
    rebase_reviewer_model: str = ""
    # Which provider runs the rebase MODULE agents: "api" (default, the
    # in-process Anthropic tool-use loop) or a harness provider id
    # ("cursor", "claude-code", "codex"). Harness runs serve the same
    # 20-tool surface through the MCP tool bridge; see
    # doc/features/provider-registry.md.
    rebase_backend: str = "api"
    # Model id INSIDE the harness for rebase module agents (e.g.
    # "cursor-grok-4.6-high-fast"). The tier model names a RAW-API model and
    # is meaningless to a harness CLI, so it is never forwarded there; empty
    # falls back to `strict_backend_model`, then the harness's own default.
    rebase_backend_model: str = ""
    # v3 remote CI (rebase_engine/ci_loop.run_ci_rounds) — neutral knobs;
    # pipeline identities live in the adapter. Defaults are the parent's.
    rebase_ci_retries: int = 2          # push/rebuild rounds after the first
    rebase_ci_job_retry_max: int = 2    # per-job flaky retries per round
    rebase_ci_poll_sec: int = 120
    rebase_ci_timeout_sec: int = 10800  # per-build monitor budget
    rebase_ci_settle_sec: int = 60      # webhook-build settle before create
    # PR4d runtime-dir cutover (completion design D3), REPO-SCOPED: a
    # comma-separated list of repo names whose knowledge stores have been
    # MIGRATED (state/<repo>/MIGRATION_COMPLETE.json validates at resolve
    # time — a listed repo without the marker fails CLOSED). Unlisted
    # repos keep the legacy locations byte-identically. Flip only after
    # `infermatrix-copilot migrate-knowledge` succeeds (RUNBOOK step).
    imx_knowledge_runtime: str = ""

    @property
    def knowledge_runtime_repos(self) -> set[str]:
        """Repos with the PR4d cutover ACTIVE (parsed, whitespace-safe)."""
        return {r.strip() for r in self.imx_knowledge_runtime.split(",")
                if r.strip()}

    def expansion_env(self) -> dict[str, str]:
        """Manifest `${VAR}` FALLBACK values derived from this Settings
        instance (upper-cased field names): the `.env` file's configuration
        is loaded into Settings fields without being exported, so manifest
        expansion would otherwise see only shell-exported variables.
        Secret-bearing fields are excluded — a manifest path must never be
        able to pull a credential into an error message or log line.

        Adapter-declared variables (a target repo path, its runtime venv,
        an external agent root, ...) are NOT Settings fields —
        `extra="ignore"` drops them at load — yet the manifest contract
        says `.env` is where they live. Re-read the raw env files for
        those, same secret filter, Settings fields winning on a name
        collision (they already reflect process-env precedence)."""
        secret_markers = ("key", "token", "secret", "password")
        out: dict[str, str] = {}
        try:
            from dotenv import dotenv_values
        except ImportError:                      # pragma: no cover
            dotenv_values = None
        env_files = self.model_config.get("env_file") or ()
        if isinstance(env_files, (str, Path)):
            env_files = (env_files,)     # pydantic accepts a bare path too
        if dotenv_values is not None:
            # later files in model_config's env_file tuple override earlier
            # ones, mirroring pydantic-settings' own precedence
            for env_file in env_files:
                try:
                    # interpolate=False (pydantic-settings' own default):
                    # `${OTHER}` references stay literal, so a secret can
                    # never surface through a non-secret-named key
                    raw = dotenv_values(env_file, interpolate=False)
                except OSError:                  # pragma: no cover
                    continue
                for key, value in raw.items():
                    if value and not any(m in key.lower()
                                         for m in secret_markers):
                        out[key.upper()] = value
        for name, value in self.__dict__.items():
            if name.startswith("_") or \
                    any(m in name.lower() for m in secret_markers):
                continue
            if isinstance(value, (str, Path, int, float, bool)) and \
                    str(value):
                out[name.upper()] = str(value)
        return out

    # Repo profiles (design v2 §V2.3)
    profile_stale_days: int = 90        # dormancy window for unconfirmed facts
    profile_briefing_enabled: bool = True  # =0: the {no-profile} ablation arm
                                        # (§V2.3.5) — briefing + review.md
                                        # injection off, machine channel stays

    # Agent-step runtime (engine/agent_runtime/)
    review_max_iters: int = 12          # tool-loop budget for agent steps
    skills_dir: Path = _resource_dir("skills")
    memory_db: Path = Path.home() / ".infermatrix-copilot" / "debug_memory.db"
    evidence_item_chars: int = 24000  # was 6000: starving lenses pushed them
                                      # to re-read full files as per-lens tool
                                      # results — uncached tokens x n_lenses;
                                      # evidence lives ONCE in the shared
                                      # cached prefix instead
    evidence_caps: dict[str, int] = {"pr_diff": 340_000, "issue_text": 30_000,
                                     "pr_context": 15_000,
                                     # second-round per-file diff slices for
                                     # its seed files (uncapped-diff extracts)
                                     "uncovered_hunk_diffs": 100_000}
                                     # pr_diff 120k->260k: a 170k-char diff
                                     # (pr4804) lost ~30% of its hunks to the
                                     # cap and review recall collapsed to 0.16
                                     # — the cap must clear real large PRs;
                                     # the diff sits in the shared cached
                                     # prefix so the cost is one lens's worth.
                                     # 260k->340k (v15): wave-3's pr5691
                                     # (316k chars) lost its tail 18% and
                                     # arm recall on it was 0.06 vs 0.38 —
                                     # ~85k tokens still fits the context
                                     # with room for the scaffolding
    # PR context bundle (W1): description/discussion/linked issues fed to the
    # reviewer. "no_discussion" excludes comments/review threads — REQUIRED for
    # eval arms (the frozen dataset's ground truth IS the review discussion;
    # PR_CONTEXT_MODE=no_discussion keeps candidate inputs baseline-equivalent).
    pr_context_mode: Literal["full", "no_discussion"] = "full"
    # Where pr.fetch_diff / pr.gate_check / pr.fetch_ci_failures get their
    # inputs: "live" calls gh/git (today's behaviour); "snapshot" reads the
    # staged item snapshot in state (`pr_snapshot`, written by
    # improve.stage_items) and never touches the network — the shadow
    # experiment path (meta-improvement design §8.2).
    pr_context_source: Literal["live", "snapshot"] = "live"
    # IMPROVE_SHADOW=1 marks a shadow (experiment) run: the executor refuses
    # every step whose risk is not read/report, the agent runtime hardens the
    # tool scope, repo_map caches in the run dir. Set only by the engine.
    improve_shadow: bool = False
    skills_top_k: int = 3

    # Ensemble agent steps (run_agent_step_ensemble): perspective-diverse
    # fan-out + verify-and-merge — trades tokens for run-to-run robustness.
    review_ensemble: bool = True        # pr-review uses the lens ensemble
    ensemble_parallel: bool = True      # lenses are independent — run concurrently
    ensemble_samples_per_lens: int = 1  # >1 buys union recall at ~linear cost;
                                        # measured: 2x cost, no recall gain (eval iter-3)
    ensemble_zero_yield_retry: bool = True  # one single-lens re-ask on an
                                        # empty candidate list (T3 #6)
    ensemble_stagger_seconds: float = 8.0  # head start for lens 0 so the
                                        # shared prompt prefix is cached
                                        # before sibling lenses send it
    ensemble_lens_max_iters: int = 14  # rounds are ~3x cheaper with windowed
                                       # reads; 6 starved lenses into paging
                                       # death (and 38/40 truncated at T0).
                                       # 10→14 with the official v4-pro: the
                                       # judged deficit vs the agentic
                                       # baseline is per-finding reading
                                       # depth, and the baseline reads
                                       # 3-10x more code per finding
    ensemble_merge_evidence_chars: int = 360_000  # must fit the pr_diff — a
                                        # reducer that can't see the diff
                                        # can't verify (T3 forensics #5);
                                        # raised with evidence_caps.pr_diff
                                        # (60k saw ~35% of a 170k diff)

    # Per-comment agentic verification (val-gate lesson: with recall at
    # parity the arm lost on per-comment grounding — precision .54-.56 vs
    # .65). Every merged draft comment gets one small tool-loop that must
    # anchor and re-derive its claim on the PR-time tree before the budget.
    review_verify_comments: bool = True
    review_verify_max_iters: int = 4    # tiny loop: read anchor, one grep,
                                        # one consumer read, conclude
    review_verify_concurrency: int = 6  # verify calls share the pr_diff
                                        # cache prefix; modest fan-out

    # Deep-investigation engine (official-model rethink): replace the four
    # narrow lenses with one long free investigation pass (+ an independent
    # adversary pass at full depth). The lens templates were variance
    # scaffolding for a weak generator; a strong generator produces its best
    # work in the agentic-investigation shape the winning baseline uses.
    review_deep_engine: bool = True
    review_deep_max_iters: int = 32     # investigation budget per pass —
                                        # the baseline reads 3-10x more code
                                        # per finding than a 14-iter lens

    # Coverage-driven second investigation round (RFC-strict-review-deep-
    # engine open q3): after reduce+promote+verify, changed files with
    # neither a comment nor a recorded verification line seed ONE bounded
    # extra pass. Wave-2 forensics: on GT-rich items the passes' comments
    # clustered on a few central files while GT concerns sat in files no
    # pass ever wrote a line about — a fixed pass count cannot see its own
    # coverage holes.
    review_second_round: bool = True
    review_second_round_max_iters: int = 16
    review_second_round_min_files: int = 1   # uncovered-file count that
                                             # triggers the round

    # Adaptive review depth (hybrid planner, review/planner.py): deterministic
    # rules decide the clear cases in pure code; only the gray middle zone
    # spends one small LLM call, and that call can pick standard/full but
    # never light. review_ensemble=False stays the hard kill-switch (single
    # pass, no planner — beats even a forced "full"). Per-run override:
    # --task-param review_depth=... / MCP start_review(review_depth=...).
    review_depth: Literal["auto", "light", "standard", "full"] = "auto"
    review_light_max_files: int = 3   # light ceiling (with lines below):
    review_light_max_lines: int = 80  #   at/under, with no API/default or
                                      #   risk signals -> one-pass review
    # A light pass that finds nothing buys one standard pass rather than
    # shipping silence (same reflex as ensemble_zero_yield_retry, one tier up).
    # Measured on a 20-PR arm: the light tier held 7 items and produced 3.1
    # anchored findings on average where a structured single pass by the same
    # model on the same PRs produced 10.4 — one of them shipped zero.
    review_light_zero_yield_escalate: bool = True
    review_light_max_iters: int = 12  # light-pass tool budget — the token
                                      #   bound for small PRs. The solo pass
                                      #   sweeps the WHOLE checklist that four
                                      #   10-iter lenses split: 8 starved it
                                      #   into a forced block on a real 60-line
                                      #   PR (5156: cut at 11 tool calls)
    # Per-pass backend routing (role-split composition, 2026-08-15): map a
    # review pass/lens name (investigator, adversary, behavior, verification,
    # docs, round2) to "provider:model" (e.g. "cursor:composer-2.5") or a bare
    # provider id (model then comes from strict_backend_model). Unmapped
    # passes ride the run's normal backend. Wave-2/4 measured Composer
    # carrying recall and DS carrying precision on the SAME pipeline — this
    # lets the breadth roles ride the subscription CLI while verify/reducer
    # stay on the precision model. Member failures fall back per-lens to the
    # tier model (existing MoA fallback path). Env: REVIEW_LENS_BACKENDS as
    # JSON, e.g. '{"investigator": "cursor:composer-2.5"}'.
    review_lens_backends: dict[str, str] = {}
    review_promotion_model: str = ""  # coverage/residue promotion (tool-less
                                      #   ledger mining); empty -> the run's
                                      #   tier model. Owner direction
                                      #   2026-08-15: cheap seats ride
                                      #   v4-flash, precision roles (verify/
                                      #   reducer/passes) stay on the tier
                                      #   model — promoted items face the
                                      #   verify pass either way
    review_planner_model: str = ""    # gray-zone planner; empty -> the run's
                                      #   tier model (model_for(mode))

    # Mixture-of-Agents (design W6; JSON in LLM_MIXTURE, single-quoted in .env).
    # Schema: {"members": [{"model": str, "base_url"?: str, "api_key_env"?: str
    # (NAMES an env var — the secret-reference mechanism; raw "api_key" accepted
    # but discouraged)], "aggregator"?: <member>, "layers"?: 1}. Malformed JSON
    # degrades to {} (MoA off) — this optional feature must never take down the
    # copilot. Members whose model has no MODEL_PRICES entry are rejected at
    # parse (the budget cap must stay enforceable). Member identity in traces
    # is model@host only; keys are never logged.
    llm_mixture: Annotated[dict, NoDecode] = {}
    moa_when: Literal["off", "full", "performance", "always"] = "full"
    moa_member_timeout_s: float = 240.0  # per-request HTTP timeout ceiling
    moa_deadline_s: float = 480.0        # overall fan-out deadline; each request
                                         #   timeout = min(member, remaining)
    moa_max_usd: float = 1.50            # per-run MoA spend cap (reservations)
    moa_max_members: int = 4

    @field_validator("model_aliases", mode="before")
    @classmethod
    def _parse_aliases(cls, v):
        """Strictly parse MODEL_ALIASES env JSON: malformed ⇒ ValueError (this
        is a guard-policy surface — a silently-dropped alias would turn a
        legitimate equivalence into a run-killing mismatch, or vice versa)."""
        import json as _json

        if isinstance(v, dict):
            return v
        if not (isinstance(v, str) and v.strip()):
            return {}
        obj = _json.loads(v)  # raises on malformed — never tolerated here
        if not isinstance(obj, dict) or not all(
                isinstance(k, str) and isinstance(val, str)
                for k, val in obj.items()):
            raise ValueError("MODEL_ALIASES must be a JSON object of "
                             "string->string equivalences")
        return obj

    @field_validator("strict_backend")
    @classmethod
    def _validate_strict_backend(cls, v):
        """Unknown backend ids fail at startup, not mid-run: the selection is
        a routing decision and a typo must not silently mean 'api'."""
        allowed = {"", "api", "cursor", "claude-code", "codex", "deepseek",
                   "zcode"}
        if v not in allowed:
            raise ValueError(
                f"STRICT_BACKEND must be one of {sorted(allowed - {''})} "
                f"(or unset), got {v!r}")
        return v

    @field_validator("native_completion_profiles", "native_session_profiles")
    @classmethod
    def _validate_completion_profiles(cls, value):
        from .providers.completion_profiles import normalize_completion_profiles
        return normalize_completion_profiles(value)

    @field_validator("strict_max_workers")
    @classmethod
    def _validate_strict_max_workers(cls, v):
        """A zero or negative worker count would accept runs that never
        execute; an absurd one is a typo, not a capacity plan."""
        if not 1 <= v <= MAX_STRICT_WORKERS_LIMIT:
            raise ValueError(
                f"STRICT_MAX_WORKERS must be between 1 and "
                f"{MAX_STRICT_WORKERS_LIMIT}, got {v}")
        return v

    @model_validator(mode="after")
    def _validate_tier_atomicity(self):
        """Reject partial tier backends at startup: URL or key alone could pair
        the shared credential with a tier host (or the reverse)."""
        for tier in ("eco", "performance"):
            model = getattr(self, f"{tier}_model")
            url = getattr(self, f"{tier}_base_url")
            key = getattr(self, f"{tier}_api_key")
            if (url or key) and not (url and key and model):
                raise ValueError(
                    f"partial {tier} backend: {tier.upper()}_BASE_URL, "
                    f"{tier.upper()}_API_KEY and {tier.upper()}_MODEL must be "
                    "set together (or the URL/key both left unset to inherit "
                    "the selected shared backend)")
        return self

    @field_validator("llm_mixture", mode="before")
    @classmethod
    def _parse_mixture(cls, v):
        """Tolerantly parse LLM_MIXTURE env JSON: malformed ⇒ {} (MoA off)."""
        import json as _json

        if isinstance(v, dict):
            return v
        try:
            obj = _json.loads(v) if isinstance(v, str) and v.strip() else {}
            return obj if isinstance(obj, dict) else {}
        except (ValueError, TypeError):
            import logging

            logging.getLogger("infermatrix_copilot").warning(
                "ignoring malformed LLM_MIXTURE JSON — MoA stays off")
            return {}

    # Patch-review trigger thresholds
    large_diff_lines: int = 400
    large_diff_files: int = 8
    # REPO-NEUTRAL default: which modules are risky is REPO knowledge — the
    # adapter's declared tiers win and this is only the adapter-less
    # fallback (an unknown repo makes no risk claim). The v1-era adapter-
    # zero module names that used to sit here were a neutrality leak
    # (2026-08-01 audit); the adapter manifest declares them.
    high_risk_modules: list[str] = []

    # trace/1 full-fidelity capture (meta-improvement engine, design §7):
    # TRACE_STORE_ROOT names the store every run binds; every model call and
    # tool call then lands as a record with blobs. Empty = no capture (the
    # legacy span/event files are unaffected either way).
    trace_store_root: str = ""
    # Extra directories of workflow declarations (os.pathsep separated),
    # consulted after the package's builtin improve/workflows/.
    improve_workflows_dirs: str = ""
    # The weekly cycle (design §4/§10). improve_enabled is THE kill switch;
    # the slot is `improve_cycle_weekday` (0=Monday) at `improve_cycle_hour`
    # UTC, after the 04:00 PR-state sweep; the ledgers and reports live under
    # improve_ledger_dir (default ~/.infermatrix-copilot/improve).
    improve_enabled: bool = False
    improve_evolve_enabled: bool = False
    # Autonomous evaluations use executable contracts and one pinned API model.
    # "gold" retains the optional P0–P4 semantic benchmark path.
    improve_evaluation_mode: Literal["objective", "gold"] = "objective"
    improve_promotion_mode: Literal["automatic", "pr"] = "automatic"
    improve_evolve_model: str = ""
    improve_evolve_source_dir: str = ""
    improve_evolve_data_dir: str = ""
    improve_evolve_outbox_dir: str = ""
    kb_draft_max_operations: int = 6
    # IMPROVE_GOVERNED=1: this process binds the weekly budget governor for
    # its whole run (a shadow subprocess of an experiment), so every model
    # call is reserved against the same weekly files as the parent's
    improve_governed: bool = False
    improve_cycle_weekday: int = 0
    improve_cycle_hour: int = 5
    improve_ledger_dir: str = ""
    # the hard weekly envelope (P3 enforces it per model call)
    improve_budget_usd_week: float = 20.0
    improve_budget_judge_calls_week: int = 300
    # Exact requested model -> provider context ceiling. Reserve this many
    # input tokens when gateways add hidden prompts beyond the visible bytes.
    improve_input_context_limits: dict[str, int] = {}
    # Trusted domain bounds: exact "provider:model" -> stop plus one request's overshoot.
    improve_native_call_limits: dict[str, dict[str, float]] = {}

    @field_validator('improve_native_call_limits', mode='before')
    @classmethod
    def _valid_improve_native_call_limits(cls, value):
        from .budgeting import normalized_cost
        if not isinstance(value, dict) or any(
                not isinstance(key, str) or ':' not in key or not all(key.split(':', 1))
                or not isinstance(limit, dict) or set(limit) != {'stop_usd', 'overshoot_usd'}
                or any(normalized_cost(amount) is None or amount <= 0
                       for amount in limit.values()) for key, limit in value.items()):
            raise ValueError('native call limits require exact provider:model and positive finite stop/overshoot USD')
        return value

    @field_validator("improve_input_context_limits", mode="before")
    @classmethod
    def _valid_improve_input_context_limits(cls, value):
        if not isinstance(value, dict) or any(
            not isinstance(model, str) or not model.strip() or type(limit) is not int or limit <= 0
            for model, limit in value.items()
        ):
            raise ValueError("improve input context limits require exact model names and positive integer ceilings")
        return value

    # the eval review adapter's inputs (design §6.1): the GT directory holding
    # gt/curated/<item>.gold.json, the judgment sets, and the arm name whose
    # verdicts belong to this copilot's units
    improve_gt_dir: str = ""
    improve_judgments_dir: str = ""
    improve_eval_arm: str = ""
    # the gold_match judge: "api:<model>" (through LLM.create) or
    # "cli:<provider>:<model>" (cursor|claude|codex, tool-less); empty = no
    # gold matching, cells stay unlabeled
    improve_judge: str = ""
    # the review bot's GitHub login, so its own replies never count as the
    # author accepting or disputing a finding
    improve_rb_bot_login: str = ""
    # Publication (design §9.2): the engine holds no GitHub token; proposal
    # actions are files in this outbox that the maintainer routine's `propose`
    # routine picks up. Unset = the publish step is a no-op even when both
    # post gates are open. `improve_proposal_repo` names the repository the
    # proposal issues go to ("owner/name"); required for publication.
    improve_outbox_dir: str = ""
    improve_proposal_repo: str = ""
    # The frozen meta-benchmark the engine measures itself against (§11.2);
    # a shadow self-experiment gets a staged copy through IMPROVE_META_DIR.
    improve_meta_dir: str = "eval/dataset/meta"

    # Metrics (eval/METRICS_RESEARCH.md) — per-run metrics.json: CATQ = Q·S/C.
    # Reference budgets are EXPLICIT deployment assumptions (RQS3e precedent):
    # at the ref cost the log discount is ~23%; raise a ref where that
    # resource is nearly free in your deployment.
    metrics_enabled: bool = True
    token_price_in_per_mtok: float = 0.0   # 0 -> metrics.MODEL_PRICES table
    cache_read_price_factor: float = 0.0   # 0 -> provider default (0.1x input)
    token_price_out_per_mtok: float = 0.0
    ci_rate_usd_per_min: float = 0.0       # billed CI $/min, folded into usd
    cost_ref_usd: dict[str, float] = {
        "default": 1.0, "pr_review": 1.0, "pr_debug": 3.0, "pr_rebase": 3.0,
        "repo_rebase": 10.0, "issue_answer": 0.30, "issue_filter": 0.10,
    }
    cost_ref_min: dict[str, float] = {
        "default": 10.0, "pr_review": 10.0, "pr_debug": 30.0, "pr_rebase": 30.0,
        "repo_rebase": 240.0, "issue_answer": 5.0, "issue_filter": 2.0,
    }

    # Escalation email
    notify_email: str = ""
    resend_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    @property
    def reviewer(self) -> str:
        """The reviewer model, falling back to `agent_model` when unset."""
        return self.reviewer_model or self.shared_model

    @property
    def intent(self) -> str:
        """The intent-classification model, falling back to `agent_model`."""
        return self.intent_model or self.shared_model

    @property
    def resolved_llm_provider(self) -> Literal["anthropic", "openai"]:
        """Resolve `auto` without making users configure a redundant switch."""
        if self.llm_provider != "auto":
            return self.llm_provider
        if self.anthropic_api_key:
            return "anthropic"
        if self.openai_api_key:
            return "openai"
        return "anthropic"

    @property
    def shared_api_key(self) -> str:
        return (self.openai_api_key if self.resolved_llm_provider == "openai"
                else self.anthropic_api_key)

    @property
    def shared_base_url(self) -> str:
        return (self.openai_base_url if self.resolved_llm_provider == "openai"
                else self.anthropic_base_url)

    @property
    def shared_model(self) -> str:
        return (self.openai_model if self.resolved_llm_provider == "openai"
                else self.agent_model)

    def tier_target(self, mode: str, role: str = "agent") -> ResolvedTarget:
        """Central tier→backend resolution (双路径, plan v2): the ONLY place a
        tier's model is paired with an endpoint and credential. `eco` (the
        default) -> `eco_model` or `agent_model` on the eco backend (or the
        shared one); `performance` with no `performance_model` raises
        `TierNotConfiguredError` — failing upfront replaced the silent
        agent_model fallback that once mislabeled a whole run."""
        backend = self.strict_backend
        if backend not in ("", "api"):
            # Harness target (doc/features/provider-registry.md): the vendor CLI
            # holds the subscription credential, so base_url/api_key stay
            # empty and per-tier API backends do not apply — the harness
            # serves both modes (its model comes from STRICT_BACKEND_MODEL,
            # else the provider's registry default for backends that require
            # one). Resolving that default HERE and not in the transport is
            # what keeps the requested model and the served model identical:
            # a transport-private fallback left this target reporting "" while
            # dsh actually ran deepseek-v4-pro.
            from .providers.registry import PROVIDERS

            spec = PROVIDERS.get(backend)
            model = self.strict_backend_model or (
                spec.default_model if spec else "")
            return ResolvedTarget(role, model, "", "",
                                  f"backend:{backend}",
                                  self.resolved_llm_provider,
                                  provider_id=backend, kind="harness")
        if mode == "performance":
            if not self.performance_model:
                raise TierNotConfiguredError(
                    "performance tier is not configured — set PERFORMANCE_MODEL "
                    "in .env (plus PERFORMANCE_BASE_URL + PERFORMANCE_API_KEY "
                    "for an independent backend), or run without requesting "
                    "the high-performance model")
            if self.performance_base_url:
                return ResolvedTarget(role, self.performance_model,
                                      self.performance_base_url,
                                      self.performance_api_key,
                                      "tier:performance",
                                      self.resolved_llm_provider)
            return ResolvedTarget(role, self.performance_model,
                                  self.shared_base_url,
                                  self.shared_api_key, "global",
                                  self.resolved_llm_provider)
        model = self.eco_model or self.shared_model
        if self.eco_base_url:
            return ResolvedTarget(role, model, self.eco_base_url,
                                  self.eco_api_key, "tier:eco",
                                  self.resolved_llm_provider)
        return ResolvedTarget(role, model, self.shared_base_url,
                              self.shared_api_key, "global",
                              self.resolved_llm_provider)

    def model_for(self, mode: str) -> str:
        """The agent-reasoning model name for an execution tier — delegates to
        `tier_target` (same upfront failure for an unconfigured performance
        tier; no silent fallback)."""
        return self.tier_target(mode).model

    @property
    def mcp_allowed_repos(self) -> list[str]:
        """Effective MCP repo allowlist: the configured list, or `[default_repo]`
        when unset (least privilege — never every installed adapter)."""
        return list(self.mcp_repo_allowlist) or [self.default_repo]

    def repo_path(self, name: str) -> Path | None:
        """The configured filesystem path for repo `name`, or None if unknown."""
        p = self.repo_paths.get(name)
        return Path(p) if p else None

    @property
    def allowed_repo_roots(self) -> list[str]:
        """Effective roots for a caller-supplied `repo_path`: the configured
        list, or the configured checkouts themselves when unset (so the default
        permits exactly the repos this install already knows about)."""
        roots = [str(r) for r in self.mcp_allowed_repo_roots if str(r).strip()]
        return roots or [str(p) for p in self.repo_paths.values() if str(p).strip()]
