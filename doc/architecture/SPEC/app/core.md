# app/core.py —— 规范

<!-- verified-against: 2026-10-30 -->

`Copilot` is the headless workflow application. It resolves a TaskSpec,
delegates reviewer verdict policy to `review.plan_gate`, confirms when needed,
executes or resumes a playbook, and reserves/executes durable runs. The CLI
imports it through `cli.copilot` for
compatibility; the application never imports CLI or MCP transports.

Repository checkout, capabilities, and adapter policy come from
`app.repository_context.RepositoryContextResolver`. Planning and execution
use the same context contract. A missing adapter retains the legacy
unknown-capabilities path when a checkout exists; a broken known adapter
blocks the run. Execution rechecks the context after planning and refuses a
changed checkout or policy before starting the executor.

`Copilot` keeps reservation and run-path methods as compatibility delegates
to `app.reservation.RunReservation`. It owns workflow resolution, confirmation,
reserved-run claims, and terminal status writes. `app.workflow_execution`
owns the executor lifecycle, locks, tracing, and terminal outcome; the old
`Copilot._execute` path delegates and retains the last-run summary. The
application does not own idempotency indexing or run ID validation.

The pre-execution gate runs before a normal CLI run directory is created.
The child rechecks the read-only policy and checkout binding. Existing
checkpoint keys, run IDs, and blocked exit codes remain stable.

`GateOutcome`, `_mode_review_context`, and `Copilot` retain their import path
through the compatibility module. The application passes its reviewer to the
plan gate, preserving the no-human fail-closed rule. Only orchestration belongs
here; command argument parsing and stdio protocol registration remain
transport concerns.

## `--resume <run-id>` 断言式恢复（2026-10-27）

`resume_last(run_id, *, repo, playbook, params)`：给定 run id 时只恢复那个
run（`_contained_run_dir` 拒绝越界路径）；`--repo/--playbook/--task-param`
在恢复时是对已保存 task.json 的**断言**而非修改，任一不匹配即 BLOCKED。
发布 launcher 因此能把 resume 绑定到不可变的 campaign，而不会误入更新的
无关任务。损坏的 task.json 同样 BLOCKED 而非崩溃。

`rerun_from`（`--rerun-from <step id>`，2026-10-29）是唯一会改变 resume 行为的
参数：原样传给 `_execute`，由 executor 让该 step 及其后各 step 重新执行；保存的
任务身份仍不改。

## 被信号终止的预约 run（2026-10-28）

`_execute_reserved_locked` 的终态写入（`0 → done`、`BLOCKED_EXIT → blocked`、
其他 → `failed`）在 `engine.lifecycle.shutdown_requested()` 非空时**跳过**：
`workflow_execution._interrupted` 已经写下 `interrupted`，`128 + signal` 的
退出码不得被这里改标成 `failed`（那会让 reconciler 把一个可恢复的 run 当作
失败收尾）。

## Managed runtime identity (2026-10-30)

New CLI and reserved runs record their runtime before execution. Resume selects
one exact run, then delegates to its pinned physical interpreter while retaining
repo/playbook/parameter assertions and rerun checkpoints. Delegated parameters
use lossless JSON; the original runtime validates the saved task. Missing pinned
runtimes block execution instead of substituting current code.

Execution acquires the per-run lock before the shared checkout/PR lock, passes
the held run lock to WorkflowExecution, and releases both after the lifecycle.
The worker holds the shared lock, so restarting its MCP parent does not permit
a second release to modify the same checkout/PR concurrently. Signal-driven
interrupted terminal states retain their existing ownership rules.
