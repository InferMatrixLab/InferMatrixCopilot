import io
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from infermatrix_copilot.knowledge_service import agreement, agreement_cli
from infermatrix_copilot.knowledge_service.agreement import (
    AGREEMENT_SHA256,
    AGREEMENT_TEXT,
    AGREEMENT_VERSION,
    AgreementRequiredError,
    AgreementStore,
    validate_upload_attestation,
)


def accept(store, name="Synthetic Test User"):
    document = store.document()
    return store._accept(name, version=document["version"], agreement_sha256=document["sha256"],
                         context_sha256=document["context_sha256"], confirmation="I AGREE")


@pytest.fixture
def store(tmp_path):
    return AgreementStore(tmp_path / "state")


def test_read_only_status_and_document_do_not_create_state(store, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_LEARNING_AGREEMENT_ACCEPTED", "1")
    document = store.document()
    assert (document["version"], document["text"], document["sha256"]) == (AGREEMENT_VERSION, AGREEMENT_TEXT, AGREEMENT_SHA256)
    assert store.status()["status"] == "required"
    with pytest.raises(AgreementRequiredError) as error:
        store.require("learn")
    assert error.value.code == "agreement_required"
    assert not store.state_dir.exists()


def test_terms_disclose_cost_data_destinations_and_revocation():
    for required in ("tokens", "execution time", "paid", "personal", "proprietary", "private local",
                     "public repository", "collector", "reasons", "task identifiers", "raw chat",
                     "perfect redaction", "in flight", "does not erase", "legal"):
        assert required in AGREEMENT_TEXT.lower()


def test_named_acceptance_is_private_exact_and_status_is_minimal(store):
    result = accept(store)
    assert result["status"] == "accepted"
    assert "signer" not in result and "Synthetic Test User" not in json.dumps(result)
    record = json.loads(store.path.read_text())
    accepted = record["acceptance"]
    assert accepted["signer"] == "Synthetic Test User"
    assert accepted["agreement_text"] == AGREEMENT_TEXT
    assert accepted["agreement_sha256"] == AGREEMENT_SHA256
    assert accepted["context"] == store.document()["context"]
    assert accepted["confirmation"] == "I AGREE"
    assert record["audit"][0]["acceptance"] == accepted
    if hasattr(os, "getuid"):
        assert accepted["owner_uid"] == os.getuid()
    if os.name != "nt":
        assert store.directory.stat().st_mode & 0o777 == 0o700
        assert store.path.stat().st_mode & 0o777 == 0o600
    minimal = store.require("feedback_grade")
    assert set(minimal) == {"version", "agreement_sha256", "context_sha256", "acceptance_id", "accepted_at"}
    assert AgreementStore(store.state_dir).status() == result


@pytest.mark.parametrize("change", [
    {"version": "old"}, {"agreement_sha256": "0" * 64}, {"context_sha256": "0" * 64},
    {"confirmation": "yes"}, {"confirmation": "I AGREE "}, {"signer": " "}, {"signer": "A\nB"},
    {"signer": "ghp_" + "A" * 30},
])
def test_acceptance_requires_name_and_exact_displayed_terms_and_destinations(store, change):
    document = store.document()
    arguments = {"signer": "Synthetic Test User", "version": document["version"],
                 "agreement_sha256": document["sha256"], "context_sha256": document["context_sha256"],
                 "confirmation": "I AGREE", **change}
    with pytest.raises(AgreementRequiredError):
        store._accept(**arguments)
    assert store.status()["status"] == "required"


def test_revocation_keeps_private_audit_and_new_acceptance_gets_new_identity(store):
    first = accept(store)
    assert store.revoke()["status"] == "revoked"
    with pytest.raises(AgreementRequiredError):
        store.attestation()
    revoked = json.loads(store.path.read_text())
    assert revoked["acceptance"]["acceptance_id"] == first["acceptance_id"]
    second = accept(store)
    assert second["acceptance_id"] != first["acceptance_id"]
    assert [event["action"] for event in json.loads(store.path.read_text())["audit"]] == ["accepted", "revoked", "accepted"]


def test_revoke_without_acceptance_remains_disabled(store):
    assert store.revoke()["status"] == "revoked"
    assert json.loads(store.path.read_text())["acceptance"] is None
    with pytest.raises(AgreementRequiredError):
        store.require("feedback_upload")


def test_context_changes_require_new_acceptance_but_token_rotation_does_not(tmp_path):
    environ = {"KNOWLEDGE_LEARNING_STATE_DIR": str(tmp_path / "state"),
               "KNOWLEDGE_LEARNING_PUBLICATION": "draft_pr", "KNOWLEDGE_LEARNING_REPOSITORY": "owner/knowledge",
               "KNOWLEDGE_LEARNING_BASE": "main", "KNOWLEDGE_FEEDBACK_URL": "https://feedback.example.test",
               "KNOWLEDGE_FEEDBACK_TOKEN": "original-token"}
    first = AgreementStore.from_env(environ=environ)
    accept(first)
    rotated = AgreementStore.from_env(environ={**environ, "KNOWLEDGE_FEEDBACK_TOKEN": "replacement-token"})
    assert rotated.status()["status"] == "accepted"
    for key, value in (("KNOWLEDGE_LEARNING_PUBLICATION", "local"),
                       ("KNOWLEDGE_LEARNING_REPOSITORY", "other/knowledge"),
                       ("KNOWLEDGE_LEARNING_BASE", "release"),
                       ("KNOWLEDGE_FEEDBACK_URL", "https://other.example.test")):
        changed = AgreementStore.from_env(environ={**environ, key: value})
        assert changed.status()["status"] == "renewal_required"
        with pytest.raises(AgreementRequiredError):
            changed.require("learn")
    override = AgreementStore.from_env(environ=environ, feedback_url="http://127.0.0.1:8787")
    assert override.document()["context"]["feedback_url"] == "http://127.0.0.1:8787"
    assert override.status()["status"] == "renewal_required"
    assert "token" not in json.dumps(first.document()["context"]).lower()


def test_terms_version_change_requires_renewal(store, monkeypatch):
    accept(store)
    monkeypatch.setattr(agreement, "AGREEMENT_VERSION", "next")
    assert store.status()["status"] == "renewal_required"
    with pytest.raises(AgreementRequiredError):
        store.require("learn")


@pytest.mark.parametrize("raw", ["not json", "{}", '{"protocol":"x","protocol":"y"}', "[]"])
def test_corrupt_records_fail_closed(store, raw):
    accept(store)
    store.path.write_text(raw)
    assert store.status()["status"] == "unavailable"
    with pytest.raises(AgreementRequiredError):
        store.require("learn")


def test_owner_and_permission_mismatch_fail_closed(store):
    accept(store)
    if hasattr(os, "getuid"):
        record = json.loads(store.path.read_text())
        record["acceptance"]["owner_uid"] = os.getuid() + 1
        store.path.write_text(json.dumps(record))
        assert store.status()["status"] == "unavailable"
    if os.name != "nt":
        store.path.chmod(0o644)
        assert store.status()["status"] == "unavailable"


def test_symlink_state_or_record_cannot_authorize_or_be_overwritten(tmp_path, store):
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    unsafe = AgreementStore(linked)
    assert unsafe.status()["status"] == "unavailable"
    with pytest.raises(AgreementRequiredError):
        accept(unsafe)
    assert not (target / "agreement").exists()
    accept(store)
    original = store.path.read_text()
    copied = tmp_path / "copied.json"
    copied.write_text(original)
    store.path.unlink()
    store.path.symlink_to(copied)
    assert store.status()["status"] == "unavailable"
    with pytest.raises(AgreementRequiredError):
        store.revoke()
    assert copied.read_text() == original


def test_interrupted_atomic_write_preserves_prior_acknowledgement(store, monkeypatch):
    first = accept(store)
    monkeypatch.setattr(agreement.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("private failure")))
    with pytest.raises(AgreementRequiredError):
        store.revoke()
    assert store.status() == first
    assert not list(store.directory.glob(".acceptance-*.tmp"))


def test_feedback_attestation_minimal_current_and_pure(store):
    accept(store)
    attestation = store.attestation()
    assert attestation["purpose"] == "feedback_upload"
    assert "signer" not in attestation and "agreement_text" not in attestation and "context" not in attestation
    assert validate_upload_attestation(attestation) == attestation
    assert validate_upload_attestation(attestation) is not attestation
    for field, value in (("purpose", "learn"), ("version", "old"), ("agreement_sha256", "0" * 64),
                         ("acceptance_id", "not-id"), ("context_sha256", "bad"),
                         ("accepted_at", "yesterday"), ("signer", "unexpected")):
        with pytest.raises(AgreementRequiredError):
            validate_upload_attestation({**attestation, field: value})
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    with pytest.raises(AgreementRequiredError):
        validate_upload_attestation({**attestation, "accepted_at": future})


def test_cli_default_show_and_status_do_not_sign(store, monkeypatch, capsys):
    monkeypatch.setattr(AgreementStore, "from_env", classmethod(lambda cls: store))
    assert agreement_cli.main([]) == 0
    assert AGREEMENT_TEXT in capsys.readouterr().out
    assert agreement_cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "required"
    assert not store.state_dir.exists()


def test_cli_piped_acceptance_cannot_sign(store, monkeypatch, capsys):
    monkeypatch.setattr(AgreementStore, "from_env", classmethod(lambda cls: store))
    monkeypatch.setattr(agreement_cli.sys, "stdin", io.StringIO("Synthetic Test User\nI AGREE\n"))
    assert agreement_cli.main(["accept"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "interactive_required"
    assert not store.state_dir.exists()


def terminal(monkeypatch):
    monkeypatch.setattr(agreement_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(agreement_cli.sys.stdout, "isatty", lambda: True)


def test_cli_interactive_acceptance_displays_terms_before_named_confirmation(store, monkeypatch, capsys):
    monkeypatch.setattr(AgreementStore, "from_env", classmethod(lambda cls: store))
    terminal(monkeypatch)
    answers = iter(["Synthetic Test User", "I AGREE"])
    shown = []
    def respond(prompt):
        shown.append(capsys.readouterr().out)
        return next(answers)
    monkeypatch.setattr("builtins.input", respond)
    assert agreement_cli.main(["accept"]) == 0
    assert AGREEMENT_TEXT in shown[0]
    assert store.status()["status"] == "accepted"
    assert agreement_cli.main(["revoke"]) == 0
    assert store.status()["status"] == "revoked"


@pytest.mark.parametrize("answer", ["yes", "I agree", "I AGREE ", ""])
def test_cli_cancel_or_inexact_confirmation_cannot_sign(store, monkeypatch, capsys, answer):
    monkeypatch.setattr(AgreementStore, "from_env", classmethod(lambda cls: store))
    terminal(monkeypatch)
    answers = iter(["Synthetic Test User", answer])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert agreement_cli.main(["accept"]) == 2
    assert "not accepted" in capsys.readouterr().out
    assert not store.state_dir.exists()


def test_cli_changed_destination_during_prompt_requires_reading_again(store, monkeypatch, capsys):
    changed = AgreementStore(store.state_dir, context={**store.context, "feedback_url": "https://changed.example.test"})
    stores = iter([store, changed])
    monkeypatch.setattr(AgreementStore, "from_env", classmethod(lambda cls: next(stores)))
    terminal(monkeypatch)
    answers = iter(["Synthetic Test User", "I AGREE"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert agreement_cli.main(["accept"]) == 2
    assert "agreement_changed" in capsys.readouterr().out
    assert not store.state_dir.exists()


def test_cli_has_no_yes_flag_or_sensitive_argument_echo(capsys):
    with pytest.raises(SystemExit) as error:
        agreement_cli.main(["accept", "--yes", "private-token"])
    assert error.value.code == 2
    assert "private-token" not in capsys.readouterr().err


def test_dynamic_environment_rechecked_on_existing_store(tmp_path, monkeypatch):
    values = {"KNOWLEDGE_LEARNING_STATE_DIR": str(tmp_path / "state")}
    monkeypatch.setattr("infermatrix_copilot.knowledge_service.learning_config.learning_environment", lambda: dict(values))
    store = AgreementStore.from_env()
    accept(store)
    values["KNOWLEDGE_FEEDBACK_URL"] = "https://new.example.test"
    assert store.status()["status"] == "renewal_required"
    assert "KNOWLEDGE_FEEDBACK_TOKEN" not in store.document()["context"]


def test_invalid_destination_never_echoes_secret_or_creates_state(tmp_path):
    store = AgreementStore.from_env(state_dir=tmp_path / "state", environ={"KNOWLEDGE_FEEDBACK_URL": "https://name:secret@example.test"})
    assert store.status()["status"] == "unavailable"
    assert "secret" not in json.dumps(store.status())
    assert not store.state_dir.exists()
    with pytest.raises(AgreementRequiredError):
        store.require("feedback_upload")


def test_symlinked_home_parent_matches_canonical_runtime_state(tmp_path, monkeypatch):
    physical = tmp_path / "physical-home"
    physical.mkdir()
    alias = tmp_path / "home-alias"
    try:
        alias.symlink_to(physical, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(agreement.Path, "home", classmethod(lambda cls: alias))
    store = AgreementStore.from_env(environ={})
    assert store.state_dir == physical / ".infermatrix-copilot" / "learning"
    accepted = accept(store)
    assert AgreementStore(physical / ".infermatrix-copilot" / "learning").status() == accepted


def test_symlink_resolution_loop_fails_closed_without_constructor_error(tmp_path):
    loop = tmp_path / "loop"
    try:
        loop.symlink_to(loop, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    for path in (loop, loop / "nested-state"):
        store = AgreementStore(path)
        assert store.status()["status"] == "unavailable"
        with pytest.raises(AgreementRequiredError):
            store.require("feedback_delivery")
        with pytest.raises(AgreementRequiredError):
            accept(store)
        assert not list(tmp_path.glob("acceptance*"))


def test_bounded_recent_audit_never_blocks_revocation(store, monkeypatch):
    accept(store)
    record = json.loads(store.path.read_text())
    record["audit"] = record["audit"] * 32
    store.path.write_text(agreement._json(record) + "\n")
    monkeypatch.setattr(agreement, "_MAX_RECORD_BYTES", len((agreement._json(record) + "\n").encode()) + 10)
    assert store.revoke()["status"] == "revoked"
    current = json.loads(store.path.read_text())
    assert current["audit"][-1]["action"] == "revoked"
    assert len(current["audit"]) <= 32
