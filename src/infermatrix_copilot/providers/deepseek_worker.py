"""One SDK turn inside the process supervisor's isolated child environment.

The parent owns deadlines and descendant cleanup. This worker only adapts the
vendor SDK protocol and step-count policy; it never supervises processes.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import json
import sys


def validate_sdk():
    from dataclasses import fields
    from deepseek_harness import DeepSeekHarnessConfig

    if not {"cordis", "session_root"} <= {field.name for field in fields(DeepSeekHarnessConfig)}:
        raise RuntimeError("DeepSeek SDK lacks pinned composition support; install infermatrix-copilot[deepseek]")


def run_request(request, event_sink=None):
    from deepseek_harness import DeepSeekHarness

    validate_sdk()
    options = request["options"]
    # The supported SDK accepts the pinned composition and session directory.
    # Newer incompatible SDKs must fail construction before starting a runtime;
    # silently dropping these options would remove sandbox/tool policy.
    harness = DeepSeekHarness(**options)
    steps, stopped = 0, ""

    def notification(note):
        nonlocal steps, stopped
        # SDK Notification is a slots dataclass: it has no __dict__. Its
        # session.event envelope wraps the native event under payload.event.
        value = note if isinstance(note, dict) else {
            "method": getattr(note, "method", ""), "payload": getattr(note, "payload", {})}
        if value.get("method") == "session.event":
            payload = value.get("payload")
            value = payload.get("event", {}) if isinstance(payload, dict) else {}
        if not isinstance(value, dict):
            return
        if event_sink is not None:
            event_sink(value)
        kind = str(value.get("kind") or value.get("type") or "")
        if kind.endswith("step/end"):
            steps += 1
            if request.get("step_cap") and steps >= request["step_cap"] and not stopped:
                stopped = f"step cap {request['step_cap']} reached"
                harness.close()

    try:
        with harness:
            result = harness.run(request["prompt"], on_notification=notification)
    except Exception:
        if not stopped:
            raise
        result = None
    return {"final_response": str(getattr(result, "final_response", "") or ""),
            "finish_reason": str(getattr(result, "finish_reason", "") or ""),
            "events": list(getattr(result, "events", None) or []), "error": stopped}


def main():
    output = sys.stdout

    def emit(value):
        output.write(json.dumps({"protocol": 1, **value}) + "\n")
        output.flush()

    try:
        request = json.load(sys.stdin)
        # SDK progress output cannot corrupt the result protocol.
        with redirect_stdout(sys.stderr):
            result = run_request(request, lambda event: emit({"event": event}))
        response = {"result": result}
    except Exception as exc:
        # Exception messages can contain supplied credentials or prompts.
        response = {"failure": type(exc).__name__}
    emit(response)


if __name__ == "__main__":
    main()
