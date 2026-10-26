"""Bounded read-only JSON sessions shared by review and knowledge workflows.

The caller owns its schema and evidence policy. This transport owns command
permissions, credential isolation, process lifetime and one formatting repair.
"""
from __future__ import annotations

import json
import math
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from ..budgeting import call_budget
from ..execution_environment import readonly_environment
from .base import json_events, native_budget_facts, run_cli, stream_cli


class JSONSessionError(RuntimeError):
    def __init__(self, message, *, failure_class="backend", timeout=False, returncode=None):
        super().__init__(message)
        self.failure_class = failure_class
        self.timeout, self.returncode = timeout, returncode



def decode_object(text):
    from ..structured_output import decode_json_object

    return decode_json_object(text)


def _require_successful_events(adapter, events, provider):
    """A successful process exit cannot override a native failure event."""
    try:
        adapter.validate_readonly_completion(events)
    except (ValueError, RuntimeError) as exc:
        raise JSONSessionError(f"{provider} reported failure: {exc}", failure_class="invalid_result") from exc


@contextmanager
def _readonly_budget(prompt, *, provider, adapter, model):
    """One preparation and accounting boundary for either native output mode."""
    request = {"kind": "harness_session", "provider_id": provider, "model": model,
               "system": "", "messages": [{"role": "user", "content": prompt}],
               "max_tokens": 8192}
    with call_budget(request) as budget:
        events = []
        try:
            if budget.prepare(native_budget_facts(adapter, session=True, provider_id=provider)):
                raise ValueError('native read-only sessions cannot apply completion budget arguments')
            yield budget, events
        finally:
            # Public metadata retains its native shape; account owners receive
            # the canonical aggregate on success, refusal and partial failure.
            budget["usage"] = adapter.readonly_usage(events)


def _invoke(command, prompt, *, cwd, provider, adapter, model, idle, absolute, event_sink):
    """Stream one invocation; reserve before dispatch and settle even on failure."""
    with _readonly_budget(prompt, provider=provider, adapter=adapter, model=model) as (budget, events):
        result = {"text": None, "session_id": None, "served_model": None, "usage": None}
        def observe(channel, text):
            if channel != "stdout":
                return
            for event in json_events(text):
                events.append(event)
                decoded = adapter.decode_event(event)
                result["session_id"] = decoded.session_id or result["session_id"]
                result["served_model"] = decoded.served_model or result["served_model"]
                if decoded.usage:
                    result["usage"] = decoded.usage
                if decoded.text is not None and not decoded.error:
                    result["text"] = decoded.text
                if event_sink:
                    event_sink(event)
        try:
            budget["sent"] = True
            stdout, stderr, code, timeout = stream_cli(command, cwd=cwd, env=readonly_environment(),
                deadline=absolute, input=prompt, idle_timeout_s=idle,
                on_line=observe, termination_grace_s=5)
        except OSError as exc:
            raise JSONSessionError(f"{provider} could not run: {exc}") from exc
        if timeout:
            raise JSONSessionError(f"{provider} exceeded maximum runtime" if timeout == "absolute" else
                                   f"{provider} idle timeout after {idle:g} seconds without stdout activity", timeout=True)
        if code:
            failures = [adapter.decode_event(event).error for event in json_events(stdout)]
            detail = next((item for item in reversed(failures) if item), None)
            raise JSONSessionError(f"{provider} exited with {code}: {(detail or stderr or stdout)[-4000:]}", returncode=code)
        _require_successful_events(adapter, events, provider)
        result["stdout"] = stdout
        budget.update(outcome="completed")
        return result


def _buffered(command, prompt, *, cwd, provider, adapter, model, idle, absolute, event_sink):
    with _readonly_budget(prompt, provider=provider, adapter=adapter, model=model) as (budget, events):
        budget["sent"] = True
        try:
            stdout, stderr, code, timed_out = run_cli(command, input=prompt, cwd=cwd,
                env=readonly_environment(), timeout_s=min(idle, max(0.001, absolute - time.monotonic())))
        except OSError as exc:
            raise JSONSessionError(f"{provider} could not run: {exc}") from exc
        # Failed processes can still have emitted trustworthy usage. Decode
        # it before checking their exit status or successful-result schema.
        try:
            events[:] = adapter.readonly_events(stdout, validate=False)
        except ValueError:
            # A truncated envelope may still contain complete JSONL events.
            events[:] = json_events(stdout)
        if timed_out:
            raise JSONSessionError(f"{provider} could not run: timed out", timeout=True)
        if code:
            raise JSONSessionError(f"{provider} exited with {code}: {(stderr or stdout)[-2000:]}", returncode=code)
        try:
            events[:] = adapter.readonly_events(stdout)
        except ValueError as exc:
            raise JSONSessionError(f"{provider} returned invalid envelope: {exc}", failure_class="invalid_result") from exc
        _require_successful_events(adapter, events, provider)
        result = {"text": None, "session_id": None, "served_model": None, "usage": None, "stdout": stdout}
        for event in events:
            decoded = adapter.decode_event(event)
            for key in ("text", "session_id", "served_model", "usage"):
                value = getattr(decoded, key)
                if value is not None:
                    result[key] = value
            if event_sink:
                event_sink(event)
        budget.update(outcome="completed")
        return result


def run_readonly_json(prompt, *, cwd, command, provider, model="", output_schema,
                      idle_timeout_s, absolute_timeout_s, wrap_up_after_s=None,
                      event_sink=None, result_validator=None, allow_sessionless_retry=True,
                      output_format="stream-json", repair_prompt=None, skip_git_repo_check=False, max_repair_attempts=1,
                      execution_config=None, execution_root=None, frozen_inputs=None, effort='', settings=None,
                      session_request=None):
    """Return parsed payload plus session metadata; no business policy is inferred.

    The validator returns an error string or None. Cursor repairs once in the
    same session; when no session identity exists it may retry the whole request
    once. Both consume the same repair allowance and original absolute deadline.
    Codex uses its native output schema and never silently changes profile.
    """
    if type(max_repair_attempts) is not int or max_repair_attempts not in (0, 1):
        raise ValueError("max_repair_attempts must be 0 or 1")
    from .descriptors import provider_descriptor
    from .registry import transport_for_id

    if "direct" not in provider_descriptor(provider).execution_modes or not command:
        raise ValueError("a supported read-only JSON command is required")
    adapter = transport_for_id(SimpleNamespace(strict_backend_cli="", strict_backend_model=model), provider)
    if (not all(type(value) in (int, float) and math.isfinite(value)
                for value in (idle_timeout_s, absolute_timeout_s))
            or idle_timeout_s <= 0 or absolute_timeout_s < idle_timeout_s):
        raise ValueError("maximum runtime must be at least the positive idle timeout")
    if wrap_up_after_s is not None and not 0 < wrap_up_after_s < absolute_timeout_s:
        raise ValueError("wrap-up time must be positive and below maximum runtime")
    if output_format not in {"stream-json", "json"} or (output_format == "json" and wrap_up_after_s is not None):
        raise ValueError("buffered JSON sessions cannot use streaming wrap-up")
    if execution_config is not None:
        if execution_root is None or wrap_up_after_s is not None:
            raise ValueError('sandbox readonly execution requires durable state and no unsupported wrap-up')
        if session_request is not None and Path(session_request.scope.root).resolve() != Path(cwd).resolve():
            raise ValueError('structured session source differs from the readonly cwd')
        from .readonly_execution import execute_readonly
        schema = output_schema if isinstance(output_schema, dict) else json.loads(Path(output_schema).read_text())
        try:
            return execute_readonly(prompt, provider=provider, model=model, schema=schema,
                config=execution_config, root=execution_root, idle=idle_timeout_s, maximum=absolute_timeout_s,
                effort=effort, frozen_inputs=frozen_inputs, validator=result_validator, event_sink=event_sink,
                settings=settings, session_request=session_request)
        except TimeoutError as exc:
            from ..budgeting import copy_native_outcome
            problem = JSONSessionError('sandbox readonly idle timeout' if getattr(exc, 'reason', '') == 'idle'
                                       else 'sandbox readonly exceeded maximum runtime', timeout=True)
            raise copy_native_outcome(problem, exc) from exc
    if execution_root is not None or frozen_inputs is not None or session_request is not None:
        raise ValueError('sandbox inputs require a trusted execution profile')
    command = [shutil.which(command[0]) or command[0], *command[1:]]
    cwd = Path(cwd).resolve()
    schema = output_schema if isinstance(output_schema, dict) else json.loads(Path(output_schema).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="copilot-json-") as temp:
        schema_path, output = Path(temp) / "schema.json", Path(temp) / "last-message.json"
        from .schema_projection import schema_json
        schema_path.write_text(schema_json(schema, provider=provider, ensure_ascii=False), encoding="utf-8")
        if adapter.json_output_file:
            argv = adapter.readonly_json_command(command, cwd=cwd, model=model,
                schema_path=schema_path, output_path=output, skip_git_repo_check=skip_git_repo_check)
            transport = _buffered if output_format == "json" else _invoke
            result = transport(argv, prompt, cwd=cwd, provider=provider, adapter=adapter, model=model,
                             idle=idle_timeout_s, absolute=time.monotonic() + absolute_timeout_s,
                             event_sink=event_sink)
            if not output.is_file():
                raise JSONSessionError("Codex did not write --output-last-message")
            result["text"] = output.read_text(encoding="utf-8")
        else:
            prompt += "\nReturn exactly one JSON object matching this schema:\n" + json.dumps(schema, ensure_ascii=False)
            absolute = time.monotonic() + absolute_timeout_s
            def invoke(text, session=None, deadline=absolute, idle=idle_timeout_s):
                deadline = min(deadline, absolute)
                if time.monotonic() >= deadline:
                    raise JSONSessionError(f"{provider} exceeded maximum runtime", timeout=True)
                argv = adapter.readonly_json_command(command, cwd=cwd, model=model,
                    output_format=output_format, session_id=session)
                transport = _buffered if output_format == "json" else _invoke
                return transport(argv, text, cwd=cwd, provider=provider, adapter=adapter, model=model,
                               idle=idle, absolute=deadline, event_sink=event_sink)
            # A wrap-up is a same-session continuation, bounded by the original
            # absolute deadline. It is never permission to start a new session.
            latest = {}
            def observe(event):
                latest["session_id"] = adapter.decode_event(event).session_id or latest.get("session_id")
                if original_sink:
                    original_sink(event)
            original_sink = event_sink
            event_sink = observe if wrap_up_after_s is not None else original_sink
            def generate():
                latest.clear()
                if wrap_up_after_s is None:
                    return invoke(prompt)
                try:
                    return invoke(prompt, deadline=min(absolute, time.monotonic() + wrap_up_after_s))
                except JSONSessionError as exc:
                    if not exc.timeout or "exceeded maximum runtime" not in str(exc):
                        raise
                    if not latest.get("session_id"):
                        raise JSONSessionError("Cursor reached wrap-up time without a session id") from exc
                    if event_sink:
                        event_sink({"type": "session.wrap_up"})
                    return invoke("Stop all exploration and tool calls now. Use only the evidence already gathered. "
                                    "Immediately return the final JSON object matching the original schema; no commentary.",
                                    latest["session_id"])
            result = generate()
            if result["text"] is None:
                raise JSONSessionError("Cursor did not return a successful result")
            try:
                result["payload"] = decode_object(result["text"])
                reason = result_validator(result["payload"]) if result_validator else None
            except (AttributeError, TypeError, ValueError) as exc:
                reason = str(exc)
            if reason:
                if max_repair_attempts == 0:
                    raise JSONSessionError("Cursor returned invalid JSON: " + reason, failure_class="invalid_result")
                if result.get("session_id"):
                    previous = result
                    if event_sink:
                        event_sink({"type": "session.repair", "reason": reason})
                    correction = repair_prompt or (
                        "Your previous final response did not match the required review "
                        f"result contract ({reason}). Do not inspect the "
                        "repository or perform any more review work. Preserve the same "
                        "findings, reviewed head SHA, review checks, and conclusions. "
                        "Correct only the invalid formatting and immediately return "
                        "exactly one JSON object matching the schema and summary contract "
                        "in the original request. Use an empty findings list if no "
                        "actionable finding was proven. Output no Markdown or commentary "
                        "outside the JSON object.")
                    result = invoke(correction,
                                    result["session_id"], time.monotonic() + min(idle_timeout_s, 120.0),
                                    min(idle_timeout_s, 120.0))
                    result["served_model"] = result["served_model"] or previous["served_model"]
                    result["session_id"] = result["session_id"] or previous["session_id"]
                    if event_sink:
                        event_sink({"type": "session.repaired"})
                elif allow_sessionless_retry:
                    if event_sink:
                        event_sink({"type": "session.retry"})
                    result = generate()
                else:
                    raise JSONSessionError("Cursor returned invalid JSON: " + reason, failure_class="invalid_result")
        try:
            result["payload"] = decode_object(result["text"])
            reason = result_validator(result["payload"]) if result_validator else None
            if reason:
                raise ValueError(reason)
        except (AttributeError, TypeError, ValueError) as exc:
            raise JSONSessionError(f"{provider.title()} returned invalid JSON: {exc}", failure_class="invalid_result") from exc
        return result
