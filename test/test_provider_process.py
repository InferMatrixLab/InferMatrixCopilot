"""Buffered CLI capture preserves provider-independent timeout and event data."""
import subprocess
import sys
import time
import os
import threading

import pytest

from infermatrix_copilot.providers.base import json_events, run_cli, stream_cli


def test_combined_output_preserves_raw_stdout_stderr_order(tmp_path):
    from infermatrix_copilot.process_execution import run_process
    chunks = []
    result = run_process([sys.executable, "-c",
        "import os; os.write(1,b'out\\xff'); os.write(2,b'err\\x80'); os.write(1,b'last')"],
        cwd=tmp_path, env={}, deadline=time.monotonic() + 3,
        merge_stderr=True, on_chunk=lambda channel, data: chunks.append((channel, data)))
    assert result.returncode == 0 and result.reason is None and result.stderr == ""
    assert {channel for channel, _ in chunks} == {"stdout"}
    assert b"".join(data for _, data in chunks) == b"out\xfferr\x80last"


def test_stopping_releases_broker_before_external_cleanup_finishes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from infermatrix_copilot.process_execution import ProcessInputChannel, run_process
    channel = ProcessInputChannel()
    launched, cleaning, release = threading.Event(), threading.Event(), threading.Event()

    def cleanup():
        cleaning.set()
        assert release.wait(3)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_process, [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path, env={}, deadline=time.monotonic() + .5,
            input_channel=channel, cleanup=cleanup,
            on_started=lambda pid, stopping: launched.set())
        try:
            assert launched.wait(3) and cleaning.wait(3)
            assert not channel.writable
            with pytest.raises(BrokenPipeError):
                channel.send("late response")
            assert not future.done()
        finally:
            release.set()
        assert future.result(timeout=3).reason == "absolute"


def test_supervisor_reaps_recorded_detached_children_after_success(tmp_path):
    from infermatrix_copilot.process_execution import run_process
    import signal
    pidfile = tmp_path / "detached.pid"
    child_code = ("import os,time,pathlib; os.setsid(); "
                  f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)")
    script = ("import subprocess,sys,time; "
              f"subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
              "time.sleep(.4); print('done')")
    child = None
    try:
        result = run_process([sys.executable, "-c", script], cwd=tmp_path, env={},
            deadline=time.monotonic() + 5, descendant_interval_s=.02,
            descendant_grace_s=.2)
        assert result.returncode == 0 and result.reason is None and result.stdout == "done\n"
        child = int(pidfile.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
    finally:
        if pidfile.exists():
            child = int(pidfile.read_text())
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass

@pytest.mark.parametrize('output', ['pipe', 'spool'])
def test_interactive_stdin_roundtrips_with_concurrent_stderr(tmp_path, output):
    from infermatrix_copilot.providers.process import ProcessInputChannel, run_process
    channel, lines = ProcessInputChannel(), []
    def observe(stream, line):
        if stream == 'stdout':
            lines.append(line)
            if line.startswith('request:'):
                channel.send(line.replace('request:', 'reply:')+'\n')
            elif line == 'done':
                channel.close()
    code = r"""
import sys
for n in range(2):
    print('request:'+str(n),flush=True)
    sys.stderr.write('diagnostic\n'*10000);sys.stderr.flush()
    assert sys.stdin.readline()=='reply:'+str(n)+'\n'
print('done',flush=True)
"""
    result = run_process([sys.executable,'-u','-c',code], cwd=tmp_path, env={},
                         deadline=time.monotonic()+10, output=output,
                         input_channel=channel,on_line=observe)
    assert result.returncode==0 and result.reason is None
    assert lines==['request:0','request:1','done']
    assert result.stderr=='diagnostic\n'*20000
    with pytest.raises(BrokenPipeError):
        channel.send('another reply')
    with pytest.raises(ValueError,match='already belongs'):
        run_process([sys.executable,'-c','raise Exception("must not dispatch")'],
                    cwd=tmp_path,env={},deadline=time.monotonic()+1,input_channel=channel)


def test_interactive_blocked_delivery_is_released_by_supervised_deadline(tmp_path):
    from infermatrix_copilot.providers.process import ProcessInputChannel, run_process
    channel,cleaned=ProcessInputChannel(),[]
    def observe(stream,line):
        if stream=='stdout':channel.send('x'*4000000)
    started=time.monotonic()
    with pytest.raises(BrokenPipeError):
        run_process([sys.executable,'-u','-c',"import time;print('request',flush=True);time.sleep(30)"],
            cwd=tmp_path,env={},deadline=started+.7,input_channel=channel,
            on_line=observe,cleanup=lambda:cleaned.append(True))
    assert time.monotonic()-started<4 and cleaned==[True]


def test_completed_protocol_shortens_deadline_without_domain_termination(tmp_path):
    import threading
    from infermatrix_copilot.providers.process import run_process
    completed=threading.Event()
    result=run_process([sys.executable,'-u','-c',"import time;print('result',flush=True);time.sleep(30)"],
        cwd=tmp_path,env={},deadline=time.monotonic()+10,completed=completed,
        completion_grace_s=.1,on_line=lambda *_:completed.set())
    assert result.stdout=='result\n' and result.reason=='absolute'


@pytest.mark.parametrize("mode", ["success", "absolute", "cancelled", "observer"])
def test_external_cleanup_runs_once_on_every_terminal_path(tmp_path, mode):
    import threading
    from infermatrix_copilot.providers.process import run_process

    cleaned, cancel = [], threading.Event()

    def observe(channel, line):
        if channel == "stdout" and line == "ready":
            if mode == "cancelled":
                cancel.set()
            elif mode == "observer":
                raise OSError("journal failed")

    def cleanup():
        # External cleanup itself uses the same supervisor, without recursion
        # into the parent's resource ownership or its expired deadline.
        result = run_process([sys.executable, "-c", "print('removed')"],
            cwd=tmp_path, env={}, deadline=time.monotonic() + 3)
        cleaned.append(result.stdout)

    kwargs = dict(cwd=tmp_path, env={}, deadline=time.monotonic() + .5,
                  on_line=observe, cancel=cancel, cleanup=cleanup)
    command = [sys.executable, "-u", "-c",
               "import time; print('ready', flush=True); time.sleep(" +
               ("0" if mode == "success" else "30") + ")"]
    if mode == "observer":
        with pytest.raises(OSError, match="journal failed"):
            run_process(command, **kwargs)
    else:
        result = run_process(command, **kwargs)
        assert result.reason == (None if mode == "success" else mode)
        assert result.stdout == "ready\n"
    assert cleaned == ["removed\n"]


def test_external_cleanup_runs_when_output_observer_is_blocked(tmp_path):
    import threading
    from infermatrix_copilot.providers.process import run_process

    cleaned = threading.Event()
    observed_cleanup = []
    result = run_process([sys.executable, "-u", "-c",
                          "import time; print('ready', flush=True); time.sleep(30)"],
        cwd=tmp_path, env={}, deadline=time.monotonic() + .5,
        cleanup=cleaned.set,
        on_line=lambda *_: observed_cleanup.append(cleaned.wait(3)))
    assert result.reason == "absolute"
    assert observed_cleanup == [True]


def test_external_cleanup_failure_preserves_output_and_requires_reconciliation(tmp_path):
    from infermatrix_copilot.providers.process import ProcessCleanupError, run_process

    def cleanup():
        raise OSError("daemon unreachable")

    with pytest.raises(ProcessCleanupError) as failure:
        run_process([sys.executable, "-c", "print('native receipt')"],
            cwd=tmp_path, env={}, deadline=time.monotonic() + 3, cleanup=cleanup)
    assert failure.value.outcome.stdout == "native receipt\n"
    assert failure.value.outcome.returncode == 0
    assert str(failure.value.__cause__) == "daemon unreachable"


def test_observer_failure_retains_external_cleanup_failure(tmp_path):
    from infermatrix_copilot.providers.process import run_process

    def cleanup():
        raise OSError("daemon unreachable")

    def observe(*_):
        raise ValueError("journal unavailable")

    with pytest.raises(ValueError, match="journal unavailable") as failure:
        run_process([sys.executable, "-u", "-c",
                     "import time; print('ready', flush=True); time.sleep(30)"],
            cwd=tmp_path, env={}, deadline=time.monotonic() + 3,
            on_line=observe, cleanup=cleanup)
    assert str(failure.value.process_cleanup_error) == "daemon unreachable"


@pytest.mark.parametrize("returncode", [0, 42])
def test_early_child_exit_preserves_output_when_stdin_is_not_consumed(tmp_path, returncode):
    stdout, stderr, code, timed_out = run_cli(
        [sys.executable, "-c", f"import sys; print('final result'); sys.exit({returncode})"],
        cwd=tmp_path, env={}, timeout_s=5, input="unused input" * 100000)
    assert (stdout, stderr, code, timed_out) == ("final result\n", "", returncode, False)


@pytest.mark.parametrize("partial", [b'{"result":"partial"}\n', '{"result":"partial"}\n', None])
def test_partial_stdout_survives_timeout_without_claiming_a_completed_process(monkeypatch, partial):
    def timeout(cmd, **kwargs):
        assert cmd == ["native-cli"] and kwargs["cwd"] == "/isolated"
        assert kwargs["env"] == {"HOME": "/auth-home"} and kwargs["input"] == "evidence"
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], output=partial)

    monkeypatch.setattr(subprocess, "run", timeout)
    stdout, stderr, code, timed_out = run_cli(["native-cli"], cwd="/isolated",
        env={"HOME": "/auth-home"}, input="evidence", timeout_s=10, runner=timeout)
    assert timed_out and code == 0 and stderr == ""
    assert json_events(stdout) == ([{"result": "partial"}] if partial else [])


def test_exit_diagnostics_and_no_stdin_policy_remain_available_to_each_provider(monkeypatch):
    def failed(cmd, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["input"] is None
        return subprocess.CompletedProcess(cmd, 7, 'warning\n{"type":"result"}\n', "login expired")

    monkeypatch.setattr(subprocess, "run", failed)
    stdout, stderr, code, timed_out = run_cli(["native-cli"], cwd="/isolated", env={},
                                             stdin=subprocess.DEVNULL, timeout_s=10, runner=failed)
    assert (stderr, code, timed_out) == ("login expired", 7, False)
    assert json_events(stdout) == [{"type": "result"}]


def test_json_events_ignore_noise_and_broken_frames_in_original_order():
    assert json_events('warning\n{broken\n  {"type":"first"}\n[]\n{"type":"last"}\n') == [
        {"type": "first"}, {"type": "last"}]


def test_stream_drains_both_pipes_and_keeps_unicode_stdin_and_unterminated_tails(tmp_path):
    code = ('import sys; text=sys.stdin.read(); '
            'sys.stderr.write("x"*200000+"\\nerror tail"); '
            'sys.stdout.write(str(len(text))+"\\noutput tail")')
    lines = []
    stdout, stderr, rc, timeout = stream_cli([sys.executable, "-u", "-c", code],
        cwd=tmp_path, env={}, deadline=time.monotonic() + 5, input="知识" * 100000,
        on_line=lambda channel, text: lines.append((channel, text)))
    assert (stdout, rc, timeout) == ("200000\noutput tail", 0, None)
    assert stderr == "x" * 200000 + "\nerror tail"
    assert ("stderr", "error tail") in lines and ("stdout", "output tail") in lines


@pytest.mark.parametrize("absolute,idle,expected", [(2, .1, None), (.2, .5, "absolute"), (2, .02, "idle")])
def test_stream_idle_tracks_non_json_stdout_and_absolute_deadline_stays_fixed(tmp_path, absolute, idle, expected):
    # Absolute wins even with cold startup; the success case separately proves
    # that non-JSON heartbeats reset idle. A shorter idle during startup would
    # legitimately win and make this case depend on host scheduling.
    startup = .15 if expected == "absolute" else 0
    code = f'import time; time.sleep({startup}); [(print("still working", flush=True), time.sleep(.04)) for _ in range(8)]'
    stdout, _, _, timeout = stream_cli([sys.executable, "-u", "-c", code],
        cwd=tmp_path, env={}, deadline=time.monotonic() + absolute, idle_timeout_s=idle)
    assert timeout == expected
    if expected is None:
        assert stdout.count("still working") == 8


def test_interrupted_stream_preserves_already_received_tail_and_original_error(tmp_path):
    lines = []
    def fail(channel, text):
        lines.append((channel, text))
        if text == "first":
            raise OSError("journal unavailable")
    code = 'import os,time; os.write(1,b"first\\npartial tail"); time.sleep(10)'
    with pytest.raises(OSError, match="journal unavailable"):
        stream_cli([sys.executable, "-u", "-c", code], cwd=tmp_path, env={},
                   deadline=time.monotonic() + 5, on_line=fail)
    assert lines == [("stdout", "first"), ("stdout", "partial tail")]


def test_exited_leader_cannot_leave_descendant_pipes_holding_the_call_open(tmp_path):
    code = ('import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(10)"]); '
            'print("leader complete", flush=True)')
    started = time.monotonic()
    stdout, _, rc, timeout = stream_cli([sys.executable, "-u", "-c", code],
        cwd=tmp_path, env={}, deadline=started + 3)
    assert (stdout, rc, timeout) == ("leader complete\n", 0, None)
    assert time.monotonic() - started < 2


def test_stalled_stdin_writer_cannot_outlive_absolute_deadline(tmp_path):
    started = time.monotonic()
    _, _, _, timeout = stream_cli([sys.executable, "-u", "-c", "import time; time.sleep(10)"],
        cwd=tmp_path, env={}, deadline=started + .1, input="x" * 2_000_000)
    assert timeout == "absolute" and time.monotonic() - started < 2


def test_closed_stdin_does_not_disable_deadline_for_a_live_child(tmp_path):
    started = time.monotonic()
    _, _, _, timeout = stream_cli(
        [sys.executable, "-u", "-c", "import os,time; os.close(0); time.sleep(10)"],
        cwd=tmp_path, env={}, deadline=started + .2, input="x" * 2_000_000)
    assert timeout == "absolute"
    assert time.monotonic() - started < 2


def test_buffered_capture_reaps_descendants_after_leader_exit(tmp_path):
    code = ('import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
            'print("complete", flush=True)')
    started = time.monotonic()
    stdout, stderr, rc, timed_out = run_cli([sys.executable, "-u", "-c", code],
        cwd=tmp_path, env={}, timeout_s=3)
    assert (stdout, stderr, rc, timed_out) == ("complete\n", "", 0, False)
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("output", ["pipe", "spool"])
def test_explicit_cancellation_preserves_partial_output_and_stops_production(tmp_path, output):
    import threading
    from infermatrix_copilot.providers.process import run_process

    cancel = threading.Event()
    result = run_process([sys.executable, "-u", "-c", 'import time; print("partial", flush=True); time.sleep(30)'],
        cwd=tmp_path, env={}, deadline=time.monotonic() + 5, output=output,
        on_line=lambda *args: cancel.set(), cancel=cancel)
    assert result.stdout == "partial\n"
    assert result.reason == "cancelled"
    assert result.returncode != 0


def test_buffered_timeout_retains_partial_stdout(tmp_path):
    stdout, _, code, timed_out = run_cli(
        [sys.executable, "-u", "-c", 'import time; print("partial", flush=True); time.sleep(30)'],
        cwd=tmp_path, env={}, timeout_s=.3)
    assert (stdout, code, timed_out) == ("partial\n", 0, True)
