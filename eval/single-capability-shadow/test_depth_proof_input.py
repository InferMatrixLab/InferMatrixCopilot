"""Seven-facet inputs preserve source/mode admission and actual judgments."""
from dataclasses import asdict
from hashlib import sha256
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

import depth_arm
from strict_depth_arm import CORE, TESTS
from infermatrix_copilot.kb_service.knowledge_coverage import Feature
from infermatrix_copilot.kb_service.knowledge_depth import FACETS, render_block
from infermatrix_copilot.kb_service.init_stages import _page_frontmatter
from infermatrix_copilot.kb_service.models import ModelRole, ModelReply
from infermatrix_copilot.kb_service.gate import DIMENSIONS


@pytest.fixture
def proof_case(tmp_path, monkeypatch):
    source = tmp_path / "source";source.mkdir()
    for name, text in [("pkg/core.py", CORE.replace("DEFAULT_INCREMENT = 1", "DEFAULT_INCREMENT = 7")),
                       ("tests/test_core.py", TESTS.replace("assert run(1) == 2", "assert run(1) == 8"))]:
        target = source / name;target.parent.mkdir(parents=True, exist_ok=True);target.write_text(text)
    for command in [("init", "-q"), ("add", "."), ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "source")]:
        subprocess.run(["git", "-C", str(source), *command], check=True, capture_output=True)
    pin = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    feature = Feature("increment", "Increment", "core", ("pkg/core.py",), (), "repos/fixture/increment.md", ("pkg/core.py",))
    inputs = tmp_path / "inputs";inputs.mkdir()
    def prepare(mode, missing_test=False):
        blocks = {}
        for facet in FACETS:
            evidence = [{"path": "pkg/core.py", "start": 1, "end": 9}]
            if facet == "validation":evidence.append({"path": "tests/test_core.py", "start": 1, "end": 7})
            section = {"facet": facet, "title": facet, "body": "run checks its input and returns the helper result.",
                       "interpretation": "fact", "evidence": evidence}
            if facet == "flow":section["trace"] = [{"path": "pkg/core.py", "symbol": "run", "start": 3, "end": 6},
                                                       {"path": "pkg/core.py", "symbol": "helper", "start": 8, "end": 9}]
            if mode == "lightweight" and facet == "validation":section["validation_kind"] = "automated_runtime"
            blocks[facet] = render_block(feature, section, source, "offline/fixture", pin, acceptance_mode=mode)
        page = _page_frontmatter("Proof input", kind="architecture", today="2026-10-16", tags=["review"]) + "\n\n".join(blocks.values())
        return {"repository": "offline/fixture", "pin": pin, "feature": asdict(feature), "blocks": blocks, "page": page,
                "evidence_files": ["pkg/core.py"] + ([] if missing_test else ["tests/test_core.py"]), "existing_knowledge": {}}
    def freeze(packet):
        raw = json.dumps(packet).encode();(inputs / "depth-proof-input.json").write_bytes(raw)
        (inputs / "manifest.json").write_text(json.dumps({"files": {"depth-proof-input.json": {"sha256": sha256(raw).hexdigest()}}}))
    role = ModelRole("judge", "codex", "fixture-judge", "high")
    monkeypatch.setattr(depth_arm, "judgment_context", lambda *a: (SimpleNamespace(strict_backend_cli="unused"), role))
    def invoke(mode, capture=True):
        monkeypatch.setattr(sys, "argv", ["depth", "--inputs", str(inputs), "--shim", "unused", "--mode", mode,
            "--source-repo", str(source), "--output", str(tmp_path / "result.json"), *(["--capture"] if capture else [])])
        depth_arm.main()
    return source, prepare, freeze, invoke


@pytest.mark.parametrize("mode", ["strict", "lightweight"])
@pytest.mark.parametrize("missing_test", [False, True])
def test_all_facets_and_selected_committed_witnesses_reach_review(proof_case, tmp_path, monkeypatch, mode, missing_test):
    source, prepare, freeze, invoke = proof_case
    freeze(prepare(mode, missing_test))
    (source / "pkg/core.py").write_text("raise AssertionError('dirty working tree')\n")
    calls = []
    class Gateway:
        def call_json(self, role, *, prompt, validate, **kwargs):
            packet = json.loads(prompt.removeprefix("<untrusted_data>\n").removesuffix("\n</untrusted_data>"));calls.append(packet)
            facets = {facet: {"dimensions": {key: "yes" for key in DIMENSIONS["prose"]}, "reason": "controlled response"} for facet in packet["sections"]}
            facets["configuration"]["dimensions"]["faithful"] = "unsure"
            data = {"facets": facets};validate(data)
            return ModelReply(role, data, json.dumps(data), "fixture-judge", {}, 0)
    monkeypatch.setattr(depth_arm, "model_gateway", lambda *a, **kw: Gateway())
    invoke(mode)
    packet, = calls
    assert set(packet["sections"]) == set(FACETS)
    assert all("dirty working tree" not in row["text"] for row in packet["evidence"])
    assert "DEFAULT_INCREMENT = 7" in packet["evidence"][0]["text"]
    assert any(row["source_reference"].endswith(":tests/test_core.py") for row in packet["evidence"]) is not missing_test
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["result"]["facets"]["configuration"]["verdict"] == "unsure"
    assert result["result"]["facets"]["flow"]["verdict"] == "pass"
    assert result["input_scope"] == "verified seven-facet source proofs"


@pytest.mark.parametrize("damage", ["pin", "proof", "mode", "missing-facet"])
def test_invalid_source_or_mode_refuses_before_gateway(proof_case, tmp_path, monkeypatch, damage):
    _, prepare, freeze, invoke = proof_case
    packet = prepare("strict")
    if damage == "pin":packet["pin"] = "f" * 40
    elif damage == "proof":packet["blocks"]["flow"] += "forged body"
    elif damage == "missing-facet":packet["blocks"].pop("flow")
    freeze(packet)
    monkeypatch.setattr(depth_arm, "model_gateway", lambda *a, **kw: pytest.fail("invalid proof created gateway"))
    with pytest.raises((ValueError, RuntimeError)):
        invoke("lightweight" if damage == "mode" else "strict")
    assert not (tmp_path / "result.json").exists()


def test_verified_source_mode_requires_capture_before_context(proof_case, tmp_path, monkeypatch):
    _, _, _, invoke = proof_case
    monkeypatch.setattr(depth_arm, "judgment_context", lambda *a: pytest.fail("unselected capture prepared context"))
    with pytest.raises(SystemExit) as failure:invoke("strict", capture=False)
    assert failure.value.code == 2
    assert not (tmp_path / "result.json").exists()
