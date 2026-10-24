"""repo-rebase v3 steps — thin, governed wrappers over `rebase_engine`
(assembly PR; the playbook is CANDIDATE until the validation gate).

Every handler is substate-first (durable, run-stamped), publishes consumed
keys via `state_updates`, and fails typed — the transition-table terminal
rows are enforced by `rebase.v3_finalize` (all-steps-ok + substate failures
⇒ BLOCKED, the reused needs-human exit 3; it runs AFTER the report step so
RUN_REPORT always exists when the run terminates needs-human)."""

from __future__ import annotations

from dataclasses import asdict as _dc_asdict

import asyncio
import shlex
import weakref
from pathlib import Path
from typing import Mapping

import yaml

from ...rebase_engine import test_loop as tl
from ...rebase_engine import wheel as wheel_mod
from ...rebase_engine.debug_patch_policy import (capture_patch_policy,
                                                 evaluate_debug_patch)
from ...rebase_engine.modes import MODES, MUTATING_MODES, mode_state_flags
from ...rebase_engine.phase1_steps import Phase1Config, run_commit_assignment
from ...rebase_engine.push_gate import evaluate_push_gate
from ...rebase_engine.substate import Substate
from ...rebase_engine.test_manifest import ManifestSpec, build_manifest
from ...rebase_engine.testing_env import scrubbed_agent_env
from ...scopes import PathScope, ToolScope
from ..step import FailureKind, StepContext, StepResult
from ._common import require_repo, step


def _task_params(ctx: StepContext) -> dict:
    spec = ctx.state.get("task_spec") or {}
    return (spec.get("params") if isinstance(spec, dict) else {}) or {}


def _adapter_manifest(ctx: StepContext) -> dict | StepResult:
    repo = (ctx.state.get("task_spec") or {}).get("repo", "")
    path = Path(ctx.settings.adapters_dir) / repo.replace("-", "_") / \
        "manifest.yaml"
    if not path.is_file():
        return StepResult(False, FailureKind.BLOCKED,
                          f"no adapter manifest for {repo!r} — the v3 "
                          "pipeline is adapter-data driven")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _substate(ctx: StepContext) -> Substate:
    return Substate(ctx.run_dir, ctx.state.get("run_id")
                    or ctx.run_dir.name)


def _parse_env_pairs(env_str: str) -> dict[str, str]:
    """The manifest job's `env` field ("K=V K2=V2", from stripped `export`
    lines) as a dict for `TestJob.env` — dropping a job's declared env would
    change what the test actually exercises."""
    out: dict[str, str] = {}
    try:
        parts = shlex.split(env_str or "")
    except ValueError:
        parts = (env_str or "").split()
    for part in parts:
        if "=" in part:
            k, v = part.split("=", 1)
            if k:
                out[k] = v
    return out


def manifest_job_to_test_job(j: Mapping):
    """THE production conversion from a built-manifest job dict to the
    runner's `TestJob` — one authority, used by the v3 test loop and pinned
    by the tier-3 shell-golden parity suite (a test-local re-implementation
    would let the two drift apart unnoticed)."""
    from ...testing.runner import TestJob
    return TestJob(key=j["slug"], command=j["command"],
                   timeout_sec=j["timeout_sec"], min_gpus=j["min_gpus"],
                   env=_parse_env_pairs(j.get("env", "")),
                   setup=j.get("setup", ""))


def _tier_client(ctx: StepContext):
    """The run's agent backend via `Settings.tier_target` — the ONLY place a
    tier's model may pair with an endpoint and credential. Returns
    `(client, target)` or a BLOCKED StepResult (with a `capability_gap`
    trace) when no credential is configured for the resolved tier."""
    from ...config import TierNotConfiguredError
    mode = (ctx.state.get("task_spec") or {}).get("mode", "eco")
    try:
        target = ctx.settings.tier_target(mode)
    except TierNotConfiguredError as exc:
        return StepResult(False, FailureKind.BLOCKED, str(exc))
    if not target.api_key:
        ctx.trace.record("capability_gap", capability="rebase.module_agent",
                         detail=f"no API key for tier target {target.source} "
                                "— rebase agents cannot run")
        return StepResult(False, FailureKind.BLOCKED,
                          "rebase agents need a configured Anthropic-"
                          "compatible backend for the resolved tier")
    from anthropic import AsyncAnthropic
    kwargs: dict = {"api_key": target.api_key}
    if target.base_url:
        kwargs["base_url"] = target.base_url
    return AsyncAnthropic(**kwargs), target


def _target_venv(manifest: dict, extra: dict | None = None) -> str:
    """The TARGET repo's virtualenv from adapter data (env-expanded).
    Empty = not configured. `extra` is the Settings-derived fallback for
    `${VAR}`s the shell did not export (adapter-declared `.env` keys)."""
    from ...adapters.base import expand_path
    return expand_path((manifest.get("repo") or {}).get("venv", ""),
                       extra=extra)


def _venv_declared(manifest: dict) -> bool:
    """Whether the adapter DECLARES a runtime venv at all — declared-but-
    unresolved blocks (misconfiguration must surface), while an adapter
    that declares none (non-venv stack) proceeds with the inherited env
    (2026-08-01 neutrality audit: the venv workflow is adapter data, not
    a core requirement)."""
    return "venv" in (manifest.get("repo") or {})


def _baseline_ref(manifest: dict) -> str:
    """`<remote>/<default_branch>` from adapter data — never a hardcoded
    origin/main (2026-08-01 neutrality audit)."""
    repo = manifest.get("repo") or {}
    return f"{repo.get('remote', 'origin')}/{repo.get('default_branch', 'main')}"


def _test_roots(manifest: dict) -> tuple:
    tm = (manifest.get("rebase") or {}).get("test_manifest") or {}
    return tuple(tm.get("test_change_roots") or ("tests/",))


def _target_test_env(settings, manifest: dict,
                     *, pythonpath_prepend: str | None = None) -> dict:
    """The env for TARGET-repo subprocesses (tests, precommit, wheel
    installs): inherit-plus-overlay with the target venv on PATH, the
    host's CUDA selection, and HF_HOME — raw manifest commands like
    `pytest ...` must resolve inside the target repo's runtime, never the
    copilot's own virtualenv."""
    import os
    from ...testing.env_plan import build_subprocess_env
    venv = _target_venv(manifest, extra=settings.expansion_env())
    return build_subprocess_env(
        venv=Path(venv) if venv else None,
        cuda_visible_devices=os.environ.get("CUDA_VISIBLE_DEVICES"),
        hf_home=os.environ.get("HF_HOME"),
        pythonpath_prepend=pythonpath_prepend)


def _agent_shell_env(ctx: StepContext, manifest: dict, repo_root: str,
                     adapter_dir: Path, *, gpu_mutex: bool = True) -> dict:
    """The env for AGENT shells (`run_shell` + everything it spawns): the
    PR1 credential scrub over the process env, then the TARGET runtime
    overlay (venv PATH/VIRTUAL_ENV, host CUDA selection, HF_HOME) and the
    `imx-omni-pytest` contract variables — without IMX_TARGET_REPO/
    IMX_LOG_DIR the mandated pytest wrapper exits immediately, and without
    the venv overlay agent verification runs against the copilot's own
    environment."""
    import os
    from ...testing.env_plan import build_subprocess_env
    venv = _target_venv(manifest, extra=ctx.settings.expansion_env())
    # HF token only on the manifest's EXPLICIT opt-in (Rev 8 §4:
    # validation.requires_hf_token — gated-model verification needs it;
    # every other adapter keeps the token scrubbed)
    keep_hf = bool((manifest.get("validation") or {})
                   .get("requires_hf_token"))
    env = build_subprocess_env(
        base=scrubbed_agent_env(keep_hf_token=keep_hf),
        venv=Path(venv) if venv else None,
        cuda_visible_devices=os.environ.get("CUDA_VISIBLE_DEVICES"),
        hf_home=os.environ.get("HF_HOME"))
    env["IMX_TARGET_REPO"] = str(repo_root)
    env["IMX_LOG_DIR"] = str(ctx.run_dir)
    env["IMX_ADAPTER_REBASE"] = str(adapter_dir / "rebase")
    # watchdog learning contract (design D4): the wrapper stamps decisions
    # with the run identity and merges the runtime noise overlay
    _state = getattr(ctx, "state", None) or {}
    env["IMX_RUN_ID"] = str(_state.get("run_id", "") or ctx.run_dir.name)
    _settings = getattr(ctx, "settings", None)
    if _settings is not None:
        from ...memory.paths import KnowledgePaths as _KP2
        _repo_slug = (_state.get("task_spec") or {}).get("repo", "")
        env["IMX_WATCHDOG_OVERLAY"] = str(_KP2.resolve(
            _settings, _repo_slug,
            adapter_root=adapter_dir).watchdog_overlay)
    if gpu_mutex:
        # Phase-2 module agents serialize every wrapper invocation on the
        # GPU lock (parent contract)
        env["IMX_GPU_MUTEX"] = "1"
    return env


def build_backends(*, settings, state, run_dir: Path, trace,
                   manifest: dict, repo: str, model: str,
                   base_url: str = "", api_key: str = ""):
    """The PRODUCTION `RebaseBackends`: plan review on the run's resolved
    tier backend, pytest/reproduce/precommit through the PR1 runner in the
    TARGET env, and the knowledge tools on the copilot stores (agents may
    only propose skills — governance preserved). Replaces the fail-closed
    `_unwired` defaults for live module/debug agents; an unavailable
    collaborator still answers with an explicit error dict, never a
    silent success. All handlers are SYNC (tool dispatch is synchronous
    inside the agent loop) — the reviewer uses its own sync client.

    Takes EXPLICIT serializable inputs rather than a `StepContext` so the
    harness tool bridge can rebuild the identical production backends in its
    own process from a bridge spec (see `tool_bridge`). `_build_backends`
    below is the thin in-process adapter."""
    from ...memory import SkillStore
    from ...memory.debug_memory import DebugMemory
    from ...rebase_engine.plan_review import review_plan
    from ...rebase_engine.rebase_tools import RebaseBackends
    from ...testing.runner import TestJob, TestRunner
    repo_name = (state.get("task_spec") or {}).get("repo", "")
    reviewer_model = getattr(settings, "rebase_reviewer_model", "") \
        or model

    def request_plan_review(**kw) -> dict:
        from anthropic import Anthropic
        ckw: dict = {"api_key": api_key}
        if base_url:
            ckw["base_url"] = base_url
        return review_plan(
            Anthropic(**ckw), reviewer_model,
            plan_json_path=str(kw.get("plan_json_path", "")),
            plan_md_path=str(kw.get("plan_md_path", "") or ""),
            kind=str(kw.get("kind", "rebase")))

    def _run_tests(kw: Mapping, key: str) -> dict:
        paths = kw.get("test_paths") or []
        if isinstance(paths, str):
            paths = paths.split()
        if not paths:
            return {"error": "test_paths is required"}
        markers = str(kw.get("markers", "") or "")
        runner_cmd = ((manifest.get("rebase") or {}).get("testing") or {}) \
            .get("pytest_command", "python -m pytest")
        cmd = f"{runner_cmd} " + " ".join(str(p) for p in paths)
        if markers:
            cmd += f" -m '{markers}'"
        runner = TestRunner(repo_root=Path(repo),
                            tests_dir=run_dir / "tests",
                            gpu_lock_dir=run_dir / "gpu_lock")
        outcome = runner.run(
            TestJob(key=f"{key}_{abs(hash(cmd)) % 10 ** 8}", command=cmd,
                    timeout_sec=float(kw.get("timeout") or 1800),
                    min_gpus=0, gpu_lock=True),
            _target_test_env(settings, manifest))
        tail = ""
        try:
            if outcome.log_file and Path(outcome.log_file).is_file():
                tail = "\n".join(Path(outcome.log_file)
                                 .read_text(encoding="utf-8",
                                            errors="replace")
                                 .splitlines()[-120:])
        except OSError:
            pass
        return {"exit_code": outcome.rc, "passed": outcome.rc == 0,
                "timed_out": outcome.timed_out, "output": tail,
                "log_file": outcome.log_file}

    def run_pytest(**kw) -> dict:
        return _run_tests(kw, "agent_pytest")

    def reproduce(**kw) -> dict:
        return _run_tests(kw, "agent_repro")

    def run_precommit(**kw) -> dict:
        pc = (manifest.get("rebase") or {}).get("precommit") or {}
        command = str(pc.get("command") or "")
        if not command:
            return {"error": "no precommit command declared in the adapter "
                             "manifest"}
        files = kw.get("files") or []
        if isinstance(files, str):
            files = files.split()
        if files:
            # `--all-files` and `--files` are mutually exclusive — a
            # file-scoped call must drop the manifest command's all-files
            # sweep or pre-commit refuses to run at all
            import re as _re
            command = _re.sub(r"\s(?:--all-files|-a)\b", "", command)
            command += " --files " + " ".join(str(f) for f in files)
        runner = TestRunner(repo_root=Path(repo),
                            tests_dir=run_dir / "tests",
                            gpu_lock_dir=run_dir / "gpu_lock")
        outcome = runner.run(
            TestJob(key="agent_precommit", command=command,
                    timeout_sec=float(pc.get("timeout_sec") or 600),
                    min_gpus=0, gpu_lock=False),
            _target_test_env(settings, manifest))
        return {"exit_code": outcome.rc, "passed": outcome.rc == 0,
                "log_file": outcome.log_file}

    from ...memory.paths import KnowledgePaths
    kpaths = KnowledgePaths.resolve(
        settings, repo_name,
        adapter_root=Path(settings.adapters_dir)
        / repo_name.replace("-", "_"))

    def _memory() -> DebugMemory:
        return DebugMemory(kpaths.rebase_backend_db)

    # READ-ONLY parent-store read-compat (Rev 8 §9 PR4c / design D2): the
    # prelude already failed CLOSED if a declared layer was unreadable at
    # run start; a MID-RUN read error here degrades open (copilot layers
    # still answer) with a traced note — a knowledge hiccup must never
    # kill a run the gate already provenance-stamped.
    from ...adapters.base import expand_path
    _knowledge_cfg = (manifest.get("rebase") or {}).get("knowledge") or {}
    _kn_extra = settings.expansion_env()
    parent_db_path = expand_path(str(_knowledge_cfg.get("parent_debug_db")
                                     or ""), extra=_kn_extra)
    parent_skills_path = expand_path(
        str(_knowledge_cfg.get("parent_skills_dir") or ""),
        extra=_kn_extra)

    def _parent_memory_hits(query: str, k: int) -> list[dict]:
        if not parent_db_path:
            return []
        try:
            from ...rebase_engine.parent_compat import ParentDebugMemory
            return ParentDebugMemory(
                parent_db_path,
                upstream_column=str(_knowledge_cfg.get(
                    "parent_upstream_column") or "")).search(query, k=k)
        except Exception as exc:  # noqa: BLE001 — degrade open, traced
            trace.record("capability_note",
                             capability="rebase.knowledge.parent_debug_db",
                             detail=f"parent layer degraded mid-run: {exc}")
            return []

    def search_debug_memory(**kw) -> dict:
        query = " ".join(str(kw.get(k, "") or "")
                         for k in ("keyword", "module", "tags")).strip()
        if not query:
            return {"results": []}
        k = int(kw.get("max_results") or 5)
        try:
            raw = _memory().search(query, k=k, repo=repo_name)
        except Exception as exc:  # noqa: BLE001 - store trouble is a result
            return {"error": f"debug memory unavailable: {exc}"}
        # dedup the copilot layer by signature FIRST: k same-signature
        # rows must not count as a full result set and mask the parent
        # layer entirely (verification-round finding)
        results = []
        result_sigs = set()
        for r in raw:
            sig = (r.get("module"), str(r.get("symptom", ""))[:80])
            if sig not in result_sigs:
                result_sigs.add(sig)
                results.append(r)
        # copilot layer first, parent read-compat layer appended (D2 union
        # order); dedup on (module, symptom head) so a migrated/duplicated
        # fact never shows twice. The parent layer is fetched TO
        # EXHAUSTION (widening rounds until the union fills k or the
        # store runs out) so cross-layer collisions can never starve a
        # distinct parent hit out of the union, no matter how deep the
        # duplicate run is (review iteration 3 F3).
        seen = set(result_sigs)
        fetch = k
        while len(results) < k:
            hits = _parent_memory_hits(query, fetch)
            for hit in hits:
                if len(results) >= k:
                    break
                sig = (hit.get("module"), str(hit.get("symptom", ""))[:80])
                if sig not in seen:
                    seen.add(sig)
                    results.append({**hit, "source_layer": "parent"})
            if len(hits) < fetch:
                break  # the parent store is exhausted
            fetch *= 4
        return {"results": results}

    def record_debug_memory(**kw) -> dict:
        try:
            run_id = str(state.get("run_id", ""))
            # additive v2 fields land in their OWN columns (round-4 F4 —
            # the old key-inside-verification packing lost every one of
            # them to curation and migration); v3_knowledge_prep
            # guarantees the columns exist before any agent records
            entry_id = _memory().record(
                repo=repo_name, module=str(kw.get("module", "")),
                run_id=run_id,
                symptom=str(kw.get("symptom", "")),
                root_cause=str(kw.get("root_cause", "")),
                fix_summary=str(kw.get("fix", "")),
                files=[f.strip() for f in
                       str(kw.get("files", "")).split(",") if f.strip()],
                verification="recorded by rebase agent",
                key=str(kw.get("key", "") or ""),
                tags=kw.get("tags", ""),
                watch_outs=str(kw.get("watch_outs", "") or ""),
                upstream_commit=str(
                    state.get("upstream_commit", "") or ""),
                last_seen_run=run_id,
                source="v3-agent")
            return {"ok": True, "id": entry_id}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"debug memory write failed: {exc}"}

    # seed skills live in the (runtime-READ-ONLY) adapter tree; agent
    # proposals land in the RUNTIME state dir — writing candidates into
    # the checked-in adapter source would dirty it and bypass the module
    # scope's writable wall (Rev 8 §1.1 seed/runtime split)
    seed_skills_dir = kpaths.skills_seed_dir
    runtime_skills_dir = kpaths.skills_runtime_dir

    def search_skills(**kw) -> dict:
        query = str(kw.get("keyword", "") or "")
        module_q = str(kw.get("module", "") or "")
        k = int(kw.get("max_results") or 3)
        # retrieval = a REAL union, priority order runtime > adapter seed >
        # parent seed (D2): runtime (learned, newer) first, then seed
        # entries it doesn't override, then the read-only parent layer —
        # first store wins per name. Each layer is fetched EXHAUSTIVELY
        # (k = the store's own size, so truncation is structurally
        # impossible, not just unlikely) — a name collision with a higher
        # layer can never starve a distinct lower-layer skill out of the
        # union; the union itself is capped at k.
        merged: dict[str, object] = {}
        layers = [runtime_skills_dir, seed_skills_dir]
        if parent_skills_path:
            layers.append(Path(parent_skills_path))
        for store_dir in layers:
            try:
                store = SkillStore(store_dir)
                found = store.find(query=query, module=module_q,
                                   k=len(store.load_all()) or 1)
            except Exception as exc:  # noqa: BLE001 — degrade open, traced
                trace.record(
                    "capability_note",
                    capability="rebase.knowledge.parent_skills_dir",
                    detail=f"skill layer {store_dir} degraded: {exc}")
                continue
            for s in found:
                merged.setdefault(s.name, s)
        return {"skills": [{"name": s.name, "description": s.description}
                           for s in list(merged.values())[:k]]}

    def skill_manage(**kw) -> dict:
        action = str(kw.get("action", ""))
        if action not in ("create", "propose", "update", "save"):
            return {"error": f"unsupported skill action {action!r} — "
                             "agents may only propose"}
        try:
            # governance: agents PROPOSE candidates; promotion to a real
            # SKILL.md is a curator/human action (read-wide/write-narrow)
            SkillStore(runtime_skills_dir).propose(
                name=str(kw.get("name", "")),
                description=str(kw.get("description", "")),
                body=str(kw.get("body", "")))
            return {"ok": True, "proposed": str(kw.get("name", "")),
                    "note": "recorded as a CANDIDATE — human promotion "
                            "required"}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"skill proposal failed: {exc}"}

    def doc_search(**kw) -> dict:
        """Curated knowledge base, repo-scoped like the review flows.

        The rebase agents previously saw only debug_memory (past FIXES) and
        skills; the human-written pages under the adapter's knowledge slice
        — component maps, model notes, CI and git lore — were unreachable,
        so a module agent rediscovered by shell what a page already stated.
        """
        try:
            from ...knowledge_docs import KnowledgeDocs
            kn = manifest.get("knowledge") or {}
            docs = KnowledgeDocs(ctx.settings.knowledge_dir,
                                 kn.get("repo_subdir"))
            query = str(kw.get("query") or kw.get("keyword") or "").strip()
            if not query:
                return {"error": "doc_search requires a non-empty query"}
            limit = max(1, min(int(kw.get("limit") or 20), 40))
            hits = docs.search(query, limit=limit)
            return {"query": query, "repo": repo, "matches": hits,
                    "truncated": len(hits) >= limit}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"doc_search failed: {exc}"}

    def doc_read(**kw) -> dict:
        """Read one knowledge page by the path doc_search returned."""
        try:
            from ...knowledge_docs import KnowledgeDocs
            kn = manifest.get("knowledge") or {}
            docs = KnowledgeDocs(ctx.settings.knowledge_dir,
                                 kn.get("repo_subdir"))
            path = str(kw.get("path") or "").strip()
            if not path:
                return {"error": "doc_read requires a path from doc_search"}
            page = docs.read(path, offset=int(kw.get("offset") or 0))
            return {"repo": repo, **page}
        except FileNotFoundError:
            return {"error": f"no such document: {kw.get('path')!r} "
                             "(use a path returned by doc_search)"}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"doc_read failed: {exc}"}

    return RebaseBackends(
        doc_search=doc_search, doc_read=doc_read,
        search_debug_memory=search_debug_memory,
        record_debug_memory=record_debug_memory,
        skill_manage=skill_manage, search_skills=search_skills,
        request_plan_review=request_plan_review,
        run_pytest=run_pytest, reproduce=reproduce,
        run_precommit=run_precommit)




def _build_backends(ctx: StepContext, manifest: dict, repo: str, target):
    """In-process adapter: the same production backends, built from a
    `StepContext`. Kept so existing callers are untouched."""
    return build_backends(
        settings=ctx.settings, state=ctx.state, run_dir=ctx.run_dir,
        trace=ctx.trace, manifest=manifest, repo=repo,
        model=target.model,
        base_url=getattr(target, "base_url", "") or "",
        api_key=getattr(target, "api_key", "") or "")


def _module_scope(repo_root: str, module: str, manifest: dict,
                  run_dir: Path | None = None) -> ToolScope:
    """C5 path governance for one module agent: the repo tree (plus the
    run's own artifact dir — the plan gate REQUIRES the agent to write its
    decision under `<run_dir>/plans/`) is the hard writable wall; the
    module's manifest `local_paths` plus the plan dir are its primary files
    — writes elsewhere execute but are RECORDED out-of-scope. An UNKNOWN
    module (e.g. a debug agent for an unassigned job) gets a never-matching
    repo primary, so every one of its repo writes is recorded rather than
    silently in-scope."""
    root = Path(repo_root).resolve()
    local = tuple(((manifest.get("modules") or {}).get(module) or {})
                  .get("local_paths") or ())
    writable = [f"{root.as_posix()}/*"]
    primary = [f"{(root / p).as_posix()}*" for p in local] \
        or ["/__imx_no_primary__/*"]
    if run_dir is not None:
        rd = Path(run_dir).resolve().as_posix()
        writable.append(f"{rd}/*")
        primary.append(f"{rd}/plans/*")
    return ToolScope(
        name=f"rebase-module:{module}",
        allowed_tools=frozenset(),  # extras bypass the builtin allowlist;
                                    # enforcement here is the path scope
        path_scope=PathScope(writable=tuple(writable),
                             primary=tuple(primary)),
        root=str(root))


# same-checkout module agents must never run concurrently — the executor's
# foreach fan-out gathers items, but every module mutates the SAME target
# tree (the parent runs wave members sequentially). Locks are LOOP-scoped:
# keyed WEAKLY by the running loop object (the runtime-registry pattern —
# id() reuse on a dead loop's address can NOT resurrect its lock) with a
# per-loop run-dir map; a collected loop drops its whole entry.
_MODULE_SERIAL: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict]" \
    = weakref.WeakKeyDictionary()


def _serial_lock(run_dir) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    per_loop = _MODULE_SERIAL.setdefault(loop, {})
    return per_loop.setdefault(str(run_dir), asyncio.Lock())


def _drop_serial_lock(run_dir) -> None:
    """Completed-run cleanup (called by the lock finalizer): drop the run's
    serialization entry from every live loop's map."""
    for per_loop in list(_MODULE_SERIAL.values()):
        per_loop.pop(str(run_dir), None)


# run dirs whose scratch teardown finalizer is registered in THIS process —
# a resumed run that ADOPTS an existing scratch must re-register teardown or
# the "disposable" checkout survives its run
_SCRATCH_REGISTERED: set[str] = set()


def _register_scratch_teardown(ctx: StepContext, scratch: Path) -> None:
    key = str(ctx.run_dir)
    if key in _SCRATCH_REGISTERED:
        return
    _SCRATCH_REGISTERED.add(key)
    from ..lifecycle import register_finalizer

    async def _teardown_scratch(_outcome, _path=scratch, _key=key) -> None:
        import shutil
        shutil.rmtree(_path, ignore_errors=True)
        _SCRATCH_REGISTERED.discard(_key)

    register_finalizer(ctx.run_dir, _teardown_scratch)


def _ensure_upstream_scratch(ctx: StepContext) -> str | StepResult:
    """The per-run DISPOSABLE upstream checkout (Rev 8 §4 risk reduction):
    wheel selection resets/checks out the tree and agent `run_shell` can
    mutate it, so those operations get a `git clone --shared` scratch of
    the canonical upstream, torn down by a lifecycle finalizer. Returns
    the scratch path — re-cloning on resume if a prior teardown removed
    it, and RE-REGISTERING teardown when a resumed process merely adopts
    a surviving scratch; BLOCKED when no canonical upstream is known or
    the clone fails."""
    import subprocess
    scratch = Path(ctx.state.get("upstream_path", "") or "")
    origin = ctx.state.get("upstream_origin_path", "")
    # only a path INSIDE the run dir counts as an existing scratch — a
    # canonical path in `upstream_path` (older state, manual seeding) must
    # never be adopted as the mutable tree
    adopted = (str(scratch).startswith(str(ctx.run_dir))
               and (scratch / ".git").exists())
    if not adopted:
        if not origin:
            return StepResult(False, FailureKind.BLOCKED,
                              "no canonical upstream recorded — prelude gap")
        scratch = ctx.run_dir / "upstream_scratch"
    if not (scratch / ".git").exists():
        r = subprocess.run(["git", "clone", "--shared", "--no-checkout",
                            str(origin), str(scratch)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return StepResult(False, FailureKind.BLOCKED,
                              "upstream scratch clone failed: "
                              + r.stderr.strip()[:300])
        subprocess.run(["git", "-C", str(scratch), "checkout", "--detach",
                        "HEAD"], capture_output=True, text=True,
                       timeout=300)
        ctx.trace.record("upstream_scratch_created", path=str(scratch))
    _register_scratch_teardown(ctx, scratch)
    if origin:
        # Borrow objects from the canonical checkout, but fetch branch refs
        # from its upstream. A local clone may have only a main branch even
        # when its remote publishes the release branch selected by this run.
        remote = ctx.state.get("upstream_remote") or "origin"
        source_remote = subprocess.run(
            ["git", "-C", str(origin), "remote", "get-url", str(remote)],
            capture_output=True, text=True, timeout=30, check=False)
        if source_remote.returncode == 0 and source_remote.stdout.strip():
            result = subprocess.run(
                ["git", "-C", str(scratch), "remote", "set-url", "origin",
                 source_remote.stdout.strip()],
                capture_output=True, text=True, timeout=30, check=False)
            if result.returncode != 0:
                return StepResult(False, FailureKind.BLOCKED,
                                  "upstream scratch remote configuration failed")
    ctx.state["upstream_path"] = str(scratch)
    return str(scratch)


# process-local registry of HELD checkout flocks, keyed by run dir. The
# prelude acquires them on a fresh run — but a `--resume` replays completed
# steps from the checkpoint WITHOUT executing them, so every mutating step
# re-ensures the locks through here (idempotent within the process; a new
# process acquires fresh flocks).
_HELD_LOCKS: dict[str, list] = {}


def _ensure_checkout_locks(ctx: StepContext, manifest: dict,
                           mode: str) -> StepResult | None:
    """Acquire (once per process per run) the shared checkout flocks for a
    mutating mode and register their lifecycle-finalizer release. Returns a
    BLOCKED StepResult on contention, None when held/not needed."""
    if mode not in MUTATING_MODES:
        return None
    key = str(ctx.run_dir)
    if _HELD_LOCKS.get(key):
        return None
    repo = require_repo(ctx)
    if isinstance(repo, StepResult):
        return repo
    from ...rebase_engine.runctx import CheckoutLock
    rb = manifest.get("rebase") or {}
    locks = [CheckoutLock(Path(repo), rb.get("lock_name", "checkout"))]
    # the CANONICAL upstream is what external users contend on — the
    # per-run scratch clone inside run_dir needs no shared lock
    upstream = ctx.state.get("upstream_origin_path", "")
    if upstream and mode == "full":
        locks.append(CheckoutLock(Path(upstream), "upstream"))
    held: list = []
    for lock in locks:
        if not lock.acquire(blocking=False):
            for h in held:
                h.release()
            if lock.last_failure.startswith("contention"):
                return StepResult(False, FailureKind.BLOCKED,
                                  f"another run holds {lock.path} — an "
                                  "external or archival run is active on "
                                  "this checkout")
            return StepResult(False, FailureKind.BLOCKED,
                              f"checkout lock SETUP failed for {lock.path}"
                              f": {lock.last_failure} — fix the checkout "
                              "metadata/permissions (retrying cannot help)")
        held.append(lock)
    _HELD_LOCKS[key] = held
    from ..lifecycle import register_finalizer

    async def _release_locks(_outcome, _key=key) -> None:
        for h in _HELD_LOCKS.pop(_key, []):
            h.release()
        _drop_serial_lock(_key)

    register_finalizer(ctx.run_dir, _release_locks)
    ctx.trace.record("checkout_locks_acquired",
                     paths=[str(lk.path) for lk in held])
    return None


@step("rebase.v3_prelude", "deterministic", "read",
      "Validate mode + adapter data; init runtime, locks, and mode flags.")
async def _v3_prelude(ctx: StepContext) -> StepResult:
    """Publishes the `mode_*` flags every later `when:` gate uses (Rev 8
    §2.1 — the mode was already resolved and written back by
    `resolve_effective_mode` before confirmation; an absent/unknown value
    here is a hard failure, not a default). For mutating modes it also
    initializes the run's world: the shared checkout flocks (released by a
    lifecycle finalizer on EVERY exit path), `upstream_path`, and the
    `last_rebase_upstream_commit` baseline (Rev 8 §3.4 — without these
    every full run would block at the wheel step and mutation paths would
    race external checkout users)."""
    mode = _task_params(ctx).get("rebase_mode", "")
    if mode not in MODES:
        return StepResult(False, FailureKind.BLOCKED,
                          f"rebase_mode missing/unknown ({mode!r}) — "
                          "resolve_effective_mode did not run; refuse to "
                          "guess permissions")
    manifest = _adapter_manifest(ctx)
    if isinstance(manifest, StepResult):
        return manifest
    if manifest.get("status") != "active":
        return StepResult(False, FailureKind.BLOCKED,
                          "adapter is not active — v3 refuses inactive "
                          "adapters")

    # transition-table row 3 (Rev 8 §3.1): a run that ends blocked at ANY
    # later step still owes RUN_REPORT — the report step never runs on the
    # blocked path, so a lifecycle finalizer augments the artifacts
    # (never upgrades the outcome). Registered before anything can block.
    from ..lifecycle import register_finalizer
    sub = _substate(ctx)

    async def _terminal_report(outcome, _run_dir=ctx.run_dir, _sub=sub,
                               _mode=mode) -> None:
        path = _run_dir / "RUN_REPORT.md"
        if path.exists():
            return
        status = getattr(outcome, "status", None) or "aborted"
        data = _sub.read()
        mods = data.get("modules") or {}
        pipeline = ((data.get("tests") or {}).get("pipeline")) or {}
        path.write_text(
            "# Run report (terminal — written by the run finalizer)\n\n"
            f"- status: {status}\n"
            f"- rebase_mode: {_mode}\n"
            f"- substate phase: {data.get('phase', '')}\n"
            f"- modules: "
            f"{sum(1 for m in mods.values() if (m or {}).get('status') == 'done')}"
            f" done / {len(mods)} total\n"
            f"- tests: {pipeline.get('passed', 0)} passed, "
            f"{pipeline.get('failed', 0)} failed\n\n"
            "The run terminated before the report step; see ESCALATION.md / "
            "DIAGNOSTICS.md and run_trace.jsonl for the failure detail.\n",
            encoding="utf-8")

    register_finalizer(ctx.run_dir, _terminal_report)

    # D2 knowledge pre-flight (every mode — it is also report_only's
    # "stores readable" precondition, and the Rev 8 §5 curator pre-flight's
    # as-built home): a DECLARED parent layer must open READ-ONLY right
    # now, and its opening logical digest is recorded for the §8 fairness
    # gate. Fail closed — an unreadable declared layer invalidates the
    # validation world; absent declarations attest nothing and block
    # nothing.
    from ...adapters.base import expand_path as _expand
    from ...rebase_engine import knowledge_attest
    kn_cfg = (manifest.get("rebase") or {}).get("knowledge") or {}
    kn_paths: dict[str, str] = {}
    _kn_extra = ctx.settings.expansion_env()
    for kn_key in ("parent_debug_db", "parent_skills_dir"):
        raw = str(kn_cfg.get(kn_key) or "")
        expanded = _expand(raw, extra=_kn_extra)
        if raw and not expanded:
            # expand_path maps an unset ${VAR} to "" ("undeclared") — for a
            # DECLARED knowledge layer that silent downgrade is exactly the
            # unfair knowledge-bare run the §8 gate must never produce
            return StepResult(False, FailureKind.BLOCKED,
                              f"declared knowledge layer {kn_key}={raw!r} "
                              "did not expand (env var unset) — refusing an "
                              "unprovenanced run")
        kn_paths[kn_key] = expanded
    kn_paths["parent_upstream_column"] = str(
        kn_cfg.get("parent_upstream_column") or "")
    try:
        provenance = knowledge_attest.attest_layers(**kn_paths)
        # the EFFECTIVE copilot store must also open read-only (a corrupt
        # runtime db would make report_only's "stores readable"
        # precondition a lie — PR-boundary F2); an absent store is a
        # legitimate fresh world
        from ...memory.debug_memory import DebugMemory as _DM
        from ...memory.paths import KnowledgePaths as _KP0
        _repo0 = (ctx.state.get("task_spec") or {}).get("repo", "")
        _eff_db = _KP0.resolve(
            ctx.settings, _repo0,
            adapter_root=Path(ctx.settings.adapters_dir)
            / _repo0.replace("-", "_")).rebase_backend_db
        if Path(_eff_db).is_file():
            _DM.open_readonly(_eff_db)
    except Exception as exc:  # noqa: BLE001 — every shape fails closed
        return StepResult(False, FailureKind.BLOCKED,
                          "declared/effective knowledge store unreadable "
                          f"— refusing an unprovenanced run: {exc}")
    if provenance:
        # the effective skill union's collision table (name → winning
        # layer, priority runtime > adapter seed > parent): fairness
        # reviewers must see WHICH content each colliding name served
        from ...memory.paths import KnowledgePaths as _KPaths
        from ...memory.skills import SkillStore as _SStore
        repo_for_paths = (ctx.state.get("task_spec") or {}).get("repo", "")
        kp = _KPaths.resolve(
            ctx.settings, repo_for_paths,
            adapter_root=Path(ctx.settings.adapters_dir)
            / repo_for_paths.replace("-", "_"))
        collisions: dict[str, str] = {}
        layer_dirs = [("runtime", kp.skills_runtime_dir),
                      ("adapter_seed", kp.skills_seed_dir)]
        if kn_paths.get("parent_skills_dir"):
            layer_dirs.append(("parent",
                               Path(kn_paths["parent_skills_dir"])))
        for layer_name, layer_dir in layer_dirs:
            if not layer_dir.is_dir():
                continue
            for s in _SStore(layer_dir).load_all():
                collisions.setdefault(s.name, layer_name)
        sub.update({"knowledge": {"open": provenance,
                                  "skill_union": collisions}})
        ctx.trace.record(
            "knowledge_provenance", when="open",
            layers={k: v.get("digest", "") for k, v in provenance.items()})

    updates: dict = {}
    from ...adapters.base import expand_path
    upstream = ctx.state.get("upstream_origin_path", "") \
        or ctx.state.get("upstream_path", "") or expand_path(
        (manifest.get("upstream") or {}).get("repo_path", ""),
        extra=ctx.settings.expansion_env())
    if upstream:
        updates["upstream_origin_path"] = upstream
        ctx.state.setdefault("upstream_origin_path", upstream)
        upstream_remote = (ctx.state.get("upstream_remote")
                           or (manifest.get("upstream") or {}).get("remote")
                           or "origin")
        updates["upstream_remote"] = upstream_remote
        ctx.state["upstream_remote"] = upstream_remote
    baseline = _task_params(ctx).get("last_rebase_commit", "") \
        or ctx.state.get("last_rebase_upstream_commit", "")
    if baseline:
        updates["last_rebase_upstream_commit"] = baseline
    if mode == "full":
        if not upstream:
            return StepResult(False, FailureKind.BLOCKED,
                              "full mode needs the upstream checkout — set "
                              "the manifest upstream.repo_path (env var "
                              "unset?)")
        if not baseline:
            return StepResult(False, FailureKind.BLOCKED,
                              "full mode needs the last-rebase baseline — "
                              "pass --task-param last_rebase_commit=<sha>")

    # §2.2 preconditions for the prepared-tree modes: they operate on a tree
    # whose pin step already ran — refuse one that was never prepared, and
    # (remote_ci) refuse to push without the upstream-commit signal
    if mode in ("local_ci", "remote_ci"):
        repo = require_repo(ctx)
        if isinstance(repo, StepResult):
            return repo
        pin_data = ((manifest.get("rebase") or {}).get("wheel") or {}) \
            .get("pin")
        # the prepared-tree pin check applies only to adapters WITH a
        # wheel workflow (2026-08-01 audit: wheel-less stacks are legal)
        if pin_data and not wheel_mod.pin_present(
                Path(repo), wheel_mod.PinSpec.from_manifest(pin_data)):
            return StepResult(False, FailureKind.BLOCKED,
                              f"{mode} operates on a PREPARED tree, but the "
                              "CI Dockerfile carries no wheel pin — run "
                              "full mode (or pin) first")
    if mode == "remote_ci":
        upstream_commit = _task_params(ctx).get("upstream_commit", "") \
            or ctx.state.get("upstream_commit", "") \
            or (sub.read().get("upstream_commit") or "")
        if not upstream_commit:
            return StepResult(False, FailureKind.BLOCKED,
                              "remote_ci needs the upstream-commit signal "
                              "(--task-param upstream_commit=<sha>, or a "
                              "resumed run's substate) — refusing to push "
                              "an unprepared branch")
        updates["upstream_commit"] = upstream_commit

    if mode in MUTATING_MODES:
        blocked = _ensure_checkout_locks(ctx, manifest, mode)
        if blocked is not None:
            return blocked
    if mode == "full":
        # per-run DISPOSABLE upstream (Rev 8 §4): wheel checkout/reset and
        # agent shells mutate the SCRATCH clone, never the canonical tree
        scratch = _ensure_upstream_scratch(ctx)
        if isinstance(scratch, StepResult):
            return scratch
        updates["upstream_path"] = scratch

    sub.update({"phase": "init", **{k: v for k, v in
                                    mode_state_flags(mode).items()}})
    return StepResult(True, summary=f"mode={mode}",
                      outputs={"state_updates": {
                          **mode_state_flags(mode), **updates,
                          "run_id": sub.run_id}})


@step("rebase.v3_guard", "deterministic", "write_workspace",
      "Locked clean-tree gate: checkout flocks first, adapter guard policy.")
async def _v3_guard(ctx: StepContext) -> StepResult:
    """The pipeline's FIRST mutator: `workspace.guard_clean_rebase` can
    abort stale git operations and discard artifacts, so checkout
    exclusion must exist BEFORE it acts — including on a resume that
    replays the completed prelude and starts here. Also injects the
    adapter's guard policy (manifest `rebase.guard`), which the generic
    step reads only from its params — without this the adapter's
    `discard_untracked_patterns` was dead configuration."""
    manifest = _adapter_manifest(ctx)
    if isinstance(manifest, StepResult):
        return manifest
    blocked = _ensure_checkout_locks(
        ctx, manifest, _task_params(ctx).get("rebase_mode", ""))
    if blocked is not None:
        return blocked
    from .workspace import _guard_clean_rebase
    guard_cfg = dict((manifest.get("rebase") or {}).get("guard") or {})
    guard_cfg.setdefault("abort_stale_state", True)
    # the shared checkout flock we JUST took lives under <repo>/locks/ —
    # it must not read as dirt, and must never be discarded while held
    guard_cfg.setdefault("ignore_untracked_prefixes", ["locks/"])
    inner = StepContext(settings=ctx.settings, state=ctx.state,
                        params={**guard_cfg, **ctx.params},
                        run_dir=ctx.run_dir, trace=ctx.trace,
                        llm=ctx.llm, item=ctx.item)
    return await _guard_clean_rebase(inner)


@step("rebase.v3_scan", "deterministic", "read",
      "Report-only scan: manifest + drift preview, stores untouched.")
async def _v3_scan(ctx: StepContext) -> StepResult:
    repo = require_repo(ctx)
    if isinstance(repo, StepResult):
        return repo
    manifest = _adapter_manifest(ctx)
    if isinstance(manifest, StepResult):
        return manifest
    spec = ManifestSpec.from_manifest(manifest)
    built = build_manifest(Path(repo), spec)
    out = ctx.run_dir / "test_manifest.json"
    import json
    out.write_text(json.dumps(built.to_dict(), indent=1), encoding="utf-8")
    summary = (f"{len(built.jobs)} CI jobs, "
               f"{len(built.changes)} test changes")
    if built.dropped:
        summary += (f"; {len(built.dropped)} labeled step(s) with no "
                    "runnable command DROPPED (structural in a test run)")
        ctx.trace.record("manifest_steps_dropped", labels=built.dropped)
    return StepResult(True, summary=summary,
                      outputs={"state_updates": {
                          "manifest_jobs": len(built.jobs)}})


@step("rebase.v3_wheel", "deterministic", "write_workspace",
      "Pick the wheel commit and pin the CI Dockerfile.")
async def _v3_wheel(ctx: StepContext) -> StepResult:
    repo = require_repo(ctx)
    if isinstance(repo, StepResult):
        return repo
    manifest = _adapter_manifest(ctx)
    if isinstance(manifest, StepResult):
        return manifest
    blocked = _ensure_checkout_locks(
        ctx, manifest, _task_params(ctx).get("rebase_mode", ""))
    if blocked is not None:
        return blocked
    rb = manifest.get("rebase") or {}
    if not rb.get("wheel"):
        return StepResult(False, FailureKind.BLOCKED,
                          "the adapter declares no rebase.wheel workflow — "
                          "full mode needs the wheel data (report_only/"
                          "local_ci remain available)")
    wheel_spec = wheel_mod.WheelSpec.from_manifest(rb["wheel"])
    pin = wheel_mod.PinSpec.from_manifest(rb["wheel"]["pin"])
    upstream = _ensure_upstream_scratch(ctx)
    if isinstance(upstream, StepResult):
        return upstream
    venv = _target_venv(manifest, extra=ctx.settings.expansion_env())
    if not venv:
        return StepResult(False, FailureKind.BLOCKED,
                          "the wheel workflow requires the target venv "
                          "(manifest repo.venv / its env var) — refusing "
                          "to install into the copilot's own environment")
    import subprocess
    pre_head = subprocess.run(
        ["git", "-C", upstream, "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=30).stdout.strip()
    branch = (manifest.get("upstream") or {}).get("target_branch") \
        or (manifest.get("repo") or {}).get("default_branch", "main")
    try:
        found = wheel_mod.pick_wheel_commit(
            Path(upstream), branch, wheel_spec,
            probe=wheel_mod.make_arch_probe(wheel_spec),
            baseline=ctx.state.get("last_rebase_upstream_commit", ""),
            force_commit=_task_params(ctx).get("force_upstream_commit", ""))
        # the selection contract ends with the package INSTALLED at the
        # picked commit in the TARGET venv — otherwise stale extensions or
        # a different installed version drive every later module check.
        # INSTALL BEFORE PIN: the pin is this step's only mutation of the
        # guard-checked target tree, while the install carries the real
        # failure modes (network, venv, broken extensions). Pinning first
        # stranded a dirty tree on install failure, and the NEXT run's
        # guard then refused to start (run-20260825-095137) — mutating
        # last keeps every failure path clean.
        installed = wheel_mod.ensure_wheel_installed(
            Path(upstream), found, wheel_spec,
            python=str(Path(venv) / "bin" / "python"),
            install_log=ctx.run_dir / "wheel_install.log",
            import_check_log=ctx.run_dir / "wheel_import_check.log",
            pre_checkout_head=pre_head)
        wheel_mod.pin_dockerfile(Path(repo), found, pin)
    except wheel_mod.WheelPickError as exc:
        return StepResult(False, FailureKind.BLOCKED, str(exc))
    except wheel_mod.PinError as exc:
        return StepResult(False, FailureKind.BLOCKED, str(exc))
    except wheel_mod.WheelInstallError as exc:
        return StepResult(False, FailureKind.BLOCKED, str(exc))
    _substate(ctx).set_field("upstream_commit", found)
    return StepResult(True,
                      summary=f"wheel commit {found[:12]} "
                              + ("(reinstalled)" if installed
                                 else "(install healthy, skipped)"),
                      outputs={"state_updates": {"upstream_commit": found}})


@step("rebase.v3_assign", "deterministic", "read",
      "Classify upstream commits into modules; publish the wave lists.")
async def _v3_assign(ctx: StepContext) -> StepResult:
    manifest = _adapter_manifest(ctx)
    if isinstance(manifest, StepResult):
        return manifest
    upstream = _ensure_upstream_scratch(ctx)
    if isinstance(upstream, StepResult):
        return upstream
    baseline = ctx.state.get("last_rebase_upstream_commit", "")
    if not baseline:
        return StepResult(False, FailureKind.BLOCKED,
                          "last-rebase baseline not in state")
    cfg = Phase1Config(
        upstream_repo=Path(upstream), target_repo=Path("."),
        log_dir=ctx.run_dir, baseline_commit=baseline,
        base_class_watch_paths=tuple((manifest.get("rebase") or {})
                                     .get("base_class_watch_paths") or ()))
    modules = manifest.get("modules") or {}
    # PATH SYNC before assignment (parent 35_sync_module_paths): after an
    # upstream rename, `git log -- <missing-path>` yields no commits and
    # the module would be SILENTLY marked skippable — retarget the static
    # upstream_paths against the live tree first (existence-filtered,
    # never-empty)
    from ...rebase_engine.path_sync import sync_path_map
    module_paths = {
        m: tuple(paths) for m, paths in sync_path_map(
            Path(upstream),
            {m: list((s or {}).get("upstream_paths") or ())
             for m, s in modules.items()}).items()}
    dropped = {m: sorted(set((modules[m] or {}).get("upstream_paths") or ())
                         - set(module_paths[m])) for m in modules}
    dropped = {m: d for m, d in dropped.items() if d}
    if dropped:
        ctx.trace.record("upstream_path_sync_dropped", dropped=dropped)
    from ...rebase_engine.assign import AssignError
    try:
        result = run_commit_assignment(cfg, module_paths, _substate(ctx))
    except AssignError as exc:
        return StepResult(False, FailureKind.BLOCKED, str(exc))
    active = [m for m, s in result.skip.items() if not s]
    # wave ordering is a dependency contract (manifest `wave`, parent
    # parity): wave 1 runs first; the wave gate empties wave 2 on failure
    wave1 = [m for m in modules if m in active
             and int((modules[m] or {}).get("wave") or 2) == 1]
    wave2 = [m for m in modules if m in active and m not in wave1]
    return StepResult(True,
                      summary=f"{result.total_commits} commits over "
                              f"{len(active)} active modules "
                              f"(wave1={len(wave1)}, wave2={len(wave2)})",
                      outputs={"state_updates": {
                          "active_modules": active,
                          "wave1_modules": wave1,
                          "wave2_modules": wave2}})


@step("rebase.v3_wave_gate", "deterministic", "read",
      "Wave gate: a wave-1 failure empties wave 2 (or ESCALATEs on halt).")
async def _v3_wave_gate(ctx: StepContext) -> StepResult:
    """Rev 8 §2.2: the gate ALWAYS runs between the waves. Any wave-1
    module failure empties `wave2_modules` (dependents must not build on a
    broken base); `halt_on_module_failure=true` escalates instead."""
    sub = _substate(ctx)
    data = sub.read()
    wave1 = ctx.state.get("wave1_modules") or []
    failed = [m for m in wave1
              if ((data.get("modules") or {}).get(m) or {})
              .get("status") == "failed"]
    if not failed:
        return StepResult(True, summary="wave 1 clean; wave 2 proceeds")
    if _task_params(ctx).get("halt_on_module_failure"):
        return StepResult(False, FailureKind.ESCALATE,
                          "halt_on_module_failure: wave-1 module(s) failed: "
                          + ", ".join(failed))
    ctx.trace.record("wave_gate", failed_wave1=failed, wave2_emptied=True)
    return StepResult(True,
                      summary=f"wave-1 failure(s) ({', '.join(failed)}) — "
                              "wave 2 emptied",
                      outputs={"state_updates": {"wave2_modules": []}})


async def _run_debug_agent(ctx: StepContext, manifest: dict, module: str,
                           slug: str, traceback_text: str) -> str:
    """One regression-debug agent attempt for a failing test's module —
    the configured state-injected backend the loop's `debug_fn` invokes.
    Returns "done" when the agent claims completion (the caller re-runs the
    test; only a green re-run counts as fixed), "not_done" when the agent
    honestly gave up, or "error:<detail>" when the DEBUG MACHINERY itself
    failed (structural, per the §2.3 taxonomy — agent dispatch failures are
    never ordinary test failures)."""
    client = _tier_client(ctx)
    if isinstance(client, StepResult):
        return "error:debug backend unavailable"
    client, target = client
    from ...rebase_engine.agent_loop import run_agent_loop
    from ...rebase_engine.prompt_builder import (ModulePromptData,
                                                 build_debug_prompt)
    from ...rebase_engine.rebase_tools import (RebasePaths,
                                               build_rebase_tools,
                                               load_tool_schemas)
    repo_name = (ctx.state.get("task_spec") or {}).get("repo", "")
    adapter_dir = Path(ctx.settings.adapters_dir) / repo_name.replace("-", "_")
    data = ModulePromptData.load(adapter_dir / "rebase")
    defs = load_tool_schemas(adapter_dir / "rebase" / "tool_schemas.json")
    repo_root = ctx.state.get("repo_path", "")
    paths = RebasePaths(omni_path=repo_root,
                        vllm_path=ctx.state.get("upstream_path", ""),
                        env=_agent_shell_env(ctx, manifest, repo_root,
                                             adapter_dir),
                        baseline_ref=_baseline_ref(manifest),
                        test_roots=_test_roots(manifest))
    tools = build_rebase_tools(
        defs, paths, _build_backends(ctx, manifest, repo_root, target))
    prompt = build_debug_prompt(module or slug, traceback_text,
                                data.debug_prompt_template_live
                                or data.debug_prompt_template, slug)
    agent_log = ctx.run_dir / "agents" / f"debug-{slug}.log"
    agent_log.parent.mkdir(parents=True, exist_ok=True)
    ctx.trace.record("debug_attempt", slug=slug, module=module,
                     model=target.model)
    try:
        result = await run_agent_loop(
            client, prompt, model=target.model, tool_defs=defs,
            extra_tools=tools,
            scope=_module_scope(repo_root, module, manifest,
                                run_dir=ctx.run_dir),
            trace=ctx.trace, require_plan_review=False,
            model_aliases=ctx.settings.model_aliases,
            model_mismatch_policy=ctx.settings.model_mismatch_policy,
            agent_log=str(agent_log))
    except Exception as exc:  # noqa: BLE001 - a debug crash is STRUCTURAL
        ctx.trace.record("debug_attempt_error", slug=slug, error=str(exc))
        return f"error:debug agent crashed: {exc}"
    if result.get("done"):
        return "done"
    # done=False from the loop is ALWAYS machinery/budget (fatal auth,
    # stream error, model mismatch, truncation abort, max turns) — an agent
    # that finishes, even unsuccessfully, returns done=True. Structural.
    return "error:agent loop did not complete: " \
           + (result.get("text") or "")[:200]


def _watchdog_collaborators(ctx: StepContext):
    """Two-tier watchdog collaborators for the local test loop (design D4).

    `record_fn` appends every Tier-2 decision to the RUN DIR's decision log
    with a stable identity `(run, attempt, job_key, seq)` — the curator's
    exactly-once harvest moves them into the state-dir log later (the run
    dir is the only writer target here, so a crashed run never half-writes
    shared state). `review_fn` is the ECO-tier LLM reviewer (Rev 8 §7:
    90 s cap, default-CONTINUE on any error/stall — a reviewer problem may
    never kill a test the patterns alone would have let live).
    `report_fn` writes the per-test watchdog report artifact (parent
    parity with the standalone pytest wrapper)."""
    import itertools

    from ...testing import watchdog_learn

    import uuid

    run_id = str(ctx.state.get("run_id", "") or ctx.run_dir.name)
    decisions = ctx.run_dir / "watchdog_decisions.jsonl"
    seq = itertools.count(1)
    # collision-resistant attempt id: the module agents' pytest wrapper
    # shares this decision file with its own restarting seq counters —
    # pid+seconds can collide across same-second retries or pid reuse and
    # the harvest would wrongly dedup a REAL decision away
    attempt = f"loop-{uuid.uuid4().hex[:12]}"

    def record_fn(pattern: str, verdict: str, test_name: str) -> None:
        watchdog_learn.record(decisions, pattern=pattern, verdict=verdict,
                              test=test_name, run=run_id, attempt=attempt,
                              job_key=test_name, seq=next(seq))

    def report_fn(test_name: str, pattern: str, detail: str) -> None:
        report = ctx.run_dir / "tests" / f"{test_name}.watchdog_report"
        report.parent.mkdir(parents=True, exist_ok=True)
        with open(report, "a", encoding="utf-8") as f:
            f.write(f"pattern={pattern}\n{detail}\n")

    client_holder: list = []

    def _client():
        if not client_holder:
            from anthropic import Anthropic

            target = ctx.settings.tier_target("eco")
            # max_retries=0: the 90 s cap is a WALL cap — the SDK's
            # default retry loop would multiply it
            ckw: dict = {"api_key": target.api_key, "max_retries": 0}
            if target.base_url:
                ckw["base_url"] = target.base_url
            client_holder.append((Anthropic(**ckw), target.model))
        return client_holder[0]

    def review_fn(test_name: str, snippet: str) -> str:
        try:
            client, model = _client()
            msg = client.messages.create(
                model=model, max_tokens=16, timeout=90.0,
                messages=[{"role": "user", "content":
                           "You are a CI test watchdog reviewing a "
                           "suspicious log excerpt from a long-running "
                           "test. KILL means the process is unrecoverable "
                           "(hung, crashed, device error) and should be "
                           "terminated now; CONTINUE means let it run. "
                           "Answer with exactly one word, KILL or "
                           "CONTINUE.\n\n"
                           f"test: {test_name}\nlog excerpt:\n"
                           f"{snippet[:4000]}"}])
            text = "".join(getattr(b, "text", "")
                           for b in msg.content).strip().upper()
            return "KILL" if text.startswith("KILL") else "CONTINUE"
        except Exception:  # noqa: BLE001 — reviewer trouble never kills
            return "CONTINUE"

    return record_fn, review_fn, report_fn


@step("rebase.v3_test_loop", "script", "write_workspace",
      "The local test loop: run, baseline-compare, debug regressions.")
async def _v3_test_loop(ctx: StepContext) -> StepResult:
    """Runs the manifest jobs through the PR1 runner with main-baseline
    comparison. Local-loop contract (parent parity): nightly-sourced jobs
    are excluded; job env pairs reach the child; the baseline run prepends
    the worktree to PYTHONPATH so main's files execute against main's code;
    timeouts/watchdog kills are recorded as INFRASTRUCTURE failures
    (structural for the push gate, never assertion pass-through); an empty
    manifest marks `manifest_empty` instead of vacuously passing. The
    regression DEBUG agent runs on the tier backend — absent one,
    regressions are recorded as failures with a declared `capability_gap`
    (fail closed, never silently skipped)."""
    repo = require_repo(ctx)
    if isinstance(repo, StepResult):
        return repo
    manifest = _adapter_manifest(ctx)
    if isinstance(manifest, StepResult):
        return manifest
    blocked = _ensure_checkout_locks(
        ctx, manifest, _task_params(ctx).get("rebase_mode", ""))
    if blocked is not None:
        return blocked
    spec = ManifestSpec.from_manifest(manifest)
    built = build_manifest(Path(repo), spec)
    sub = _substate(ctx)

    jobs = built.to_dict()["jobs"]
    local_jobs = [j for j in jobs if j.get("source") != "nightly"]
    if len(local_jobs) < len(jobs):
        ctx.trace.record("nightly_jobs_excluded",
                         count=len(jobs) - len(local_jobs))
    # labeled steps the builder DROPPED for lack of a runnable command are
    # STRUCTURAL on every path — including the all-dropped one, where the
    # generic manifest_empty block must still carry the labels
    dropped_infra = [f"step '{label}': no runnable command"
                     for label in built.dropped]
    if dropped_infra:
        ctx.trace.record("manifest_steps_dropped", labels=built.dropped)
    if not local_jobs:
        # zero runnable jobs is corrupt/empty manifest territory — the push
        # gate must block, not sail through a vacuous "0 failed"
        sub.update({"manifest_empty": True,
                    "tests": {"pipeline": {"passed": 0, "failed": 0,
                                           "failed_tests": [],
                                           "skipped": 0},
                              "infra_failures": dropped_infra}})
        return StepResult(True,
                          summary="no runnable local jobs — manifest_empty "
                                  "set (push gate blocks)"
                                  + (f"; {len(dropped_infra)} dropped "
                                     "step(s) recorded"
                                     if dropped_infra else ""),
                          outputs={"state_updates": {"phase3_failed": []}})

    import os
    from ...testing.runner import TestJob, TestRunner
    from ...testing.watchdog import WatchdogPatterns
    if _venv_declared(manifest) and not _target_venv(
            manifest, extra=ctx.settings.expansion_env()):
        return StepResult(False, FailureKind.BLOCKED,
                          "the adapter DECLARES a runtime venv (repo.venv) "
                          "but its env var is unset — raw manifest commands "
                          "would execute against the copilot's own "
                          "environment")
    if not _venv_declared(manifest):
        ctx.trace.record("capability_note", capability="repo.venv",
                         detail="adapter declares no runtime venv — test "
                                "jobs run with the inherited environment")
    rb = manifest.get("rebase") or {}
    repo_slug = ((ctx.state.get("task_spec") or {}).get("repo", ""))
    adapter_dir = Path(ctx.settings.adapters_dir) / repo_slug.replace("-", "_")
    pat_file = adapter_dir / "testing" / "watchdog_patterns.yaml"
    # two-tier pattern load (design D4): adapter seed ∪ runtime overlay —
    # the overlay (learned noise) only ever appends; absent ⇒ seed-only,
    # byte-identical to the pre-wiring behavior
    from ...memory.paths import KnowledgePaths as _KP
    overlay_path = _KP.resolve(ctx.settings, repo_slug,
                               adapter_root=adapter_dir).watchdog_overlay
    patterns = WatchdogPatterns.from_yaml(pat_file, overlay=overlay_path) \
        if pat_file.is_file() else None
    record_fn, review_fn, report_fn = _watchdog_collaborators(ctx)

    def notify_download(key: str, repo_id: str) -> None:
        ctx.trace.record("model_download_expected", job=key, repo=repo_id)

    runner = TestRunner(
        repo_root=Path(repo), tests_dir=ctx.run_dir / "tests",
        patterns=patterns, gpu_lock_dir=ctx.run_dir / "gpu_lock",
        artifact_globs=list((rb.get("testing") or {})
                            .get("artifact_globs") or []),
        cuda_visible_devices=os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        record_fn=record_fn, review_fn=review_fn, report_fn=report_fn,
        notify_download=notify_download)
    jobs_by_slug = {j["slug"]: j for j in local_jobs}

    def _job(slug: str) -> TestJob:
        return manifest_job_to_test_job(jobs_by_slug[slug])

    def _to_result(outcome) -> tl.TestRunResult:
        infra = "timeout" if outcome.timed_out else \
            ("watchdog kill" if outcome.watchdog_triggered else "")
        return tl.TestRunResult(rc=outcome.rc, skipped=outcome.skipped,
                                skip_reason=outcome.skip_reason,
                                output=outcome.log_file, infra=infra)

    def run_fn(slug: str) -> tl.TestRunResult:
        # a non-RUNNABLE command (empty or comment-only) must NEVER read as
        # a pass — bash exits 0 on both (the parent's §10 false-pass
        # mechanism, DRIFT_TRIAGE #4); the builder already drops such
        # steps, so one arriving here is corruption and classifies
        # STRUCTURAL
        from ...rebase_engine.test_manifest import is_runnable_command
        if not is_runnable_command(jobs_by_slug[slug].get("command", "")):
            return tl.TestRunResult(rc=1, infra="no runnable command")
        # tests INHERIT the process env (Rev 8 §6: inherit-plus-overlay;
        # the credential scrub applies to agent shells only) with the
        # TARGET venv + CUDA + HF_HOME overlay — raw manifest commands
        # must resolve inside the target repo's runtime, never ours
        return _to_result(runner.run(_job(slug),
                                     _target_test_env(ctx.settings, manifest)))

    worktree_path = ctx.run_dir / "main_worktree"

    def baseline_fn(slug: str) -> tl.TestRunResult | None:
        wt = tl.ensure_main_worktree(Path(repo), worktree_path,
                                     base_ref=_baseline_ref(manifest))
        if wt is None:
            return None
        wt_runner = TestRunner(
            repo_root=wt, tests_dir=ctx.run_dir / "tests",
            patterns=patterns, gpu_lock_dir=ctx.run_dir / "gpu_lock",
            cuda_visible_devices=runner.cuda,
            record_fn=record_fn, review_fn=review_fn, report_fn=report_fn,
            notify_download=notify_download)
        # main's files must import main's code: prepend the worktree so the
        # baseline never executes against rebase dependencies (parent's
        # baseline PYTHONPATH override) — same TARGET env otherwise; infra
        # outcomes propagate (a baseline timeout must never read as "fails
        # on main too")
        env = _target_test_env(ctx.settings, manifest, pythonpath_prepend=str(wt))
        return _to_result(wt_runner.run(_job(slug), env, baseline=True))

    async def debug_fn(slug: str, label: str, rc: int,
                       output: str) -> bool | str:
        tier = _tier_client(ctx)
        if isinstance(tier, StepResult):
            ctx.trace.record("capability_gap",
                             capability="rebase.debug_agent",
                             detail=f"no agent backend; {slug} regression "
                                    "recorded as a structural failure")
            return "debug backend unavailable (capability_gap)"
        traceback_text = ""
        try:
            log_path = Path(output)
            if log_path.is_file():
                traceback_text = "\n".join(
                    log_path.read_text(encoding="utf-8",
                                       errors="replace")
                    .splitlines()[-200:])
        except OSError:
            pass
        # attempt-scoped snapshot (parent parity): a rejected/unverified
        # debug patch must NOT stay in the tree — assertion failures pass
        # the push gate by default, so leftover edits would eventually be
        # committed and pushed
        snap, untracked = tl.snapshot_worktree(Path(repo))
        verdict = await _run_debug_agent(
            ctx, manifest, jobs_by_slug[slug].get("module", ""), slug,
            traceback_text or f"{label} failed with rc={rc}")
        if verdict.startswith("error:"):
            tl.restore_worktree(Path(repo), snap, untracked)
            return f"debug agent failed: {verdict[6:]}"
        rerun = run_fn(slug)             # only a green re-run counts
        if rerun.infra:
            tl.restore_worktree(Path(repo), snap, untracked)
            return f"post-debug re-run {rerun.infra}"
        if rerun.skipped:
            tl.restore_worktree(Path(repo), snap, untracked)
            return "post-debug re-run skipped (unverifiable fix)"
        if rerun.rc != 0:
            tl.restore_worktree(Path(repo), snap, untracked)
            return False
        return True

    result = await tl.run_test_loop(
        local_jobs, substate=sub, run_fn=run_fn,
        baseline_fn=baseline_fn, debug_fn=debug_fn,
        visible_gpus=len([d for d in runner.cuda.split(",") if d.strip()
                          and not d.strip().startswith("-")]))
    tl.remove_main_worktree(Path(repo), worktree_path)
    infra = result["infra_failures"] + dropped_infra
    sub.update({"tests": {
        "pipeline": {
            "passed": result["passed"], "failed": result["failed"],
            "failed_tests": result["failed_tests"],
            "skipped": len(result["skipped_tests"])},
        "infra_failures": infra}})
    return StepResult(True,
                      summary=f"{result['passed']} passed, "
                              f"{result['failed']} failed "
                              f"({len(infra)} infra), "
                              f"{len(result['skipped_tests'])} skipped",
                      outputs={"state_updates": {
                          "phase3_failed": result["failed_tests"]}})


def _halt_on_phase3(ctx: StepContext, sub: Substate) -> StepResult | None:
    """`halt_on_phase3_failures=true`: the operator explicitly asked the run
    to STOP at the end of phase 3 (loop + precommit — precommit still runs)
    instead of proceeding toward push/remote CI with failures aboard.
    ESCALATE (needs-human, exit 3) when any phase-3 failure exists."""
    if not _task_params(ctx).get("halt_on_phase3_failures"):
        return None
    data = sub.read()
    tests = data.get("tests") or {}
    problems: list[str] = []
    problems.extend((tests.get("pipeline") or {}).get("failed_tests") or [])
    problems.extend(tests.get("infra_failures") or [])
    if ((tests.get("precommit") or {}).get("result")) == "failed":
        problems.append("precommit red")
    if not problems:
        return None
    return StepResult(False, FailureKind.ESCALATE,
                      "halt_on_phase3_failures: "
                      f"{len(problems)} phase-3 failure(s) — halting "
                      "before any push/CI: " + "; ".join(problems[:5]))


_PRECOMMIT_HOOK_ID_RE = None  # compiled lazily


def _precommit_failed_hooks(outcome) -> frozenset | None:
    """The set of pre-commit hook ids that FAILED, parsed from the run's
    log (`- hook id: <id>` lines follow each Failed hook), or None when
    the log is missing/unparseable - callers must then treat the failure
    as structural rather than guess."""
    global _PRECOMMIT_HOOK_ID_RE
    import re
    if _PRECOMMIT_HOOK_ID_RE is None:
        _PRECOMMIT_HOOK_ID_RE = re.compile(r"^- hook id: (\S+)$", re.M)
    if not outcome.log_file:
        return None
    try:
        text = Path(outcome.log_file).read_text(encoding="utf-8",
                                                errors="replace")
    except OSError:
        return None
    ids = frozenset(_PRECOMMIT_HOOK_ID_RE.findall(text))
    return ids or None


def _precommit_baseline(ctx: StepContext, repo: str, pc: dict,
                        command: str, manifest: dict):
    """(rc, failed-hook-ids) of the same precommit command on the PRE-RUN
    tree (a detached worktree of HEAD - module-agent changes are
    uncommitted at phase 3.2), or None when the baseline cannot be built
    or proves nothing (timeout). Same doctrine as the test loop's
    pre-existing-failure baseline and remote CI's baseline pipeline: red
    the run did NOT introduce is recorded distinctly, never invented into
    a structural block that would gate every push on repo-wide debt
    forever (live full run 2026-08-23: 31 upstream-main test files
    missing CI marks)."""
    import subprocess
    from ...testing.runner import TestJob, TestRunner
    wt = ctx.run_dir / "precommit_baseline_wt"
    add = subprocess.run(["git", "-C", str(repo), "worktree", "add",
                          "--detach", str(wt), "HEAD"],
                         capture_output=True, text=True)
    if add.returncode != 0:
        ctx.trace.record("precommit_baseline_unavailable",
                         error=(add.stderr or add.stdout).strip()[:500])
        return None
    try:
        runner = TestRunner(repo_root=wt, tests_dir=ctx.run_dir / "tests",
                            gpu_lock_dir=ctx.run_dir / "gpu_lock")
        job = TestJob(key="__precommit_baseline__", command=command,
                      timeout_sec=float(pc.get("timeout_sec") or 600),
                      min_gpus=0, gpu_lock=False)
        outcome = runner.run(job, _target_test_env(ctx.settings, manifest))
        if outcome.timed_out:
            # a timed-out probe proves nothing about the baseline - treat
            # it as unavailable so the original red stays STRUCTURAL
            ctx.trace.record("precommit_baseline_unavailable",
                             error="baseline probe timed out")
            return None
        failed = _precommit_failed_hooks(outcome) if outcome.rc != 0 \
            else frozenset()
        ctx.trace.record("precommit_baseline", rc=outcome.rc,
                         failed_hooks=sorted(failed or ()))
        return outcome.rc, failed
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove",
                        "--force", str(wt)], capture_output=True)


@step("rebase.v3_precommit", "script", "write_workspace",
      "Phase-3.2 precommit workflow: run, retry once, record in substate.")
async def _v3_precommit(ctx: StepContext) -> StepResult:
    """The parent's Phase 3.2 (always runs after the local test loop): the
    adapter-declared precommit command, with one retry — auto-fix hooks
    (formatters, end-of-file fixers) often pass on the second run. The
    result is SUBSTATE DATA: `tests.precommit.result` is what the push gate
    reads ("failed" is a structural block, §2.3), so the step itself
    returns ok either way. write_workspace risk: hooks auto-fix files in
    place. An adapter with no declared precommit records `not_declared`
    (data-driven — never invented, never silently green-as-passed)."""
    repo = require_repo(ctx)
    if isinstance(repo, StepResult):
        return repo
    manifest = _adapter_manifest(ctx)
    if isinstance(manifest, StepResult):
        return manifest
    blocked = _ensure_checkout_locks(
        ctx, manifest, _task_params(ctx).get("rebase_mode", ""))
    if blocked is not None:
        return blocked
    sub = _substate(ctx)
    pc = (manifest.get("rebase") or {}).get("precommit") or {}
    command = str(pc.get("command") or "")
    if not command:
        ctx.trace.record("capability_gap", capability="rebase.precommit",
                         detail="no precommit command declared in the "
                                "adapter manifest")
        sub.update({"tests": {"precommit": {"result": "not_declared",
                                            "attempt": 0}}})
        halted = _halt_on_phase3(ctx, sub)
        if halted is not None:
            return halted
        return StepResult(True, summary="precommit: not declared (recorded)")

    from ...testing.runner import TestJob, TestRunner
    runner = TestRunner(repo_root=Path(repo),
                        tests_dir=ctx.run_dir / "tests",
                        gpu_lock_dir=ctx.run_dir / "gpu_lock")
    job = TestJob(key="__precommit__", command=command,
                  timeout_sec=float(pc.get("timeout_sec") or 600),
                  min_gpus=0, gpu_lock=False)
    outcome = runner.run(job, _target_test_env(ctx.settings, manifest))
    attempt = 0
    if outcome.rc != 0 and pc.get("retry_once", True):
        # parity: many hooks fix files in place; a second run then passes.
        # NO `git add -A` here (parent-documented: indiscriminate staging is
        # how stray artifacts ended up in rebase commits)
        attempt = 1
        outcome = runner.run(job, _target_test_env(ctx.settings, manifest))
    passed = outcome.rc == 0 and not outcome.timed_out
    result = "passed" if passed else "failed"
    baseline_rc = None
    # A current-run TIMEOUT is infrastructure, structural regardless of
    # the baseline; only a plain red is compared. `failed_preexisting`
    # requires the current failed-hook set to be a SUBSET of the
    # baseline's - a new failure riding on old debt stays structural
    # (hook-id granularity: the finest level pre-commit output names
    # reliably). Unparseable logs on either side stay structural.
    if not passed and not outcome.timed_out:
        current_failed = _precommit_failed_hooks(outcome)
        baseline = _precommit_baseline(ctx, repo, pc, command, manifest)
        if baseline is not None:
            baseline_rc, baseline_failed = baseline
            if (baseline_rc != 0 and current_failed and baseline_failed
                    and current_failed <= baseline_failed):
                result = "failed_preexisting"
    sub.update({"tests": {"precommit": {
        "result": result, "attempt": attempt, "baseline_rc": baseline_rc,
        "last_log": outcome.log_file or None}}})
    halted = _halt_on_phase3(ctx, sub)
    if halted is not None:
        return halted
    if result == "failed_preexisting":
        return StepResult(True,
                          summary=f"precommit red PRE-EXISTS the run "
                                  f"(baseline rc={baseline_rc}); gate "
                                  "flags, does not block")
    return StepResult(True,
                      summary="precommit "
                              + ("passed" if passed else
                                 f"FAILED (rc={outcome.rc}; push gate "
                                 "blocks)"))


@step("rebase.v3_push_gate", "deterministic", "read",
      "Fail-closed push gate: structural failures block; assertions flag.")
async def _v3_push_gate(ctx: StepContext) -> StepResult:
    from ...rebase_engine.modes import ModeConflictError
    sub = _substate(ctx)
    try:
        decision = evaluate_push_gate(sub.read(), _task_params(ctx))
    except ModeConflictError as exc:
        return StepResult(False, FailureKind.BLOCKED, str(exc))
    if not decision.allowed:
        return StepResult(False, FailureKind.FORBIDDEN,
                          "push gate: " + "; ".join(decision.reasons))
    summary = "push gate open"
    if decision.flagged:
        summary += f" ({len(decision.flagged)} flagged test failure(s))"
    if decision.reasons:
        # "explicit and logged" (Rev 8 §2.3): a push_with_failures override
        # must be UNMISTAKABLE in the trace and report — never
        # indistinguishable from a genuinely clean gate
        ctx.trace.record("push_gate_override",
                         reasons=list(decision.reasons))
        summary += (f"; OVERRIDDEN structural failure(s): "
                    + "; ".join(decision.reasons))
    return StepResult(True, summary=summary,
                      outputs={"state_updates": {
                          "push_gate_flagged": list(decision.flagged),
                          "push_gate_overrides": list(decision.reasons)}})


@step("rebase.v3_finalize", "deterministic", "read",
      "Transition-table terminal row: substate failures ⇒ needs-human.")
async def _v3_finalize(ctx: StepContext) -> StepResult:
    """Rev 8 §3.1 row 2: all steps ok but substate carries failed
    modules/tests ⇒ the run terminates needs-human via the REUSED blocked /
    exit-3 semantics (Decision 4 — no new exit code). Runs AFTER the report
    step, so RUN_REPORT exists and the BLOCKED outcome only adds the
    ESCALATION artifact. The finalizer never upgrades a failure into
    success; substate `phase` records the honest terminal name."""
    sub = _substate(ctx)
    data = sub.read()
    failures: list[str] = []
    for module, spec in (data.get("modules") or {}).items():
        if (spec or {}).get("status") == "failed":
            failures.append(f"module {module}")
    tests = data.get("tests") or {}
    failed_tests = ((tests.get("pipeline") or {}).get("failed_tests")) or []
    failures.extend(f"test {t}" for t in failed_tests)
    failures.extend(f"infra {i}" for i in tests.get("infra_failures") or [])
    if ((tests.get("precommit") or {}).get("result")) == "failed":
        failures.append("precommit red")
    if data.get("manifest_empty"):
        failures.append("manifest empty")
    ci = data.get("ci") or {}
    ci_result = str(ci.get("result") or "")
    if ci_result and ci_result != "passed":
        failures.append(f"remote CI {ci_result}"
                        + (f": {ci.get('reason')}" if ci.get("reason")
                           else ""))
        failures.extend(f"ci job {name}" for name in ci.get("unfixed") or [])
    sub.update({"phase": "needs_human" if failures else "done"})
    if failures:
        return StepResult(False, FailureKind.BLOCKED,
                          "completed with failures needing a human: "
                          + "; ".join(failures[:10]))
    return StepResult(True, summary="substate clean; phase=done")


@step("rebase.v3_module_rebase", "script", "write_workspace",
      "One module's rebase agent (foreach over the wave lists).")
async def _v3_module_rebase(ctx: StepContext) -> StepResult:
    """The PR4a/PR4b assembly: golden-parity prompt → gated loop → substate
    outcome, per module via foreach fan-out. Fan-out siblings SERIALIZE on a
    per-run lock (they all mutate the same checkout; the parent runs wave
    members sequentially). Requires the tier-resolved backend; absent one
    this BLOCKS with a declared capability_gap (never a silent skip). Module
    failures are substate data (the wave gate and the finalize row consume
    them), so the step itself returns ok."""
    module = str(ctx.item or "")
    if not module:
        return StepResult(False, FailureKind.BLOCKED,
                          "v3_module_rebase needs a foreach module item")
    # substate-first idempotency (crash-window contract): a module whose
    # durable status is already terminal-done short-circuits on re-entry —
    # a crash between the substate write and the executor checkpoint must
    # never run the agent (and apply its edits) twice
    sub = _substate(ctx)
    prior = ((sub.read().get("modules") or {}).get(module)) or {}
    if prior.get("status") in ("done", "skipped"):
        return StepResult(True,
                          summary=f"{module}: already {prior['status']} "
                                  "(substate short-circuit)",
                          outputs={"state_updates": {
                              f"module_{module}_status": prior["status"]}})
    manifest = _adapter_manifest(ctx)
    if isinstance(manifest, StepResult):
        return manifest
    blocked = _ensure_checkout_locks(
        ctx, manifest, _task_params(ctx).get("rebase_mode", ""))
    if blocked is not None:
        return blocked
    client = _tier_client(ctx)
    if isinstance(client, StepResult):
        return client
    client, target = client
    from ...rebase_engine.hooks import load_hooks
    from ...rebase_engine.module_rebase import ModuleRunConfig, rebase_module
    from ...rebase_engine.prompt_builder import ModulePromptData
    from ...rebase_engine.rebase_tools import (RebasePaths,
                                               build_rebase_tools,
                                               load_tool_schemas)
    repo_name = (ctx.state.get("task_spec") or {}).get("repo", "")
    adapter_dir = Path(ctx.settings.adapters_dir) / repo_name.replace("-", "_")
    data = ModulePromptData.load(adapter_dir / "rebase")
    defs = load_tool_schemas(adapter_dir / "rebase" / "tool_schemas.json")
    repo_root = ctx.state.get("repo_path", "")
    paths = RebasePaths(omni_path=repo_root,
                        vllm_path=ctx.state.get("upstream_path", ""),
                        env=_agent_shell_env(ctx, manifest, repo_root,
                                             adapter_dir),
                        baseline_ref=_baseline_ref(manifest),
                        test_roots=_test_roots(manifest))
    tools = build_rebase_tools(
        defs, paths, _build_backends(ctx, manifest, repo_root, target))
    hooks = load_hooks(adapter_dir, manifest)
    # the LIVE test plan (authoritative CI obligations + upstream test
    # changes) goes into the module prompt — reuse the scan artifact when
    # present, else build fresh
    test_plan: dict | None = None
    try:
        import json as _json
        plan_file = ctx.run_dir / "test_manifest.json"
        if plan_file.is_file():
            plans = (_json.loads(plan_file.read_text(encoding="utf-8"))
                     .get("module_plans") or {})
            raw = plans.get(module)
            if raw:
                test_plan = {"ci_tests": raw.get("ci_tests") or [],
                             "upstream_changes":
                                 raw.get("upstream_changes") or [],
                             "omni_specific":
                                 raw.get("omni_specific") or []}
        else:
            built = build_manifest(Path(repo_root),
                                   ManifestSpec.from_manifest(manifest))
            plan_file.write_text(_json.dumps(built.to_dict(), indent=1),
                                 encoding="utf-8")
            mp = built.for_module(module)
            test_plan = {"ci_tests": [j.slug for j in mp.ci_tests],
                         "upstream_changes": [
                             {"path": c.path, "type": c.change_type,
                              "new_path": c.new_path}
                             for c in mp.upstream_changes],
                         "omni_specific": mp.omni_specific_tests}
    except Exception:  # noqa: BLE001 - plan enrichment is best-effort
        ctx.trace.record("module_test_plan_unavailable", module=module)
    import os
    config = ModuleRunConfig(
        vllm_path=paths.vllm_path, omni_path=paths.omni_path,
        script_dir=str(adapter_dir / "rebase"), model=target.model,
        log_dir=str(ctx.run_dir),
        signal_dir=str(ctx.run_dir / "signals"),
        last_rebase_vllm_commit=ctx.state.get("last_rebase_upstream_commit",
                                              ""),
        # the prompt's CUDA/HF facts must describe THIS host, not the
        # dataclass defaults
        cuda_devices=os.environ.get("CUDA_VISIBLE_DEVICES", "0,1"),
        hf_home=os.environ.get("HF_HOME", "/model"),
        model_aliases=ctx.settings.model_aliases,
        model_mismatch_policy=ctx.settings.model_mismatch_policy,
        baseline_ref=_baseline_ref(manifest),
        # harness delegation: "api" (default) keeps the in-process loop
        backend=getattr(ctx.settings, "rebase_backend", "api") or "api",
        backend_model=getattr(ctx.settings, "rebase_backend_model", "") or "",
        settings=ctx.settings,
        manifest_path=str(adapter_dir / "manifest.yaml"),
        paths_spec={k: val for k, val in _dc_asdict(paths).items()
                    if k != "env"},
        repo=str((ctx.state.get("task_spec") or {}).get("repo") or ""),
        state_slice={"task_spec": ctx.state.get("task_spec") or {},
                     "run_id": ctx.state.get("run_id", ""),
                     "target_branch": (manifest.get("upstream") or {})
                     .get("target_branch") or (manifest.get("repo") or {})
                     .get("default_branch", "main"),
                     "last_rebase_upstream_commit": ctx.state.get(
                         "last_rebase_upstream_commit", ""),
                     "upstream_commit": ctx.state.get("upstream_commit", "")})
    async with _serial_lock(ctx.run_dir):
        outcome = await rebase_module(
            module, client=client, config=config,
            prompt_data=data, tool_defs=defs, extra_tools=tools,
            substate=sub, hooks=hooks,
            scope=_module_scope(repo_root, module, manifest,
                                run_dir=ctx.run_dir),
            module_test_plan=test_plan,
            trace=ctx.trace)
    return StepResult(True,
                      summary=f"{module}: {outcome['status']} "
                              f"(debug_attempts={outcome['debug_attempts']})",
                      outputs={"state_updates": {
                          f"module_{module}_status": outcome["status"]}})


def _make_ci_client(token: str, org: str, pipeline: str,
                    build_env: dict[str, str],
                    ignore_branch_filters: bool = False):
    """Provider-client factory — module-level so tests inject a fake."""
    from ...ci.buildkite import BuildkiteCI
    return BuildkiteCI(token, org, pipeline, build_env=build_env,
                       ignore_branch_filters=ignore_branch_filters)


def _worktree_digest(repo) -> str:
    """Content digest of ALL uncommitted work — staged + unstaged diffs
    AND untracked file bytes. `status --porcelain` alone cannot see a
    content edit to an already-dirty file (round-1 review), which would
    let a rejected debug attempt's edits leak into the next push."""
    import hashlib
    import subprocess
    h = hashlib.sha256()
    st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                        capture_output=True, text=True, timeout=60).stdout
    h.update(st.encode("utf-8", errors="replace"))
    diff = subprocess.run(["git", "-C", str(repo), "diff", "HEAD"],
                          capture_output=True, timeout=120).stdout
    h.update(diff)
    import os
    for line in st.splitlines():
        if not line.startswith("??"):
            continue
        p = Path(repo) / line[3:].strip().strip('"')
        files = [p] if p.is_file() or p.is_symlink() else \
            sorted(q for q in p.rglob("*")
                   if q.is_file() or q.is_symlink()) \
            if p.is_dir() else []
        for q in files:
            h.update(str(q).encode("utf-8", errors="replace"))
            try:
                # mode + symlink target too: a chmod-only or retarget-only
                # attempt is still a change (round-3 review)
                st_q = q.lstat()
                h.update(str(st_q.st_mode).encode())
                if q.is_symlink():
                    h.update(os.readlink(q).encode("utf-8",
                                                   errors="replace"))
                else:
                    h.update(q.read_bytes())
            except OSError:
                pass
    return h.hexdigest()


def _cancel_owned_active_builds(client, ops_dir,
                                trace=None) -> list[str]:
    """Abort cleanup (§3.3 rollback inventory: 'running CI → cancel
    op-recorded builds'): cancel OUR still-active builds; adopted builds
    (no op record) are never touched. Best-effort per build — cleanup must
    never raise out of a finalizer."""
    from ...rebase_engine import ci_loop
    cancelled: list[str] = []
    try:
        ops = ci_loop.load_ops(Path(ops_dir))
    except Exception:  # noqa: BLE001 - a corrupt ledger cannot block teardown
        return cancelled
    for op in ops:
        if op.state != "created" or not op.build_id:
            continue
        try:
            state = str(client.get_build(op.build_id).get("state", ""))
            if state in (*ci_loop.PENDING_JOB_STATES, "failing"):
                if ci_loop.cancel_build_guarded(client, Path(ops_dir),
                                                op.op_id):
                    cancelled.append(op.build_id)
                    if trace is not None:
                        trace("ci_build_cancelled_on_abort",
                              op_id=op.op_id, build=op.build_id)
        except Exception:  # noqa: BLE001 - best-effort per build
            continue
    return cancelled


@step("rebase.v3_ci", "script", "push",
      "Remote CI: push, guarded build creation, monitored to terminal.")
async def _v3_ci(ctx: StepContext) -> StepResult:
    """remote_ci/full phase 4: commit-and-push through the PR3 cluster (C4
    double gate inside — mode + push_gate ordering are the task-governance
    half, ALLOW_PUSH the env half), then guarded op-recorded builds
    monitored to terminal with serialized CI-debug rounds
    (`ci_loop.run_ci_rounds`). CI job failures that survive debugging are
    SUBSTATE data (parent parity) — the finalize step rules needs-human;
    structural refusals (no client, no signal, push refused) BLOCK here."""
    import subprocess
    from ...rebase_engine import ci_loop, push_to_ci
    repo = require_repo(ctx)
    if isinstance(repo, StepResult):
        return repo
    manifest = _adapter_manifest(ctx)
    if isinstance(manifest, StepResult):
        return manifest
    blocked = _ensure_checkout_locks(
        ctx, manifest, _task_params(ctx).get("rebase_mode", ""))
    if blocked is not None:
        return blocked
    token = getattr(ctx.settings, "buildkite_api_token", "")
    if not token:
        ctx.trace.record("capability_gap", capability="ci.provider_client",
                         detail="no CI token — remote CI cannot run")
        return StepResult(False, FailureKind.BLOCKED,
                          "remote CI needs a provider token; run local_ci "
                          "instead")
    provider = str((manifest.get("ci") or {}).get("provider") or "")
    if provider != "buildkite":
        ctx.trace.record("capability_gap", capability="ci.provider_client",
                         detail=f"no remote-CI client for provider "
                                f"{provider!r}")
        return StepResult(False, FailureKind.BLOCKED,
                          f"remote CI has no client for provider "
                          f"{provider!r}")
    org = str((manifest.get("ci") or {}).get("org") or "")
    rb = manifest.get("rebase") or {}
    ci_cfg = rb.get("ci") or {}
    pipeline = str(ci_cfg.get("pipeline") or "")
    if not org or not pipeline:
        return StepResult(False, FailureKind.BLOCKED,
                          "adapter declares no ci.org / rebase.ci.pipeline "
                          "— the pipeline identity is adapter data")
    push_cfg = manifest.get("push") or {}
    signoff = push_cfg.get("signoff") or {}
    author_name = str(signoff.get("name") or "")
    author_email = str(signoff.get("email") or "")
    if not author_name or not author_email:
        return StepResult(False, FailureKind.BLOCKED,
                          "adapter declares no push.signoff identity — "
                          "refusing to commit as an unknown author")
    br = subprocess.run(["git", "-C", repo, "rev-parse", "--abbrev-ref",
                         "HEAD"], capture_output=True, text=True, timeout=30)
    branch = br.stdout.strip()
    if br.returncode != 0 or not branch or branch == "HEAD":
        return StepResult(False, FailureKind.BLOCKED,
                          "cannot determine the checkout branch (detached "
                          "HEAD?) — refusing to push")
    protected = sorted({*(push_cfg.get("protected_branches") or []),
                        *ctx.settings.protected_branches})
    # the adapter-governance half of C4 (round-1 review: SCOPED, not just
    # non-protected): the rebase pipeline may push ONLY to the branch the
    # adapter declares — the scope itself is adapter data, fail closed
    rebase_branch = str(push_cfg.get("rebase_branch") or "")
    if not rebase_branch:
        return StepResult(False, FailureKind.BLOCKED,
                          "adapter declares no push.rebase_branch — the "
                          "push scope is adapter data; refusing an "
                          "unscoped push authorization")
    if branch != rebase_branch or branch in protected \
            or not push_cfg.get("rebase_branch_allowed"):
        return StepResult(False, FailureKind.FORBIDDEN,
                          f"push governance refuses branch {branch!r} — "
                          f"only the declared rebase branch "
                          f"{rebase_branch!r} may be pushed "
                          "(and only with push.rebase_branch_allowed, "
                          "never a protected branch)")
    sub = _substate(ctx)
    upstream_commit = ctx.state.get("upstream_commit", "") \
        or (sub.read().get("upstream_commit") or "")
    if not upstream_commit:
        return StepResult(False, FailureKind.BLOCKED,
                          "no upstream-commit signal — the prepared-tree "
                          "precondition did not hold")
    pin_data = (rb.get("wheel") or {}).get("pin")
    pin = wheel_mod.PinSpec.from_manifest(pin_data) if pin_data else None
    remote = str(push_cfg.get("default_remote") or "origin")
    message_template = str((rb.get("push") or {})
                           .get("commit_message_template")
                           or "rebase: align with upstream {short}")
    unstage = list((rb.get("push") or {}).get("unstage_globs") or [])
    run_id = str(ctx.state.get("run_id") or ctx.run_dir.name)

    client = _make_ci_client(token, org, pipeline,
                             dict(ci_cfg.get("build_env") or {}),
                             ignore_branch_filters=bool(
                                 ci_cfg.get("ignore_branch_filters")))
    baseline: tuple = ()
    baseline_log_fn = None
    baseline_pipeline = str(ci_cfg.get("baseline_pipeline") or "")
    if baseline_pipeline:
        default_branch = str((manifest.get("repo") or {})
                             .get("default_branch") or "main")
        # the baseline's build ids belong to the BASELINE pipeline — its
        # logs must be fetched on THAT client, never the build-under-test
        # one (round-2 review: a cross-pipeline 404 would fall into the
        # lenient same-cause path and ignore real regressions)
        baseline_client = _make_ci_client(token, org, baseline_pipeline,
                                          {})
        try:
            baseline = ci_loop.fetch_baseline_failures(baseline_client,
                                                       default_branch)
            baseline_log_fn = baseline_client.get_job_log
            ctx.trace.record("ci_baseline", count=len(baseline),
                             pipeline=baseline_pipeline,
                             branch=default_branch)
        except Exception as exc:  # noqa: BLE001 - baseline is best-effort
            # parent parity: an unavailable baseline means NOTHING gets
            # baseline-ignored (strictly safer), recorded for the report
            ctx.trace.record("ci_baseline_unavailable", error=str(exc))
    spec = ci_loop.CIClassifySpec(
        ignorable_name_patterns=tuple(
            ci_cfg.get("ignorable_name_patterns") or ()),
        ignorable_log_patterns=tuple(
            ci_cfg.get("ignorable_log_patterns") or ()),
        structural_name_patterns=tuple(
            ci_cfg.get("structural_name_patterns") or ()),
        extra_exception_names=tuple(
            ci_cfg.get("extra_exception_names") or ()),
        baseline=baseline, baseline_log_fn=baseline_log_fn)

    def _porcelain() -> str:
        out = subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip()

    def changes_fn() -> bool:
        return bool(_porcelain())

    def push_fn(op_index: int):
        def _attempt():
            return push_to_ci.commit_and_push(
                Path(repo), upstream_commit=upstream_commit, pin=pin,
                branch=branch, message_template=message_template,
                unstage_globs=unstage, author_name=author_name,
                author_email=author_email, protected_branches=protected,
                wal_dir=ctx.run_dir / "push_wal",
                op_id=f"{run_id}-push-r{op_index}", allowed=True,
                allow_push=bool(ctx.settings.allow_push), remote=remote,
                token=getattr(ctx.settings, "github_token", ""))
        try:
            return _attempt()
        except push_to_ci.PushPreflightError as exc:
            # parent self-heal: a push refused over a stale Dockerfile pin
            # names its own fix — re-pin and retry ONCE
            if pin is not None and "wheel pin does not match" in str(exc):
                ctx.trace.record("ci_push_pin_selfheal", detail=str(exc))
                wheel_mod.pin_dockerfile(Path(repo), upstream_commit, pin)
                return _attempt()
            raise

    local_jobs_cache: dict = {}

    def _local_jobs() -> dict:
        if "jobs" not in local_jobs_cache:
            try:
                built = build_manifest(Path(repo),
                                       ManifestSpec.from_manifest(manifest))
                local_jobs_cache["jobs"] = {j["slug"]: j
                                            for j in built.to_dict()["jobs"]}
            except Exception:  # noqa: BLE001 - verification degrades, traced
                ctx.trace.record("ci_local_manifest_unavailable")
                local_jobs_cache["jobs"] = {}
        return local_jobs_cache["jobs"]

    def _verify_locally(slug: str) -> str:
        """'passed' | 'failed' | 'skipped' | 'unavailable' — the HARNESS
        owns the fix verdict where a local manifest equivalent exists."""
        from ...rebase_engine.test_manifest import is_runnable_command
        job = _local_jobs().get(slug)
        if not job or not is_runnable_command(job.get("command", "")):
            return "unavailable"
        if _venv_declared(manifest) and not _target_venv(
                manifest, extra=ctx.settings.expansion_env()):
            return "unavailable"
        import os
        from ...testing.runner import TestRunner
        runner = TestRunner(
            repo_root=Path(repo), tests_dir=ctx.run_dir / "tests",
            gpu_lock_dir=ctx.run_dir / "gpu_lock",
            artifact_globs=list((rb.get("testing") or {})
                                .get("artifact_globs") or []),
            cuda_visible_devices=os.environ.get("CUDA_VISIBLE_DEVICES", ""))
        outcome = runner.run(manifest_job_to_test_job(job),
                             _target_test_env(ctx.settings, manifest))
        if outcome.timed_out or outcome.watchdog_triggered:
            return "failed"
        if outcome.skipped:
            return "skipped"
        return "passed" if outcome.rc == 0 else "failed"

    from ...rebase_engine.test_manifest import _label_to_slug

    async def debug_fn(jr) -> bool:
        """One serialized CI-debug dispatch (parent `_dispatch_sdk_ci_debug`
        parity): snapshot → agent → tree-change check → local harness
        verification where possible → patch-policy check. Assertion/tolerance
        edits and locally unverified test edits are REVERTED so they cannot
        leak into the retry commit."""
        tier = _tier_client(ctx)
        if isinstance(tier, StepResult):
            ctx.trace.record("capability_gap",
                             capability="rebase.debug_agent",
                             detail=f"no agent backend; CI failure "
                                    f"{jr.name} recorded unfixed")
            return False
        slug = _label_to_slug(jr.name)
        module = str((_local_jobs().get(slug) or {}).get("module", ""))
        log_tail = ""
        try:
            if jr.log_file and Path(jr.log_file).is_file():
                log_tail = "\n".join(
                    Path(jr.log_file).read_text(
                        encoding="utf-8", errors="replace")
                    .splitlines()[-200:])
        except OSError:
            pass
        # CONTENT digest, not porcelain: after an accepted fix dirties a
        # file, a later agent's edit to that same file is invisible to
        # `status --porcelain` (round-1 review)
        before = _worktree_digest(repo)
        snap, untracked = tl.snapshot_worktree(Path(repo))
        try:
            policy_before = capture_patch_policy(Path(repo))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            ctx.trace.record("ci_debug_policy_unavailable", job=jr.name,
                             detail=str(exc))
            return False
        verdict = await _run_debug_agent(
            ctx, manifest, module, slug,
            log_tail or f"CI job {jr.name} failed "
                        f"(state {jr.state}, exit {jr.exit_status}); "
                        "no log available")
        if verdict != "done":
            tl.restore_worktree(Path(repo), snap, untracked)
            ctx.trace.record("ci_debug_rejected", job=jr.name,
                             verdict=verdict)
            return False
        if _worktree_digest(repo) == before:
            # completed but changed nothing — unfixed (parent parity); an
            # identical tree needs no restore
            ctx.trace.record("ci_debug_no_changes", job=jr.name)
            return False
        local = _verify_locally(slug)
        try:
            policy = evaluate_debug_patch(
                Path(repo), policy_before, local_verdict=local)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            tl.restore_worktree(Path(repo), snap, untracked)
            ctx.trace.record("ci_debug_policy_unavailable", job=jr.name,
                             detail=str(exc))
            return False
        if not policy.allowed:
            tl.restore_worktree(Path(repo), snap, untracked)
            ctx.trace.record("ci_debug_policy_rejected", job=jr.name,
                             reason=policy.reason,
                             paths=list(policy.paths))
            return False
        if local == "failed":
            tl.restore_worktree(Path(repo), snap, untracked)
            ctx.trace.record("ci_debug_verify_failed", job=jr.name)
            return False
        if local in ("skipped", "unavailable"):
            # no local reproduction — accept unverified; the CI re-run is
            # the judge (parent parity, traced)
            ctx.trace.record("ci_debug_unverified", job=jr.name,
                             reason=local)
        return True

    ops_dir = ctx.run_dir / "ci_ops"

    # abort cleanup (§3.3): a cancelled/killed run must not leave OUR
    # op-recorded builds running — registered BEFORE any build can exist;
    # adopted builds carry no op record and are never touched
    from ..lifecycle import register_finalizer

    async def _abort_ci_cleanup(outcome, _client=client, _ops=ops_dir):
        if getattr(outcome, "status", "") == "done":
            return
        _cancel_owned_active_builds(_client, _ops,
                                    trace=ctx.trace.record)

    register_finalizer(ctx.run_dir, _abort_ci_cleanup)

    # resume safety (round-1 review): op indices clear BOTH durable
    # ledgers — a re-entered run must never reuse an op id that already
    # names a different push or build
    import re as _re
    from ...rebase_engine import push_wal
    try:
        ops_base = ci_loop.op_index_base(ci_loop.load_ops(ops_dir), run_id)
        push_rx = _re.compile(_re.escape(run_id) + r"-push-r(\d+)")
        wal_base = max(
            [int(m.group(1)) + 1
             for r in push_wal.load_records(ctx.run_dir / "push_wal")
             if (m := push_rx.fullmatch(r.op_id)) is not None] or [0])
    except (ci_loop.CIOpError, push_wal.PushWalError) as exc:
        return StepResult(False, FailureKind.BLOCKED,
                          f"CI/push op ledger unreadable: {exc}")

    try:
        head_oid = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        # The pre-push adoption exemption's premise is "the push is a ref
        # no-op" - PROVE it, never assume it: a clean tree alone does not
        # rule out a diverged/advanced remote (a lease-authorized push
        # would then move the ref and could cancel the very build the
        # exemption spares). The head is passed only when the remote
        # branch ref already equals it; any lookup failure or mismatch
        # disables the exemption (safe default), and the push machinery's
        # own lease/WAL guards the residual check-to-push race.
        ls = subprocess.run(
            ["git", "-C", repo, "ls-remote", remote,
             f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=60)
        remote_oid = (ls.stdout.split() or [""])[0] \
            if ls.returncode == 0 else ""
        adoption_head = head_oid \
            if head_oid and remote_oid == head_oid else ""
        result = await ci_loop.run_ci_rounds(
            client=client, ops_dir=ops_dir, run_id=run_id,
            branch=branch, spec=spec, push_fn=push_fn,
            head_commit=adoption_head,
            changes_fn=changes_fn, debug_fn=debug_fn,
            log_dir=ctx.run_dir / "ci_logs",
            max_retries=int(ctx.settings.rebase_ci_retries),
            settle_sec=float(ctx.settings.rebase_ci_settle_sec),
            poll_sec=float(ctx.settings.rebase_ci_poll_sec),
            timeout_sec=float(ctx.settings.rebase_ci_timeout_sec),
            job_retry_max=int(ctx.settings.rebase_ci_job_retry_max),
            message=message_template.format(commit=upstream_commit,
                                            short=upstream_commit[:12]),
            op_base=max(ops_base, wal_base),
            trace=ctx.trace.record)
    except push_to_ci.PushPreflightError as exc:
        return StepResult(False, FailureKind.BLOCKED,
                          f"push preflight refused: {exc}")

    sub.update({"ci": {
        "result": result.result, "reason": result.reason,
        "fixed": list(result.fixed_jobs),
        "unfixed": list(result.unfixed_jobs),
        "rounds": [{
            "purpose": r.purpose, "op_id": r.op_id,
            "build_id": r.build_id, "build_url": r.build_url,
            "adopted": r.adopted, "build_state": r.build_state,
            "passed": r.passed, "failed": list(r.failed),
            "incomplete": list(r.incomplete),
            "budget_timeouts": list(r.budget_timeouts),
            "ignored": r.ignored,
            "ignored_baseline": r.ignored_baseline,
            "jobs": list(r.jobs),
            "monitor_duration_sec": r.monitor_duration_sec,
        } for r in result.rounds]}})
    updates = {"ci_result": result.result,
               "ci_build_urls": [r.build_url for r in result.rounds
                                 if r.build_url]}
    if result.result == "passed":
        last = result.rounds[-1]
        return StepResult(
            True,
            summary=f"CI passed: build {last.build_id} ({last.passed} "
                    f"passed, {last.ignored_baseline} baseline-ignored, "
                    f"{last.ignored} ignored)"
                    + (f"; {len(result.fixed_jobs)} debug-fixed"
                       if result.fixed_jobs else ""),
            outputs={"state_updates": updates})
    if result.result == "push_dry_run":
        # §2.2: remote CI without ALLOW_PUSH=1 is FORBIDDEN at phase 4 —
        # the dry-run already reported the exact authorized command
        return StepResult(False, FailureKind.FORBIDDEN,
                          "remote CI push is dry-run (ALLOW_PUSH not set); "
                          + result.reason)
    if result.result in ("push_failed", "refused", "no_signal"):
        return StepResult(False, FailureKind.BLOCKED,
                          f"remote CI {result.result}: {result.reason}")
    # result == "failed": CI verdicts are substate data (parent parity) —
    # the transition table's terminal row (finalize) rules needs-human
    return StepResult(
        True,
        summary=f"CI failed: {result.reason}"
                + (f" (unfixed: {', '.join(result.unfixed_jobs[:5])})"
                   if result.unfixed_jobs else ""),
        outputs={"state_updates": updates})
