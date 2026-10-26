"""Per-module rebase execution — the neutral core of the parent's
`node_rebase_module` (prompt build → agent loop → substate result), with
every repo specific injected. Wave orchestration, foreach fan-out, and step
wiring land in the assembly PR; this module is the single-module unit they
compose.

Parent-parity behaviors: the module's result is SUBSTATE DATA (status/
turns), a loop failure marks the module failed (never raises through), and
the adaptive-guidance knowledge layer is best-effort via hooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..run_trace import RunTrace
from ..scopes import ToolScope
from .agent_loop import GATED_TOOL_NAMES, run_agent_loop
from .hooks import RebaseHooks
from .prompt_builder import (ModulePromptData, build_debug_prompt,
                             build_module_prompt)
from .substate import Substate


@dataclass(frozen=True)
class ModuleRunConfig:
    """Everything one module agent needs, assembled by the caller."""

    vllm_path: str
    omni_path: str
    script_dir: str
    model: str
    log_dir: str
    signal_dir: str = ""
    last_rebase_vllm_commit: str = ""
    cuda_devices: str = "0,1"
    hf_home: str = "/model"
    # Raised from 150 for the v0.30.0 campaign: a flash-class module agent
    # needs ~2.3x the turns of the pro tier on the same module (140+ vs 62),
    # mostly shell exploration before the first edit. Exhausting the budget
    # is NOT fatal — the engine restarts the agent loop — but a restart
    # discards accumulated context, so too low a ceiling makes the agent
    # re-explore from scratch. Bounds the in-process loop; a harness session
    # is bounded by `harness_timeout_s` instead (see below).
    max_turns: int = 400
    # Harness backend selection (doc/features/provider-registry.md). "api"
    # keeps the in-process Anthropic tool-use loop; any other provider id
    # delegates the whole module step to that harness, with the SAME 20-tool
    # surface served through the MCP tool bridge.
    backend: str = "api"
    backend_model: str = ""       # model INSIDE the harness; see config
    settings: Any = None          # Settings — transport construction
    manifest_path: str = ""       # adapter manifest, rebuilt inside the bridge
    paths_spec: Mapping = field(default_factory=dict)  # serialized RebasePaths
    state_slice: Mapping = field(default_factory=dict)  # run state the backends read
    repo: str = ""                # repo name recorded in the bridge spec
    # Harness session bound. `max_iters` maps to a native turn cap only where
    # the harness HAS one (claude --max-turns); cursor/codex have none, so the
    # real bound there is this timeout plus the prompt's budget discipline.
    harness_timeout_s: float = 7200.0
    max_debug_retries: int = 3
    plan_review_max_rounds: int = 2
    model_aliases: Mapping[str, str] | None = None
    model_mismatch_policy: str = "fail"
    # adapter baseline ref (repo.remote/default_branch) — reaches the
    # LIVE prompt's test-plan prose (2026-08-01 neutrality audit)
    baseline_ref: str = "origin/main"


def _plan_gate_opened(run_dir: Path, *, plan_prefix: str = "",
                      since_ts: float | None = None) -> bool:
    """Read `plan_done` back out of the bridge trace.

    The gate runs in the bridge process, so the parent cannot observe it
    directly; `PlanGate` records `plan_gate_opened` when a decision file is
    successfully written. Production callers restrict observations to this
    module's plan directory and this attempt's start time: another module's
    decision or an older attempt proves nothing about the current session.
    Bridge restarts may restore the same module's durable decision, recording
    a new event during this attempt. Absent/unreadable trace ⇒ NOT opened."""
    import json as _json
    import math as _math

    from .agent_loop import _under_plan_dir
    tp = Path(run_dir) / "bridge_trace.jsonl"
    try:
        for line in tp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = _json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("kind") != "plan_gate_opened":
                continue
            if since_ts is not None:
                ts = event.get("ts")
                if (not isinstance(ts, (int, float)) or not _math.isfinite(ts)
                        or ts < since_ts):
                    continue
            if plan_prefix:
                decision = event.get("decision")
                if (not isinstance(decision, str) or ".decision.md" not in decision
                        or not _under_plan_dir(decision, plan_prefix)):
                    continue
            return True
    except (OSError, UnicodeError):
        return False
    return False


def _changed_files(root: str) -> dict[str, int]:
    """{path: mtime_ns} for every file git reports as changed under `root`."""
    import subprocess
    try:
        proc = subprocess.run(["git", "-C", root, "status", "--porcelain"],
                              capture_output=True, text=True, timeout=120,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    out: dict[str, int] = {}
    for line in (proc.stdout or "").splitlines():
        rel = line[3:].strip().strip('"')
        if " -> " in rel:                      # rename: take the destination
            rel = rel.split(" -> ", 1)[1]
        fp = Path(root) / rel
        try:
            out[str(fp)] = fp.stat().st_mtime_ns
        except OSError:
            continue
    return out


def _native_writes(root: str, run_dir: Path, before: dict[str, int],
                   since_ts: float) -> tuple[list[str], list[str]]:
    """Files the harness wrote WITHOUT going through the bridge.

    A harness that keeps its own built-in file tools (cursor has no
    `builtin_tools_off`) can edit the checkout without touching the bridge,
    which means those writes miss BOTH the scope guard's out-of-scope
    recording and the plan gate. Sandboxing would prevent it, but needs user
    namespaces; where that is unavailable, detection is the honest ceiling.

    Returns (native, pre_gate): every natively-written file, and the subset
    that landed BEFORE the plan gate opened — the ones that broke the
    contract rather than merely bypassing the bookkeeping.
    """
    import json as _json
    import math as _math

    bridged: set[str] = set()
    gate_ts: float | None = None
    tp = Path(run_dir) / "bridge_trace.jsonl"
    try:
        for line in tp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = _json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            ts = d.get("ts")
            if (not isinstance(ts, (int, float)) or not _math.isfinite(ts)
                    or ts < since_ts):
                continue
            if d.get("kind") == "plan_gate_opened" and gate_ts is None:
                gate_ts = float(ts)
            if d.get("kind") == "tool_call" and d.get("path"):
                bridged.add(str(d["path"]))
    except (OSError, UnicodeError):
        pass

    native, pre_gate = [], []
    for path, mtime_ns in _changed_files(root).items():
        if path in bridged or before.get(path) == mtime_ns:
            continue
        native.append(path)
        if gate_ts is not None and mtime_ns / 1e9 < gate_ts:
            pre_gate.append(path)
    return sorted(native), sorted(pre_gate)


async def _harness_attempt(prompt: str, *, module: str, config,
                           scope: ToolScope, trace: RunTrace,
                           tool_defs: list[dict], plan_prefix: str,
                           require_plan_review: bool) -> dict:
    """Delegate one module attempt to a harness backend.

    The harness runs its OWN loop, so the pieces `run_agent_loop` owns
    in-process move into the bridge: the 20-tool surface (rebuilt there from
    the spec) and the plan gate (enforced at dispatch, which a harness cannot
    bypass). Returns the same dict shape the in-process attempt does so
    `rebase_module`'s retry/debug logic is untouched.
    """
    import asyncio

    from ..providers import AgentSessionRequest
    from ..providers.registry import transport_for_id
    from ..tool_bridge import write_bridge_spec

    run_dir = Path(config.log_dir)
    transport = transport_for_id(config.settings, config.backend)
    spec_path = write_bridge_spec(
        run_dir=run_dir, step_name=f"rebase.module.{module}", scope=scope,
        repo=config.repo,
        rebase={
            "tool_schemas": str(Path(config.script_dir) / "tool_schemas.json"),
            "manifest_path": config.manifest_path,
            "model": config.model,
            # prebuilt upstream: serializing the checkout paths HERE would
            # add repo-specific vocabulary to a neutral core module
            "paths": dict(config.paths_spec or {}),
            "state": dict(config.state_slice or {}),
            "plan_write_prefix": plan_prefix if require_plan_review else "",
            "gated_tools": list(GATED_TOOL_NAMES),
        })
    signal_dir = getattr(config, "signal_dir", "")
    done_path = (Path(signal_dir) / f"module.{module}.done"
                 if signal_dir else None)
    fail_path = (Path(signal_dir) / f"module.{module}.fail"
                 if signal_dir else None)
    if done_path is not None:
        # Debug templates may retain an older signal location. Supply the
        # same authoritative path on every attempt, including retries.
        prompt += ("\n\nAuthoritative completion signal for this attempt: "
                   f"write exactly `MODULE_DONE {module}` as the first line "
                   f"of `{done_path}` only after verification succeeds. "
                   "Rewrite it during this attempt; an older file does not "
                   "count. This path overrides other signal locations.")
    # NEVER forward the tier model: it names a raw-API model the harness
    # does not have. Empty lets the transport fall back to its own setting.
    req = AgentSessionRequest(
        system=prompt, prompt="", scope=scope,
        model=config.backend_model or "",
        max_iters=config.max_turns, timeout_s=config.harness_timeout_s,
        run_dir=run_dir, step_name=f"rebase.module.{module}",
        bridge_spec_path=spec_path, trace=trace)
    import time as _time

    # A normal harness exit is only transport success. The prompt requires
    # an explicit completion file, rewritten by THIS attempt; older attempts
    # may have left a signal even when the write audit rejected their work.
    def signal_snapshot(path):
        if path is None:
            return None
        try:
            stat = path.stat()
            return (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns,
                    path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return None

    prior_done = signal_snapshot(done_path)
    prior_fail = signal_snapshot(fail_path)
    before = _changed_files(scope.root)
    started = _time.time()
    outcome = await asyncio.to_thread(transport.run_session, req)

    native, pre_gate = _native_writes(scope.root, run_dir, before, started)
    if native and trace is not None:
        trace.record("harness_native_writes", step=f"rebase.module.{module}",
                     count=len(native), pre_gate=len(pre_gate),
                     files=[str(f) for f in native[:20]],
                     pre_gate_files=[str(f) for f in pre_gate[:20]])
    if pre_gate:
        # product code changed before a decision existed: the plan gate's
        # whole contract. Fail the module rather than let the wave gate
        # accept work the contract never covered.
        return {"done": False, "turns": 0, "plan_done": False,
                "text": ("harness wrote product files BEFORE the plan-review "
                         "decision, bypassing the bridge: "
                         + ", ".join(pre_gate[:10]))}
    current_done = signal_snapshot(done_path)
    current_fail = signal_snapshot(fail_path)
    signaled = (current_done is not None and current_done != prior_done
                and current_done[3].splitlines()[:1]
                == [f"MODULE_DONE {module}"])
    failed = current_fail is not None and current_fail != prior_fail
    truncated = bool(getattr(outcome, "truncated", False))
    # Refusals are typed provider outcomes (scope-audit violations or session
    # failures), not words in the final prose. A marker cannot override them.
    refusals = getattr(outcome, "refusals", ()) or ()
    plan_done = (not require_plan_review or bool(plan_prefix) and
                 _plan_gate_opened(run_dir, plan_prefix=plan_prefix,
                                   since_ts=started))
    done = signaled and not failed and not truncated and not refusals and plan_done
    result_text = getattr(outcome, "text", "") or ""
    if not done:
        reasons = []
        if not signaled:
            reasons.append(f"a fresh exact MODULE_DONE {module} signal is required")
        if failed:
            reasons.append("a fresh failure signal was written")
        if truncated:
            reasons.append("the session was truncated")
        if refusals:
            reasons.append("provider refusal(s): " + "; ".join(
                str(refusal) for refusal in refusals[:3]))
        if not plan_done:
            reasons.append("this attempt did not open its module's plan-review gate")
        result_text += "\nHarness completion rejected: " + "; ".join(reasons) + "."
    return {"done": done,
            "session_completed": not truncated,
            "text": result_text,
            "turns": int(getattr(outcome, "iterations", 0) or 0),
            "plan_done": plan_done}


async def rebase_module(
    module: str,
    *,
    client: Any,
    config: ModuleRunConfig,
    prompt_data: ModulePromptData,
    tool_defs: list[dict],
    extra_tools: Mapping,
    substate: Substate,
    hooks: RebaseHooks | None = None,
    scope: ToolScope | None = None,
    trace: RunTrace | None = None,
    broken_imports: list[dict] | None = None,
    module_test_plan: dict | None = None,
) -> dict:
    """Run one module's rebase agent and record the outcome in substate.
    Returns the module's result dict (also written under
    ``modules.<module>``)."""
    hooks = hooks or RebaseHooks()
    try:
        guidance = hooks.adaptive_guidance(module)
    except Exception:  # noqa: BLE001 - knowledge layer never blocks a rebase
        guidance = ""

    state = config.state_slice or {}
    params = (state.get("task_spec") or {}).get("params") or {}
    assignment_baseline = (config.last_rebase_vllm_commit
                           or state.get("last_rebase_upstream_commit", "")
                           or params.get("last_rebase_commit", ""))
    target_branch = (state.get("target_branch", "")
                     or params.get("target_branch", ""))
    upstream_commit = (state.get("upstream_commit", "")
                       or params.get("upstream_commit", "")
                       or params.get("force_upstream_commit", ""))
    prompt = build_module_prompt(
        module, prompt_data,
        vllm_path=config.vllm_path, omni_path=config.omni_path,
        script_dir=config.script_dir,
        last_rebase_vllm_commit=assignment_baseline,
        cuda_devices=config.cuda_devices, hf_home=config.hf_home,
        log_dir=config.log_dir, signal_dir=config.signal_dir,
        rebase_run_id=substate.run_id,
        max_debug_retries=config.max_debug_retries,
        plan_review_max_rounds=config.plan_review_max_rounds,
        broken_imports=broken_imports, module_test_plan=module_test_plan,
        adaptive_guidance=guidance, live=True,
        baseline_ref=config.baseline_ref,
        target_branch=target_branch, upstream_commit=upstream_commit)

    substate.update({"modules": {module: {"status": "running"}}})
    agent_log = str(Path(config.log_dir) / "agents" / f"module-{module}.log")
    Path(agent_log).parent.mkdir(parents=True, exist_ok=True)
    plan_prefix = str(Path(config.log_dir) / "plans" / f"module-{module}")

    async def _attempt(p: str, *, require_plan_review: bool = True) -> dict:
        try:
            if config.backend and config.backend != "api":
                return await _harness_attempt(
                    p, module=module, config=config, scope=scope, trace=trace,
                    tool_defs=tool_defs, plan_prefix=plan_prefix,
                    require_plan_review=require_plan_review)
            return await run_agent_loop(
                client, p, model=config.model, tool_defs=tool_defs,
                extra_tools=extra_tools, scope=scope, trace=trace,
                max_turns=config.max_turns, plan_write_prefix=plan_prefix,
                require_plan_review=require_plan_review,
                model_aliases=config.model_aliases,
                model_mismatch_policy=config.model_mismatch_policy,
                agent_log=agent_log)
        except Exception as exc:  # noqa: BLE001 - failure is substate data
            return {"done": False, "text": f"agent loop error: {exc}",
                    "turns": 0, "plan_done": False}

    # parent parity: an incomplete first run gets up to max_debug_retries
    # follow-up attempts with the debug prompt built from the failure text.
    # The plan gate PERSISTS across attempts: once the module's decision was
    # written, debug retries run unlocked (the debug prompt carries no
    # plan-review contract, and the parent's re-locking left debug agents
    # unable to edit — a fixed, recorded parent defect); an initial run that
    # never passed the gate keeps it closed for retries.
    debug_attempts = 0
    result = await _attempt(prompt)
    gate_passed = bool(result.get("plan_done"))
    while not result.get("done") and \
            debug_attempts < config.max_debug_retries:
        debug_attempts += 1
        debug_prompt = build_debug_prompt(
            module, result.get("text", ""),
            prompt_data.debug_prompt_template_live
            or prompt_data.debug_prompt_template, "")
        result = await _attempt(debug_prompt,
                                require_plan_review=not gate_passed)
        gate_passed = gate_passed or bool(result.get("plan_done"))

    outcome = {"status": "done" if result.get("done") else "failed",
               "exit_code": 0 if result.get("done") else -1,
               "debug_attempts": debug_attempts,
               "turns": result.get("turns", 0),
               "summary": (result.get("text") or "")[:2000]}
    substate.update({"modules": {module: outcome}})
    try:
        hooks.on_module_result(module, outcome)
    except Exception:  # noqa: BLE001 - observation hook never raises through
        pass
    return outcome
