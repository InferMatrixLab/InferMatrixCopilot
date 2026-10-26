from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from infermatrix_copilot.knowledge_view import KnowledgeViewError, build_manifest
from infermatrix_copilot.engine.agent_runtime.knowledge import _repo_docs_tool
from infermatrix_copilot import tool_bridge
from infermatrix_copilot.config import Settings
from infermatrix_copilot.scopes import read_only_scope


def test_bridge_and_inprocess_refuse_changed_pinned_knowledge(tmp_path, monkeypatch):
    root = tmp_path / 'snapshot' / 'knowledge'
    doc = root / 'repos/r/doc.md'
    doc.parent.mkdir(parents=True)
    doc.write_text('original trusted document')
    (root / 'AGENTS.md').write_text('knowledge')
    (root.parent / 'MANIFEST.json').write_text(json.dumps(build_manifest(root, 'signed-read-fixture')))
    monkeypatch.setenv('KNOWLEDGE_ROOT', str(root.parent))
    run = tmp_path / 'run';run.mkdir()
    trace = SimpleNamespace(record=lambda *args, **kwargs: None)
    ctx = SimpleNamespace(settings=Settings(_env_file=None,knowledge_dir=root,run_root=run,
        memory_db=tmp_path/'memory.sqlite',repo_paths={},knowledge_general_docs=[]),
        state={'task_spec':{'repo':'r'}},params={'knowledge_context_profile':'legacy'},
        run_dir=run,trace=trace)
    from infermatrix_copilot.engine.agent_runtime import knowledge
    adapter = SimpleNamespace(manifest={'knowledge':{'repo_subdir':'repos/r'}})
    monkeypatch.setattr(knowledge, '_resolve_adapter', lambda ctx: adapter)
    monkeypatch.setattr(knowledge, '_repo_map_tool', lambda *args: {})
    inprocess = _repo_docs_tool(ctx, adapter)
    handlers = {}
    class Capture:
        def tool(self, **kwargs):
            def decorate(fn):handlers[fn.__name__] = fn;return fn
            return decorate
    monkeypatch.setattr(tool_bridge, '_bridge_ctx', lambda *args: ctx)
    tool_bridge._register_knowledge_tools(Capture(), {'repo':'r','run_dir':str(run)}, read_only_scope(), trace)
    assert 'doc_read' in handlers
    assert inprocess['doc_read'].handler('repos/r/doc.md') == 'original trusted document'
    assert handlers['doc_read']('repos/r/doc.md') == 'original trusted document'
    doc.write_text('tampered document')
    with pytest.raises(KnowledgeViewError):
        inprocess['doc_read'].handler('repos/r/doc.md')
    with pytest.raises(KnowledgeViewError):
        handlers['doc_read']('repos/r/doc.md')
    with pytest.raises(KnowledgeViewError):
        handlers['doc_search']('tampered')


def test_frozen_session_carries_only_manifest_sidecar_and_rejects_tampering(session, monkeypatch, tmp_path):
    from infermatrix_copilot.providers.session_inputs import freeze_session
    from infermatrix_copilot.sandbox.inputs import stage_inputs
    from infermatrix_copilot.knowledge_view import KnowledgeView
    settings, _, request = session
    knowledge = Path(settings.knowledge_dir)
    (knowledge / 'AGENTS.md').write_text('knowledge')
    manifest = knowledge.parent / 'MANIFEST.json'
    manifest.write_text(json.dumps(build_manifest(knowledge, 'frozen-pinned-view')))
    (knowledge.parent / 'host-secret.txt').write_text('must not enter the sandbox')
    frozen = freeze_session(request, knowledge_dir=knowledge)
    assert '/tmp/inputs/MANIFEST.json' in frozen.frozen_inputs['files']
    assert not any('host-secret' in name for name in frozen.frozen_inputs['files'])
    staged, _ = stage_inputs(tmp_path / 'staged-inputs', frozen.frozen_inputs)[0]
    monkeypatch.setenv('KNOWLEDGE_ROOT', str(tmp_path / 'unavailable-host-activation'))
    view = KnowledgeView.for_root(staged / 'knowledge')
    assert view.verified and view.snapshot == 'frozen-pinned-view'
    assert view.read_text('general/rules.md') == 'read-only knowledge'
    path = staged / 'knowledge/general/rules.md'
    path.chmod(0o644)
    path.write_text('tampered after freeze')
    with pytest.raises(KnowledgeViewError):
        view.read_text('general/rules.md')


# Reuse the maintained agent-session fixtures rather than another provider loop.
from test_sandbox_agent_session import session, configured  # noqa: E402,F401
