# config.py —— 规范

<!-- verified-against: 2026-10-30 -->

`LOC ~658 · 配置 · refactor-status: oversized`

## 职责
从 env / `.env` 加载的 `Settings`（pydantic-settings），以及把本次 run 的档位与后端
选择变成具体目标的那些推导 helper。

## 功能
为 LLM 端点与逐档模型、仓库、引擎预算、推送安全、PR debug、v3 rebase
（reviewer 模型、远端 CI 轮次/预算、`github_token` 推送凭据）、agent 运行时、
ensemble、MoA、评审深度与按 pass 路由、Strict 后端选择、profile、patch 触发器、
metrics 与升级，提供带类型字段和安全默认值；外加 PR4d 知识运行时 cutover
（`imx_knowledge_runtime`）、manifest 展开回退（`expansion_env()`），以及
MCP `repo_path` 授权面与 idempotency 保留期：
`mcp_allowed_repo_roots`（`MCP_ALLOWED_REPO_ROOTS`，空 = 只有已配置的
checkout —— 最小权限；放宽是**运维**决定，不是调用方的）与
`idem_retention_days`（默认 30，圈住 `.idem/` 索引）；run service 的并发
worker 数 `strict_max_workers`（`STRICT_MAX_WORKERS`，默认 1，启动时校验
1–`MAX_STRICT_WORKERS_LIMIT`=32，越界即失败而非静默收敛）；以及
知识回流落盘目录 `knowledge_intake_dir`（`KNOWLEDGE_INTAKE_DIR`，默认空 = 关闭；
消费方见 `doc/RFC-knowledge-intake.md`）与跨主机发布目标 `knowledge_intake_issue`
（`KNOWLEDGE_INTAKE_ISSUE`，`owner/repo#N`，默认空 = 关闭：harvest step 把同一条记录
以带标记的 JSON 评论贴到该锁定的 mailbox issue，供另一台主机上的 reviewbot 摄取）。

## 公开契约

`allowed_knowledge_repositories`（`ALLOWED_KNOWLEDGE_REPOSITORIES`，默认空 tuple）是
运行主机授权读取的跨仓库 ID。它不进入模型工具的权限参数；授权仍须与固定快照中的
confirmed 依赖及源码/目录 pin 一同验证，空配置只允许当前仓库。

带全部可调项的 `Settings`；`reviewer` / `intent`（回退到 `agent_model`）；
`repo_path(name)`；`model_for(mode)`；`tier_target(role)` → `ResolvedTarget`；
`expansion_env()`；`knowledge_runtime_repos`；`allowed_repo_roots`
（配置列表，未设时 = 已配置 checkout 本身 —— `mcp_policy.authorize_repo_path`
的圈禁判据即此属性）；以及 `strict_backend` 校验器。

## 不变量（**A5**、**C2**、**B1**）
- 密钥只经 env / `.env`（被 git 忽略，**绝不提交**）。
- 仓库专属默认值（`default_repo`、`rebase_agent_root`、`cost_ref_*`）**只是兜底**；
  adapter/profile 会覆盖它们。它们是这里**唯一被允许**的仓库字面量，由
  `test_v2_p0.py::test_repo_neutral_core` 钉住上限。`high_risk_modules` 的默认值
  已**中立化为空**（风险声明是仓库知识，由 adapter 的 tier 声明获胜）。
- **`expansion_env()` 过滤密钥**：名字含 key/token/secret/password 的字段与 `.env`
  键一律排除，dotenv 读取 `interpolate=False`——manifest 路径永远不能把凭据拉进
  错误信息或日志；Settings 字段在重名时获胜（已体现进程 env 优先）。
- **`imx_knowledge_runtime` 按仓库 fail-closed**：列出的仓库在 resolve 时校验
  `MIGRATION_COMPLETE.json`，缺标记即失败；未托管安装中未列出的仓库保持 legacy 路径逐字节不变。
- `github_token` 只服务 v3 远端 CI 推送的 **header 认证**（推送传输按设计禁用
  credential helper，绝不落共享凭据存储）。
- **`STRICT_BACKEND` 在前面就被校验**，失败时列出合法集合。未知后端**绝不能**到达
  `providers.resolve_provider` —— 两层，因为 `.env` 里的一个拼写错误不该启动一次注定
  失败的 run。
- **`tier_target` 对 harness provider 返回空 key。** harness 的认证住在厂商 CLI 自己的
  状态里，本代码库从不接触。**唯一例外是 `deepseek`**：它是 API-keyed 的，凭据在它自己的
  transport 内部解析 —— 见 `providers/deepseek.md`。
- **fail-closed 的 served-model 守卫**：实际服务的模型与请求不符是**错误**，
  而不是静默替换。
- **推理模型需要 token 留白。** 灰区评审 planner 曾在两个战役里对每一个灰区条目静默
  失败，因为思考在任何 JSON 出现之前就吃完了 400-token 上限 —— 对推理模型调用的上限
  是**正确性设置，不是成本旋钮**。

## 边界 —— 不属于这里
除推导 helper 外无逻辑；除加载 env 外无 I/O；不含 provider transport（`providers/`）；
不调模型。

## 依赖（允许）
`pydantic-settings`；外加 `tier_target` 内部**函数级**引入的
`providers.registry`（只读 `PROVIDERS[...].default_model`）。后者是刻意的：
provider 的缺省模型必须在**这一处**解析，否则 `ResolvedTarget.model` 会报 `""`
而 transport 实跑另一个模型——即不变量 7 要防的贴错标签。函数级 import 保持模块
导入期只依赖 pydantic-settings，不成环。

## 扩展点
新可调项 → 一个带安全默认值的类型字段，并用一行注释写明含义与单位。
新后端 → 注册表条目 + 本文件的校验器集合，**绝不是这里的一条新分支**。

## 测试
`test_llm_providers.py`、`test_tier_split.py`、`test_providers.py`；
全套测试间接覆盖（fixture 会构造 `Settings(_env_file=None, ...)`）。

## 重构备注
在 v3 后端工作中从约 116 行长到约 418 行，现在**确实偏大**，但仍是单一关注点。
如果再增长，请分组成嵌套 settings 模型（`LLMSettings` / `PushSettings` /
`ReviewSettings` / `BackendSettings`），**而不是拆文件** —— 调用点依赖"一个 `Settings`
对象到达每个 `StepContext`"。

## rebase 后端抽象（2026-09-19）

`rebase_backend`（默认 `api`）选择跑 rebase **module agent** 的 provider：
`api` 为进程内 Anthropic tool-use 循环，其余为 harness provider id
（`cursor` / `claude-code` / `codex`）。`rebase_backend_model` 是 harness
**内部**的模型 id（如 `cursor-grok-4.6-high-fast`）。

注意：tier 模型名指向 RAW-API 模型，对 harness CLI 无意义，**绝不**转发过去；
为空时回落到 `strict_backend_model`，再回落到 harness 自身默认值。

`rebase_harness_timeout_sec`（默认 7200，必须 > 0；2026-10-27）是每次 module /
debug harness 会话的墙钟预算，取代 `ModuleRunConfig.harness_timeout_s` 原来的
硬编码值；zcode 传输同时用它设置工具桥的 MCP `timeoutMs`。模块测试命令自身的
`TEST_TIMEOUT_SEC` 不在这里——那是 adapter 的 `rebase.testing.module_timeout_sec`。

## 自进化接入（2026-10-04）

`TRACE_STORE_ROOT` 控制 trace/1 全量采集。`IMPROVE_ENABLED=false` 是总停止开关，`IMPROVE_EVOLVE_ENABLED=false` 控制代码进化；源码、数据和发布 outbox 各有独立目录。周预算默认 20 美元，周度槽位默认周一 05:00 UTC。`PR_CONTEXT_SOURCE=snapshot` 与 `IMPROVE_SHADOW` 由可信实验控制器设置；`KB_DRAFT_MAX_OPERATIONS` 默认 6。

`IMPROVE_INPUT_CONTEXT_LIMITS` 默认 `{}`，按精确请求模型名配置正整数输入上下文上限；空模型名、布尔值及非正整数均拒绝。网关可能补充不可见输入时，每次调用按该上限与可见请求字节数的较大值预留，实验子进程继承父 governor 的映射，候选覆盖无效。美元及判官周预算、实际用量结算与超预留中止门不变。

## 自主进化接入

自主进化新增 `improve_evaluation_mode=objective`、`improve_promotion_mode=automatic`、`improve_evolve_model`；固定单模型，无需判官。总开关与周预算默认不变。

## Managed installation home (2026-10-30)

`INFERMATRIX_COPILOT_HOME` selects the stable user configuration, default run
directory and memory database; it defaults to `~/.infermatrix-copilot`.
The managed CLI can additionally select its existing authoring checkout's
`.env` through `COPILOT_SOURCE_CONFIG`. Source configuration, managed user
configuration, cwd overrides and process environment retain their precedence.
Background upgrades never rewrite model credentials or repository bindings.
