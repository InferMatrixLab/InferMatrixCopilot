from infermatrix_copilot.review.diff import diff_index, patch_index
from infermatrix_copilot.review.publication_policy import review_head_error
from types import SimpleNamespace
import pytest

from infermatrix_copilot.review.dispositions import disposition, DispositionError
from infermatrix_copilot.sdk.v1 import review_evidence_error


def test_head_gate_requires_both_heads_and_equality():
    assert review_head_error("a" * 40, "a" * 40) == ""
    for old, new in (("", "a" * 40), ("a" * 40, ""), ("a" * 40, "b" * 40), ("", "")):
        assert review_head_error(old, new)


def test_per_file_and_full_diff_share_segments_and_clipping():
    for patch in ("@@ -1,2 +1,2 @@\n same\n-old\n+new", "@@ -1,2 +1,2 @@\n same"):
        indexed = patch_index(patch)
        assert indexed == diff_index("--- a/file.py\n+++ b/file.py\n" + patch)["file.py"]
    assert patch_index("@@ -1,2 +1,2 @@\n same").state == "incomplete"


@pytest.mark.parametrize("kind,thread,recheck,error", [
    ("new", "thread", "", "new finding"),
    ("duplicate", "", "", "existing_thread"),
    ("resolved_or_outdated", "thread", "", "fixed/still_affected"),
    ("extends_existing", "thread", "fixed", "head_recheck"),
    ("invented", "", "", "disposition is invalid"),
])
def test_execution_and_publication_use_the_same_disposition_semantics(kind, thread, recheck, error):
    item = dict(anchor="a.py:1", disposition=kind, existing_thread=thread, head_recheck=recheck)
    result = SimpleNamespace(existing_feedback_status="checked", finding_dispositions=(item,),
                             findings=(SimpleNamespace(path="a.py", line=1),))
    with pytest.raises(DispositionError, match=error):
        disposition(item, strict=True, normalize_reference=lambda value: value)
    assert error in review_evidence_error(mode="direct", result=result, expected_status="checked",
                                          normalize_reference=lambda value: value,
                                          allowed_references={"thread"})


def test_publication_binds_valid_disposition_to_frozen_thread_inventory():
    result = SimpleNamespace(existing_feedback_status="checked",
        finding_dispositions=({"anchor":"a.py:1", "disposition":"duplicate",
                               "existing_thread":"foreign", "head_recheck":""},),
        findings=(SimpleNamespace(path="a.py", line=1),))
    assert "not a member" in review_evidence_error(mode="direct", result=result, expected_status="checked",
        normalize_reference=lambda value: value, allowed_references={"thread"})


def test_standalone_publication_rechecks_withheld_candidate_proof():
    from infermatrix_copilot.engine.steps.pr.publish import _review_payload

    state = {"review_comments": [{"file":"a.py", "line":1, "comment":"withheld"}],
             "review_finding_dispositions": [{"anchor":"a.py:1", "disposition":"excluded"}]}
    with pytest.raises(ValueError, match="own finalized set withheld"):
        _review_payload(state)
