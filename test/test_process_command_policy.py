"""Legacy background commands and provider-owned lifetimes stay distinct policies."""
from pathlib import Path
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from infermatrix_copilot.process_execution import run_command, run_process
from infermatrix_copilot.process_tree import _start_time


def _state(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return None


def test_process_disappearing_during_proc_read_is_stopped(monkeypatch):
    def disappeared(*args, **kwargs):
        raise ProcessLookupError('process exited during proc read')
    monkeypatch.setattr(Path, 'read_text', disappeared)
    assert _state(12345) is None


def _program(pidfile, *, redirect=False, finite=False):
    child = "import os,time; time.sleep(.3); os.write(1,b'child\\n')" if finite else 'import time; time.sleep(30)'
    output = ',stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL' if redirect else ''
    return ('import pathlib,subprocess,sys,time; '
            f'p=subprocess.Popen([sys.executable,"-c",{child!r}]{output}); '
            f'pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); '
            'print("parent",flush=True); time.sleep(.1)')


def _cleanup(pidfile):
    if pidfile.exists():
        pid = int(pidfile.read_text())
        born = _start_time(pid)
        if born is not None and _start_time(pid) == born and _state(pid) not in (None, 'Z', 'X'):
            os.kill(pid, signal.SIGKILL)


def _assert_stopped(pidfile):
    assert pidfile.exists()
    pid = int(pidfile.read_text())
    limit = time.monotonic() + 1
    while _state(pid) not in (None, 'Z', 'X') and time.monotonic() < limit:
        time.sleep(.01)
    assert _state(pid) in (None, 'Z', 'X')


def test_successful_legacy_command_releases_redirected_background_child(tmp_path):
    pidfile = tmp_path / 'child.pid'
    try:
        result = run_command([sys.executable, '-c', _program(pidfile, redirect=True)], cwd=tmp_path, env={}, timeout=3)
        assert result.returncode == 0 and result.stdout == 'parent\n'
        time.sleep(.1)
        assert _state(int(pidfile.read_text())) not in (None, 'Z', 'X')
    finally:
        _cleanup(pidfile)


def test_legacy_command_drains_inherited_pipes_after_parent_exit(tmp_path):
    pidfile = tmp_path / 'child.pid'
    try:
        result = run_command([sys.executable, '-c', _program(pidfile, finite=True)], cwd=tmp_path, env={}, timeout=3)
        assert result.returncode == 0 and result.stdout == 'parent\nchild\n'
    finally:
        _cleanup(pidfile)


def test_inherited_pipe_deadline_is_still_timeout_after_parent_exit(tmp_path):
    pidfile = tmp_path / 'child.pid'
    try:
        with pytest.raises(subprocess.TimeoutExpired) as raised:
            run_command([sys.executable, '-c', _program(pidfile)], cwd=tmp_path, env={}, timeout=.5)
        assert raised.value.output == b'parent\n' and raised.value.timeout == .5
        _assert_stopped(pidfile)
    finally:
        _cleanup(pidfile)


def test_command_cancellation_reaps_child_after_parent_exit(tmp_path):
    pidfile = tmp_path / 'child.pid'
    cancel = threading.Event()
    timer = threading.Timer(.4, cancel.set)
    timer.start()
    try:
        result = run_process([sys.executable, '-c', _program(pidfile)], cwd=tmp_path, env={},
                             deadline=time.monotonic() + 3, exit_policy='command', cancel=cancel)
        assert result.reason == 'cancelled' and result.stdout == 'parent\n'
        _assert_stopped(pidfile)
    finally:
        timer.cancel()
        _cleanup(pidfile)


def test_default_owned_provider_lifetime_still_reaps_on_success(tmp_path):
    pidfile = tmp_path / 'child.pid'
    try:
        result = run_process([sys.executable, '-c', _program(pidfile, redirect=True)], cwd=tmp_path, env={},
                             deadline=time.monotonic() + 3)
        assert result.returncode == 0 and result.reason is None and result.stdout == 'parent\n'
        _assert_stopped(pidfile)
    finally:
        _cleanup(pidfile)


def test_command_policy_cannot_release_external_cleanup_ownership(tmp_path):
    cleaned = []
    with pytest.raises(ValueError, match='external ownership'):
        run_process([sys.executable, '-c', 'raise AssertionError("must not dispatch")'],
                    cwd=tmp_path, env={}, deadline=time.monotonic() + 3,
                    exit_policy='command', cleanup=lambda: cleaned.append(True))
    assert not cleaned


@pytest.mark.parametrize('caller', ['builtin_shell', 'rebase_shell', 'import_check', 'shadow_default', 'shadow_meta', 'wheel'])
def test_migrated_command_entry_points_keep_background_success(tmp_path, monkeypatch, caller):
    import shlex
    from infermatrix_copilot.improve import experiments
    from infermatrix_copilot.rebase_engine import wheel
    from infermatrix_copilot.tools import dispatch
    from test_auxiliary_processes import _tools, _call, _shadow_entry

    pidfile = tmp_path / 'child.pid'
    program = _program(pidfile, redirect=True)
    argv = [sys.executable, '-c', program]
    try:
        if caller == 'builtin_shell':
            result = dispatch('run_shell', {'cmd': shlex.join(argv), 'cwd': str(tmp_path), 'timeout': 3})
            assert result['ok'] and result['result'] == 'exit=0\nparent\n\n'
        elif caller in {'rebase_shell', 'import_check'}:
            kwargs = {'command': shlex.join(argv), 'timeout': 3} if caller == 'rebase_shell' else {'import_code': program}
            name = 'run_shell' if caller == 'rebase_shell' else 'run_import_check'
            assert _call(_tools(tmp_path), name, **kwargs) == {'exit_code': 0, 'stdout': 'parent\n', 'stderr': ''}
        elif caller.startswith('shadow_'):
            kwargs = _shadow_entry(tmp_path, monkeypatch, program)
            runner = experiments.default_run_unit if caller == 'shadow_default' else experiments.meta_run_unit
            assert runner(**kwargs, timeout=3) == {'rc': 0, 'stdout': 'parent\n', 'stderr': '', 'tag': 'original-unit'}
        else:
            assert wheel._run(argv, cwd=tmp_path, env={}, timeout=3) == (0, 'parent\n', '')
        time.sleep(.1)
        assert _state(int(pidfile.read_text())) not in (None, 'Z', 'X')
    finally:
        _cleanup(pidfile)


def test_binary_command_preserves_output_bytes_and_nonzero_exit():
    result = run_command([sys.executable, '-c',
                          "import os; os.write(1,b'\\xff\\r\\n\\x00'); os.write(2,b'\\xfe\\r'); raise SystemExit(7)"],
                         text=False, exit_policy='owned', timeout=3)
    assert result.returncode == 7
    assert result.stdout == b'\xff\r\n\x00' and result.stderr == b'\xfe\r'
