"""Cursor harness transport — Strict on a Cursor subscription (M1).

Invocation shape proven by the Composer eval arm
(`eval/dataset/run_cursor_arm.py`): headless `cursor-agent --print --force
--output-format stream-json`, prompt on STDIN (argv has a 128KiB per-arg
limit on Linux and evidence packs exceed it), events parsed line-wise.

Governance (decision record in doc/features/provider-registry.md): cursor-agent
cannot fully disable its built-in tools, so the copilot's scoped tools are
OFFERED via the MCP tool bridge (preventive where used) and every session is
post-audited (`audit.py`) with the verdict traced — the detective fallback,
disclosed, never silent. The subprocess env is an allowlist: the vendor CLI
must keep its own subscription auth (HOME state) but must never inherit our
model-endpoint variables (this machine's ANTHROPIC_BASE_URL points at a
DeepSeek gateway) or repo credentials.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from ..agent_loop import AgentOutcome
from ..llm import Reply
from .base import (
    AgentSessionRequest,
    HarnessTransport,
    SessionUsage,
    SessionEvent,
    bridge_server,
    flatten_messages,
    json_events,
    run_cli,
    sanitized_env,
)
from .registry import PROVIDERS


def _session_lock_path(cwd: Path) -> Path | None:
    """Where to serialize cursor sessions in `cwd`, or None when no other
    run can be in it.

    The bridge config lives at ONE fixed path per working directory
    (`.cursor/mcp.json`), so two sessions of different runs in the same cwd
    overwrite each other's config and one agent binds to the other run's tool
    scope and trace. A managed PR-time worktree is keyed by repo+PR+sha and the
    run service never runs two runs on one PR at once, and a run directory is
    one run's own, so neither needs anything here. A checkout does: issue
    tasks and PR runs whose worktree could not be materialized all work in the
    live checkout. The lock lives in that checkout's git dir, beside git's own
    locks, never in the working tree."""
    from ..engine import worktrees

    if worktrees.is_managed_dest(cwd):
        return None
    dot_git = cwd / ".git"
    if dot_git.is_dir():
        return dot_git / "imx-cursor-session.lock"
    if dot_git.is_file():  # a linked worktree: `gitdir: <its admin dir>`
        try:
            line = dot_git.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            admin = Path(line.split(":", 1)[1].strip())
            admin = admin if admin.is_absolute() else cwd / admin
            if admin.is_dir():
                return admin / "imx-cursor-session.lock"
    return None


@contextmanager
def _exclusive_cwd(cwd: Path, timeout_s: float):
    """Hold `cwd` exclusively for one session when other runs may share it
    (see `_session_lock_path`). An `flock` serializes across threads,
    processes and services. Yields False when the hold timed out."""
    from ..engine.lifecycle import fcntl

    lock = _session_lock_path(cwd)
    if lock is None or fcntl is None:
        yield True
        return
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.2)
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def _no_guard():
    yield True


class CursorTransport(HarnessTransport):
    """cursor-agent CLI as a Strict backend."""

    def readonly_usage(self, events: list[dict]) -> dict | None:
        return dict(self._usage(events).reported_usage) or None

    spec = PROVIDERS["cursor"]
    supports_native_events = True
    supports_session_resume = True

    @staticmethod
    def readonly_json_command(command, *, model, output_format="stream-json", session_id=None, **_):
        return [*command, "-p", *(["--resume", session_id] if session_id else []),
                "--mode=ask", "--output-format", output_format,
                "--model", model, "--trust"]

    @staticmethod
    def decode_event(event: dict) -> SessionEvent:
        kind = event.get('type', 'result')
        nested = event.get("session")
        session = next((str(value).strip() for value in (
            event.get("session_id"), event.get("sessionId"), event.get("conversation_id"),
            event.get("conversationId"), nested.get("id") if isinstance(nested, dict) else None)
            if str(value or "").strip()), None)
        error = ((event.get("message") or event.get("error") or "transport reports failure")
                 if kind in {"error", "turn.failed"} else None)
        if event.get("is_error") or (kind == "result" and event.get("subtype") not in (None, "success")):
            error = error or "transport result reports failure"
        if kind == 'result' and (event.get('is_error') is not None and type(event['is_error']) is not bool
                                or not isinstance(event.get('result'), str)):
            error = error or 'transport result is malformed'
        return SessionEvent(
            text=event['result'].strip() if kind == 'result' and isinstance(event.get('result'), str) else None,
            session_id=session, served_model=event['model'].strip() if isinstance(event.get('model'), str) and event['model'].strip() else None,
            usage=event.get("usage") if isinstance(event.get("usage"), dict) else None,
            error=str(error.get("message") or "transport reports failure") if isinstance(error, dict) else str(error) if error else None)

    @staticmethod
    def readonly_events(stdout: str, *, validate: bool = True) -> list[dict]:
        envelope = json.loads(stdout)
        if not isinstance(envelope, dict):
            raise ValueError("transport envelope is not an object")
        if validate:
            error = CursorTransport.decode_event(envelope).error
            if error:
                raise ValueError(error)
            if envelope.get('type', 'result') != 'result':
                raise ValueError("transport envelope reports failure or malformed result")
        return [envelope]

    def auth_gap(self) -> str | None:
        cli = self.cli_path()
        if not cli:
            return None  # the CLI-missing gap is reported separately
        try:
            out = self.auth_probe([cli])
        except (OSError, subprocess.SubprocessError):
            return None
        return self.auth_status_issue(out.returncode, f"{out.stdout}\n{out.stderr}")

    @staticmethod
    def auth_status_issue(returncode: int, text: str) -> str | None:
        if returncode or "Logged in" not in text or "Not logged in" in text:
            return "cursor-agent is not logged in — run: cursor-agent login"
        return None

    # -- process plumbing ----------------------------------------------------
    def _run(self, text: str, *, cwd: str, timeout_s: float,
             model: str = "", tool_less: bool = False, native_event_sink=None) -> tuple[list[dict], bool]:
        """One CLI invocation → (parsed events, timed_out). A timeout kills
        the process but keeps the partial stream — a half-done investigation
        is salvage material, not garbage."""
        # --approve-mcps is load-bearing: headless runs do not auto-approve
        # configured MCP servers, and without it the tool bridge is silently
        # ignored (found in the live smoke — session ran on native tools only)
        cmd = [self.require_cli(), "--print", "--output-format", "stream-json"]
        cmd += ["--mode", "ask", "--trust", "--workspace", cwd] if tool_less else ["--force", "--approve-mcps"]
        selected = model or self.settings.strict_backend_model
        if selected:
            cmd += ["--model", selected]
        stdout, _, returncode, timed_out = run_cli(cmd, input=text, cwd=cwd,
                                          env=sanitized_env(), timeout_s=timeout_s,
                                          runner=getattr(self, "runner", None))
        events = json_events(stdout)
        if native_event_sink is not None:
            for event in events:
                native_event_sink({"type": "native.cursor.event", "payload": event})
        # All callers interpret terminal envelopes through the same decoder.
        # Emit captured events first so failed replies retain their known usage.
        terminals = [event for event in events if event.get('type', 'result') in {'result', 'error', 'turn.failed'}]
        if any(self.decode_event(event).error for event in terminals):
            raise RuntimeError('cursor command errored or returned a malformed result')
        if not timed_out and (returncode != 0 or not any(event.get('type', 'result') == 'result' for event in terminals)):
            raise RuntimeError('cursor command exited without a successful terminal result')
        return events, timed_out

    @staticmethod
    def _final_text(events: list[dict]) -> str:
        for event in reversed(events):
            decoded = CursorTransport.decode_event(event)
            if decoded.text is not None:
                return decoded.text
        return ""

    @staticmethod
    def _usage(events: list[dict]) -> SessionUsage:
        """Best-effort: cursor-agent's usage/model reporting varies by
        version; absent billing counters remain unknown, not zero."""
        usage = SessionUsage()
        for event in events:
            decoded = CursorTransport.decode_event(event)
            if decoded.served_model:
                usage.served_model = decoded.served_model
            raw = decoded.usage
            if isinstance(raw, dict) or event.get("type") == "result":
                # live cursor-agent emits camelCase (inputTokens); accept
                # snake_case too so a future rename does not zero the counts
                raw = raw or {}
                usage.add_token_usage({
                    "input_tokens": raw.get("inputTokens", raw.get("input_tokens")),
                    "output_tokens": raw.get("outputTokens", raw.get("output_tokens")),
                    **{key: raw[key] for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
                       if key in raw},
                })
        if usage.reported_usage is None:
            usage.reported_usage = {}
        return usage

    @classmethod
    def native_snapshot(cls, events: list[dict]) -> dict:
        native = [event["payload"] for event in events
                  if event.get("type") == "native.cursor.event"
                  and isinstance(event.get("payload"), dict)]
        usage = cls._usage(native)
        return {"usage": dict(usage.reported_usage or {}),
                "served_model": usage.served_model, "text": cls._final_text(native)}

    # -- MCP bridge wiring ---------------------------------------------------
    @staticmethod
    def _is_our_stale_config(config: Path) -> bool:
        """True when an existing mcp.json is one WE wrote (it launches our
        tool bridge) and is therefore safe to replace."""
        try:
            existing = json.loads(config.read_text(encoding="utf-8"))
            entry = (existing.get("mcpServers") or {}).get(
                "infermatrix-tools") or {}
        except (OSError, ValueError, AttributeError):
            return False
        return "infermatrix_copilot.tool_bridge" in " ".join(
            str(a) for a in (entry.get("args") or ()))

    def _write_mcp_config(self, cwd: Path, spec_path: Path) -> list[Path]:
        """Project-scope `.cursor/mcp.json` in the session cwd pointing at the
        tool bridge. Returns the paths WE created (and only those) so the
        session can restore the tree afterwards — the cwd is our detached
        PR-time worktree and must not accumulate config litter."""
        created: list[Path] = []
        cursor_dir = cwd / ".cursor"
        if not cursor_dir.exists():
            cursor_dir.mkdir()
            created.append(cursor_dir)
        config = cursor_dir / "mcp.json"
        if config.exists() and not self._is_our_stale_config(config):
            # Never clobber a REPO-COMMITTED config. A config WE wrote and
            # failed to clean up (a killed session skips the `finally`) is a
            # different case and must be replaced: leaving it silently binds
            # this session to a DEAD run's spec -- wrong ToolScope, wrong
            # plan-gate prefix, and tool events appended to the old run's
            # bridge trace. Observed 2026-09-18, 27 minutes of a module run.
            return created
        config.write_text(json.dumps({"mcpServers": {"infermatrix-tools": bridge_server(spec_path)}},
                                     indent=2), encoding="utf-8")
        created.insert(0, config)
        return created

    # -- transport contract --------------------------------------------------
    def run_session(self, req: AgentSessionRequest) -> AgentOutcome:
        self.validate_session(req)
        from .audit import audit_events

        cwd = Path(req.scope.root or req.run_dir)
        created: list[Path] = []
        # Waiting may outlast one session in the cwd, never an unbounded queue.
        wait_s = max(60.0, float(req.timeout_s) + 60.0)
        with (_exclusive_cwd(cwd, wait_s) if req.bridge_spec_path is not None
              else _no_guard()) as held:
            if not held:
                reason = (f"working directory {cwd} stayed busy with another "
                          f"run's cursor session for {wait_s:.0f}s")
                if req.trace is not None:
                    req.trace.record("capability_gap",
                                     capability="cursor.exclusive_cwd",
                                     step=req.step_name, detail=reason)
                return AgentOutcome(text="", iterations=0, tool_calls=0,
                                    truncated=True, refusals=[reason])
            if req.bridge_spec_path is not None:
                created = self._write_mcp_config(cwd, req.bridge_spec_path)
            try:
                events, timed_out = self._run(
                    f"{req.system}\n\n{req.prompt}", cwd=str(cwd),
                    timeout_s=req.timeout_s, model=req.model,
                    **({"native_event_sink": req.native_event_sink} if req.native_event_sink is not None else {}))
            finally:
                for path in created:
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
        audit = audit_events(events, roots=(str(cwd), str(req.run_dir)),
                             read_only=req.scope.read_only, cwd=str(cwd))
        usage = self._usage(events)
        if req.trace is not None:
            req.trace.record(
                "harness_session", provider=self.spec.id, step=req.step_name,
                audit_ok=audit.ok, audit_violations=audit.violations[:10],
                shell_commands=audit.shell_commands,
                file_reads=audit.file_reads, writes=audit.writes,
                other_tool_calls=audit.other_tool_calls, timed_out=timed_out,
                served_model=usage.served_model)
        # Cursor does not expose its round count.
        return usage.outcome(self._final_text(events), tool_calls=audit.tool_calls,
                             tools_used=audit.tools_used, truncated=timed_out,
                             refusals=[f"audit: {v}" for v in audit.violations])

    def complete(self, *, system: str, messages: list[dict],
                 model: str = "", max_tokens: int | None = None,
                 role: str = "", effort: str = "",
                 max_budget_usd: float | None = None, native_event_sink=None) -> Reply:
        """Tool-less one-shot. Runs in an EMPTY scratch cwd so cursor-agent's
        native tools have nothing to read — the containment for calls that
        need no repo at all (intent, reducer, repair)."""
        with tempfile.TemporaryDirectory(prefix="imc-cursor-oneshot-", ignore_cleanup_errors=True) as scratch:
            events, timed_out = self._run(
                flatten_messages(system, messages), cwd=scratch,
                timeout_s=self.settings.strict_backend_timeout_s, model=model, tool_less=True,
                **({"native_event_sink": native_event_sink} if native_event_sink is not None else {}))
        from .audit import assert_tool_less

        assert_tool_less(events)
        usage = self._usage(events)
        return usage.reply(self._final_text(events),
                           stop_reason="max_tokens" if timed_out else "end_turn")
