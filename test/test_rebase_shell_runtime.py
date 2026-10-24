"""Agent shell tools must preserve the runtime selected for the rebase."""

import json
import os
import shlex
import subprocess
import sys

import pytest

from infermatrix_copilot.rebase_engine.rebase_tools import (
    RebasePaths,
    build_rebase_tools,
)


@pytest.mark.parametrize("nested_process", [False, True])
def test_rebase_shell_ssh_startup_preserves_target_cli(tmp_path, nested_process):
    home = tmp_path / "home"
    target = tmp_path / "runtime" / "bin"
    foreign = home / ".local" / "bin"
    target.mkdir(parents=True)
    foreign.mkdir(parents=True)
    for directory, result in [(target, "target"), (foreign, "foreign")]:
        cli = directory / "probe-cli"
        cli.write_text(f"#!/bin/sh\nprintf '%s\\n' '{result}'\n")
        cli.chmod(0o755)
    (home / ".bashrc").write_text('export PATH="$HOME/.local/bin:$PATH"\n')
    env = dict(os.environ, HOME=str(home),
               PATH=str(target) + os.pathsep + os.environ["PATH"],
               SSH_CLIENT="127.0.0.1 12345 22", SHLVL="0")
    env.pop("BASH_ENV", None)
    command = "probe-cli"
    if nested_process:
        # Verification often launches a subprocess that resolves the CLI
        # afresh, as the benchmark regression did in the v0.31 campaign.
        probe = "import subprocess; subprocess.run(['probe-cli'], check=True)"
        command = shlex.quote(sys.executable) + " -c " + shlex.quote(probe)

    control = subprocess.run(["bash", "-c", command], env=env,
                             capture_output=True, text=True, check=True)
    assert control.stdout.strip() == "foreign"
    tools = build_rebase_tools(
        [{"name": "run_shell", "description": "Run a command",
          "input_schema": {"type": "object"}}],
        RebasePaths(omni_path=str(tmp_path), vllm_path=str(tmp_path), env=env),
    )
    result = json.loads(tools["run_shell"].handler(command=command, timeout=10))
    assert result["exit_code"] == 0, result
    assert result["stdout"].strip() == "target"
    assert result["stderr"] == ""


def test_ssh_bash_startup_cannot_override_target_cli_for_setup_or_tests(tmp_path):
    from infermatrix_copilot.testing.runner import TestJob, TestRunner

    home = tmp_path / "home"
    target = tmp_path / "runtime" / "bin"
    foreign = home / ".local" / "bin"
    target.mkdir(parents=True)
    foreign.mkdir(parents=True)
    for directory, result in [(target, "target"), (foreign, "foreign")]:
        cli = directory / "probe-cli"
        cli.write_text(f"#!/bin/sh\nprintf '%s\\n' '{result}'\n")
        cli.chmod(0o755)
    (home / ".bashrc").write_text('export PATH="$HOME/.local/bin:$PATH"\n')
    env = dict(os.environ, HOME=str(home), PATH=str(target) + os.pathsep + os.environ["PATH"],
               SSH_CLIENT="127.0.0.1 12345 22", SHLVL="0", CUDA_VISIBLE_DEVICES="")
    env.pop("BASH_ENV", None)
    # Reproduce the original failure through Bash's real SSH startup behavior.
    control = subprocess.run(["bash", "-c", "probe-cli"], env=env,
                             capture_output=True, text=True, check=True)
    assert control.stdout.strip() == "foreign"
    runner = TestRunner(repo_root=tmp_path, tests_dir=tmp_path / "logs")
    job = TestJob(key="cli", setup="probe-cli > setup-cli.txt",
                  command="probe-cli > test-cli.txt", timeout_sec=10,
                  min_gpus=0, gpu_lock=False)
    outcome = runner.run(job, env)
    assert outcome.rc == 0
    assert (tmp_path / "setup-cli.txt").read_text().strip() == "target"
    assert (tmp_path / "test-cli.txt").read_text().strip() == "target"
