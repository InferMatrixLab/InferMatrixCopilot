"""The shared wire schema preserves canonical discussion-reference shapes."""
import jsonschema
import pytest

from infermatrix_copilot.sdk.v1 import direct_review_schema


@pytest.mark.parametrize("reference", [
    "", "issue_comment:1", "inline_comment:42", "review_summary:9",
    "https://github.com/owner/repo/pull/7#discussion_r12",
    "https://github.com/owner/repo/issues/7#issuecomment-12",
    "https://github.com/owner/repo/pull/7#pullrequestreview-12",
])
def test_review_schema_accepts_empty_or_canonical_thread_reference(reference):
    field = direct_review_schema()["properties"]["finding_dispositions"]["items"]["properties"]["existing_thread"]
    jsonschema.Draft202012Validator(field).validate(reference)


@pytest.mark.parametrize("reference", [
    "issue_comment:0", "issue_comment:-1", "issue_comment:01", "inline_comment:",
    "review_summary:any", "unknown:1", "http://github.com/o/r/pull/1#discussion_r1",
    "https://github.com.attacker.invalid/o/r/pull/1#discussion_r1",
    "https://attacker.invalid/o/r/pull/1#discussion_r1",
    "https://github.com/o/r/pull/0#discussion_r1",
    "https://github.com/o/r/pull/1#discussion_r0",
    "https://github.com/o/r/pull/1?query=x#discussion_r1",
    "https://github.com/o/r/pull/1", "issue_comment:" + "1" * 513,
])
def test_review_schema_rejects_noncanonical_thread_reference(reference):
    field = direct_review_schema()["properties"]["finding_dispositions"]["items"]["properties"]["existing_thread"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(field).validate(reference)
