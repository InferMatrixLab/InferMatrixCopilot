"""Frozen maintenance source acquisition uses committed objects before dispatch."""
from hashlib import sha256
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

import maintenance_arm
from infermatrix_copilot.kb_service.models import ModelRole, ModelReply


@pytest.fixture
def capsule(tmp_path, monkeypatch):
    source = tmp_path / "source";source.mkdir()
    code = "def value():\n    return 11\n"
    (source / "value.py").write_text(code)
    for command in [("init", "-q"), ("add", "."), ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "pinned")]:
        subprocess.run(["git", "-C", str(source), *command], check=True, capture_output=True)
    pin = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    evidence = {"repository": "offline/fixture", "sha": pin, "path": "value.py", "start_line": 1, "end_line": 2,
                "content_sha256": sha256(code.encode()).hexdigest(), "excerpt": code,
                "source_reference": f"offline/fixture@{pin}:value.py:L1-L2"}
    data = {"unit": {"unit_id": "source-value", "repository": "offline/fixture", "text": "value returns 11.",
                     "upstream_pin": pin, "primary_sources": [{key: evidence[key] for key in
                         ("repository", "sha", "path", "start_line", "end_line", "content_sha256")}]}, "evidence": [evidence]}
    inputs = tmp_path / "inputs";inputs.mkdir()
    role = ModelRole("judge", "codex", "fixture-judge", "high")
    monkeypatch.setattr(maintenance_arm, "judgment_context", lambda *a: (SimpleNamespace(strict_backend_cli="unused"), role))
    def freeze():
        raw = json.dumps(data).encode();(inputs / "maintenance-input.json").write_bytes(raw)
        (inputs / "manifest.json").write_text(json.dumps({"files": {"maintenance-input.json": {"bytes": len(raw), "sha256": sha256(raw).hexdigest()}}}))
    freeze()
    def invoke(*args):
        monkeypatch.setattr(sys, "argv", ["maintenance", "--inputs", str(inputs), "--shim", "unused", "--model", "fixture-judge", "--output", str(tmp_path / "result.json"), *args])
        maintenance_arm.main()
    class Gateway:
        quote = "return 11"
        def call_json(self, requested, *, system, prompt, validate, **kwargs):
            reply = {"outcome": "verified", "reason": "The shown return is 11.", "witnesses": [0], "conflicts": [],
                     "assessments": [{"claim": "value returns 11.", "witness": 0, "quote": self.quote,
                                      "relation": "supports", "explanation": "The body directly returns 11."}]}
            validate(reply)
            return ModelReply(requested, reply, json.dumps(reply), "fixture-judge", {}, 0)
    gateway = Gateway()
    monkeypatch.setattr(maintenance_arm, "model_gateway", lambda *a, **kwargs: gateway)
    return source, data, freeze, invoke, gateway


def test_local_source_audit_uses_commit_not_dirty_working_tree(capsule, tmp_path):
    source, _, _, invoke, _ = capsule
    (source / "value.py").write_text("def value():\n    return 99\n")
    invoke("--source-repo", str(source))
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "validated" and result["source_acquisition"] == "pinned local Git objects"
    assert result["result"]["evidence"][0]["excerpt"] == "def value():\n    return 11\n"


@pytest.mark.parametrize("damage", ["pin", "witness"])
def test_wrong_pin_or_witness_refuses_before_gateway(capsule, tmp_path, monkeypatch, damage):
    source, data, freeze, invoke, _ = capsule
    if damage == "pin":
        data["unit"]["upstream_pin"] = data["unit"]["primary_sources"][0]["sha"] = "f" * 40
    else:
        data["evidence"][0]["excerpt"] = "invented text"
    freeze()
    monkeypatch.setattr(maintenance_arm, "model_gateway", lambda *a, **k: pytest.fail("invalid source constructed model gateway"))
    with pytest.raises(ValueError):invoke("--source-repo", str(source))
    assert not (tmp_path / "result.json").exists()


@pytest.mark.parametrize("invalid_quote", [False, True])
def test_archived_source_scope_restores_reader_and_retains_quote_validation(capsule, tmp_path, invalid_quote):
    _, _, _, invoke, gateway = capsule
    original = maintenance_arm.maintenance_audit.source_evidence
    if invalid_quote:
        gateway.quote = "invented return 99"
        with pytest.raises(SystemExit) as error:invoke()
        assert error.value.code == 1
    else:
        invoke()
    assert maintenance_arm.maintenance_audit.source_evidence is original
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == ("failed" if invalid_quote else "validated")
    assert result["source_acquisition"] == "frozen archived witnesses"
