"""Codex CLI harness transport — Strict on a ChatGPT subscription (M3).

``codex exec --json`` emits JSONL events; the final agent message is the
session's answer and ``turn.completed`` events carry token usage. Probed on
codex-cli 0.145.0 (auth was absent on the dev machine, so unlike cursor/
claude-code this transport is exercised offline against recorded shapes —
the readiness path reports the login gap before any run starts).

Default tool-free completion is unavailable until native tool disabling is verified.
Read-only sandboxing controls shell execution, not all built-in or connected
tools. Reject that unsupported contract before dispatch. Agent sessions remain
separate: the MCP bridge is offered via configuration overrides, while broad
native reads remain a documented limitation of this backend class.

An explicitly supplied container completion profile uses the shared worker,
isolated environment and durable identity. It retains post-call tool auditing;
it is not a tool-free capability. Host sessions use `base.sanitized_env` and
Codex's native HOME authentication cache. Configured container calls may use a
filtered external-token snapshot of an existing native login; the original
host remains responsible for refresh and its cache is never mounted."""

from __future__ import annotations

import json
import hashlib
import math
import os
import subprocess
import sys
import tomllib
import threading
import time
from pathlib import Path

from ..agent_loop import AgentOutcome
from ..llm import Reply
from .base import (
    AgentSessionRequest,
    HarnessTransport,
    SessionUsage,
    SessionEvent,
    json_events,
    flatten_messages,
    run_cli,
    sanitized_env,
)
from .registry import PROVIDERS
from .codex_policy import EFFORTS as _EFFORTS, VERIFIED_VERSION as _CONFIGURATION_VERSION, validate_effort

_BRIDGE_SERVER = "infermatrix-tools"


class CodexTransport(HarnessTransport):
    """codex CLI (exec mode) as a Strict backend."""

    spec = PROVIDERS["codex"]
    supports_native_events = True
    json_output_file = True

    @staticmethod
    def validate_effort(value):
        validate_effort(value)

    def readonly_usage(self, events: list[dict]) -> dict | None:
        return dict(self._usage(events).reported_usage) or None

    @classmethod
    def validate_readonly_completion(cls, events: list[dict]) -> None:
        try:
            cls._require_completed(events)
        except RuntimeError:
            super().validate_readonly_completion(events)  # retain native error details for JSON callers
            raise

    @staticmethod
    def readonly_json_command(command, *, cwd, model, schema_path, output_path,
                              skip_git_repo_check=False, **_):
        return [*command, "exec", *(["--model", model] if model else []),
                "--ephemeral", "--ignore-user-config", "--strict-config",
                *(["--skip-git-repo-check"] if skip_git_repo_check else []),
                "--sandbox", "read-only", "--json", "--output-schema", str(schema_path),
                "--output-last-message", str(output_path), "-C", str(cwd), "-"]

    @staticmethod
    def _message_text(event):
        item = event.get('item')
        kind = str(item.get('item_type') or item.get('type') or '') if isinstance(item, dict) else ''
        return str(item['text']) if 'agent_message' in kind and item.get('text') else None

    @staticmethod
    def decode_event(event: dict) -> SessionEvent:
        text = CodexTransport._message_text(event)
        error = (event.get("message") or event.get("error")) if event.get("type") in {"error", "turn.failed"} else None
        return SessionEvent(
            text=text.strip() if text is not None else None,
            session_id=str(event["thread_id"]) if event.get("thread_id") else None,
            served_model=str(event["model"]) if event.get("model") else None,
            usage=event.get("usage") if isinstance(event.get("usage"), dict) else None,
            error=str(error.get("message", "")) if isinstance(error, dict) else str(error) if error else None)

    @property
    def subscription_billing(self) -> bool:
        """Only confirmed ChatGPT login on the default provider is uncapped.

        Authentication success alone also includes API-key logins. Unknown or
        custom provider settings never establish subscription billing.
        """
        # A host login cannot establish the account used by an isolated worker.
        # Profile admission must be bound to this prepared execution before use.
        configured = self._configured_subscription_billing()
        if configured is not None:
            return configured
        cli = self.cli_path()
        if not cli or os.environ.get("OPENAI_BASE_URL"):
            return False
        # Exec uses the sanitized environment, which intentionally drops
        # CODEX_HOME. Probe that same home and authentication context rather
        # than approving a different login selected by the parent environment.
        env = sanitized_env()
        config = Path(env.get("HOME") or Path.home()) / ".codex" / "config.toml"
        try:
            data = tomllib.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
            provider = data.get("model_provider", "openai")
            providers = data.get("model_providers", {})
            if (data.get("profile") or data.get("openai_base_url") or data.get("chatgpt_base_url")
                    or provider != "openai" or not isinstance(providers, dict) or providers.get(provider)):
                return False
            result = self.auth_probe([cli], env=env)
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        return self.subscription_auth_status(result.returncode, result.stdout + result.stderr)

    @staticmethod
    def subscription_auth_status(returncode, text):
        return returncode == 0 and 'Logged in using ChatGPT' in text

    def credential_snapshot(self, path, *, owner_uid=None, options=None):
        from .codex_credentials import login_snapshot
        return login_snapshot(path, owner_uid=owner_uid)

    def subscription_credential_identity(self, credentials):
        return (credentials.get('kind') == 'codex-chatgpt'
                and type(credentials.get('expires_at')) is int
                and credentials['expires_at'] > time.time())

    def auth_gap(self) -> str | None:
        cli = self.cli_path()
        if not cli:
            return None  # the CLI-missing gap is reported separately
        try:
            out = self.auth_probe([cli])
        except (OSError, subprocess.SubprocessError):
            return None  # status probe itself broken — let the run surface it
        return self.auth_status_issue(out.returncode, f"{out.stdout}\n{out.stderr}")

    def bridge_probe(self, execution_profile, request):
        """Validate the installed scoped bridge in the selected execution host."""
        from .session_inputs import BRIDGE, BRIDGE_PYTHON
        command = [BRIDGE_PYTHON, '-I', '-m', 'infermatrix_copilot.tool_bridge', '--spec', BRIDGE, '--check']
        deadline = request.absolute_deadline
        result = execution_profile.execute(command, cwd='/tmp/work', env={},
            deadline=deadline, frozen_inputs=request.frozen_inputs)
        if result.returncode or result.reason is not None:
            raise ValueError('sandbox bridge readiness control failed')
        proof = json.loads(result.stdout)
        if (not isinstance(proof, dict) or set(proof) != {'schema', 'tools'}
                or proof['schema'] != 'bridge-readiness/1' or not isinstance(proof['tools'], list)
                or any(not isinstance(tool, str) for tool in proof['tools'])
                or not set(request.scope.allowed_tools) | {'doc_read', 'doc_search'} <= set(proof['tools'])):
            raise ValueError('sandbox bridge capabilities are unavailable')
        return proof, execution_profile.sandbox.context()

    def configuration_probe(self, execution_profile, *, configuration_arguments=(),
                            frozen_inputs=None, timeout_s=15, include_requirements=False,
                            absolute_deadline=None):
        """Read native configuration without creating a model thread.

        The control helper is this adapter's protocol implementation inside the
        same registered sandbox/worker. It delegates all process ownership to
        the shared supervisor. It can only initialize and read configuration;
        model dispatch and account settlement are never part of this control.
        """
        if getattr(self, 'runner', None) is not None:
            raise ValueError('configuration control cannot inherit a host runner')
        if absolute_deadline is not None and (
                type(absolute_deadline) not in (int, float) or not math.isfinite(absolute_deadline)):
            raise ValueError('configuration control requires a finite absolute deadline')
        command, payload = _configuration_envelope(execution_profile, configuration_arguments, timeout_s, include_requirements)
        execution_profile.validate()
        # Frozen session inputs may take longer to transfer/stage than the RPC.
        # Keep the native RPC timeout in the payload; use the session's existing
        # deadline for the complete sandbox preparation and execution.
        result = execution_profile.execute(
            command, cwd='/tmp/work', env={}, input=payload, frozen_inputs=frozen_inputs,
            deadline=absolute_deadline if absolute_deadline is not None else time.monotonic() + timeout_s)
        return _configuration_proof(result, include_requirements)

    def get_configuration_result(self, execution_profile, *, configuration_arguments=(),
                                 frozen_inputs=None, timeout_s=15, recover=False, include_requirements=False):
        """Read the bound control result; never repeat native initialization."""
        command, payload = _configuration_envelope(execution_profile, configuration_arguments, timeout_s, include_requirements)
        return _configuration_proof(execution_profile.get_result(command, cwd='/tmp/work', env={},
            input=payload, frozen_inputs=frozen_inputs, recover=recover), include_requirements)

    @staticmethod
    def auth_status_issue(returncode: int, text: str) -> str | None:
        if returncode or "Not logged in" in text:
            return ("codex CLI is not logged in — run: codex login "
                    "(ChatGPT subscription auth)")
        return None

    # -- process plumbing ----------------------------------------------------
    def _mcp_overrides(self, spec_path: Path, *, contained=False) -> list[str]:
        """``-c`` config overrides wiring the tool bridge as an MCP server —
        config-only, so nothing is written into the session tree."""
        if contained:
            from .session_inputs import BRIDGE, BRIDGE_PYTHON, SOURCE
            if str(spec_path) != BRIDGE:
                raise ValueError('contained agent requires its mapped bridge spec')
            return ['-c', f'mcp_servers.{_BRIDGE_SERVER}.command={json.dumps(BRIDGE_PYTHON)}',
                    '-c', f'mcp_servers.{_BRIDGE_SERVER}.args=' + json.dumps([
                        '-I', '-m', 'infermatrix_copilot.tool_bridge', '--spec', BRIDGE]),
                    '-c', f'mcp_servers.{_BRIDGE_SERVER}.env=' +
                          '{GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="safe.directory", GIT_CONFIG_VALUE_0=' + json.dumps(SOURCE) + '}',
                    '-c', f'mcp_servers.{_BRIDGE_SERVER}.enabled=true']
        package_root = Path(__file__).resolve().parents[2]
        args = json.dumps(["-m", "infermatrix_copilot.tool_bridge",
                           "--spec", str(spec_path)])
        return [
            "-c", f'mcp_servers.{_BRIDGE_SERVER}.command="{sys.executable}"',
            "-c", f"mcp_servers.{_BRIDGE_SERVER}.args={args}",
            "-c", (f"mcp_servers.{_BRIDGE_SERVER}.env="
                   f'{{PYTHONPATH = "{package_root}"}}'),
        ]

    def _command(self, *, cwd, model='', mcp_spec=None, effort='', execution_profile=None):
        if execution_profile is not None:
            execution_profile.validate(for_dispatch=False)
            if cwd != '/tmp/work' or getattr(self, 'runner', None) is not None:
                raise ValueError('contained completion cannot inherit a host bridge, cwd or runner')
            cmd = [execution_profile.native_cli, 'exec', '--json', '--ephemeral',
                   '--ignore-user-config', '--strict-config',
                   '--dangerously-bypass-approvals-and-sandbox',
                   '-c', 'features.apps=false', '-c', 'web_search="disabled"',
                   '--skip-git-repo-check', '-C', cwd]
        else:
            cmd = [self.require_cli(), "exec", "--json", "-s", "read-only",
                   "--skip-git-repo-check", "-C", cwd]
        selected = model or self.settings.strict_backend_model
        if selected:
            cmd += ["-m", selected]
        if effort:
            validate_effort(effort)
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        policy = getattr(self, 'native_completion_policy', None)
        if policy is not None:
            if execution_profile is None:
                raise ValueError('native completion policy requires its original sandbox')
            self.validate_completion_request({'model': model, 'effort': effort})
            cmd += policy.arguments(model=selected, effort=effort)
        if mcp_spec is not None:
            cmd += self._mcp_overrides(mcp_spec, contained=execution_profile is not None)
        return cmd + ['-']

    def _run(self, text: str, *, cwd: str, timeout_s: float, model: str = "",
             mcp_spec: Path | None = None, effort: str = "", native_event_sink=None,
             execution_profile=None, frozen_inputs=None, output_schema=None,
             idle_timeout_s=None, absolute_deadline=None, readonly=False) -> tuple[list[dict], bool]:
        """One CLI invocation, with the prompt on stdin."""
        cmd = self._command(cwd=cwd, model=model, mcp_spec=mcp_spec, effort=effort,
                            execution_profile=execution_profile)
        if execution_profile is not None:
            from ..sandbox.inputs import merge_inputs
            inputs = merge_inputs(frozen_inputs,
                self.native_completion_policy.inputs() if getattr(self, 'native_completion_policy', None) else None)
            if output_schema is not None:
                from .schema_projection import SCHEMA_PATH, schema_inputs
                inputs = schema_inputs(inputs, output_schema, provider=self.spec.id)
                cmd[-1:-1] = ['--output-schema', SCHEMA_PATH]
            if frozen_inputs and any(name.startswith('/tmp/inputs/repository/') for name in frozen_inputs['files']):
                cmd[cmd.index('-C') + 1] = '/tmp/inputs/repository'
            execution_profile.validate()
            streamed = []
            def observe(channel, line):
                if channel == 'stdout' and native_event_sink is not None:
                    for event in json_events(line):
                        streamed.append(event)
                        native_event_sink({'type': 'native.codex.event', 'payload': event})
            options = {'frozen_inputs': inputs} if inputs is not None else {}
            if idle_timeout_s is not None:
                options.update(idle_timeout_s=idle_timeout_s, termination_grace_s=5)
            outcome = execution_profile.execute(cmd, cwd=cwd, env={},
                deadline=absolute_deadline if absolute_deadline is not None else time.monotonic() + timeout_s,
                input=text, on_line=observe if native_event_sink is not None else None, **options)
            self.native_process_outcome = outcome
            position = 0
            def finalize(frame):
                nonlocal position
                if position < len(streamed):
                    if streamed[position] != frame['payload']:
                        raise ValueError('native stream differs from its original terminal output')
                elif native_event_sink is not None:
                    native_event_sink(frame)
                position += 1
            return self._native_events(outcome.stdout, returncode=outcome.returncode,
                timed_out=outcome.reason is not None, native_event_sink=finalize,
                require_success=True), outcome.reason is not None
        if frozen_inputs is not None or output_schema is not None or idle_timeout_s is not None:
            raise ValueError('structured execution controls require their original sandbox')
        stdout, _, returncode, timed_out = run_cli(cmd, input=text, cwd=cwd,
            env=sanitized_env(), timeout_s=timeout_s, runner=getattr(self, 'runner', None))
        return self._native_events(stdout, returncode=returncode, timed_out=timed_out,
            native_event_sink=native_event_sink, require_success=True), timed_out

    @staticmethod
    def _native_events(stdout, *, returncode, timed_out, native_event_sink=None, require_success=False):
        events = json_events(stdout)
        if native_event_sink is not None:
            for event in events:
                native_event_sink({"type": "native.codex.event", "payload": event})
        if require_success and not timed_out and returncode != 0:
            raise RuntimeError(f'codex command exited {returncode}')
        return events

    @classmethod
    def _require_completed(cls, events):
        """One terminal-result contract for sessions and contained completion."""
        if any(event.get('type') in ('error', 'turn.failed') or cls.decode_event(event).error
               for event in events):
            raise RuntimeError('codex execution errored')
        terminal = [event.get('type') for event in events
                    if event.get('type') in ('turn.started', 'turn.completed', 'turn.failed')]
        if not terminal or terminal[-1] != 'turn.completed':
            raise RuntimeError('codex execution has no terminal result')

    @staticmethod
    def _final_text(events: list[dict]) -> str:
        """The last agent message. Shape is version-dependent — accept both
        the item.completed/agent_message form and any event carrying an
        agent message text field."""
        return CodexTransport.output_text(events).strip()

    @staticmethod
    def output_text(events):
        """Native last-message rendering; keep whitespace for file consumers."""
        last = ''
        for event in events:
            text = CodexTransport._message_text(event)
            if text is not None:
                last = text
        return last

    def process_receipt(self, outcome):
        """One raw outcome/usage/terminal projection for translating hosts."""
        events = self.readonly_events(outcome['stdout'], validate=False)
        decoded = [self.decode_event(event) for event in events]
        try:
            self.validate_readonly_completion(events)
            completed = True
        except (RuntimeError, ValueError):
            completed = False
        return {**outcome,
            'usage': next((event.usage for event in reversed(decoded) if event.usage is not None), None),
            'accounting_usage': self.readonly_usage(events),
            'native_complete': outcome['returncode'] == 0 and outcome['reason'] is None and completed,
            'terminal_failure': self._terminal_failure(events, outcome.get('returncode'), outcome.get('reason')),
            'reply_text': self._final_text(events), 'output_text': self.output_text(events)}

    @staticmethod
    def _terminal_failure(events, returncode, reason):
        return (type(returncode) is int and returncode > 0 and reason is None
            and bool(events) and events[-1].get('type') == 'turn.failed' and bool(events[-1].get('error'))
            and not any(event.get('type') == 'turn.completed' for event in events))

    @classmethod
    def terminal_failure(cls, outcome):
        return cls._terminal_failure(cls.readonly_events(outcome.get('stdout', ''), validate=False),
                                     outcome.get('returncode'), outcome.get('reason'))

    @staticmethod
    def _usage(events: list[dict]) -> SessionUsage:
        usage = SessionUsage()
        for event in events:
            decoded = CodexTransport.decode_event(event)
            raw = decoded.usage
            if isinstance(raw, dict):
                names = {"input_tokens": "input_tokens", "output_tokens": "output_tokens",
                         "cached_input_tokens": "cache_read_input_tokens",
                         "cache_write_input_tokens": "cache_creation_input_tokens",
                         "reasoning_output_tokens": "reasoning_output_tokens"}
                usage.add_token_usage({target: raw[source] for source, target in names.items()
                                       if source in raw})
            elif event.get("type") == "turn.completed":
                usage.add_token_usage({})
            if decoded.served_model:
                usage.served_model = decoded.served_model
        if usage.reported_usage is None:
            usage.reported_usage = {}
        if "input_tokens" in usage.reported_usage:
            usage.reported_usage["input_tokens_include_cache"] = True
        return usage

    @classmethod
    def native_snapshot(cls, events: list[dict]) -> dict:
        """Recover reported usage even when the application rejects the reply."""
        native = [event["payload"] for event in events
                  if event.get("type") == "native.codex.event"
                  and isinstance(event.get("payload"), dict)]
        usage = cls._usage(native)
        return {"usage": dict(usage.reported_usage or {}),
                "served_model": usage.served_model, "text": cls._final_text(native)}

    @staticmethod
    def _tool_activity(events: list[dict]) -> list[str]:
        """Names of non-message items the session completed (commands, MCP
        tool calls…) — codex's specific item types vary by version, so this
        is a best-effort activity log, not an audit (the sandbox is the
        enforcement layer)."""
        used: list[str] = []
        for event in events:
            item = event.get("item")
            if isinstance(item, dict):
                kind = str(item.get("item_type") or item.get("type") or "")
                if kind and "agent_message" not in kind \
                        and "reasoning" not in kind:
                    used.append(kind)
        return used

    # -- transport contract --------------------------------------------------
    def validate_session(self, req: AgentSessionRequest):
        super().validate_session(req)
        if getattr(self, 'completion_profile', None) is not None:
            raise ValueError('completion execution profile does not support agent sessions')
        profile = getattr(self, 'session_profile', None)
        if profile is not None:
            from ..sandbox import SandboxCompletionProfile
            from .session_inputs import BRIDGE, SOURCE
            if (not isinstance(profile, SandboxCompletionProfile) or getattr(self, 'runner', None) is not None
                    or not req.scope.read_only or req.scope.root != SOURCE or not req.frozen_inputs
                    or req.bridge_spec_path is not None and str(req.bridge_spec_path) != BRIDGE):
                raise ValueError('agent session requires its prepared sandbox scope and inputs')
            self.validate_completion_request({'model': req.model, 'effort': req.effort})
            from .completion_profiles import validate_bound_native_policy
            bound = validate_bound_native_policy(self)
            if bound is not None:
                original, _ = bound
                if (original['schema'] != 'native-policy-request/3'
                        or original['session_inputs_sha256'] != hashlib.sha256(json.dumps(req.frozen_inputs, sort_keys=True).encode()).hexdigest()
                        or bool(original['allowed_mcp']) != (req.bridge_spec_path is not None)):
                    raise ValueError('agent request differs from its original native policy')
            profile.validate()
        elif (req.frozen_inputs is not None or req.idle_timeout_s is not None
              or req.absolute_deadline is not None or getattr(req, 'output_schema', None) is not None):
            raise ValueError('agent execution controls require their original sandbox')

    def run_session(self, req: AgentSessionRequest) -> AgentOutcome:
        self.validate_session(req)
        profile = getattr(self, 'session_profile', None)
        cwd = '/tmp/work' if profile is not None else str(req.scope.root or req.run_dir)
        controls = dict(execution_profile=profile, frozen_inputs=req.frozen_inputs,
                        idle_timeout_s=req.idle_timeout_s, absolute_deadline=req.absolute_deadline,
                        effort=req.effort) if profile is not None else {}
        if getattr(req, 'output_schema', None) is not None:
            controls['output_schema'] = req.output_schema
        events, timed_out = self._run(
            f"{req.system}\n\n{req.prompt}", cwd=cwd,
            timeout_s=req.timeout_s, model=req.model,
            mcp_spec=req.bridge_spec_path,
            **controls,
            **({"native_event_sink": req.native_event_sink} if req.native_event_sink is not None else {}))
        if not timed_out:
            self._require_completed(events)
        usage = self._usage(events)
        used = self._tool_activity(events)
        if req.trace is not None:
            req.trace.record(
                "harness_session", provider=self.spec.id, step=req.step_name,
                timed_out=timed_out, item_count=len(events),
                tool_items=len(used), served_model=usage.served_model)
        # Codex does not expose a turn budget/counter.
        return usage.outcome(self._final_text(events), tool_calls=len(used),
                             tools_used=used, truncated=timed_out)

    def validate_completion(self) -> None:
        if getattr(self, 'native_recovery_only', False):
            from .completion_profiles import CompletionPolicyError
            raise CompletionPolicyError('original result transport cannot dispatch another model call')
        from .completion_profiles import validate_bound_native_policy
        validate_bound_native_policy(self)
        profile = getattr(self, 'completion_profile', None)
        if profile is not None:
            from ..sandbox import SandboxCompletionProfile
            if not isinstance(profile, SandboxCompletionProfile):
                raise ValueError('unsupported Codex completion profile')
            if getattr(self, 'runner', None) is not None:
                raise ValueError('contained completion cannot inherit a host runner')
            profile.validate()
            return
        self.require_capability("builtin_tools_off")
        raise NotImplementedError("codex tool-free execution policy is not implemented")

    def validate_completion_request(self, request):
        from .completion_profiles import CompletionPolicyError, validate_bound_native_policy
        bound = validate_bound_native_policy(self)
        if bound is None:
            return
        original, _ = bound
        model = request.get('model') or self.settings.strict_backend_model
        effort = request.get('effort') or ''
        if model != original['model'] or effort != original['effort']:
            raise CompletionPolicyError('completion request differs from its original native policy')
        self.native_completion_policy.arguments(model=model, effort=effort)

    def complete(self, *, system: str, messages: list[dict],
                 model: str = "", max_tokens: int | None = None,
                 role: str = "", effort: str = "",
                 max_budget_usd: float | None = None, native_event_sink=None,
                 output_schema=None, frozen_inputs=None, idle_timeout_s=None, absolute_deadline=None, readonly=False) -> Reply:
        """Explicit contained completion; the default no-tools contract fails.

        Containment permits local tools inside the disposable container, but a
        verdict that used tools is still rejected. This does not advertise the
        registry's stronger builtin_tools_off capability.
        """
        self.validate_completion()
        self.validate_completion_request({'model': model, 'effort': effort})
        if max_budget_usd is not None:
            raise ValueError('codex cannot stop a call at a spend threshold')
        events, timed_out = self._run(flatten_messages(system, messages),
            cwd='/tmp/work', timeout_s=self.settings.strict_backend_timeout_s,
            model=model, effort=effort, native_event_sink=native_event_sink,
            execution_profile=self.completion_profile, frozen_inputs=frozen_inputs, output_schema=output_schema,
            idle_timeout_s=idle_timeout_s, absolute_deadline=absolute_deadline, readonly=readonly)
        return self._completion_reply(events, timed_out=timed_out, audit_tools=not readonly)

    def get_completion_result(self, *, system: str, messages: list[dict], model='', effort='',
                              recover=False) -> Reply:
        """Read the original contained result without dispatch or settlement.

        The caller retains its domain receipt/settlement authority. A missing
        process result is pending, not permission to restart the native call.
        Explicit recovery validates terminal logs from the original worker.
        """
        profile = getattr(self, 'completion_profile', None)
        if profile is None:
            raise ValueError('completion recovery requires the original container profile')
        cmd = self._command(cwd='/tmp/work', model=model, effort=effort, execution_profile=profile)
        result = profile.get_result(cmd, cwd='/tmp/work', env={},
                                    input=flatten_messages(system, messages), recover=recover,
                                    **({'frozen_inputs': self.native_completion_policy.inputs()}
                                       if getattr(self, 'native_completion_policy', None) is not None else {}))
        events = self._native_events(result.stdout, returncode=result.returncode,
            timed_out=result.reason is not None, require_success=True)
        return self._completion_reply(events, timed_out=result.reason is not None)

    def _completion_reply(self, events, *, timed_out, audit_tools=True):
        if timed_out:
            error = TimeoutError('codex contained completion exceeded its deadline')
            error.reason = getattr(getattr(self, 'native_process_outcome', None), 'reason', 'absolute')
            raise error
        from .audit import assert_tool_less

        if audit_tools:
            assert_tool_less(events)
        self._require_completed(events)
        return self._usage(events).reply(self._final_text(events), stop_reason='end_turn')


def _configuration_request(value):
    """The control endpoint accepts config flags, never a model RPC method."""
    fields = {'native_cli', 'expected_version', 'configuration_arguments', 'timeout_s'}
    if isinstance(value, dict) and value.get('include_requirements') is True:
        fields.add('include_requirements')
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('invalid Codex configuration control')
    arguments = value['configuration_arguments']
    if (not isinstance(value['native_cli'], str) or not value['native_cli'].startswith('/')
            or '\0' in value['native_cli'] or value['expected_version'] != _CONFIGURATION_VERSION
            or type(value['timeout_s']) not in (int, float)
            or not math.isfinite(value['timeout_s']) or not 0 < value['timeout_s'] <= 120
            or not isinstance(arguments, list) or len(arguments) % 2
            or any(not isinstance(arg, str) or '\0' in arg for arg in arguments)
            or any(arg not in ('-c', '--disable') for arg in arguments[::2])):
        raise ValueError('invalid Codex configuration control fields')
    return value


def _configuration_envelope(profile, arguments, timeout_s, include_requirements=False):
    from ..sandbox import SandboxCompletionProfile
    if not isinstance(profile, SandboxCompletionProfile):
        raise ValueError('configuration control requires a registered sandbox profile')
    if type(include_requirements) is not bool:
        raise ValueError('invalid native requirements control')
    request = _configuration_request({'native_cli': profile.native_cli,
        'expected_version': _CONFIGURATION_VERSION, 'configuration_arguments': list(arguments),
        'timeout_s': timeout_s - 2, **({'include_requirements': True} if include_requirements else {})})
    return (['/usr/local/bin/python', '-I', '-m', 'infermatrix_copilot.providers.codex',
             '--configuration-probe'], json.dumps(request))


def _configuration_proof(result, include_requirements=False):
    if result.reason is not None or result.returncode:
        raise ValueError('native configuration control did not complete')
    value = json.loads(result.stdout)
    if (not isinstance(value, dict) or value.get('schema') != 'codex-configuration/1'
            or value.get('version') != _CONFIGURATION_VERSION
            or value.get('methods_sent') != ['initialize', 'config/read', *(['configRequirements/read'] if include_requirements else [])]
            or not isinstance(value.get('effective'), dict) or not isinstance(value.get('layers'), list)):
        raise ValueError('invalid native configuration proof')
    return value


def _configuration_view(config):
    """Expose policy facts without commands, credentials or arbitrary config."""
    if not isinstance(config, dict):
        raise ValueError('invalid native configuration response')
    servers = config.get('mcp_servers') or {}
    if not isinstance(servers, dict):
        raise ValueError('invalid native MCP configuration')
    mcp = {}
    for name, server in servers.items():
        if not isinstance(server, dict) or type(server.get('enabled', True)) is not bool:
            raise ValueError('invalid native MCP enablement')
        mcp[name] = {'enabled': server.get('enabled', True)}
    features = config.get('features')
    if features is not None and (not isinstance(features, dict)
                                or any(value is not None and type(value) is not bool for value in features.values())):
        raise ValueError('invalid native feature configuration')
    view = {key: config.get(key) for key in ('model', 'model_provider', 'model_catalog_json',
                                          'model_reasoning_effort', 'web_search')}
    if any(value is not None and not isinstance(value, str) for value in view.values()):
        raise ValueError('invalid native model configuration')
    tools = config.get('tools') or {}
    if not isinstance(tools, dict):
        raise ValueError('invalid native tool configuration')
    enabled = {}
    for name in ('update_plan', 'experimental_request_user_input'):
        tool = tools.get(name)
        value = tool.get('enabled') if isinstance(tool, dict) else None
        if tool is not None and (not isinstance(tool, dict) or value is not None and type(value) is not bool):
            raise ValueError('invalid native tool enablement')
        enabled[name] = value
    return {**view, 'mcp_servers': mcp, 'features': features, 'tools': enabled,
            'observed_tool_keys': sorted(tools),
            'configured_startup_keys': [key for key in ('hooks', 'plugins', 'skills') if config.get(key)],
            'observed_keys': sorted(config),
            'config_sha256': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()}


def _configuration_control(request, *, env=None, cwd='/tmp/work'):
    """Adapter-owned finite RPC handshake, using only shared process ownership.

    EOF shuts the native app server down before its asynchronous replies. Keep
    stdin open through the two responses, then signal completion to the same
    supervisor. Deliberate control cleanup is recorded separately from a model
    outcome; this function cannot create a thread or start a turn.
    """
    from ..process_execution import ProcessInputChannel, run_process
    request = _configuration_request(request)
    env = dict(os.environ) if env is None else env
    deadline = time.monotonic() + request['timeout_s']
    version = CodexTransport(None).version_probe([request['native_cli']], env=env,
                                                timeout_s=deadline - time.monotonic())
    if version.returncode or version.stdout.strip() != request['expected_version']:
        raise ValueError('native configuration control requires the verified Codex version')
    channel, completed = ProcessInputChannel(), threading.Event()
    sent, responses = [], {}

    def send(method, params, identity):
        sent.append(method)
        channel.send(json.dumps({'id': identity, 'method': method, 'params': params}) + '\n')

    def started(pid, stopping):
        send('initialize', {'clientInfo': {'name': 'infermatrix-config-probe', 'version': '1'},
                            'capabilities': {'experimentalApi': True}}, 1)

    def line(source, text):
        if source != 'stdout':
            return
        for message in json_events(text):
            identity = message.get('id')
            if type(identity) is not int or identity not in (1, 2, 3):
                continue
            if identity in responses or 'error' in message or not isinstance(message.get('result'), dict):
                raise ValueError('native configuration handshake rejected')
            responses[identity] = message['result']
            if identity == 1:
                send('config/read', {'includeLayers': True}, 2)
            elif identity == 2 and request.get('include_requirements'):
                send('configRequirements/read', {}, 3)
            else:
                completed.set()

    result = run_process([request['native_cli'], 'app-server', '--strict-config',
                          *request['configuration_arguments']], cwd=cwd, env=env,
                         input_channel=channel, on_started=started, on_line=line,
                         completed=completed, completion_grace_s=0, deadline=deadline)
    methods = ['initialize', 'config/read', *(['configRequirements/read'] if request.get('include_requirements') else [])]
    if (sent != methods or len(methods) not in responses or result.reason not in (None, 'absolute')
            or result.reason is None and result.returncode):
        raise ValueError('native configuration response is incomplete')
    response = responses[2]
    layers = response.get('layers') or []
    if not isinstance(layers, list) or any(not isinstance(layer, dict) for layer in layers):
        raise ValueError('invalid native configuration layers')
    effective = _configuration_view(response.get('config'))
    active = [layer for layer in layers if layer.get('disabledReason', layer.get('disabled_reason')) is None]
    projected = [_configuration_view(layer.get('config', {})) for layer in active]
    # Native 0.162.1 emits layers high-to-low, but its public ApiConfig drops
    # these two tools. Recover only absent fields from active raw layers. An
    # explicit null remains unknown and cannot be replaced by a lower value.
    for name in effective['tools']:
        if name not in effective['observed_tool_keys']:
            for layer in projected:
                if name in layer['observed_tool_keys']:
                    effective['tools'][name] = layer['tools'][name]
                    break
    requirements = responses.get(3, {}).get('requirements')
    if request.get('include_requirements') and 'requirements' not in responses[3]:
        raise ValueError('native requirements response is incomplete')
    from .codex_policy import requirements_view
    return {'schema': 'codex-configuration/1', 'version': version.stdout.strip(),
            'methods_sent': sent, 'effective': effective, 'layers': projected,
            'requirements_checked': bool(request.get('include_requirements')),
            'requirements_empty': requirements is None,
            'requirements_sha256': hashlib.sha256(json.dumps(requirements, sort_keys=True).encode()).hexdigest(),
            **({'requirements': requirements_view(requirements)} if request.get('include_requirements') else {}),
            'layers_sha256': hashlib.sha256(json.dumps(layers, sort_keys=True).encode()).hexdigest(),
            'native_returncode': result.returncode, 'native_reason': result.reason,
            'stdout_sha256': hashlib.sha256(result.stdout.encode()).hexdigest(),
            'stderr_sha256': hashlib.sha256(result.stderr.encode()).hexdigest()}


def _configuration_main():
    if sys.argv[1:] == ['--identity-probe']:
        from .native_identity import main
        return main()
    if sys.argv[1:] != ['--configuration-probe']:
        raise SystemExit('expected --configuration-probe')
    try:
        result = _configuration_control(json.load(sys.stdin))
        print(json.dumps(result), flush=True)
        return 0
    except Exception as error:
        # Native/configuration diagnostics can contain credentials. Keep their
        # hashes in successful proofs and never interpolate them in failures.
        print('Codex configuration control rejected: ' + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(_configuration_main())
