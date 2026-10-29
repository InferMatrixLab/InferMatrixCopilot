"""Installed completion routing and the JSON CLI's service boundary."""

import importlib.util
import io
import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("accepted_learning_feature")

from infermatrix_copilot.knowledge_service import learning_cli

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "plugins" / "infermatrix-copilot" / "skills"


def _installer():
    spec = importlib.util.spec_from_file_location(
        "learning_hosts_installer", ROOT / "scripts" / "install_mcp.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("host_root", [".agents", ".claude", ".cursor"])
def test_installed_workflows_resolve_one_completion_contract(tmp_path, host_root):
    destination = tmp_path / host_root / "skills"
    _installer()._install_skills(destination)
    workflows = sorted(SKILLS.glob("im*/SKILL.md"))
    assert {p.parent.name for p in workflows} >= {
        "imreview", "imcoding", "imcifix", "imdesign", "imrfc", "imupdate", "imkbinit", "imlearn",
    }
    for source in workflows:
        installed = destination / source.parent.name / "SKILL.md"
        text = installed.read_text(encoding="utf-8")
        match = re.search(r"\[task completion learning\]\(([^)]+)\)", text)
        assert match is not None, f"{source.parent.name} has no completion route"
        contract = (installed.parent / match[1]).resolve()
        assert contract.is_relative_to(destination.resolve())
        assert contract.read_bytes() == (
            SKILLS / "imlearn" / "references" / "task-completion.md"
        ).read_bytes()
    assert "$imlearn" in (destination / "imlearn" / "SKILL.md").read_text(encoding="utf-8")


@pytest.fixture
def services(monkeypatch):
    calls = []

    def operation(name):
        def execute(*args, **kwargs):
            calls.append((name, args, kwargs))
            return {"operation": name, "status": "retained"}
        return execute

    learning = SimpleNamespace(**{name: operation(name) for name in ("prepare", "submit", "status")})
    feedback = SimpleNamespace(**{name: operation(name) for name in (
        "record", "report_for_scope", "sync_pending", "sync_status")})
    for module_name, class_name, service in (
        ("learning", "TaskLearningService", learning),
        ("feedback", "KnowledgeFeedbackStore", feedback),
    ):
        module = ModuleType(f"infermatrix_copilot.knowledge_service.{module_name}")
        setattr(module, class_name, SimpleNamespace(from_env=lambda service=service: service))
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return calls, learning, feedback


def _input(monkeypatch, tmp_path, value, stdin):
    text = json.dumps(value)
    if stdin:
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))
        return "-"
    path = tmp_path / "input.json"
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("stdin", [False, True])
def test_prepare_delegates_bound_packet_with_empty_lessons(
    monkeypatch, tmp_path, capsys, services, stdin
):
    path = _input(monkeypatch, tmp_path, {
        "task_id": "task-1", "repo": "vllm-omni", "source_pin": "a" * 40, "lessons": [],
    }, stdin)
    assert learning_cli.main(["prepare", "--input", path]) == 0
    assert services[0] == [("prepare", (), {
        "task_id": "task-1", "repository": "vllm-omni", "source_pin": "a" * 40, "lessons": [],
    })]
    assert json.loads(capsys.readouterr().out)["operation"] == "prepare"


def test_submit_preserves_the_prepared_identity(monkeypatch, tmp_path, services):
    proposals = {"rules": []}
    path = _input(monkeypatch, tmp_path, {
        "task_id": "task-1", "packet_digest": "b" * 64, "proposals": proposals,
    }, False)
    assert learning_cli.main(["submit", "--input", path]) == 0
    assert services[0] == [("submit", (), {
        "task_id": "task-1", "packet_digest": "b" * 64, "proposals": proposals,
    })]


def test_feedback_preserves_receipt_targets_and_evidence(monkeypatch, tmp_path, services):
    ratings = [{"target_id": "rule-1", "score": -1, "used": True,
                "reason": "stale API", "evidence": "test pin"}]
    path = _input(monkeypatch, tmp_path, {
        "task_id": "task-1", "delivery_id": "delivery-1", "ratings": ratings,
    }, True)
    assert learning_cli.main(["feedback", "--input", path]) == 0
    assert services[0] == [("record", (), {
        "task_id": "task-1", "delivery_id": "delivery-1", "ratings": ratings,
    })]


def test_status_and_report_delegate_without_starting_learning(services):
    assert learning_cli.main(["status", "task-1"]) == 0
    assert learning_cli.main(["report", "--repo", "vllm-omni", "--limit", "7"]) == 0
    assert services[0] == [
        ("status", ("task-1",), {}),
        ("report_for_scope", (), {"repository": "vllm-omni", "limit": 7, "scope": "auto"}),
    ]


def test_feedback_retry_status_and_explicit_report_scope(services):
    assert learning_cli.main(["sync", "--limit", "5"]) == 0
    assert learning_cli.main(["sync-status"]) == 0
    assert learning_cli.main(["report", "--scope", "local"]) == 0
    assert services[0] == [
        ("sync_pending", (), {"limit": 5}),
        ("sync_status", (), {}),
        ("report_for_scope", (), {"repository": "", "limit": 100, "scope": "local"}),
    ]


@pytest.mark.parametrize("extra", ["transcript", "token", "workspace", "service_url"])
def test_cli_rejects_unscoped_input_before_service_access(
    monkeypatch, tmp_path, services, capsys, extra
):
    path = _input(monkeypatch, tmp_path, {
        "task_id": "task-1", "repository": "vllm-omni", "source_pin": "a" * 40,
        "lessons": [], extra: "private-secret",
    }, False)
    assert learning_cli.main(["prepare", "--input", path]) == 2
    assert services[0] == []
    result = capsys.readouterr().out
    assert "private-secret" not in result
    assert json.loads(result)["error"]["code"] == "invalid_input"


def test_service_error_never_echoes_provider_details(monkeypatch, services, capsys):
    def fail(task_id):
        raise RuntimeError("token=private-secret from provider output")
    services[1].status = fail
    assert learning_cli.main(["status", "task-1"]) == 1
    output = capsys.readouterr().out
    assert "private-secret" not in output
    assert json.loads(output)["error"]["code"] == "learning_failed"


@pytest.mark.parametrize("raw", ['{"lessons": "private-secret"', '["private-secret"]'])
def test_malformed_input_is_bounded_and_does_not_echo_content(monkeypatch, services, capsys, raw):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    assert learning_cli.main(["prepare", "--input", "-"]) == 2
    assert services[0] == []
    assert "private-secret" not in capsys.readouterr().out


def test_oversized_input_is_rejected_before_service_access(monkeypatch, services, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(" " * (learning_cli._MAX_INPUT_BYTES + 1)))
    assert learning_cli.main(["prepare", "--input", "-"]) == 2
    assert services[0] == []
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"


def test_argument_errors_do_not_echo_unexpected_credentials(capsys):
    with pytest.raises(SystemExit) as exc:
        learning_cli.main(["report", "--token", "private-secret"])
    assert exc.value.code == 2
    assert "private-secret" not in capsys.readouterr().err


def test_cli_empty_task_round_trips_through_real_service(monkeypatch, tmp_path, capsys, accept_learning_agreement):
    monkeypatch.setenv("KNOWLEDGE_LEARNING_STATE_DIR", str(tmp_path / "state"))
    accept_learning_agreement()
    payload = tmp_path / "task.json"
    payload.write_text(json.dumps({
        "task_id": "completed-with-no-new-lesson", "repository": "vllm-omni",
        "source_pin": "a" * 40, "lessons": [],
    }), encoding="utf-8")
    assert learning_cli.main(["prepare", "--input", str(payload)]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["status"] == "no_op"
    assert learning_cli.main(["status", "completed-with-no-new-lesson"]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["packet_digest"] == prepared["packet_digest"]
    assert restored["status"] == "no_op"
