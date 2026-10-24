"""Tool-bridge spec round-trip and the read-containment guard
(doc/features/provider-registry.md — the `.env`-exfiltration case)."""

from pathlib import Path

import pytest

from infermatrix_copilot.run_trace import RunTrace
from infermatrix_copilot.scopes import ToolScope, read_only_scope
from infermatrix_copilot.tool_bridge import (
    load_bridge_spec,
    make_dispatcher,
    write_bridge_spec,
)


def _scoped(tmp_path: Path) -> tuple[ToolScope, Path, Path]:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scope = read_only_scope()
    scope = type(scope)(name=scope.name, allowed_tools=scope.allowed_tools,
                        read_only=True, root=str(worktree))
    return scope, worktree, run_dir


def test_bridge_spec_round_trips(tmp_path):
    scope, _worktree, run_dir = _scoped(tmp_path)
    spec_path = write_bridge_spec(run_dir=run_dir,
                                  step_name="agent.review_diff#lens0",
                                  scope=scope, repo="vllm-omni")
    loaded, raw = load_bridge_spec(spec_path)
    assert 'knowledge_dir' not in raw  # omitted legacy field stays omitted
    assert loaded == scope
    assert raw["repo"] == "vllm-omni"
    assert raw["run_dir"] == str(run_dir)
    assert "lens0" in spec_path.name and "#" not in spec_path.name


def test_bridged_read_inside_root_passes_and_relative_resolves(tmp_path):
    scope, worktree, run_dir = _scoped(tmp_path)
    (worktree / "mod.py").write_text("x = 1\n", encoding="utf-8")
    trace = RunTrace(run_dir / "bridge_trace.jsonl")
    call = make_dispatcher(scope, (str(worktree), str(run_dir)), trace)

    assert call("read_file", {"path": str(worktree / "mod.py")}) == "x = 1\n"
    # repo-relative paths resolve against the worktree, as in-process
    assert call("read_file", {"path": "mod.py"}) == "x = 1\n"
    assert any(e["tool"] == "read_file"
               for e in trace.events("tool_call"))


def test_bridged_read_outside_roots_is_refused_and_traced(tmp_path):
    scope, worktree, run_dir = _scoped(tmp_path)
    secret = tmp_path / "home" / ".infermatrix-copilot" / ".env"
    secret.parent.mkdir(parents=True)
    secret.write_text("ANTHROPIC_API_KEY=leak-me\n", encoding="utf-8")
    trace = RunTrace(run_dir / "bridge_trace.jsonl")
    call = make_dispatcher(scope, (str(worktree), str(run_dir)), trace)

    with pytest.raises(RuntimeError, match="refused"):
        call("read_file", {"path": str(secret)})
    refusals = list(trace.events("tool_refused"))
    assert refusals and "outside session roots" in refusals[0]["reason"]


def test_scope_tool_allowlist_still_enforced(tmp_path):
    scope, worktree, run_dir = _scoped(tmp_path)
    trace = RunTrace(run_dir / "bridge_trace.jsonl")
    call = make_dispatcher(scope, (str(worktree), str(run_dir)), trace)

    # read_only_scope has no run_shell — dispatch refuses by tool name; the
    # bridge surfaces it as an error, identically to the in-process loop
    with pytest.raises(RuntimeError, match="not allowed"):
        call("run_shell", {"cmd": "echo hi"})


def test_bridged_write_refused_in_read_only_scope(tmp_path):
    scope, worktree, run_dir = _scoped(tmp_path)
    scope = type(scope)(name=scope.name,
                        allowed_tools=scope.allowed_tools | {"write_file"},
                        read_only=True, root=str(worktree))
    trace = RunTrace(run_dir / "bridge_trace.jsonl")
    call = make_dispatcher(scope, (str(worktree), str(run_dir)), trace)

    with pytest.raises(RuntimeError, match="read-only"):
        call("write_file", {"path": str(worktree / "a.txt"), "content": "x"})
    assert not (worktree / "a.txt").exists()


def test_bridged_docs_keep_explicit_snapshot_when_cli_filters_or_changes_env(tmp_path, monkeypatch):
    import json
    from infermatrix_copilot.knowledge_view import build_manifest, KnowledgeViewError
    from infermatrix_copilot.tool_bridge import _register_knowledge_tools

    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    scope, _worktree, run_dir = _scoped(tmp_path)
    roots = []
    for name, text in [('pinned', 'original pinned knowledge'), ('later', 'later active knowledge')]:
        root = tmp_path / name / 'knowledge'
        (root / 'general').mkdir(parents=True)
        (root / 'AGENTS.md').write_text('Knowledge fixture')
        (root / 'general/pinned.md').write_text(text)
        (root.parent / 'MANIFEST.json').write_text(json.dumps(build_manifest(root, name)))
        roots.append(root)
    path = write_bridge_spec(run_dir=run_dir, step_name='review', scope=scope,
                             repo='fixture', knowledge_dir=roots[0])
    _, spec = load_bridge_spec(path)

    class ToolRegistry:
        def __init__(self):
            self.functions = {}

        def tool(self, **kwargs):
            def register(function):
                self.functions[function.__name__] = function
                return function
            return register

    monkeypatch.delenv('KNOWLEDGE_ROOT', raising=False)
    monkeypatch.delenv('KNOWLEDGE_DIR', raising=False)
    registry = ToolRegistry()
    _register_knowledge_tools(registry, spec, scope, RunTrace(run_dir / 'filtered.jsonl'))
    assert 'original pinned knowledge' in registry.functions['doc_read']('general/pinned.md')
    monkeypatch.setenv('KNOWLEDGE_ROOT', str(roots[1].parent))
    monkeypatch.setenv('KNOWLEDGE_DIR', str(roots[1]))
    changed_env = ToolRegistry()
    _register_knowledge_tools(changed_env, spec, scope, RunTrace(run_dir / 'swapped.jsonl'))
    assert 'original pinned knowledge' in changed_env.functions['doc_read']('general/pinned.md')
    (roots[0] / 'general/pinned.md').write_text('tampered pinned knowledge')
    with pytest.raises(KnowledgeViewError, match='does not match manifest'):
        changed_env.functions['doc_read']('general/pinned.md')
