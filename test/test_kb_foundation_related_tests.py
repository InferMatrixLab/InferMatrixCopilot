"""Shared test inventory supplies real late assertions within the source cap."""
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service.feature_discovery_index import build_discovery_index
from infermatrix_copilot.kb_service.init_knowledge_parallel import related_test_sources
from infermatrix_copilot.kb_service.init_support import InitError, InitRecord


def _stage(tmp_path):
    files = {"src/tool.py": "class SpecificTool:\n    def run(self):\n        return 7\n",
        "sdk/nested/tests/test_tool.py": "from src.tool import SpecificTool\n" + "# filler\n" * 220 +
            "def test_real_tool():\n    assert SpecificTool().run() == 7\n",
        "tests/test_name_only_tool.py": "def test_unrelated():\n    assert 'SpecificTool' == 'SpecificTool'\n",
        "frontend/src/widget.ts": "export function SpecificWidget() { return 7; }\n",
        "frontend/tests/widget.test.ts": "import {SpecificWidget} from '../src/widget';\n" + "// filler\n" * 240 +
            "test('widget', () => { expect(SpecificWidget()).toBe(7); });\n",
        "opaque/tool.strange": "function SpecificOpaque() { return 7; }\n",
        "opaque/tests/check.strange": "import 'opaque/tool.strange';\nassert SpecificOpaque() == 7\n"}
    for path, text in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    index = build_discovery_index(tmp_path, pin="a" * 40, scope={"roots": ["."], "exclude": []})
    return SimpleNamespace(source_index=index, record=InitRecord("knowledge", "toy", pin="a" * 40))


@pytest.mark.parametrize("source,target,assertion", [
    ("src/tool.py", "sdk/nested/tests/test_tool.py", "assert SpecificTool().run() == 7"),
    ("frontend/src/widget.ts", "frontend/tests/widget.test.ts", "expect(SpecificWidget()).toBe(7)"),
    ("opaque/tool.strange", "opaque/tests/check.strange", "assert SpecificOpaque() == 7"),
])
def test_direct_imports_and_late_assertions_include_nested_multilanguage_tests(tmp_path, source, target, assertion):
    stage = _stage(tmp_path)
    excerpts = related_test_sources(stage, [source])
    chosen = [item for item in excerpts if item["path"] == target]
    assert chosen and any(assertion in item["text"] for item in chosen)
    if source != "opaque/tool.strange":
        assert any(item["start"] > 180 for item in chosen)
    assert all("source text only, no execution outcome evidence" in item["test_context"] for item in chosen)
    assert all(item["path"] != "tests/test_name_only_tool.py" for item in excerpts)
    assert sum(len(item["text"].encode()) for item in excerpts) <= 20000


def test_test_cap_keeps_complete_intervals_and_read_failures_are_not_absence(tmp_path):
    stage = _stage(tmp_path)
    assert related_test_sources(stage, ["src/tool.py"], limit=1) == []
    path = "sdk/nested/tests/test_tool.py"
    stage.source_index.entries[path]["status"] = "error"
    assert related_test_sources(stage, ["src/tool.py"]) == []
    assert stage.record.evidence == {}  # No missing-test assertion or recognition.
    stage.record.pin = "b" * 40
    with pytest.raises(InitError, match="test index source pin"):
        related_test_sources(stage, ["src/tool.py"])


def test_specific_capability_test_survives_earlier_shared_entry_test_budget(tmp_path):
    files = {
        "src/app.py": "def shared_entry():\n    return 1\n",
        "src/share_image_export.py": "class ShareImageExportManager:\n    def export(self):\n        return 'png'\n",
        "tests/a_shared.py": "from src.app import shared_entry\n\ndef test_shared_entry():\n" +
            "    assert shared_entry() == 1\n" * 150,
        "tests/test_share_image_export.py": "from src.share_image_export import ShareImageExportManager\n\n" +
            "def test_png_export():\n    assert ShareImageExportManager().export() == 'png'\n",
    }
    for path, text in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    index = build_discovery_index(tmp_path, pin="a" * 40, scope={"roots": ["."], "exclude": []})
    stage = SimpleNamespace(source_index=index, record=InitRecord("knowledge", "toy", pin="a" * 40))
    excerpts = related_test_sources(stage, ["src/app.py", "src/share_image_export.py"], limit=600)
    assert excerpts[0]["path"] == "tests/test_share_image_export.py"
    assert any("assert ShareImageExportManager().export()" in item["text"] for item in excerpts)
    assert sum(len(item["text"].encode()) for item in excerpts) <= 600


@pytest.mark.parametrize("limit", [1, 600, 100000])
def test_foundation_packet_reserves_actual_test_bytes_inside_one_existing_cap(tmp_path, limit):
    from infermatrix_copilot.kb_service.init_knowledge_parallel import foundation_source_packet
    stage = _stage(tmp_path)
    budgets = []
    def read_sources(remaining):
        budgets.append(remaining)
        return [{"path":"src/tool.py", "text":"源" * (remaining // 3)}]
    packet, count = foundation_source_packet(stage, ["src/tool.py"],
        read_sources=read_sources, byte_limit=limit)
    tests = [item for item in packet if item.get("test_context")]
    assert count == len(tests)
    assert budgets == [limit - sum(len(item["text"].encode()) for item in tests)]
    assert sum(len(item["text"].encode()) for item in packet) <= limit
    assert all("source text only, no execution outcome evidence" in item["test_context"] for item in tests)
    assert all("tests have not been executed" not in item["test_context"] for item in tests)
    if limit == 100000:
        assert any("assert SpecificTool().run() == 7" in item["text"] for item in tests)
