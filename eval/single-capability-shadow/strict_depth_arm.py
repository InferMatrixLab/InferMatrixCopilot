"""Depth proofs over a complete synthetic Git tree, with explicit mode policy."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import infermatrix_copilot
from infermatrix_copilot.kb_service.knowledge_coverage import Feature
from infermatrix_copilot.kb_service.knowledge_depth import render_block, verified_blocks
from infermatrix_copilot.kb_service.init_stages import _page_frontmatter
from infermatrix_copilot.kb_service.depth_judge import review_facets
from infermatrix_copilot.kb_service.init_budget import Budget
from arm_runtime import model_gateway, judgment_context

CORE = '''DEFAULT_INCREMENT = 1

def run(value, increment=DEFAULT_INCREMENT):
    if value < 0:
        raise ValueError("value must be nonnegative")
    return helper(value, increment)

def helper(value, increment):
    return value + increment
'''
TESTS = '''from pkg.core import run

def test_default():
    assert run(1) == 2

def test_override():
    assert run(1, increment=3) == 4
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shim", required=True)
    parser.add_argument("--incomplete", action="store_true", help="Legacy malformed-response fixture expectation; does not remove input evidence")
    parser.add_argument("--omit-test-witness", action="store_true", help="Withhold test source from the judge; requires --capture and independent adjudication")
    parser.add_argument("--mixed-lightweight", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--capture", action="store_true", help="Record actual judgments without asserting scripted answers")
    args = parser.parse_args()
    if args.omit_test_witness and not args.capture:
        parser.error("--omit-test-witness requires --capture; fixture verdict assertions are not quality labels")
    settings, role = judgment_context(args.shim, args.model)
    root = Path.cwd()
    tree = root / "source"
    (tree / "pkg").mkdir(parents=True)
    (tree / "tests").mkdir()
    (tree / "pkg/core.py").write_text(CORE)
    (tree / "tests/test_core.py").write_text(TESTS)
    env = {**os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"}
    def git(*command):
        return subprocess.run(["git", "-C", str(tree), *command], env=env,
                              check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.name", "Offline fixture")
    git("config", "user.email", "offline@example.invalid")
    git("add", ".")
    git("commit", "-qm", "Frozen Strict proof fixture")
    pin = git("rev-parse", "HEAD")
    feature = Feature("increment", "Validated increment", "core", ("pkg/core.py",), (),
                      "repos/depth-fixture/components/core/increment.md", ("pkg/core.py",))
    claims = {
        "flow": "run checks the input, calls helper(value, increment), and returns its result.",
        "api": "run(value, increment=1) returns value plus increment for nonnegative input.",
        "configuration": "The increment argument defaults to DEFAULT_INCREMENT=1 and callers may override it.",
        "dependencies": "run delegates addition to the local helper function; the shown module imports no external library.",
        "failure_modes": "run raises ValueError when value is negative before calling helper.",
        "tradeoffs": "Checking nonnegative input rejects negative arithmetic requests but establishes that precondition before delegation.",
        "validation": "The included tests assert default and overridden increments. They were not executed in this evaluation.",
    }
    mode = "lightweight" if args.mixed_lightweight else "strict"
    if args.mixed_lightweight:
        claims["configuration"] = "The increment argument defaults to 2."
        claims["tradeoffs"] = "This implementation may reduce application latency compared with alternative implementations."
    sections = {}
    for facet, body in claims.items():
        evidence = [{"path": "pkg/core.py", "start": 1, "end": 9}]
        if facet == "validation":
            evidence.append({"path": "tests/test_core.py", "start": 1, "end": 7})
        section = {"facet": facet, "title": facet, "body": body,
                   "interpretation": "inference" if facet == "tradeoffs" else "fact", "evidence": evidence}
        if args.mixed_lightweight and facet == "validation":
            section["validation_kind"] = "helper_unit"
        if facet == "flow":
            section["trace"] = [{"path": "pkg/core.py", "symbol": "run", "start": 3, "end": 6},
                                {"path": "pkg/core.py", "symbol": "helper", "start": 8, "end": 9}]
        sections[facet] = section
    blocks = {facet: render_block(feature, section, tree, "offline/depth-fixture", pin, acceptance_mode=mode)
              for facet, section in sections.items()}
    page = _page_frontmatter("Increment depth", kind="architecture", today="2000-01-01", tags=["fixture"]) + "\n\n".join(blocks.values())
    verified, problems = verified_blocks(page, feature, tree, pin)
    assert not problems and verified == blocks
    # Reject an invented reverse call edge even when both named functions exist.
    wrong = {**sections["flow"], "trace": list(reversed(sections["flow"]["trace"]))}
    try:
        render_block(feature, wrong, tree, "offline/depth-fixture", pin)
    except ValueError:
        pass
    else:
        raise AssertionError("invented call edge passed Strict proof validation")
    (tree / "pkg/core.py").unlink()
    try:
        missing, problems = verified_blocks(page, feature, tree, pin)
        assert not missing and problems
    finally:
        (tree / "pkg/core.py").write_text(CORE)
    evidence = [{"kind": "upstream_text", "source_reference": f"offline/depth-fixture@{pin}:{path}", "text": text}
                for path, text in (("pkg/core.py", CORE), ("tests/test_core.py", TESTS))
                if not (args.omit_test_witness and path == "tests/test_core.py")]
    records = []
    gateway = model_gateway(settings, shim=settings.strict_backend_cli, recorder=records.append)
    rt = SimpleNamespace(gateway=gateway, judge=role, unlimited_subscription=False)
    result = review_facets(rt, Budget(1), SimpleNamespace(judge_call_usd=.01), feature=feature.id,
                           pin=pin, blocks=blocks, existing={}, evidence=evidence)
    expected = {facet: "unjudged" if args.incomplete else "pass" for facet in claims}
    if args.mixed_lightweight:
        expected.update(configuration="fail", tradeoffs="unsure", validation="unjudged")
    observed = {facet: r["verdict"] for facet, r in result["facets"].items()}
    if not args.capture:
        assert observed == expected, result
    (tree / "pkg/core.py").write_text(CORE.replace("return value + increment", "return value - increment"))
    stale, problems = verified_blocks(page, feature, tree, pin)
    assert not stale and problems
    (tree / "pkg/core.py").write_text(CORE)
    (root / "depth-proof.json").write_text(json.dumps({"pin": pin, "feature": feature.id,
        "sections": sections, "blocks": blocks, "page": page, "evidence": evidence,
        "input_variant": "missing-test-witness" if args.omit_test_witness else "complete",
        "omitted_evidence": ["tests/test_core.py"] if args.omit_test_witness else [],
        "result": result, "records": records, "source": {"pkg/core.py": CORE, "tests/test_core.py": TESTS}}, indent=2))
    print(json.dumps({"package_file": infermatrix_copilot.__file__, "facets": 7, "verdict": observed,
                      "adjudication": "pending" if args.capture else "scripted assertions passed",
                      "acceptance_mode": mode,
                      "pin": pin, "source_proofs_verified": True, "invented_edge_rejected": True,
                      "missing_source_rejected": True,
                      "stale_source_rejected": True, "scope": "synthetic pinned depth proof and judgment; no stage correction or catalog publication"}))


if __name__ == "__main__":
    main()
