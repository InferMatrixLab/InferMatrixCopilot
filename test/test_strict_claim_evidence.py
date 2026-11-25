import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from infermatrix_copilot.engine.steps.review import claim_evidence as evidence


RULE = "**Simplicity is 0/3** if the diff adds or rewrites an explanatory code\ncomment. License headers and directives are exempt."


@pytest.fixture
def records(tmp_path):
    def run(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    run("init", "-q")
    (tmp_path / "AGENTS.md").write_text(RULE + "\nA passing review requires results/raw.tsv.\n")
    (tmp_path / "design.md").write_text(RULE)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("def value():\n    return 1\n")
    run("add", ".")
    run("-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "base")
    base = run("rev-parse", "HEAD")
    (tmp_path / "src/app.py").write_text("def value():\n    # increment value\n    return 2\n")
    run("add", ".")
    run("-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "head")
    return evidence.ReviewRecords(tmp_path, run("rev-parse", "HEAD"), base, False)


def claim(status="verified", **changes):
    return {"claim": "Value API", "kind": "source", "status": status, "reason": "Pinned source check.",
            "records": [{"path": "src/app.py", "quote": "return 2"}], **changes}


def ratings(**overrides):
    return [{"dimension": key, "rating": overrides.get(key, 3), "reason": "Observed changed branch.",
             "path": "src/app.py", "quote": "return 2"} for key in evidence.WEIGHTS]


def test_committed_claims_require_real_blob_quotes(records):
    rows = [claim(), claim(records=[{"path": "src", "quote": "return 2"}]),
            claim(records=[{"path": "src/app.py", "quote": "return 3"}]),
            claim(records=[{"path": "../AGENTS.md", "quote": "EXEMPT"}])]
    assert [r["status"] for r in evidence.assess_claims(rows, records)] == ["verified", "unverified", "unverified", "unverified"]
    assert evidence.assess_claims([], records)[0]["status"] == "unverified"


def test_required_artifact_zero_differs_from_unperformed_check(records):
    missing = claim("missing", records=[{"path": "results/raw.tsv", "requirement_path": "AGENTS.md",
                                          "requirement_quote": "requires results/raw.tsv."}])
    checked = evidence.assess_claims([missing], records)
    assert checked[0]["status"] == "missing"
    assert evidence.score_review(ratings(), checked, records)["total"] == 70
    for bad in [claim("missing"), claim("missing", records=[{"path": "made-up"}]), claim("unverified")]:
        claims = evidence.assess_claims([bad], records)
        assert evidence.score_review(ratings(), claims, records)["total"] is None
    records.head = "f" * 40
    assert evidence.assess_claims([missing], records)[0]["status"] == "unverified"


def test_comparison_needs_raw_records_from_both_actual_revisions(records):
    def job_revision(job_id):
        return {1: records.head, 2: records.base, 3: records.head}[job_id]
    records.job_revision = job_revision
    records.job_line = lambda job, quote: ("inaccessible", "") if job == 3 else ("verified", quote)
    raw = [{"job_id": 1, "quote": "42 ms"}, {"job_id": 2, "quote": "76 ms", "role": "baseline"}]
    def check(refs):
        return evidence.assess_claims([claim(kind="runtime", comparison=True, records=refs)], records)[0]["status"]
    assert check(raw) == "verified"
    assert check(raw[:1]) == "unverified"
    assert check(raw[1:]) == "unverified"
    assert check([{"job_id": 2, "quote": "76 ms"}]) == "mismatched"
    assert check([{"job_id": 3, "quote": "expired"}]) == "inaccessible"
    assert check([{"path": "AGENTS.md", "quote": "requires results/raw.tsv."}]) == "unverified"


def test_scores_reject_hallucinated_citations_and_normalize_with_half_up_rounding(records):
    checked = evidence.assess_claims([claim()], records)
    unsupported = ratings()
    unsupported[1]["quote"] = "invented"
    assert evidence.score_review(unsupported, checked, records)["total"] is None
    for bad in (True, 4, "3", {}, None):
        assert evidence.score_review(ratings(validation=bad), checked, records)["total"] is None
    assert evidence.score_review(ratings(claim_evidence="N/A"), checked, records)["total"] is None
    excluded = evidence.assess_claims([claim("not_applicable")], records)
    assert evidence.score_review(ratings(scope=2), excluded, records)["total"] == 93
    # Applicable weights sum to 80. 30 weighted points gives 12.5.
    assert evidence.score_review(ratings(scope=0, correctness=0, validation="N/A", claim_evidence=1,
                                         simplicity=0), checked, records)["total"] == 13


def test_upstream_multiline_comment_rule_requires_a_governing_policy_and_changed_comment(records):
    checked = evidence.assess_claims([claim()], records)
    diff = "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n+    # increment value\n"
    rows = ratings()
    rows[-1].update(comment_penalty=True, policy_quote=RULE, policy_path="AGENTS.md", quote="# increment value")
    assert evidence.score_review(rows, checked, records, diff=diff)["total"] == 90
    for path in ["design.md", "src/AGENTS.md"]:
        rows[-1]["policy_path"] = path
        assert evidence.score_review(rows, checked, records, diff=diff)["total"] is None
    assert not evidence._explanatory_comment("#!/bin/sh")
    assert not evidence._explanatory_comment("# noqa: F821")
    assert not evidence._explanatory_comment('"""Required contract."""')
    assert not evidence._requires_path("Normal reports are required but results/raw.tsv is optional.", "results/raw.tsv")


def test_actual_merge_verdict_only_changes_for_cited_acceptance_condition(records):
    ctx = SimpleNamespace(settings=SimpleNamespace(pr_context_source="snapshot"), state={
        "pr_head_sha": records.head, "pr_base_sha": records.base, "task_spec": {"params": {}}})
    raw = claim("missing", records=[{"path": "results/raw.tsv", "requirement_path": "AGENTS.md",
                                     "requirement_quote": "requires results/raw.tsv."}])
    for policy, state, verdict in [(False, "OPEN", "APPROVE"), (True, "OPEN", "REQUEST CHANGES"),
                                   (True, "MERGED", "FOLLOW-UP REQUIRED (post-merge)")]:
        ctx.state["pr_state"] = state
        if policy:
            raw.update(acceptance_path="AGENTS.md", acceptance_quote="A passing review requires results/raw.tsv.")
        updates = {"review_verdict": "APPROVE", "review_summary": "**Verdict:** APPROVE"}
        asyncio.run(evidence.apply_review_evidence(ctx, {"claim_checks": [raw]}, updates, records=records))
        assert updates["review_verdict"] == verdict
        assert updates["review_summary"].endswith(f"**Verdict:** {verdict}")
        assert "review_score" not in updates


def test_freshness_unavailable_or_moved_withholds_score(records, monkeypatch):
    ctx = SimpleNamespace(settings=SimpleNamespace(pr_context_source="live"), state={
        "pr_head_sha": records.head, "pr_base_sha": records.base, "task_spec": {"pr": 123, "params": {"include_score": True}}})
    events = []
    ctx.trace = SimpleNamespace(record=lambda event, **details: events.append((event, details)))
    monkeypatch.setattr(evidence, "repo_path", lambda ctx: records.repo)
    raw = {"claim_checks": [claim()], "dimension_ratings": ratings()}
    for sha, expected in [(records.head, 100), (records.base, None), ("", None)]:
        monkeypatch.setattr(evidence.fetch, "_resolve_pr_head", lambda *args: ("main", sha, ""))
        updates = {"review_verdict": "APPROVE"}
        asyncio.run(evidence.apply_review_evidence(ctx, raw, updates, records=records))
        assert updates["review_score"]["total"] == expected
        assert updates["review_summary"].endswith("**Verdict:** APPROVE")
    assert events == [("expected_head_mismatch", {"pr": 123, "expected": records.head,
                                                  "actual": records.base, "step": "agent.review_diff"})]
