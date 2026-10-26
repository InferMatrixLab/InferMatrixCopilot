"""Scoped-tool MCP bridge for harness backends (doc/features/provider-registry.md).

Entry: ``python -m infermatrix_copilot.tool_bridge --spec <bridge_spec.json>``
— an stdio MCP server a harness session launches from its MCP config. It
exposes the run's builtin tools plus knowledge doc search/read, and every
builtin call passes ``tools.dispatch`` with the step's deserialized
`ToolScope`, so refusals, relative-path resolution against the PR-time
worktree, and result bounds behave identically to the in-process loop.

Two things are deliberately STRONGER than the in-process loop:

- **Read containment.** `ToolScope` path-guards only writes; a harness is a
  less-trusted caller holding an untrusted PR diff, so the bridge refuses
  read/list/grep targets outside the containment roots (scope root + run
  dir) — the `.env`-exfiltration guard.
- **Separate trace file.** Tool events append to ``bridge_trace.jsonl``
  next to the spec; a second process must not interleave with the parent's
  ``run_trace.jsonl``.

What a harness session gets: the run's builtin tools, `doc_search`/`doc_read`,
the on-demand `repo_map` (reconstructed here via `_repo_map_tool`; a failure
degrades to a traced `capability_gap`, never a crash), and the read-only
change-archaeology set (`diff_stat`, `file_at_base`, `show_commit`,
`search_history`, `calc`).

Known gap, disclosed: **skill/memory retrieval is deliberately NOT bridged.**
Those tools can propose knowledge candidates, and opening a cross-process
write path for them was declined — a harness session may read this repo's
knowledge, never add to it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from .run_trace import RunTrace
from . import trace_store
from .scopes import PathScope, ToolScope
from .tools import _PATH_ARGS, TOOLS, dispatch

_SPEC_VERSION = 2


# -- spec serialization ------------------------------------------------------
def write_bridge_spec(*, run_dir: Path, step_name: str, scope: ToolScope,
                      repo: str, rebase: dict | None = None) -> Path:
    """Serialize one step's tool surface under ``<run_dir>/bridge/``. The
    filename is sanitized from the step name (ensemble steps carry ``#``)."""
    bridge_dir = Path(run_dir) / "bridge"
    bridge_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", step_name or "step") or "step"
    path = bridge_dir / f"{stem}.json"
    ps = scope.path_scope
    path.write_text(json.dumps({
        "version": _SPEC_VERSION,
        "scope": {
            "name": scope.name,
            "allowed_tools": sorted(scope.allowed_tools),
            "read_only": scope.read_only,
            "root": scope.root,
            "path_scope": {"writable": list(ps.writable),
                           "primary": list(ps.primary)} if ps else None,
            # the shadow fences travel with the scope: a bridge that restored
            # permissive defaults would let a harness read the source object
            # store and call undeclared extras (design §8.2)
            "read_roots": list(scope.read_roots),
            "deny_prefixes": list(scope.deny_prefixes),
            "strict_extras": bool(scope.strict_extras),
            "executables": list(scope.executables),
        },
        "repo": repo,
        "run_dir": str(run_dir),
        "trace_path": str(Path(run_dir) / "bridge_trace.jsonl"),
        # trace/1 capture continuity: the bridge is a separate process the
        # harness CLI launches, so the parent's bound store and unit context
        # travel in the spec (paths and context fields only, never secrets)
        "trace_store_root": str(trace_store.current_store().root)
        if trace_store.current_store() is not None else "",
        "trace_context": trace_store.current_context(),
        # Optional rebase surface (see `_rebase_extra`). Carries PATHS and
        # model identity only: no api_key and no child env, so nothing
        # secret lands in this file — the bridge process reads credentials
        # from its own environment.
        **({"rebase": rebase} if rebase else {}),
    }, indent=2), encoding="utf-8")
    return path


def load_bridge_spec(path: Path) -> tuple[ToolScope, dict]:
    """Deserialize a spec back into a `ToolScope` + the raw dict."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version") != _SPEC_VERSION:
        raise ValueError(f"unsupported bridge spec version: {data.get('version')!r}")
    s = data["scope"]
    ps = s.get("path_scope")
    scope = ToolScope(
        name=str(s["name"]),
        allowed_tools=frozenset(s["allowed_tools"]),
        path_scope=PathScope(writable=tuple(ps["writable"]),
                             primary=tuple(ps["primary"])) if ps else None,
        read_only=bool(s["read_only"]),
        root=str(s.get("root") or ""),
        read_roots=tuple(str(r) for r in s.get("read_roots") or ()),
        deny_prefixes=tuple(str(r) for r in s.get("deny_prefixes") or ()),
        strict_extras=bool(s.get("strict_extras", False)),
        executables=tuple(str(e) for e in s.get("executables") or ()),
    )
    return scope, data


# -- rebase surface ----------------------------------------------------------
# The rebase module agent runs with 20 adapter-declared tools, not the 6 in the
# shared registry. When a spec carries a "rebase" section the bridge rebuilds
# that surface IN THIS PROCESS — the same `build_rebase_tools` the in-process
# loop uses, over production backends reconstructed from serializable inputs —
# and passes them to `dispatch` as `extra`, so scope guards, out-of-scope
# recording and result bounds stay identical.
#
# Containment caveat, disclosed: `_PATH_ARGS` only knows the shared tools, so
# the read-containment pre-check is a no-op for adapter tools that take paths.
# Writes remain guarded by `ToolScope` inside `dispatch`; reads through
# adapter tools are bounded by the handlers themselves.


class PlanGate:
    """Bridge-side mirror of `agent_loop`'s plan gate.

    The in-process loop withholds gated tools until a decision file exists;
    a harness runs its own loop, so without this the gate would simply not
    exist for harness backends. Enforcing it at dispatch is STRONGER than
    advertisement-based gating: the harness cannot call what it was never
    offered, and cannot bypass by calling it anyway.

    Opens on the same event as the parent: a SUCCESSFUL `write_file` whose
    path contains `.decision.md`. A refused or error-carrying write proves
    nothing was decided and leaves the gate shut.
    """

    def __init__(self, plan_prefix: str, gated: tuple[str, ...],
                 trace: RunTrace | None = None):
        self.plan_prefix = plan_prefix
        self.gated = tuple(gated)
        self.trace = trace
        # A harness may restart its stdio MCP process while preserving the
        # model session. The decision file is the durable gate state; starting
        # every replacement bridge closed forces the agent to rewrite an
        # already-reviewed decision before it can finish or emit its signal.
        try:
            restored = next(
                (path for path in Path(plan_prefix).rglob("*.decision.md")
                 if path.is_file()), None)
        except OSError:
            restored = None
        self.open = restored is not None
        if restored is not None and self.trace is not None:
            self.trace.record("plan_gate_opened", decision=str(restored),
                              restored=True)

    def refusal(self, name: str, args: dict) -> str | None:
        """The refusal text for a call that must not run yet, else None."""
        if self.open:
            return None
        from .rebase_engine.agent_loop import _under_plan_dir
        locked_write = (name == "write_file"
                        and not _under_plan_dir(
                            str(args.get("file_path", "")), self.plan_prefix))
        if name in self.gated or locked_write:
            what = ("write_file outside the plan directory"
                    if locked_write else name)
            extra = (f" Write plan/decision files under {self.plan_prefix}"
                     if locked_write else "")
            return (f"{what} is locked until the plan-review decision file "
                    f"(.decision.md) is written.{extra}")
        return None

    def observe(self, name: str, args: dict, result: str) -> None:
        """Open the gate on a successful decision-file write."""
        if self.open or name != "write_file":
            return
        if ".decision.md" not in str(args.get("file_path", "")):
            return
        try:
            self.open = "error" not in json.loads(result)
        except (TypeError, ValueError):
            self.open = False
        if self.open and self.trace is not None:
            # the parent process reads this back to learn `plan_done`
            self.trace.record("plan_gate_opened",
                              decision=str(args.get("file_path", "")))


def _rebase_extra(spec: dict, trace: RunTrace, scope: ToolScope) -> dict:
    """Rebuild the adapter's rebase tool pack in the bridge process.

    Credentials are NOT in the spec: `api_key` comes from this process's
    settings, and the child env for shell/pytest handlers is this process's
    own environment (inherited from the harness), so no secret is written to
    `run_dir/bridge/`.
    """
    from .config import Settings
    from .engine.steps.rebase_v3 import build_backends
    from .rebase_engine.rebase_tools import (RebasePaths, build_rebase_tools,
                                             load_tool_schemas)
    rb = spec["rebase"]
    settings = Settings()
    # generic reconstruction: the spec's payload IS the dataclass's fields,
    # so naming them here would put repo vocabulary in a neutral module
    payload = dict(rb.get("paths") or {})
    if "test_roots" in payload:
        payload["test_roots"] = tuple(payload["test_roots"])
    paths = RebasePaths(env=dict(os.environ), **payload)
    manifest: dict = {}
    mpath = rb.get("manifest_path", "")
    if mpath and Path(mpath).is_file():
        import yaml
        manifest = yaml.safe_load(Path(mpath).read_text(encoding="utf-8")) or {}
    # `repo` here is the repo ROOT PATH — build_backends feeds it to
    # TestRunner(repo_root=...). The spec's "repo" is the repo NAME and
    # belongs in `state.task_spec.repo`; passing the name here made every
    # TestRunner-backed tool (run_pytest/run_precommit/reproduce) die with
    # FileNotFoundError on a relative path.
    state = dict(rb.get("state") or {})
    state.setdefault("task_spec", {"repo": spec.get("repo", "")})
    backends = build_backends(
        settings=settings, state=state,
        run_dir=Path(spec["run_dir"]), trace=trace, manifest=manifest,
        # the module scope's root IS the repo root (see `_module_scope`);
        # no repo-specific fallback, so a missing root fails loudly
        repo=scope.root,
        model=rb.get("model", ""),
        base_url=rb.get("base_url", ""),
        api_key=getattr(settings, "anthropic_api_key", "") or "",
    )
    defs = load_tool_schemas(Path(rb["tool_schemas"]))
    return build_rebase_tools(defs, paths, backends), defs


def _fn_from_schema(name: str, schema: dict, call):
    """A real-signature function for FastMCP, generated from the tool's JSON
    schema — `add_tool` derives its input schema by inspecting the signature,
    so a `**kwargs` shim would advertise no parameters at all."""
    types = {"string": "str", "integer": "int", "number": "float",
             "boolean": "bool", "array": "list", "object": "dict"}
    props = (schema.get("properties") or {})
    required = set(schema.get("required") or ())
    params, names, omit_if_none = [], [], []
    for pname, pspec in props.items():
        if not pname.isidentifier():
            continue
        ann = types.get((pspec or {}).get("type"), "str")
        names.append(pname)
        if pname in required:
            params.append(f"{pname}: {ann}")
        else:
            default = (pspec or {}).get("default")
            if default is not None:
                params.append(f"{pname}: {ann} = {default!r}")
            else:
                # no schema default: sentinel None, and DROP it below rather
                # than forwarding None into a handler that expects its own
                # Python default (read_file's `offset` did `None + int`)
                params.append(f"{pname}: {ann} | None = None")
                omit_if_none.append(pname)
    # KEYWORD-ONLY: a schema may interleave required and optional properties
    # (record_debug_memory does), which as positionals is a SyntaxError —
    # "parameter without a default follows parameter with a default". MCP
    # calls tools by name, so keyword-only costs nothing and keeps the
    # schema's own property order.
    # a no-parameter tool (git_diff_tests_upstream) must not emit a bare "*"
    sig = f"*, {', '.join(params)}" if params else ""
    body_args = "{" + ", ".join(f"{n!r}: {n}" for n in names) + "}"
    drop = "{" + ", ".join(repr(n) for n in omit_if_none) + "}"
    src = (f"def _tool({sig}) -> str:\n"
           f"    _a = {body_args}\n"
           f"    _drop = {drop}\n"
           "    _a = {k: v for k, v in _a.items()"
           "         if not (k in _drop and v is None)}\n"
           f"    return _call({name!r}, _a)\n")
    ns: dict = {"_call": call}
    exec(compile(src, f"<bridge:{name}>", "exec"), ns)  # noqa: S102
    fn = ns["_tool"]
    fn.__name__ = name
    return fn


# -- server ------------------------------------------------------------------
def make_dispatcher(scope: ToolScope, roots: tuple[str, ...], trace: RunTrace,
                    extra: dict | None = None, gate: "PlanGate | None" = None):
    """The bridge's call path: read containment (stronger than ToolScope,
    which path-guards only writes — see module docstring) and then the same
    `tools.dispatch` the in-process loop uses. Errors raise so the MCP layer
    marks the tool result as an error. Module-level so the guard is testable
    without the MCP SDK installed."""
    from .providers.audit import contained_in

    def _call(name: str, args: dict) -> str:
        key = _PATH_ARGS.get(name)
        if key is not None and roots:
            # mirror dispatch's relative-path resolution BEFORE containment,
            # so the checked path is the one that would actually be touched
            target = args.get(key)
            if isinstance(target, str) and target and not os.path.isabs(target):
                target = os.path.join(roots[0], target)
            if isinstance(target, str) and target and not contained_in(target, roots):
                trace.record("tool_refused", tool=name,
                             reason=f"path outside session roots: {target}")
                raise RuntimeError(
                    f"refused: {name} target is outside this session's "
                    "worktree/run dir")
        if gate is not None:
            refusal = gate.refusal(name, args)
            if refusal:
                trace.record("tool_refused", tool=name, reason=refusal)
                raise RuntimeError(f"refused: {refusal}")
        out = dispatch(name, args, scope=scope, trace=trace, extra=extra)
        if not out["ok"]:
            # `dispatch` traces the call with ok=False but not WHY. Without
            # the reason, "agent read a path that does not exist" and "this
            # tool is broken" look identical in bridge_trace.jsonl and have
            # to be reproduced by hand to tell apart.
            err = str(out.get("error") or "tool error")
            trace.record("tool_error", tool=name, error=err[:500])
            raise RuntimeError(err)
        result = str(out["result"])
        if gate is not None:
            gate.observe(name, args, result)
        return result

    return _call


def traced_call(call, spec: dict):
    """Wrap the bridge dispatcher so each tool call runs under the parent's
    trace/1 store and unit context (from the spec); without a root in the
    spec the wrapper is the identity. Bound per call, not per process: the
    MCP server may serve calls from more than one thread."""
    root = str(spec.get("trace_store_root") or "")
    context = dict(spec.get("trace_context") or {})
    if not root:
        return call
    store = trace_store.TraceStore(Path(root))

    def bound(name: str, args: dict):
        with trace_store.bind_store(store), trace_store.trace_context(**context):
            return call(name, args)

    return bound


def build_server(spec_path: Path):
    """Build the FastMCP server for one spec. Import of the MCP SDK is local
    so the module stays importable (spec read/write) without the extra."""
    from mcp.server.fastmcp import FastMCP

    scope, spec = load_bridge_spec(spec_path)
    trace = RunTrace(Path(spec["trace_path"]))
    roots = tuple(r for r in (scope.root, spec.get("run_dir", "")) if r)

    rebase_extra: dict = {}
    rebase_defs: list[dict] = []
    gate = None
    if spec.get("rebase"):
        rb = spec["rebase"]
        rebase_extra, rebase_defs = _rebase_extra(spec, trace, scope)
        if rb.get("plan_write_prefix"):
            gate = PlanGate(rb["plan_write_prefix"],
                            tuple(rb.get("gated_tools")
                                  or ("edit_file", "run_pytest",
                                      "run_precommit")),
                            trace=trace)
    _call = traced_call(make_dispatcher(scope, roots, trace,
                                        extra=rebase_extra or None, gate=gate), spec)

    mcp = FastMCP("infermatrix-tool-bridge")
    # `dispatch` resolves `extra` BEFORE the builtin registry, so a name the
    # rebase pack provides must be served by ITS handler here too — register
    # the adapter version and drop the builtin, never both.
    allowed = (scope.allowed_tools & set(TOOLS)) - set(rebase_extra)

    for _d in rebase_defs:
        _name = _d.get("name", "")
        if not _name or _name not in rebase_extra:
            continue
        mcp.add_tool(_fn_from_schema(_name, _d.get("input_schema") or {},
                                     _call),
                     name=_name, description=_d.get("description", ""))

    if "read_file" in allowed:
        @mcp.tool(description=TOOLS["read_file"].description)
        def read_file(path: str, offset: int = 0) -> str:
            return _call("read_file", {"path": path, "offset": offset})

    if "list_dir" in allowed:
        @mcp.tool(description=TOOLS["list_dir"].description)
        def list_dir(path: str) -> str:
            return _call("list_dir", {"path": path})

    if "grep" in allowed:
        @mcp.tool(description=TOOLS["grep"].description)
        def grep(pattern: str, path: str, regex: bool = False) -> str:
            return _call("grep", {"pattern": pattern, "path": path,
                                  "regex": regex})

    if "write_file" in allowed:
        @mcp.tool(description=TOOLS["write_file"].description)
        def write_file(path: str, content: str) -> str:
            return _call("write_file", {"path": path, "content": content})

    if "edit_file" in allowed:
        @mcp.tool(description=TOOLS["edit_file"].description)
        def edit_file(path: str, old: str, new: str) -> str:
            return _call("edit_file", {"path": path, "old": old, "new": new})

    if "run_shell" in allowed:
        @mcp.tool(description=TOOLS["run_shell"].description)
        def run_shell(cmd: str, cwd: str = "") -> str:
            return _call("run_shell", {"cmd": cmd, "cwd": cwd or None})

    _register_knowledge_tools(mcp, spec, scope, trace)
    _register_repo_tools(mcp, scope, trace)
    return mcp


def _register_repo_tools(mcp, scope: ToolScope, trace: RunTrace) -> None:
    """Change-archaeology + calc tools (review/repo_tools.py), reconstructed
    from `scope.root` — they need nothing from a live StepContext, so unlike
    skill/memory retrieval they CAN cross the process boundary. Closes that
    slice of the disclosed M1 extra-tools gap for harness sessions."""
    from .engine.steps.review.repo_tools import review_repo_tools

    root = Path(scope.root) if scope.root else None
    tools = review_repo_tools(root if root and root.exists() else None)
    if not tools:
        return

    def _call(name: str, args: dict) -> str:
        out = dispatch(name, args, scope=scope, trace=trace, extra=tools)
        if not out["ok"]:
            raise RuntimeError(str(out.get("error") or "tool error"))
        return str(out["result"])

    @mcp.tool(description=tools["diff_stat"].description)
    def diff_stat() -> str:
        return _call("diff_stat", {})

    @mcp.tool(description=tools["file_at_base"].description)
    def file_at_base(path: str, offset: int = 0) -> str:
        return _call("file_at_base", {"path": path, "offset": offset})

    @mcp.tool(description=tools["show_commit"].description)
    def show_commit(sha: str) -> str:
        return _call("show_commit", {"sha": sha})

    @mcp.tool(description=tools["search_history"].description)
    def search_history(term: str, path: str = "") -> str:
        return _call("search_history", {"term": term, "path": path})

    @mcp.tool(description=tools["calc"].description)
    def calc(expr: str) -> str:
        return _call("calc", {"expr": expr})


def _bridge_ctx(spec: dict, scope: ToolScope, trace: RunTrace):
    """A minimal StepContext view for the agent-runtime knowledge factories:
    they consume only settings / state / run_dir / trace, all of which the
    bridge spec can reconstruct."""
    from types import SimpleNamespace

    from .config import Settings

    return SimpleNamespace(
        settings=Settings(),
        state={"task_spec": {"repo": spec.get("repo", "")},
               "repo_path": scope.root},
        run_dir=Path(spec["run_dir"]),
        trace=trace)


def _register_knowledge_tools(mcp, spec: dict, scope: ToolScope,
                              trace: RunTrace) -> None:
    """Knowledge doc search/read + the on-demand repo_map — the same
    read-only extra tools the in-process runtime hands agent steps, rebuilt
    from the spec. Any piece that cannot be reconstructed degrades to not
    registering (capability_gap traced), never to a crash. Still absent vs
    in-process: skill_search / memory_search / candidate proposals (a
    cross-process write surface deliberately not opened here)."""
    try:
        from .engine.agent_runtime.knowledge import (
            _repo_map_tool,
            _resolve_adapter,
        )
        from .knowledge_docs import KnowledgeDocs

        ctx = _bridge_ctx(spec, scope, trace)
        adapter = _resolve_adapter(ctx)
        repo_subdir = None
        if adapter is not None:
            repo_subdir = (adapter.manifest.get("knowledge")
                           or {}).get("repo_subdir")
        if not repo_subdir and spec.get("repo"):
            repo_subdir = f"repos/{spec['repo']}"
        docs = KnowledgeDocs(ctx.settings.knowledge_dir, repo_subdir)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the bridge
        trace.record("capability_gap", capability="bridge.knowledge_docs",
                     effect=f"doc tools unavailable: {type(exc).__name__}: {exc}")
        return

    @mcp.tool(description="Search the curated knowledge base (general + this "
                          "repo's slice); returns matching doc paths.")
    def doc_search(query: str, limit: int = 20) -> str:
        trace.record("tool_call", tool="doc_search", ok=True,
                     out_of_scope=False, path=None)
        hits = docs.search(query, limit=limit)
        return "\n".join(
            f"{h.get('path')}:{h.get('line')}:{str(h.get('text') or '').strip()}"
            for h in hits) or "(no matches)"

    @mcp.tool(description="Read a knowledge doc returned by doc_search "
                          "(paged; pass the previous next_offset).")
    def doc_read(path: str, offset: int = 0) -> str:
        trace.record("tool_call", tool="doc_read", ok=True,
                     out_of_scope=False, path=path)
        page = docs.read(path, offset=offset)
        text = str(page.get("content") or "")
        nxt = page.get("next_offset")
        return text + (f"\n\n[continues — doc_read offset={nxt}]" if nxt else "")

    try:
        map_tools = _repo_map_tool(ctx, adapter)
    except Exception as exc:  # noqa: BLE001 — optional; degrade loudly
        trace.record("capability_gap", capability="bridge.repo_map",
                     effect=f"repo_map unavailable: {type(exc).__name__}: {exc}")
        return
    if "repo_map" in map_tools:
        tool = map_tools["repo_map"]

        @mcp.tool(description=tool.description)
        def repo_map(query: str) -> str:
            # dispatch with extra= mirrors the in-process extra-tool path
            # (traced, bypasses the builtin allowlist by design)
            out = dispatch("repo_map", {"query": query}, scope=scope,
                           trace=trace, extra=map_tools)
            if not out["ok"]:
                raise RuntimeError(str(out.get("error") or "tool error"))
            return str(out["result"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="infermatrix_copilot.tool_bridge",
        description="Scoped-tool MCP bridge for harness backends (stdio).")
    parser.add_argument("--spec", required=True, help="bridge spec JSON path")
    args = parser.parse_args(argv)
    try:
        server = build_server(Path(args.spec))
    except ImportError:
        import sys

        sys.stderr.write("the tool bridge needs the MCP SDK — "
                         "pip install 'infermatrix-copilot[mcp]'\n")
        return 1
    server.run()  # stdio transport by default
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
