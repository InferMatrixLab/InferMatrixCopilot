"""Provider-layer contracts (doc/features/provider-registry.md).

A *provider* is one way to reach a model. Two kinds exist:

- ``api`` — a raw Anthropic/OpenAI-compatible completions endpoint (today's
  `llm.LLM`, resolved by `Settings.tier_target`). Stateless
  ``system+messages+tools -> tool_use`` round trips; our `agent_loop` owns
  the tool loop.
- ``harness`` — a subscription-authed vendor agent CLI (cursor-agent,
  claude -p, codex exec). Harnesses own their tool loop, so the integration
  seam is a WHOLE agent step (`run_session`), never `LLM.create`; tool
  access flows back through the MCP tool bridge so every call still passes
  `tools.dispatch`.

The credential model is deliberately asymmetric: api providers resolve
key+endpoint through Settings; harness providers hold their subscription
auth inside the vendor CLI's own state and this codebase never sees it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..scopes import ToolScope
from ..budgeting import valid_token_counts
from .process import run_process

# Env a harness CLI subprocess keeps. Everything else — API keys, base URLs,
# gh tokens, host markers like CLAUDECODE — is dropped: subscription auth
# lives in HOME state, and an inherited ANTHROPIC_BASE_URL (a gateway on
# this class of machine) would silently reroute a vendor CLI's traffic.
_ENV_KEEP = {"PATH", "HOME", "TERM", "COLORTERM", "LANG", "USER", "LOGNAME",
             "SHELL", "TMPDIR"}
_ENV_KEEP_PREFIXES = ("LC_", "XDG_")


def sanitized_env() -> dict[str, str]:
    """The allowlisted environment for spawning a harness CLI."""
    from .worker_environment import proxy_environment
    return {**{k: v for k, v in os.environ.items()
              if k in _ENV_KEEP or k.startswith(_ENV_KEEP_PREFIXES)},
            **proxy_environment(os.environ)}


def run_cli(cmd, *, cwd, env, timeout_s, input=None, stdin=None, runner=None, executor=None):
    """Buffered compatibility projection over the shared process supervisor.

    An explicitly injected runner is a legacy test/integration seam; production
    dispatch uses the supervisor, or an explicitly composed execution profile.
    """
    if executor is not None and (runner is not None or stdin is not None):
        raise ValueError('an execution profile cannot share a runner or host stdin')
    if runner is not None:
        try:
            proc = runner(cmd, input=input, stdin=stdin, cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired as exc:
            raw = exc.stdout or b""
            return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw), "", 0, True
        return proc.stdout or "", proc.stderr or "", proc.returncode, False
    execute = executor or run_process
    kwargs = {} if executor is not None else {'stdin': stdin}
    result = execute(cmd, cwd=cwd, env=env, deadline=time.monotonic() + timeout_s,
                     input=input, **kwargs)
    # Preserve the legacy timeout sentinel; partial stdout remains available.
    return result.stdout, "" if result.reason else result.stderr, 0 if result.reason else result.returncode, bool(result.reason)


def stream_cli(cmd, *, cwd, env, deadline, input=None, idle_timeout_s=None,
               on_line=None, termination_grace_s=0):
    """Pipe-streaming compatibility projection over the process supervisor."""
    result = run_process(cmd, cwd=cwd, env=env, deadline=deadline, input=input,
                         idle_timeout_s=idle_timeout_s, on_line=on_line,
                         termination_grace_s=termination_grace_s)
    return result.stdout, result.stderr, result.returncode, result.reason


def json_events(stdout):
    """Read the existing newline JSON protocol, ignoring warnings and bad lines."""
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def bridge_server(spec_path):
    """The stdio command entry; each provider owns its enclosing config policy."""
    return {"command": sys.executable,
            "args": ["-m", "infermatrix_copilot.tool_bridge", "--spec", str(spec_path)],
            "env": {"PYTHONPATH": str(Path(__file__).resolve().parents[2])}}


@dataclass(frozen=True)
class ProviderSpec:
    """One registry entry: identity, kind, and the capability flags the
    integration can rely on (``mcp_tools``, ``builtin_tools_off``,
    ``max_turns``, ``system_prompt``, ``usage_reporting``,
    ``cost_reporting``). `cli_names` are the binaries probed on PATH for
    harness kinds.

    `default_model` is the model a backend serves when `STRICT_BACKEND_MODEL`
    is unset. It is empty for backends that pick their own (the subscription
    CLIs) and set only where the backend REQUIRES one — declaring it here
    rather than inside a transport keeps `Settings.tier_target` the single
    place a model is resolved, so the requested model and the served model
    are the same string in the target, the trace and the metrics."""

    id: str
    kind: Literal["api", "harness"]
    display: str
    cli_names: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset()
    default_model: str = ""
    execution_modes: tuple[str, ...] = ("strict",)
    readiness_args: tuple[str, ...] = ()


@dataclass
class AgentSessionRequest:
    """One delegated agent step: the SAME prompt bundle `run_agent` would
    receive (contract `system` + rendered dispatch context `prompt`), the
    step's `scope`, and the session bounds. `bridge_spec_path` points at the
    serialized tool-bridge spec the harness's MCP config should launch;
    `max_iters` maps to a native turn cap where the harness has one
    (claude --max-turns) and otherwise rides `timeout_s` plus the prompt's
    budget-discipline lines."""

    system: str
    prompt: str
    scope: ToolScope
    model: str
    max_iters: int
    timeout_s: float
    run_dir: Path
    step_name: str = ""
    bridge_spec_path: Path | None = None
    trace: Any = None  # RunTrace-shaped (record(kind, **fields)) or None
    native_event_sink: Any = None  # Optional receipt observer; independent of trace.
    frozen_inputs: dict | None = None  # Prepared by the trusted session workflow.
    idle_timeout_s: float | None = None
    absolute_deadline: float | None = None
    effort: str = ""
    output_schema: dict | None = None  # Explicit capability; never inferred from prompts.


@dataclass
class SessionUsage:
    """Best-effort usage extracted from a harness CLI's output. Harnesses
    report unevenly; legacy display counters remain numeric while
    ``reported_usage`` distinguishes unknown facts from reported zero.
    Missing cost stays None; subscription invoice amounts are never invented.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    served_model: str = ""
    tools_used: list[str] = field(default_factory=list)
    reported_usage: dict | None = None

    def add_token_usage(self, counters: dict) -> None:
        """Sum per-turn counters without presenting partial totals as complete.

        Adapters translate native names before calling this method. Missing or
        malformed counters remain unknown; legacy display totals remain numeric.
        """
        known = {key: value for key, value in counters.items()
                 if valid_token_counts(value)}
        self.input_tokens += known.get("input_tokens", 0)
        self.output_tokens += known.get("output_tokens", 0)
        if self.reported_usage is None:
            self.reported_usage = known
        else:
            self.reported_usage = {key: total + known[key]
                                   for key, total in self.reported_usage.items() if key in known}

    @staticmethod
    def normalized_cost(value) -> float | None:
        from ..budgeting import normalized_cost
        return normalized_cost(value)

    def set_reported_cost(self, value) -> None:
        """Record a native total, retaining missing/invalid cost as unknown."""
        self.cost_usd = self.normalized_cost(value)
        if self.reported_usage is None:
            self.reported_usage = {}
        self.reported_usage.pop("cost_usd", None)
        if self.cost_usd is not None:
            self.reported_usage["cost_usd"] = self.cost_usd

    def reply(self, text: str, *, stop_reason="end_turn", usage=None):
        """Normalize a tool-less result; native usage may replace the default counters."""
        from ..llm import Block, Reply

        return Reply(blocks=[Block(type="text", text=text)] if text else [],
                     stop_reason=stop_reason, model=self.served_model,
                     usage=dict(usage) if usage is not None else
                     dict(self.reported_usage) if self.reported_usage is not None else {
                         "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

    def outcome(self, text: str, *, tool_calls: int, tools_used=(),
                truncated=False, refusals=(), iterations=0):
        """Normalize a session after its provider's containment checks."""
        from ..agent_loop import AgentOutcome

        return AgentOutcome(text=text, iterations=iterations, tool_calls=tool_calls,
                            truncated=truncated, refusals=list(refusals),
                            input_tokens=self.input_tokens, output_tokens=self.output_tokens,
                            tools_used=list(tools_used)[:40],
                            usage=dict(self.reported_usage) if self.reported_usage is not None else None)


@dataclass(frozen=True)
class SessionEvent:
    """Vendor-independent facts decoded by one provider adapter."""

    text: str | None = None
    session_id: str | None = None
    served_model: str | None = None
    usage: dict | None = None
    error: str | None = None


def execution_effort(transport, requested=''):
    """Resolve a trusted native default before request capture and admission."""
    value = requested or getattr(getattr(transport, 'settings', None), 'strict_backend_effort', '')
    if value:
        validate = getattr(transport, 'validate_effort', None)
        if not callable(validate):
            raise ValueError('provider does not support an explicit native reasoning effort')
        validate(value)
    return value


def native_budget_facts(transport, *, session=False, provider_id=''):
    """Execution facts only; domains select the account and authorize its limits."""
    profile = getattr(transport, 'completion_profile', None)
    admitted = (not session and getattr(transport, 'configured_subscription_admission', None) is not None
                and profile is not None
                and getattr(getattr(profile, 'sandbox', None), 'expected_context', None) is not None
                and getattr(transport, 'subscription_billing', False) is True)
    return {'provider_id': getattr(getattr(transport, 'spec', None), 'id', provider_id), 'subscription_admitted': admitted,
            'stops_at_spend': not session and getattr(transport, 'stops_at_spend', False) is True}


class HarnessTransport:
    """Base for harness-kind transports. Subclasses set `spec` and implement
    `run_session` (a whole agent step) and `complete` (one-shot TOOL-LESS
    call for `HarnessLLM`). `cli_path` is the shared binary resolution:
    explicit `STRICT_BACKEND_CLI` first, then the spec's names on PATH."""

    spec: ProviderSpec
    json_output_file = False
    supports_session_resume = False

    @staticmethod
    def readonly_json_command(command, **options):
        """Encode this vendor's read-only structured session policy."""
        raise NotImplementedError("provider has no read-only JSON session policy")

    @staticmethod
    def decode_event(event: dict) -> SessionEvent:
        raise NotImplementedError("provider has no native event decoder")

    @staticmethod
    def readonly_events(stdout: str, *, validate: bool = True) -> list[dict]:
        """Decode native output, optionally enforcing its successful envelope."""
        return json_events(stdout)

    @classmethod
    def validate_readonly_completion(cls, events: list[dict]) -> None:
        """One native success contract; callers translate their own errors."""
        for event in reversed(events):
            error = cls.decode_event(event).error
            if error:
                raise ValueError(error)

    @staticmethod
    def auth_status_issue(returncode: int, text: str) -> str | None:
        """Interpret the vendor's authentication probe without choosing its environment."""
        return "provider authentication probe failed" if returncode else None

    def __init__(self, settings):
        self.settings = settings

    def auth_probe(self, command=None, *, env=None, timeout_s=15, execution_profile=None):
        """One bounded authentication probe through the shared supervisor."""
        return self._probe(command, self.spec.readiness_args, env=env, timeout_s=timeout_s,
                           execution_profile=execution_profile)

    def authentication_command(self, native_cli):
        return [native_cli, *self.spec.readiness_args]

    def credential_snapshot(self, path, *, owner_uid=None, options=None):
        raise ValueError('provider cannot prepare a native credential snapshot')

    def subscription_credential_identity(self, credentials):
        return False

    def _configured_subscription_billing(self):
        profile = getattr(self, 'completion_profile', None)
        if getattr(self, 'configured_subscription_admission', None) is not None:
            return profile is not None and getattr(profile.sandbox, 'expected_context', None) is not None
        return False if profile is not None else None

    def version_probe(self, command=None, *, env=None, timeout_s=30):
        """A native version diagnostic, with the same lifecycle as auth probes."""
        return self._probe(command, ("--version",), env=env, timeout_s=timeout_s)

    def _probe(self, command, arguments, *, env, timeout_s, execution_profile=None):
        if execution_profile is not None:
            if command is not None or env or getattr(self, 'runner', None) is not None:
                raise ValueError('sandbox probe cannot inherit a host command, environment or runner')
            execution_profile.validate()
        argv = [*(command or [execution_profile.native_cli if execution_profile else self.require_cli()]), *arguments]
        stdout, stderr, code, timed_out = run_cli(argv, cwd='/tmp/work' if execution_profile else None,
            env={} if execution_profile else sanitized_env() if env is None else env, timeout_s=timeout_s,
            runner=getattr(self, "runner", None),
            **({'executor': execution_profile.execute} if execution_profile else {}))
        if timed_out:
            raise subprocess.TimeoutExpired(argv, timeout_s, output=stdout)
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    def cli_path(self) -> str | None:
        """The harness binary to execute, or None when not installed."""
        override = getattr(self.settings, "strict_backend_cli", "")
        if override:
            return override
        for name in self.spec.cli_names:
            found = shutil.which(name)
            if found:
                return found
        return None

    def require_cli(self) -> str:
        """`cli_path` or a RuntimeError naming the exact fix."""
        cli = self.cli_path()
        if not cli:
            raise RuntimeError(
                f"{self.spec.id} backend selected but no CLI found "
                f"({' / '.join(self.spec.cli_names)}) — install it or set "
                "STRICT_BACKEND_CLI=/path/to/cli in ~/.infermatrix-copilot/.env")
        return cli

    def auth_gap(self) -> str | None:
        """A one-line auth problem with its fix, or None when unknown/fine.
        Cheap enough for `strict_readiness` (one fast CLI status call at
        most); transports without a cheap check return None and let the run
        surface auth errors loudly."""
        return None

    def identity_probe(self, execution_profile, *, timeout_s=30):
        from .native_identity import probe
        return probe(execution_profile, timeout_s=timeout_s)

    # -- contract ------------------------------------------------------------
    # True only where the CLI itself stops a `complete(max_budget_usd=...)`
    # call from STARTING another API request once its spend reaches the
    # threshold. The request that crosses it still completes and is billed,
    # so this bounds the overshoot to one request; it is not a hard ceiling.
    # `max_tokens` is no spend bound at all for CLI transports.
    stops_at_spend: bool = False

    def require_capability(self, capability: str) -> None:
        """Reject an unsupported execution contract before launching a process."""
        if capability not in self.spec.capabilities:
            raise ValueError(f"{self.spec.id} does not support required capability {capability}")

    def validate_completion(self) -> None:
        """Preflight adapter-specific completion restrictions without dispatch."""

    def readonly_usage(self, events: list[dict]) -> dict | None:
        """Normalize structured-session accounting using the adapter decoder."""
        raise NotImplementedError("provider does not support structured-session usage")

    def validate_session(self, req: AgentSessionRequest):
        """Reject requested capabilities before a provider can dispatch work."""
        if req.bridge_spec_path is not None and "mcp_tools" not in self.spec.capabilities:
            raise ValueError(f"{self.spec.id} does not support the requested MCP tool bridge")
        if getattr(req, 'output_schema', None) is not None:
            if 'session_schema' not in self.spec.capabilities:
                raise ValueError(f'{self.spec.id} does not support structured agent sessions')
            import jsonschema
            jsonschema.Draft202012Validator.check_schema(req.output_schema)

    def run_session(self, req: AgentSessionRequest):
        """Run one delegated agent step; returns `agent_loop.AgentOutcome`."""
        raise NotImplementedError

    def complete(self, *, system: str, messages: list[dict],
                 model: str = "", max_tokens: int | None = None,
                 role: str = "", effort: str = "",
                 max_budget_usd: float | None = None):
        """One-shot tool-less completion; returns a normalized `llm.Reply`.

        `effort` is a reasoning-effort pin (`low`/`medium`/`high`/`xhigh`) for
        transports whose CLI takes one (codex); the others accept and ignore
        it, because their model id already fixes the reasoning budget.

        `max_budget_usd` is a stop threshold where the CLI honours one
        (`stops_at_spend`): no further API request starts once the call's
        spend reaches it, but the request that crosses it is still billed. A
        caller that needs a hard ceiling reserves the threshold PLUS one
        request's worst case. Transports without it accept and ignore the
        argument, so callers must check `stops_at_spend` first (the knowledge
        service's gateway refuses them). The call's cost, when the transport
        reports one, is `Reply.usage["cost_usd"]`."""
        raise NotImplementedError


def flatten_messages(system: str, messages: list[dict]) -> str:
    """Render an internal block-protocol conversation as one prompt string for
    harnesses without a messages channel. Tool-less conversations only (the
    caller guarantees it): content is either a string or text blocks."""
    parts = [system] if system else []
    for message in messages:
        role = str(message.get("role", "user")).upper()
        content = message.get("content", "")
        if isinstance(content, str):
            text = content
        else:
            text = "\n".join(str(b.get("text", "")) for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
        if text:
            parts.append(f"[{role}]\n{text}")
    return "\n\n".join(parts)
