"""GPUs on demand from the scheduler: `testing/gpu_demand.py`, `TestRunner` and
the agents' shell in demand mode, and the module test wrapper.

The scheduler is a stand-in `gpu` executable. It records the request, prints
what the real one prints on this machine (including the booking warning that
matches the watchdog's `failed to` pattern), sets CUDA_VISIBLE_DEVICES and runs
the command, or reports an expired wait. No card is involved.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from infermatrix_copilot.rebase_engine import module_pytest
from infermatrix_copilot.rebase_engine.rebase_tools import (
    RebasePaths,
    build_rebase_tools,
)
from infermatrix_copilot.testing import gpu_demand
from infermatrix_copilot.testing.gpu_demand import (
    DEFAULT_WAIT,
    IDLE_MAX_SEC,
    NO_GPU_RC,
    SCHEDULER_TIMEOUT_MARGIN_SEC,
    GpuDemand,
    command_gpus,
    duration_seconds,
    granted_gpus,
    idle_timeout,
    prompt_devices,
    scheduler_error,
    scheduler_wrap,
    session_devices,
    shell_needs_gpu,
)
from infermatrix_copilot.testing.runner import TestJob, TestRunner

_STUB = r"""#!/bin/sh
# A stand-in for canhazgpu `run`.
gpus=""
while [ $# -gt 0 ]; do
    case "$1" in
        --gpus) gpus="$2"; shift 2 ;;
        --) shift; break ;;
        *) shift ;;
    esac
done
printf 'gpus=%s\n' "$gpus" >> "$IMX_GPU_STUB_LOG"
if [ -f "$(dirname "$IMX_GPU_STUB_REFUSE")/deny" ]; then
    printf 'Error: invalid idle timeout: idle timeout cannot exceed 3h\nUsage:\n  canhazgpu run\nFlags:\n  --gpus int   Number of GPUs\n' >&2
    printf 'Error: invalid idle timeout: idle timeout cannot exceed 3h\n' >&2
    exit 1
fi
if [ -f "$IMX_GPU_STUB_REFUSE" ]; then
    printf 'Waiting for GPUs... (queue position: 3)\n'
    printf 'Error: wait timeout exceeded\nUsage:\n  canhazgpu run\n' >&2
    printf 'Error: wait timeout exceeded\n' >&2
    exit 1
fi
if [ -f "$(dirname "$IMX_GPU_STUB_REFUSE")/slow" ]; then
    printf 'Waiting for GPUs... (queue position: 2)\n'
    sleep "${IMX_GPU_STUB_DELAY:-4}"
fi
printf 'Waiting for GPUs... (queue position: 2)\n'
printf 'Reserved %s GPU(s): [7] for command execution (timeout: 1h 0m 0s)\n' "$gpus"
printf 'Warning: failed to activate booking abc: failed to stop run task x\n' >&2
CUDA_VISIBLE_DEVICES=7 exec "$@"
"""
_PYTEST_STUB = """#!/bin/sh
printf 'STUB-PYTEST cards=%s\\n' "$CUDA_VISIBLE_DEVICES"
"""
_DEMAND = GpuDemand(enabled=True, wait="5s")      # a bounded wait: the opt-in
_UNBOUNDED = GpuDemand(enabled=True)              # the rule since 2026-11-26
_PROBE = ("printf 'hello\\n'\n"
          "printf 'cards=%s\\n' \"$CUDA_VISIBLE_DEVICES\"\n"
          "printf 'held=%s\\n' \"$IMX_GPU_HELD\"")


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    """Puts the stand-in `gpu` (and a stand-in `pytest`) first on PATH."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, text in (("gpu", _STUB), ("pytest", _PYTEST_STUB)):
        path = stubs / name
        path.write_text(text)
        path.chmod(0o755)
    calls = tmp_path / "scheduler.log"
    refuse = tmp_path / "refuse"
    env = {"PATH": str(stubs) + os.pathsep + os.environ["PATH"],
           "IMX_GPU_STUB_LOG": str(calls),
           "IMX_GPU_STUB_REFUSE": str(refuse)}
    for key in ("IMX_GPU_MODE", "IMX_GPU_HELD", "IMX_GPU_WAIT", "IMX_GPU_MAX"):
        monkeypatch.delenv(key, raising=False)
    return env, calls, refuse


def _requests(calls: Path) -> list[str]:
    return calls.read_text().split() if calls.exists() else []


def _runner(tmp_path, demand=_DEMAND):
    return TestRunner(repo_root=tmp_path, tests_dir=tmp_path / "tests",
                      gpu_demand=demand)


# -- pure helpers ---------------------------------------------------------------

def test_command_gpus_is_the_largest_cards_marker():
    assert command_gpus('pytest -m "core_model and (cards_1 or cards_4)"') == 4
    assert command_gpus("pytest tests/x -m cards_2") == 2
    assert command_gpus("pytest tests/x") == 0
    assert command_gpus("pytest tests/x", 1) == 1
    assert command_gpus("pytest -m cards_99") == 0       # not a card count


@pytest.mark.parametrize("expression, need", [
    ("core_model and (cards_1 or cards_4)", 4),
    ("cards_2", 2),
    # an exclusion is not a need: the repository's own CPU job says so
    ("core_model and cuda and L4 and not cards_2", 0),
    ("core_model and cpu and not (cards_2 or cards_3 or cards_8)", 0),
    ("full_model and cards_4 and not cards_2", 4),
    ("not cards_4 and cards_2", 2),
    ("cards_99", 0),                         # not a card count
])
def test_a_negated_cards_marker_is_not_a_need(expression, need):
    assert command_gpus(f"pytest -m '{expression}'") == need


@pytest.mark.parametrize("command, needs", [
    ("pytest -x tests/a", True),
    ("python -m pytest tests/a", True),
    ("python3.12 -m pytest tests/a", True),
    ("cd tests && pytest b", True),
    ("(cd x && pytest)", True),
    ("(pytest a)", True),
    ("FOO=1 pytest -k foo", True),
    ("VLLM_X=1 python -m pytest a", True),
    ("env FOO=bar pytest", True),
    ("if true; then pytest a; fi", True),
    ("timeout 600 pytest a", True),
    ("uv run pytest a", True),
    ("nohup pytest a > x.log &", True),
    ('pytest -m "core_model and (cards_1 or cards_4)"', True),
    ('python -c "import torch; print(torch.cuda.device_count())"', True),
    ("CUDA_VISIBLE_DEVICES=0 python x.py --device cuda", True),
    ("python - <<\"PY\"\nimport torch\ntorch.cuda.synchronize()\nPY", True),
    ("imx-omni-pytest tests/worker", False),
    ("imx-other-pytest tests/worker", False),   # named per repository
    ("gpu run --gpus 1 -- pytest a", False),
    ("cat pytest.ini", False),
    ('git commit -m "fix pytest"', False),
    ("grep -rn pytest tests/", False),
    ("grep -rn torch.cuda vllm_omni/", False),
    ("python script.py", False),
    ("echo pytest", False),
    ("echo \"run pytest now\"", False),
    ("git status", False),
    ("python check.py; echo torch.cuda", False),
    ("python a.py | grep -n cuda.is_available", False),
    ("cat conftest.py | grep -n cuda.is_available", False),
    ("sed -i s/torch.cuda// a.py", False),
    ("", False),
])
def test_shell_needs_gpu(command, needs):
    assert shell_needs_gpu(command) is needs


def test_duration_seconds_reads_the_scheduler_formats():
    assert DEFAULT_WAIT == ""                     # no bound (owner decision, 2026-11-26)
    assert duration_seconds("30m") == 1800
    assert duration_seconds("2h") == 7200
    assert duration_seconds("90s") == 90
    assert duration_seconds("1h30m") == 5400
    assert duration_seconds("later", -1) == -1


@pytest.mark.parametrize("seconds, text", [
    (0, "1s"), (59, "59s"), (60, "1m"), (1040, "18m"), (1500, "25m"),
    (1501, "26m"), (3599, "1h"), (3600, "1h"), (7200, "2h"), (7201, "3h"),
    (8100, "3h"), (IDLE_MAX_SEC, "3h"),
    (21600 + SCHEDULER_TIMEOUT_MARGIN_SEC, "3h"),
])
def test_idle_timeout_is_one_unit_within_the_schedulers_cap(seconds, text):
    assert idle_timeout(seconds) == text
    # measured on the scheduler (2026-11-25): it reads one unit only (given
    # `1h30m` it refuses the request) and at most three hours
    assert re.fullmatch(r"\d+[smh]", text)
    assert duration_seconds(text) <= IDLE_MAX_SEC


def test_mode_comes_from_the_environment(monkeypatch):
    assert GpuDemand.from_env({}) == GpuDemand(enabled=False)
    demand = {gpu_demand.MODE_ENV: "demand", gpu_demand.WAIT_ENV: "2m"}
    assert GpuDemand.from_env(demand) == GpuDemand(enabled=True, wait="2m")
    # a wait is a bound someone sets; the cap on cards is the same kind of knob
    assert GpuDemand.from_env({gpu_demand.MODE_ENV: "demand"}).wait == ""
    assert GpuDemand.from_env({gpu_demand.MAX_ENV: "4"}).maximum == 4
    assert GpuDemand.from_env({gpu_demand.MAX_ENV: "four"}).maximum is None
    assert GpuDemand.from_env({gpu_demand.MAX_ENV: "0"}).maximum is None
    assert GpuDemand.from_env({gpu_demand.MODE_ENV: "reserved"}).enabled is False
    # inside a command tree that already holds the cards nothing asks again
    assert GpuDemand.from_env(
        {**demand, gpu_demand.HELD_ENV: "1"}).enabled is False
    monkeypatch.setenv(gpu_demand.MODE_ENV, "demand")
    assert TestRunner(repo_root=Path("."), tests_dir=Path(".")).gpu_demand.enabled


def test_session_and_prompt_name_no_cards_on_demand():
    # under a campaign-wide reservation the historical default is kept
    assert session_devices({}) == "0,1"
    assert session_devices({"CUDA_VISIBLE_DEVICES": "4,5"}) == "4,5"
    demand = {gpu_demand.MODE_ENV: "demand"}
    assert session_devices(demand) == ""
    assert session_devices({**demand, "CUDA_VISIBLE_DEVICES": "4,5"}) == "4,5"
    # the prompt offers no selection an agent would use on cards it lacks
    text = gpu_demand.ON_DEMAND_TEXT
    assert prompt_devices("", demand) == text
    assert prompt_devices("", {}) == ""
    assert prompt_devices("4,5", {}) == "4,5"


# -- the wrapper program, run by a real bash ------------------------------------

def test_wrapper_routes_the_schedulers_output_and_the_commands(scheduler, tmp_path):
    env, _, _ = scheduler
    side = tmp_path / "side.log"
    program = scheduler_wrap(
        "printf 'out\\n'\nprintf 'err\\n' >&2\nprintf 'cards=%s\\n' \"$CUDA_VISIBLE_DEVICES\"\nexit 3",
        gpus=2, timeout_sec=600, wait="30m", note="a test", side_log=side)
    done = subprocess.run(["bash", "--norc", "-c", program], env=env,
                          capture_output=True, text=True)
    assert done.returncode == 3
    assert done.stdout.splitlines() == ["out", "cards=7"]
    assert done.stderr.splitlines() == ["err"]
    spoken = side.read_text()
    assert "Reserved 2 GPU(s): [7]" in spoken
    assert "failed to activate booking" in spoken     # never in the command's streams
    assert granted_gpus(side) == ["7"] and scheduler_error(side) == ""


def test_wrapper_without_a_bound_waits_for_the_cards(scheduler, tmp_path):
    env, _, refuse = scheduler
    (tmp_path / "slow").touch()
    env = dict(env, IMX_GPU_STUB_DELAY="3")
    side = tmp_path / "side.log"
    program = scheduler_wrap("printf 'ran\\n'", gpus=1, timeout_sec=600,
                             wait="", note="a test", side_log=side)
    assert "--wait" not in program
    done = subprocess.run(["bash", "--norc", "-c", program], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout == "ran\n"


def test_wrapper_reports_an_expired_wait_with_its_own_status(scheduler, tmp_path):
    env, _, refuse = scheduler
    refuse.touch()
    side = tmp_path / "side.log"
    program = scheduler_wrap("printf 'ran\\n'", gpus=4, timeout_sec=600,
                             wait="30m", note="a test", side_log=side)
    done = subprocess.run(["bash", "--norc", "-c", program], env=env,
                          capture_output=True, text=True)
    assert done.returncode == NO_GPU_RC and done.stdout == ""
    assert "wait timeout exceeded" in scheduler_error(side)


# -- TestRunner -------------------------------------------------------------------

def test_runner_in_demand_mode_runs_on_the_granted_cards(scheduler, tmp_path):
    env, calls, _ = scheduler
    outcome = _runner(tmp_path).run(
        TestJob(key="probe", command=_PROBE, timeout_sec=120), dict(env))
    assert outcome.rc == 0 and not outcome.skipped
    assert _requests(calls) == ["gpus=1"]
    log = Path(outcome.log_file).read_text()
    assert "hello" in log and "cards=7" in log and "held=1" in log
    assert "[gpu] 1 card(s) from the scheduler: [7]" in log
    # the scheduler's warning would match the watchdog's `failed to` pattern
    assert "failed to activate booking" not in log
    side = Path(outcome.log_file + ".scheduler")
    assert "failed to activate booking" in side.read_text()
    assert Path(outcome.log_file).with_name(".passed_probe").exists()


def test_runner_asks_for_the_jobs_cards_and_bounds_the_wait(scheduler, tmp_path):
    env, calls, _ = scheduler
    command = _PROBE + "\nprintf '%s\\n' pytest -m 'cards_1 or cards_2'"
    job = TestJob(key="two", command=command, timeout_sec=100, min_gpus=1)
    outcome = _runner(tmp_path, GpuDemand(enabled=True, wait="30m")).run(
        job, dict(env))
    assert outcome.rc == 0 and _requests(calls) == ["gpus=2"]
    program = outcome.plan.argv[-1]
    assert "--gpus 2" in program
    # the runner's own timer starts at the spawn: the wait is at most half of it
    assert "--wait 50s" in program
    assert f"--timeout {int(100 + SCHEDULER_TIMEOUT_MARGIN_SEC)}s" in program
    assert "--idle-timeout 17m" in program


def test_runner_waits_for_cards_and_times_the_test_from_the_grant(scheduler, tmp_path):
    env, calls, refuse = scheduler
    (tmp_path / "slow").touch()
    # 4 s in the queue, a test allowed 2 s: timed from the spawn it would be
    # killed in the queue; timed from the grant it runs to its end
    job = TestJob(key="queued", command="printf 'ran\\n'", timeout_sec=2)
    outcome = _runner(tmp_path, _UNBOUNDED).run(job, dict(env))
    assert outcome.rc == 0 and not outcome.timed_out, outcome
    program = outcome.plan.argv[-1]
    assert "--wait" not in program                # nothing gives up on cards
    log = Path(outcome.log_file).read_text()
    assert "[gpu] queued for the scheduler's cards" in log
    assert "[gpu] granted after" in log and "[gpu] 1 card(s) from the scheduler: [7]" in log
    assert "\nran\n" in log


def test_runner_a_refusal_is_not_a_wait(scheduler, tmp_path):
    env, _, _ = scheduler
    (tmp_path / "deny").touch()
    job = TestJob(key="probe", command="printf 'ran\\n'", timeout_sec=600)
    outcome = _runner(tmp_path, _UNBOUNDED).run(job, dict(env))
    assert outcome.rc == 1 and not outcome.skipped and not outcome.timed_out
    log = Path(outcome.log_file).read_text()
    assert "[gpu] not run: the scheduler named no card(s) and said:" in log
    assert "\nran\n" not in log


def test_runner_lock_keeps_its_bounded_wait_whatever_the_queue(scheduler, tmp_path, monkeypatch):
    import infermatrix_copilot.testing.runner as module

    seen = []

    class FakeLock:
        def __init__(self, lock_dir, **kwargs):
            seen.append(kwargs)

        def acquire(self):
            return self

        def release(self):
            pass

    monkeypatch.setattr(module, "GpuLock", FakeLock)
    job = TestJob(key="cpu", command="printf 'cpu\\n'", timeout_sec=30,
                  min_gpus=0, gpu_lock=True, gpus=0)
    locked = lambda demand: TestRunner(   # noqa: E731
        repo_root=tmp_path, tests_dir=tmp_path / "tests",
        gpu_lock_dir=tmp_path / "gpu_lock", gpu_demand=demand)
    locked(_UNBOUNDED).run(job, dict(scheduler[0]))
    locked(_DEMAND).run(job, dict(scheduler[0]))
    # a run holding the lock may itself be queued for cards without a bound:
    # the next one still gives up on the lock after its hour, as feedback
    assert seen == [{}, {}]


def test_runner_asks_for_what_the_jobs_tests_declare(scheduler, tmp_path):
    env, calls, _ = scheduler
    (tmp_path / "tests_x").mkdir()
    (tmp_path / "tests_x" / "test_need.py").write_text(
        "import pytest\npytestmark = [pytest.mark.core_model, pytest.mark.cards_4]\n"
        "def test_a():\n    pass\n")
    command = _PROBE + "\nprintf '%s\\n' pytest tests_x/test_need.py"
    job = TestJob(key="need", command=command, timeout_sec=60, min_gpus=0, gpu_lock=True)
    outcome = _runner(tmp_path).run(job, dict(env))
    assert outcome.rc == 0 and _requests(calls) == ["gpus=4"]
    assert "--gpus 4" in outcome.plan.argv[-1]


def test_runner_caps_the_idle_release_of_a_long_job(scheduler, tmp_path):
    env, _, _ = scheduler
    job = TestJob(key="long", command="printf 'ran\\n'", timeout_sec=21600)
    outcome = _runner(tmp_path).run(job, dict(env))
    assert outcome.rc == 0
    program = outcome.plan.argv[-1]
    assert f"--timeout {21600 + SCHEDULER_TIMEOUT_MARGIN_SEC}s" in program
    assert "--idle-timeout 3h" in program


def test_runner_says_so_when_the_scheduler_refuses_the_request(scheduler, tmp_path):
    env, _, _ = scheduler
    (tmp_path / "deny").touch()
    job = TestJob(key="probe", command="printf 'ran\\n'", timeout_sec=600)
    outcome = _runner(tmp_path).run(job, dict(env))
    assert outcome.rc == 1 and not outcome.skipped   # the scheduler's own status
    log = Path(outcome.log_file).read_text()
    assert ("[gpu] not run: the scheduler named no card(s) and said: "
            "Error: invalid idle timeout: idle timeout cannot exceed 3h\n") in log
    assert "Usage:" not in log and "\nran\n" not in log


def test_runner_leaves_a_command_that_asks_for_no_cards_alone(scheduler, tmp_path):
    env, calls, _ = scheduler
    job = TestJob(key="cpu", command="printf 'cpu\\n'", timeout_sec=60,
                  min_gpus=0, gpu_lock=False, gpus=0)
    outcome = _runner(tmp_path).run(job, dict(env))
    assert outcome.rc == 0 and _requests(calls) == []
    assert "gpu run" not in outcome.plan.argv[-1]


def test_runner_an_expired_wait_is_a_skip_not_a_failure(scheduler, tmp_path):
    env, calls, refuse = scheduler
    refuse.touch()
    job = TestJob(key="probe", command="printf 'ran\\n'", timeout_sec=600)
    outcome = _runner(tmp_path).run(job, dict(env))
    assert outcome.skipped and outcome.rc == 0 and not outcome.timed_out
    assert "scheduler granted none within" in outcome.skip_reason
    log = Path(outcome.log_file).read_text()
    assert "[gpu] not run" in log and "\nran\n" not in log   # the command never ran
    assert not Path(outcome.log_file).with_name(".passed_probe").exists()


def test_runner_in_demand_mode_ignores_an_inherited_selection(scheduler, tmp_path):
    env, calls, _ = scheduler
    outcome = _runner(tmp_path).run(
        TestJob(key="probe", command=_PROBE, timeout_sec=120),
        dict(env, CUDA_VISIBLE_DEVICES="0,1,2,3"))
    assert "cards=7" in Path(outcome.log_file).read_text()
    assert _requests(calls) == ["gpus=1"]


def test_runner_outside_demand_mode_keeps_the_hardware_gate(scheduler, tmp_path):
    env, calls, _ = scheduler
    job = TestJob(key="probe", command="printf 'ran\\n'", timeout_sec=60)
    gated = _runner(tmp_path, GpuDemand(enabled=False)).run(job, dict(env))
    assert gated.skipped and "1 GPU(s), 0 available" in gated.skip_reason
    assert _runner(tmp_path).run(job, dict(env)).skipped is False
    assert _requests(calls) == ["gpus=1"]


# -- the agents' shell -------------------------------------------------------------

def _shell(env):
    tools = build_rebase_tools(
        [{"name": "run_shell", "description": "Run a command",
          "input_schema": {"type": "object"}}],
        RebasePaths(omni_path="/", vllm_path="/", env=env))
    return lambda command, timeout=60: json.loads(
        tools["run_shell"].handler(command=command, timeout=timeout))


def test_shell_runs_a_test_under_the_scheduler_and_leaves_the_rest_alone(
        scheduler, tmp_path, monkeypatch):
    env, calls, _ = scheduler
    monkeypatch.setenv(gpu_demand.MODE_ENV, "demand")
    monkeypatch.delenv(gpu_demand.HELD_ENV, raising=False)
    run = _shell(env)
    plain = run("printf 'plain cards=%s\\n' \"$CUDA_VISIBLE_DEVICES\"")
    assert plain["exit_code"] == 0 and plain["stdout"].strip() == "plain cards="
    assert _requests(calls) == []
    test = run("cd /tmp && FOO=1 pytest -k x")
    assert test["exit_code"] == 0, test
    assert test["stdout"].strip() == "STUB-PYTEST cards=7"
    assert "failed to activate booking" not in test["stderr"]
    assert _requests(calls) == ["gpus=1"]
    # a command that names the wrapper is left to it: the wrapper asks for
    # its own cards
    assert run("printf '%s\\n' imx-omni-pytest")["exit_code"] == 0
    assert _requests(calls) == ["gpus=1"]


def test_shell_tells_the_agent_when_no_card_was_granted(scheduler, monkeypatch):
    env, calls, refuse = scheduler
    monkeypatch.setenv(gpu_demand.MODE_ENV, "demand")
    refuse.touch()
    result = _shell(env)("pytest -k x", timeout=60)
    assert result["gpu_unavailable"] is True and result["exit_code"] == 1
    assert "granted no GPU within 30s" in result["stderr"]
    assert _requests(calls) == ["gpus=1"]


def test_shell_asks_for_what_the_test_declares(scheduler, tmp_path, monkeypatch):
    env, calls, _ = scheduler
    monkeypatch.setenv(gpu_demand.MODE_ENV, "demand")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_need.py").write_text(
        "import pytest\n"
        "@pytest.mark.core_model\n"
        "@hardware_test(res={\"cuda\": \"L20\"}, num_cards=2)\n"
        "def test_a():\n    pass\n")
    tools = build_rebase_tools(
        [{"name": "run_shell", "description": "Run a command",
          "input_schema": {"type": "object"}}],
        RebasePaths(omni_path=str(tmp_path), vllm_path="/", env=env))
    result = json.loads(tools["run_shell"].handler(command="pytest tests/test_need.py",
                                                   timeout=60))
    assert result["exit_code"] == 0, result
    assert _requests(calls) == ["gpus=2"]


def test_shell_outside_demand_mode_never_asks(scheduler, tmp_path):
    env, calls, _ = scheduler
    result = _shell(env)("pytest -k x")
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "STUB-PYTEST cards="
    assert _requests(calls) == []


# -- what the selected tests declare ---------------------------------------------

_TREE = {
    "test_decorator.py": (
        "import pytest\n"
        "@pytest.mark.core_model\n"
        "@hardware_test(res={\"cuda\": \"L20\"}, num_cards=2)\n"
        "def test_a():\n    pass\n"),
    "test_module.py": (
        "import pytest\n"
        "pytestmark = [pytest.mark.core_model, pytest.mark.cards_4]\n"
        "def test_b():\n    pass\n"),
    "test_class.py": (
        "import pytest\n"
        "class TestGroup:\n"
        "    pytestmark = pytest.mark.core_model\n"
        "    @hardware_marks(res={\"cuda\": \"L20\"}, num_cards={\"cuda\": 3, \"rocm\": 1})\n"
        "    def test_c(self):\n        pass\n"),
    "test_other_level.py": (
        "import pytest\n"
        "@pytest.mark.advanced_model\n"
        "@hardware_test(res={\"cuda\": \"H100\"}, num_cards=8)\n"
        "def test_d():\n    pass\n"),
    "test_strings.py": (
        "def test_quote():\n"
        "    assert '-m \"cards_8\"' in buildkite()\n"),
    "test_undeclared.py": "def test_e():\n    pass\n",
}


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "tests").mkdir()
    for name, text in _TREE.items():
        (tmp_path / "tests" / name).write_text(text)
    gpu_demand._NEEDS.clear()
    return tmp_path


def test_command_gpus_reads_what_the_selected_tests_declare(tree):
    need = lambda command, **kw: command_gpus(command, 1, tree, **kw)   # noqa: E731
    # the command's own expression is the selection
    assert command_gpus("pytest -m 'core_model and cards_2'", 1, tree) == 2
    # decorators, module marks, class marks, a per-platform number
    assert need("pytest tests/test_decorator.py") == 2
    assert need("pytest tests/test_module.py") == 4
    assert need("pytest tests/test_class.py::TestGroup::test_c") == 3
    # a test of another run level does not count at the default level
    assert need("pytest tests/test_other_level.py") == 1
    assert need("pytest tests/test_other_level.py --run-level advanced_model") == 8
    # names inside strings are not needs, and a directory is all of its files
    assert need("pytest tests/test_strings.py") == 1
    assert need("pytest tests/ --run-level core_model") == 4
    assert need("pytest tests/ --run-level advanced_model") == 8
    # the level the command selects: its option, else the one level it names
    assert need("pytest tests/test_other_level.py -m 'advanced_model and cuda'") == 8
    # nothing declared: one card
    assert need("pytest tests/test_undeclared.py") == 1
    assert need("pytest tests/missing.py") == 1
    assert need("python -c 'import torch'") == 1


def test_command_gpus_does_not_count_what_the_expression_excludes(tree):
    cpu_job = "core_model and cpu and not (cards_2 or cards_3 or cards_4 or cards_8)"
    assert command_gpus(f"pytest tests/ -m '{cpu_job}'", 1, tree) == 1
    # the module's four and the class's three; four is excluded
    assert command_gpus("pytest tests/ -m 'core_model and not cards_4'", 1, tree) == 3


def test_command_gpus_cap_never_dips_below_a_callers_minimum(tree):
    assert command_gpus("pytest tests/ --run-level advanced_model", 1, tree, 4) == 4
    assert command_gpus("pytest tests/", 6, tree, 4) == 6
    assert command_gpus("pytest -m 'full_model and cards_8'", 1, None, 4) == 4
    assert command_gpus("pytest tests/", 0, tree, 4) == 4


def test_command_gpus_rereads_edited_tests(tree):
    assert command_gpus("pytest tests/test_undeclared.py", 1, tree) == 1
    (tree / "tests" / "test_undeclared.py").write_text(
        "import pytest\npytestmark = pytest.mark.core_model\n"
        "@pytest.mark.cards_2\ndef test_e():\n    pass\n")
    assert command_gpus("pytest tests/test_undeclared.py", 1, tree) == 1   # kept briefly
    gpu_demand._NEEDS.clear()
    assert command_gpus("pytest tests/test_undeclared.py", 1, tree) == 2


# -- the module test wrapper --------------------------------------------------------

def _wrapper_env(repo, tmp_path, monkeypatch):
    repo.mkdir()
    for name in ("IMX_ADAPTER_REBASE", "IMX_WATCHDOG_OVERLAY"):
        monkeypatch.delenv(name, raising=False)
    for key, value in {"IMX_TARGET_REPO": str(repo),
                       "IMX_LOG_DIR": str(tmp_path / "logs"),
                       "IMX_RUN_ID": "run-20200101-000000-abcdef",
                       "IMX_GPU_MUTEX": "0", "CUDA_VISIBLE_DEVICES": "",
                       "TEST_TIMEOUT_SEC": "10"}.items():
        monkeypatch.setenv(key, value)
    gpu_demand._NEEDS.clear()


def _capturing_wrapper(monkeypatch):
    seen = {}

    class CapturingRunner:
        def __init__(self, **kwargs):
            seen["options"] = kwargs

        def run(self, job, env):
            seen["job"] = job
            return module_pytest.TestOutcome(
                rc=0, skipped=True, log_file="", skip_reason="needs 1 GPU(s)")

    monkeypatch.setattr(module_pytest, "TestRunner", CapturingRunner)
    return seen


def test_module_wrapper_asks_for_one_card_and_says_so_when_nothing_ran(
        tmp_path, monkeypatch, capsys):
    _wrapper_env(tmp_path / "repo", tmp_path, monkeypatch)
    seen = _capturing_wrapper(monkeypatch)
    # The wrapper keeps the outcome's own rc (its contract); a run that did
    # not happen is said on both streams, so no reader takes it for a pass.
    assert module_pytest.main(["-vv", "-s", "tests/worker/"]) == 0
    assert seen["job"].gpus == 1
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert "skipped — needs 1 GPU(s)" in stream


def test_module_wrapper_asks_for_what_its_tests_declare(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _wrapper_env(repo, tmp_path, monkeypatch)
    (repo / "tests" / "worker").mkdir(parents=True)
    # the wrapper selects core_model by default: the test says it needs two
    (repo / "tests" / "worker" / "test_need.py").write_text(
        "import pytest\npytestmark = pytest.mark.core_model\n"
        "@pytest.mark.cards_2\ndef test_a():\n    pass\n")
    seen = _capturing_wrapper(monkeypatch)
    assert module_pytest.main(["-vv", "-s", "tests/worker/"]) == 0
    assert seen["job"].gpus == 2
