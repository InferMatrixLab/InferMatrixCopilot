# config.py —— 规范

<!-- verified-against: 2026-10-08 -->

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

## 可信 native execution profile

`NATIVE_COMPLETION_PROFILES` 与 `NATIVE_SESSION_PROFILES` 分别选择 completion 与只读 agent 的执行配置，JSON 对象以 provider ID 为键。completion 支持 `codex` 与 `zcode`；只读 session 目前支持 `codex`。两者复用同一 backend parser 和 profile；配置来自运行主机的 `Settings`，业务请求只绑定已选定的执行身份。

配置中的 `kind: sandbox` 组合两个独立选择：`backend` 指向注册的沙箱实现（当前为 Docker），`location` 指向 `local` 或 `ssh` 执行位置。Docker 的 executable、socket、immutable image 和 native CLI 属于 backend；SSH host、Python、state root、sudo 与 control timeout 属于 location。原生 `policy` 可选，支持经过版本校验的 Codex restricted catalog 和指定的映射 bridge。

ZCode completion 必须显式配置容器内 `native_cli`。通用 provider worker 仅传递类型化
请求／结果，并调用已有 ZCode adapter 一次；原生 CLI 命令、文件 spool、事件与工具规则
仍由该 adapter 维护。ZCode 不接受 Codex 的 `codex_auth_file` 或 `policy`；schema、
session、原生 byte-idle 和 USD 停止阈值在派发前拒绝。absolute deadline 仍由沙箱执行层
负责。配置 worker 不继承主机的订阅计费声明；真实账户准入尚待补齐。未知执行结果
保持 pending，只能读取原绑定结果，不能据此启动替代调用。

只读 agent profile 将源码、Git 元数据、knowledge、run evidence 和 MCP spec 冻结为通用只读输入。目标镜像须安装 `infermatrix-copilot[mcp]`；Git 源码调查还需要 Git。准备流程在模型派发前运行 bridge readiness control，并绑定实际 context。完成策略与 session 策略分别准入账户；未知费用继续显式记录。

配置 profile 或 effort 的新 run 使用 `native-completion.json` 的 `run-completion/4` pin，冻结 provider、model、CLI、native deadline、effort 和两种 profile。child／resume 读取原配置；worker 环境也接收 SDK 的有效覆盖值。旧 schema 1／2／3 pin 保持原字节和未捕获字段的原语义。session profile 当前接受只读 scope，writable／rebase scope 在派发前拒绝。空 profile 使用现有宿主执行路径。

`STRICT_BACKEND_EFFORT` 是可选的原生推理强度默认值，同时供 agent session 和无工具 completion／输出修复使用。显式 session effort 优先；空值保留原行为。当前 Codex 使用同一原生 effort 校验，不支持该能力的 provider 在派发前拒绝非空默认值。已有 schema 1／2 pin 的空 effort 语义保持不变；schema 3 继续恢复原 effort。

PR-history 的主机侧留存选择是 `KB_INIT_HISTORY_PAYLOADS=omit|retain-private`，默认
`omit`。它由 `kb_service.init_history` 应用策略解析，独立于 backend/location；
`retain-private` 将请求／回复保留在主机执行归档中，不写入知识页。显式选择加入原批次
fingerprint 和 history 状态，并在抽取 trace 中记录；默认省略不改变旧 fingerprint／行。
切换选择不能重新派发原批次。耐久 native 执行要求显式留存，默认省略会在派发前
返回 blocked；不从 profile 推断留存，也不将这种拒绝视作可启动另一付费运行的依据。

## 不变量（**A5**、**C2**、**B1**）
- 密钥只经 env / `.env`（被 git 忽略，**绝不提交**）。
- 仓库专属默认值（`default_repo`、`cost_ref_*`）**只是兜底**；
  adapter/profile 会覆盖它们。它们是这里**唯一被允许**的仓库字面量，由
  `test_v2_p0.py::test_repo_neutral_core` 钉住上限。`high_risk_modules` 的默认值
  已**中立化为空**（风险声明是仓库知识，由 adapter 的 tier 声明获胜）。
- `rebase_agent_root` 是已弃用的显式历史数据导入路径，默认 `None`，空字符串也归一化
  为 `None`。内部 rebase 不再查找外部 agent；随包 adapter 不再声明外部知识层。
  旧 `.env` 中显式填写的路径继续支持自定义 adapter 的数据迁移。
- **`expansion_env()` 过滤密钥**：名字含 key/token/secret/password 的字段与 `.env`
  键一律排除，dotenv 读取 `interpolate=False`——manifest 路径永远不能把凭据拉进
  错误信息或日志；Settings 字段在重名时获胜（已体现进程 env 优先）。
- **`imx_knowledge_runtime` 按仓库 fail-closed**：列出的仓库在 resolve 时校验
  `MIGRATION_COMPLETE.json`，缺标记即失败；未列出的仓库保持 legacy 路径逐字节不变。
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

## 自进化接入（2026-10-04）

`TRACE_STORE_ROOT` 控制 trace/1 全量采集。`IMPROVE_ENABLED=false` 是总停止开关，`IMPROVE_EVOLVE_ENABLED=false` 控制代码进化；源码、数据和发布 outbox 各有独立目录。周预算默认 20 美元，周度槽位默认周一 05:00 UTC。`PR_CONTEXT_SOURCE=snapshot` 与 `IMPROVE_SHADOW` 由可信实验控制器设置；`KB_DRAFT_MAX_OPERATIONS` 默认 6。

`IMPROVE_INPUT_CONTEXT_LIMITS` 默认 `{}`，按精确请求模型名配置正整数输入上下文上限；空模型名、布尔值及非正整数均拒绝。网关可能补充不可见输入时，每次调用按该上限与可见请求字节数的较大值预留，实验子进程继承父 governor 的映射，候选覆盖无效。美元及判官周预算、实际用量结算与超预留中止门不变。

`IMPROVE_NATIVE_CALL_LIMITS` 默认 `{}`，由可信宿主为受 improvement 美元账户保护的原生 completion 配置精确 `provider:model` 边界。例如 `{"claude-code:claude-sonnet-4-6":{"stop_usd":0.5,"overshoot_usd":0.25}}`。
两个金额必须为有限正数：`stop_usd` 是原生 CLI 停止开启下一次请求的阈值，`overshoot_usd` 是宿主根据已验证执行 profile 声明的单次请求最大超额费用，不能由 token 展示值或模型输出推断。账户预留两者之和；请求自身更小的阈值继续生效。无明确边界或 provider 不支持原生停止能力时，在模型前拒绝。
CLI 判官保留显式次数账户；已绑定执行环境的订阅准入可以由该领域免除美元预留，二者均不证明原生发票为零。未知费用保留预留额，实际超支入账并阻断后续调用。实验子进程继承父账户的同一映射，运行臂不能覆盖它；原登记和旧账本不重写。

## 自主进化接入

自主进化新增 `improve_evaluation_mode=objective`、`improve_promotion_mode=automatic`、`improve_evolve_model`；固定单模型，无需判官。总开关与周预算默认不变。

`kind: sandbox` profile 可选 `provider_options`，用于绑定非密钥的 provider 参数。
当前公共项是 `model_mismatch_policy`；ZCode 还支持 `zcode_provider_id` 和
`zcode_reasoning_level`。其他字段（包括 key／token）在派发前拒绝。选项由同一个
provider helper 使用 Settings 规则校验并应用到每次调用的 transport 副本，调用方和
原缓存不被修改。该 map 已包含在原 profile／run-completion/4 绑定中；恢复使用原值。
省略 map 保留旧序列化和未捕获字段语义。真实 mixed-provider 评测必须显式冻结全部
支持的非空选项，不能用 ambient 默认值代替执行身份。

### Provider-native credential sources

Sandbox completion profiles accept optional `credentials_source` with `provider`,
absolute `path`, and optional `owner_uid`. The provider must match the configured
profile; the cache must be a private owned regular file. Do not combine it with
legacy `codex_auth_file`. Local and SSH locations use the same shape; paths resolve
on the execution host. Profile configuration stores only the reference. Dispatch
uses the registered adapter's filtered snapshot via the existing private worker
sidechannel; recovery-only original-result reads do not load the live cache.

ZCode's selected coding-plan source also requires explicit frozen
`zcode_provider_id`; credentialed runtime support currently requires native CLI
0.16.9. Account controls do not spend model calls or refresh OAuth credentials.
Unreported expiry/cost remain unknown. Detailed source and safety contracts are
recorded in the framework inventory and native-credential evidence.

Sandbox source snapshots preserve empty directories using frozen-input protocol 6
and worker envelope 8. Docker images supporting this protocol must include the
matching packaged worker and declare `LABEL io.infermatrix.sandbox.inputs="6"`.
An unlabeled image retains protocol 5 support; directory-bearing requests refuse
before model dispatch. SSH preflight reports the intersection of agent and image
capabilities, so local and remote execution use the same input implementation.
Original protocol 1–7 worker receipts and binary-input fingerprints remain valid.

Provision native CLIs with their vendor runtime layout and adjacent helper
executables, not only the main executable. In particular, Codex runtimes using
Code Mode require `codex-code-mode-host` beside `codex`. Freeze the full runtime
bundle and immutable image identity; validate effective native configuration, managed requirements, the allowed MCP
server list, Git/source and tool availability
without inference before running a paid review. Existing pending runs retain
their original controller, image, credentials binding, and execution location.
