"""Repo-scoped knowledge for agent steps: adapter resolution, skill/debug-memory
retrieval, and the read-only tools an agent uses to pull more on demand.

Retrieval is scoped per repo — the active adapter's own store ranks before the
shared pool, and skill-update *candidates* land in the repo's namespace (agents
never edit active skills). `_repo_map_tool` exposes goal-ranked structure as a
tool (design §V2.3.4 channel 3: structure is pulled, never injected wholesale).
"""

from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from ...memory.debug_memory import DebugMemory
from ...memory.skills import SkillStore
from ...knowledge_docs import KnowledgeDocs, KnowledgeDocsError
from ...tools import ToolDef
from ..step import StepContext


def _resolve_adapter(ctx: StepContext):
    """The active repo's adapter, when one is registered (never raises)."""
    from ...adapters.base import AdapterRegistry

    registry = AdapterRegistry(ctx.settings.adapters_dir)
    spec = ctx.state.get("task_spec") or {}
    try:
        adapter = registry.resolve(repo_path=ctx.state.get("repo_path") or None)
        if adapter is None and spec.get("repo"):
            adapter = registry.resolve(name=str(spec["repo"]).replace("-", "_"))
        return adapter
    except Exception:
        return None


class _ScopedKnowledge:
    """Skill retrieval scoped per repo: runtime (learned) first, the repo
    adapter's seed store second, the shared pool last. WRITES are
    runtime-side ONLY (Rev 8 §10, adapter tree read-only at run time —
    PR-boundary F3): proposals land in the repo's runtime candidates, and
    seed-skill usage appends to the runtime usage journal instead of
    rewriting seed `SKILL.md` frontmatter in the checked-in tree."""

    def __init__(self, stores: list[SkillStore], *,
                 runtime_store: SkillStore | None = None,
                 usage_journal: Path | None = None):
        """`stores` is the ordered READ list (runtime first when present);
        `runtime_store` is the only write target; `usage_journal` takes
        seed-usage records."""
        self.stores = stores
        self.runtime_store = runtime_store or stores[0]
        self.usage_journal = usage_journal

    def find(self, query: str = "", module: str = "", k: int = 3):
        """Search the stores in priority order and return up to `k` skills,
        deduped by name (first store wins on a tie, so a learned runtime
        skill outranks a same-named seed one). `query`/`module` are passed
        through to each store's own ranking, with the runtime usage
        journal's counts layered onto the frozen seed frontmatter counts —
        the usage prior the journal write-side feeds (round-2 F8)."""
        from ...memory.skills import read_usage_counts

        extra = read_usage_counts(self.usage_journal) \
            if self.usage_journal is not None else {}
        out, seen = [], set()
        for store in self.stores:
            for s in store.find(query=query, module=module, k=k,
                                extra_run_counts=extra):
                if s.name not in seen:
                    seen.add(s.name)
                    out.append(s)
        return out[:k]

    def propose(self, **kwargs) -> None:
        """Record a skill-update candidate in the RUNTIME store — proposals
        never leak across repos and never touch the adapter tree."""
        self.runtime_store.propose(**kwargs)

    def touch(self, name: str) -> None:
        """Record one use of skill `name`: a runtime-owned skill is touched
        in place (runtime state, outside the adapter tree); a seed skill's
        usage appends to the runtime usage journal — seed files stay
        byte-identical at run time."""
        if self.runtime_store.touch(name):
            return
        if self.usage_journal is not None:
            from ...memory.skills import append_usage

            append_usage(self.usage_journal, name)


def _knowledge_stores(ctx: StepContext) -> _ScopedKnowledge:
    """Build the per-repo `_ScopedKnowledge`: the repo's RUNTIME store
    first (learned knowledge wins collisions), the adapter seed store
    second, the shared pool last — writes runtime-side only."""
    from ...memory.paths import KnowledgePaths

    adapter = _resolve_adapter(ctx)
    repo = str((ctx.state.get("task_spec") or {}).get("repo", ""))
    kp = KnowledgePaths.resolve(
        ctx.settings, repo,
        adapter_root=adapter.root if adapter is not None else None)
    runtime_store = SkillStore(kp.skills_runtime_dir)
    stores: list[SkillStore] = [runtime_store]
    if adapter is not None and adapter.skills_dir != Path(ctx.settings.skills_dir):
        stores.append(SkillStore(adapter.skills_dir))
    stores.append(SkillStore(ctx.settings.skills_dir))
    return _ScopedKnowledge(stores, runtime_store=runtime_store,
                            usage_journal=kp.skills_usage_journal)


def _retrieve_skills(ctx: StepContext, query: str) -> tuple[list[dict], "_ScopedKnowledge"]:
    """Pre-retrieve the top-k skills for `query` to seed the dispatch context.
    Returns light `{name, summary}` dicts (summary falls back to the skill body
    head when it has no description) plus the live `_ScopedKnowledge` store, so
    the caller can also hand the agent its on-demand `skill_search` tool."""
    store = _knowledge_stores(ctx)
    hits = store.find(query=query, k=ctx.settings.skills_top_k)
    summaries = [{"name": s.name, "summary": s.description or s.body[:200]}
                 for s in hits]
    return summaries, store


def _retrieve_memories(ctx: StepContext, query: str) -> list[str]:
    """Search the debug-memory stores for `query` and return up to 3 one-line
    `[module] symptom -> fix` hits. The repo adapter's DB is searched before the
    shared one so repo-scoped memories rank first; duplicate lines are dropped
    and any DB error is swallowed (retrieval never fails a step)."""
    from ...memory.paths import KnowledgePaths

    adapter = _resolve_adapter(ctx)
    repo = str((ctx.state.get("task_spec") or {}).get("repo", ""))
    dbs = [Path(p) for p in KnowledgePaths.resolve(
        ctx.settings, repo,
        adapter_root=adapter.root if adapter is not None else None,
    ).debug_read_layers]
    hits: list[str] = []
    seen: set[str] = set()
    for db in dbs:  # repo-scoped memories rank before the shared pool's
        try:
            if not db.exists():
                continue
            for h in DebugMemory(db).search(query, k=3):
                line = f"[{h['module']}] {h['symptom']} -> {h['fix_summary']}"
                if line not in seen:
                    seen.add(line)
                    hits.append(line)
        except Exception:
            continue
    return hits[:3]


def _knowledge_tools(store: "_ScopedKnowledge", ctx: StepContext) -> dict[str, ToolDef]:
    """Read-only knowledge tools + governed skill-candidate proposals."""

    def skill_search(query: str, **_: Any) -> str:
        """Tool: return up to 5 matching skills rendered as name + description +
        body head, or a `(no matching skills)` sentinel."""
        hits = store.find(query=query, k=5)
        if not hits:
            return "(no matching skills)"
        return "\n\n".join(f"# {s.name}\n{s.description}\n{s.body[:1500]}"
                           for s in hits)

    def memory_search(query: str, **_: Any) -> str:
        """Tool: return the debug-memory hits for `query` as newline-joined
        lines, or a `(no matching debug memories)` sentinel."""
        hits = _retrieve_memories(ctx, query)
        return "\n".join(hits) or "(no matching debug memories)"

    def skill_update_candidate(name: str, description: str, body: str,
                               **_: Any) -> str:
        """Tool: record a proposed skill as a curator-gated candidate (active
        skills are never edited by an agent), trace it, and confirm to the
        agent."""
        store.propose(name=name, description=description, body=body)
        ctx.trace.record("skill_candidate_proposed", name=name)
        return (f"candidate '{name}' recorded for curator review — active skills "
                "are never modified directly")

    s = {"type": "string"}
    return {
        "skill_search": ToolDef("skill_search", "Search procedural skills.",
                                {"type": "object", "properties": {"query": s},
                                 "required": ["query"]}, skill_search),
        "memory_search": ToolDef("memory_search", "Search debug memories.",
                                 {"type": "object", "properties": {"query": s},
                                  "required": ["query"]}, memory_search),
        "skill_update_candidate": ToolDef(
            "skill_update_candidate",
            "Propose a new/updated skill (candidate only; curator-gated).",
            {"type": "object", "properties": {"name": s, "description": s,
                                              "body": s},
             "required": ["name", "description", "body"]}, skill_update_candidate,
            internal_write=True),
    }


def _repo_map_tool(ctx: StepContext, adapter) -> dict[str, ToolDef]:
    """`repo_map` — goal-ranked, budgeted structure queries (design §V2.3.4
    channel 3: structure is pulled on demand, never injected as an overview)."""
    from ...profiles.repo_map import RepoMap

    repo = ctx.state.get("repo_path") or (adapter.repo_path if adapter else "")
    if not repo or not Path(repo).exists():
        return {}
    from ..agent_policy import agent_policy

    policy = agent_policy(ctx)
    language = str(adapter.manifest.get("repo", {}).get("language") or "python") if adapter else "python"
    cache_dir = ctx.run_dir / "repo_map"
    if policy.repo_map_cache is not None:
        cache_dir = policy.repo_map_cache(ctx)
    elif adapter is not None:
        from ...memory.paths import KnowledgePaths

        cache_dir = KnowledgePaths.resolve(
            ctx.settings, str((ctx.state.get("task_spec") or {}).get("repo", "")),
            adapter_root=adapter.root).repo_map_cache_dir
    accept = None
    if policy.repo_map_accept is not None:
        accept = lambda resolved: policy.repo_map_accept(repo, resolved)
    rmap = RepoMap(repo, language, cache_dir=cache_dir, accept=accept)
    if not rmap.supported:
        ctx.trace.record("capability_gap", capability=f"repo_map.{language}",
                         step="agent_runtime",
                         effect="repo_map tool unavailable; agent uses grep")
        return {}

    def repo_map(query: str, **_: Any) -> str:
        """Tool: render the goal-ranked, budgeted repo map for `query`."""
        return rmap.render(str(query))

    return {"repo_map": ToolDef(
        "repo_map",
        "Goal-ranked map of the repo's files and symbol signatures for a "
        "query (budgeted). Use it to LOCATE where something lives before "
        "reading files; it is not a substitute for reading them.",
        {"type": "object", "properties": {"query": {"type": "string"}},
         "required": ["query"]}, repo_map)}


def _repo_docs_tool(ctx: StepContext, adapter) -> dict[str, ToolDef]:
    """Shared read/search tools plus bounded implementation context.

    General docs are readable; related prose is selected only from the active
    repository. Review dispatch injects it before the first model call, and
    agents can retrieve additional context on demand without writing the KB.
    """
    kdir = Path(ctx.settings.knowledge_dir)  # shared base (general/ + repos/)
    if not kdir.exists():
        return {}
    repo_subdir = None
    if adapter is not None:
        repo_subdir = (adapter.manifest.get("knowledge") or {}).get("repo_subdir")
    from ...knowledge_view import KnowledgeView
    from ...kb_service.repo_spec import resolve_snapshot_repo
    view = KnowledgeView.for_root(kdir)
    state = getattr(ctx, "state", {})
    params = getattr(ctx, "params", {})
    selector = str((state.get("task_spec") or {}).get("repo") or "")
    binding = resolve_snapshot_repo(view, selector) if selector else None
    if binding is not None:
        repo_subdir = binding.knowledge_slice
    docs = KnowledgeDocs(kdir, repo_subdir, verify=view.path)
    profile = params.get("knowledge_context_profile", "adaptive" if (kdir / "_repositories.yaml").is_file() else "legacy")
    adaptive = None
    session_id = ""
    if profile == "adaptive" and binding is not None:
        from ...knowledge_context import ContextBudget, KnowledgeContextService
        adaptive = KnowledgeContextService(view, ctx.run_dir / "knowledge-context.sqlite",
                    allowed_repositories=getattr(ctx.settings, "allowed_knowledge_repositories", ()))
        head = str(state.get("pr_head_sha") or (state.get("task_spec") or {}).get("expected_head_sha") or "")
        budget = ContextBudget(**dict(params.get("knowledge_context_budget") or {}))
        session_id = adaptive.open_session(selector, source_pin=head,
                                          review_id=ctx.run_dir.name, budget=budget)["session_id"]

    def doc_search(query: str, repository: str | None = None, **_: Any) -> str:
        """Tool: grep the knowledge base's markdown for `query`; return
        knowledge-relative `path:line:text` matches (capped), or a sentinel."""
        try:
            if adaptive is not None:
                return json.dumps(adaptive.search(session_id, query, repository=repository), ensure_ascii=False)
            hits = docs.search(query)
        except KnowledgeDocsError as exc:
            return f"refused: {exc}"
        return "\n".join(
            f"{h['path']}:{h['line']}:{h['text']}" for h in hits
        ) or "(no matching docs)"

    def doc_read(path: str, offset: int = 0, repository: str | None = None, **_: Any) -> str:
        """Tool: read a knowledge-base doc by its knowledge-relative path
        (windowed 24k chars; page with offset). Refuses paths escaping the base."""
        try:
            if adaptive is not None:
                return json.dumps(adaptive.read(session_id, path, offset=offset, repository=repository), ensure_ascii=False)
            page = docs.read(path, offset=offset)
        except FileNotFoundError:
            return f"(no such doc: {path})"
        except KnowledgeDocsError as exc:
            return f"refused: {exc}"
        window = page["content"]
        if page["next_offset"] is not None:
            window += (f"\n...[truncated — call doc_read again with "
                       f"offset={page['next_offset']}]")
        return window

    def doc_related(changed_files: list[str], query: str = "", repository: str | None = None, **_: Any) -> str:
        """The same bounded implementation context used by Direct plans."""
        try:
            if adaptive is not None:
                packet = adaptive.related(session_id, changed_files, query=query, repository=repository)
                ctx.trace.record("review_knowledge_injected", session_id=session_id,
                                 model_content_sha256=packet["model_content_sha256"],
                                 model_content=packet["model_content"], documents=packet["documents"],
                                 consumed_tokens=packet["consumed_tokens"], token_accounting=packet["token_accounting"])
                return json.dumps(packet, ensure_ascii=False)
            return json.dumps(docs.related(changed_files, query=query), ensure_ascii=False)
        except KnowledgeDocsError as exc:
            return json.dumps({"status": "refused", "reason": str(exc), "documents": []})

    s = {"type": "string"}
    tools = {
        "doc_related": ToolDef(
            "doc_related", "Retrieve relevant implementation explanations within the active context budget. "
                           "Includes pinned sources and missing facets; verify against the PR head.",
            {"type": "object", "properties": {"changed_files": {"type": "array", "items": s}, "query": s, "repository": s},
             "required": ["changed_files"]}, doc_related),
        "doc_search": ToolDef(
            "doc_search",
            "Search the repo's curated knowledge base (community docs) for a "
            "string; returns knowledge-relative path:line matches to open with "
            "doc_read.",
            {"type": "object", "properties": {"query": s, "repository": s}, "required": ["query"]},
            doc_search),
        "doc_read": ToolDef(
            "doc_read",
            "Read a doc from the curated knowledge base by its knowledge-relative "
            "path (e.g. `repos/<repo>/rules.md`, or a `general/...` guide it "
            "links to). Windowed; page with offset.",
            {"type": "object", "properties": {"path": s, "repository": s,
                                              "offset": {"type": "integer"}},
             "required": ["path"]}, doc_read),
    }
    if adaptive is not None:
        def context_briefing(**_: Any) -> str:
            paths = list(ctx.settings.knowledge_general_docs or [])
            if adapter is not None:
                paths += list((adapter.manifest.get("knowledge") or {}).get("briefing_docs") or [])
            rows = [{"path": path} for path in dict.fromkeys(paths) if (view.root / path).is_file()]
            return json.dumps(adaptive.inject(session_id, rows, purpose="general-and-owner-briefing"), ensure_ascii=False)

        def context_expand(target_tokens: int, reason: str, **_: Any) -> str:
            return json.dumps(adaptive.expand(session_id, target_tokens=target_tokens, reason=reason), ensure_ascii=False)

        tools["doc_context_briefing"] = ToolDef("doc_context_briefing", "Budget the pinned general/owner briefing.",
                                               {"type": "object", "properties": {}}, context_briefing)
        tools["doc_context_expand"] = ToolDef("doc_context_expand", "Expand cumulative knowledge context within model reservations, with an unresolved contract as reason.",
            {"type": "object", "properties": {"target_tokens": {"type": "integer"}, "reason": {"type": "string"}},
             "required": ["target_tokens", "reason"]}, context_expand)
    return tools
