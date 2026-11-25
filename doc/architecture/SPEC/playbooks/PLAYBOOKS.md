# playbooks/*.yaml —— 规范

<!-- verified-against: 2026-11-25 -->

`9 个文件 · 声明式编排数据 · refactor-status: ok`

> 当前集合：`pr-review`@6、`pr-quality`@1、`pr-debug`@2、`pr-rebase`@2、`issue-answer`@2、
> `issue-triage`@2、`repo-profile`@1（active）；`repo-rebase-v3`@1（**locked**）；
> `profile-consolidate`@1（candidate）。带 step 链的完整清单
> 在 [`../../../GUIDE.md`](../../../GUIDE.md) §5 —— **不在这里**。

## 职责
用声明式的有序 step 列表（含 `foreach`、`when:`、逐 step `params`）实现一种任务 kind。

## 每个文件的契约
`name, version, status, task_kinds, repos, requires?, params, provenance,
success, steps[]`。

## 已注册的 playbook
- `repo-rebase-v3` —— **locked**，L0，仓库中立（`repos: []`、
  `requires: [modules, upstream.fork_tracking, ci.provider]`）。全仓库
  rebase 引擎（2026-08-25 切换；委托版 v2 与 native-v1 已删除）——
  **不要改它的 step 列表**。
- `pr-rebase`/`pr-debug`/`pr-review`/`pr-quality`/`issue-answer`/`issue-triage` —— active，
  仓库中立（`repos: []`、`requires: [repo.path]`）。
- `pr-review` 声明可选布尔参数 `include_score`。默认按固定版本输出发现和证据，
  显式请求后附加参考分数。
- `pr-debug` 的 report-only 模式跳过 checkout 和 push；修复模式在推送前审查
  已提交的变更。
- `repo-profile` —— active，仓库中立（用于接入第二个仓库）。
- `profile-consolidate` —— **candidate**
  （planner 不可见；只能经 `--playbook` 运行）。

## 不变量
- 每个 step id 唯一；每个 `step` 名字都必须已注册（由 `store.validate` 强制）。
- 写/推送 step **只**出现在已审核（非生成）的 playbook 里。
- locking 面向会改代码/会推送的 playbook；晋升 candidate→active→locked
  是**带 provenance 的人类动作**。
- **`candidate` 对 `find()` 不可见** —— 只能经 `--playbook <name>` 到达。
  正是这一条让 `profile-consolidate` 保持"刻意的节奏"（连续的 LLM 重写**实测**会腐蚀
  记忆）。
- 仓库中立的 playbook 声明 `repos: []` + 一份 `requires:` 能力清单；
  存在精确 repo 的 playbook 时仍然由它获胜。

## 边界 —— 不属于这里
不含代码；除 `repos`/`requires` 匹配外不含仓库知识。

## 扩展点
新任务 = 一个 YAML 文件；新仓库在其 profile 满足 `requires` 之后，
**直接复用仓库中立的 playbook**（核心零改动）。

## 重构备注
它们是 reuse>adapt>generate 模型里的"配置"那一半 —— **保持声明式**。
要顶住在 `when:`/`foreach` 之外加条件逻辑的冲动；更复杂的东西属于某个 step。
**当第二个仓库接入时，它应该不需要任何新 playbook —— 那就是不变性测试。**

## kb-intake（candidate）
知识服务按仓库运行：`knowledge.collect_events` → `knowledge.intake` → `knowledge.publish`。candidate 状态，planner 不可见，由知识服务调度。

## kb-merge / kb-sweep / kb-activate（candidate）
知识服务的合并推进、发版巡检与快照激活；candidate 状态，由 `kb serve` 调度或 `kb run` 手动运行。

## kb-init（candidate）
为一个仓库建知识库：单步 `knowledge.init`，每次运行一个阶段（skeleton → feature-discovery（显式启用）→ modules → knowledge（解释性知识）→ deepen + 生命周期开关 →
pr-history → harvest-calibration），每阶段一个由人合并的 PR。只由 `kb init REPO --stage S` 运行，不经 `kb serve`，不写服务账本。
`feature-discovery` 先提取文档功能基线，再遍历固定源码与测试库存；独立评审后的目录 PR
合并后冻结，下游核对目录哈希与源码版本。新初始化模板启用发现；旧仓库保持兼容。
`from_existing` 可用于发现、modules 与解释性知识：缺少本地 skeleton 记录时
核验已合并入口与 owner 页面；已有阶段记录及发现目录仍保留完成和合并门禁。
默认 GLM‑5.3 提取、Codex 独立评审，共享并发 13；支持 `from_existing`、
`retry_unfinished`、`budget_usd` 或 `unlimited_subscription`。未知项不增加正式功能计数，
原生追踪归档在 Git 外。详细契约见 [`../kb_service.md`](../kb_service.md)。
`pr-history` 默认回溯 1000 个已合并上游 PR；可传 `pr_count` 和累计花费上限 `budget_usd`。
每个非空知识升级一个 commit，汇成一个 draft PR，完整 Codex 审阅通过后才 ready。
`knowledge` 可传 `from_existing` 从已合并索引与路由增补（保留已有阶段记录的门禁），
可显式传 `subscription_generator` 使用声明订阅计费的生成器；未报告的订阅费用不伪造为 USD。
独立 knowledge-deepen 可传 `unlimited_subscription`，同时检查生成与裁判订阅；不兼容
budget_usd，固定记账仍可观察。启用语义政策时 partial 预览不能当作阶段成功。

## 自进化接入（2026-10-04）

`workflow-improve` 通过 mode 区分元基准、旧周期与进化协调器；CLI、playbook 和周度调度共用协调器。`improve.evolve_publish` 是 push 风险步骤，只有候选 pr-ready 且任务 post、ALLOW_POST、ALLOW_PUSH 门均开放时才写独立进化 outbox。
