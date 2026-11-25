# engine/steps/_common.py —— 规范

<!-- verified-against: 2026-09-26 -->

`LOC ~300 · step 库基础设施 · refactor-status: ok`

## 职责
step 的自注册面，以及各 step 文件共享的跨模块 helper。内建 step 模块由
`steps.register_builtin_steps` 显式加载；加载模块时装饰器收集定义，再写入
`StepRegistry`。

## 功能
`@step`/`register_step` 装饰器 + `_COLLECTED` + `collected()`；
helper：`repo_path`、`require_repo`、`task_spec`、`from_state`、`published`、
`no_llm_gap`、`gh`、`git`、`gh_read_tools`、`post_step`、`record_debug_memory`。

## 公开契约
`step(name, kind, risk, description)`；`register_step(StepSpec)`；`collected()`；
以及上述 helper（含 K3/K4/K7 的守卫 helper）。

## 不变量
- step 名重复 → **在显式装配期间加载 step 模块时抛错**（**A4**）。
- 这是 step 共享 helper 的**唯一**归处 —— step 模块从这里 import，
  **绝不互相 import**（**A2**）。
- helper 保持轻薄、对副作用诚实、且仓库中立。
- **`repo_path(ctx)` 不再是纯访问器** —— 两处刻意的变化都要知道：
  (1) 三级优先序 `ctx.params → ctx.state → state["task_spec"]["repo_path"]`
  （最后一级是预约时冻结、`authorize_repo_path` 授过权的绑定 —— executor
  state seed 之外触达的 step 也与规划看到的 checkout 一致）；
  (2) 解析出的路径落在 `worktrees.worktree_root()` 之下时经
  `_hold_if_worktree` 取共享 worktree 持有 —— 挂在**使用**而非创建上
  （`--resume` 跳过已完成 step 的函数体，见 `engine/worktrees.md`）；
  取不到持有**绝不抛**（那是被回收的风险，不是评审错误）。

## 边界 —— 不属于这里
不含 step handler；不含领域逻辑。**只有基础设施 + 共享 IO。**

## 依赖（允许）
`engine/step`；`...tools`（在 `gh_read_tools` 内部）；stdlib。

## 扩展点
**确实**被 ≥2 个 step 文件共享的 helper → 加到这里。只被一个 step 用的 helper
属于那个 step 自己的文件。

## 测试
经各 step 测试覆盖；`post_step`/`gh`/`git` 在
`test_ci_and_repo_map.py` 等处被 patch。

## 重构备注
**当心职责蔓延** —— 如果这里堆积了很多只有单一使用者的 helper，把它们推回各自的 step。
真正跨切的是 `gh`/`git`/`repo_path`；请把 monkeypatch 接缝
（测试 patch 的是 `steps.pr._gh`，即 import 别名）记录清楚，
免得将来一次改名**静默地**弄坏 patch。

## 精简 —— **K3/K4/K7** helper 的归处
step 样板的收敛就落在这个文件。`require_repo`（K3）、`no_llm_gap`（K3）、
`published`（K4）、`from_state`（K7）**已落地**；`record_debug_memory` 经
`KnowledgePaths.resolve(...).shared_write_db` 落库（**D1**/**D4**），失败被吞掉
——学习回路绝不搞坏修复本身。仍未抽取的：
- `adapter_or_result(ctx)` / `@needs_adapter`（K3 —— profile 守卫）、
  `store_for(adapter)`（K3）。
已落地与待抽取的每一个都必须**保住它所包裹的那条保证**（B1 类型化返回、B2 交接、
E2 `capability_gap` 事件）。**只在 ≥2 个真实现场时才抽取**。

Strict review passes `issue_comments=False` to `gh_read_tools`. Its issue reader
can inspect issue title, body and labels without exposing conversation on a PR
addressed by the same number. Issue workflows retain the default comment access.
