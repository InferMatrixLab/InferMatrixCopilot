"""W1-W3 focused tests: PR context bundle (incl. the eval-leakage mode),
review render (category scan + merged-verdict calibration), reducer dup guard,
and the non-lossy issue slot rendering."""

from infermatrix_copilot.engine.steps.issue import _render_answer
from infermatrix_copilot.engine.steps.review.utils import _render_review_md


# ---- W2: render ------------------------------------------------------------

def _c(file, line, sev, comment):
    return {"file": file, "line": line, "severity": sev, "comment": comment,
            "evidence": "hunk"}


def test_category_scan_table_is_honest():
    md = _render_review_md({"review_comments": [
        _c("a.py", 1, "major", "missing regression test for the new path"),
        _c("b.py", 2, "minor", "stale docstring"),
    ]})
    assert "| Tests / verification | 1 finding(s) below |" in md
    assert "| Docs / comments | 1 finding(s) below |" in md
    assert "| Security | no finding reported |" in md  # never a claimed PASS


def test_merged_pr_blocker_renders_followup_not_request_changes():
    out = {"review_comments": [_c("a.py", 1, "blocker", "breaks quantized load")]}
    assert _render_review_md(out).endswith("**Verdict:** REQUEST CHANGES")
    assert _render_review_md(out, pr_state="MERGED").endswith(
        "**Verdict:** FOLLOW-UP REQUIRED (post-merge)")
    # non-blocking comments keep their verdicts regardless of state
    ok = {"review_comments": [_c("a.py", 1, "minor", "tidy this")]}
    assert _render_review_md(ok, pr_state="MERGED").endswith("**Verdict:** COMMENT")


# ---- W3: non-lossy slot rendering ------------------------------------------

def test_slots_canonical_when_core_present_draft_preserved():
    out = {"answer_draft": "Long prose diagnosis.\n\nUnique extra insight.",
           "root_cause": "run-level defaults to core_model",
           "fix": "add --run-level=full_model",
           "verification": "re-run pytest with the flag",
           "disposition": "close as invalid"}
    _, text = _render_answer(out)
    assert text.index("### Root cause") < text.index("### Fix") \
        < text.index("### Verification") < text.index("### Disposition")
    assert "Unique extra insight." in text            # draft never discarded
    assert "### Additional context" in text


def test_peripheral_slots_append_to_draft():
    out = {"answer_draft": "The full diagnosis lives here.",
           "disposition": "keep-open until the fix PR merges"}
    _, text = _render_answer(out)
    assert text.startswith("The full diagnosis lives here.")
    assert "**Disposition:** keep-open" in text


def test_no_slots_falls_back_to_old_contract():
    _, text = _render_answer({"answer_draft": "plain answer", "summary": "s"})
    assert text.startswith("plain answer")


# ---- W1: pr_context reaches the review evidence ----------------------------

def test_pr_context_mode_controls_discussion_exposure(settings, trace, tmp_path,
                                                      git_repo, monkeypatch):
    import infermatrix_copilot.engine.steps.pr.fetch as fetch_mod

    view_json = ('{"title": "T", "body": "fixes #7", "labels": [], '
                 '"headRefName": "fix-7", '
                 '"comments": [{"author": {"login": "alice"}, '
                 '"body": "DISCUSSION-MARKER concern"}], "reviews": []}')

    def fake_gh(args, cwd=None, timeout=120):
        if args[:2] == ["pr", "view"]:
            return 0, view_json
        if args[:2] == ["repo", "view"]:
            return 0, '{"nameWithOwner": "org/repo"}'
        if args[:2] == ["issue", "view"]:
            return 0, '{"title": "Linked", "body": "acceptance criteria"}'
        if args[0] == "api":
            return 0, ""
        return 1, "unexpected"

    monkeypatch.setattr(fetch_mod, "_gh", fake_gh)

    class Ctx:
        def __init__(self, mode):
            self.settings = settings
            self.state = {"task_spec": {"kind": "pr_quality"}}
            settings.pr_context_mode = mode

    full = fetch_mod._pr_context_bundle(Ctx("full"), str(git_repo), 9)
    assert "DISCUSSION-MARKER" in full and "Linked issue #7" in full
    assert "do not repeat these concerns" in full

    strict_ctx = Ctx("full")
    strict_ctx.state["task_spec"]["kind"] = "pr_review"
    strict = fetch_mod._pr_context_bundle(strict_ctx, str(git_repo), 9)
    assert "DISCUSSION-MARKER" not in strict and "Linked issue #7" in strict

    bare = fetch_mod._pr_context_bundle(Ctx("no_discussion"), str(git_repo), 9)
    assert "DISCUSSION-MARKER" not in bare        # eval-leakage policy
    assert "Linked issue #7" in bare              # linked issues stay


def test_pr_context_degrades_partial_on_gh_failure(settings, git_repo,
                                                   monkeypatch):
    import infermatrix_copilot.engine.steps.pr.fetch as fetch_mod

    monkeypatch.setattr(fetch_mod, "_gh",
                        lambda args, cwd=None, timeout=120: (1, "boom"))

    class Ctx:
        settings_ = None

        def __init__(self):
            self.settings = settings
            self.state = {}

    text = fetch_mod._pr_context_bundle(Ctx(), str(git_repo), 9)
    assert "partial context" in text              # note, never a raise/block
