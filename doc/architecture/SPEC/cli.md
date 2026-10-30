# cli/ —— 规范

<!-- verified-against: 2026-10-30 -->

`LOC ~1420（6 个文件） · 接口 + 编排门面 · refactor-status: ok`

## 职责
flag CLI 与 `Copilot` 门面：解析 → 过门 → 执行；并持有 run 目录、RunTrace、notifier
和 metrics 的接线。它曾是一个 406 行的模块；现在是一个把 argparse/REPL 接线与编排器
分开的包。

## 包内布局（一个文件一个关注点）
- `__init__.py` —— 只再导出 `Copilot`、`main`（见下方公开面）；无逻辑。
- `__main__.py` —— `python -m infermatrix_copilot.cli` 的对等入口。
- `copilot.py` —— 兼容导出 `app.core.Copilot`；编排实现不再属于 CLI。
- `entry.py` —— `argparse`、`_handle_line`、`main`（把 argv/stdin 变成对 `Copilot`
  的调用）。
- `utils.py` —— `parse_task_params` 和共享 metrics 格式化器的兼容导出。
- `doctor.py` —— 预检诊断（2026-07 新增）：逐项 ✓/✗，每个失败给出**唯一**确切的修复命令。
- 子命令：`doctor` 与 `migrate-knowledge`（PR4d 部署期知识迁移；**显式 owner
  动作，零 LLM**，需 `--repo <name>`，支持 report-only；见 RUNBOOK）。
- `rfc`：在主 argparse 解析前把其余 argv 原样交给 `rfc_service.cli.main`，
  与独立的 `infermatrix-rfc` 共用入口，不创建 Copilot run 或调用模型。
- `learning` delegates to `knowledge_service.learning_cli.main` before general
  parsing: agreement, prepare, submit, status, feedback, report, sync, sync-status. Bounded JSON file/stdin
  inputs share SDK/MCP services; this path never constructs a model or Copilot.
  `report --scope auto|local|central` selects the team aggregate when configured
  by default. `sync --limit 100` retries durable uploads; `sync-status` is local
  inspection. `infermatrix-feedback` independently starts the authenticated
  collector via `knowledge_service.feedback_server_cli` without a model or MCP.
- `learning agreement show|status|accept|revoke` delegates to `agreement_cli`
  before the learning JSON parser. Show/status are side-effect-free inspection.
  Accept requires a human TTY, displays the actual versioned terms and configured
  destinations, and asks for a name plus exact `I AGREE`; no flags or pipes can
  accept. Agents must not execute acceptance for the person. Prepare/submit,
  grading and sync are off until current acceptance; task status, source reads
  and explicit local reports remain available. Changed terms/destinations require
  renewed acknowledgement. Revoke gates future operations, not already completed
  or in-flight external effects. See [the learning guide](../../guide/knowledge-learning.md).

## 公开契约（可从 `infermatrix_copilot.cli` import）
`main(argv)`；`Copilot`（`resolve`、`run_task`、`run_playbook`、`run_queue`、
`resume_last`、`status`、`logs`、`playbooks`、`_execute`、`_adapter_for`、
`_resolve_repo_path`）。再导出的 `__init__` 让 `infermatrix_copilot.cli:main`
（entry point）和 `from infermatrix_copilot.cli import Copilot` 保持不变。

## 不变量
- `resolve` 把能力（adapter + REPO_PATHS）喂给 planner。
- 确认之前先过 plan-review 门；除非 `--yes`，`confirm_required or requires_review`
  时触发确认（`_gate_and_confirm`，K6）。
- **plan-review 门在无人时失败关闭（C6）。** `block` 永远停机；非 `lgtm` 的其余裁决
  （`revise`、`unavailable`）只是**呈现**给人看——真正把门的是随后的 `[y/N]`。因此
  `--yes` 抹掉那个人时，同一裁决必须改为停机，而不是凭一个已经不存在的确认放行。
  交互路径不变（照旧打印裁决再问确认）。反例是实测出来的：一次无法解析的评审回复
  让四个后端里的三个把同一份 pr-rebase 计划一路跑到推送门，而评审回复解析正常的那个
  后端**阻断**了它。
- `_execute` 是**唯一**的执行路径（task / 显式 playbook / resume）。
- 仓库知识（保护分支、高风险模块）由 adapter 进入 run state（**A5**）；
  被阻塞 → 退出码 3（`BLOCKED_EXIT`）。
- `--playbook` 是运行 candidate 的**唯一**方式。
- **rebase_mode 是带权威写回的**：`params.rebase_mode` 在过门前经
  `rebase_engine.modes` 解析并写回（`mode_state_flags` 决定 `when:` 门），
  冲突抛 `ModeConflictError` —— review 上下文向 reviewer 说明该模式下
  哪些 step 会跑。
- **每仓库知识锁（SHARED）持有整个 run 的生命周期**：run 之间不互斥，
  只与 `migrate-knowledge` 的 EXCLUSIVE 锁互斥 —— 迁移绝不与活跃 run 并发。
- **`doctor` 只读，且永不打印密钥的值** —— 只打印它的名字。除非传 `--probe`，
  否则它不做任何付费 LLM 调用；`--probe` 是唯一的付费检查（每个已配置档位一个 token）。
  `--json` 供 CI 使用，而**在没有凭据时以非零码退出正是 CI 里的预期状态**。
- **`--performance` 是抬高模型档位的唯一方式**；默认是 `eco`。
  **抬高档位永远不会扩大权限**（`tier` 仍然由 `kind` 推导）。
- CLI 主路径在**创建 run 目录之前**过门，所以被放弃的计划不留下任何东西。
  MCP 的预约形状（先建、后规划）是**刻意不同**的 —— 见 `mcp_server.md`。
- **`reserve_run` 返回 `(run_id, created)`，签名变更是契约的一部分**：
  调用方只在 `created` 为真时入队（idempotency：命中既有键返回既有 run）。
  预约时即经 `mcp_policy.authorize_repo_path` 授权/固化 `spec.repo_path`；
  带键的预约走 `_reserve_keyed`/`_reserve_new`（重拉-冲突-已决三分）。
- **执行的 at-most-once 是 claim，不是锁**：`_execute_reserved_locked`
  先 `rs.claim_for_execution(run_dir, child_pid)` —— 输掉 claim 的子进程
  不规划、不执行、不写状态，打印后 0 码退出（关掉"server 死后重试 +
  旧子进程醒来"的双评审竞态）。
- **冻结的 `spec.repo_path` 压过环境解析**：`_repo_path_for(spec)` 先看
  冻结绑定、再落回 alias 解析（`_resolve_repo_path` 现在只管 alias）；
  `resolve()` 与 `_execute` 的 state seed 都走它。
- 阻塞原因随终局落盘：`last_blocked_reason` 进入 `run_status.json` 的
  note —— 以前只打在子进程 console 上就被丢掉。
- resume 成功终局调用 notifier resolution：旧 `ESCALATION.md` 归档到 history，
  不让已经完成的 run 继续显示活动阻塞标记。

## 边界 —— 不属于这里
不含 step 逻辑、不含仓库知识字面量、不含 LLM prompt。**只做编排接线。**

## 依赖（允许）
`engine/*`、`playbooks/*`、`intent`、`task_spec`、`adapters/base`、`push`、
`review/reviewer`、`notify`、`run_trace`、`config`、`ui`、`chat`。
独立服务入口允许惰性委托 `kb_service.cli`、`rfc_service.cli`；服务自身的授权、
状态与执行契约分别属于对应服务规范。
**任何下层都不得 import 它**（**§ARCH.4.2**）。

## 扩展点
新 REPL 命令 → `_handle_line`（entry.py）；新的 run 接线 → `_execute`（copilot.py）；
新的纯格式化器 → utils.py。

## 测试
`test_cli.py`、`test_phase_b.py`、`test_chat.py`、`test_ui.py`；
预约/claim/repo_path 冻结：`test_mcp.py`、`test_idempotency.py`、
`test_contract.py`。
RFC 子命令原样转发与共享入口：`test_rfc_integration.py`；服务契约见
[rfc_service.md](rfc_service.md)。

## 重构备注
拆分**已完成**（它曾是内聚拆分候选）。`Copilot` 类完整留在 `copilot.py`，
好让 resolve→execute 的流程能在一个文件里读完；只有 argparse/REPL 前端（`entry.py`）
和两个纯格式化器（`utils.py`）搬了出去。K6（`_gate_and_confirm`）已完成，挂在类上。

## 2026-09-28 kb 子命令
`infermatrix-copilot kb …` 在 argparse 之前分派到 `kb_service.cli.main`（知识服务运维命令），不启动 Copilot/LLM。

## 2026-10-02 RFC 子命令
`infermatrix-copilot rfc …` 原样转发 RFC 服务选项、子命令与退出码。草稿、文件导入、
进度查询与发布操作由 RFC 服务处理；这条路径不经过维护任务的 planner 或 run 状态。
`--post`、内容摘要、权限与操作恢复均遵循 RFC 服务契约，不能由 Copilot 的 `--yes`
替代。

## 自进化接入（2026-10-04）

`infermatrix-copilot improve …` 在通用 argparse 之前分派到 `improve.cli.main`；工作流检查、候选生成、实验、发布和账本查询使用该入口。

## 2026-10-27 `--resume [run-id]`

`--resume` 现在接受可选 run id；`--repo`、`--playbook`、`--task-param` 与
`--resume` 同用时是对保存任务的断言（见 `app/core`）。
`--rerun-from STEP_ID`（2026-10-29）只能与 `--resume` 同用，否则 argparse 报错。
