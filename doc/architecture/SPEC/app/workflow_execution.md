# app/workflow_execution.py — workflow execution boundary

<!-- verified-against: 2026-10-29 -->

`WorkflowExecution` runs an already selected playbook with the caller's
current LLM dependency. It rechecks the repository context before starting,
owns the run and shared knowledge locks,
initializes tracing and notification, seeds executor state with checkout and
adapter policy, and drives `Executor` through `run_guarded`. It returns an
immutable `ExecutionResult` containing the exit code and blocked reason.

Around `asyncio.run(run_guarded(...))` it installs the run's SIGINT/SIGTERM
handlers (`engine/lifecycle.py::install_run_signal_handlers`) and restores
them afterwards. A signalled run ends through `_interrupted`: run_status
`interrupted` with this process as `child_pid` (`run_status.mark_interrupted`),
a `run_interrupted` trace event (signal, exit code, completed steps), the
per-step marks plus `run <id>: interrupted (<SIG>)`, and
`ExecutionResult(128 + signal, reason)`. Both paths land there: the
cooperative one (a step saw `shutdown_requested()` and returned blocked, so
`executor.run` returned normally) and the forced one (the run task was
cancelled; `CancelledError`/`KeyboardInterrupt` is caught only when a
shutdown request exists, otherwise it propagates as before). Finalizers have
already run inside `run_guarded` by then: GPU locks are released and the
upstream scratch is kept, so the run is resumable with `--resume`.

A non-empty `rerun_from` is seeded as `state["rerun_from"]`; the executor
re-executes that step and every later one.

A losing run lock leaves active run artifacts untouched. A failed knowledge
lock releases an acquired run lock. A caller-provided lock stays owned by the
caller. Finalizers and locks run on every executor outcome, including failure.
The application owns planning, confirmation, durable reservation, and the
reserved worker's terminal `run_status.json` write. CLI and MCP transports do
not belong in this module.

`Copilot._execute` remains the compatibility entry point and records
`last_run_dir`/`last_blocked_reason` from the returned result. Existing run
state keys, traces, exit codes, and checkpoint behavior stay stable.
