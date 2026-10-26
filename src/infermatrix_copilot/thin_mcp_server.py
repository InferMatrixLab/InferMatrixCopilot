"""Default MCP: Direct knowledge access plus the Strict review workflow."""

from __future__ import annotations

import re
import signal
import sys
import time

from .config import Settings

# Direct's routing tables and mechanism moved to `direct_routing`, which
# `contract` re-exports as the public surface. These private aliases keep this
# module's own call sites unchanged and delegate DOWN — nothing imports back up
# into a server module.
from .direct_routing import (  # noqa: F401 — aliases kept for existing importers
    _ROOT,
    _adapter_changed_file_routes,
    _adapter_for_repo,
    _direct_completion_result,
    _direct_execution_budget,
    _direct_knowledge_routes,
    _direct_mandatory_review_guides,
    _direct_quick_map,
    _direct_route,
    _knowledge_path,
    _normalize_repo,
    direct_review_plan,
)
from .knowledge_view import KnowledgeView
from .intent import resolve_repo_alias
from .knowledge_docs import KnowledgeDocs, KnowledgeDocsError
from .mcp_policy import PolicyError
from .mcp_server import CopilotMCP

_PR_URL = re.compile(
    r"https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)",
    re.IGNORECASE,
)
_PR_NUMBER = re.compile(r"^(?:pr\s*#?\s*)?(\d+)$", re.IGNORECASE)
def __getattr__(name: str):
    # formerly an import-time constant; now the knowledge root of the current view
    if name == "_KNOWLEDGE":
        return KnowledgeView.current().root
    raise AttributeError(name)


def _supported_repos(view: KnowledgeView | None = None) -> list[str]:
    knowledge = (view or KnowledgeView.current()).root
    if not (knowledge / "repos").is_dir():
        return []
    return sorted(
        path.name for path in (knowledge / "repos").iterdir()
        if path.is_dir() and (path / "_index.md").is_file()
    )


def _docs(repo: str, view: KnowledgeView | None = None) -> KnowledgeDocs:
    view = view or KnowledgeView.current()
    repo = _normalize_repo(repo, view)
    repo_dir = view.root / "repos" / repo
    if not repo_dir.is_dir():
        raise KnowledgeDocsError(f"unsupported knowledge repo: {repo}")
    return KnowledgeDocs(view.root, f"repos/{repo}", verify=view.path)


def _guard(fn):
    try:
        return fn()
    except (KnowledgeDocsError, FileNotFoundError, PolicyError, TypeError,
            ValueError) as exc:
        return {"error": str(exc)}


def _model_context_delivery(packet: dict) -> dict:
    """Raw MCP responses are the host model's actual context delivery."""
    from .knowledge_service.containment import configuration, knowledge_usage_record
    if configuration()["enabled"] and packet.get("model_content"):
        return {**packet, "knowledge_usage": knowledge_usage_record(packet, injected=True)}
    return packet


def _raw_document_delivery(result, receipt, session, view):
    if session is None:
        return result
    from .knowledge_service.containment import _issue_usage
    if result.get("path") or result.get("matches"):
        receipt = _issue_usage({"session_id":session,"delivery":result}, view=view, session_id=session, injected=True)
    return {**result, "knowledge_usage": receipt}


def _legacy_plan_delivery(plan):
    from .knowledge_service.containment import configuration, _usage_session_view, _issue_usage, digest
    if not configuration()["enabled"]:
        return plan
    session, view = _usage_session_view(plan["knowledge_usage"])
    context = {"raw_plan_sha256":digest(plan),
               "documents":[{"document_id":row["path"]} for row in plan["knowledge_usage"]["pages"]],
               "quick_maps":[{"path":view.relative(row["path"]),"content":row.get("quick_map","")}
                             for row in plan["knowledge_routes"]],
               "related_knowledge":plan["related_knowledge"]}
    return {**plan,"knowledge_usage":_issue_usage(context,view=view,session_id=session,injected=True)}


def _knowledge_entry(name: str) -> str:
    path = (KnowledgeView.current().root / name).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"knowledge entry is missing: {path}")
    return str(path)


def _contributing_entry() -> str:
    """The knowledge CONTRIBUTION entry point, which lives in `doc/`, not in the
    knowledge tree.

    The authoring rules are documentation and moved to `doc/knowledge/`; the
    knowledge tree is now data only. This resolver therefore deliberately does
    NOT go through `_knowledge_path` — that helper's whole job is to refuse
    paths outside the knowledge root, and this path is legitimately outside it.
    Source checkout first, then the packaged copy beside the wheel's knowledge
    tree, so an installed wheel still answers the tool.
    """
    for candidate in (_ROOT / "doc" / "knowledge" / "CONTRIBUTING.md",
                      KnowledgeView.current().root.parent / "doc" / "knowledge" / "CONTRIBUTING.md"):
        if candidate.is_file():
            return str(candidate.resolve())
    raise FileNotFoundError(
        "knowledge contribution entry is missing: doc/knowledge/CONTRIBUTING.md")


def _review_knowledge(repo: str, changed_files: list[str]) -> list[dict]:
    """Return adapter-backed review knowledge for smoke tests and strict hosts."""
    selected_repo = _normalize_repo(repo)
    docs = _docs(selected_repo)
    routed, unmatched = _adapter_changed_file_routes(selected_repo, changed_files)
    paths = [
        "general/review/_index.md",
        f"repos/{selected_repo}/rules.md",
        f"repos/{selected_repo}/_index.md",
    ]
    adapter = _adapter_for_repo(selected_repo)
    if adapter is not None:
        knowledge = adapter.manifest.get("knowledge") or {}
        paths.extend(knowledge.get("briefing_docs") or [])
        paths.extend(knowledge.get("briefing_docs_extra") or [])
    paths.extend(str(route["relative_path"]) for route in routed)
    entries = []
    if changed_files:
        lines = ["# Changed-file routing", "", f"repo: {selected_repo}", ""]
        if routed:
            lines.append("## Routed")
            for route in routed:
                lines.append(
                    f"- `{route['changed_file']}` -> {route['owner']} -> "
                    f"`{route['relative_path']}`")
            lines.append("")
        if unmatched:
            lines.append("## Unmatched")
            for path in unmatched:
                lines.append(f"- `{path}`")
            lines.append("")
            lines.append(
                "Unmatched changed paths remain reviewer-visible and must not "
                "be treated as covered.")
        entries.append({
            "path": "changed-file-routing",
            "content": "\n".join(lines).strip(),
            "next_offset": None,
        })
    for path in dict.fromkeys(paths):
        try:
            entries.append(docs.read(path, limit=24_000))
        except FileNotFoundError:
            continue
    return entries


def _strict_review_request(
    target: str,
    repo: str,
    *,
    post: bool,
    review_depth: str,
    settings: Settings,
    expected_head_sha: str = "",
    repo_path: str = "",
    idempotency_key: str = "",
) -> dict:
    """Translate the Strict public surface to the internal review TaskSpec."""
    target = str(target).strip()
    if not target:
        raise ValueError("target must not be empty")
    if not isinstance(post, bool):
        raise TypeError("post must be a boolean")

    match = _PR_URL.search(target)
    if match:
        owner, name, number = match.groups()
        selected_repo = resolve_repo_alias(owner, name, settings)
        selected_repo = selected_repo or _REPO_ALIASES.get(
            f"{owner}/{name}".casefold())
        if selected_repo is None:
            raise ValueError(
                f"repository {owner}/{name} is not configured for Strict mode")
        pr = int(number)
    else:
        match = _PR_NUMBER.fullmatch(target)
        if not match:
            raise ValueError("Strict mode requires a PR URL or number")
        selected_repo = _normalize_repo(repo or settings.default_repo)
        pr = int(match.group(1))

    request = {
        "kind": "pr_review",
        "repo": selected_repo,
        "pr": pr,
        "mode": "eco",
        "post": post,
    }
    if review_depth:
        request["params"] = {"review_depth": review_depth}
    if expected_head_sha:
        # carried as a first-class field, not a param: the policy gate validates
        # it as a full 40-hex sha and refuses anything else
        request["expected_head_sha"] = str(expected_head_sha).strip().lower()
    if repo_path:
        # rides on the request so it is frozen per run, rather than written into
        # process-global settings where a concurrent call would see it
        request["repo_path"] = str(repo_path)
    if idempotency_key:
        request["idempotency_key"] = str(idempotency_key)
    return request


def build_mcp(
    settings: Settings | None = None,
    core: CopilotMCP | None = None,
):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    core = core or CopilotMCP(settings)
    from .review.direct import DirectClient
    context_client = DirectClient(knowledge_context_path=core.settings.run_root / "knowledge-context.sqlite",
                                 allowed_knowledge_repositories=core.settings.allowed_knowledge_repositories)

    # Approval-gating hosts (codex asks per-call approval for tools without
    # hints, and cancels them outright in headless runs) read these
    # annotations, so they must stay truthful: `review` reserves run state
    # and its Strict child reaches the network; everything else only reads.
    read_only = ToolAnnotations(
        readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    mcp = FastMCP(
        "infermatrix-copilot",
        instructions=(
            "Use review in direct mode unless the user explicitly requests "
            "Strict. For Direct, first freeze the PR snapshot and report head "
            "SHA, CI, mergeability, and preliminary findings in the host "
            "conversation. Do not read knowledge or source before that update. "
            "Then call review once with title, body, and changed_files. Direct "
            "returns at most three exact knowledge routes with embedded quick "
            "maps; use those excerpts and do not open full rule pages unless a "
            "concrete ambiguity blocks source review. Follow the returned "
            "execution_budget as a hard ceiling; stop with the supported "
            "verdict at the limit. Extend it once by the returned allowance "
            "only for a stated unresolved P1/high-risk contract. Continue "
            "reviewing without "
            "posting an early GitHub comment. Read every source file cited as "
            "evidence at the frozen head SHA; fetch the PR head ref when the "
            "local checkout holds another revision. Before treating a Direct "
            "review as complete or posting its only final comment, call "
            "validate_direct_review with that evidence_head_sha. "
            "When containment is enabled, pass the plan's knowledge_usage "
            "receipt to doc_read, doc_search, and final validation. "
            "After source findings are independently verified, fetch bounded PR discussion "
            "and thread-aware review feedback, classify every candidate, and "
            "suppress duplicates. Pass existing_feedback_status=checked for "
            "that PR path, disabled only for PR_CONTEXT_MODE=no_discussion "
            "evaluation, unavailable when the fetch fails, or not_applicable "
            "only for local/worktree reviews. Mark "
            "subtraction_signal=none when the diff "
            "does not add or expand a helper, class, fallback, compatibility "
            "branch, or public behavior. Only subtraction_signal=triggered "
            "requires subtraction evidence. "
            "Strict runs the packaged background review workflow with the "
            "configured model, returns a run_id, and must be "
            "polled with get_review_result until terminal. Posting is never "
            "implicit. For Direct, pass post=true only when the user "
            "explicitly asks. Strict never posts and refuses post=true: exactly "
            "one publisher owns a PR's review marker and head gate, so read the "
            "structured result from get_review_result and publish it yourself."
        ),
    )

    @mcp.tool(annotations=ToolAnnotations(
        title="Start a Direct or Strict review",
        readOnlyHint=False, destructiveHint=False, idempotentHint=False,
        openWorldHint=True))
    def review(
        target: str,
        repo: str = "vllm-omni",
        mode: str = "direct",
        post: bool = False,
        review_depth: str = "",
        title: str = "",
        body: str = "",
        changed_files: list[str] | None = None,
        repo_path: str = "",
        expected_head_sha: str = "",
        idempotency_key: str = "",
        diff: str = "",
        knowledge_profile: str = "legacy",
    ) -> dict:
        """Begin a Direct or Strict review.

        For Direct, first collect the frozen PR title, body, and changed files,
        publish the host progress update, then pass that context here. Direct
        returns at most three exact owner/model routes. Title/body normally select
        them and changed files validate scope; if none of the selected routes match
        an owner the changed files imply, the changed files select the routes
        instead and the response says so (``status: scope_fallback``). Any route whose
        ``quick_map_status`` is not ``"ok"`` — ``"unavailable"`` (no embedded map) or
        ``"truncated"`` (only part of one) — must be opened, and the budget grants a
        read for each. Strict runs the packaged PR-review
        workflow and accepts an optional local checkout through ``repo_path``.
        ``expected_head_sha`` (Strict only) pins the review to one snapshot: pass
        the full 40-hex head you observed, and the run stops as stale rather than
        reviewing a different commit if the PR moved in between.
        ``diff`` (Direct only, optional) is the frozen unified diff; with it the
        plan lists the new public functions no test in the diff names
        (``untested_public_api``) for you to judge.
        ``idempotency_key`` (Strict only) makes a retry safe: pass a stable id for
        the attempt and a repeated call returns the SAME run — including a
        finished one, whose result you can read — instead of starting a second
        review. Use a new key for a genuinely new attempt.
        ``post`` applies to Direct only. Strict never publishes: exactly one
        publisher must own a PR's review marker and head gate, so read the
        structured result and post it yourself (``post=true`` with
        ``mode="strict"`` is refused rather than silently ignored).
        """
        def run() -> dict:
            started = time.perf_counter()
            if not str(target).strip():
                raise ValueError("target must not be empty")
            selected_mode = str(mode).strip().casefold() or "direct"
            if selected_mode not in {"direct", "strict"}:
                raise ValueError("mode must be 'direct' or 'strict'")
            if selected_mode == "direct":
                if knowledge_profile == "adaptive":
                    from .contracts.models import ChangedPath, DirectReviewRequest, RepositoryRef
                    request = DirectReviewRequest(idempotency_key or f"direct:{target}", RepositoryRef(repo),
                                                  0, expected_head_sha, title, body,
                                                  tuple(ChangedPath(path) for path in changed_files or ()), diff=diff)
                    return _model_context_delivery(context_client.plan_adaptive(request))
                if knowledge_profile != "legacy":
                    raise ValueError("knowledge_profile must be legacy or adaptive")
                return _legacy_plan_delivery(direct_review_plan(
                    repo,
                    title=title,
                    body=body,
                    changed_files=changed_files,
                    diff=diff,
                ))

            if post:
                # Refused at the surface as well as in the policy, so the
                # caller gets the reason instead of a generic rejection from
                # deeper in the stack. Exactly one publisher must own a PR's
                # review marker and head gate.
                raise ValueError(
                    "strict mode cannot post: read the structured result and "
                    "publish it yourself, or use the CLI with ALLOW_POST=1 for "
                    "a human-driven post")
            strict_started = time.perf_counter()
            request = _strict_review_request(
                target, repo, post=post, review_depth=review_depth,
                settings=core.settings, expected_head_sha=expected_head_sha,
                repo_path=repo_path, idempotency_key=idempotency_key)
            missing = core.strict_readiness(request["repo"], repo_path)
            if missing:
                raise ValueError(
                    "Strict mode is not ready: " + "; ".join(missing)
                )
            return {
                "run_id": core.start_strict_review(request),
                "mode": "strict",
                "execution_mode": "eco",
                "post": request["post"],
                "diagnostics": {
                    "timing_ms": {
                        "start_strict_review": int(
                            (time.perf_counter() - strict_started) * 1000
                        ),
                        "total": int(
                            (time.perf_counter() - started) * 1000
                        ),
                    }
                },
            }

        return _guard(run)

    @mcp.tool(annotations=read_only)
    def validate_direct_review(
        subtraction_signal: str = "",
        subtraction: list[dict[str, str]] | None = None,
        minimality_proof: dict[str, str] | None = None,
        final_comment_count: int = 1,
        evidence_head_sha: str = "",
        existing_feedback_status: str = "",
        finding_dispositions: list[dict[str, str]] | None = None,
        knowledge_usage: dict | None = None,
    ) -> dict:
        """Validate the Direct completion gate before the only final comment.

        Use ``subtraction_signal='none'`` when the diff does not add or expand a
        helper, class, fallback, compatibility branch, or public behavior. Use
        ``'triggered'`` for those diffs, then supply actionable subtraction
        items or concrete minimality evidence. Pass ``evidence_head_sha`` as
        the frozen head commit every cited source file and validation result
        was read at; a review whose evidence came from another revision is not
        complete. A ``partial_review`` result is not a completed Direct review.
        For PR reviews, fetch existing feedback only after independently
        verifying candidate findings, then pass ``existing_feedback_status``
        and classify each candidate in ``finding_dispositions``. Duplicate and
        resolved/outdated findings must identify the existing thread and must
        not be emitted as new comments.
        When containment is enabled, pass the plan's ``knowledge_usage`` receipt
        unchanged; missing or newly held knowledge prevents completion.
        """
        started = time.perf_counter()
        result = _direct_completion_result(
            subtraction_signal=subtraction_signal,
            subtraction=subtraction,
            minimality_proof=minimality_proof,
            final_comment_count=final_comment_count,
            evidence_head_sha=evidence_head_sha,
            existing_feedback_status=existing_feedback_status,
            finding_dispositions=finding_dispositions,
            knowledge_usage=knowledge_usage,
        )
        result.setdefault("diagnostics", {})["timing_ms"] = {
            "validate_direct_review": int(
                (time.perf_counter() - started) * 1000
            )
        }
        return result

    @mcp.tool(annotations=read_only)
    def get_review_result(run_id: str, offset: int = 0) -> dict:
        """Poll a Strict run and page its final report with ``next_offset``."""
        def run() -> dict:
            started = time.perf_counter()
            out = {"mode": "strict", **core.get_result(run_id, offset)}
            out.setdefault("diagnostics", {})["timing_ms"] = {
                "get_review_result": int(
                    (time.perf_counter() - started) * 1000
                )
            }
            return out

        return _guard(run)

    @mcp.tool(annotations=read_only)
    def get_review_status(run_id: str) -> dict:
        """Return a Strict run's durable status and step progress."""
        def run() -> dict:
            started = time.perf_counter()
            out = {"mode": "strict", **core.get_status(run_id)}
            out.setdefault("diagnostics", {})["timing_ms"] = {
                "get_review_status": int(
                    (time.perf_counter() - started) * 1000
                )
            }
            return out

        return _guard(run)

    @mcp.tool(annotations=read_only)
    def update_knowledge(repo: str = "vllm-omni") -> dict:
        """Return the knowledge contribution entrypoint for the host to follow."""
        return _guard(lambda: {"knowledge_entry": _contributing_entry()})

    @mcp.tool(annotations=read_only)
    def doc_search(
        query: str,
        repo: str = "vllm-omni",
        limit: int = 20,
        knowledge_usage: dict | None = None,
    ) -> dict:
        """Search knowledge; enforced reads require the review plan's knowledge_usage receipt."""
        def run() -> dict:
            from .knowledge_service.containment import _usage_session_view
            session, view = _usage_session_view(knowledge_usage)
            selected_repo = _normalize_repo(repo, view)
            if selected_repo not in _supported_repos(view):
                supported = ", ".join(_supported_repos(view)) or "(none)"
                return {
                    "error": (
                        f"unsupported knowledge repo: {repo}. "
                        f"Supported: {supported}."
                    )
                }
            matches = _docs(selected_repo, view).search(query, limit=limit)
            result = {
                "query": query,
                "repo": selected_repo,
                "matches": matches,
                **({"hint": (
                    "No literal text match. For review routing, call review. "
                    "For knowledge edits, call update_knowledge and read its "
                    "knowledge_entry."
                )} if not matches else {}),
            }
            return _raw_document_delivery(result, knowledge_usage, session, view)

        return _guard(run)

    @mcp.tool(annotations=read_only)
    def doc_read(
        path: str,
        repo: str = "vllm-omni",
        offset: int = 0,
        knowledge_usage: dict | None = None,
    ) -> dict:
        """Read a doc_search page; enforced reads require the review plan's knowledge_usage receipt."""
        def run() -> dict:
            from .knowledge_service.containment import _usage_session_view
            session, view = _usage_session_view(knowledge_usage)
            selected_repo = _normalize_repo(repo, view)
            result = {
                "repo": selected_repo,
                **_docs(selected_repo, view).read(path, offset=offset),
            }
            return _raw_document_delivery(result, knowledge_usage, session, view)

        return _guard(run)

    def rfc_call(action: str, payload: dict | None = None) -> dict:
        from .rfc_service.client import RFCClient, RFCClientError

        try:
            return RFCClient.from_env().dispatch(action, payload)
        except RFCClientError as exc:
            return {"error": {"code": exc.code, "message": str(exc)}}

    @mcp.tool(annotations=ToolAnnotations(
        title="Operate a portable RFC workspace", readOnlyHint=False,
        destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def rfc_request(action: str, payload: dict | None = None) -> dict:
        """Invoke an authorized RFC action using the host's configured identity.

        RFC_SERVICE_URL/RFC_STATE_DIR and RFC_TOKEN configure the workspace;
        callers cannot supply an endpoint or credential through this tool.
        Start with rfc_capabilities, repositories.list and rfcs.list. Drafts
        remain private until an explicit rfcs.publish with post=true, the exact
        previewed content_digest and an idempotency_key. Queueing an operation
        does not establish publication: read operations.get to its terminal state.
        Use rfcs.status next_actions for actionable implementation and acceptance
        work; a merged PR alone does not establish feature acceptance.
        Private RFC conversations use chat.create/list/get/send/events/cancel/retry.
        chat.send returns a durable job immediately; poll events or get for the
        validated answer. Review chat.proposals.preview, then explicitly confirm
        chat.proposals.apply with its exact candidate_digest. Existing sources
        are synchronized by a separate operation; unsourced drafts stay local.
        Manual source edits use rfcs.changes.preview then rfcs.changes.apply,
        with the exact preview_id/candidate_digest and a stable idempotency_key.
        service.status reports readiness; repositories.check verifies a connection.
        Chat cannot pass acceptance or waive criteria.
        """
        return rfc_call(action, payload)

    @mcp.tool(annotations=ToolAnnotations(
        title="Read RFC service capabilities", readOnlyHint=True,
        idempotentHint=True, openWorldHint=True))
    def rfc_capabilities() -> dict:
        """Read the configured RFC service's version and supported operations."""
        return rfc_call("capabilities")

    @mcp.tool(annotations=ToolAnnotations(
        title="Read RFC progress and next actions", readOnlyHint=True,
        idempotentHint=True, openWorldHint=True))
    def rfc_status(rfc_id: str) -> dict:
        """Read cached evidence, acceptance, freshness and next actions for an RFC."""
        return rfc_call("rfcs.status", {"rfc_id": rfc_id})
    @mcp.tool(annotations=read_only)
    def open_knowledge_context(repo: str, source_pin: str, review_id: str,
                               model_context_tokens: int = 131072,
                               source_reserve_tokens: int = 32000,
                               output_reserve_tokens: int = 8192) -> dict:
        """Open an explicit adaptive 24k/64k session; supplied model capacity is configuration, not detected usage."""
        from .contracts.models import DirectReviewRequest, RepositoryRef
        from .knowledge_context import ContextBudget
        def run():
            request = DirectReviewRequest(review_id, RepositoryRef(repo), 0, source_pin, "", "", ())
            return context_client.open_knowledge_context(request, budget=ContextBudget(
                model_context_tokens=model_context_tokens, source_reserve_tokens=source_reserve_tokens,
                output_reserve_tokens=output_reserve_tokens))
        return _guard(run)

    @mcp.tool(annotations=read_only)
    def read_knowledge_context(session_id: str, path: str, offset: int = 0, repository: str | None = None) -> dict:
        """Read source-pinned knowledge using the session's remaining cumulative budget."""
        return _guard(lambda: _model_context_delivery(context_client.read_knowledge_context(session_id, path, offset=offset, repository=repository)))

    @mcp.tool(annotations=read_only)
    def search_knowledge_context(session_id: str, query: str, repository: str | None = None) -> dict:
        """Search snippets consume the same budget as full knowledge reads."""
        return _guard(lambda: _model_context_delivery(context_client.search_knowledge_context(session_id, query, repository=repository)))

    @mcp.tool(annotations=read_only)
    def related_knowledge_context(session_id: str, changed_files: list[str], query: str = "", repository: str | None = None) -> dict:
        """Select additional intact feature contexts within the shared remaining budget."""
        return _guard(lambda: _model_context_delivery(context_client.related_knowledge_context(session_id, changed_files, query=query, repository=repository)))

    @mcp.tool(annotations=read_only)
    def expand_knowledge_context(session_id: str, target_tokens: int, reason: str) -> dict:
        """Request more context, with a concrete unresolved contract; never consumes reserved source/output capacity."""
        return _guard(lambda: _model_context_delivery(context_client.expand_knowledge_context(session_id, target_tokens=target_tokens, reason=reason)))

    return mcp


def main() -> int:
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        mcp = build_mcp()
    except ImportError:
        sys.stderr.write(
            "infermatrix-copilot-mcp needs the MCP SDK. Install with "
            "pip install -e '.[mcp]'\n"
        )
        return 1
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
