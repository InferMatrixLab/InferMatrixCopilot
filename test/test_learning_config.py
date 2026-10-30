from infermatrix_copilot.config import Settings
from infermatrix_copilot.knowledge_service.feedback import KnowledgeFeedbackStore
from infermatrix_copilot.knowledge_service.learning import TaskLearningService
from infermatrix_copilot.knowledge_service.learning_config import learning_environment
from infermatrix_copilot.knowledge_service.learning_publication import LearningPublisher


def test_dotenv_options_reach_services_without_exporting_credentials(tmp_path, monkeypatch):
    first, second = tmp_path / "source.env", tmp_path / "user.env"
    state = tmp_path / "learning-state"
    workspace = tmp_path / "knowledge-checkout"
    first.write_text("KNOWLEDGE_LEARNING_PUBLICATION=local\nALLOW_PUSH=0\n")
    second.write_text(f"KNOWLEDGE_LEARNING_STATE_DIR={state}\n"
                      f"KNOWLEDGE_LEARNING_WORKSPACE={workspace}\n"
                      "KNOWLEDGE_LEARNING_PUBLICATION=draft_pr\nALLOW_PUSH=1\nALLOW_POST=1\n"
                      "ANTHROPIC_API_KEY=private-model-value\n")
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": (str(first), str(second))})
    for key in ("KNOWLEDGE_LEARNING_STATE_DIR", "KNOWLEDGE_LEARNING_WORKSPACE",
                "KNOWLEDGE_LEARNING_PUBLICATION", "ALLOW_PUSH", "ALLOW_POST"):
        monkeypatch.delenv(key, raising=False)
    options = learning_environment()
    assert options["KNOWLEDGE_LEARNING_PUBLICATION"] == "draft_pr"
    assert "ANTHROPIC_API_KEY" not in options
    assert TaskLearningService.from_env().state_dir == state
    assert TaskLearningService.from_env().workspace_root == workspace
    assert KnowledgeFeedbackStore.from_env().state_dir == state
    assert LearningPublisher().environ["ALLOW_PUSH"] == "1"
    assert LearningPublisher({}).environ == {}
    monkeypatch.setenv("KNOWLEDGE_LEARNING_PUBLICATION", "local")
    assert learning_environment()["KNOWLEDGE_LEARNING_PUBLICATION"] == "local"
