"""CLI adapters to Copilot's shared execution entry point."""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path


def run_playbook(settings, name: str, repo: str, *, state_dir: Path, params: dict | None = None):
    from ..app.workflow_execution import WorkflowExecution
    from ..engine.registry import StepRegistry
    from ..engine.steps import register_builtin_steps
    from ..playbooks.store import PlaybookStore
    from ..resources import resource_dir

    registry = register_builtin_steps(StepRegistry())
    store = PlaybookStore(resource_dir("playbooks"), registry)
    playbook = store.get(name)
    if playbook is None or not name.startswith("kb-"):
        raise ValueError(f"unknown knowledge playbook: {name}")
    run_dir = state_dir / "runs" / f"{name}-{repo}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    if name == "kb-init":
        from ..engine.steps.knowledge import init_options
        from .init_execution import execute_init
        from .init_stages import _make_stage
        from .init_support import InitRuntime

        from .init_support import InitError
        from ..engine.executor import RunOutcome

        try:
            rt = InitRuntime.from_env(settings, state_dir=state_dir)
            lifecycle = rt.registry.get(repo)
            if lifecycle is None:
                raise ValueError(f"no adapter declares knowledge repo {repo!r}")
            options = dict(params or {})
            stage = str(options.get("stage") or "")
            return asyncio.run(execute_init(_make_stage(rt, lifecycle, stage, **init_options(options))))
        except (InitError, ValueError, OSError) as exc:
            return RunOutcome("blocked", blocked_reason=str(exc)), run_dir
    from .runtime import KbRuntime

    try:
        rt = KbRuntime.from_env(settings, state_dir=state_dir)
    except Exception as exc:
        from ..engine.executor import RunOutcome
        return RunOutcome("blocked", blocked_reason=f"runtime unavailable: {type(exc).__name__}: {exc}"), run_dir
    for spec in playbook.steps:
        spec.params = {**spec.params, "repo": repo, **(params or {})}
    return asyncio.run(WorkflowExecution(settings, registry).execute(
        playbook, run_dir=run_dir, state={"kb_repo": repo}, runtime=rt)), run_dir
