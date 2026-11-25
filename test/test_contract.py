"""The public contract surface: the structured review result, the capability
handshake, and the import direction that keeps them stable. All offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from infermatrix_copilot import contract
from infermatrix_copilot import run_status as rs


def _run(tmp_path: Path, *, state: str = rs.DONE, updates: dict | None = None,
         events: list[dict] | None = None, note: str = "") -> Path:
    """A finished run directory with just the artifacts the assembler reads."""
    run_dir = tmp_path / "run-20260828-101010-abc123"
    run_dir.mkdir(parents=True, exist_ok=True)
    rs.init_queued(run_dir, run_id=run_dir.name, owner_server_id="S1",
                   owner_server_pid=1)
    rs.mark(run_dir, state, note=note)
    (run_dir / "progress.json").write_text(json.dumps({
        "completed": {"review": {"summary": "ok",
                                 "outputs": {"state_updates": updates or {}}}},
    }), encoding="utf-8")
    if events:
        (run_dir / "run_trace.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return run_dir


# ── the structured result ─────────────────────────────────────────────────────
def test_result_reads_the_verdict_as_a_field_not_from_markdown(tmp_path):
    """Scraping `**Verdict:** …` back out of prose would make a wording change
    silently change what a bot posts."""
    run_dir = _run(tmp_path, updates={
        "review_verdict": "REQUEST CHANGES",
        "review_summary": "2 findings.\n\n**Verdict:** REQUEST CHANGES",
        "pr_head_sha": "a" * 40,
    })
    result = contract.build_review_result(run_dir)
    assert result["verdict"] == "REQUEST CHANGES"
    assert result["reviewed_head_sha"] == "a" * 40
    assert result["state"] == rs.DONE
    assert "review_score" not in result and "review_claim_evidence" not in result
    assert result["contract_version"] == contract.STRICT_API_VERSION


def test_result_whitelists_comment_fields(tmp_path):
    """Internal bookkeeping seen on real runs must not cross the boundary."""
    run_dir = _run(tmp_path, updates={"review_comments": [{
        "file": "a.py", "line": 3, "severity": "major", "comment": "c",
        "evidence": "e", "suggestion": "s",
        "_verified": True, "_anchor_unverified": False,
        "corroborated_by": ["lens-1"],
    }]})
    (comment,) = contract.build_review_result(run_dir)["comments"]
    assert comment == {"file": "a.py", "line": 3, "severity": "major",
                       "comment": "c", "evidence": "e", "suggestion": "s"}


def test_unknown_comment_keys_are_dropped_by_default(tmp_path):
    """An allow-list, so a future internal key is excluded without anyone
    remembering to exclude it."""
    run_dir = _run(tmp_path, updates={
        "review_comments": [{"file": "a.py", "some_future_internal": 1}]})
    assert contract.build_review_result(run_dir)["comments"] == [{"file": "a.py"}]


def test_stale_head_is_a_terminal_structured_fact(tmp_path):
    """The executor checkpoints only successful steps, so the mismatch has to
    survive in the trace — and it has to reach the caller as `stale`, not as
    prose they must parse."""
    run_dir = _run(tmp_path, state=rs.BLOCKED, note="head moved",
                   events=[{"ts": 1, "kind": "expected_head_mismatch", "pr": 7,
                            "expected": "b" * 40, "actual": "c" * 40}])
    result = contract.build_review_result(run_dir)
    assert result["stale"] is True
    assert result["expected_head_sha"] == "b" * 40
    assert result["actual_head_sha"] == "c" * 40
    assert result["state"] == rs.BLOCKED and result["note"] == "head moved"


def test_result_degrades_for_a_run_that_died_before_reviewing(tmp_path):
    """No progress.json, no trace: state plus empty fields, never an exception."""
    run_dir = tmp_path / "run-20260828-101010-dead01"
    run_dir.mkdir()
    rs.init_queued(run_dir, run_id=run_dir.name, owner_server_id="S1",
                   owner_server_pid=1)
    rs.mark(run_dir, rs.FAILED, note="tier not configured")
    result = contract.build_review_result(run_dir)
    assert result["state"] == rs.FAILED and result["note"] == "tier not configured"
    assert result["verdict"] == "" and result["comments"] == []
    assert result["stale"] is False


def test_result_surfaces_planner_and_capability_diagnostics(tmp_path):
    run_dir = _run(tmp_path, events=[
        {"ts": 1, "kind": "review_plan", "depth": "standard",
         "planner": "llm-fallback", "planner_error": "unavailable"},
        {"ts": 2, "kind": "capability_gap", "capability": "review.planner",
         "effect": "empty_reply (stop_reason=max_tokens)"},
    ])
    diagnostics = contract.build_review_result(run_dir)["diagnostics"]
    assert diagnostics["review_plan"][0]["planner_error"] == "unavailable"
    assert diagnostics["capability_gap"][0]["capability"] == "review.planner"


def test_unknown_run_is_explicit_not_an_error():
    """A bot holding an id it can no longer match must be able to tell "lost"
    from "still running"."""
    result = contract.unknown_run_result("run-nope")
    assert result["state"] == "unknown" and result["run_id"] == "run-nope"


def test_quality_result_has_its_own_typed_contract(tmp_path):
    run_dir = _run(tmp_path, updates={
        "pr_head_sha": "d" * 40,
        "quality_verdict": "needs_rework",
        "quality_confidence": "high",
        "quality_summary": "Not ready.",
        "quality_reasons": [
            {"criterion": "validation", "evidence": "No failure-path test",
             "path": "a.py", "line": 3, "private": "drop me"},
        ],
    })
    result = contract.build_quality_result(run_dir)
    assert result["contract_version"] == contract.QUALITY_API_VERSION
    assert result["reviewed_head_sha"] == "d" * 40
    assert result["verdict"] == "needs_rework"
    assert result["confidence"] == "high"
    assert result["reasons"] == [{
        "criterion": "validation", "evidence": "No failure-path test",
        "path": "a.py", "line": 3,
    }]


# ── handshake ─────────────────────────────────────────────────────────────────
def test_capabilities_reports_the_real_worker_count():
    """The MCP server drains its queue with one worker, so Strict requests
    serialize; advertising anything else would invite a bot to fan out."""
    caps = contract.capabilities(max_strict_workers=1)
    assert caps["max_strict_workers"] == 1
    assert caps["strict_api_version"] == contract.STRICT_API_VERSION
    assert caps["knowledge_api_version"] == contract.KNOWLEDGE_API_VERSION
    assert caps["quality_api_version"] == contract.QUALITY_API_VERSION
    assert caps["supports_quality_review"] is True
    assert caps["supports_expected_head"] is True
    assert caps["supports_structured_result"] is True
    assert caps["supports_knowledge_curation"] is True
    assert contract.KNOWLEDGE_API_VERSION == "1.1.0"


def test_capabilities_reports_missing_file_locking():
    caps = contract.capabilities(supports_file_locking=False)
    assert caps["supports_file_locking"] is False
    assert caps["supports_knowledge_curation"] is False


# ── per-run repo binding (item 8) ─────────────────────────────────────────────
def _clone_of(tmp_path: Path, name: str, origin: str) -> Path:
    """A git checkout whose `origin` is `origin`."""
    import subprocess

    path = tmp_path / name
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin",
                    f"https://github.com/{origin}.git"], check=True)
    return path


@pytest.fixture(autouse=True)
def _no_remote_cache():
    """`intent._remote_full_name` caches per path for the process; these tests
    build fresh checkouts at fresh paths but clear it to stay independent."""
    from infermatrix_copilot import intent

    intent._remote_cache.clear()
    yield
    intent._remote_cache.clear()


def test_repo_path_must_be_a_checkout_of_the_named_repo(tmp_path, settings):
    """A root allowlist alone is not enough: an allowed alias paired with a
    different checkout under the same root would review the wrong repository
    while every other guard said yes."""
    from infermatrix_copilot.mcp_policy import PolicyError, authorize_repo_path

    right = _clone_of(tmp_path, "right", "acme/widget")
    wrong = _clone_of(tmp_path, "wrong", "acme/other")
    settings.repo_full_names = {"widget": "acme/widget"}
    settings.mcp_allowed_repo_roots = [str(tmp_path)]

    assert authorize_repo_path("widget", str(right), settings) == str(right)
    with pytest.raises(PolicyError, match="checkout of acme/other"):
        authorize_repo_path("widget", str(wrong), settings)


def test_repo_path_outside_the_allowed_roots_is_refused(tmp_path, settings):
    """A genuine clone of the right repo in an arbitrary place is still an
    operator decision, not a caller's."""
    from infermatrix_copilot.mcp_policy import PolicyError, authorize_repo_path

    elsewhere = _clone_of(tmp_path / "elsewhere", "widget", "acme/widget")
    settings.repo_full_names = {"widget": "acme/widget"}
    settings.mcp_allowed_repo_roots = [str(tmp_path / "allowed")]
    with pytest.raises(PolicyError, match="outside the allowed roots"):
        authorize_repo_path("widget", str(elsewhere), settings)


def test_repo_path_with_unverifiable_identity_fails_closed(tmp_path, settings):
    from infermatrix_copilot.mcp_policy import PolicyError, authorize_repo_path

    path = _clone_of(tmp_path, "c", "acme/widget")
    settings.repo_full_names = {}
    settings.repo_paths = {}
    settings.mcp_allowed_repo_roots = [str(tmp_path)]
    with pytest.raises(PolicyError, match="no known GitHub identity"):
        authorize_repo_path("widget", str(path), settings)


def test_frozen_repo_path_reaches_resolution_and_execution(tmp_path, settings):
    """The hole a `_common.repo_path` change alone would have left: with no
    ambient REPO_PATHS and no adapter `repo.path`, an alias-only lookup drops
    the `repo.path` capability while a perfectly valid checkout sits on the
    spec."""
    from infermatrix_copilot.cli.copilot import Copilot
    from infermatrix_copilot.task_spec import TaskSpec

    checkout = _clone_of(tmp_path, "widget", "acme/widget")
    settings.repo_paths = {}
    copilot = Copilot(settings)
    spec = TaskSpec(kind="pr_review", repo="widget", pr=1,
                    repo_path=str(checkout))
    assert copilot._repo_path_for(spec) == str(checkout)
    assert copilot._resolve_repo_path("widget") == ""  # ambient knows nothing


def test_frozen_repo_path_wins_over_a_different_ambient_one(tmp_path, settings):
    """With both configured, planning must not evaluate one checkout while
    execution runs against another."""
    from infermatrix_copilot.cli.copilot import Copilot
    from infermatrix_copilot.task_spec import TaskSpec

    frozen = _clone_of(tmp_path, "frozen", "acme/widget")
    ambient = _clone_of(tmp_path, "ambient", "acme/widget")
    settings.repo_paths = {"widget": str(ambient)}
    copilot = Copilot(settings)
    spec = TaskSpec(kind="pr_review", repo="widget", pr=1,
                    repo_path=str(frozen))
    assert copilot._repo_path_for(spec) == str(frozen)


def test_configure_strict_repo_is_gone(settings):
    """It mutated process-global `settings.repo_paths` per call, so two
    concurrent Strict requests could each preflight against the other's repo."""
    from infermatrix_copilot.mcp_server import CopilotMCP

    assert not hasattr(CopilotMCP, "configure_strict_repo")


# ── import direction ──────────────────────────────────────────────────────────
def test_contract_imports_no_server_module():
    """The servers import down into `contract`, never the reverse. A cycle here
    is what would make the "only supported import surface" claim false."""
    import ast

    src = Path(contract.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
    assert not {"mcp_server", "thin_mcp_server"} & imported


def test_direct_helpers_have_a_public_home_on_the_contract():
    """The coupling this module exists to retire: a downstream consumer imported
    four `_direct_*` privates out of `thin_mcp_server` through importlib, so a
    rename inside a server module broke another repository at runtime with no
    build-time signal. They are public names here now."""
    for name in ("direct_knowledge_routes", "direct_execution_budget",
                 "direct_completion_result", "direct_mandatory_review_guides"):
        assert callable(getattr(contract, name)), name
        assert name in contract.__all__


def test_direct_routing_does_not_import_a_server_module():
    """Same acyclicity requirement as `contract` itself — `contract` re-exports
    from it, so a server import here would reintroduce the cycle one level
    down."""
    import ast

    from infermatrix_copilot import direct_routing

    src = Path(direct_routing.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
    assert not {"mcp_server", "thin_mcp_server", "contract"} & imported


def test_contract_stays_repo_neutral():
    """The routing tables live in `direct_routing`, not here. `contract` is the
    module every consumer imports; embedding one repo's owner table in it is
    exactly what invariant 6 forbids."""
    import re

    src = Path(contract.__file__).read_text(encoding="utf-8")
    assert not re.search(r"vllm[_\- ]?omni", src, re.IGNORECASE)


def test_get_result_attaches_the_structured_result(settings, tmp_path):
    """The additive `result` key, alongside the report paging kept for hosts
    that already page it."""
    from infermatrix_copilot.mcp_server import CopilotMCP

    core = CopilotMCP(settings)
    try:
        run_dir = _run(Path(settings.run_root),
                       updates={"review_verdict": "APPROVE"})
        (run_dir / "RUN_REPORT.md").write_text("# report", encoding="utf-8")
        out = core.get_result(run_dir.name)
        assert out["result"]["verdict"] == "APPROVE"
        assert out["report"] == "# report"  # back-compat preserved
    finally:
        core.close()


def test_get_result_on_an_unknown_run_id_does_not_raise(settings):
    from infermatrix_copilot.mcp_server import CopilotMCP

    core = CopilotMCP(settings)
    try:
        out = core.get_result("run-20260828-101010-0f0f0f")
        assert out["state"] == "unknown"
        assert out["result"]["state"] == "unknown"
    finally:
        core.close()


@pytest.mark.parametrize("bad", ["../escape", "not-a-run-id", "",
                                 "run-20260828-101010-../../x"])
def test_get_result_still_refuses_a_malformed_run_id(settings, bad):
    """`unknown` is for a well-formed id that merely does not exist. A malformed
    or escaping id is a rejected argument, not a polite empty result — softening
    it would turn the traversal guard into a silent no-op."""
    from infermatrix_copilot.mcp_server import CopilotMCP

    core = CopilotMCP(settings)
    try:
        with pytest.raises(ValueError):
            core.get_result(bad)
    finally:
        core.close()


# -- per-finding result (RFC-pr-state-machine, #174) ------------------------


def _finding(file="vllm_omni/core/engine.py", line=42, severity="blocker",
             comment="Null dereference when the sampler returns None"):
    return {"file": file, "line": line, "severity": severity, "comment": comment}


def test_findings_carry_identity_severity_and_the_head_they_were_found_on(tmp_path):
    run_dir = _run(tmp_path, updates={
        "pr_head_sha": "head-1",
        "review_comments": [_finding()],
    })

    findings = contract.build_review_result(run_dir)["findings"]

    assert len(findings) == 1
    found = findings[0]
    assert set(found) == set(contract.FINDING_FIELDS)
    assert found["severity"] == "blocker"
    assert found["anchor"] == {"path": "vllm_omni/core/engine.py", "line": 42}
    assert found["head_sha"] == "head-1"
    assert len(found["finding_id"]) == 16


def test_a_findings_identity_survives_a_line_shift_but_not_a_new_file():
    same = contract.finding_id("a/b.py", "Null deref when x is None")
    # The same finding after an unrelated edit moved it down the file.
    assert same == contract.finding_id("a/b.py", "Null  deref when  x is None")
    assert same == contract.finding_id("a/b.py", "NULL DEREF WHEN X IS NONE")
    assert same != contract.finding_id("a/c.py", "Null deref when x is None")
    assert same != contract.finding_id("a/b.py", "Unrelated finding")


def test_two_findings_sharing_a_long_prefix_are_not_the_same_finding():
    """Identity hashes the whole text. Truncating it would merge a blocker
    into a nit whenever a review opens two findings the same way."""
    prefix = "The sampler path does not validate its input before use, " * 4
    assert contract.finding_id("a/b.py", prefix + "and dereferences None.") != (
        contract.finding_id("a/b.py", prefix + "and logs a misleading message.")
    )


def test_two_findings_published_separately_both_survive(tmp_path):
    """Whatever the pipeline chose to publish separately is two findings;
    collapsing them could drop a blocker."""
    prefix = "The sampler path does not validate its input before use, " * 4
    run_dir = _run(tmp_path, updates={
        "pr_head_sha": "head-1",
        "review_comments": [
            _finding(comment=prefix + "and dereferences None."),
            _finding(line=91, severity="nit",
                     comment=prefix + "and logs a misleading message."),
        ],
    })

    findings = contract.build_review_result(run_dir)["findings"]

    assert [f["severity"] for f in findings] == ["blocker", "nit"]
    assert findings[0]["finding_id"] != findings[1]["finding_id"]


def test_the_published_severity_is_the_one_that_crosses_the_boundary(tmp_path):
    """`review_comments` is the finalized set, so a demoted finding reports
    the severity it was published with, not the one it was raised with."""
    run_dir = _run(tmp_path, updates={
        "pr_head_sha": "head-1",
        "review_comments": [_finding(severity="Minor")],
    })

    assert contract.build_review_result(run_dir)["findings"][0]["severity"] == "minor"


def test_a_fileless_finding_keeps_its_identity_without_an_anchor(tmp_path):
    run_dir = _run(tmp_path, updates={
        "pr_head_sha": "head-1",
        "review_comments": [{"severity": "major", "comment": "No tests at all"}],
    })

    found = contract.build_review_result(run_dir)["findings"][0]

    assert found["anchor"] is None and found["finding_id"]


def test_findings_never_borrow_a_disposition_from_another_candidate(tmp_path):
    """A withheld candidate and a published one can share an anchor, so the
    audit trail is reported as itself and never joined onto a finding."""
    run_dir = _run(tmp_path, updates={
        "pr_head_sha": "head-1",
        "review_comments": [_finding()],
        "review_finding_dispositions": [
            {"anchor": "vllm_omni/core/engine.py:42",
             "disposition": "no_issue", "declared": "no_issue"},
            {"anchor": "vllm_omni/core/engine.py:42",
             "disposition": "published", "declared": "issue"},
        ],
    })

    result = contract.build_review_result(run_dir)

    assert result["findings"][0]["severity"] == "blocker"
    assert set(result["findings"][0]) == set(contract.FINDING_FIELDS)
    assert len(result["finding_dispositions"]) == 2


def test_findings_do_not_change_the_aggregate_disposition_list(tmp_path):
    run_dir = _run(tmp_path, updates={
        "pr_head_sha": "head-1",
        "review_comments": [_finding()],
        "review_finding_dispositions": [{
            "anchor": "vllm_omni/core/engine.py:42",
            "disposition": "published",
            "declared": "issue",
            "secret": "withheld prose",
        }],
    })

    result = contract.build_review_result(run_dir)

    assert result["finding_dispositions"] == [
        {"anchor": "vllm_omni/core/engine.py:42",
         "disposition": "published", "declared": "issue"}
    ]


def test_the_contract_version_announces_the_new_field(tmp_path):
    run_dir = _run(tmp_path, updates={"pr_head_sha": "h", "review_comments": []})
    result = contract.build_review_result(run_dir)
    assert result["contract_version"] >= "1.2.0"
    assert result["findings"] == []


def test_a_run_that_died_before_reviewing_still_reports_findings(tmp_path):
    run_dir = tmp_path / "run-20260828-101010-dead02"
    run_dir.mkdir(parents=True, exist_ok=True)
    rs.init_queued(run_dir, run_id=run_dir.name, owner_server_id="S1",
                   owner_server_pid=1)
    rs.mark(run_dir, rs.FAILED, note="died")

    assert contract.build_review_result(run_dir)["findings"] == []


@pytest.mark.parametrize("answers", [[], [{"finding_id": "old", "head_sha": "wrong", "outcome": "fixed", "evidence": "guard"}]])
def test_omitted_or_wrong_head_rechecks_never_claim_complete(tmp_path, answers):
    result = contract.build_review_result(_run(tmp_path, updates={
        "pr_head_sha": "a" * 40,
        "review_carried_findings": [{"finding_id": "old"}],
        "review_finding_rechecks": answers,
    }))
    assert not result["rechecks_complete"]
    assert not result["finding_rechecks"]


@pytest.mark.parametrize("state", [rs.QUEUED, rs.RUNNING, rs.FAILED])
def test_expected_rechecks_come_from_persisted_request_before_review(tmp_path, state):
    run_dir = _run(tmp_path, state=state)
    (run_dir / "request.json").write_text(json.dumps({"params": {"carried_findings": [{
        "finding_id": "old", "source_head_sha": "b" * 40, "title": "Old blocker"}]}}))
    result = contract.build_review_result(run_dir)
    assert not result["rechecks_complete"]
    assert "missing finding recheck: old" in result["recheck_missing"]


@pytest.mark.parametrize("outcome", [[], {}, None, 42])
def test_malformed_agent_rechecks_become_gaps_instead_of_crashes(tmp_path, outcome):
    result = contract.build_review_result(_run(tmp_path, updates={
        "pr_head_sha": "a" * 40,
        "review_carried_findings": [{"finding_id": "old"}],
        "review_finding_rechecks": [{"finding_id": "old", "head_sha": "a" * 40,
            "outcome": outcome, "evidence": "guard"}],
    }))
    assert not result["rechecks_complete"]
    assert result["finding_rechecks"] == []
