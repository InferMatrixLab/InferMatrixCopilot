"""PR4b — adapter knowledge: prompt-render byte-parity goldens, the
request-shape golden, hooks loading, imx-omni-pytest, and the module-rebase
partial e2e (prompt build → gated loop → substate result)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from infermatrix_copilot.rebase_engine import module_pytest
from infermatrix_copilot.rebase_engine.hooks import (
    HooksError, RebaseHooks, load_hooks)
from infermatrix_copilot.rebase_engine.module_rebase import (
    ModuleRunConfig, rebase_module)
from infermatrix_copilot.rebase_engine.prompt_builder import (
    ModulePromptData, build_debug_prompt, build_module_prompt)
from infermatrix_copilot.rebase_engine.rebase_tools import (
    RebasePaths, build_rebase_tools, load_tool_schemas)
from infermatrix_copilot.rebase_engine.substate import Substate

REPO_ROOT = Path(__file__).resolve().parents[1]
REBASE_DATA = REPO_ROOT / "adapters/vllm_omni/rebase"
GOLDENS = Path(__file__).parent / "goldens"
# what the parent derived from its own file location when the goldens were
# captured — pinned so renders stay byte-comparable forever
PARENT_SCRIPT_DIR = "/data/zhoutaichang/copilot/vllm-omni-rebase-agent/agent"

GOLDEN_KWARGS = dict(
    vllm_path="/nonexistent/vllm-checkout",
    omni_path="/nonexistent/omni-checkout",
    script_dir=PARENT_SCRIPT_DIR,
    last_rebase_vllm_commit="abc123",
    log_dir="/fixed/logdir", signal_dir="/fixed/signals",
    rebase_run_id="run-golden",
    broken_imports=[{"omni_file": "x.py", "symbol": "gone",
                     "upstream_module": "vllm.old.mod",
                     "vllm_commits": [{"hash": "deadbee", "message": "moved it",
                                       "diff": "-old\n+new"}],
                     "call_sites": ["x.py:10"]}],
    module_test_plan={"ci_tests": ["slug_a"],
                      "upstream_changes": [{"type": "renamed",
                                            "path": "tests/a.py",
                                            "new_path": "tests/b.py"}]})


@pytest.fixture(scope="module")
def prompt_data() -> ModulePromptData:
    return ModulePromptData.load(REBASE_DATA)


# -- prompt goldens ------------------------------------------------------------

@pytest.mark.parametrize("module", ["model_config", "worker_runner"])
def test_module_prompt_matches_parent_golden(prompt_data, module):
    """Byte-identical render vs the stored golden. Prompt bytes are
    prompt-cache load-bearing.

    NOTE (2026-09-18): these goldens no longer record the PARENT builder's
    output. The parent agent had no access to the curated knowledge base, and
    the owner asked for the rebase pipeline to consult it, so the render now
    carries knowledge-base guidance in the ADAPTIVE_GUIDANCE slot and the tool
    surface gained doc_search/doc_read. The goldens were deliberately
    regenerated from THIS builder at that change. Historical parity templates
    are bundled here; no external checkout is needed to check their renders."""
    ours = build_module_prompt(module, prompt_data, **GOLDEN_KWARGS)
    golden = (GOLDENS / f"module_prompt_{module}.txt").read_text()
    assert ours == golden


def test_debug_prompt_matches_parent_golden():
    raw = yaml.safe_load((REBASE_DATA / "prompt_data.yaml").read_text())
    ours = build_debug_prompt("worker_runner", "Traceback: boom",
                              raw["debug_prompt_template"], "tests/t.py")
    assert ours == (GOLDENS / "debug_prompt.txt").read_text()


def test_live_debug_prompt_carries_rebase_attribution(prompt_data):
    live = build_debug_prompt(
        "worker_runner", "TypeError: RopeState",
        prompt_data.debug_prompt_template_live, "tests/worker_v2/test_state.py")
    assert "TypeError: RopeState" in live
    assert "failure signature" in live
    assert "main's last build predates a" in live
    assert "adjacent constructor, override, result-field" in live


def test_live_template_changes_are_bounded():
    """Live additions remain bounded against the bundled parity template."""
    live = (REBASE_DATA / "templates" / "module_rebase.live.prompt.tmpl")
    parity = (REBASE_DATA / "templates" / "module_rebase.prompt.tmpl")
    live_text = live.read_text()
    start = live_text.index("## Release transition and adjacent API checks")
    end = live_text.index("**Engine / unpack:**", start)
    assert "real upstream constructor" in live_text[start:end]
    without_live_guidance = live_text[:start] + live_text[end:]

    def stable_sections(text):
        for begin, finish in (("### Debug-memory workflow (mandatory)", "{BROKEN_IMPORTS_SECTION}"),
                              ("## Killing hung tests" if "## Killing hung tests" in text
                               else "## Hung tests", "## Completion contract")):
            a, b = text.index(begin), text.index(finish, text.index(begin))
            text = text[:a] + text[b:]
        return text.splitlines()

    old_lines = stable_sections(parity.read_text())
    new_lines = stable_sections(without_live_guidance)
    assert len(old_lines) == len(new_lines)
    diff = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
    assert len(diff) == 2
    assert all("imx-omni-pytest" in b and "run_module_pytest" in a
               for a, b in diff)


def test_live_prompt_uses_registered_tool_contracts(prompt_data):
    import jsonschema
    import re

    live = build_module_prompt("model_config", prompt_data, live=True, **GOLDEN_KWARGS)
    assert "lib/kill_test_tree.sh" not in live
    assert "python3 'Use `search_debug_memory`" not in live
    assert "memory/debug_memory.md" not in live
    definitions = {t["name"]: t for t in load_tool_schemas(REBASE_DATA / "tool_schemas.json")}
    examples = [json.loads(raw) for raw in re.findall(r"```json\n(.*?)\n\s*```", live, re.S)]
    assert len(examples) == 3
    for tool, example in zip(("search_debug_memory", "record_debug_memory", "run_pytest"), examples):
        assert f"`{tool}`" in live
        jsonschema.validate(example, definitions[tool]["input_schema"])
    assert examples[0]["module"] == examples[1]["module"] == "model_config"


def test_shipped_rebase_requires_no_external_agent(monkeypatch):
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.engine.steps.rebase_knowledge import _knowledge_layer_paths
    from infermatrix_copilot.rebase_engine.knowledge_attest import attest_layers

    monkeypatch.delenv("REBASE_AGENT_ROOT", raising=False)
    settings = Settings(_env_file=None)
    assert settings.rebase_agent_root is None
    assert "REBASE_AGENT_ROOT" not in settings.expansion_env()
    manifest = yaml.safe_load((REBASE_DATA.parent / "manifest.yaml").read_text())
    layers = _knowledge_layer_paths(manifest, settings)
    assert layers == {"parent_debug_db": "", "parent_skills_dir": "",
                      "parent_upstream_column": ""}
    assert not attest_layers(**layers)
    # Existing environment files may supply an explicit data import path;
    # an unavailable legacy root cannot add a shipped runtime dependency.
    configured = Settings(_env_file=None, rebase_agent_root="/missing/legacy")
    assert configured.expansion_env()["REBASE_AGENT_ROOT"] == "/missing/legacy"
    assert _knowledge_layer_paths(manifest, configured) == layers
    assert Settings(_env_file=None, rebase_agent_root="").rebase_agent_root is None


# -- request-shape golden ------------------------------------------------------

def test_request_shape_golden(prompt_data):
    """The first-turn request payload (model, max_tokens, tool schema order,
    message shape) serialized against a stored golden — the cache-parity
    contract Rev 8 §4 demands. The golden regenerates ONLY via a deliberate
    refresh (delete the file and rerun)."""
    from infermatrix_copilot.rebase_engine.agent_loop import GATED_TOOL_NAMES
    defs = load_tool_schemas(REBASE_DATA / "tool_schemas.json")
    prompt = build_module_prompt("model_config", prompt_data, **GOLDEN_KWARGS)
    gated = [t for t in defs if t["name"] not in GATED_TOOL_NAMES]
    payload = {"model": "golden-model", "max_tokens": 32000,
               "tools": gated,
               "messages": [{"role": "user", "content": prompt}]}
    serialized = json.dumps(payload, sort_keys=False, ensure_ascii=False,
                            indent=1)
    golden_file = GOLDENS / "request_shape_turn1.json"
    if not golden_file.exists():
        golden_file.write_text(serialized)
    assert serialized == golden_file.read_text()


def test_live_prompt_uses_shipped_wrapper_with_sound_quoting(prompt_data):
    """Live renders reference imx-omni-pytest (the retired shell wrapper does
    not exist here) and quote the import-check payload so bash preserves the
    inner quotes — both recorded divergences from the parent bytes."""
    import shlex
    live = build_module_prompt("model_config", prompt_data, live=True,
                               **GOLDEN_KWARGS)
    assert "run_module_pytest.sh" not in live
    assert "imx-omni-pytest -vv -s tests/entrypoints/test_stage_utils.py" in live
    # the rendered import-check line round-trips through shlex intact
    line = next(ln for ln in live.splitlines()
                if ln.startswith("imx-omni-pytest python -c "))
    parts = shlex.split(line)
    assert parts[:3] == ["imx-omni-pytest", "python", "-c"]
    assert "print('OK')" in parts[3]      # inner quotes SURVIVE
    # parity mode still reproduces the parent's (broken) quoting for goldens
    parity = build_module_prompt("model_config", prompt_data, **GOLDEN_KWARGS)
    assert "run_module_pytest.sh" in parity


def test_module_pytest_mutex_without_gpus_still_runs(tmp_path, monkeypatch):
    """IMX_GPU_MUTEX=1 on a GPU-less box: the lock SERIALIZES but must not
    become a hardware gate — a skip would be a false rc=0 pass."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("IMX_TARGET_REPO", str(repo))
    monkeypatch.setenv("IMX_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("IMX_GPU_MUTEX", "1")
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    rc = module_pytest.main(["python", "-c", "print('RAN')"])
    assert rc == 0
    log = next((tmp_path / "logs" / "tests").glob("*.log")).read_text()
    assert "RAN" in log                    # executed, not skipped
    # and a real failure's rc propagates (never masked by a skip)
    rc = module_pytest.main(["python", "-c", "raise SystemExit(7)"])
    assert rc == 7


def test_module_pytest_artifact_cleanup_and_watchdog_wiring(tmp_path,
                                                            monkeypatch):
    """Adapter artifact globs reach the runner (leaked root-level WAVs are
    removed) and watchdog learning/report callbacks are wired (a critical
    pattern kill records a decision and writes a report)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("IMX_TARGET_REPO", str(repo))
    monkeypatch.setenv("IMX_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("IMX_ADAPTER_REBASE",
                       str(REPO_ROOT / "adapters/vllm_omni/rebase"))
    monkeypatch.delenv("IMX_GPU_MUTEX", raising=False)
    rc = module_pytest.main([
        "python", "-c",
        "import pathlib; pathlib.Path('test_leak.wav').write_text('x')"])
    assert rc == 0
    assert not (repo / "test_leak.wav").exists()     # cleaned up
    # watchdog wiring: a critical line gets the run killed AND recorded
    monkeypatch.setenv("TEST_TIMEOUT_SEC", "30")
    rc = module_pytest.main([
        "python", "-c",
        "import time,sys; print('CUDA out of memory', flush=True); "
        "time.sleep(30)"])
    assert rc != 0
    reports = list((tmp_path / "logs" / "tests").glob("*.watchdog_report"))
    assert reports and "CUDA out of memory" in reports[0].read_text()
    # TIER-2 (review) pattern: with no LLM reviewer the documented
    # default-CONTINUE applies, and the decision IS recorded via the
    # learning callback — the signature contract that only tier 2 exercises
    rc = module_pytest.main([
        "python", "-c",
        "print('ValueError: suspicious but reviewable output')"])
    decisions = (tmp_path / "logs" / "watchdog_decisions.jsonl")
    assert decisions.is_file(), "tier-2 decision was not recorded"
    import json as _json
    entries = [_json.loads(l) for l in decisions.read_text().splitlines()]
    assert any(e.get("verdict") == "CONTINUE" for e in entries)


# -- hooks ---------------------------------------------------------------------

def test_hooks_load_declared_active_adapter():
    manifest = yaml.safe_load(
        (REPO_ROOT / "adapters/vllm_omni/manifest.yaml").read_text())
    hooks = load_hooks(REPO_ROOT / "adapters/vllm_omni", manifest)
    assert isinstance(hooks, RebaseHooks)
    assert hooks.adaptive_guidance("worker_runner") == ""


def test_hooks_fail_closed(tmp_path):
    # undeclared ⇒ neutral defaults
    assert isinstance(load_hooks(tmp_path, {"status": "active"}), RebaseHooks)
    # declared but missing ⇒ error, never silently stock
    with pytest.raises(HooksError, match="missing"):
        load_hooks(tmp_path, {"status": "active",
                              "rebase": {"hooks": "rebase/hooks.py"}})
    # declared on an inactive adapter ⇒ error
    with pytest.raises(HooksError, match="not active"):
        load_hooks(tmp_path, {"status": "draft",
                              "rebase": {"hooks": "rebase/hooks.py"}})
    # declared but exports no HOOKS instance ⇒ error
    f = tmp_path / "rebase" / "hooks.py"
    f.parent.mkdir(parents=True)
    f.write_text("x = 1\n")
    with pytest.raises(HooksError, match="must export HOOKS"):
        load_hooks(tmp_path, {"status": "active",
                              "rebase": {"hooks": "rebase/hooks.py"}})


def test_rebase_section_is_high_risk():
    from infermatrix_copilot.adapters.base import HIGH_RISK_SECTIONS
    assert "rebase" in HIGH_RISK_SECTIONS   # agents cannot self-install hooks


# -- imx-omni-pytest -----------------------------------------------------------

def test_module_pytest_wrapper(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("IMX_TARGET_REPO", str(repo))
    monkeypatch.setenv("IMX_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("IMX_GPU_MUTEX", raising=False)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    # command mode (the parent wrapper accepted `... python -c '...'`)
    rc = module_pytest.main(["python", "-c", "print('OK')"])
    assert rc == 0
    log = next((tmp_path / "logs" / "tests").glob("*.log")).read_text()
    assert "OK" in log
    # missing env fails loudly
    monkeypatch.delenv("IMX_TARGET_REPO")
    assert module_pytest.main(["tests/x"]) == 2


def test_module_pytest_gpu_gating_rules(monkeypatch):
    monkeypatch.delenv("IMX_GPU_MUTEX", raising=False)
    assert module_pytest._needs_gpu_lock(["tests/e2e/foo.py"]) is True
    assert module_pytest._needs_gpu_lock(["tests/unit/foo.py"]) is False
    monkeypatch.setenv("IMX_GPU_MUTEX", "1")
    assert module_pytest._needs_gpu_lock(["tests/unit/foo.py"]) is True


# -- module rebase partial e2e -------------------------------------------------

class FakeStream:
    def __init__(self, response, capture, kwargs):
        self._response = response
        capture.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            if False:  # pragma: no cover
                yield None
        return gen()

    async def get_final_message(self):
        return self._response


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        return FakeStream(self._responses.pop(0), self.requests, kwargs)


def _blk_text(text):
    return SimpleNamespace(type="text", text=text)


def _blk_tool(name, tid, tinput):
    return SimpleNamespace(type="tool_use", name=name, id=tid, input=tinput)


def _resp(blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


def test_module_rebase_partial_e2e(tmp_path, prompt_data):
    """One module end-to-end: adapter prompt (golden-parity builder) → gated
    loop → decision unlock → real dispatch edit → substate outcome, with the
    hooks observation called and a failure path marking the module failed."""
    omni = tmp_path / "omni"
    omni.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=omni, check=True)
    (omni / "mod.py").write_text("v1\n")
    log_dir = tmp_path / "logs"
    defs = load_tool_schemas(REBASE_DATA / "tool_schemas.json")
    tools = build_rebase_tools(defs, RebasePaths(omni_path=str(omni),
                                                 vllm_path=str(omni)))
    decision = log_dir / "plans" / "module-model_config" / "p.decision.md"
    client = FakeClient([
        _resp([_blk_tool("write_file", "t1",
                         {"file_path": str(decision), "content": "accept"})]),
        _resp([_blk_tool("edit_file", "t2",
                         {"file_path": str(omni / "mod.py"),
                          "old_string": "v1", "new_string": "v2"})]),
        _resp([_blk_text("module rebased")]),
    ])
    substate = Substate(tmp_path / "run", "run-1")
    observed = []

    class Hooks(RebaseHooks):
        def adaptive_guidance(self, module):
            return "GUIDANCE-BLOCK"

        def on_module_result(self, module, result):
            observed.append((module, result["status"]))

    config = ModuleRunConfig(vllm_path=str(omni), omni_path=str(omni),
                             script_dir="/parent", model="fake-model",
                             log_dir=str(log_dir))
    outcome = asyncio.run(rebase_module(
        "model_config", client=client, config=config,
        prompt_data=prompt_data, tool_defs=defs, extra_tools=tools,
        substate=substate, hooks=Hooks()))
    assert outcome["status"] == "done" and outcome["turns"] == 3
    assert (omni / "mod.py").read_text() == "v2\n"
    assert substate.get("modules.model_config.status") == "done"
    assert observed == [("model_config", "done")]
    # the hooks' guidance reached the prompt's {ADAPTIVE_GUIDANCE} slot
    prompt = client.requests[0]["messages"][0]["content"]
    assert "GUIDANCE-BLOCK" in prompt

    # parent-parity debug retries: an incomplete first run is retried with
    # the DEBUG prompt (built from the failure text) — and the plan gate
    # PERSISTS: the decision written in the initial run keeps edit tools
    # unlocked for the retry, which performs a REAL edit before completing
    # (the parent re-locked debug retries, leaving them unable to edit —
    # fixed, recorded divergence)
    (omni / "retryme.py").write_text("broken\n")
    decision2 = log_dir / "plans" / "module-platform" / "p.decision.md"
    retry_client = FakeClient([
        # initial attempt: passes the gate, then dies truncated
        _resp([_blk_tool("write_file", "r1",
                         {"file_path": str(decision2), "content": "accept"})]),
        _resp([_blk_text("half done, ran out of road")],
              stop_reason="max_tokens"),            # not done, gate PASSED
        # debug attempt 1: edits WITHOUT writing a new decision, completes
        _resp([_blk_tool("edit_file", "r2",
                         {"file_path": str(omni / "retryme.py"),
                          "old_string": "broken", "new_string": "fixed"})]),
        _resp([_blk_text("fixed it on the debug pass")]),
    ])
    outcome = asyncio.run(rebase_module(
        "platform", client=retry_client, config=config,
        prompt_data=prompt_data, tool_defs=defs, extra_tools=tools,
        substate=substate))
    assert outcome["status"] == "done" and outcome["debug_attempts"] == 1
    assert outcome["exit_code"] == 0
    assert (omni / "retryme.py").read_text() == "fixed\n"   # edit LANDED
    debug_req = retry_client.requests[2]["messages"][0]["content"]
    assert debug_req.startswith("## Debug: fix failing test for module")
    assert "half done, ran out of road" in debug_req
    assert "failure signature" in debug_req
    # the debug turn ADVERTISED the gated tools (gate persisted as passed)
    assert "edit_file" in [x["name"] for x in retry_client.requests[2]["tools"]]

    # a loop exception is substate data, never a raise
    class Boom:
        @property
        def messages(self):
            raise RuntimeError("client exploded")

    outcome = asyncio.run(rebase_module(
        "worker_runner", client=Boom(), config=config,
        prompt_data=prompt_data, tool_defs=defs, extra_tools=tools,
        substate=substate))
    assert outcome["status"] == "failed"
    assert outcome["debug_attempts"] == config.max_debug_retries
    assert substate.get("modules.worker_runner.status") == "failed"
    assert substate.get("modules.model_config.status") == "done"  # untouched


def test_phase1_steps_composition(tmp_path):
    """The deterministic phase-1 units end-to-end over fixture repos: drift +
    assignment reports under the parent's filenames, skip flags in substate,
    and path-sync retargeting a manifest copy with its report embedded in a
    subsequent assignment render."""
    from infermatrix_copilot.rebase_engine.phase1_steps import (
        Phase1Config, run_commit_assignment, run_path_sync)
    up = tmp_path / "up"
    up.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=up, check=True)
    for k, v in (("user.name", "t"), ("user.email", "t@e.c")):
        subprocess.run(["git", "config", k, v], cwd=up, check=True)
    (up / "vllm").mkdir()
    (up / "vllm" / "a.py").write_text("1")
    subprocess.run(["git", "add", "-A"], cwd=up, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=up, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=up, check=True,
                          capture_output=True, text=True).stdout.strip()
    (up / "vllm" / "a.py").write_text("2")
    subprocess.run(["git", "commit", "-aqm", "change"], cwd=up, check=True)

    target = tmp_path / "target"
    (target / "pkg").mkdir(parents=True)
    (target / "pkg" / "keep.py").write_text("x")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        (REPO_ROOT / "adapters/vllm_omni/manifest.yaml").read_text())

    log_dir = tmp_path / "logs"
    substate = Substate(tmp_path / "run", "run-1")
    cfg = Phase1Config(upstream_repo=up, target_repo=target, log_dir=log_dir,
                       baseline_commit=base, target_branch="main")
    result = run_commit_assignment(
        cfg, {"mod_a": ["vllm/"], "mod_b": ["gone/"]}, substate)
    assert result.counts == {"mod_a": 1, "mod_b": 0}
    assert substate.get("modules.mod_a.skip") is False
    assert substate.get("modules.mod_b.skip") is True
    assert (log_dir / "commits_assignment.md").is_file()
    assert "MISSING" in (log_dir / "path_drift_check.md").read_text()

    from infermatrix_copilot.rebase_engine.path_sync import CuratedEntry
    updates = run_path_sync(
        cfg, manifest,
        {"local_paths": {"input_output": ["pkg/keep.py", "pkg/gone.py"]}},
        {}, substate)
    assert updates["local_paths"]["input_output"] == ["pkg/keep.py"]
    assert (log_dir / "path_sync_report.md").is_file()
    assert substate.get("phase1.path_sync.changed") is True
    import yaml as _yaml
    assert _yaml.safe_load(manifest.read_text())["modules"]["input_output"][
        "local_paths"] == ["pkg/keep.py"]
