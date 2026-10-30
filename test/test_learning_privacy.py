import pytest

pytestmark = pytest.mark.usefixtures("accepted_learning_feature")

from infermatrix_copilot.knowledge_service.learning_privacy import (
    LearningPrivacyError,
    validate_distilled_text,
)


@pytest.mark.parametrize("value", [
    "ghp_" + "a" * 36,
    "github_pat_" + "a" * 55,
    "sk-proj-" + "b" * 40,
    "AKIA" + "1" * 16,
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "Bearer " + "a" * 32,
    "https://user:secret@example.com/repo",
])
def test_known_credentials_are_rejected_without_being_echoed(value):
    with pytest.raises(LearningPrivacyError) as caught:
        validate_distilled_text({"rules": [{"section_markdown": value}]})
    assert value not in str(caught.value)


def test_chat_export_is_rejected_but_protocol_examples_are_allowed():
    validate_distilled_text({"summary": 'Validate the {"role":"user"} message shape before dispatch.'})
    validate_distilled_text({"evidence": ["tests/test_api.py::test_empty_input passed"]})
    with pytest.raises(LearningPrivacyError, match="conversation export"):
        validate_distilled_text("user: " + "private chat " * 40 + "\nassistant: another private response")
