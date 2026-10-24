"""Real non-model commands exercise auxiliary entry points and tree cleanup."""
from types import SimpleNamespace
from pathlib import Path
import json
import os
import shlex
import signal
import subprocess
import sys
import time

import pytest

from infermatrix_copilot import process_execution
from infermatrix_copilot.improve import experiments
from infermatrix_copilot.process_tree import _alive, _start_time
from infermatrix_copilot.rebase_engine import rebase_tools
from infermatrix_copilot.rebase_engine import wheel
from infermatrix_copilot.resources import adapters_root
from infermatrix_copilot.tools import dispatch


def _tools(tmp_path):
    return rebase_tools.build_rebase_tools(
        rebase_tools.load_tool_schemas(adapters_root() / "vllm_omni/rebase/tool_schemas.json"),
        rebase_tools.RebasePaths(str(tmp_path), str(tmp_path),
                                env={"PATH": os.defpath, "AUXILIARY_VALUE": "bound"}))


def _call(tools, name, **kwargs):
    return json.loads(dispatch(name, kwargs, extra=tools)["result"])


def test_text_command_translation_preserves_exit_encoding_and_newlines(tmp_path):
    result = process_execution.run_command([sys.executable, "-c",
        "import os; os.write(1,b'a\\r\\nb\\rc\\xff'); os.write(2,b'err\\r\\n'); raise SystemExit(7)"],
        cwd=tmp_path, env={}, timeout=3, errors="replace")
    assert result.returncode == 7
    assert result.stdout == "a\nb\nc\ufffd" and result.stderr == "err\n"
    with pytest.raises(UnicodeDecodeError):
        process_execution.run_command([sys.executable, "-c", "import os; os.write(1,b'\\xff')"],
                                      cwd=tmp_path, env={}, timeout=3)


def test_existing_shell_uses_platform_shell_and_explicit_utf8(tmp_path):
    result = process_execution.run_command("printf 'ok\\r\\n'; exit 6", shell=True,
        cwd=tmp_path, env={}, timeout=3, encoding="utf-8", errors="replace")
    assert result.returncode == 6 and result.stdout == "ok\n" and not result.stderr


@pytest.mark.parametrize("name", ["run_shell", "run_import_check"])
def test_rebase_command_contracts_use_original_environment(tmp_path, name):
    code = ("import os; print(os.getcwd()); print(os.environ['AUXILIARY_VALUE']); "
            "os.write(2,b'error\\r\\n'); raise SystemExit(9)")
    kwargs = ({"command": shlex.join([sys.executable, "-c", code])} if name == "run_shell"
              else {"import_code": code})
    result = _call(_tools(tmp_path), name, **kwargs)
    assert result == {"exit_code": 9, "stdout": f"{tmp_path}\nbound\n", "stderr": "error\n"}


def _child_program(pidfile):
    child = ("import os,pathlib,signal,time; os.setsid(); "
             "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
             f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)")
    return ("import subprocess,sys,time,os; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
            "os.write(1,b'partial\\xff\\r\\n'); os.write(2,b'error\\x80'); time.sleep(30)")


def _assert_child_stopped(pidfile):
    assert pidfile.exists(), "positive control: the detached child must have started"
    pid = int(pidfile.read_text())
    assert not _alive(pid), f"owned child {pid} survived command deadline"


def _cleanup_child(pidfile):
    if pidfile.exists():
        pid = int(pidfile.read_text())
        born = _start_time(pid)
        if born is not None and _alive(pid) and _start_time(pid) == born:
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("name", ["run_shell", "run_import_check"])
def test_rebase_timeout_reaps_detached_child_without_changing_tool_shape(tmp_path, monkeypatch, name):
    pidfile = tmp_path / "child.pid"
    code = _child_program(pidfile)
    # Keep the shipped import-check deadline contract while accelerating only
    # this non-model test's actual process deadline.
    original = process_execution.run_process
    deadlines = []

    def bounded(cmd, **kwargs):
        deadlines.append(kwargs["deadline"] - time.monotonic())
        kwargs["deadline"] = time.monotonic() + .8
        kwargs["descendant_interval_s"] = .02
        return original(cmd, **kwargs)

    monkeypatch.setattr(process_execution, "run_process", bounded)
    kwargs = ({"command": shlex.join([sys.executable, "-c", code]), "timeout": 17}
              if name == "run_shell" else {"import_code": code})
    try:
        result = _call(_tools(tmp_path), name, **kwargs)
        message = "Command timed out after 17s" if name == "run_shell" else "Import check timed out"
        assert result == {"exit_code": -1, "stdout": "", "stderr": message}
        assert deadlines[0] == pytest.approx(17 if name == "run_shell" else 120, abs=.1)
        _assert_child_stopped(pidfile)
    finally:
        _cleanup_child(pidfile)


def _shadow_entry(tmp_path, monkeypatch, code):
    entry = tmp_path / "python-fixture"
    entry.write_text(f"#!{sys.executable}\n" + code + "\n")
    entry.chmod(0o700)
    monkeypatch.setattr(experiments, "sys", SimpleNamespace(executable=str(entry)))
    return {"side": "arm", "item": "meta:case-1", "replicate": 1,
            "env": {"IMPROVE_UNIT_TAG": "original-unit"},
            "snapshot_path": tmp_path / "snapshot.json", "shadow_dir": tmp_path,
            "run_root": tmp_path / "runs", "playbook": "workflow-improve", "repo": "fixture", "pr": 4}


@pytest.mark.parametrize("runner", [experiments.default_run_unit, experiments.meta_run_unit])
def test_shadow_entry_preserves_arguments_environment_tails_and_status(tmp_path, monkeypatch, runner):
    code = ("import json,os,sys; "
            "print('x'*2500); print(json.dumps({'argv':sys.argv[1:],'cwd':os.getcwd(),'env':dict(os.environ)})); "
            "sys.stderr.write('z'*2500); raise SystemExit(8)")
    kwargs = _shadow_entry(tmp_path, monkeypatch, code)
    result = runner(**kwargs)
    observed = json.loads(result["stdout"].splitlines()[-1])
    assert result["rc"] == 8 and result["tag"] == "original-unit"
    assert len(result["stdout"]) == 2000 and result["stderr"] == "z" * 2000
    assert observed["cwd"] == str(tmp_path) and observed["env"]["RUN_ROOT"] == str(tmp_path / "runs")
    assert observed["argv"][:2] == ["-m", "infermatrix_copilot"]
    if runner is experiments.default_run_unit:
        assert observed["argv"][-1] == "pr=4"
        assert observed["env"]["PR_SNAPSHOT_FILE"] == str(tmp_path / "snapshot.json")
    else:
        assert observed["argv"][-1] == "meta_case=case-1"
        assert observed["env"]["IMPROVE_META_DIR"] == str(tmp_path / "meta")


@pytest.mark.parametrize("runner", [experiments.default_run_unit, experiments.meta_run_unit])
def test_shadow_deadline_preserves_raw_partial_exception_and_reaps_children(tmp_path, monkeypatch, runner):
    pidfile = tmp_path / "child.pid"
    kwargs = _shadow_entry(tmp_path, monkeypatch, _child_program(pidfile))
    try:
        with pytest.raises(subprocess.TimeoutExpired) as raised:
            runner(**kwargs, timeout=.8)
        assert raised.value.timeout == .8
        assert raised.value.output == b"partial\xff\r\n" and raised.value.stderr == b"error\x80"
        _assert_child_stopped(pidfile)
    finally:
        _cleanup_child(pidfile)


@pytest.mark.parametrize("caller", ["builtin_shell", "wheel_install"])
def test_command_callers_reap_children_and_keep_their_error_contract(tmp_path, caller):
    pidfile = tmp_path / "child.pid"
    argv = [sys.executable, "-c", _child_program(pidfile)]
    try:
        if caller == "builtin_shell":
            result = dispatch("run_shell", {"cmd": shlex.join(argv), "cwd": str(tmp_path), "timeout": .8})
            assert result["ok"] is False and "timed out after 0.8 seconds" in result["error"]
        else:
            rc, stdout, stderr = wheel._run(argv, cwd=tmp_path, env={}, timeout=.8)
            assert rc == 127 and not stdout and "timed out after 0.8 seconds" in stderr
        _assert_child_stopped(pidfile)
    finally:
        _cleanup_child(pidfile)


@pytest.mark.skipif(not Path('/proc/self/stat').exists(), reason='requires Linux procfs')
def test_reaped_work_is_not_live_while_its_zombie_pid_still_exists():
    proc = subprocess.Popen([sys.executable, '-c',
        "import sys; print('ready', flush=True); sys.stdin.readline()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == 'ready'
        assert _alive(proc.pid)  # live positive control
        proc.stdin.write('\n')
        proc.stdin.flush()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = Path(f'/proc/{proc.pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
            if state == 'Z':
                break
            time.sleep(.01)
        assert state == 'Z'  # do not reap before testing the liveness predicate
        os.kill(proc.pid, 0)  # PID existence alone still reports success
        assert not _alive(proc.pid)
    finally:
        proc.kill()
        proc.wait()
        proc.stdin.close()
        proc.stdout.close()


@pytest.mark.skipif(not Path('/proc/self/stat').exists(), reason='requires Linux procfs')
def test_deadline_waits_for_recorded_detached_child_to_finish_sigkill(tmp_path, monkeypatch):
    import threading
    from infermatrix_copilot import process_tree
    pidfile = tmp_path / 'detached.pid'
    child = ("import os,pathlib,signal,time; os.setsid(); "
             "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
             "os.close(1); os.close(2); "
             f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)")
    code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
    original_kill = os.kill
    timers = []

    def delayed_kill(pid, sig):
        if sig == signal.SIGKILL and pidfile.exists() and pid == int(pidfile.read_text()):
            timer = threading.Timer(.2, original_kill, args=(pid, sig))
            timers.append(timer)
            timer.start()
        else:
            original_kill(pid, sig)

    monkeypatch.setattr(process_tree.os, 'kill', delayed_kill)
    try:
        outcome = process_execution.run_process([sys.executable, '-c', code], cwd=tmp_path,
            env={}, deadline=time.monotonic() + .8, descendant_interval_s=.02)
        assert outcome.reason == 'absolute'
        assert timers  # the observed detached child received the kill request
        _assert_child_stopped(pidfile)
    finally:
        for timer in timers:
            timer.join()
        monkeypatch.setattr(process_tree.os, 'kill', original_kill)
        _cleanup_child(pidfile)
