"""Live module prompts carry this run's campaign, including resumed runs."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from infermatrix_copilot.rebase_engine import module_rebase
from infermatrix_copilot.rebase_engine.module_rebase import ModuleRunConfig
from infermatrix_copilot.rebase_engine.prompt_builder import (
    ModulePromptData,
    build_module_prompt,
)
from infermatrix_copilot.rebase_engine.substate import Substate

REBASE_DATA = Path(__file__).resolve().parents[1] / "adapters/vllm_omni/rebase"


@pytest.fixture
def upstream(tmp_path):
    root = tmp_path / "upstream"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Campaign test",
                    "-c", "user.email=campaign@example.invalid", "commit",
                    "-q", "--allow-empty", "-m", "baseline"],
                   cwd=root, check=True)
    return root


def _head(root):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                          capture_output=True, text=True, check=True).stdout.strip()


def _module_prompt(tmp_path, upstream, monkeypatch, **config_kwargs):
    prompts = []

    async def capture(client, prompt, **kwargs):
        prompts.append(prompt)
        return {"done": True, "text": "verified", "turns": 1, "plan_done": True}

    monkeypatch.setattr(module_rebase, "run_agent_loop", capture)
    config = ModuleRunConfig(
        vllm_path=str(upstream), omni_path=str(upstream),
        script_dir=str(REBASE_DATA), model="test-model",
        log_dir=str(tmp_path / "logs"), **config_kwargs)
    substate = Substate(tmp_path / "run", "campaign-test")
    outcome = asyncio.run(module_rebase.rebase_module(
        "worker_runner", client=None, config=config,
        prompt_data=ModulePromptData.load(REBASE_DATA),
        tool_defs=[], extra_tools={}, substate=substate,
        module_test_plan={"upstream_changes": [
            {"type": "modified", "path": "tests/test_runtime.py"}]}))
    assert outcome["status"] == "done"
    assert substate.get("modules.worker_runner.status") == "done"
    assert len(prompts) == 1
    return prompts[0]


@pytest.mark.parametrize("branch", ["main", "releases/v0.32.0"])
def test_module_prompt_uses_run_pin_and_baselines(
        tmp_path, upstream, monkeypatch, branch):
    """Exercise the production config → module → adapter prompt path.

    A detached checkout is common during rebase. A conflicting task parameter
    or checkout HEAD must not replace the pin already resolved in run state.
    """
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=upstream, check=True)
    pin = "f" * 40
    baseline = _head(upstream)
    prompt = _module_prompt(
        tmp_path, upstream, monkeypatch,
        last_rebase_vllm_commit=baseline, baseline_ref="fork/trunk",
        state_slice={"target_branch": branch, "upstream_commit": pin,
                     "task_spec": {"params": {
                         "target_branch": "other-branch",
                         "upstream_commit": "e" * 40,
                         "last_rebase_commit": "d" * 40}}})

    assert f"targets upstream `{branch}`, pinned at `{pin}`" in prompt
    assert f"module assignment baseline is `{baseline}`" in prompt
    assert f"commit: {baseline[:7]}" in prompt
    assert "preserving vllm-omni intent from `fork/trunk`" in prompt
    assert "git show fork/trunk:<path>" in prompt
    assert "compare with fork/trunk" in prompt
    assert "origin/main" not in prompt
    assert "other-branch" not in prompt
    assert "releases/v0.31.0" not in prompt
    assert "ac9126e58aa7bbab1856ba6593ba4d5003fea516" not in prompt
    assert "Module agents run one at a time" in prompt
    assert "Completed\nmodules and resumed attempts may leave dirty files" in prompt
    assert "concurrently" not in prompt
    assert "parallel wave issue" not in prompt
    for token in ("TARGET_BRANCH", "UPSTREAM_COMMIT", "BASELINE_REF",
                  "LAST_REBASE_UPSTREAM_COMMIT"):
        assert "{" + token + "}" not in prompt


def test_module_prompt_can_resume_from_task_parameters(tmp_path, upstream, monkeypatch):
    baseline = _head(upstream)
    pin = "b" * 40
    prompt = _module_prompt(
        tmp_path, upstream, monkeypatch,
        state_slice={"task_spec": {"params": {
            "target_branch": "releases/v0.33.0",
            "force_upstream_commit": pin, "last_rebase_commit": baseline}}})
    assert f"targets upstream `releases/v0.33.0`, pinned at `{pin}`" in prompt
    assert f"module assignment baseline is `{baseline}`" in prompt


@pytest.mark.parametrize("detached", [False, True])
def test_legacy_live_caller_uses_checkout_facts(upstream, detached):
    if detached:
        subprocess.run(["git", "checkout", "-q", "--detach"],
                       cwd=upstream, check=True)
    prompt = build_module_prompt(
        "worker_runner", ModulePromptData.load(REBASE_DATA),
        vllm_path=str(upstream), omni_path=str(upstream),
        script_dir=str(REBASE_DATA), live=True)
    branch = "unspecified (detached checkout)" if detached else "main"
    assert f"targets upstream `{branch}`, pinned at `{_head(upstream)}`" in prompt
    assert "module assignment baseline is `not supplied`" in prompt
    assert "releases/v0.31.0" not in prompt


def test_campaign_arguments_do_not_change_parity_prompt():
    calls = []

    def git(args, cwd):
        calls.append(args)
        return "test-head" if args[0] == "rev-parse" else ""

    kwargs = {"vllm_path": "/vllm", "omni_path": "/omni",
              "script_dir": "/adapter", "run_git": git}
    data = ModulePromptData.load(REBASE_DATA)
    legacy = build_module_prompt("worker_runner", data, **kwargs)
    campaign = build_module_prompt("worker_runner", data, **kwargs,
                                   target_branch="releases/v0.32.0",
                                   upstream_commit="f" * 40)
    assert campaign == legacy
    assert calls == [["rev-parse", "--short", "HEAD"]] * 4


@pytest.mark.parametrize("target_branch", ["releases/v0.32.0", None])
def test_assembly_passes_manifest_campaign_into_module_prompt(
        tmp_path, upstream, settings, trace, monkeypatch, target_branch):
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import rebase_v3

    settings.adapters_dir = REBASE_DATA.parents[1]
    manifest = yaml.safe_load((REBASE_DATA.parent / "manifest.yaml").read_text())
    manifest["repo"].update({"remote": "fork", "default_branch": "trunk"})
    if target_branch:
        manifest["upstream"]["target_branch"] = target_branch
    else:
        manifest["upstream"].pop("target_branch", None)
    monkeypatch.setattr(rebase_v3, "_adapter_manifest", lambda ctx: manifest)
    monkeypatch.setattr(rebase_v3, "_ensure_checkout_locks", lambda *args: None)
    monkeypatch.setattr(rebase_v3, "_tier_client", lambda ctx: (
        object(), SimpleNamespace(model="test-model")))
    monkeypatch.setattr(rebase_v3, "_build_backends", lambda *args: None)
    monkeypatch.setattr(rebase_v3, "_agent_shell_env", lambda *args: {})
    configs, prompts = [], []
    real_module = module_rebase.rebase_module

    async def capture_module(module, **kwargs):
        configs.append(kwargs["config"])
        return await real_module(module, **kwargs)

    async def capture_loop(client, prompt, **kwargs):
        prompts.append(prompt)
        return {"done": True, "text": "verified", "turns": 1, "plan_done": True}

    monkeypatch.setattr(module_rebase, "rebase_module", capture_module)
    monkeypatch.setattr(module_rebase, "run_agent_loop", capture_loop)
    run_dir = tmp_path / "assembly-run"
    run_dir.mkdir()
    (run_dir / "test_manifest.json").write_text(json.dumps({"module_plans": {}}))
    pin = _head(upstream)
    context = StepContext(
        settings=settings, params={}, run_dir=run_dir, trace=trace,
        item="worker_runner", state={
            "task_spec": {"repo": "vllm-omni", "params": {}},
            "run_id": "assembly-campaign", "repo_path": str(upstream),
            "upstream_path": str(upstream), "upstream_commit": pin,
            "last_rebase_upstream_commit": pin})

    result = asyncio.run(rebase_v3._v3_module_rebase(context))

    assert result.ok, result.summary
    assert len(configs) == len(prompts) == 1
    branch = target_branch or "trunk"
    assert configs[0].state_slice["target_branch"] == branch
    assert configs[0].state_slice["upstream_commit"] == pin
    assert configs[0].state_slice["last_rebase_upstream_commit"] == pin
    assert f"targets upstream `{branch}`, pinned at `{pin}`" in prompts[0]
    assert f"module assignment baseline is `{pin}`" in prompts[0]
    assert "preserving vllm-omni intent from `fork/trunk`" in prompts[0]
