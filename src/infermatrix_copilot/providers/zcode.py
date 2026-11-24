"""ZCode CLI harness transport — Strict on a Z.AI (GLM) subscription.

Headless ``zcode --prompt=... --output-format stream-json --mode plan``
emits JSONL events; the ``result`` event carries the final ``response`` and
token usage, ``session.updated`` events name the served ``providerId`` /
``modelId``, and ``tool.updated`` events with ``kind: scheduled`` are the
tool calls. Probed live on zcode 0.16.9 (2026-09-30, bigmodel coding plan).

Governance (disclosed, per doc/features/provider-registry.md):

- **Built-in tools are removed, not trusted.** ``--mode plan`` alone still
  leaves a node REPL MCP, subagents (``Agent``, which can reach a shell),
  workflows and web fetch/search. ``--disallowed-tools`` strips everything
  outside the read-only set (measured: 28 tools → 11, all reads), and every
  session is post-audited against that allowlist — a tool a future zcode
  version adds and our denylist misses shows up as an ``audit:`` refusal,
  never silently. The native reads that remain (``Read``/``Grep``/``Glob``,
  needed to read an attached oversized prompt) are audited for containment
  like cursor's (`audit.contained_in`): a path outside the checkout, the run
  dir and the session dir is a violation. Detective, not preventive — the
  bridge's own reads are the preventive path.
- **The session cwd is a scratch dir WE own, never the PR worktree.** zcode
  loads ``.env`` from its cwd and discovers ``zcode.json`` /
  ``.zcode/config.json`` from the cwd up to the git root — project config
  can declare stdio MCP servers (i.e. commands). Running inside a PR
  checkout would let the PR under review configure the reviewer. The
  worktree is read by absolute path instead; the prompt names it.
- **The MCP tool bridge rides that scratch dir's ``.zcode/config.json``
  (measured live: ``mcp__infermatrix-tools__read_file`` called, bridge
  trace recorded), so scoped reads still pass ``tools.dispatch``.
  zcode 0.17 added a workspace trust gate for project-scope servers that
  headless prompts cannot grant; the provider pre-seeds the host trust
  store's matching record (``_seed_workspace_mcp_trust``) so the bridge
  still connects — verified live on 0.17.1 (bridge spawned, tool call
  round-trip). A future digest change fails CLOSED: the bridge stays
  unconnected and the engine's lifecycle check blocks the module.
- **A read-only scope runs ``--mode guarded``, not ``plan`` (zcode 0.18).**
  Plan mode asks for every MCP tool there, so a headless prompt gets the
  bridge denied ("Plan mode requires approval for MCP tools that are not
  read-only"; probed live on 0.18.0, 2026-11-24). After the denylist both
  modes leave the same native tools, all reads; ``guarded`` keeps the ask for
  dangerous commands. The host's plugin servers (``mcp__plugin_*``) are listed
  in every mode and stay outside the audit allowlist.

Model selection: zcode has no ``--model`` flag; a session takes the
*configured default* of its personal provider config, and only when that
entry is *selectable* — the CLI (0.16.9, ``resolveInitialModelSelection``)
requires ``options.reasoningLevel`` on the entry and otherwise falls back to
the first catalog model without a word (which is why an entry written by
hand without a level "did nothing"). The transport therefore pins the model
PER RUN: it writes a minimal personal provider config into the session
scratch dir — only ``defaultModelSelection`` (the host's provider id, the
requested model in the catalog's casing, ``zcode_reasoning_level``) on empty
rule sets, never the host's provider rules, which may carry API keys and
would otherwise sit inside a read root of the session — and points the
subprocess at it through ``ZCODE_PERSONAL_PROVIDER_CONFIG_FILE``; the host's
file is never touched.
``STRICT_BACKEND_MODEL`` / the request's model is still ASSERTED against the
served ``modelId`` under ``MODEL_MISMATCH_POLICY`` — a mislabeled arm is the
failure this campaign has already paid for three times.

Env is the shared allowlist (`base.sanitized_env`); zcode keeps its Z.AI
OAuth login under HOME (``~/.zcode/v2``)."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import signal
import time
import shutil
import subprocess
import sys
import tempfile
import threading
from functools import cached_property
from pathlib import Path

from ..agent_loop import AgentOutcome
from ..llm import Block, ModelMismatchError, Reply
from .audit import contained_in
from .base import (
    AgentSessionRequest,
    HarnessTransport,
    SessionUsage,
    flatten_messages,
    sanitized_env,
)
from .registry import PROVIDERS

logger = logging.getLogger(__name__)

_BRIDGE_SERVER = "infermatrix-tools"
_BRIDGE_PREFIX = f"mcp__{_BRIDGE_SERVER}__"

# zcode 0.17 gates project-scope MCP servers behind a workspace trust review
# (createWorkspaceMcpTrust) that headless `--prompt` sessions cannot grant —
# the server is silently left unconnected ("bridge never started"). The
# trust record lives in `<home>/.zcode/security/workspace-mcp-trust-v1.json`
# and matches on (workspaceIdentity, serverName, declarationDigest); we
# pre-seed it after writing the session's project config. User-scope
# declaration (kind "user" skips the gate) would need HOME redirection,
# which measurably breaks the CLI's model-selection bootstrap (0.17.1) and
# runs retention cleanup against symlinked host dirs — rejected.
_TRUST_STORE_REL = (".zcode", "security", "workspace-mcp-trust-v1.json")

# Everything zcode exposes that is not a pure read: 0.16.9's set, plus the
# session management zcode 0.18 added, which reaches the host's other sessions.
_DISALLOWED = (
    "Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Agent", "Skill",
    "ExitPlanMode", "EnterPlanMode", "AskUserQuestion", "SendMessage",
    "WebFetch", "WebSearch", "CreateWorkflow", "AmendWorkflow",
    "SaveWorkflow", "EvalWorkflowSnippet", "ResumeWorkflowRun",
    "ResolveWorkflowQuestion", "TaskStop", "mcp__node_repl__js",
    "mcp__web_reader__webReader", "mcp__4_5v_mcp__analyze_image",
    "CreateSession", "ForkSession", "InterruptSession", "ListSessions",
    "ReadSession", "RenameSession", "SendToSession", "WaitSessions",
)
# What remains after the denylist — the audit's allowlist.
_READ_TOOLS = frozenset({
    "Read", "Grep", "Glob", "TodoRead", "TodoWrite", "TaskOutput",
    "ReadSessionContext", "ListWorkflowRuns", "GetWorkflowRun",
    "ListSavedWorkflows", "ListModels",
})

# Env the zcode subprocess keeps on top of `sanitized_env()`: the data dir
# `auth_gap` checks must be the one the run logs in with.
_ZCODE_ENV_KEEP = ("ZCODE_DATA_BASE_DIR",)
# The personal provider config the CLI reads its configured default model
# from (see the module docstring): written per run into the session dir.
_PERSONAL_CONFIG_ENV = "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"
_PERSONAL_CONFIG_NAME = "provider_config.json"
# The Z.AI coding-plan provider the OAuth login provisions; the host's own
# personal config names it when one exists, settings can override it.
_DEFAULT_PROVIDER_ID = "account:bigmodel-individual-coding-plan"
_REASONING_LEVELS = ("low", "high", "max")
# Model ids as zcode 0.16.9 spells them (its own catalog list): the CLI
# selects a configured model by case-sensitive equality.
_KNOWN_MODEL_IDS = (
    "GLM-5.3", "GLM-5.3-Flash", "GLM-5V-Turbo", "GLM-5.2", "GLM-5.1", "GLM-5.1-Highspeed",
    "GLM-5", "GLM-5-Turbo", "GLM-4.7", "GLM-4.7-FlashX", "GLM-4.7-Flash", "GLM-4.6",
    "GLM-4.5-Air", "GLM-4.5", "GLM-4.6V", "GLM-4.6V-Flash",
)
_KNOWN_ID = re.compile(r'"(GLM-[A-Za-z0-9.\-]+)"')

# Input keys under which zcode's native tools name a filesystem path.
_PATH_KEYS = ("file_path", "path", "notebook_path")

# The prompt travels as ONE argv string (zcode has no stdin channel) and
# Linux caps a single argument at 128KiB. Past this budget the prompt is
# written to a file in the scratch dir and attached instead.
_ARG_BUDGET = 100_000


class ZCodeFailure(RuntimeError):
    """Known provider failure with a fixed public message and recovery code.

    Provider messages, request headers, prompts and raw stream events never
    become fields on this exception. Unknown CLI failures retain the existing
    diagnostic path instead of being guessed from arbitrary text.
    """
    MESSAGES = {
        "model_quota_exceeded": "The Zcode model usage quota is exhausted. Wait for the provider quota to reset or ask the service administrator to check the coding plan.",
        "model_rate_limited": "The Zcode model is rate limited. Try again after a short wait.",
        "model_authentication_required": "The configured Zcode model requires reauthorization or model access. Contact the service administrator.",
        "model_unavailable": "The Zcode model is temporarily unavailable. Try again later.",
    }

    def __init__(self, code: str, *, retryable: bool):
        super().__init__(self.MESSAGES[code])
        self.code = code
        self.retryable = retryable


def _known_model_failure(events: list[dict]) -> ZCodeFailure | None:
    for event in reversed(events):
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        code = payload.get("errorCode")
        provider_code = str(payload.get("providerErrorCode", ""))
        status = payload.get("statusCode")
        if provider_code == "1310" or (code == "model_rate_limited" and payload.get("retryable") is False):
            return ZCodeFailure("model_quota_exceeded", retryable=False)
        if code == "model_rate_limited":
            return ZCodeFailure("model_rate_limited", retryable=True)
        if code in {"model_authentication_failed", "model_auth_failed", "model_unauthorized", "model_access_denied"} or (isinstance(code, str) and code.startswith("model_") and status in (401, 403)):
            return ZCodeFailure("model_authentication_required", retryable=False)
        if code in {"model_unavailable", "model_connection_error", "model_timeout", "model_server_error"} or (isinstance(code, str) and code.startswith("model_") and isinstance(status, int) and status >= 500):
            return ZCodeFailure("model_unavailable", retryable=True)
    return None


class ZCodeTransport(HarnessTransport):
    """zcode CLI (headless prompt mode) as a Strict backend."""

    supports_native_events = True
    spec = PROVIDERS["zcode"]

    @cached_property
    def _selected_provider_id(self) -> str:
        return (str(getattr(self.settings, "zcode_provider_id", "") or "")
                or self._host_provider_id() or _DEFAULT_PROVIDER_ID)

    @property
    def subscription_billing(self) -> bool:
        """Known OAuth coding plan only; custom API providers still need caps.

        Billing mode is not a measured zero-dollar call cost.
        """
        return self._selected_provider_id == _DEFAULT_PROVIDER_ID

    def auth_gap(self) -> str | None:
        """zcode has no login-status command; the OAuth login writes
        ``~/.zcode/v2/credentials.json``, so its absence is the cheap signal.
        Presence does not prove the token is still valid — an expired login
        surfaces loudly on the first run."""
        if not self.cli_path():
            return None  # the CLI-missing gap is reported separately
        base = os.environ.get("ZCODE_DATA_BASE_DIR") or str(Path.home())
        if not (Path(base) / ".zcode" / "v2" / "credentials.json").is_file():
            return "zcode is not logged in — run: zcode login"
        return None

    # -- process plumbing ----------------------------------------------------
    @staticmethod
    def _zcode_canon(value) -> str:
        """zcode's hash2 pre-image: JSON.stringify with a replacer that
        sorts the keys of every object (arrays untouched), compact
        separators, raw non-ASCII — json.dumps(sort_keys=True) matches it
        exactly (golden-verified against a live 0.17.1 session)."""
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)

    @classmethod
    def _trust_declaration_digest(cls, session: Path, server: str,
                                  server_config: dict) -> str:
        """The declarationDigest zcode 0.17 computes over a project-scope
        server: sha256 over the canonical form of
        ``{version, workspaceIdentity, workingDirectory, serverName,
        sourcePaths, config}`` where config is the server entry's execution
        fields (enabled/source stripped, env defaulted to {}, cwd defaulted
        to the working directory) and workspaceIdentity defaults to the
        working directory. sourcePaths is the project config file list —
        here exactly the one file we write."""
        execution = {k: v for k, v in server_config.items()
                     if k not in ("enabled", "source")}
        execution["env"] = server_config.get("env") or {}
        execution["cwd"] = server_config.get("cwd") or str(session)
        value = {
            "version": 1,
            "workspaceIdentity": str(session),
            "workingDirectory": str(session),
            "serverName": server,
            "sourcePaths": [str(session / ".zcode" / "config.json")],
            "config": execution,
        }
        canonical = cls._zcode_canon(value)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _seed_workspace_mcp_trust(cls, session: Path, server: str,
                                  server_config: dict) -> None:
        """Append the session's trust record (idempotent, flock-serialized)
        to the host's workspace MCP trust store. Best effort: on failure the
        bridge stays untrusted and the engine's bridge-lifecycle check
        surfaces it — never a silent pass."""
        home = Path(sanitized_env().get("HOME") or Path.home())
        store = home.joinpath(*_TRUST_STORE_REL)
        record = {
            "workspaceIdentity": str(session),
            "serverName": server,
            "declarationDigest": cls._trust_declaration_digest(
                session, server, server_config),
            "grantedAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
                          + f".{int(time.time() * 1000) % 1000:03d}Z",
        }
        store.parent.mkdir(parents=True, exist_ok=True)
        with open(store, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                try:
                    data = json.loads(fh.read() or "{}")
                except ValueError:
                    data = {}
                records = data.get("records") if isinstance(data, dict) else None
                if not isinstance(records, list):
                    records = []
                key = (record["serverName"], record["declarationDigest"])
                if not any((r.get("serverName"), r.get("declarationDigest"))
                           == key for r in records):
                    records.append(record)
                # prune OUR records whose session dir is gone — the store is
                # host-global and every dispatch session adds one record
                records = [r for r in records
                           if r.get("serverName") != _BRIDGE_SERVER
                           or Path(str(r.get("workspaceIdentity", ""))).is_dir()]
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps({"schemaVersion": 1, "records": records},
                                    indent=2))
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    @classmethod
    def _write_mcp_config(cls, session: Path, spec_path: Path, *,
                          timeout_s: float = 30.0) -> None:
        package_root = Path(__file__).resolve().parents[2]
        # The harness stays credential-free; its scoped bridge also needs
        # the lease's GPU visibility and the target environment for tests.
        bridge_env = {k: os.environ[k] for k in (
            "CUDA_VISIBLE_DEVICES", "LD_LIBRARY_PATH", "HF_HOME",
            "OMP_NUM_THREADS", "MKL_NUM_THREADS", "ZCODE_DATA_BASE_DIR",
            "STRICT_BACKEND", "STRICT_BACKEND_MODEL", "STRICT_BACKEND_CLI",
            "ZCODE_PROVIDER_ID", "ZCODE_REASONING_LEVEL",
            "REBASE_REVIEWER_MODEL", "NOTIFY_EMAIL") if k in os.environ}
        bridge_env["PYTHONPATH"] = str(package_root)
        server = {
            "type": "stdio",
            # ZCode otherwise abandons tools after 30s, even while a valid
            # test or nested plan review is still running in the bridge.
            # The session deadline remains the outer bound for execution.
            "timeoutMs": max(1, int(timeout_s * 1000)),
            "command": sys.executable,
            "args": ["-m", "infermatrix_copilot.tool_bridge",
                     "--spec", str(spec_path)],
            "env": bridge_env,
        }
        config = session / ".zcode" / "config.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"features": {"memory": False}, "memory": {"use": False},
                                     "mcp": {"servers": {_BRIDGE_SERVER: server}}},
                                     indent=2), encoding="utf-8")
        try:
            cls._seed_workspace_mcp_trust(session, _BRIDGE_SERVER, server)
        except OSError as exc:
            logger.warning("zcode workspace MCP trust seed failed "
                           "(bridge will stay untrusted): %s", exc)

    @staticmethod
    def _write_oneshot_config(session: Path) -> None:
        """Disable implicit memory, plugins and MCP in structured calls.

        Oversized prompts need native Read for their attachment. That must
        never also load personal memory outside the declared evidence roots.
        """
        config = session / ".zcode" / "config.json"
        config.parent.mkdir()
        storage = session / "storage"
        config.write_text(json.dumps({
            "features": {"memory": False, "skill": False, "subagent": False, "mcp": False},
            "memory": {"use": False}, "plugins": {"enabled": False},
            "storage": {"dir": str(storage), "sessionDbPath": str(storage / "db.sqlite")},
        }), encoding="utf-8")

    @staticmethod
    def _zcode_home() -> Path:
        return Path(os.environ.get("ZCODE_DATA_BASE_DIR") or str(Path.home())) / ".zcode" / "v2"

    def _host_provider_id(self) -> str:
        """The provider the host's own configured default names — the one id
        read from that file; its provider rules (which may carry API keys for
        key-based providers) are never read into anything the session can see."""
        host = self._zcode_home() / _PERSONAL_CONFIG_NAME
        try:
            data = json.loads(host.read_text(encoding="utf-8")) if host.is_file() else {}
        except (OSError, ValueError):
            return ""
        current = (data.get("config") or {}).get("defaultModelSelection") if isinstance(data, dict) else None
        return str(current.get("providerId") or "") if isinstance(current, dict) else ""

    def _catalog_model_ids(self) -> list[str]:
        """Model ids zcode knows, for canonical casing: the bundled catalog
        under the zcode home when it is readable, plus the 0.16.9 list."""
        ids: list[str] = []
        root = self._zcode_home() / "runtime" / "provider"
        try:
            for path in sorted(root.rglob("zcode-builtin.json")):
                ids.extend(_KNOWN_ID.findall(path.read_text(encoding="utf-8")))
        except OSError:
            pass
        return [*ids, *_KNOWN_MODEL_IDS]

    def canonical_model_id(self, model: str) -> str:
        """zcode matches a configured model id case-SENSITIVELY, while our
        model assertion is case-insensitive: a request such as
        ``glm-5.3-flash`` must be written as the catalog spells it or the CLI
        selects nothing and falls back. Unknown ids pass through unchanged
        (the served-model assertion then reports the mismatch)."""
        wanted = model.strip().casefold()
        for known in self._catalog_model_ids():
            if known.casefold() == wanted:
                return known
        return model.strip()

    def _write_model_config(self, session: Path, model: str) -> Path:
        """The session's personal provider config: ONLY a configured default
        (provider id, canonical model id, ``zcode_reasoning_level``) on the
        empty rule sets. Nothing from the host's file besides its provider id
        is copied — the session dir is a read root of the zcode session, so
        a provider rule carrying an API key must never land in it. Without
        the level the CLI treats the entry as unselectable and falls back
        silently — the mismatch assertion then catches it."""
        level = str(getattr(self.settings, "zcode_reasoning_level", "") or "max")
        if level not in _REASONING_LEVELS:
            raise ValueError(f"zcode_reasoning_level must be one of {_REASONING_LEVELS}, got {level!r}")
        config = {
            "providerConfigRules": {"providerRules": []},
            "modelConfigRules": {"providerModelRules": [], "manualProviderModelRules": []},
            "defaultModelSelection": {"providerId": self._selected_provider_id, "modelId": self.canonical_model_id(model),
                                      "options": {"reasoningLevel": level}},
        }
        path = session / _PERSONAL_CONFIG_NAME
        path.write_text(json.dumps({"schemaVersion": 1, "config": config}, indent=2), encoding="utf-8")
        return path

    def _run(self, text: str, *, session: Path, timeout_s: float,
             tool_less: bool = False, model: str = "", native_event_sink=None,
             mode: str = "plan") -> tuple[list[dict], bool]:
        """One CLI invocation → (parsed events, timed_out). A timeout kills
        the process but keeps the partial stream as salvage material.
        `tool_less` also removes the native reads unless an oversized prompt
        needs `Read` to reach its attachment. `model` pins the served model
        for this run (empty: the host's configured default)."""
        cmd = [self.require_cli()]
        disallowed = list(_DISALLOWED)
        oversized = len(text.encode("utf-8")) > _ARG_BUDGET
        if tool_less:
            disallowed += ["Grep", "Glob"] + ([] if oversized else ["Read"])
        if oversized:
            prompt_file = session / "PROMPT.md"
            prompt_file.write_text(text, encoding="utf-8")
            cmd += [(f"--prompt=Your complete instructions are in the attached "
                    f"file {prompt_file}. Read ALL of it (page through it with "
                    f"offset) before acting, then follow it exactly."),
                    "--attach", str(prompt_file)]
        else:
            # `--prompt=` form: a prompt starting with "-" must not be
            # parsed as an option.
            cmd += [f"--prompt={text}"]
        cmd += ["--output-format", "stream-json", "--mode", mode,
                "--cwd", str(session),
                f"--disallowed-tools={','.join(disallowed)}"]
        env = sanitized_env()
        env.update({k: os.environ[k] for k in _ZCODE_ENV_KEEP
                    if k in os.environ})
        if model:
            env[_PERSONAL_CONFIG_ENV] = str(self._write_model_config(session, model))
        if native_event_sink is not None:
            return self._stream_run(cmd, session, env, timeout_s, native_event_sink)
        timed_out = False
        returncode, stderr = 0, ""
        try:
            proc = subprocess.run(
                cmd, cwd=str(session), env=env,
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout_s,
                check=False)
            stdout = proc.stdout or ""
            returncode, stderr = proc.returncode, proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            raw = exc.stdout or b""
            stdout = raw.decode("utf-8", "replace") if isinstance(raw, bytes) \
                else str(raw)
        events: list[dict] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not timed_out and (returncode != 0 or not any(
                e.get("type") == "result" for e in events)):
            # Known model failure events have stable, content-free recovery
            # messages. Never expose their request headers or provider text.
            known = _known_model_failure(events)
            if known is not None:
                raise known
            # Unknown CLI failures retain the established diagnostic behavior.
            detail = " ".join(stderr.strip().splitlines()[-3:])[:400]
            raise RuntimeError(
                f"zcode exited {returncode} without a result event"
                + (f": {detail}" if detail else "")
                + " — check `zcode login` and the CLI version")
        return events, timed_out

    @staticmethod
    def _stream_run(cmd, session, env, timeout_s, sink):
        """Journal private regular-file spools without backpressuring the CLI.

        ZCode writes stream events without awaiting pipe backpressure, then
        forces process exit after a short grace period. A durable event sink
        can therefore lose even a completed result through an output pipe.
        POSIX regular-file writes are synchronous in Node; anonymous files
        keep the full output while we journal it at the sink's own pace.
        """
        events, stderr, buffers = [], [], {"stdout": b"", "stderr": b""}

        def line(channel, raw):
            text = raw.decode("utf-8", "replace")
            if channel == "stdout":
                try:
                    event = json.loads(text)
                except ValueError:
                    event = None
                if isinstance(event, dict):
                    events.append(event)
                    sink(event)
                    return
            if channel == "stderr":
                stderr.append(text)
            sink({"type": "native." + channel, "text": text})

        # TemporaryFile creates mode-0600 anonymous files in our session;
        # no named raw-output file persists or enters session attachments.
        with tempfile.TemporaryFile(mode="w+b", buffering=0, dir=session) as stdout, \
                tempfile.TemporaryFile(mode="w+b", buffering=0, dir=session) as stderr_file:
            spools = {"stdout": stdout, "stderr": stderr_file}
            offsets = {channel: 0 for channel in spools}
            proc = subprocess.Popen(cmd, cwd=str(session), env=env, stdin=subprocess.DEVNULL,
                                    stdout=stdout, stderr=stderr_file, start_new_session=True)
            timed_out = False
            group_stopped = False
            stop_lock = threading.Lock()

            def kill(*, at_deadline=False):
                nonlocal group_stopped, timed_out
                with stop_lock:
                    if group_stopped or (at_deadline and proc.poll() is not None):
                        return
                    group_stopped = True
                    if at_deadline:
                        timed_out = True
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

            # A slow sink must neither postpone a real producer timeout nor
            # turn draining an already-exited producer into a false timeout.
            watchdog = threading.Timer(timeout_s, lambda: kill(at_deadline=True))
            watchdog.daemon = True
            watchdog_started = False

            def emit(channel, raw, *, best_effort=False):
                if not best_effort:
                    line(channel, raw)
                    return
                try:
                    line(channel, raw)
                except BaseException:
                    pass  # retain the original interruption / sink failure

            def drain(channel, *, best_effort=False):
                # pread must not move the shared child descriptor's offset:
                # seek/read on it could make the CLI overwrite prior output.
                chunk = os.pread(spools[channel].fileno(), 65536, offsets[channel])
                offsets[channel] += len(chunk)
                buffers[channel] += chunk
                while b"\n" in buffers[channel]:
                    raw, buffers[channel] = buffers[channel].split(b"\n", 1)
                    emit(channel, raw, best_effort=best_effort)
                return bool(chunk)

            def tails(*, best_effort=False):
                for channel in buffers:
                    raw, buffers[channel] = buffers[channel], b""
                    if raw:
                        emit(channel, raw, best_effort=best_effort)

            try:
                watchdog.start()
                watchdog_started = True
                while True:
                    alive = proc.poll() is None
                    if not alive:
                        # The leader has finished; no owned descendant may
                        # keep growing a spool after its completed invocation.
                        kill()
                    changed = False
                    for channel in spools:
                        changed = drain(channel) or changed
                    if not alive and not changed:
                        tails()
                        break
                    if not changed:
                        time.sleep(0.02)
                proc.wait()
            except BaseException:
                # Stop production first, then preserve all bytes already
                # spooled, including tails lacking a final newline. Sink
                # failures must never replace the original interruption.
                kill()
                proc.wait()
                for channel in spools:
                    try:
                        while drain(channel, best_effort=True):
                            pass
                    except BaseException:
                        pass
                tails(best_effort=True)
                raise
            finally:
                # This process group belongs only to this invocation.
                watchdog.cancel()
                if watchdog_started:
                    watchdog.join()
                kill()
                proc.wait()
        if not timed_out and (proc.returncode != 0 or not any(e.get("type") == "result" for e in events)):
            known = _known_model_failure(events)
            if known is not None:
                raise known
            detail = " ".join(stderr[-3:])[:400]
            raise RuntimeError(f"zcode exited {proc.returncode} without a result event"
                               + (f": {detail}" if detail else ""))
        return events, timed_out

    @staticmethod
    def _final_text(events: list[dict]) -> str:
        for event in reversed(events):
            if event.get("type") == "result":
                return str(event.get("response") or "").strip()
        return ""

    @staticmethod
    def _usage(events: list[dict]) -> SessionUsage:
        usage = SessionUsage()
        for event in events:
            payload = event.get("payload")
            if event.get("type") == "session.updated" \
                    and isinstance(payload, dict) and payload.get("modelId"):
                usage.served_model = str(payload["modelId"])
            if event.get("type") == "result" \
                    and isinstance(event.get("usage"), dict):
                raw = event["usage"]
                usage.input_tokens = int(raw.get("inputTokens") or 0)
                usage.output_tokens = int(raw.get("outputTokens") or 0)
        return usage

    @classmethod
    def native_snapshot(cls, events: list[dict]) -> dict:
        """Reported native facts; omitted counters and invoice cost are unknown."""
        usage, served_model = {}, ""
        keys = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
                "cacheReadInputTokens": "cache_read_input_tokens",
                "cacheCreationInputTokens": "cache_creation_input_tokens", "costUsd": "cost_usd"}
        for event in events:
            payload = event.get("payload")
            if event.get("type") == "session.updated" and isinstance(payload, dict) and payload.get("modelId"):
                served_model = str(payload["modelId"])
            if event.get("type") == "result" and isinstance(event.get("usage"), dict):
                raw = event["usage"]
                usage = {target: raw[source] for source, target in keys.items()
                         if isinstance(raw.get(source), (int, float)) and not isinstance(raw[source], bool)}
        return {"served_model": served_model, "usage": usage,
                "text": cls._final_text(events)}

    @staticmethod
    def _tool_calls(events: list[dict]) -> list[tuple[str, dict]]:
        """(name, input) of every tool the session scheduled, in order."""
        calls: list[tuple[str, dict]] = []
        for event in events:
            payload = event.get("payload")
            if event.get("type") == "tool.updated" \
                    and isinstance(payload, dict) \
                    and payload.get("kind") == "scheduled" \
                    and payload.get("toolName"):
                args = payload.get("input")
                calls.append((str(payload["toolName"]),
                              args if isinstance(args, dict) else {}))
        return calls

    @staticmethod
    def _audit(calls: list[tuple[str, dict]], *, roots: tuple[str, ...],
               cwd: str) -> list[str]:
        """Allowlist + containment violations. Bridge calls are exempt: the
        bridge enforces its own scope preventively."""
        violations: set[str] = set()
        for name, args in calls:
            if name.startswith(_BRIDGE_PREFIX):
                continue
            if name not in _READ_TOOLS:
                violations.add(
                    f"zcode tool outside the read-only allowlist: {name}")
                continue
            paths = [os.path.join(cwd, str(args[key])) for key in _PATH_KEYS
                     if args.get(key)]
            pattern = str(args.get("pattern") or "")
            if name == "Glob" and pattern:
                # a glob names part of its root in the pattern itself
                # ("/etc/*", "../../etc/*"): resolve it against the search
                # dir and check the literal prefix before the first wildcard
                base = paths[0] if paths else cwd
                literal = re.split(r"[*?\[{]", pattern, maxsplit=1)[0]
                paths.append(os.path.normpath(os.path.join(base, literal)))
            for path in paths:
                if not contained_in(path, roots):
                    violations.add(
                        f"{name} outside session roots: {path[:160]}")
        return sorted(violations)

    def _check_model(self, requested: str, served: str, trace=None) -> None:
        """`STRICT_BACKEND_MODEL` is an assertion on zcode (see module
        docstring): compare it with what was served."""
        if not requested:
            return
        if not served:
            verdict = "unverified"
        elif requested.casefold() == served.casefold():
            verdict = "ok"
        else:
            verdict = "mismatch"
        if trace is not None:
            trace.record("harness_model_check", provider=self.spec.id,
                         requested=requested, served=served, verdict=verdict)
        if verdict == "unverified":
            logger.warning("zcode stream named no model — served model "
                           "unverified (requested %s)", requested)
        elif verdict == "mismatch":
            if getattr(self.settings, "model_mismatch_policy", "fail") == "fail":
                raise ModelMismatchError(requested=requested, served=served,
                                         endpoint="zcode (host default model; "
                                         "set it with /model in zcode)")
            logger.warning("MODEL MISMATCH accepted by policy=warn: requested "
                           "%s, zcode served %s", requested, served)

    # -- transport contract --------------------------------------------------
    def run_session(self, req: AgentSessionRequest) -> AgentOutcome:
        session = Path(tempfile.mkdtemp(prefix="imc-zcode-session-"))
        if req.trace is not None:
            req.trace.record(
                "harness_session_started", provider=self.spec.id,
                step=req.step_name,
                requested_model=req.model or self.settings.strict_backend_model,
                session_dir=str(session))
        try:
            if req.bridge_spec_path is not None:
                self._write_mcp_config(session, req.bridge_spec_path,
                                       timeout_s=req.timeout_s)
            header = ""
            if req.scope.root:
                header = (f"The repository checkout is at {req.scope.root}. "
                          "Your working directory is a scratch directory: "
                          "read repository files by absolute path under "
                          "that checkout.\n\n")
            events, timed_out = self._run(
                f"{header}{req.system}\n\n{req.prompt}", session=session,
                timeout_s=req.timeout_s,
                model=req.model or self.settings.strict_backend_model,
                # Mutating workflows write only through the scoped bridge;
                # native writes/shell/subagents retain the same denylist.
                # A read-only scope takes `guarded`, not `plan`: zcode 0.18
                # asks for every MCP tool in plan mode, so a headless prompt
                # gets the bridge denied. After the denylist both modes leave
                # the same native tools; `guarded` keeps asking for dangerous
                # commands, which headless turns into a denial.
                mode="guarded" if req.scope.read_only else "yolo")
        finally:
            shutil.rmtree(session, ignore_errors=True)
        usage = self._usage(events)
        calls = self._tool_calls(events)
        used = [name for name, _ in calls]
        violations = self._audit(
            calls, roots=(req.scope.root or "", str(req.run_dir),
                          str(session)), cwd=str(session))
        bridge_calls = sum(1 for name in used
                           if name.startswith(_BRIDGE_PREFIX))
        if req.trace is not None:
            req.trace.record(
                "harness_session", provider=self.spec.id, step=req.step_name,
                audit_ok=not violations, audit_violations=violations[:10],
                tool_calls=len(used), bridge_calls=bridge_calls,
                timed_out=timed_out, event_count=len(events),
                served_model=usage.served_model)
        self._check_model(req.model or self.settings.strict_backend_model,
                          usage.served_model, req.trace)
        return AgentOutcome(
            text=self._final_text(events),
            iterations=0,  # zcode does not expose a turn budget
            tool_calls=len(used),
            truncated=timed_out,
            refusals=[f"audit: {v}" for v in violations],
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            tools_used=used[:40])

    def complete(self, *, system: str, messages: list[dict],
                 model: str = "", max_tokens: int | None = None,
                 role: str = "", effort: str = "",
                 max_budget_usd: float | None = None, native_event_sink=None) -> Reply:
        """Tool-less one-shot in an empty scratch cwd with no bridge. Every
        native read is removed, except `Read` when the prompt is too big for
        argv and rides an attachment — then any read outside the scratch dir
        (the input here can be untrusted text) fails the call."""
        session = Path(tempfile.mkdtemp(prefix="imc-zcode-oneshot-"))
        try:
            self._write_oneshot_config(session)
            events, timed_out = self._run(
                flatten_messages(system, messages), session=session,
                timeout_s=self.settings.strict_backend_timeout_s,
                tool_less=True, model=model or self.settings.strict_backend_model,
                **({"native_event_sink": native_event_sink} if native_event_sink is not None else {}))
        finally:
            shutil.rmtree(session, ignore_errors=True)
        violations = self._audit(self._tool_calls(events),
                                 roots=(str(session),), cwd=str(session))
        if violations:
            raise RuntimeError("zcode one-shot broke containment: "
                               + "; ".join(violations))
        usage = self._usage(events)
        self._check_model(model or self.settings.strict_backend_model,
                          usage.served_model)
        text = self._final_text(events)
        return Reply(
            blocks=[Block(type="text", text=text)] if text else [],
            stop_reason="max_tokens" if timed_out else "end_turn",
            usage=self.native_snapshot(events)["usage"],
            model=usage.served_model)
