"""Installed-wheel validation: copy this module outside the source checkout."""

from __future__ import annotations

import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from infermatrix_copilot.knowledge_service.validation import (
    PROVIDER_IMPORT_ROOT, VALIDATOR_IDS, validate_workspace,
)


@pytest.fixture
def portable_workspace(tmp_path):
    package = files("infermatrix_copilot")
    knowledge = package.joinpath("knowledge")
    if not knowledge.is_dir() and os.environ.get("INFERMATRIX_WHEEL_VALIDATION") != "1":
        pytest.skip("installed wheel resources are required")
    assert knowledge.is_dir(), "wheel is missing its knowledge resources"
    assert package.joinpath("_runtime", "knowledge-schema.md").is_file()
    for identifier in VALIDATOR_IDS:
        target = tmp_path / identifier
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(package.joinpath(*identifier.split("/")).read_bytes())
    for name in ("general", "repos"):
        index = tmp_path / "knowledge" / name / "_index.md"
        index.parent.mkdir(parents=True)
        index.write_text("---\ntitle: Index\ncreated: 2026-10-12\nupdated: 2026-10-12\n"
                         "type: index\ntags: [general]\n---\n# Index\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    assert not (tmp_path / "src").exists()
    assert not (tmp_path / "doc").exists()
    return tmp_path


def test_wheel_cli_wrappers_use_packaged_schema_without_a_source_checkout(portable_workspace):
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONPATH", PROVIDER_IMPORT_ROOT}}
    for identifier in VALIDATOR_IDS:
        completed = subprocess.run([sys.executable, identifier], cwd=portable_workspace,
            env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "0 个错误" in completed.stdout


def test_wheel_runner_reports_canonical_policy_and_schema_failures(portable_workspace):
    report = validate_workspace(portable_workspace, timeout_seconds=30)
    assert report.passed, report.problems()
    package = files("infermatrix_copilot").joinpath("knowledge_service")
    names = ("validation.py", "format.py", "validators/tree.py", "validators/wiki.py",
             "../process_execution.py", "../process_tree.py")
    material = {name: hashlib.sha256(package.joinpath(*name.split("/")).read_bytes()).hexdigest()
                for name in names}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    for execution in report.executions:
        assert execution.implementation_sha256 == digest
        script = portable_workspace / execution.result.validator_id
        assert execution.code_sha256 == hashlib.sha256(script.read_bytes()).hexdigest()
        assert len(execution.output_sha256) == 64
        assert execution.receipt()["exit_code"] == 0
    index = portable_workspace / "knowledge/repos/_index.md"
    index.write_text(index.read_text().replace("tags: [general]", "tags: [not-a-schema-taxonomy-tag]"))
    failed = validate_workspace(portable_workspace, timeout_seconds=30)
    assert failed.results[0].passed
    assert not failed.results[1].passed and failed.results[1].returncode == 1
    assert "not-a-schema-taxonomy-tag" in failed.results[1].output
    assert failed.executions[1].implementation_sha256 == digest
    assert failed.executions[1].output_sha256 != report.executions[1].output_sha256


def test_wheel_authoring_guidance_preserves_layout_without_expanding_read_scope(portable_workspace):
    from infermatrix_copilot.knowledge_docs import KnowledgeDocs, KnowledgeDocsError

    package = files("infermatrix_copilot")
    guidance = package.joinpath("doc", "knowledge")
    assert guidance.joinpath("CONTRIBUTING.md").is_file()
    for name in ("incidents.md", "validation.md", "layout.md", "page-rules.md"):
        assert guidance.joinpath("contributing", name).is_file()
    assert guidance.joinpath("SCHEMA.md").read_bytes() == package.joinpath(
        "_runtime", "knowledge-schema.md").read_bytes()
    # Link targets exist beside the KB, but retrieval authority stays inside
    # the original knowledge root and selected slices.
    docs = KnowledgeDocs(Path(str(package.joinpath("knowledge"))))
    with pytest.raises(KnowledgeDocsError, match="escapes the knowledge root"):
        docs.read("../doc/knowledge/contributing/incidents.md")
