"""Real worker processes, with only the vendor SDK replaced by a fixture."""
import os
from pathlib import Path
import sys
import time

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.providers import deepseek


def worker(tmp_path, monkeypatch, source):
    (tmp_path / "deepseek_harness.py").write_text(
        "from dataclasses import dataclass\n@dataclass\nclass DeepSeekHarnessConfig:\n"
        "    cordis: str = ''\n    session_root: str = ''\n" + source)
    stream = deepseek.stream_cli

    def launch(cmd, **kwargs):
        # Only the fake vendor module comes from this directory. The real
        # installed/editable Copilot worker and supervisor execute unchanged.
        script = (f"import sys,runpy; sys.path.insert(0, {str(tmp_path)!r}); "
                  "runpy.run_module('infermatrix_copilot.providers.deepseek_worker', run_name='__main__')")
        return stream([sys.executable, "-I", "-c", script], **kwargs)

    monkeypatch.setattr(deepseek, "stream_cli", launch)
    return deepseek.DeepSeekHarnessTransport(Settings(_env_file=None))


def invoke(transport, tmp_path, *, timeout=3):
    return transport._invoke(options={"cwd": str(tmp_path), "api_key": "fixture-key"},
                             env=deepseek.sanitized_env(), prompt="fixture", timeout_s=timeout)


def test_worker_inheritance_is_filtered_and_sdk_output_cannot_corrupt_protocol(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-inherit")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-inherit")
    transport = worker(tmp_path, monkeypatch, '''
import os
from types import SimpleNamespace
class DeepSeekHarness:
    def __init__(self, **options):
        assert "GITHUB_TOKEN" not in os.environ
        assert "ANTHROPIC_API_KEY" not in os.environ
        assert options["api_key"] == "fixture-key"
        print("SDK setup progress")
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def run(self, prompt, **kwargs):
        print("SDK turn progress")
        return SimpleNamespace(final_response="done", finish_reason="completed", events=[])
''')
    result = invoke(transport, tmp_path)
    assert result.final_response == "done" and not result.error


@pytest.mark.skipif(os.name != "posix", reason="/proc descendant assertion is POSIX-only")
@pytest.mark.parametrize("phase", ["startup", "turn", "shutdown"])
def test_worker_deadline_covers_sdk_lifecycle_and_descendants(tmp_path, monkeypatch, phase):
    transport = worker(tmp_path, monkeypatch, f'''
import subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace
def hang():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path("child.pid").write_text(str(child.pid))
    time.sleep(60)
class DeepSeekHarness:
    def __init__(self, **options): pass
    def __enter__(self):
        if {phase!r} == "startup": hang()
        return self
    def __exit__(self, *args):
        if {phase!r} == "shutdown": hang()
    def run(self, prompt, **kwargs):
        if {phase!r} == "turn": hang()
        return SimpleNamespace(final_response="done", finish_reason="completed", events=[])
''')
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="absolute"):
        invoke(transport, tmp_path, timeout=1)
    assert time.monotonic() - started < 4
    pid = int((tmp_path / "child.pid").read_text())
    status = Path(f"/proc/{pid}/stat")
    for _ in range(100):
        if not status.exists() or status.read_text().split()[2] == "Z":
            break
        time.sleep(.01)
    else:
        pytest.fail("SDK descendant survived supervisor cleanup")


def test_worker_failure_does_not_disclose_prompt_or_credential(tmp_path, monkeypatch):
    transport = worker(tmp_path, monkeypatch, '''
class DeepSeekHarness:
    def __init__(self, **options):
        raise TypeError(options["api_key"])
''')
    with pytest.raises(RuntimeError, match="TypeError") as error:
        invoke(transport, tmp_path)
    assert "fixture-key" not in str(error.value)


def test_partial_native_usage_reaches_parent_before_worker_deadline(tmp_path, monkeypatch):
    transport = worker(tmp_path, monkeypatch, '''
import time
class DeepSeekHarness:
    def __init__(self, **options): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def run(self, prompt, on_notification):
        on_notification({"method": "session.event", "payload": {"event": {
            "kind": "turn/end", "data": {"model": "served", "usage": {
                "input_tokens": 17, "output_tokens": 3, "cost_usd": .25}}}}})
        time.sleep(60)
''')
    events = []
    with pytest.raises(RuntimeError, match="absolute"):
        transport._invoke(options={"cwd": str(tmp_path)}, env=deepseek.sanitized_env(),
                          prompt="fixture", timeout_s=1, native_event_sink=events.append)
    assert transport.native_snapshot(events) == {
        "text": "", "served_model": "served",
        "usage": {"input_tokens": 17, "output_tokens": 3, "cost_usd": .25}}
    assert transport.native_snapshot([])["usage"] is None
    assert transport.native_snapshot([{"type": "native.deepseek.event", "payload": {
        "kind": "turn/end", "data": {"usage": {"cost_usd": .5}}}}])["usage"] == {"cost_usd": .5}
