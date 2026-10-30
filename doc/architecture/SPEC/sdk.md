# sdk/ —— 规范

<!-- verified-against: 2026-10-29 -->


`Python SDK v1 · 跨仓库唯一 typed 边界 · refactor-status: ok`

## 职责

`DirectReviewPlan.related_knowledge` 是有默认空字典的增量字段，与 MCP 提供同一有界背景。
每个相关文档另带内容寻址的 `document` 引用，路径保持知识相对 ID；后续读取使用原
`review_context_id`，不混入激活切换后的另一快照。正文 pin 与 PR head 分别记录。

给 ReviewBot 等嵌入式宿主提供一个版本化、可序列化、wheel-safe 的公开 API：
`infermatrix_copilot.sdk.v1`。宿主不得再 import `mcp_server`、`config`、
`thin_mcp_server` 或其私有 helper。

## 公开契约

- `KnowledgeAgreementClient.document/status/revoke` is the lazy inspection-only
  agreement facade. It has no acceptance method, signer/attestation input or
  endpoint override. Status/document do not create acceptance state. Named
  acknowledgement is human-only through `learning agreement accept` in a TTY,
  bound to current terms and configured publication/collector destinations.
  Changed terms/destinations require renewal. Revoke gates future work without
  deleting prior data or guaranteeing cancellation of in-flight operations.
- `KnowledgeLearningClient.prepare/submit/status` and
  `KnowledgeFeedbackClient.record/report/sync/sync_status` lazily delegate to the same durable
  services as MCP and `learning` CLI. Importing either class does not open state,
  resolve a workspace or initialize model credentials. Prepare/submit/record/sync
  require agreement before creating their runtimes. Lesson distillation uses
  extra host-model tokens/time and possible quota; it is off until accepted.
  Publication additionally requires operator destination and write gates.
- With current agreement, feedback records persist locally before optional configured central sync.
  `report(scope="auto"|"local"|"central")` uses the team aggregate when
  configured by default; central failures do not silently return local counts.
  `sync(limit=100)` retries durable pending uploads and `sync_status()` inspects
  the queue without a network request. Neither endpoint nor token is accepted
  from model/tool input. An SDK `state_dir` override changes storage location
  while retaining the operator's configured transport.
  Task status, explicit local reports and sync status remain readable unsigned.
  Central reports/uploads require current acceptance for the actual destination.
- `DirectReviewPlan` and `DocumentPage` add `knowledge_feedback` receipts.
  Plan receipts bind `review_id`; follow-up `read_document` uses that plan's task
  identity, or explicit `task_id` for standalone reads. Receipts contain exact
  delivered-version targets, not source text. Adaptive packets carry the same
  receipt for only their actual model content. Existing containment issuance
  binds the final returned packet including this metadata.
  Unsigned/revoked/renewal-required delivery instead returns `agreement_required`
  with empty targets and no delivery ID, without blocking the original source
  read. A previously issued receipt cannot bypass the later grading/upload gate.
- Feedback repositories are normalized against the pinned view before plan
  issuance and follow-up context storage. `read_document(repository=...)` can
  bind a standalone general-page read to its task's canonical repository;
  an explicit task ID otherwise reuses an unambiguous prior delivery context,
  including after client restart. A task with no known or multiple repository
  contexts must supply a repository/context for general-page reads. Reads without
  an explicit task ID retain standalone behavior. Existing receipts and votes
  are immutable; canonicalization does not rewrite historical identities.
  Optional feedback-storage failures return a fixed unavailable code and do not
  prevent source delivery.

`changes_preview()` / `changes_apply()` 绑定人工编辑的完整候选、版本、摘要及幂等键；
未传 reason 时保留预览原因。`list_rfcs()` 支持可选服务端筛选与分页；`grants()`、
`check_repository()`、`service_status()` 继续委托同一授权应用。HTTP 错误保留
`request_id`，不改变原有 code/status 属性，也不在远端故障后创建本地写入方。

- RFC：`RFCClient` 选择显式本地或托管工作区，统一通过 `dispatch(action, payload)`
  调用 RFC 服务并校验 API 版本。`work()` 支持人工实现记录和清除，`graph()` 支持
  可逆节点分组；`chat_*` 提供私有会话、异步任务、分页事件和完整候选差异确认。
  Bearer 身份来自宿主配置，权限、版本与事实校验由共享应用执行；远端故障不改写
  另一套本地状态。验证见 `test_rfc_work_controls.py`、`test_rfc_chat_integration.py`。
- 显式自适应知识协议：`ContextBudget`、`DirectClient.open_knowledge_context` /
  `plan_adaptive` / `read_knowledge_context` / `search_knowledge_context` /
  `related_knowledge_context` / `expand_knowledge_context`。仅 `model_content` 是实际注入
  正文；引用没有 SDK 额外 excerpt。会话默认 24k、最大累计 64k，计量单位和实际用量
  未知状态明确返回；规则、导航、相关知识和补读同预算。旧 v1 `plan` 默认两页/6k 不变。
- 自适应会话支持 SHA-1/SHA-256 pin，可从私有运行 SQLite 恢复原冻结 view；签发的
  review context 绑定完整请求。旧 v1 completion 的 SHA-1 校验不因新增知识协议放宽。
  `allowed_knowledge_repositories` 是 DirectClient 构造时的主机权限配置，不是工具授权。
- capabilities 的支持仓库来自固定视图 registry；新仓库不要求打包 adapter。
  `StrictRuntimeConfig` 在 registry 已识别仓库时允许不填 forge full_name；这只提供
  知识身份兼容，不承诺旧 Strict PR 获取链路支持所有 forge 或无 forge PR。

- `get_capabilities()` / `DirectClient.capabilities()` 返回 typed
  `Capabilities`：distribution/SDK/Direct/Strict/Quality/Knowledge 版本、resource revision、支持仓库、
  expected-head/structured-result/post-false/file-lock/idempotency 能力与 worker 上限。
- Direct：`DirectReviewRequest` → `DirectClient.plan()` → `DirectReviewPlan`；
  `read_document(document_id, offset, max_bytes)`；
  `DirectCompletionRequest` → `validate()` → `DirectCompletionDecision`。
- Strict：`StrictRuntime(config=StrictRuntimeConfig(...))`，以及
  `capabilities` / `readiness` / `reserve_review`（`start_review` 别名）/
  `get_status` / `get_result` / `close`。旧 `settings_overrides` 暂作兼容入口；
  新配置显式绑定完整仓库名、checkout、允许根、backend、可选 run root 与
  `max_workers`（默认 1，正整数；即宿主并发执行的 run 数，经 handshake 的
  `max_strict_workers` 回报）。
  `StrictPollResult.review` 提供类型化结构结果，`payload` 保持旧 wire 兼容。
- Quality：同一 `StrictRuntime` 接受 typed `QualityReviewRequest`，提供
  `quality_readiness` / `reserve_quality_review`（`start_quality_review` 别名）/
  `get_quality_result`；机械信号只是 bounded hints，结果固定绑定 expected head。
  `QualityPollResult.review` 投影类型化质量结果；非法结构触发 `ResultDecodeError`。
- Knowledge：宿主把不可信输入投影成 `KnowledgeEvidenceEvent` / `KnowledgeEvidenceBatch`，
  再依次调用 `KnowledgeCurator.build_prompt()`、自己的 model adapter、
  `validate_proposals()` 与 `apply()`。返回值为 typed
  `KnowledgeProposalValidation` / `KnowledgeApplyResult`；validator 失败以
  `KnowledgeValidatorError.result` 携带完整、可序列化的失败结果。
- 所有公开 dataclass 皆 frozen，并有 lossless `to_dict()`。

## 不变量

- **公开 import 无副作用**：单纯 import `sdk.v1` 不加载 `config`、legacy
  `contract`、`direct_routing` 或任一 server；Direct provider 实现在调用
  `plan`/`validate` 时才加载；构造 `StrictRuntime` 加载 headless RunService，
  不加载 CLI 或 MCP transport。
- **只跨 document ID，不跨 provider filesystem path**：知识入口、route、guide、
  map 与 fallback 均为相对 knowledge-root 的 ID；`read_document` 拒绝 absolute、
  traversal 与 bundle 外路径。Strict result 丢弃私有 `report_path`，保留 report
  内容、分页游标和结构化 result。
- **资源由 artifact 定义**：`sdk._resources` 用 `importlib.resources.files` 找
  wheel 内 `knowledge` 与 `_runtime/adapters`，用 marker 校验；editable checkout
  才回退仓库根。resource revision 跳过 `__pycache__`/bytecode，保证 clean wheel
  与源码内容寻址不被解释器副产物污染。
- **Direct completion fail closed**：context ID 不只是长得像 hash；它必须由同一
  `DirectClient.plan` 签发、expected head 必须一致、资源 revision 不能漂移。
  签发缓存为有界 LRU（默认 256），被淘汰/未知 context 都拒绝完成。
- **Strict created 语义真实**：SDK 从 core 的 `(run_id, created)` 读取；命中
  idempotency key 的重试返回 `created=False` 且绝不再次入队。review depth 只经
  policy allowlisted `params` 传入。
- **Strict findings 绑定发布结果**：`get_result` 的 `findings` 对最终发布集合逐条返回
  `finding_id`、最终 `severity`、`anchor` 与 `head_sha`，保持顺序及重复 identity。
  identity 以文件路径与完整归一化文本生成，行号变动不改变 identity；它不与
  `finding_dispositions` 按 anchor 拼接。Direct 1.1 / Strict 1.3 对传入的
  carried finding 返回独立、绑定当前 head 的显式 `finding_rechecks`；缺失或
  `unverified` 的答案不代表已修复，详见下方 carried-finding recheck 契约。
- **知识规则由 provider 唯一定义**：catalog 只暴露当前仓库 owner 与 general 的
  owner rule page document ID —— 每个 owner 的 `rules.md` 入口页及其
  `type: rule` 的 `rules-<topic>.md` 专题页（`catalog_entries()` 附每页
  `free_bytes`/`free_lines`，与 `check_knowledge_tree.py` 的拆分门对齐）；prompt 把
  事件放进唯一 `<untrusted_data>` fence（`KnowledgeEvidenceEvent.diff_excerpt` 是宿主
  选取的 diff 片段，SDK 以每事件 8 KiB、每批 160 KiB 截断，先到先得），proposal shape、rule ID、heading、source
  citation、目标页、重复 ID 与页面剩余容量都由 SDK 机械校验——rule ID 全树唯一：
  任何 catalog 页（所有仓库与 general）、任何 heading 层级已使用的 ID，以及 section
  内嵌套 heading 携带的 ID，都被拒绝，同批两个 proposal 也不得共用 ID；超出目标页剩余容量的

  proposal 以 `page full` 在 validator 之前被拒绝，宿主据此改路由而不是整批回滚。
  `proposal_id` 同时绑定 batch、输入下标、repository、section、sources 与目标页
  SHA，宿主不能在 model call 后静默改写已接纳 proposal。
- **知识 apply 是 append-only transaction**：只追加完整 rule section 并更新唯一
  `updated:` frontmatter；写前复核 page SHA，并在锁内再次全树复核 rule ID 唯一性
  （并行校验的两批不能把同一新 ID 落到不同页）。固定且按序执行

  `knowledge/tools/check_knowledge_tree.py`、`knowledge/tools/check_wiki_lint.py`；
  validator 缺失则写前 fail closed，执行失败/超时则逐 byte rollback 全部目标页。
  同一 work checkout 的 writer 以 process 内 mutex 与位于系统临时目录的
  `flock` 串行化；等待后的 SHA 复核让第二个 stale writer 失败，不会覆盖首个结果。
- **知识出版 orchestration 留在宿主**：SDK 不 clone、调用 model、管理出版 ledger、commit、
  push、开 PR 或 schedule。ReviewBot 必须向 `KnowledgeCurator` 传 dedicated work
  checkout，并继续拥有重试、artifact 与本地补丁导出；SDK 也绝不写 packaged
  knowledge tree。自适应 DirectClient 仅管理知识上下文的运行账本，不代替知识出版治理。
- SDK、Quality API 版本常量为 `1.0.0`；Direct 为 `1.1.0`，Strict 为 `1.4.0`
  （1.1 新增 `finding_dispositions`；1.2 新增 `findings`；1.3 新增显式 recheck；
  1.4 新增 `StrictRuntimeConfig.max_workers`），Knowledge 为 `1.1.0`
  （1.1 向后兼容地新增 `catalog_entries()` / `KnowledgeCatalogEntry` 与容量预检），
  distribution 为 `0.2.0`；`Capabilities.knowledge_api_version` 与
  `supports_knowledge_curation` 组成 ReviewBot 的 paired-release 握手，避免只按
  wheel 名称误接缺失知识 API 的 artifact。后者只有完整 apply 所需的 process file
  lock 在当前平台可用时才为 true，否则 fail closed；旧 `contract.py` 仅做
  dict/helper 兼容投影，不拥有第二套版本事实。

## 依赖方向

`sdk.v1.models` 为纯 stdlib 模型；`direct` import-time 依赖模型、资源解析与知识上下文／
containment helper，但不创建运行账本或读取宿主配置，
函数调用时才向下进入 `direct_routing`；`strict` 构造时才向下进入
headless `app.RunService`。`sdk.v1.knowledge` 是兼容导出，实际实现位于
`knowledge_service.curation`；后者只依赖 stdlib、公开模型和显式 work checkout，
并以 subprocess 运行上述两个固定 validator。任何 server 都不得被 SDK package
initializer 反向 import，provider domain 也不得反向依赖 ReviewBot。

## 测试

`test/test_sdk_v1.py` 钉住公开 import 边界、两 adapter wheel 路由、无绝对知识
路径、有界 context 绑定、feedback/disposition 透传、Strict serializer/path
边界和真实 runtime capabilities smoke；CI 另在 clean venv 中安装 wheel 再执行
公开 API smoke。`test/test_sdk_knowledge_v1.py` 钉住 repo-scoped catalog、prompt
fence、strict proposal shape/ID/source/page 校验、typed index accounting、append-only
写入、固定 validator 顺序、missing-validator fail-closed、multi-page byte rollback、
tamper/stale detection 与两个 curator 的 writer serialization；测试只创建临时
knowledge checkout，不改仓库真实 `knowledge/`。

## Carried finding rechecks

Direct 1.1 / Strict 1.3 accept typed `CarriedFinding` inputs (unique IDs, source head, severity, location, title and evidence). Direct binds the carried set into its issued context and validates `FindingRecheck` answers. Strict passes the set through the policy allowlist, performs a read-only recheck against the frozen PR head, and persists answers for the public structured result. Every carried ID needs an explicit outcome: `fixed`, `still_affected`, or `unverified`. Missing evidence is never a fix.

## 2026-09-28 知识视图
`DirectClient` 每次 `plan` 解析一个 `KnowledgeView`，文档引用与 `resource_revision` 均取自该视图；`validate` 按计划时钉住的知识根校验资源未变，激活新快照不会使进行中的 review 失效；`diagnostics.knowledge_snapshot` 报告所用快照。`read_document(..., review_context_id=...)` 从该 review 计划时钉住的知识树读取（含分页），未签发的 context id 被拒绝；不带 context id 时读当前激活树。

## 2026-09-28 退役规则不再提供
`DirectClient` 返回的文档内容与 `sha256` 均基于去掉退役规则后的服务文本（无退役规则时与磁盘字节一致）。

## 2026-09-28 trace/1 导出
`sdk.v1` 导出 `TraceStore`、`TRACE_SCHEMA`、`trace_context`、`redact`（见 `trace_store.md`），RB 用同一 schema
记录 review 的模型调用、判定与结果；导入它们不加载 `config`/`contract`/`mcp_server` 等私有模块。

## 2026-09-28 计划携带知识快照
`DirectReviewPlan` 新增 `knowledge_snapshot`（激活快照 ID、`packaged` 或 `unverified`，从不暴露开发树路径）
与 `knowledge_tree_sha256`（已验证快照的清单树哈希，否则为空）。同一 `review_context_id` 下的文档读取都来自这份
快照，RB 应随每次 review 记录这两个值。

## 2026-09-29 未测公开函数候选
`DirectReviewRequest` 新增可选 `diff`（冻结 head 的 unified diff），`DirectReviewPlan` 新增
`untested_public_api`（`direct_routing` 的同名块，默认空字典）。两者都是可选增量字段，旧调用方不受影响（#164）。


## 2026-10-08 Signed containment

Opt-in protocol 1 exports `knowledge_maintenance_status`, `knowledge_usage_record`, `knowledge_availability_check`, `knowledge_publication_guard` and `knowledge_policy_install`. DirectClient and StrictRuntimeConfig accept explicit maintenance configuration. Usage accepts an exact privately issued provider context; arbitrary caller hashes cannot mint provenance. Retrieved and actually injected units are separate from conservative Strict snapshot scope. Current generation is checked at Direct validation and Strict polling; held Strict results retain the original result with top-level state `held`. Hosts hold the publication guard around final admissibility and the external write; the installer drains that same lock before signed policy publication and ACK. ACK includes actual current knowledge snapshot/tree and corrected-content observations, separately from protocol readiness. No service private key crosses this SDK boundary.

Direct plan receipts bind a provider-private durable delivery session. Adaptive follow-up read/search/related deliveries and continuations after expansion join the same session before returning; legacy `read_document(review_context_id=...)` pages join their original review's session as well. `validate` and the generic final `knowledge_availability_check` resolve all delivered dependencies, including when the host reuses its initial receipt after another SDK instance resumes an adaptive session. Caller-supplied session IDs cannot create that binding. Enforced document reads require the existing provider-issued `review_context_id` and include it in `DocumentPage.to_dict()` so identical pages in concurrent reviews retain separate provenance. The optional field is omitted when empty; disabled standalone reads retain compatibility. SDK retrieval does not claim model injection; the host records actual injection separately.
