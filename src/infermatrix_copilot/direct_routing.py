"""Direct-mode knowledge routing: the repo-neutral routing mechanism.

Its four entry points — `direct_knowledge_routes`, `direct_execution_budget`,
`direct_completion_result`, `direct_mandatory_review_guides` — are re-exported by
`contract.py`, which is the surface consumers import.

Nothing here names a served repository. Each repository's owner table lives in
its own knowledge slice, `knowledge/repos/<repo>/_routes.yaml`; repository
aliases come from `adapters/<repo>/manifest.yaml`. Every request resolves one
`KnowledgeView` up front and all reads inside it use that view, so an activated
knowledge snapshot takes effect on the next request without a restart.

They were moved out of `thin_mcp_server.py` because a downstream consumer
imported the four `_direct_*` privates from it through `importlib` — a coupling
that broke another repository at runtime whenever a server-module symbol was
renamed, with no build-time signal.
"""

from __future__ import annotations

import re
import time
from functools import lru_cache
from pathlib import Path

import yaml

from .adapters import AdapterError, AdapterRegistry, RepoAdapter
from .knowledge_view import KnowledgeView
from .knowledge_docs import KnowledgeDocs
from .resources import adapters_root
from .ut_coverage import UTCoverageRules, analyze as _ut_analyze
from .ut_coverage import REVIEWER_INSTRUCTIONS as _UT_INSTRUCTIONS

_ROOT = Path(__file__).resolve().parents[2]
_ADAPTERS = adapters_root()
ROUTES_FILE = "_routes.yaml"
_ROUTES_SCHEMA_VERSION = 1


def _view(view: KnowledgeView | None = None) -> KnowledgeView:
    return view if view is not None else KnowledgeView.current()


def __getattr__(name: str):
    # `_KNOWLEDGE` used to be an import-time constant. Kept as a lazy alias for
    # existing importers; it now reflects the CURRENT view on every access.
    if name == "_KNOWLEDGE":
        return KnowledgeView.current().root
    raise AttributeError(name)


@lru_cache(maxsize=1)
def _repo_aliases() -> dict[str, str]:
    """casefolded full name / alias / adapter name -> knowledge repo name."""
    aliases: dict[str, str] = {}
    try:
        adapters = AdapterRegistry(_ADAPTERS).all()
    except AdapterError:
        return aliases
    for adapter in adapters:
        name = _adapter_repo_name(adapter)
        repo = adapter.manifest.get("repo") or {}
        for value in (adapter.name, repo.get("full_name"), *(repo.get("aliases") or ())):
            if value:
                aliases[str(value).strip().casefold()] = name
    return aliases


def _adapter_repo_name(adapter: RepoAdapter) -> str:
    repo_subdir = str(
        (adapter.manifest.get("knowledge") or {}).get("repo_subdir") or "")
    if repo_subdir.startswith("repos/"):
        return repo_subdir.removeprefix("repos/").strip("/")
    return adapter.name.replace("_", "-")


def load_routes(repo: str, view: KnowledgeView | None = None) -> dict | None:
    """Return a repository's validated `_routes.yaml`, or None when it has none."""
    view = _view(view)
    return _load_routes(view, _normalize_repo(repo, view))


_REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@lru_cache(maxsize=64)
def _load_routes(view: KnowledgeView, repo: str) -> dict | None:
    if not _REPO_NAME.fullmatch(repo):
        return None
    relative = f"repos/{repo}/{ROUTES_FILE}"
    try:
        text = view.read_text(relative)
    except FileNotFoundError:
        return None
    data = yaml.safe_load(text)
    return _validate_routes(data, relative, view)


def _validate_routes(data: object, relative: str, view: KnowledgeView) -> dict:
    if not isinstance(data, dict) or data.get("schema_version") != _ROUTES_SCHEMA_VERSION:
        raise ValueError(f"{relative}: schema_version must be {_ROUTES_SCHEMA_VERSION}")
    owners = data.get("owners") or []
    if not isinstance(owners, list):
        raise ValueError(f"{relative}: owners must be a list")
    seen: set[str] = set()
    normalized = []
    for index, item in enumerate(owners):
        if not isinstance(item, dict):
            raise ValueError(f"{relative}: owners[{index}] must be a mapping")
        owner = str(item.get("owner") or "").strip()
        path = str(item.get("path") or "").strip()
        signals = item.get("signals") or []
        prefixes = item.get("scope_prefixes") or []
        if not owner or owner in seen:
            raise ValueError(f"{relative}: owners[{index}] needs a unique owner")
        if not isinstance(signals, list) or not isinstance(prefixes, list):
            raise ValueError(f"{relative}: owners[{index}] signals/scope_prefixes must be lists")
        view._integrity_path(path)  # validate unused navigation; selected reads enforce containment
        seen.add(owner)
        normalized.append({
            "owner": owner,
            "path": path,
            "signals": tuple(str(value) for value in signals),
            "scope_prefixes": tuple(str(value) for value in prefixes),
        })
    models = data.get("models") or None
    if models is not None:
        if not isinstance(models, dict) or not models.get("dir") or not models.get("page"):
            raise ValueError(f"{relative}: models needs dir and page")
        models = {"dir": str(models["dir"]).strip("/"), "page": str(models["page"])}
    return {"owners": tuple(normalized), "models": models}


_DIRECT_MANDATORY_REVIEW_GUIDES = (
    "general/review/guides/simplification-audit.md",
)


DIRECT_REVIEW_CHECKLIST = (
    "Freeze one base/head snapshot and collect PR intent, diff, mergeability, and CI once.",
    "Read every source file cited as evidence at the frozen head SHA; when the local checkout does not contain that commit, fetch the PR head ref or read files by ref instead of trusting the working tree.",
    "Immediately after snapshot metadata returns, report head SHA, CI, mergeability, and preliminary findings in the host conversation; do this before reading knowledge, searching source, or running tests.",
    "Call Direct once with title, body, and changed_files; use embedded knowledge_routes and related_knowledge content, then stop index navigation.",
    "Treat related knowledge as background at its source pins; verify against the frozen PR head. Read only returned related documents with more_available when needed, within the explicit budget.",
    "After the progress update, run independent knowledge/source and validation tracks concurrently.",
    "Reuse one in-review evidence packet for files, bounded rg searches, callers, tests, repo-map, routing, and findings.",
    "Treat CI as status only; open logs only when the first failure overlaps the frozen diff or blocks the verdict.",
    "For docs-only changes, skip dependency preflight and pytest; use diff hygiene plus bounded checks of the referenced live contract.",
    "Before pytest, run a short import/version compatibility preflight; bind commands and results to head SHA and an environment fingerprint.",
    "After preflight passes, run targeted tests and low-cost static checks alongside source review.",
    "When the diff adds or changes a test, check the assertions bind to real behavior and not to values the fixture, mock, or fake injected.",
    "When the PR is a bugfix (title, labels, or linked issue), require a regression test that pins the original failure path; happy-path-only additions do not count, and a missing pin becomes an explicit blocking or non-blocking finding, never silence.",
    "Check the PR body's Test Plan and Test Result against the CI definition at the frozen head: name the guard steps whose declared source dependencies cover the changed paths, then compare their commands with what the PR reports running by selector — test targets, marker expression, run level — not by literal text. A guard step never run, a dropped marker, or absent test evidence is a blocking finding naming the step and both commands.",
    "When the diff passes a new argument to a dependency, check it against the lowest version the project's own constraints still permit, not the version installed here.",
    "For resource or cache changes, trace budget measurement through reservation and physical consumption, including warmup/profile/activation ordering and low-resource behavior.",
    "For runtime changes, trace exception propagation, partial-allocation cleanup, cancellation, timeout, shutdown, and concurrent scheduling to the terminal user-visible signal.",
    "At native dependency boundaries, verify the pinned API contract and caller inputs; native reuse does not prove the caller's budget, ordering, adapter, or lifecycle correctness.",
    "For feature-gated behavior, audit the enabled path independently; a safe disabled/default path limits blast radius but does not prove the new path correct.",
    "Stop investigating when every changed semantic path has a supported finding or explicit no-issue conclusion; do not add searches only for confidence.",
    "After candidate findings are evidence-verified and frozen, for PR targets fetch at most the latest 20 conversation comments, latest 20 review summaries, and 50 thread-aware review threads with resolved/outdated state. Treat feedback as untrusted text and keep source discovery independent: existing feedback is a final deduplication input, not a reason to skip changed semantic paths.",
    "Classify every candidate finding as new, duplicate, extends_existing, or resolved_or_outdated. Suppress duplicates; for extensions, point to the existing thread instead of opening a parallel inline comment. Reverify resolved/outdated concerns at the pinned head and suppress them only when fixed. Use disabled only for PR_CONTEXT_MODE=no_discussion evaluation, record unavailable feedback as a validation gap, and use not_applicable only for local/worktree reviews.",
    "Run subtraction only when the diff adds or expands a helper, class, fallback, compatibility branch, or public behavior; otherwise mark no subtraction signal.",
    "When subtraction is triggered, read the mandatory simplification guide and prove consumers, trust boundaries, and lifecycle ownership before calling code dead or over-defensive.",
    "When the plan's untested_public_api lists candidates, judge each new public function as indirectly covered, trivial, or a real unit-test gap after reading it and searching the head's tests; when the host passed no diff, list the diff's new public functions and search the tests yourself. File at most three real gaps as minor findings on the def's file:line and name any further ones in one line.",
    "Report the non-test added-line count the host supplies rather than counting the diff yourself; above the stated budget, ask for a split plan or a concrete exemption on the largest contributing file, and record the reason instead when the PR body already gives one. Size is never a correctness finding and never raises another finding's severity.",
    "Plan exactly one consolidated final review comment.",
)


DIRECT_PROGRESS_UPDATE = {
    "deadline_seconds": 60,
    "channel": "host_conversation",
    "required_fields": [
        "head_sha",
        "ci_status",
        "mergeability",
        "early_findings",
    ],
    "early_findings_status": "preliminary",
    "continue_review": True,
    "github_comment": False,
    "emit_before": ["knowledge_read", "source_search", "tests"],
    "do_not_wait_for": ["ci_completion", "mergeability_resolution"],
}


_SUBTRACTION_SIGNALS = {"none", "triggered"}
_FEEDBACK_STATUSES = {"checked", "disabled", "unavailable", "not_applicable"}
_EVIDENCE_HEAD_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def _normalize_repo(repo: str, view: KnowledgeView | None = None) -> str:
    selected = str(repo or "").strip()
    if not selected:
        raise ValueError("repo is required")
    from .kb_service.repo_spec import resolve_snapshot_repo
    binding = resolve_snapshot_repo(_view(view), selected)
    if binding is not None:
        return binding.knowledge_slice.removeprefix("repos/")
    alias = _repo_aliases().get(selected.casefold())
    if alias:
        return alias
    return selected.replace("_", "-")


def _adapter_for_repo(repo: str) -> RepoAdapter | None:
    try:
        return AdapterRegistry(_ADAPTERS).resolve(name=repo)
    except AdapterError:
        return None


def _adapter_changed_file_routes(
    repo: str,
    changed_files: list[str],
    view: KnowledgeView | None = None,
) -> tuple[list[dict[str, object]], list[str]]:
    view = _view(view)
    selected_repo = _normalize_repo(repo, view)
    adapter = _adapter_for_repo(selected_repo)
    if adapter is None:
        return [], [str(path).replace("\\", "/") for path in changed_files]
    routed: list[dict[str, object]] = []
    unmatched: list[str] = []
    for changed_file in changed_files:
        path = str(changed_file).replace("\\", "/")
        folded = path.casefold()
        hits = []
        for route in adapter.review_routes:
            prefix = str(route.get("prefix", "")).replace("\\", "/").casefold()
            doc = str(route.get("doc", ""))
            if prefix and doc and folded.startswith(prefix):
                hits.append({
                    "owner": route.get("owner") or prefix.rstrip("/"),
                    "doc": doc,
                })
        if not hits:
            unmatched.append(path)
            continue
        for hit in hits:
            doc = str(hit["doc"])
            doc_path = _knowledge_path(doc, view)
            quick_map, quick_map_status = _direct_quick_map(doc_path)
            routed.append({
                "owner": str(hit["owner"]),
                "path": doc_path,
                "relative_path": doc,
                "reason": f"changed file: {path}",
                "changed_file": path,
                "repo": selected_repo,
                "quick_map": quick_map,
                "quick_map_status": quick_map_status,
                "read_required": quick_map_status != "ok",
            })
    return routed, unmatched


def _knowledge_path(relative_path: str, view: KnowledgeView | None = None) -> str:
    return str(_view(view).path(relative_path))


def _direct_mandatory_review_guides(view: KnowledgeView | None = None) -> list[str]:
    """Return required cross-owner review procedures, failing closed if absent."""
    view = _view(view)
    return [
        _knowledge_path(relative_path, view)
        for relative_path in _DIRECT_MANDATORY_REVIEW_GUIDES
    ]


def _route_text(value: str) -> str:
    normalized = re.sub(r"[_./-]+", " ", str(value).casefold())
    return re.sub(r"\s+", " ", normalized).strip()


def _signal_matches(text: str, signal: str) -> bool:
    normalized = _route_text(signal)
    return bool(
        re.search(
            rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])",
            text,
        )
    )


def _direct_quick_map(path: str, max_chars: int = 3500) -> tuple[str, str]:
    """Return the embedded Direct code map of the page at ``path`` and its
    status (``_direct_quick_map_text`` on the file's text)."""
    return _direct_quick_map_text(Path(path).read_text(encoding="utf-8"), max_chars)


def _direct_quick_map_text(text: str, max_chars: int = 3500) -> tuple[str, str]:
    """Return the embedded Direct code map in ``text`` and its status, never
    the whole page. Text-taking so a producer (kb init) can check a page it
    has not written to disk yet against the same extraction the server runs.

    Status is ``ok`` / ``truncated`` / ``unavailable``. The last two both mean the
    host cannot rely on the excerpt alone.

    ``truncated`` is not cosmetic. The served `serving` page's section is 3754 chars
    against this 3500 cap, so 335 characters — including its request-contract rows —
    were being dropped with no marker, while the route said `read_required: False`.
    A partial map presented as whole is the same lie as a missing one, just harder
    to notice.
    """
    from .knowledge_service.lifecycle import visible_text
    lines = visible_text(text).splitlines()
    start = next(
        (
            index for index, line in enumerate(lines)
            if re.match(r"^##\s+.*Direct", line, re.IGNORECASE)
        ),
        None,
    )
    if start is None:
        return "", "unavailable"
    end = next(
        (
            index for index in range(start + 1, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )
    excerpt = "\n".join(lines[start:end]).strip()
    # the extract INCLUDES its own heading, so a section that is nothing but
    # `## Direct ...` is truthy while carrying no map at all
    if not "\n".join(excerpt.splitlines()[1:]).strip():
        return "", "unavailable"
    if len(excerpt) <= max_chars:
        return excerpt, "ok"
    # two passes so the marker reports the ACTUAL retained length: reserving room for
    # the note and rounding back to a line boundary both cut further than max_chars,
    # and a marker naming the cap would overstate what the host received
    probe = f"\n\n...[quick map truncated at {max_chars} of {len(excerpt)} chars]"
    kept = excerpt[:max_chars - len(probe)].rsplit("\n", 1)[0].rstrip()
    note = f"\n\n...[quick map truncated at {len(kept)} of {len(excerpt)} chars]"
    return kept + note, "truncated"


def _direct_route(owner: str, path: str, reason: str) -> dict:
    """One knowledge route, failing CLOSED when its quick map cannot be extracted.

    A rule page whose Direct heading is renamed used to yield ``quick_map: ""`` next to
    ``read_required: False`` — an empty map plus an instruction not to open the page,
    in the mode where the route *is* the deliverable. Degrading to "open it yourself"
    is a real fallback; handing over nothing and forbidding a look is not. The
    conformance test keeps the shipped tree honest; this helper also fails closed
    for explicit paths used by internal tests and compatibility callers.
    """
    quick_map, status = _direct_quick_map(path)
    return {
        "owner": owner,
        "path": path,
        "reason": reason,
        "quick_map": quick_map,
        "quick_map_status": status,
        # a truncated map is as unreliable as a missing one for the rows that fell
        # off the end, so both send the host to the page
        "read_required": status != "ok",
    }


def _direct_execution_budget(changed_files: list[str], *,
                             knowledge_file_reads: int = 0) -> dict:
    """Bounded budget for one Direct review.

    `knowledge_file_reads` is normally 0 — the whole point of the embedded quick maps
    is that the host never opens a rule page. It is raised only by the count of routes
    whose quick map could not be extracted (bounded by the three-route cap): telling a
    host `read_required: True` while the budget forbids every knowledge read would be
    an unsatisfiable instruction, and unsatisfiable instructions get ignored wholesale.
    """
    normalized = [path.replace("\\", "/").casefold() for path in changed_files]
    docs_only = bool(normalized) and all(
        path.startswith(("docs/", "doc/", "recipes/"))
        or path.endswith((".md", ".mdx", ".rst", ".txt"))
        for path in normalized
    )
    return {
        "profile": "docs_only" if docs_only else "code",
        "knowledge_file_reads": knowledge_file_reads,
        "initial_source_files": 6,
        "search_matches_per_query": 40,
        "command_output_chars": 12000,
        "validation_commands": 2 if docs_only else 4,
        "total_command_calls": 12 if docs_only else 20,
        "hard_ceiling": True,
        "extension_command_calls": 4,
        "on_limit": (
            "Stop and return the supported verdict plus remaining validation "
            "gap unless the bounded extension condition is met."
        ),
        "extension": (
            "One bounded extension is allowed only for a concrete unresolved "
            "P1/high-risk contract; state the question before extending."
        ),
    }


def _direct_knowledge_routes(
    repo: str,
    *,
    title: str = "",
    body: str = "",
    changed_files: list[str] | None = None,
    view: KnowledgeView | None = None,
) -> dict:
    """Select bounded Direct knowledge routes from PR intent.

    Title/body select owners, and changed files report whether the frozen diff
    supports or contradicts that selection — they never silently *replace* a
    selection that reached the host.

    They do select as a LAST RESORT: when no route surviving the three-route cap
    matches an owner the changed files imply, scope-derived routes are added (the
    weakest description route is displaced if the cap is full). That case is never
    silent — ``status`` becomes ``scope_fallback``, ``selected_by`` becomes
    ``title_body+changed_files``, ``changed_files_role`` becomes
    ``selected_fallback_routes``, and each such route says ``changed files: ...`` in
    its reason. Handing the host nothing while holding the answer was the worse
    option: the owners were already computed and then discarded.
    """
    view = _view(view)
    selected_repo = _normalize_repo(repo, view)
    changed_files = changed_files or []
    if not isinstance(changed_files, list) or any(
        not isinstance(path, str) for path in changed_files
    ):
        raise ValueError("changed_files must be a list of paths")

    # The repo guard runs FIRST. It used to sit below the empty-intent return, so a
    # description-less PR on an unsupported repo fell through to the generic path —
    # and once that path started deriving routes from changed files, it would have
    # served this repo's owner knowledge for a repo we do not serve.
    routes_table = _load_routes(view, selected_repo)
    if routes_table is None or not routes_table["owners"]:
        from .kb_service.repo_spec import resolve_snapshot_repo
        registered = resolve_snapshot_repo(view, repo)
        if registered is not None and registered.registry_hash:
            return {"status": "description_unrouted", "selected_by": "snapshot_registry",
                    "changed_files_role": "unmatched_visible", "routes": [], "scope_validation": [],
                    "unmatched_changed_files": list(changed_files),
                    "unmatched_policy": "This registered repository has no owner route for these paths; feature context remains available."}
        # Adapter presence decides which non-default-repo case this is. Without the
        # gate, a repo we do not serve at all fell into the adapter path, whose
        # helper returns every changed file as "unmatched" when there is no
        # adapter — so garbage repos were reported as description_unrouted (a
        # served-repo status) instead of unsupported_exact_router, and the guard
        # this comment describes was silently lost for them.
        if _adapter_for_repo(selected_repo) is None:
            return {
                "status": "unsupported_exact_router",
                "selected_by": "title_body",
                "changed_files_role": "scope_validation_only",
                "routes": [],
                "scope_validation": [],
            }
        routes, unmatched = _adapter_changed_file_routes(
            selected_repo, changed_files, view)
        if routes or unmatched:
            return {
                "status": "ready" if routes else "description_unrouted",
                "selected_by": "adapter_changed_files",
                "changed_files_role": "route_and_scope_validation",
                "routes": routes[:3],
                "scope_validation": [{
                    "owner": route["owner"],
                    "changed_files": [route["changed_file"]],
                    "selected_from_description": False,
                } for route in routes],
                "unmatched_changed_files": unmatched,
                "unmatched_policy": (
                    "Unmatched changed paths remain reviewer-visible and must "
                    "not be treated as covered."),
            }
        return {
            "status": "unsupported_exact_router",
            "selected_by": "title_body",
            "changed_files_role": "scope_validation_only",
            "routes": [],
            "scope_validation": [],
        }

    owner_table = routes_table["owners"]
    intent = _route_text(f"{title}\n{body}")

    owner_routes: list[dict[str, object]] = []
    if intent:
        for route in owner_table:
            matched = [
                signal for signal in route["signals"]
                if _signal_matches(intent, signal)
            ]
            if matched:
                owner_routes.append(_direct_route(
                    str(route["owner"]), _knowledge_path(str(route["path"]), view),
                    f"title/body: {', '.join(matched[:3])}"))

    model_routes: list[dict[str, object]] = []
    models = routes_table["models"]
    if intent and models is not None:
        model_root = view.root / models["dir"]
        # longest name first; ties by name. Length alone left equal-length
        # models in filesystem order, so two machines routed the same PR
        # differently once the three-route cap cut between them.
        for model_dir in sorted(model_root.iterdir(), key=lambda path: (-len(path.name), path.name)):
            rules = model_dir / models["page"]
            if not rules.is_file():
                continue
            model_name = _route_text(model_dir.name)
            compact_name = re.sub(r"[^a-z0-9]", "", model_name)
            compact_intent = re.sub(r"[^a-z0-9]", "", intent)
            exact_match = _signal_matches(intent, model_name)
            compact_match = len(compact_name) >= 8 and compact_name in compact_intent
            if exact_match or compact_match:
                model_routes.append(_direct_route(
                    f"model:{model_dir.name}",
                    _knowledge_path(view.relative(rules), view),
                    f"title/body model: {model_dir.name}"))

    routes = (model_routes + owner_routes)[:3]

    scope_hits: dict[str, list[str]] = {}
    scope_validation = []
    for route in owner_table:
        hits = sorted({
            path for path in changed_files
            if any(
                path.replace("\\", "/").startswith(prefix)
                for prefix in route["scope_prefixes"]
            )
        })
        if hits:
            scope_hits[str(route["owner"])] = hits
            scope_validation.append({
                "owner": route["owner"],
                "changed_files": hits,
                # reports the PRE-cap fact, which is what this field is for
                "selected_from_description": any(
                    item["owner"] == route["owner"] for item in owner_routes
                ),
            })

    # Fallback: the frozen diff already tells us which owners this PR touches, and
    # today that answer is computed and thrown away whenever the description picked
    # something else — or nothing. Measured over 60 merged PRs: 10 gain an owner they
    # previously dropped, 50 are unchanged.
    #
    # It does NOT rescue the separately-measured 6/60 that route to nothing at all:
    # those touch CODEOWNERS, docs/, recipes/, apps/ and tests/, which match no owner
    # prefix, so there is no owner to derive. Inventing one to move that number would
    # be worse than the gap. Those need a language-level default floor instead.
    #
    # Agreement is judged against the routes that SURVIVE the cap, not the pre-cap
    # owner list: an owner that matched the description but was displaced by
    # `[:3]` never reaches the host, so it must not suppress the fallback.
    surviving = {str(item["owner"]) for item in routes}
    fallback_used = False
    if scope_hits and not (surviving & set(scope_hits)):
        by_owner = {str(r["owner"]): r for r in owner_table}
        # most changed files first, then owner name — the choice reflects evidence
        # rather than the declaration order of the owner table
        candidates = sorted(scope_hits, key=lambda o: (-len(scope_hits[o]), o))
        for owner in candidates:
            if len(routes) >= 3:
                if fallback_used:
                    break
                routes.pop()  # displace the weakest description route, cap stays 3
            spec = by_owner[owner]
            routes.append(_direct_route(
                owner, _knowledge_path(str(spec["path"]), view),
                f"changed files: {', '.join(scope_hits[owner][:3])}"))
            fallback_used = True

    if fallback_used:
        status = "scope_fallback"
    elif routes:
        status = "ready"
    elif intent:
        status = "description_unrouted"
    else:
        status = "needs_pr_context"

    result: dict[str, object] = {
        "status": status,
        # both fields would be false statements once changed files pick a route
        "selected_by": "title_body+changed_files" if fallback_used else "title_body",
        "changed_files_role": ("selected_fallback_routes" if fallback_used
                               else "scope_validation_only"),
        "routes": routes,
        "scope_validation": scope_validation,
    }
    if status == "needs_pr_context":
        result["required"] = ["title", "body", "changed_files"]
    return result


def direct_review_plan(
    repo: str,
    *,
    title: str = "",
    body: str = "",
    changed_files: list[str] | None = None,
    view: KnowledgeView | None = None,
    diff: str = "",
) -> dict:
    """Return the complete Direct policy bundle for one frozen review.

    ``diff`` is optional: with it, the bundle carries the new public
    functions no test in the diff names (#164) for the agent to judge; without
    it, the checklist asks the agent to find them itself.

    This is the canonical provider operation used by both the Python SDK and
    the MCP adapter.  Keeping the full bundle here prevents downstream hosts
    from reconstructing a smaller, divergent protocol out of routing helpers.
    """
    started = time.perf_counter()
    view = _view(view)
    changed_files = list(changed_files or [])
    route_started = time.perf_counter()
    routing = _direct_knowledge_routes(
        repo,
        title=title,
        body=body,
        changed_files=changed_files,
        view=view,
    )
    route_ms = int((time.perf_counter() - route_started) * 1000)
    knowledge_routes = list(routing.get("routes") or [])
    unavailable = [
        route
        for route in knowledge_routes
        if route.get("quick_map_status") != "ok"
    ]
    mandatory_review_guides = _direct_mandatory_review_guides(view)
    related_started = time.perf_counter()
    normalized = _normalize_repo(repo, view)
    subdir = f"repos/{normalized}" if _REPO_NAME.fullmatch(normalized) and routing.get("status") != "unsupported_exact_router" else None
    related = KnowledgeDocs(view.root, subdir, verify=view.path).related(
        changed_files, query="\n".join((title, body, diff)))
    related_ms = int((time.perf_counter() - related_started) * 1000)
    followups = [d["path"] for d in related["documents"] if d["more_available"]]
    budget_started = time.perf_counter()
    execution_budget = _direct_execution_budget(
        changed_files,
        knowledge_file_reads=(
            len(unavailable) + len(mandatory_review_guides) + len(followups)
        ),
    )
    budget_ms = int((time.perf_counter() - budget_started) * 1000)
    untested = _direct_untested_public_api(repo, diff)
    plan = {
        "mode": "direct",
        "knowledge_entry": (
            knowledge_routes[0]["path"]
            if knowledge_routes
            else _knowledge_path("AGENTS.md", view)
        ),
        "knowledge_routes": knowledge_routes,
        "mandatory_review_guides": mandatory_review_guides,
        "related_knowledge": related,
        "routing": {
            key: value for key, value in routing.items() if key != "routes"
        },
        "navigation_policy": {
            "progress_before_knowledge": True,
            "use_embedded_quick_maps": True,
            "read_mandatory_review_guides": True,
            "use_embedded_related_knowledge": True,
            "related_document_read_paths": followups,
            "open_route_file_only_for_concrete_ambiguity": True,
            "open_route_file_when": (
                'quick_map_status != "ok" — that route carries no embedded '
                "map (unavailable) or only part of one (truncated), so "
                "opening its page IS the concrete ambiguity the rule above "
                "allows for"
            ),
            "max_routes": 3,
            "stop_after_routes": True,
            "fallback_entry": _knowledge_path("AGENTS.md", view),
        },
        "execution_budget": execution_budget,
        "untested_public_api": untested,
        "first_review_checklist": list(DIRECT_REVIEW_CHECKLIST),
        "progress_update": {
            **DIRECT_PROGRESS_UPDATE,
            "required_fields": list(
                DIRECT_PROGRESS_UPDATE["required_fields"]
            ),
        },
        "completion_gate": {
            "tool": "validate_direct_review",
            "evidence_head_sha": (
                "Required: the frozen head commit SHA every cited source file "
                "and validation result was read at; fetch the PR head ref when "
                "the local checkout holds another revision."
            ),
            "existing_feedback_status": {
                "checked": "PR feedback was fetched after independent source verification and every candidate was classified.",
                "disabled": "PR_CONTEXT_MODE=no_discussion explicitly disabled feedback for evaluation.",
                "unavailable": "PR feedback could not be fetched; report this validation gap.",
                "not_applicable": "The target is a local/worktree review without a PR.",
            },
            "finding_dispositions": (
                "For checked PR reviews: [{anchor, disposition, "
                "existing_thread?, head_recheck?}] where disposition is new, "
                "duplicate, extends_existing, or resolved_or_outdated; "
                "resolved/outdated items require head_recheck=fixed or "
                "still_affected."
            ),
            "subtraction_signal": {
                "none": "No helper/class/fallback/compatibility/public-behavior expansion; no subtraction evidence required.",
                "triggered": "Require subtraction items or minimality_proof.",
            },
            "triggered_require_one_of": [
                "subtraction[{anchor, action, risk}]",
                "minimality_proof{scope_ledger, abstraction_census, why_no_safe_deletion}",
            ],
            "final_comment_count": 1,
            "if_missing": "partial_review",
        },
        "diagnostics": {
            "knowledge_snapshot": view.public_snapshot,
            "timing_ms": {
                "routing": route_ms,
                "related_knowledge": related_ms,
                "execution_budget": budget_ms,
                "total": int((time.perf_counter() - started) * 1000),
            }
        },
    }
    from .knowledge_service.containment import configuration, _issue_usage, digest
    if configuration()["enabled"]:
        from secrets import token_hex
        # Raw MCP callers receive paths rather than SDK document references.
        # Bind their selected resources to private provider issuance as well.
        paths = {plan["knowledge_entry"], *mandatory_review_guides,
                 *(route["path"] for route in knowledge_routes),
                 *(document["path"] for document in related["documents"])}
        context = {"knowledge_snapshot": view.public_snapshot,
                   "knowledge_tree_sha256": view.tree_sha256,
                   "plan_sha256": digest(plan),
                   "session_id": token_hex(32),
                   "documents": [{"document_id": view.relative(view.root / path)}
                                 for path in sorted(paths)]}
        plan["knowledge_usage"] = _issue_usage(context, view=view, session_id=context["session_id"])
        plan["completion_gate"]["knowledge_usage"] = (
            "Pass this plan's knowledge_usage unchanged to doc_read, doc_search, and validate_direct_review; "
            "the provider rechecks its issued resources against current containment."
        )
    return plan


def _direct_untested_public_api(repo: str, diff: str) -> dict:
    """Candidates from the diff alone: the provider holds no checkout of the
    PR head, so the head-tree test search is the agent's (see checklist)."""
    if not diff:
        return {"status": "no_diff"}
    adapter = _adapter_for_repo(repo)
    rules = UTCoverageRules.from_manifest(
        adapter.manifest if adapter is not None else None)
    if rules is None:
        return {"status": "disabled"}
    report = _ut_analyze(diff, rules)
    return {"status": "ok", **report.to_dict(),
            "instructions": _UT_INSTRUCTIONS if report.candidates else ""}


def _direct_completion_result(
    subtraction_signal: str = "",
    subtraction: list[dict[str, str]] | None = None,
    minimality_proof: dict[str, str] | None = None,
    final_comment_count: int = 1,
    evidence_head_sha: str = "",
    existing_feedback_status: str = "not_applicable",
    finding_dispositions: list[dict[str, str]] | None = None,
    knowledge_usage: dict | None = None,
) -> dict:
    """Mechanically gate Direct completion on subtraction classification.

    This deliberately checks structure, not whether the review evidence is
    true. A diff without a subtraction trigger can finish after explicitly
    declaring ``none``. Triggered diffs still need actionable subtraction or
    concrete evidence that the inspected scope is already minimal.
    ``evidence_head_sha`` forces an explicit declaration that every cited
    source file and validation result was read at the frozen head commit,
    not at whatever revision the local working tree happened to hold.
    """
    subtraction_signal = str(subtraction_signal).strip().casefold()
    subtraction = subtraction or []
    minimality_proof = minimality_proof or {}
    evidence_head_sha = str(evidence_head_sha).strip().casefold()
    existing_feedback_status = str(existing_feedback_status).strip().casefold()
    finding_dispositions = finding_dispositions or []
    missing: list[str] = []
    from .knowledge_service.containment import knowledge_availability_check
    availability = knowledge_availability_check(knowledge_usage)
    if not availability["allowed"]:
        missing.append("knowledge_usage requires current, admissible provider-issued provenance")

    if final_comment_count != 1:
        missing.append("final_comment_count must be exactly 1")

    if subtraction_signal not in _SUBTRACTION_SIGNALS:
        missing.append("subtraction_signal must be 'none' or 'triggered'")

    if not _EVIDENCE_HEAD_SHA.fullmatch(evidence_head_sha):
        missing.append(
            "evidence_head_sha must be the frozen head commit SHA "
            "(7-40 hex characters) that every cited source file and "
            "validation result was read at"
        )

    if existing_feedback_status not in _FEEDBACK_STATUSES:
        missing.append(
            "existing_feedback_status must be 'checked', 'disabled', "
            "'unavailable', or 'not_applicable'"
        )

    from .review.review_result import disposition, has_minimality_proof, valid_subtraction
    malformed_dispositions = []
    for index, item in enumerate(finding_dispositions):
        try:
            disposition(item)
        except ValueError:
            malformed_dispositions.append(index)
    if malformed_dispositions:
        missing.append(
            "each finding disposition needs a path:line anchor, a valid "
            "new/duplicate/extends_existing/resolved_or_outdated disposition, "
            "existing_thread for every non-new item, and head_recheck=fixed "
            "or still_affected for resolved_or_outdated items "
            f"(invalid indexes: {malformed_dispositions})"
        )
    if existing_feedback_status != "checked" and finding_dispositions:
        missing.append(
            "finding_dispositions require existing_feedback_status='checked'"
        )

    malformed_subtractions = [index for index, item in enumerate(subtraction) if not valid_subtraction(item)]
    if malformed_subtractions:
        missing.append(
            "each subtraction item needs a path:line anchor, a "
            "DELETE/DEFER/INLINE/MERGE/MOVE action, and a non-empty risk "
            f"(invalid indexes: {malformed_subtractions})"
        )

    has_subtraction = bool(subtraction) and not malformed_subtractions
    has_minimality_proof = has_minimality_proof(minimality_proof)
    if (
        subtraction_signal == "none"
        and (subtraction or minimality_proof)
    ):
        missing.append(
            "subtraction_signal 'none' cannot include subtraction evidence"
        )
    elif (
        subtraction_signal == "triggered"
        and not has_subtraction
        and not has_minimality_proof
    ):
        missing.append(
            "triggered subtraction requires subtraction items or a concrete "
            "minimality_proof with "
            "scope_ledger, abstraction_census, and why_no_safe_deletion"
        )

    complete = not missing
    result = {
        "status": "complete" if complete else "partial_review",
        "publish_ready": complete,
        "final_comment_count": final_comment_count,
        "evidence_head_sha": evidence_head_sha,
        "subtraction_signal": subtraction_signal,
        "subtraction_required": subtraction_signal == "triggered",
        "subtraction_items": len(subtraction),
        "minimality_proof": has_minimality_proof,
        "existing_feedback_status": existing_feedback_status,
        "finding_dispositions": len(finding_dispositions),
        "duplicate_findings_suppressed": sum(
            str(item.get("disposition", "")).strip().casefold() == "duplicate" or (
                str(item.get("disposition", "")).strip().casefold() == "resolved_or_outdated"
                and str(item.get("head_recheck", "")).strip().casefold() == "fixed")
            for item in finding_dispositions if isinstance(item, dict)),
        "resolved_or_outdated_still_affected": sum(
            str(item.get("disposition", "")).strip().casefold() == "resolved_or_outdated"
            and str(item.get("head_recheck", "")).strip().casefold() == "still_affected"
            for item in finding_dispositions if isinstance(item, dict)),
        "missing": missing,
        "next_action": (
            "Return the single consolidated review comment with duplicate findings suppressed."
            if complete
            else "Classify the subtraction signal; only a triggered diff needs one bounded subtraction pass using the existing evidence packet."
        ),
    }
    if availability["enabled"]:
        result["knowledge_availability"] = availability
        if not availability["allowed"]:
            result["next_action"] = "Retrieve admissible knowledge and reassess the review before completion."
    return result


# ── public names ──────────────────────────────────────────────────────────────
# What `contract.py` re-exports. The underscored definitions above are the
# implementation and are kept only so `thin_mcp_server`'s existing call sites and
# tests keep working; new callers use these.
direct_knowledge_routes = _direct_knowledge_routes
direct_execution_budget = _direct_execution_budget
direct_completion_result = _direct_completion_result
direct_mandatory_review_guides = _direct_mandatory_review_guides
