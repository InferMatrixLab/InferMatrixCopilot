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
  can declare stdio MCP servers (i.e. commands) and zcode connects them
  without a trust prompt. Running inside a PR checkout would let the PR
  under review configure the reviewer. The worktree is read by absolute
  path instead; the prompt names it.
- The MCP tool bridge rides that scratch dir's ``.zcode/config.json``
  (measured live: ``mcp__infermatrix-tools__read_file`` called, bridge
  trace recorded), so scoped reads still pass ``tools.dispatch``.

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

import json
import logging
import os
import re
import time
import subprocess
import tempfile
from functools import cached_property
from pathlib import Path

from ..agent_loop import AgentOutcome
from ..llm import ModelMismatchError, Reply
from .audit import contained_in
from .base import (
    AgentSessionRequest,
    HarnessTransport,
    SessionUsage,
    bridge_server,
    flatten_messages,
    json_events,
    run_cli,
    sanitized_env,
)
from .registry import PROVIDERS

logger = logging.getLogger(__name__)

_BRIDGE_SERVER = "infermatrix-tools"
_BRIDGE_PREFIX = f"mcp__{_BRIDGE_SERVER}__"

# Everything zcode 0.16.9 exposes in plan mode that is not a pure read.
_DISALLOWED = (
    "Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Agent", "Skill",
    "ExitPlanMode", "EnterPlanMode", "AskUserQuestion", "SendMessage",
    "WebFetch", "WebSearch", "CreateWorkflow", "AmendWorkflow",
    "SaveWorkflow", "EvalWorkflowSnippet", "ResumeWorkflowRun",
    "ResolveWorkflowQuestion", "TaskStop", "mcp__node_repl__js",
    "mcp__web_reader__webReader", "mcp__4_5v_mcp__analyze_image",
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
_CREDENTIAL_RUNTIME_VERSION = '0.16.9'  # audited native tool names and coding-account format
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
    completion_worker_settings = ('zcode_provider_id', 'zcode_reasoning_level')

    @cached_property
    def _selected_provider_id(self) -> str:
        return (str(getattr(self.settings, "zcode_provider_id", "") or "")
                or self._host_provider_id() or _DEFAULT_PROVIDER_ID)

    @property
    def subscription_billing(self) -> bool:
        """Known OAuth coding plan only; custom API providers still need caps.

        Billing mode is not a measured zero-dollar call cost.
        """
        configured = self._configured_subscription_billing()
        if configured is not None:
            return configured
        return self._selected_provider_id == _DEFAULT_PROVIDER_ID

    def credential_snapshot(self, path, *, owner_uid=None, options=None):
        from .zcode_credentials import login_snapshot
        return login_snapshot(path, owner_uid=owner_uid, provider_id=(options or {}).get('zcode_provider_id'))

    def subscription_credential_identity(self, credentials):
        expiry = credentials.get('expires_at')
        return (credentials.get('kind') == 'zcode-coding-plan'
                and (expiry is None or type(expiry) is int and expiry > time.time()))

    def authentication_command(self, native_cli):
        return ['/usr/local/bin/python', '-I', '-B', '-m', 'infermatrix_copilot.providers.zcode_credentials']

    @staticmethod
    def subscription_auth_status(returncode, text):
        try:
            data = json.loads(text)
            return (returncode == 0 and isinstance(data, dict)
                and set(data) == {'schema', 'provider', 'authenticated', 'native_version'}
                and data['schema'] == 'zcode-account-status/1' and data['provider'] == _DEFAULT_PROVIDER_ID
                and data['authenticated'] is True and ZCodeTransport.credential_runtime_supported(data['native_version']))
        except (ValueError, TypeError):
            return False

    @staticmethod
    def credential_runtime_supported(version):
        return isinstance(version, str) and version.strip() == _CREDENTIAL_RUNTIME_VERSION

    def auth_probe(self, command=None, *, env=None, timeout_s=15, execution_profile=None):
        if execution_profile is None:
            return super().auth_probe(command, env=env, timeout_s=timeout_s)
        if command is not None or env or getattr(self, 'runner', None) is not None:
            raise ValueError('sandbox authentication cannot inherit a host command or environment')
        from .provider_options import configured_settings
        settings = configured_settings(self.settings, self.spec.id)
        execution_profile.validate()
        argv = self.authentication_command(execution_profile.native_cli)
        result = execution_profile.execute(argv, cwd='/tmp/work', env={},
            input=json.dumps({'provider_id': settings.zcode_provider_id, 'native_cli': execution_profile.native_cli}),
            deadline=time.monotonic() + timeout_s)
        if result.reason is not None:
            raise subprocess.TimeoutExpired(argv, timeout_s)
        return subprocess.CompletedProcess(argv, result.returncode, result.stdout, result.stderr)

    def _check_credential_provider(self, events):
        from .zcode_credentials import snapshot_home
        if snapshot_home() is None:
            return
        providers = [(event.get('payload') or {}).get('providerId') for event in events
                     if event.get('type') == 'session.updated' and isinstance(event.get('payload'), dict)
                     and 'providerId' in event['payload']]
        if not providers or any(provider != self._selected_provider_id for provider in providers):
            from ..sandbox.context import ContextChanged
            raise ContextChanged('zcode did not attest the selected native account provider')

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
    def _write_mcp_config(session: Path, spec_path: Path) -> None:
        config = session / ".zcode" / "config.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"features": {"memory": False}, "memory": {"use": False},
                                     "mcp": {"servers": {_BRIDGE_SERVER: {
                                         "type": "stdio", **bridge_server(spec_path)}}}}, indent=2), encoding="utf-8")

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
             tool_less: bool = False, model: str = "", native_event_sink=None) -> tuple[list[dict], bool]:
        """One CLI invocation → (parsed events, timed_out). A timeout kills
        the process but keeps the partial stream as salvage material.
        `tool_less` also removes the native reads unless an oversized prompt
        needs `Read` to reach its attachment. `model` pins the served model
        for this run (empty: the host's configured default)."""
        cmd = [self.require_cli()]
        disallowed = list(_DISALLOWED)
        oversized = len(text.encode("utf-8")) > _ARG_BUDGET
        from .zcode_credentials import snapshot_home
        credentialed = snapshot_home() is not None
        argument = None
        if tool_less and oversized and credentialed:
            from .node_arguments import NodeArgument
            argument = NodeArgument(session, text, prefix='--prompt=')
        if tool_less:
            disallowed += (sorted(_READ_TOOLS) if credentialed else
                           ["Grep", "Glob"] + ([] if oversized else ["Read"]))
        if argument is not None:
            cmd += [argument.argument]
        elif oversized:
            prompt_file = session / "PROMPT.md"
            prompt_file.write_text(text, encoding="utf-8")
            cmd += [f"--prompt=Your complete instructions are in the attached "
                    f"file {prompt_file}. Read ALL of it (page through it with "
                    f"offset) before acting, then follow it exactly.",
                    "--attach", str(prompt_file)]
        else:
            # `--prompt=` form: a prompt starting with "-" must not be
            # parsed as an option.
            cmd += [f"--prompt={text}"]
        cmd += ["--output-format", "stream-json", "--mode", "plan",
                "--cwd", str(session),
                f"--disallowed-tools={','.join(disallowed)}"]
        env = sanitized_env()
        env.update({k: os.environ[k] for k in _ZCODE_ENV_KEEP
                    if k in os.environ})
        if model:
            env[_PERSONAL_CONFIG_ENV] = str(self._write_model_config(session, model))
        if credentialed:
            started = time.monotonic()
            version = self.version_probe(env=env, timeout_s=min(timeout_s, 10))
            if version.returncode or not self.credential_runtime_supported(version.stdout):
                raise ValueError('native CLI does not support the audited credential tool policy')
            timeout_s = max(0, timeout_s - (time.monotonic() - started))
        if argument is not None:
            env.update(argument.env)
            started = time.monotonic()
            stdout, _, code, timed_out = run_cli([cmd[0], '--version'], cwd=str(session), env=env,
                timeout_s=min(timeout_s, 15), stdin=subprocess.DEVNULL)
            argument.accept_probe(stdout, code, timed_out)
            timeout_s = max(0, timeout_s - (time.monotonic() - started))
        if native_event_sink is not None:
            result = self._stream_run(cmd, session, env, timeout_s, native_event_sink)
            if argument is not None:
                argument.validate_events(result[0])
            return result
        stdout, stderr, returncode, timed_out = run_cli(cmd, cwd=str(session), env=env,
                                                      stdin=subprocess.DEVNULL, timeout_s=timeout_s)
        events = json_events(stdout)
        if argument is not None:
            argument.validate_events(events)
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
        from .process import run_process

        events, stderr = [], []

        def line(channel, text):
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

        result = run_process(cmd, cwd=str(session), env=env,
                             deadline=time.monotonic() + timeout_s,
                             output="spool", on_line=line)
        timed_out = bool(result.reason)
        if not timed_out and (result.returncode != 0 or not any(e.get("type") == "result" for e in events)):
            known = _known_model_failure(events)
            if known is not None:
                raise known
            detail = " ".join(stderr[-3:])[:400]
            raise RuntimeError(f"zcode exited {result.returncode} without a result event"
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
            if event.get("type") == "result":
                raw = event.get("usage")
                raw = raw if isinstance(raw, dict) else {}
                # Result usage is a cumulative snapshot, not a per-turn delta.
                usage = SessionUsage(served_model=usage.served_model)
                usage.add_token_usage({target: raw.get(source) for source, target in (
                    ("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                    ("cacheReadInputTokens", "cache_read_input_tokens"),
                    ("cacheCreationInputTokens", "cache_creation_input_tokens"))})
                usage.set_reported_cost(raw.get("costUsd"))
        if usage.reported_usage is None:
            usage.reported_usage = {}
        return usage

    @classmethod
    def native_snapshot(cls, events: list[dict]) -> dict:
        """Reported native facts; omitted counters and invoice cost are unknown."""
        usage = cls._usage(events)
        return {"served_model": usage.served_model, "usage": dict(usage.reported_usage),
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
    def validate_completion(self):
        if getattr(self, 'native_recovery_only', False):
            from .completion_profiles import CompletionPolicyError
            raise CompletionPolicyError('original result transport cannot dispatch another model call')
        profile = getattr(self, 'completion_profile', None)
        if profile is None:
            return super().validate_completion()
        from ..sandbox import SandboxCompletionProfile
        if not isinstance(profile, SandboxCompletionProfile) or getattr(self, 'runner', None) is not None:
            raise ValueError('unsupported ZCode completion profile')
        profile.validate()

    def validate_completion_request(self, request):
        if getattr(self, 'completion_profile', None) is not None:
            if request.get('readonly') or request.get('output_schema') is not None:
                raise ValueError('ZCode sandbox native schema/session execution is unsupported')
            if request.get('max_budget_usd') is not None:
                raise ValueError('zcode cannot stop a call at a spend threshold')
            if request.get('idle_timeout_s') is not None:
                raise ValueError('ZCode native byte-idle deadline is unsupported')

    @classmethod
    def process_receipt(cls, outcome):
        from .completion_worker import process_receipt
        return process_receipt(outcome, native_snapshot=cls.native_snapshot)

    def run_session(self, req: AgentSessionRequest) -> AgentOutcome:
        self.validate_session(req)
        with tempfile.TemporaryDirectory(prefix="imc-zcode-session-", ignore_cleanup_errors=True) as scratch:
            session = Path(scratch)
            if req.bridge_spec_path is not None:
                self._write_mcp_config(session, req.bridge_spec_path)
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
                **({"native_event_sink": req.native_event_sink} if req.native_event_sink is not None else {}))
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
        # Zcode does not expose a turn budget.
        return usage.outcome(self._final_text(events), tool_calls=len(used), tools_used=used,
                             truncated=timed_out, refusals=[f"audit: {v}" for v in violations])

    def complete(self, *, system: str, messages: list[dict],
                 model: str = "", max_tokens: int | None = None,
                 role: str = "", effort: str = "",
                 max_budget_usd: float | None = None, native_event_sink=None,
                 frozen_inputs=None, idle_timeout_s=None, absolute_deadline=None,
                 output_schema=None, readonly=False) -> Reply:
        """Tool-less one-shot in an empty scratch cwd with no bridge. Every
        native read is removed, except `Read` when the prompt is too big for
        argv and rides an attachment — then any read outside the scratch dir
        (the input here can be untrusted text) fails the call."""
        self.validate_completion_request(dict(system=system, messages=messages, readonly=readonly,
            output_schema=output_schema, max_budget_usd=max_budget_usd, idle_timeout_s=idle_timeout_s))
        if getattr(self, 'completion_profile', None) is not None:
            self.validate_completion()
            from .completion_worker import complete
            return complete(self, dict(system=system, messages=messages, model=model,
                max_tokens=max_tokens, role=role, effort=effort, max_budget_usd=max_budget_usd),
                frozen_inputs=frozen_inputs, idle_timeout_s=idle_timeout_s,
                absolute_deadline=absolute_deadline, native_event_sink=native_event_sink)
        if (frozen_inputs is not None or output_schema is not None or readonly
                or idle_timeout_s is not None or absolute_deadline is not None):
            raise ValueError('explicit sandbox inputs require a ZCode completion profile')
        with tempfile.TemporaryDirectory(prefix="imc-zcode-oneshot-", ignore_cleanup_errors=True) as scratch:
            session = Path(scratch)
            self._write_oneshot_config(session)
            events, timed_out = self._run(
                flatten_messages(system, messages), session=session,
                timeout_s=self.settings.strict_backend_timeout_s,
                tool_less=True, model=model or self.settings.strict_backend_model,
                **({"native_event_sink": native_event_sink} if native_event_sink is not None else {}))
        violations = self._audit(self._tool_calls(events),
                                 roots=(str(session),), cwd=str(session))
        if violations:
            raise RuntimeError("zcode one-shot broke containment: "
                               + "; ".join(violations))
        usage = self._usage(events)
        self._check_model(model or self.settings.strict_backend_model,
                          usage.served_model)
        self._check_credential_provider(events)
        return usage.reply(self._final_text(events), stop_reason="max_tokens" if timed_out else "end_turn")

    def get_completion_result(self, *, recover=False):
        """Read the original sandbox binding; do not reconstruct model inputs."""
        if getattr(self, 'completion_profile', None) is None:
            raise ValueError('original sandbox completion profile is required')
        from .completion_worker import read_result
        return read_result(self, recover=recover)
