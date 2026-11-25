"""The standalone Cursor command and installed hosts consume one review workflow."""

import re
from pathlib import Path

from infermatrix_copilot.upgrade.workflows import get_workflow


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/infermatrix-copilot/skills/imreview/SKILL.md"


def test_cursor_command_requests_the_shipped_review_workflow():
    command = (ROOT / "integrations/cursor/imreview.md").read_text(encoding="utf-8")
    assert 'get_workflow(skill_name="imreview", skill_protocol=1)' in command
    workflow = get_workflow("imreview", skill_protocol=1)
    assert workflow["content"] == SKILL.read_text(encoding="utf-8")
    links = re.findall(r"\]\(([^)]+\.md)\)", workflow["content"])
    assert links
    for target in links:
        path = (Path(workflow["skill_root"]) / target).resolve()
        assert path.is_file()
        if target.startswith("references/"):
            assert Path(workflow["references"][target]) == path


def test_review_freezes_findings_before_feedback_and_validates_before_reporting():
    prompt = SKILL.read_text(encoding="utf-8")
    assert prompt.index("Within 60 seconds") < prompt.index('mode="direct"')
    assert prompt.index("Freeze independently verified findings") < prompt.index("references/pr-feedback.md")
    assert prompt.index("references/pr-feedback.md") < prompt.index("`validate_direct_review`")
    for requirement in ("expected_head_sha", "execution_budget", "explicit human authorization",
                        "direct-continuation/1", "evidence_head_sha", "subtraction_signal",
                        "environment fingerprint", "partial review", "line/hunk"):
        assert requirement in prompt


def test_feedback_dispositions_and_context_modes():
    workflow = get_workflow("imreview")
    feedback = Path(workflow["references"]["references/pr-feedback.md"]).read_text(encoding="utf-8")
    for disposition in ("new", "duplicate", "extends_existing", "resolved_or_outdated"):
        assert f"`{disposition}`" in feedback
    for status in ("checked", "unavailable", "disabled", "not_applicable"):
        assert f"`{status}`" in feedback
    normalized = " ".join(feedback.split())
    for limit in ("latest 20 PR conversation comments", "latest 20 review summaries",
                  "50 thread-aware inline review threads"):
        assert limit in normalized
    assert "PR_CONTEXT_MODE=no_discussion" in feedback


def test_claim_checks_precede_optional_scoring_in_the_delivered_workflow():
    workflow = get_workflow("imreview")
    prompt = workflow["content"]
    assert prompt.index("references/claim-evidence.md") < prompt.index("Freeze independently")
    assert prompt.index("Only when the user requests a") < prompt.index("references/review-score.md")

    score = Path(workflow["references"]["references/review-score.md"]).read_text(encoding="utf-8")
    rows = re.findall(r"^\| ([^|]+?) \| (\d+) \|", score, re.MULTILINE)
    weights = {dimension: int(weight) for dimension, weight in rows}
    assert weights == {
        "Scope and intent": 15, "Correctness and safety": 25, "Validation": 20,
        "Claim evidence": 30, "Simplicity": 10,
    }
