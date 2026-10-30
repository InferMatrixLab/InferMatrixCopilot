from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import pytest

from infermatrix_copilot.app.core import Copilot
from infermatrix_copilot.app.repository_context import RepositoryContextResolver
from infermatrix_copilot.engine.lifecycle import RunLock
from infermatrix_copilot.notify import BLOCKED_EXIT
from infermatrix_copilot.task_spec import TaskSpec


def _adapter(settings, checkout: Path, *, branches: str = "[main]") -> Path:
    root = settings.adapters_dir / "vllm_omni"
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "manifest.yaml"
    manifest.write_text(
        "name: vllm_omni\nstatus: active\nrepo:\n"
        f"  path: {checkout}\n"
        "modules:\n  scheduler:\n    risk: high\n"
        f"push:\n  protected_branches: {branches}\n"
    )
    return manifest


def test_planning_and_execution_context_share_checkout_and_adapter_policy(
    settings, tmp_path,
):
    ambient = tmp_path / "ambient"
    frozen = tmp_path / "frozen"
    _adapter(settings, ambient, branches="[main, release]")
    spec = TaskSpec(kind="repo_rebase", repo="vllm-omni", repo_path=str(frozen))

    context = RepositoryContextResolver(settings).for_spec(spec)

    assert context.repo_path == str(frozen)
    assert "repo.path" in context.capabilities
    assert context.protected_branches == ("main", "release")
    assert context.high_risk_modules == ("scheduler",)


@pytest.mark.parametrize("manifest", (
    "{not yaml: [",
    "name: vllm_omni\nrepo: []\n",
    "name: vllm_omni\nrepo: {}\npush: []\n",
    "name: vllm_omni\nrepo:\n  path: 123\n",
    "name: vllm_omni\nrepo: {}\ncapabilities: reviewer\n",
    "name: vllm_omni\nrepo: {}\nmodules: []\n",
    "name: vllm_omni\nrepo: {}\npush:\n  protected_branches: main\n",
))
def test_known_broken_adapter_blocks_execution_before_run_artifacts(
    settings, tmp_path, manifest,
):
    bad = settings.adapters_dir / "vllm_omni"
    bad.mkdir(parents=True)
    (bad / "manifest.yaml").write_text(manifest)
    copilot = Copilot(settings)
    run_dir = tmp_path / "run-bad-adapter"

    code = copilot._execute(None, TaskSpec(kind="repo_rebase"), run_dir)

    assert code == BLOCKED_EXIT
    assert "repository context invalid" in copilot.last_blocked_reason
    assert not run_dir.exists()
    assert copilot.run_task(TaskSpec(kind="repo_rebase")) == BLOCKED_EXIT


def test_adapter_policy_change_after_planning_blocks_execution(
    settings, tmp_path,
):
    manifest = _adapter(settings, tmp_path / "checkout")
    spec = TaskSpec(kind="repo_rebase", repo="vllm-omni")
    copilot = Copilot(settings)
    planned = copilot.repository_context.for_spec(spec)
    manifest.write_text(manifest.read_text().replace("[main]", "[main, release]"))
    run_dir = tmp_path / "run-changed-policy"

    code = copilot._execute(None, spec, run_dir, planned_context=planned)

    assert code == BLOCKED_EXIT
    assert copilot.last_blocked_reason == "repository context changed after planning"
    assert not run_dir.exists()


def test_unresolved_adapter_path_does_not_claim_checkout_capability(
    settings, tmp_path, monkeypatch,
):
    monkeypatch.delenv("IMX_TEST_MISSING_REPOSITORY_PATH", raising=False)
    _adapter(settings, tmp_path / "unused")
    manifest = settings.adapters_dir / "vllm_omni" / "manifest.yaml"
    manifest.write_text(manifest.read_text().replace(
        str(tmp_path / "unused"), "${IMX_TEST_MISSING_REPOSITORY_PATH}"))

    context = RepositoryContextResolver(settings).for_spec(
        TaskSpec(kind="repo_rebase", repo="vllm-omni"))

    assert context.repo_path == ""
    assert "repo.path" not in context.capabilities


def test_workflow_execution_seeds_context_and_returns_blocked_reason(
    settings, tmp_path, monkeypatch,
):
    _adapter(settings, tmp_path / "ambient", branches="[main, release]")
    frozen = tmp_path / "frozen"
    spec = TaskSpec(kind="repo_rebase", repo_path=str(frozen))
    seen = {}

    class FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, playbook, state):
            seen.update(state)
            return SimpleNamespace(
                status="blocked", blocked_reason="fixture stop", step_results={},
            )

    monkeypatch.setattr(
        "infermatrix_copilot.app.workflow_execution.Executor", FakeExecutor,
    )
    copilot = Copilot(settings)
    run_dir = tmp_path / "run-context"

    code = copilot._execute(
        SimpleNamespace(name="fixture", version=1), spec, run_dir,
        planned_context=copilot.repository_context.for_spec(spec),
    )

    assert code == BLOCKED_EXIT
    assert copilot.last_blocked_reason == "fixture stop"
    assert seen["repo_path"] == str(frozen)
    assert seen["protected_branches"] == ["main", "release"]
    assert seen["high_risk_modules"] == ["scheduler"]
    with RunLock(run_dir):
        pass


def test_workflow_execution_import_does_not_load_transports():
    code = (
        "import sys\n"
        "import infermatrix_copilot.app.workflow_execution\n"
        "assert not any(name.startswith(('infermatrix_copilot.cli', "
        "'infermatrix_copilot.mcp', 'infermatrix_copilot.thin_mcp')) "
        "for name in sys.modules)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_checkout_change_while_waiting_for_execution_lock_blocks(
    settings, tmp_path, monkeypatch,
):
    original = tmp_path / "original-checkout"
    changed = tmp_path / "changed-checkout"
    manifest = _adapter(settings, original)
    copilot = Copilot(settings)
    spec = TaskSpec(kind="repo_rebase", repo="vllm-omni")
    run_dir = tmp_path / "run-context-changed-while-queued"

    @contextmanager
    def queued_checkout_lock(checkout, pr):
        assert checkout == str(original)
        # Another session updates the adapter while this run waits for the
        # previous checkout's shared execution lock. Resumes have no planning
        # context, so the validated lock context must become their assertion.
        manifest.write_text(manifest.read_text().replace(str(original), str(changed)))
        yield

    monkeypatch.setattr("infermatrix_copilot.app.core.execution_lock", queued_checkout_lock)
    code = copilot._execute(None, spec, run_dir, resuming=True)

    assert code == BLOCKED_EXIT
    assert copilot.last_blocked_reason == "repository context changed after planning"
    assert not (run_dir / "run_trace.jsonl").exists()
    with RunLock(run_dir):
        pass
