"""Known owned work can remain live even after its CLI leader has stopped."""
from pathlib import Path
from types import SimpleNamespace
import os
import signal
import sys
import time

import pytest

from infermatrix_copilot import process_execution, process_tree
from infermatrix_copilot.providers.outcomes import requires_reconciliation


@pytest.mark.skipif(not Path('/proc/self/stat').exists(), reason='requires Linux procfs')
@pytest.mark.parametrize('ending', ['deadline', 'observer'])
def test_surviving_owned_child_is_not_a_terminal_cli_failure(tmp_path, monkeypatch, ending):
    pidfile = tmp_path / 'child.pid'
    child = ("import os,pathlib,signal,time; os.setsid(); "
             "signal.signal(signal.SIGTERM,signal.SIG_IGN); os.close(1); os.close(2); "
             f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)")
    code = (f"import subprocess,sys,time,pathlib; subprocess.Popen([sys.executable,'-c',{child!r}]); "
            f"p=pathlib.Path({str(pidfile)!r}); "
            "exec('while not p.exists(): time.sleep(.01)'); "
            "print('ready',flush=True); time.sleep(30)")
    original = os.kill

    def deny_owned_child(pid, sig):
        if sig in (signal.SIGTERM, signal.SIGKILL) and pidfile.exists() and pid == int(pidfile.read_text()):
            raise PermissionError('injected owned child cannot be signalled')
        return original(pid, sig)

    def observe(*_):
        if ending == 'observer':
            raise ValueError('journal unavailable')

    monkeypatch.setattr(process_tree.os, 'kill', deny_owned_child)
    try:
        expected = process_execution.ProcessCleanupError if ending == 'deadline' else ValueError
        with pytest.raises(expected) as caught:
            process_execution.run_process([sys.executable, '-u', '-c', code], cwd=tmp_path,
                env={}, deadline=time.monotonic() + .6, on_line=observe, descendant_interval_s=.02)
        pid = int(pidfile.read_text())
        assert process_tree._alive(pid)  # positive control: this work really remains live
        assert requires_reconciliation(caught.value)
        error = caught.value.process_cleanup_error
        assert error.survivors[pid] == process_tree._start_time(pid)
        if ending == 'deadline':
            assert caught.value.outcome.reason == 'absolute'
            assert caught.value.outcome.stdout == 'ready\n'
        else:
            assert caught.value.process_outcome.stdout == 'ready\n'
    finally:
        monkeypatch.setattr(process_tree.os, 'kill', original)
        if pidfile.exists():
            try: original(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError: pass


@pytest.mark.parametrize('wrapped', [False, True])
def test_native_cleanup_unknown_cannot_dispatch_fallback(wrapped):
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole, ModelUnavailable
    from infermatrix_copilot.budgeting import bind_call_budget
    calls, records, settlements = [], [], []
    error = process_execution.ProcessCleanupError(process_execution.ProcessOutcome('partial', '', 0))
    if wrapped:
        observer = ValueError('journal unavailable')
        observer.process_cleanup_error = error
        error = observer

    def complete(**kwargs):
        calls.append(kwargs['model'])
        raise error

    role = ModelRole('generator', 'fixture', 'primary',
                     fallback=ModelRole('generator', 'fixture', 'fallback'))
    gateway = ModelGateway(None, transport_factory=lambda _: SimpleNamespace(complete=complete), recorder=records.append)
    with bind_call_budget('test', lambda _: 'ticket', lambda ticket, facts: settlements.append(facts)):
        with pytest.raises(ModelUnavailable) as caught:
            gateway.call_json(role, system='frozen instructions', prompt='evidence')
    assert calls == ['primary']
    assert caught.value.native_outcome_pending and caught.value.allow_fallback is False
    assert len(records) == len(settlements) == 1
    assert settlements[0].native_outcome_pending


@pytest.mark.skipif(not Path('/proc/self/stat').exists(), reason='requires Linux procfs')
@pytest.mark.parametrize('blocked_input', [False, True])
def test_unstoppable_leader_and_its_pipes_return_pending_without_hanging(tmp_path, monkeypatch, blocked_input):
    import threading
    identities, failures = [], []
    original_kill, original_group = os.kill, os.killpg

    def deny_owned(pid, sig):
        if identities and pid == identities[0][0] and sig in (signal.SIGTERM, signal.SIGKILL):
            raise PermissionError('injected original leader cannot be signalled')
        return original_kill(pid, sig)

    def deny_group(pgid, sig):
        if identities and pgid == identities[0][0]:
            raise PermissionError('injected owned group cannot be signalled')
        return original_group(pgid, sig)

    monkeypatch.setattr(os, 'kill', deny_owned)
    monkeypatch.setattr(os, 'killpg', deny_group)

    def run():
        try:
            process_execution.run_process([sys.executable, '-u', '-c',
                "import time; print('ready',flush=True); time.sleep(30)"], cwd=tmp_path, env={},
                input='x' * 2_000_000 if blocked_input else None,
                deadline=time.monotonic() + .3,
                on_started=lambda pid, stopping: identities.append((pid, process_tree._start_time(pid))))
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        thread.join(timeout=8)
        assert not thread.is_alive(), 'unresolved work must not hang the supervisor'
        assert len(failures) == 1 and isinstance(failures[0], process_execution.ProcessCleanupError)
        pid, birth = identities[0]
        assert birth is not None and process_tree._alive(pid)
        error = failures[0]
        assert error.leader_returncode is None
        assert error.outcome.returncode == -1 and error.outcome.reason == 'absolute'
        assert error.outcome.stdout == 'ready\n'
        assert error.process_cleanup_error.survivors[pid] == birth
        assert requires_reconciliation(error)
    finally:
        monkeypatch.setattr(os, 'kill', original_kill)
        monkeypatch.setattr(os, 'killpg', original_group)
        if identities:
            pid, birth = identities[0]
            if birth is not None and process_tree._start_time(pid) == birth:
                original_kill(pid, signal.SIGKILL)
            thread.join(timeout=5)
            try: os.waitpid(pid, 0)
            except ChildProcessError: pass
