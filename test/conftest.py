import os
import subprocess
from functools import wraps
from pathlib import Path

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.run_trace import RunTrace


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        run_root=tmp_path / "runs",
        playbooks_dir=tmp_path / "playbooks",
        adapters_dir=tmp_path / "adapters",
        repo_paths={},
        allow_push=False,
        rebase_agent_root=tmp_path / "agent_root",  # never the real one in tests
        rebase_poll_interval=1,
        skills_dir=tmp_path / "skills",  # never the shipped skills in tests
        memory_db=tmp_path / "memory.db",
        review_ensemble=False,  # ensemble tests opt in explicitly
        ensemble_parallel=False,  # ordered ScriptedLLM fakes need determinism;
                                  # the parallel path has its own keyed-fake test
        ensemble_samples_per_lens=1,  # sampling tests opt in explicitly
        ensemble_stagger_seconds=0,  # tests never sleep for cache warm-up
        ensemble_zero_yield_retry=False,  # scripted fakes opt in explicitly
        review_verify_comments=False,  # verify-pass tests opt in explicitly
        review_deep_engine=False,  # deep-pass tests opt in explicitly; the
                                   # legacy scripted fakes assume 4 lenses
        review_second_round=False,  # second-round tests opt in explicitly —
                                    # scripted fakes budget exact call counts
    )


@pytest.fixture()
def trace(tmp_path: Path) -> RunTrace:
    return RunTrace(tmp_path / "trace" / "run_trace.jsonl")




MINI_REBASE_PLAYBOOK = """\
# test-only minimal LOCKED repo_rebase playbook (the deleted v2's shape
# minus the external delegation, retired in the 2026-08-25 PR7 purge):
# generic steps only, so planner/CLI/queue/chat flows stay exercised
# without the heavy repo-rebase-v3 pipeline.
name: repo-rebase-mini
version: 1
status: locked
provenance:
  created_by: human
  source: test fixture (post-PR7 cutover)
task_kinds: [repo_rebase]
repos: [vllm-omni]
requires: []
params:
  report_only: {type: bool, default: true}
steps:
  - {id: guard, step: workspace.guard_clean}
  - {id: report, step: report.final_summary}
success: guard + report complete
"""


def install_mini_rebase_playbook(playbooks_dir):
    """Write the minimal locked repo_rebase playbook into a sandbox store."""
    from pathlib import Path
    d = Path(playbooks_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "repo-rebase-mini.yaml").write_text(MINI_REBASE_PLAYBOOK)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A tiny real git repo with one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (repo / "mod_a.py").write_text("A = 1\n")
    (repo / "mod_b.py").write_text("B = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    return repo


@pytest.fixture(autouse=True)
def _no_real_run_roots(tmp_path, monkeypatch):
    """Snapshot pruning looks for runs that pin a snapshot; tests never read
    the real ~/.infermatrix-copilot/runs. Feedback tests also never write a
    user's ledger or contact a configured team collector unless opted in."""
    monkeypatch.setenv("KB_RUN_ROOTS", str(tmp_path / "no-runs"))
    monkeypatch.setenv("KNOWLEDGE_LEARNING_STATE_DIR", str(tmp_path / "learning-state"))
    monkeypatch.setenv("KNOWLEDGE_FEEDBACK_URL", "")
    monkeypatch.setenv("KNOWLEDGE_FEEDBACK_TOKEN", "")


@pytest.fixture
def accept_learning_agreement(tmp_path):
    """Simulate a named signature only inside this test's private directory."""
    from infermatrix_copilot.knowledge_service.agreement import AgreementStore

    def accept(state_dir=None, *, environ=None, feedback_url=None):
        store = AgreementStore.from_env(state_dir=state_dir, environ=environ, feedback_url=feedback_url)
        assert store.state_dir.resolve().is_relative_to(tmp_path.resolve()), "never sign real user state"
        if store.status()["status"] != "accepted":
            document = store.document()
            store._accept("Synthetic Test User", version=document["version"],
                          agreement_sha256=document["sha256"], context_sha256=document["context_sha256"],
                          confirmation="I AGREE")
        return store
    return accept


@pytest.fixture
def accepted_learning_feature(monkeypatch, accept_learning_agreement):
    """Legacy feature suites opt in; agreement tests exercise the closed default.

    These constructors still run production gates. The fixture only supplies
    synthetic, destination-specific signatures in tmp_path, like a signed-in
    test-user fixture; it never changes production checks or environment policy.
    """
    from infermatrix_copilot.knowledge_service.agreement import AgreementRequiredError
    from infermatrix_copilot.knowledge_service.feedback import KnowledgeFeedbackStore
    from infermatrix_copilot.knowledge_service.learning import TaskLearningService
    from infermatrix_copilot.knowledge_service.learning_publication import (
        LearningPublisher,
    )

    accept_learning_agreement()
    for cls in (KnowledgeFeedbackStore, TaskLearningService, LearningPublisher):
        original = cls.__init__

        def wrap(original, cls):
            @wraps(original)
            def initialize(self, *args, **kwargs):
                if cls is LearningPublisher and kwargs.get("state_dir") is None:
                    kwargs["state_dir"] = os.environ["KNOWLEDGE_LEARNING_STATE_DIR"]
                original(self, *args, **kwargs)
                if cls is KnowledgeFeedbackStore and not self._feedback_sync_enabled:
                    return
                options = {"environ": self.environ} if cls is LearningPublisher else {}
                if cls is KnowledgeFeedbackStore and self.sync_config is not None:
                    options["feedback_url"] = self.sync_config.url
                try:
                    accept_learning_agreement(getattr(self, "state_dir", None), **options)
                except AgreementRequiredError:
                    # Invalid deployment destinations cannot be consented to.
                    # Configuration-error tests still exercise that closed path.
                    return
            return initialize
        monkeypatch.setattr(cls, "__init__", wrap(original, cls))
