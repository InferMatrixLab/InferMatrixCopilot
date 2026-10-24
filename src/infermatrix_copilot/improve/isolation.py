"""OS isolation for generated code; the parent is the only model/credential broker."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import shutil
import sys
import time
import tempfile
import threading

from ..process_execution import ProcessInputChannel, run_process

class SandboxUnavailable(RuntimeError):
    pass

def probe() -> dict:
    binary = shutil.which("bwrap")
    if not binary:
        return {"ready": False, "reason": "bubblewrap-not-installed"}
    limit = Path("/proc/sys/user/max_user_namespaces")
    if limit.exists() and limit.read_text().strip() == "0":
        return {"ready": False, "reason": "user-namespaces-disabled (max_user_namespaces=0)"}
    command = [binary, "--unshare-all", "--die-with-parent"]
    for path in ("/usr", "/lib", "/lib64"):
        if Path(path).exists(): command += ["--ro-bind", path, path]
    r = run_process([*command, "/usr/bin/true"], cwd=tempfile.gettempdir(), env={},
                    deadline=time.monotonic() + 10)
    return {"ready": r.returncode == 0 and r.reason is None,
            "reason": "namespace probe timed out" if r.reason else r.stderr.strip() if r.returncode else ""}

def argv(source: Path, payload: Path, worker: Path, tests: Path | None = None) -> list[str]:
    command = [shutil.which("bwrap") or "bwrap", "--unshare-all", "--die-with-parent", "--new-session",
               "--clearenv", "--setenv", "PATH", "/usr/bin", "--setenv", "HOME", "/tmp",
               "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for path in ("/usr", "/lib", "/lib64", "/bin"):
        if Path(path).exists():
            command += ["--ro-bind", path, path]
    # Python and dependencies only; never bind HOME, workspace, credentials or the ledger.
    for path in {sys.prefix, sys.base_prefix, str(Path(sys.executable).resolve().parent.parent)}:
        if path != "/" and not path.startswith("/usr") and Path(path).is_dir():
            command += ["--ro-bind", path, path]
    command += ["--ro-bind", str(source), "/candidate", "--ro-bind", str(payload), "/input.json",
                "--ro-bind", str(worker), "/controller.py"]
    if tests is not None:
        command += ["--ro-bind", str(tests), "/tests"]
        for prefix in ("src", "playbooks", "adapters", "skills"):
            if (source / prefix).is_dir():
                command += ["--ro-bind", str(source / prefix), f"/{prefix}"]
    command += ["--chdir", "/tmp", sys.executable, "-I", "/controller.py"]
    return command

class Sandbox:
    def check(self) -> dict:
        return probe()

    def run(self, source: Path, payload: dict, work: Path, *, llm=None, settings=None,
            governor=None, tests: Path | None = None, timeout: int = 900) -> dict:
        from .artifacts import atomic_json, verify, dependency_hash
        from .budget import governed
        from ..trace_store import trace_context
        status = self.check()
        if not status["ready"]:
            raise SandboxUnavailable(status["reason"])
        artifact = verify(source)
        if artifact.get("dependency_sha") != dependency_hash():
            raise SandboxUnavailable("dependency lock changed since artifact creation")
        work.mkdir(parents=True, exist_ok=True)
        atomic_json(work / "input.json", {**payload, "source_sha": artifact["tree_sha"]})
        worker = Path(__file__).with_name("worker.py")
        channel = ProcessInputChannel()
        completed = threading.Event()
        calls, size, result, pending, failed = 0, 0, None, b"", False

        def parse_request(stream, chunk):
            nonlocal calls, size, result, pending
            if stream != "stdout":
                return
            size += len(chunk)
            if size > 24_000_000:
                raise SandboxUnavailable("worker output limit exceeded")
            pending += chunk
            if b"\n" not in pending:
                return
            line, pending = pending.split(b"\n", 1)
            # Preserve the synchronous broker's framing contract, including
            # rejection of another partial message before a response is sent.
            if pending or completed.is_set():
                raise SandboxUnavailable("worker sent overlapping broker messages")
            event = json.loads(line)
            if event.get("type") == "result":
                result = event["result"]
                completed.set()
                channel.close()
                return
            if event.get("type") != "model_call" or calls >= 128 or llm is None or governor is None:
                raise SandboxUnavailable("worker sent an unauthorized broker request")
            if not channel.writable:
                raise SandboxUnavailable("worker stopped before broker admission")
            calls += 1
            request = event["request"]
            allowed = {settings.tier_target(m).model for m in ("eco", "performance")}
            if request.get("model") not in allowed or not (0 < int(request.get("max_tokens", 0)) <= settings.llm_max_tokens):
                raise SandboxUnavailable("worker requested an undeclared model or token limit")
            if set(request) - {"system", "messages", "tools", "model", "max_tokens", "role"}:
                raise SandboxUnavailable("invalid model request fields")
            target = next(settings.tier_target(m) for m in ("eco", "performance") if settings.tier_target(m).model == request["model"])
            client = llm.for_target(target) if hasattr(llm, "for_target") else llm
            with governed(governor), trace_context(**payload.get("trace_context", {})):
                reply = client.create(**request)
            try:
                channel.send(json.dumps(dataclasses.asdict(reply)) + "\n")
            except BrokenPipeError as exc:
                raise SandboxUnavailable("worker stopped before broker response delivery") from exc

        def receive(stream, chunk):
            nonlocal failed
            if stream != "stdout":
                return
            if failed:
                raise SandboxUnavailable("worker broker has already failed")
            try:
                parse_request(stream, chunk)
            except BaseException:
                # The shared supervisor may drain already-read diagnostics
                # after an observer fails. Protocol rejection is terminal: a
                # drained request must never authorize another model call.
                failed = True
                channel.close()
                raise

        outcome = run_process(argv(source.resolve(), (work / "input.json").resolve(), worker, tests),
            cwd=work, env={}, deadline=time.monotonic() + timeout, input_channel=channel,
            on_chunk=receive, completed=completed, completion_grace_s=5)
        stderr_tail = "".join(outcome.stderr.splitlines(keepends=True)[-100:])
        if outcome.reason:
            raise SandboxUnavailable("candidate timed out")
        if outcome.returncode or result is None or pending:
            raise SandboxUnavailable(f"worker failed rc={outcome.returncode}: {stderr_tail[-1000:]}")
        if result.get("source_sha") != artifact["tree_sha"] or not result.get("package_path", "").startswith("/candidate/src/"):
            raise SandboxUnavailable("worker source fingerprint/import mismatch")
        verify(source)
        # Counted by the parent; a worker cannot forge resource accounting.
        result["_broker_calls"] = calls
        if payload.get("mode") == "tests":
            result["report"] = stderr_tail[-12000:]
        return result
