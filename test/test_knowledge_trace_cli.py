"""User-facing knowledge trace inspection works without a model or KB checkout."""

import json

from infermatrix_copilot.cli.entry import main
from infermatrix_copilot.knowledge_trace import KnowledgeTrace


def _session(tmp_path):
    root = tmp_path / "traces"
    recorder = KnowledgeTrace(root=root, mode="required")
    session = recorder.start_session(repository="demo", mode="direct", source_revision="a" * 40)
    trace_id = session["trace_id"]
    recorder.retrieve(trace_id, step_id="defaults", entries=[], mechanism="search", query="zero")
    recorder.finish_session(trace_id)
    return root, trace_id


def test_show_and_export_are_readable_with_collection_disabled(tmp_path, monkeypatch, capsys):
    root, trace_id = _session(tmp_path)
    monkeypatch.setenv("KNOWLEDGE_TRACE_MODE", "off")
    assert main(["knowledge-trace", "show", trace_id, "--root", str(root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["session"]["repository"] == "demo"
    assert report["retrievals"][0]["step_id"] == "defaults"
    destination = tmp_path / "exports" / "知识追踪.md"
    assert main(["knowledge-trace", "--root", str(root), "export", trace_id,
                 "--out", str(destination), "--format", "markdown"]) == 0
    content = destination.read_text(encoding="utf-8")
    assert "defaults" in content
    assert "demo" in content


def test_query_and_json_stdout_export(tmp_path, capsys):
    root, trace_id = _session(tmp_path)
    assert main(["knowledge-trace", "query", "--root", str(root), "--repo", "demo"]) == 0
    records = json.loads(capsys.readouterr().out)
    assert len(records) == 3 and {r["trace_id"] for r in records} == {trace_id}
    assert [r["event_type"] for r in records] == [
        "session_started", "retrieval_completed", "session_finished"]
    assert main(["knowledge-trace", "query", "--root", str(root), "--repo", "other"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["knowledge-trace", "export", trace_id, "--root", str(root),
                 "--format", "json", "--out", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["session"]["repository"] == "demo"


def test_invalid_session_and_invalid_limit_have_actionable_errors(tmp_path, capsys):
    root, trace_id = _session(tmp_path)
    assert main(["knowledge-trace", "show", "missing", "--root", str(root)]) == 2
    assert "knowledge-trace:" in capsys.readouterr().err
    assert main(["knowledge-trace", "query", "--repo", "demo", "--root", str(root),
                 "--limit", "0"]) == 2
    assert "limit" in capsys.readouterr().err.lower()


def test_export_collects_all_event_pages(tmp_path, monkeypatch, capsys):
    import infermatrix_copilot.knowledge_trace as module

    root, trace_id = _session(tmp_path)

    class TinyPages(KnowledgeTrace):
        def report(self, trace_id, cursor=None, limit=None):
            return super().report(trace_id, cursor=cursor, limit=1)

    monkeypatch.setattr(module, "KnowledgeTrace", TinyPages)
    assert main(["knowledge-trace", "export", trace_id, "--root", str(root),
                 "--format", "json", "--out", "-"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["events"]) == report["counts"]["events"] == 3
    assert report["next_cursor"] is None
