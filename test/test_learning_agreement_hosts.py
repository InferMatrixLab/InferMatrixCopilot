"""The host must inspect consent before optional learning, never sign it."""

import inspect
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_thin_mcp_server import _fake_mcp

from infermatrix_copilot.knowledge_service import learning_cli
from infermatrix_copilot.knowledge_service.agreement import (
    AgreementRequiredError,
    AgreementStore,
)
from infermatrix_copilot.sdk.v1 import (
    KnowledgeAgreementClient,
    KnowledgeFeedbackClient,
    KnowledgeLearningClient,
)

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "plugins/infermatrix-copilot/skills"


@pytest.fixture
def unsigned_state(monkeypatch, tmp_path):
    state = tmp_path / "unsigned-learning"
    monkeypatch.setenv("KNOWLEDGE_LEARNING_STATE_DIR", str(state))
    return state


def test_sdk_agreement_is_inspection_only_and_default_status_has_no_side_effects(unsigned_state):
    client = KnowledgeAgreementClient()
    assert not unsigned_state.exists()
    status = client.status()
    document = client.document()
    assert status["status"] == "required"
    assert status["agreement_sha256"] == document["sha256"]
    assert document["text"]
    assert not unsigned_state.exists()
    assert {name for name in dir(client) if not name.startswith("_")} == {"document", "status", "revoke"}
    for operation in (client.document, client.status, client.revoke):
        assert not inspect.signature(operation).parameters


@pytest.mark.parametrize("operation", ["prepare", "submit", "record", "sync"])
def test_sdk_refuses_optional_work_before_constructing_runtime(unsigned_state, monkeypatch, operation):
    def forbidden():
        pytest.fail("An unsigned optional operation constructed its runtime")
    learning = KnowledgeLearningClient()
    feedback = KnowledgeFeedbackClient()
    monkeypatch.setattr(learning, "_runtime", forbidden)
    monkeypatch.setattr(feedback, "_store", forbidden)
    calls = {
        "prepare": lambda: learning.prepare(task_id="task", repository="demo", source_pin="a" * 40, lessons=[]),
        "submit": lambda: learning.submit(task_id="task", packet_digest="b" * 64, proposals={"rules": []}),
        "record": lambda: feedback.record(task_id="task", delivery_id="delivery", ratings=[]),
        "sync": feedback.sync,
    }
    with pytest.raises(AgreementRequiredError):
        calls[operation]()
    assert not unsigned_state.exists()


def test_sdk_local_inspection_remains_available_without_consent(unsigned_state, monkeypatch):
    learning = KnowledgeLearningClient()
    feedback = KnowledgeFeedbackClient()
    monkeypatch.setattr(learning, "_runtime", lambda: SimpleNamespace(status=lambda task: {"status": "unknown", "task_id": task}))
    monkeypatch.setattr(feedback, "_store", lambda: SimpleNamespace(
        report_for_scope=lambda **kwargs: {"targets": [], **kwargs},
        sync_status=lambda: {"pending": 0},
    ))
    assert learning.status("task")["status"] == "unknown"
    assert feedback.report(scope="local")["targets"] == []
    assert feedback.sync_status() == {"pending": 0}
    assert not unsigned_state.exists()


def test_sdk_accepted_check_precedes_prepare(monkeypatch, tmp_path):
    events = []
    fake = SimpleNamespace(require=lambda action: events.append(("require", action)))
    monkeypatch.setattr(AgreementStore, "from_env", classmethod(lambda cls, **kwargs: fake))
    client = KnowledgeLearningClient(state_dir=tmp_path)
    monkeypatch.setattr(client, "_runtime", lambda: SimpleNamespace(
        prepare=lambda *args: events.append(("prepare", args)) or {"status": "prepared"},
    ))
    assert client.prepare(task_id="task", repository="demo", source_pin="a" * 40, lessons=[])["status"] == "prepared"
    assert [event[0] for event in events] == ["require", "prepare"]


def test_mcp_has_no_acceptance_or_signature_payload(unsigned_state, monkeypatch):
    server, _ = _fake_mcp(monkeypatch)
    tool = server.tools["knowledge_agreement"]
    assert list(inspect.signature(tool).parameters) == ["action"]
    assert tool()["status"] == "required"
    assert tool(action="show")["text"]
    assert not unsigned_state.exists()
    assert "error" in tool(action="accept")
    with pytest.raises(TypeError):
        tool(action="status", signer="Agent", accepted=True)
    assert not unsigned_state.exists()
    annotations = server.tool_annotations["knowledge_agreement"]
    assert annotations.readOnlyHint is False  # revoke mutates local consent
    assert annotations.openWorldHint is False
    assert tool(action="revoke")["status"] == "revoked"


def test_mcp_learning_refuses_unsigned_request_without_runtime(unsigned_state, monkeypatch):
    server, _ = _fake_mcp(monkeypatch)
    monkeypatch.setattr(KnowledgeLearningClient, "_runtime", lambda self: pytest.fail("unsigned runtime"))
    reply = server.tools["knowledge_learning"](
        action="prepare", task_id="task", repo="demo", source_pin="a" * 40, lessons=[],
    )
    assert reply["status"] == "agreement_required"
    assert reply["human_command"].endswith("learning agreement accept")
    assert not unsigned_state.exists()


def test_cli_agreement_status_and_show_use_human_agreement_interface(unsigned_state, capsys):
    assert learning_cli.main(["agreement", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "required"
    assert learning_cli.main(["agreement", "show"]) == 0
    assert AgreementStore.from_env().document()["text"] in capsys.readouterr().out
    assert not unsigned_state.exists()


def test_cli_will_not_sign_from_a_pipe_or_accept_flag(unsigned_state, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("Agent\nI AGREE\n"))
    monkeypatch.setattr(AgreementStore, "_accept", lambda *args, **kwargs: pytest.fail("agent signed"))
    assert learning_cli.main(["agreement", "accept"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "interactive_required"
    with pytest.raises(SystemExit) as error:
        learning_cli.main(["agreement", "accept", "--yes"])
    assert error.value.code == 2
    assert not unsigned_state.exists()


def test_cli_unsigned_prepare_reports_gate_without_creating_service(unsigned_state, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "task_id": "task", "repository": "demo", "source_pin": "a" * 40, "lessons": [],
    })))
    monkeypatch.setattr(learning_cli, "_learning_service", lambda: pytest.fail("unsigned service"))
    assert learning_cli.main(["prepare", "--input", "-"]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "agreement_required"
    assert not unsigned_state.exists()


def test_every_host_completion_route_checks_agreement_before_learning():
    for skill in SKILLS.glob("im*/SKILL.md"):
        text = skill.read_text(encoding="utf-8")
        completion = text if skill.parent.name == "imlearn" else text.split("## Task completion learning", 1)[1]
        assert completion.index('knowledge_agreement(action="status")') < completion.index("task completion learning](")
        assert 'status="accepted"' in completion
        assert "Never sign" in completion or "Never accept" in completion
    shared = (SKILLS / "imlearn/references/task-completion.md").read_text(encoding="utf-8")
    assert shared.index('knowledge_agreement(action="status")') < shared.index("## Distill locally")
    assert "skip learning and grading" in shared
    assert "Operator configuration alone is not consent" in shared
    assert "Do not repeatedly remind" in " ".join(shared.split())
