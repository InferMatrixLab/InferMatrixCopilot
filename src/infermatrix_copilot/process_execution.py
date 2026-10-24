"""Process ownership shared by buffered, pipe and regular-file CLI output.

Provider protocols and domain accounting are deliberately outside this module.
The deadline guard runs independently of consumers: a blocked event journal must
not keep the producer alive, and draining a completed producer is not a timeout.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
import os
import locale
import math
import queue
import subprocess
import tempfile
import threading
import time
import uuid

from .process_tree import OwnedProcessTree


@dataclass(frozen=True)
class ProcessOutcome:
    stdout: str
    stderr: str
    returncode: int
    reason: str | None = None

    @classmethod
    def from_dict(cls, value):
        """Canonical persisted/wire outcome validation for every location."""
        if (not isinstance(value, dict) or set(value) != {'stdout', 'stderr', 'returncode', 'reason'}
                or not isinstance(value['stdout'], str) or not isinstance(value['stderr'], str)
                or type(value['returncode']) is not int
                or value['reason'] not in (None, 'absolute', 'idle', 'cancelled')):
            raise ValueError('invalid process outcome')
        return cls(**value)


class ProcessCleanupError(RuntimeError):
    """The client stopped, but its externally owned work may still be running."""

    def __init__(self, outcome: ProcessOutcome):
        super().__init__("external process cleanup failed; reconciliation is required")
        self.outcome = outcome

    @classmethod
    def from_facts(cls, value):
        """Decode producer diagnostics without assigning this host's identity."""
        if (not isinstance(value, dict) or 'process_cleanup' not in value
                or set(value) - {'process_cleanup', 'native_process'}):
            raise ValueError('invalid process cleanup envelope')
        diagnostics = validate_cleanup_diagnostics(value['process_cleanup'])
        outcome = (ProcessOutcome.from_dict(value['native_process']) if 'native_process' in value
                   else ProcessOutcome('', '', -1))
        error = cls(outcome)
        error.cleanup_diagnostics = diagnostics
        error.process_outcome_available = 'native_process' in value
        error.leader_returncode = diagnostics['leader_returncode']
        return error


class OwnedProcessCleanupError(RuntimeError):
    """Recorded local work did not stop; PID identities are diagnostics only."""

    def __init__(self, survivors, prior_error=None):
        super().__init__("owned processes remain unresolved after bounded cleanup")
        self.survivors = dict(survivors)
        self.prior_error = prior_error
        self.scope = _cleanup_scope()


def _cleanup_scope():
    """Capture the producer's boot/namespace; unavailable facts stay unknown."""
    try:
        boot = str(uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()))
    except (OSError, ValueError):
        boot = None
    try:
        namespace = os.stat('/proc/self/ns/pid').st_ino
    except OSError:
        namespace = None
    return {'boot_id': boot, 'pid_namespace': namespace}


def validate_cleanup_diagnostics(value):
    """Validate persisted diagnostics, never recovery or signalling authority."""
    if (not isinstance(value, dict) or set(value) != {
            'schema', 'scope', 'survivors', 'leader_returncode', 'error_types'}
            or value['schema'] != 'owned-process-cleanup/1'):
        raise ValueError('invalid process cleanup diagnostics')
    scope = value['scope']
    if not isinstance(scope, dict) or set(scope) != {'boot_id', 'pid_namespace'}:
        raise ValueError('invalid process cleanup scope')
    boot, namespace = scope['boot_id'], scope['pid_namespace']
    if boot is not None:
        if not isinstance(boot, str) or str(uuid.UUID(boot)) != boot:
            raise ValueError('invalid process cleanup boot identity')
    if namespace is not None and (type(namespace) is not int or namespace <= 0):
        raise ValueError('invalid process cleanup namespace')
    if value['leader_returncode'] is not None and type(value['leader_returncode']) is not int:
        raise ValueError('invalid process cleanup leader outcome')
    if not isinstance(value['survivors'], list) or not isinstance(value['error_types'], list):
        raise ValueError('invalid process cleanup observations')
    seen = set()
    for row in value['survivors']:
        if (not isinstance(row, dict) or set(row) != {'pid', 'start_time'}
                or type(row['pid']) is not int or row['pid'] <= 0 or row['pid'] in seen
                or row['start_time'] is not None and
                (type(row['start_time']) is not int or row['start_time'] < 0)):
            raise ValueError('invalid process cleanup survivor identity')
        seen.add(row['pid'])
    if any(not isinstance(name, str) or not name or len(name) > 128 for name in value['error_types']):
        raise ValueError('invalid process cleanup error types')
    return {**value, 'scope': dict(scope), 'survivors': [dict(row) for row in value['survivors']],
            'error_types': list(value['error_types'])}


def process_cleanup_facts(error):
    """Project one cleanup error into portable, explicitly retained trace facts."""
    if not isinstance(error, ProcessCleanupError) and getattr(error, 'process_cleanup_error', None) is None:
        return {}
    pending, seen, errors, local = [error], set(), [], None
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        errors.append(type(item).__name__)
        if local is None and isinstance(item, OwnedProcessCleanupError):
            local = item
        pending.extend(other for name in ('process_cleanup_error', 'prior_error', 'external_cleanup_error')
                       if isinstance(other := getattr(item, name, None), BaseException))
    retained = getattr(error, 'cleanup_diagnostics', None)
    facts = validate_cleanup_diagnostics(retained if retained is not None else {
        'schema': 'owned-process-cleanup/1',
        'scope': dict(local.scope) if local is not None else {'boot_id': None, 'pid_namespace': None},
        'survivors': [{'pid': pid, 'start_time': birth} for pid, birth in sorted(local.survivors.items())]
                     if local is not None else [],
        'leader_returncode': getattr(error, 'leader_returncode', None),
        'error_types': sorted(set(errors)),
    })
    result = {'process_cleanup': facts}
    outcome = error.outcome if isinstance(error, ProcessCleanupError) else getattr(error, 'process_outcome', None)
    if isinstance(outcome, ProcessOutcome) and getattr(error, 'process_outcome_available', True):
        from dataclasses import asdict
        result['native_process'] = asdict(outcome)
    return result


def run_command(cmd, *, cwd=None, env=None, timeout=None, encoding=None, errors="strict", shell=False,
                text=True, exit_policy="command"):
    """Translate buffered command contracts onto the shared supervisor.

    As with ``subprocess.run(text=True)``, text uses the current locale and
    universal newlines, while ``TimeoutExpired`` retains raw partial bytes.
    Inherited stdin/environment and nonzero exit codes keep their old meaning.
    Successful commands retain background tasks only after both output pipes
    close. Deadline/cancellation still clean up the owned tree.
    ``text=False`` preserves raw bytes; ``exit_policy="owned"`` also cleans up
    descendants after successful completion. The defaults retain existing command
    behavior. Process ownership and deadlines remain in ``run_process``.
    """
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    outcome = run_process(cmd, cwd=cwd, env=env, stdin=0, shell=shell, exit_policy=exit_policy,
        deadline=time.monotonic() + timeout if timeout is not None else float("inf"),
        on_chunk=lambda channel, data: captured[channel].extend(data))
    if outcome.reason is not None:
        raise subprocess.TimeoutExpired(cmd, timeout,
            output=bytes(captured["stdout"]), stderr=bytes(captured["stderr"]))
    if not text:
        return subprocess.CompletedProcess(cmd, outcome.returncode,
                                           bytes(captured["stdout"]), bytes(captured["stderr"]))
    encoding = encoding or locale.getpreferredencoding(False)
    stdout, stderr = (bytes(value).decode(encoding, errors).replace("\r\n", "\n").replace("\r", "\n")
                      for value in captured.values())
    return subprocess.CompletedProcess(cmd, outcome.returncode, stdout, stderr)


class ProcessInputChannel:
    """One supervised request/response stdin stream.

    Sending waits for delivery, preserving the broker's one-request-at-a-time
    contract. The supervisor's stop signal also releases a blocked sender.
    This channel owns no process, deadline, model policy or accounting.
    """
    def __init__(self):
        self._pending = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._stopped = None
        self._closed = False
        self._sending = False

    def _claim(self, stopped):
        with self._lock:
            if self._stopped is not None:
                raise ValueError("stdin channel already belongs to a process")
            self._stopped = stopped

    @property
    def writable(self):
        with self._lock:
            return self._stopped is not None and not self._closed and not self._stopped.is_set()

    def send(self, value):
        if not isinstance(value, (str, bytes)):
            raise ValueError("stdin channel accepts text or bytes")
        packet = (value.encode() if isinstance(value, str) else value, threading.Event(), [])
        with self._lock:
            if self._stopped is None or self._closed or self._stopped.is_set():
                raise BrokenPipeError("supervised stdin is unavailable")
            if self._sending:
                raise ValueError("stdin channel has an outstanding response")
            try:
                self._pending.put_nowait(packet)
            except queue.Full as exc:
                raise ValueError("stdin channel has an outstanding response") from exc
            self._sending = True
        try:
            while not packet[1].wait(.02):
                if self._stopped.is_set():
                    raise BrokenPipeError("supervised stdin stopped before delivery")
            if packet[2]:
                raise packet[2][0]
        finally:
            with self._lock:
                self._sending = False

    def close(self):
        with self._lock:
            self._closed = True

    def _packets(self):
        while not self._stopped.is_set():
            try:
                yield self._pending.get(timeout=.02)
            except queue.Empty:
                if self._closed:
                    return


def run_process(cmd, *, cwd, env, deadline, input=None, stdin=None,
                idle_timeout_s=None, on_line=None, termination_grace_s=0,
                output="pipe", cancel=None, cleanup=None, on_chunk=None, input_channel=None,
                completed=None, completion_grace_s=None, on_started=None,
                merge_stderr=False, descendant_interval_s=.5, descendant_grace_s=0, shell=False, exit_policy="owned"):
    """Run an owned process tree; ``cancel`` may be a threading.Event.

    ``spool`` uses private anonymous regular files and positional reads, preserving
    producer offsets and avoiding pipe backpressure. It requires POSIX pread.
    ``cleanup`` is a bounded, idempotent external-resource cleanup operation.
    It runs once in the stopping thread, even when an output observer is blocked.
    Callers must retain external identities when it fails. This does not make
    the local supervisor capable of killing processes at a higher privilege.
    ``on_chunk(channel, bytes)`` preserves stream bytes for nested workers;
    ``on_line`` remains the existing decoded, delimiter-free event callback.
    ``input_channel`` supplies interactive replies through the same supervised
    writer. It cannot be combined with fixed input or an inherited stdin.
    A protocol owner can signal ``completed`` to shorten production's remaining
    deadline to ``completion_grace_s``; the supervisor still owns termination.
    ``on_started(pid, stopping)`` attaches domain observers without exposing a process
    lifecycle. ``merge_stderr`` preserves combined test-log byte ordering.
    Descendant identities and their observation cadence belong to this mechanism.
    ``shell`` is explicit for existing authorized command tools; providers keep
    direct argument execution by default.
    ``owned`` cleans up the full tree when its leader exits. ``command`` retains
    background children after successful pipe EOF, as the legacy text commands
    did; inherited pipes must still finish before the absolute deadline.
    External resources and interactive brokers always retain owned lifetimes.
    """
    if exit_policy not in {"owned", "command"}:
        raise ValueError("unsupported process exit policy")
    if exit_policy == "command" and (output != "pipe" or cleanup is not None or input_channel is not None):
        raise ValueError("command exit policy requires buffered pipes without external ownership")
    if output not in {"pipe", "spool"}:
        raise ValueError("unsupported process output strategy")
    if (type(shell) is not bool or type(merge_stderr) is not bool or type(descendant_interval_s) not in (int, float)
            or not math.isfinite(descendant_interval_s) or descendant_interval_s <= 0):
        raise ValueError("invalid process observation configuration")
    if any(type(grace) not in (int, float) or not math.isfinite(grace) or grace < 0
           for grace in (termination_grace_s, descendant_grace_s)):
        raise ValueError("invalid process termination configuration")
    if output == "spool" and not hasattr(os, "pread"):
        raise ValueError("regular-file spooling requires positional reads")
    if input is not None and stdin is not None:
        raise ValueError("input and stdin cannot both be supplied")
    if input_channel is not None and (not isinstance(input_channel, ProcessInputChannel)
                                     or input is not None or stdin is not None):
        raise ValueError("invalid interactive stdin configuration")
    if ((completed is None) != (completion_grace_s is None)
            or completion_grace_s is not None and (type(completion_grace_s) not in (int, float)
                or not math.isfinite(completion_grace_s) or completion_grace_s < 0)):
        raise ValueError("invalid completion deadline configuration")
    stopped = threading.Event()
    stopping = threading.Event()
    writes_input = input is not None or input_channel is not None
    with ExitStack() as resources:
        spools = ({channel: resources.enter_context(tempfile.TemporaryFile(
            mode="w+b", buffering=0, dir=cwd)) for channel in ("stdout", "stderr")}
            if output == "spool" else {})
        if input_channel is not None:
            input_channel._claim(stopping)
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=env,
                stdin=subprocess.PIPE if writes_input else (subprocess.DEVNULL if stdin is None else stdin),
                stdout=spools.get("stdout", subprocess.PIPE),
                stderr=subprocess.STDOUT if merge_stderr else spools.get("stderr", subprocess.PIPE),
                **({"shell": True} if shell else {}),
                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == "nt" else {"start_new_session": True}))
        except BaseException:
            stopping.set()
            stopped.set()
            if input_channel is not None:
                input_channel.close()
            raise
        pending = queue.Queue()
        stop_lock = threading.Lock()
        reason = None
        cleanup_error = None
        idle_deadline = time.monotonic() + idle_timeout_s if idle_timeout_s is not None else float("inf")
        owned = OwnedProcessTree(proc, descendant_interval_s) if os.name != "nt" else None

        streams_closed = {channel: threading.Event() for channel in ("stdout", "stderr")}

        def pipes_closed():
            return all(closed.is_set() for closed in streams_closed.values())

        def stop(cause=None, preserve_background=False):
            nonlocal reason, cleanup_error
            with stop_lock:
                if stopped.is_set():
                    return
                stopping.set()
                # Deadlines apply to production, not subsequent journal draining.
                reason = cause if proc.poll() is None or (exit_policy == "command" and not pipes_closed()) else None
                survivors = {}
                try:
                    if preserve_background:
                        if owned is not None:
                            owned.stop_observing()
                    elif os.name == "nt":
                        try:
                            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                           capture_output=True, check=False, timeout=5)
                        except (OSError, subprocess.TimeoutExpired):
                            if proc.poll() is None:
                                proc.kill()
                    else:
                        survivors = owned.terminate(termination_grace_s, descendant_grace_s) or {}
                except BaseException as exc:
                    cleanup_error = exc
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    survivors[proc.pid] = owned.identity.get(proc.pid) if owned is not None else None
                if survivors:
                    cleanup_error = OwnedProcessCleanupError(survivors, cleanup_error)
                try:
                    if cleanup is not None:
                        cleanup()
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                    else:
                        cleanup_error.external_cleanup_error = exc
                finally:
                    stopped.set()

        def guard():
            completion_deadline = float("inf")
            while not stopped.is_set():
                now = time.monotonic()
                if completed is not None and completed.is_set() and completion_deadline == float("inf"):
                    completion_deadline = now + completion_grace_s
                absolute_deadline = min(deadline, completion_deadline)
                if proc.poll() is not None and (exit_policy == "owned" or pipes_closed()):
                    stop(preserve_background=exit_policy == "command")
                elif cancel is not None and cancel.is_set():
                    stop("cancelled")
                elif now >= absolute_deadline:
                    stop("absolute")
                elif now >= idle_deadline:
                    stop("idle")
                else:
                    stopped.wait(min(.02, max(.001, min(absolute_deadline, idle_deadline) - now)))

        def read(channel):
            nonlocal idle_deadline
            stream = getattr(proc, channel)
            try:
                while stream is not None and (chunk := os.read(stream.fileno(), 65536)):
                    if channel == "stdout" and chunk.strip() and idle_timeout_s is not None:
                        idle_deadline = time.monotonic() + idle_timeout_s
                    pending.put((channel, chunk))
            except OSError as exc:
                pending.put(("error", exc))
            finally:
                streams_closed[channel].set()
                pending.put((channel, None))

        def read_spools():
            nonlocal idle_deadline
            offsets = {channel: 0 for channel in spools}
            try:
                while True:
                    complete = stopped.is_set()
                    changed = False
                    # Preserve the spool strategy's stdout-before-stderr sweep.
                    for channel, stream in spools.items():
                        chunk = os.pread(stream.fileno(), 65536, offsets[channel])
                        if chunk:
                            changed = True
                            offsets[channel] += len(chunk)
                            if channel == "stdout" and chunk.strip() and idle_timeout_s is not None:
                                idle_deadline = time.monotonic() + idle_timeout_s
                            pending.put((channel, chunk))
                    if complete and not changed:
                        break
                    if not changed:
                        stopped.wait(.02)
            except OSError as exc:
                pending.put(("error", exc))
            finally:
                for channel in spools:
                    pending.put((channel, None))

        def write():
            try:
                if input_channel is None:
                    proc.stdin.write(input.encode("utf-8") if isinstance(input, str) else input)
                else:
                    for value, delivered, errors in input_channel._packets():
                        try:
                            proc.stdin.write(value)
                            proc.stdin.flush()
                        except OSError as exc:
                            errors.append(exc)
                            raise
                        finally:
                            delivered.set()
            except BrokenPipeError:
                # A child may finish or reject the request without consuming
                # stdin. Preserve its output and exit status, as communicate()
                # does, rather than replacing them with a writer failure.
                pass
            except OSError as exc:
                pending.put(("error", exc))
            finally:
                if input_channel is not None:
                    input_channel.close()
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
                pending.put(("stdin", None))

        received = {"stdout": [], "stderr": []}
        buffers = {channel: b"" for channel in received}
        finished = set() if writes_input else {"stdin"}

        def emit(channel, raw, best_effort=False):
            if on_line is not None:
                try:
                    on_line(channel, raw.decode("utf-8", "replace"))
                except BaseException:
                    if not best_effort:
                        raise

        def consume(channel, chunk, best_effort=False):
            if channel == "error":
                if reason is None and not best_effort:
                    raise chunk
            elif chunk is None:
                finished.add(channel)
                if channel in buffers and buffers[channel]:
                    raw, buffers[channel] = buffers[channel], b""
                    emit(channel, raw, best_effort)
            else:
                received[channel].append(chunk)
                buffers[channel] += chunk
                if on_chunk is not None:
                    try:
                        on_chunk(channel, chunk)
                    except BaseException:
                        if not best_effort:
                            raise
                while b"\n" in buffers[channel]:
                    raw, buffers[channel] = buffers[channel].split(b"\n", 1)
                    emit(channel, raw, best_effort)

        workers = ([threading.Thread(target=read_spools, daemon=True)] if spools else
                   [threading.Thread(target=read, args=(channel,), daemon=True) for channel in received])
        if writes_input:
            workers.append(threading.Thread(target=write, daemon=True))
        watchdog = threading.Thread(target=guard, daemon=True)
        started = []
        try:
            if owned is not None:
                owned.start()
            for worker in [watchdog, *workers]:
                worker.start()
                started.append(worker)
            if on_started is not None:
                on_started(proc.pid, stopping)
            while len(finished) < 3:
                try:
                    packet = pending.get(timeout=.02)
                except queue.Empty:
                    if stopped.is_set() and cleanup_error is not None:
                        break  # unresolved work may retain its inherited pipes
                    continue
                consume(*packet)
            # All pipes may close while the producer is still doing work.
            stopped.wait()
        except BaseException as exc:
            stop()
            if cleanup_error is not None:
                exc.process_cleanup_error = cleanup_error
            for worker in started:
                worker.join(timeout=1)
            # A callback may have interrupted consumption halfway through a
            # chunk. Emit its complete lines before processing the queued EOF.
            for channel in buffers:
                while b"\n" in buffers[channel]:
                    raw, buffers[channel] = buffers[channel].split(b"\n", 1)
                    emit(channel, raw, True)
            while not pending.empty():
                consume(*pending.get_nowait(), best_effort=True)
            for channel, raw in buffers.items():
                if raw:
                    emit(channel, raw, True)
            if cleanup_error is not None:
                exc.process_outcome = ProcessOutcome(
                    *(b"".join(received[channel]).decode("utf-8", "replace") for channel in received),
                    proc.returncode if proc.returncode is not None else -1, reason)
                exc.leader_returncode = proc.returncode
            raise
        finally:
            stop()
            for worker in started:
                worker.join(timeout=1)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    if stream is proc.stdin and writes_input and cleanup_error is not None and workers[-1].is_alive():
                        continue  # a live producer can still hold the writer's lock
                    stream.close()
        outcome = ProcessOutcome(
            *(b"".join(received[channel]).decode("utf-8", "replace") for channel in received),
            proc.returncode if proc.returncode is not None else -1, reason)
        if cleanup_error is not None:
            error = ProcessCleanupError(outcome)
            error.process_cleanup_error = cleanup_error
            error.leader_returncode = proc.returncode
            raise error from cleanup_error
        return outcome
