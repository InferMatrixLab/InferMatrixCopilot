"""Missing source is an input variant, separate from malformed native replies."""
import json
import sys
from types import SimpleNamespace

import pytest

import strict_depth_arm


def test_missing_witness_requires_capture_before_work_or_gateway(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["depth", "--shim", "unused", "--omit-test-witness"])
    monkeypatch.setattr(strict_depth_arm, "judgment_context", lambda *a: pytest.fail("prepared model context"))
    with pytest.raises(SystemExit) as failure:
        strict_depth_arm.main()
    assert failure.value.code == 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("missing", [False, True])
def test_source_omission_reaches_canonical_review_without_rewriting_response(tmp_path, monkeypatch, missing):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(strict_depth_arm, "judgment_context", lambda *a: (SimpleNamespace(strict_backend_cli="unused"), object()))
    monkeypatch.setattr(strict_depth_arm, "model_gateway", lambda *a, **k: object())
    packets = []
    def review(rt, *args, **kwargs):
        packets.append(kwargs)
        return {"facets": {facet: {"verdict": "pass"} for facet in kwargs["blocks"]}}
    monkeypatch.setattr(strict_depth_arm, "review_facets", review)
    monkeypatch.setattr(sys, "argv", ["depth", "--shim", "unused", "--capture", *(["--omit-test-witness"] if missing else [])])
    strict_depth_arm.main()
    packet, = packets
    references = [item["source_reference"] for item in packet["evidence"]]
    assert any(reference.endswith(":pkg/core.py") for reference in references)
    assert any(reference.endswith(":tests/test_core.py") for reference in references) is not missing
    assert "tests/test_core.py" in packet["blocks"]["validation"]
    output = json.loads((tmp_path / "depth-proof.json").read_text())
    assert output["input_variant"] == ("missing-test-witness" if missing else "complete")
    assert output["omitted_evidence"] == (["tests/test_core.py"] if missing else [])
    # Capture preserves even an unsupported acceptance for independent assessment.
    assert output["result"]["facets"]["validation"]["verdict"] == "pass"
    assert output["evidence"] == packet["evidence"]
