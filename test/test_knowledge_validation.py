"""Checkout, snapshot and portable CLI paths share the knowledge validation gate."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from infermatrix_copilot.knowledge_service.format import PAGE_MAX_BYTES, page_over_capacity
from infermatrix_copilot.knowledge_service.validation import (
    VALIDATOR_IDS, implementation_digest, validate_snapshot, validate_workspace, validator_preflight,
)
from infermatrix_copilot.knowledge_service.validators import tree
from infermatrix_copilot.knowledge_service.validators import wiki


def scripts(root, *, tree_code=0):
    for index, identifier in enumerate(VALIDATOR_IDS):
        path = root / identifier
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"print('validator {index}')\nraise SystemExit({tree_code if index == 0 else 0})\n")


def test_workspace_reports_order_and_exact_execution_evidence(tmp_path):
    scripts(tmp_path)
    report = validate_workspace(tmp_path)
    assert report.passed
    assert [result.validator_id for result in report.results] == list(VALIDATOR_IDS)
    assert [result.output for result in report.results] == ["validator 0", "validator 1"]
    for index, execution in enumerate(report.executions):
        assert execution.receipt() == {
            "name": Path(VALIDATOR_IDS[index]).name,
            "code_sha256": hashlib.sha256((tmp_path / VALIDATOR_IDS[index]).read_bytes()).hexdigest(),
            "output_sha256": hashlib.sha256(f"validator {index}\n".encode()).hexdigest(),
            "exit_code": 0,
            "implementation_sha256": implementation_digest(),
        }


@pytest.mark.parametrize("stop, count", [(False, 2), (True, 1)])
def test_workspace_failure_has_same_result_with_bounded_collection(tmp_path, stop, count):
    scripts(tmp_path, tree_code=3)
    report = validate_workspace(tmp_path, stop_on_failure=stop)
    assert not report.passed
    assert len(report.results) == count
    assert report.results[0].returncode == 3
    assert report.problems() == ["check_knowledge_tree.py failed: validator 0"]


def test_missing_or_linked_validator_is_not_executed(tmp_path):
    scripts(tmp_path)
    first = tmp_path / VALIDATOR_IDS[0]
    first.unlink()
    first.symlink_to(tmp_path / VALIDATOR_IDS[1])
    assert validator_preflight(tmp_path)[0].status == "missing"
    report = validate_workspace(tmp_path, stop_on_failure=True)
    assert not report.passed and report.results[0].status == "missing"


def test_timeout_is_a_failed_report(tmp_path):
    scripts(tmp_path)
    (tmp_path / VALIDATOR_IDS[0]).write_text("import time\ntime.sleep(10)\n")
    report = validate_workspace(tmp_path, timeout_seconds=1, stop_on_failure=True)
    assert report.results[0].status == "timeout"
    assert report.results[0].returncode is None
    assert not report.passed


def test_receipt_changes_when_provider_policy_changes(monkeypatch):
    before = implementation_digest()
    original_read = Path.read_bytes

    def revised_policy(path):
        content = original_read(path)
        return content + b"\n# policy revision\n" if path.name == "format.py" else content

    monkeypatch.setattr(Path, "read_bytes", revised_policy)
    assert implementation_digest() != before


def test_snapshot_validates_candidate_without_changing_checkout(tmp_path):
    scripts(tmp_path)
    for directory in ("adapters", "doc/knowledge"):
        target = tmp_path / directory / "baseline.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("baseline")
    first = tmp_path / VALIDATOR_IDS[0]
    first.write_text("from pathlib import Path\nassert Path('knowledge/candidate.md').read_text() == 'candidate'\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.org",
                    "commit", "-qm", "baseline"], check=True)
    assert validate_snapshot(tmp_path, "HEAD", {"knowledge/candidate.md": "candidate"}).passed
    assert not (tmp_path / "knowledge/candidate.md").exists()
    assert not validate_snapshot(tmp_path, "HEAD", {"../outside": "candidate"}).passed


def test_portable_cli_adapters_use_installed_provider_with_explicit_workspace(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    for identifier in VALIDATOR_IDS:
        target = tmp_path / identifier
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checkout / identifier, target)
    for name in ("general", "repos"):
        index = tmp_path / "knowledge" / name / "_index.md"
        index.parent.mkdir(parents=True)
        index.write_text("---\ntitle: Index\ncreated: 2026-10-12\nupdated: 2026-10-12\n"
                         "type: index\ntags: [general]\n---\n# Index\n")
    schema = tmp_path / "doc/knowledge/SCHEMA.md"
    schema.parent.mkdir(parents=True)
    schema.write_text("## 标签分类法\n`general`\n## Sources\n")
    candidate_package = tmp_path / "src/infermatrix_copilot/__init__.py"
    candidate_package.parent.mkdir(parents=True)
    candidate_package.write_text("raise AssertionError('candidate provider must never be imported')\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    report = validate_workspace(tmp_path)
    assert report.passed, report.problems()
    (tmp_path / "knowledge/repos/_index.md").write_text("# Missing metadata\n")
    report = validate_workspace(tmp_path)
    assert report.results[0].passed
    assert not report.results[1].passed


@pytest.mark.parametrize("size, rejected", [(PAGE_MAX_BYTES - 1, False), (PAGE_MAX_BYTES, True)])
def test_tree_and_operation_capacity_have_one_boundary(tmp_path, size, rejected):
    page = tmp_path / "rules.md"
    text = "x" * size
    page.write_text(text)
    tree.configure(tmp_path)
    tree.check_file_size(page, "")
    assert bool(tree.errors) is rejected
    assert bool(page_over_capacity(text)) is rejected


def test_wiki_metadata_and_lifecycle_share_complete_frontmatter_boundaries(tmp_path):
    from infermatrix_copilot.knowledge_service.lifecycle import Page

    page = tmp_path / "rules.md"
    page.write_text('---\ntitle: "a---b"\ntype: rule\n---\n# Rules\n')
    assert wiki.frontmatter(page) == Page.parse(page.read_text()).frontmatter_data()
    assert wiki.frontmatter(page)["title"] == "a---b"


def test_wiki_accepts_an_explicit_taxonomy_outside_the_workspace(tmp_path):
    schema = tmp_path / "policy.md"
    schema.write_text("## 标签分类法\n`explicit-policy`\n## Sources\n")
    wiki.configure(tmp_path / "portable/knowledge", schema_path=schema)
    assert wiki.taxonomy() == {"explicit-policy"}


@pytest.mark.parametrize("identifier, module", zip(VALIDATOR_IDS, (tree, wiki)))
def test_cli_adapters_support_legacy_direct_module_loading(identifier, module):
    import importlib.util

    script = Path(__file__).resolve().parents[1] / identifier
    spec = importlib.util.spec_from_file_location("legacy_knowledge_validator", script)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    assert adapter.main is module.main
    assert adapter.ROOT == script.parents[1]


@pytest.mark.parametrize("leader_waits", [False, True])
def test_validator_owns_descendants_before_returning(tmp_path, leader_waits):
    import os
    import signal
    from infermatrix_copilot.process_tree import _start_time
    scripts(tmp_path)
    pidfile = tmp_path / "child.pid"
    (tmp_path / VALIDATOR_IDS[0]).write_text(
        "import pathlib,subprocess,sys,time\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))\n"
        + ("time.sleep(30)\n" if leader_waits else "time.sleep(.15)\n"))
    def state(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return None
    pid = birth = None
    try:
        report = validate_workspace(tmp_path, timeout_seconds=1, stop_on_failure=True)
        pid = int(pidfile.read_text()); birth = _start_time(pid)
        assert report.results[0].status == ("timeout" if leader_waits else "passed")
        assert state(pid) in (None, "Z", "X")
    finally:
        if pid is None and pidfile.exists():
            pid = int(pidfile.read_text()); birth = _start_time(pid)
        if pid is not None and birth is not None and _start_time(pid) == birth and state(pid) not in (None, "Z", "X"):
            os.kill(pid, signal.SIGKILL)


def test_unresolved_cleanup_stops_validation_and_retains_producer_facts(tmp_path, monkeypatch):
    import json
    from infermatrix_copilot.knowledge_service import validation
    from infermatrix_copilot.process_execution import ProcessCleanupError, ProcessOutcome, process_cleanup_facts
    scripts(tmp_path)
    error = ProcessCleanupError(ProcessOutcome("partial", "diagnostic", 0))
    calls = []
    def unresolved(*args, **kwargs):
        calls.append(args)
        raise error
    monkeypatch.setattr(validation, "run_command", unresolved)
    report = validate_workspace(tmp_path, stop_on_failure=False)
    assert report.pending and not report.passed and len(calls) == 1
    assert report.results[0].status == "unresolved" and report.results[0].returncode is None
    assert not report.executions[0].output_sha256
    assert json.loads(report.results[0].output) == process_cleanup_facts(error)


@pytest.mark.parametrize("filename", ["process_execution.py", "process_tree.py"])
def test_receipt_binds_shared_process_implementation(monkeypatch, filename):
    before = implementation_digest()
    read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: read(path) + (b"# revision" if path.name == filename else b""))
    assert implementation_digest() != before
