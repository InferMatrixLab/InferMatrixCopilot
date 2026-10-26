"""Release campaigns resume their recorded task without changing its identity."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from infermatrix_copilot.app.core import Copilot
from infermatrix_copilot.app.reservation import RunReservation
from infermatrix_copilot.cli import entry
from infermatrix_copilot.notify import BLOCKED_EXIT
from infermatrix_copilot.task_spec import TaskSpec

RELEASE_RUN = "run-20261002-115358-33726c"
NEWER_RUN = "run-20261003-120000-abcdef"
RELEASE_COMMIT = "f42629247d0efcd4f7fd9d0a1cf6fb2060909fc9"


def _save_run(root, run_id, *, repo="vllm-omni", params=None):
    run = root / run_id
    run.mkdir(parents=True)
    spec = TaskSpec(kind="repo_rebase", repo=repo, params=params or {})
    (run / "task.json").write_text(json.dumps({
        "spec": spec.model_dump(),
        "playbook": {"name": "repo-rebase-v3", "status": "locked",
                     "task_kinds": ["repo_rebase"], "steps": []},
    }), encoding="utf-8")
    return run


@pytest.fixture
def resume_context(tmp_path):
    # Exercise selection and assertions without initializing a provider or
    # executing any workflow, repository command, or GPU check.
    copilot = Copilot.__new__(Copilot)
    copilot.settings = SimpleNamespace(run_root=tmp_path / "runs",
                                       tier_target=lambda mode: None)
    copilot.reservations = RunReservation(copilot.settings)
    calls = []

    def execute(playbook, spec, run_dir, **kwargs):
        calls.append((playbook, spec, run_dir, kwargs))
        return 0

    copilot._execute = execute
    return copilot, calls


def test_explicit_resume_selects_release_despite_unrelated_newer_run(resume_context):
    copilot, calls = resume_context
    root = copilot.settings.run_root
    release = _save_run(root, RELEASE_RUN,
                        params={"force_upstream_commit": RELEASE_COMMIT})
    _save_run(root, NEWER_RUN, repo="other-repo")

    assert copilot.resume_last(RELEASE_RUN) == 0
    assert len(calls) == 1
    assert calls[0][2] == release
    assert calls[0][1].params["force_upstream_commit"] == RELEASE_COMMIT
    assert calls[0][3] == {"resuming": True}


def test_bare_resume_still_selects_latest(resume_context):
    copilot, calls = resume_context
    root = copilot.settings.run_root
    _save_run(root, RELEASE_RUN)
    newer = _save_run(root, NEWER_RUN, repo="other-repo")

    assert copilot.resume_last() == 0
    assert calls[0][2] == newer
    assert calls[0][1].repo == "other-repo"


@pytest.mark.parametrize("constraints", [
    {"repo": "other-repo"},
    {"playbook": "repo-rebase-native-v1"},
    {"params": {"force_upstream_commit": "d" * 40}},
    {"params": {"missing_param": True}},
    {"params": {"retry_limit": True}},  # bool must not match the integer 1
])
def test_mismatched_identity_refuses_without_execution_or_rewriting(
        resume_context, constraints, capsys):
    copilot, calls = resume_context
    run = _save_run(copilot.settings.run_root, RELEASE_RUN,
                    params={"force_upstream_commit": RELEASE_COMMIT,
                            "retry_limit": 1})
    original = (run / "task.json").read_bytes()

    assert copilot.resume_last(RELEASE_RUN, **constraints) == BLOCKED_EXIT
    assert calls == []
    assert (run / "task.json").read_bytes() == original
    assert "saved task does not match" in capsys.readouterr().out


def test_matching_identity_assertions_preserve_saved_spec(resume_context):
    copilot, calls = resume_context
    saved_params = {"force_upstream_commit": RELEASE_COMMIT,
                    "rebase_mode": "full", "retry_limit": 1}
    run = _save_run(copilot.settings.run_root, RELEASE_RUN, params=saved_params)
    original = (run / "task.json").read_bytes()

    assert copilot.resume_last(
        RELEASE_RUN, repo="vllm-omni", playbook="repo-rebase-v3",
        params={"force_upstream_commit": RELEASE_COMMIT}) == 0
    assert calls[0][1].params == saved_params
    assert (run / "task.json").read_bytes() == original


@pytest.mark.parametrize("contents", ["{bad json", "null", "{}", '{"spec": {}}',
                                      '{"spec": {"kind": "repo_rebase"}, "playbook": null}'])
def test_explicit_malformed_task_fails_without_falling_back(
        resume_context, contents, capsys):
    copilot, calls = resume_context
    run = _save_run(copilot.settings.run_root, RELEASE_RUN)
    (run / "task.json").write_text(contents, encoding="utf-8")
    _save_run(copilot.settings.run_root, NEWER_RUN)

    assert copilot.resume_last(RELEASE_RUN) == BLOCKED_EXIT
    assert calls == []
    assert "invalid task.json" in capsys.readouterr().out


def test_explicit_missing_task_fails_without_falling_back(resume_context, capsys):
    copilot, calls = resume_context
    root = copilot.settings.run_root
    (root / RELEASE_RUN).mkdir(parents=True)
    _save_run(root, NEWER_RUN)

    assert copilot.resume_last(RELEASE_RUN) == BLOCKED_EXIT
    assert calls == []
    assert "task.json is missing" in capsys.readouterr().out


@pytest.mark.parametrize("run_id", ["../outside", "/tmp/outside", "run-invalid",
                                    "run-20261002-115358-ffffff"])
def test_explicit_invalid_or_missing_id_blocks(resume_context, run_id):
    copilot, calls = resume_context
    assert copilot.resume_last(run_id) == BLOCKED_EXIT
    assert calls == []


def test_explicit_symlink_outside_run_root_blocks(resume_context, tmp_path):
    copilot, calls = resume_context
    outside = _save_run(tmp_path / "outside", RELEASE_RUN)
    root = copilot.settings.run_root
    root.mkdir()
    (root / RELEASE_RUN).symlink_to(outside, target_is_directory=True)

    assert copilot.resume_last(RELEASE_RUN) == BLOCKED_EXIT
    assert calls == []


def test_explicit_symlink_loop_blocks_cleanly(resume_context, capsys):
    copilot, calls = resume_context
    root = copilot.settings.run_root
    root.mkdir()
    run = root / RELEASE_RUN
    run.symlink_to(run, target_is_directory=True)

    assert copilot.resume_last(RELEASE_RUN) == BLOCKED_EXIT
    assert calls == []
    assert "cannot resume" in capsys.readouterr().out


@pytest.mark.parametrize("run_args,selected", [([], ""), ([RELEASE_RUN], RELEASE_RUN)])
def test_cli_forwards_run_selection_and_identity_assertions(
        tmp_path, monkeypatch, run_args, selected):
    calls = []
    fake = SimpleNamespace(resume_last=lambda *args, **kwargs: calls.append(
        (args, kwargs)) or 17)
    monkeypatch.setattr(entry, "Copilot", lambda: fake)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("IMX_INVOCATION_ID", raising=False)

    assert entry.main(["--resume", *run_args, "--repo", "vllm-omni",
                       "--playbook", "repo-rebase-v3", "--task-param",
                       f"force_upstream_commit={RELEASE_COMMIT}",
                       "--task-param", "retry_limit=1"]) == 17
    assert calls == [((selected,), {"repo": "vllm-omni",
                                   "playbook": "repo-rebase-v3",
                                   "params": {"force_upstream_commit": RELEASE_COMMIT,
                                              "retry_limit": 1}})]
