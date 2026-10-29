"""Standalone wrapper outcomes, optional adapter budgets, and private receipts."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from infermatrix_copilot.rebase_engine import module_pytest
from infermatrix_copilot.testing.runner import TestOutcome as Outcome

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def wrapper_env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}",
        "PYTHONPATH": str(ROOT / "src"),
        "IMX_TARGET_REPO": str(repo),
        "IMX_LOG_DIR": str(tmp_path / "logs"),
        "IMX_RUN_ID": "run-20200101-000000-abcdef",
        "IMX_GPU_MUTEX": "0",
        "CUDA_VISIBLE_DEVICES": "",
        "TEST_TIMEOUT_SEC": "10",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }
    for name in ("IMX_ADAPTER_REBASE", "IMX_WATCHDOG_OVERLAY"):
        monkeypatch.delenv(name, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def configure_adapter(tmp_path, env, monkeypatch, testing):
    adapter = tmp_path / "adapter"
    rebase = adapter / "rebase"
    rebase.mkdir(parents=True)
    (adapter / "manifest.yaml").write_text(
        yaml.safe_dump({"rebase": {"testing": testing}}), encoding="utf-8"
    )
    env["IMX_ADAPTER_REBASE"] = str(rebase)
    monkeypatch.setenv("IMX_ADAPTER_REBASE", str(rebase))
    return adapter


def receipts(env):
    return sorted((Path(env["IMX_LOG_DIR"]) / "tests" / "outcomes").glob("*.json"))


@pytest.mark.parametrize(
    "cap", [None, True, False, "7200", 1.5, 0, -1, float("inf"), float("nan")]
)
def test_invalid_adapter_cap_rejects_before_runner(
    tmp_path, monkeypatch, wrapper_env, cap, capsys
):
    configure_adapter(
        tmp_path, wrapper_env, monkeypatch, {"module_max_timeout_sec": cap}
    )

    def forbidden_runner(**kwargs):
        pytest.fail("invalid cap constructed a TestRunner")

    monkeypatch.setattr(module_pytest, "TestRunner", forbidden_runner)
    assert module_pytest.main(["python", "-c", "raise SystemExit(99)"]) == 2
    assert "module_max_timeout_sec" in capsys.readouterr().err
    assert not Path(wrapper_env["IMX_LOG_DIR"]).exists()


@pytest.mark.parametrize("timeout", ["7201", "28800", "0", "-1", "nope"])
def test_configured_cap_rejects_before_runner(
    tmp_path, monkeypatch, wrapper_env, timeout, capsys
):
    configure_adapter(
        tmp_path, wrapper_env, monkeypatch, {"module_max_timeout_sec": 7200}
    )
    monkeypatch.setenv("TEST_TIMEOUT_SEC", timeout)

    def forbidden_runner(**kwargs):
        pytest.fail("rejected timeout constructed a TestRunner")

    monkeypatch.setattr(module_pytest, "TestRunner", forbidden_runner)
    assert module_pytest.main(["python", "-c", "raise SystemExit(99)"]) == 2
    assert "TEST_TIMEOUT_SEC" in capsys.readouterr().err
    assert not Path(wrapper_env["IMX_LOG_DIR"]).exists()


@pytest.mark.parametrize(
    "configured,timeout", [(True, None), (True, "7200"), (False, "28800")]
)
def test_default_cap_and_unset_adapter_preserve_job(
    tmp_path, monkeypatch, wrapper_env, configured, timeout
):
    configure_adapter(
        tmp_path,
        wrapper_env,
        monkeypatch,
        {"module_max_timeout_sec": 7200} if configured else {},
    )
    if timeout is None:
        monkeypatch.delenv("TEST_TIMEOUT_SEC")
    else:
        monkeypatch.setenv("TEST_TIMEOUT_SEC", timeout)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "2,3,4,5")
    monkeypatch.setenv("IMX_GPU_MUTEX", "1")
    observed = {}

    class CapturingRunner:
        def __init__(self, **kwargs):
            observed["options"] = kwargs

        def run(self, job, env):
            observed["job"] = job
            observed["env"] = env
            return Outcome(rc=7)

    monkeypatch.setattr(module_pytest, "TestRunner", CapturingRunner)
    assert module_pytest.main(["-vv", "-s", "tests/worker/"]) == 7
    job = observed["job"]
    assert job.timeout_sec == (7200 if timeout is None else int(timeout))
    assert job.command == "python -m pytest -vv -s tests/worker/"
    assert job.gpu_lock is True and job.min_gpus == 0
    assert observed["options"]["cuda_visible_devices"] == "2,3,4,5"
    assert (
        observed["options"]["gpu_lock_dir"]
        == Path(wrapper_env["IMX_LOG_DIR"]) / "gpu_lock"
    )
    receipt = json.loads(receipts(wrapper_env)[0].read_text())
    assert receipt["cuda_visible_devices"] == ["2", "3", "4", "5"]
    assert receipt["log_hash_status"] == "missing"


def test_adapter_platform_filters_only_other_platform_items(
    tmp_path, monkeypatch, wrapper_env
):
    configure_adapter(
        tmp_path,
        wrapper_env,
        monkeypatch,
        {"module_max_timeout_sec": 7200, "module_test_platform": "cuda"},
    )
    observed = {}

    class CapturingRunner:
        def __init__(self, **kwargs):
            pass

        def run(self, job, env):
            observed["job"] = job
            return Outcome(rc=0)

    monkeypatch.setattr(module_pytest, "TestRunner", CapturingRunner)
    args = ["-vv", "-s", "tests/diffusion/"]
    assert module_pytest.main(args) == 0
    marker = "(cuda or (not musa and not npu and not rocm and not xpu)) and core_model"
    assert observed["job"].command == (
        "python -m pytest -vv -s tests/diffusion/ --run-level core_model -m "
        + shlex.quote(marker)
    )
    receipt = json.loads(receipts(wrapper_env)[0].read_text())
    requested = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    effective = json.dumps(
        [*args, "--run-level", "core_model", "-m", marker],
        ensure_ascii=False, separators=(",", ":"))
    assert receipt["requested_argv_sha256"] == hashlib.sha256(requested.encode()).hexdigest()
    assert receipt["effective_argv_sha256"] == hashlib.sha256(effective.encode()).hexdigest()
    assert receipt["platform_marker_injected"] == "cuda"


def test_explicit_run_level_is_paired_and_not_forced_to_core(
    tmp_path, monkeypatch, wrapper_env
):
    configure_adapter(
        tmp_path, wrapper_env, monkeypatch, {"module_test_platform": "cuda"}
    )
    observed = {}

    class CapturingRunner:
        def __init__(self, **kwargs):
            pass

        def run(self, job, env):
            observed["job"] = job
            return Outcome(rc=0)

    monkeypatch.setattr(module_pytest, "TestRunner", CapturingRunner)
    args = ["-vv", "--run-level", "advanced_model", "tests/entrypoints/"]
    assert module_pytest.main(args) == 0
    marker = "(cuda or (not musa and not npu and not rocm and not xpu)) and advanced_model"
    assert observed["job"].command == (
        "python -m pytest -vv --run-level advanced_model tests/entrypoints/ -m "
        + shlex.quote(marker)
    )


def test_explicit_marker_and_command_mode_are_not_rewritten(
    tmp_path, monkeypatch, wrapper_env
):
    configure_adapter(
        tmp_path, wrapper_env, monkeypatch, {"module_test_platform": "cuda"}
    )
    commands = []

    class CapturingRunner:
        def __init__(self, **kwargs):
            pass

        def run(self, job, env):
            commands.append(job.command)
            return Outcome(rc=0)

    monkeypatch.setattr(module_pytest, "TestRunner", CapturingRunner)
    assert module_pytest.main(["-m", "npu", "tests/diffusion/"]) == 0
    assert module_pytest.main(["python", "-c", "print('OK')"]) == 0
    assert commands == [
        "python -m pytest -m npu tests/diffusion/",
        "python -c " + shlex.quote("print('OK')"),
    ]
    assert all(json.loads(p.read_text())["platform_marker_injected"] is None
               for p in receipts(wrapper_env))


@pytest.mark.parametrize("platform", [True, "gpu", "CUDA", ""])
def test_invalid_adapter_platform_rejects_before_runner(
    tmp_path, monkeypatch, wrapper_env, platform, capsys
):
    configure_adapter(
        tmp_path, wrapper_env, monkeypatch, {"module_test_platform": platform}
    )
    monkeypatch.setattr(
        module_pytest,
        "TestRunner",
        lambda **kwargs: pytest.fail("invalid platform constructed a runner"),
    )
    assert module_pytest.main(["tests/diffusion/"]) == 2
    assert "module_test_platform" in capsys.readouterr().err
    assert not receipts(wrapper_env)


@pytest.mark.parametrize(
    "code,timeout,expected_rc,timed_out",
    [
        ("print('NORMAL')", "10", 0, False),
        ("raise SystemExit(7)", "10", 7, False),
        ("import time; time.sleep(30)", "1", 124, True),
    ],
)
def test_real_standalone_cpu_outcome(
    tmp_path, monkeypatch, wrapper_env, code, timeout, expected_rc, timed_out
):
    configure_adapter(
        tmp_path, wrapper_env, monkeypatch, {"module_max_timeout_sec": 7200}
    )
    wrapper_env["TEST_TIMEOUT_SEC"] = timeout
    args = ["python", "-c", code]
    proc = subprocess.run(
        [sys.executable, "-m", module_pytest.__name__, *args],
        env=wrapper_env,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == expected_rc, proc.stderr.decode()
    files = receipts(wrapper_env)
    assert len(files) == 1
    receipt = json.loads(files[0].read_text())
    assert receipt["rc"] == expected_rc
    assert receipt["timed_out"] is timed_out
    assert receipt["skipped"] is False
    assert receipt["watchdog_triggered"] is False
    assert receipt["timeout_sec"] == int(timeout)
    assert receipt["cuda_visible_devices"] == []
    assert receipt["run_id"] == "run-20200101-000000-abcdef"
    assert receipt["wrapper_pid"] > 0
    if Path("/proc/self/stat").is_file():
        assert receipt["wrapper_starttime_ticks"] > 0
    canonical = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    assert (
        receipt["requested_argv_sha256"]
        == hashlib.sha256(canonical.encode()).hexdigest()
    )
    log = next((Path(wrapper_env["IMX_LOG_DIR"]) / "tests").glob("*.log"))
    assert receipt["log_sha256"] == hashlib.sha256(log.read_bytes()).hexdigest()
    assert receipt["log_hash_status"] == "stable_observation"
    assert receipt["log_path_sha256"] == hashlib.sha256(str(log).encode()).hexdigest()
    assert (
        "args" not in receipt and "command" not in receipt and "log_file" not in receipt
    )
    assert not list(files[0].parent.glob("*.tmp"))


def test_real_standalone_cap_rejection_cannot_spawn(tmp_path, monkeypatch, wrapper_env):
    configure_adapter(
        tmp_path, wrapper_env, monkeypatch, {"module_max_timeout_sec": 7200}
    )
    wrapper_env["TEST_TIMEOUT_SEC"] = "28800"
    marker = Path(wrapper_env["IMX_TARGET_REPO"]) / "spawned"
    code = f"from pathlib import Path; Path({str(marker)!r}).write_text('BAD')"
    proc = subprocess.run(
        [sys.executable, "-m", module_pytest.__name__, "python", "-c", code],
        env=wrapper_env,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 2
    assert b"7200" in proc.stderr
    assert not marker.exists()
    assert not Path(wrapper_env["IMX_LOG_DIR"]).exists()


def test_real_watchdog_outcome_is_not_a_pass(tmp_path, monkeypatch, wrapper_env):
    adapter = configure_adapter(tmp_path, wrapper_env, monkeypatch, {})
    testing = adapter / "testing"
    testing.mkdir()
    (testing / "watchdog_patterns.yaml").write_text(
        "critical: ['CRITICAL_FIXTURE']\n", encoding="utf-8"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            module_pytest.__name__,
            "python",
            "-c",
            "print('CRITICAL_FIXTURE')",
        ],
        env=wrapper_env,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 1
    receipt = json.loads(receipts(wrapper_env)[0].read_text())
    assert receipt["rc"] == 1 and receipt["watchdog_triggered"] is True
    assert receipt["timed_out"] is False and receipt["skipped"] is False


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome(rc=0, skipped=True),
        Outcome(rc=-15),
        Outcome(rc=1, watchdog_triggered=True),
    ],
)
def test_actual_outcome_is_preserved_and_private(
    monkeypatch, wrapper_env, tmp_path, outcome
):
    private = "FAKE_PRIVATE_COMMAND_OR_PARAMETER"
    log = tmp_path / f"tests_node[{private}].log"
    log.write_bytes(b"FAKE_PRIVATE_STDOUT\n")
    outcome.log_file = str(log)
    monkeypatch.setenv("HF_TOKEN", "FAKE_PRIVATE_CREDENTIAL")
    monkeypatch.setenv("IMX_RUN_ID", "FAKE_PRIVATE_CREDENTIAL_RUN_ID")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "FAKE_PRIVATE_CREDENTIAL_CUDA")
    monkeypatch.setattr(module_pytest.TestRunner, "run", lambda *a: outcome)
    args = ["python", "-c", f"print({private!r})"]
    assert module_pytest.main(args) == outcome.rc
    file = receipts(wrapper_env)[0]
    text = file.read_text()
    receipt = json.loads(text)
    assert receipt["rc"] == outcome.rc
    assert receipt["skipped"] is outcome.skipped
    assert receipt["watchdog_triggered"] is outcome.watchdog_triggered
    assert receipt["run_id"] is None
    assert (
        receipt["run_id_sha256"]
        == hashlib.sha256(b"FAKE_PRIVATE_CREDENTIAL_RUN_ID").hexdigest()
    )
    assert receipt["cuda_visible_devices"] is None
    assert "FAKE_PRIVATE" not in text
    assert "HF_TOKEN" not in text
    assert str(log) not in text
    assert file.stat().st_mode & 0o777 == 0o600
    assert receipt["attempt_id"] == file.stem


def test_same_command_retries_have_distinct_atomic_receipts(monkeypatch, wrapper_env):
    outcome = Outcome(rc=7)
    monkeypatch.setattr(module_pytest.TestRunner, "run", lambda *a: outcome)
    real_replace = os.replace
    seen = []

    def checked_replace(source, dest):
        assert Path(source).parent == Path(dest).parent
        assert not Path(dest).exists()
        seen.append(json.loads(Path(source).read_text()))
        real_replace(source, dest)

    monkeypatch.setattr(os, "replace", checked_replace)
    for _ in range(2):
        assert module_pytest.main(["python", "-c", "raise SystemExit(7)"]) == 7
    assert len(receipts(wrapper_env)) == 2 and len(seen) == 2
    assert seen[0]["attempt_id"] != seen[1]["attempt_id"]
    assert seen[0]["requested_argv_sha256"] == seen[1]["requested_argv_sha256"]


def test_runner_exception_never_creates_success_receipt(monkeypatch, wrapper_env):
    def fail(*a):
        raise RuntimeError("FAKE_PRIVATE_EXCEPTION")

    monkeypatch.setattr(module_pytest.TestRunner, "run", fail)
    with pytest.raises(RuntimeError):
        module_pytest.main(["python", "-c", "pass"])
    assert not receipts(wrapper_env)


def test_receipt_write_exception_never_returns_zero(monkeypatch, wrapper_env):
    monkeypatch.setattr(module_pytest.TestRunner, "run", lambda *a: Outcome(rc=0))

    def fail(*a):
        raise OSError("FAKE_PRIVATE_EXCEPTION")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        module_pytest.main(["python", "-c", "pass"])
    assert not receipts(wrapper_env)
    assert not list(
        (Path(wrapper_env["IMX_LOG_DIR"]) / "tests" / "outcomes").glob("*.tmp")
    )


def test_omni_manifest_declares_consistent_module_budget():
    """The wrapper's cap must admit the engine's planned budget, or every
    required command is refused before it starts."""
    testing = yaml.safe_load(
        (ROOT / "adapters/vllm_omni/manifest.yaml").read_text())["rebase"]["testing"]
    assert testing["module_timeout_sec"] == 21600
    assert testing["module_max_timeout_sec"] >= testing["module_timeout_sec"]
    assert testing["module_test_platform"] == "cuda"


def test_rejection_does_not_certify_a_stale_marker(tmp_path, monkeypatch, wrapper_env):
    configure_adapter(
        tmp_path, wrapper_env, monkeypatch, {"module_max_timeout_sec": 7200}
    )
    monkeypatch.setenv("TEST_TIMEOUT_SEC", "28800")
    tests = Path(wrapper_env["IMX_LOG_DIR"]) / "tests"
    tests.mkdir(parents=True)
    marker = tests / ".passed_module_pytest"
    marker.write_text("historical only")
    assert module_pytest.main(["python", "-c", "pass"]) == 2
    assert marker.read_text() == "historical only"
    assert not receipts(wrapper_env)


@pytest.mark.parametrize(
    "mode,status",
    [
        ("changed", "changed_during_hash"),
        ("rotated", "path_changed_during_hash"),
        ("unreadable", "unreadable"),
    ],
)
def test_log_hash_observation_reports_races_and_errors(
    tmp_path, monkeypatch, mode, status
):
    import builtins

    log = tmp_path / "log"
    log.write_bytes(b"original bytes")
    real_open = builtins.open

    class ObservedStream:
        def __init__(self):
            self.stream = real_open(log, "rb")
            self.changed = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            chunk = self.stream.read(size)
            if not self.changed:
                self.changed = True
                if mode == "changed":
                    with real_open(log, "ab") as writer:
                        writer.write(b"changed bytes")
                else:
                    replacement = tmp_path / "replacement"
                    with real_open(replacement, "wb") as writer:
                        writer.write(b"rotated bytes")
                    os.replace(replacement, log)
            return chunk

    def intercepted_open(*a, **kw):
        if mode == "unreadable":
            raise PermissionError("FAKE_PRIVATE_ERROR")
        return ObservedStream()

    monkeypatch.setattr(builtins, "open", intercepted_open)
    digest, observed_status = module_pytest._log_hash(str(log))
    assert observed_status == status
    assert (digest is None) is (mode == "unreadable")
