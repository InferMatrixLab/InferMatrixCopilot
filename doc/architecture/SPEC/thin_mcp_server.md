# thin_mcp_server.py —— 规范

<!-- verified-against: 2026-10-29 -->

`LOC ~490 · 默认 MCP：Direct 门面 + Strict 入口 · refactor-status: ok`

## 职责
安装器**实际注册**的那个 MCP 门面：以**零模型**提供 Direct 模式的知识路由，
并在被要求时桥接到 Strict。
同时通过 RFC SDK 委托已配置工作区的草稿、纳管、进度与人工决策操作。

## 功能
工具包括：`review`（按 `mode` 分流）、`validate_direct_review`、
`get_review_status` / `get_review_result`（转发给 `CopilotMCP`）、
`update_knowledge`、`doc_search`、`doc_read`，显式自适应会话
`open_knowledge_context` / `read_knowledge_context` / `search_knowledge_context` /
`related_knowledge_context` / `expand_knowledge_context`，以及
`rfc_request`、`rfc_capabilities`、`rfc_status`。

## 公开契约
上述工具；`build_mcp(...)`；`main()`。`review(knowledge_profile="adaptive")`
返回单一实际 `model_content` 和不重复正文的引用；默认 `legacy` 保持旧契约。
会话工具累积预算，跨仓库目标只在主机授权与 confirmed 固定依赖同时成立时可读；
模型不能通过工具参数扩大权限。模型容量与源码/输出留白是明确配置，非探测结果。

人工来源编辑通过 `rfcs.changes.preview/apply` 委托共享应用，应用必须绑定实际
审阅的 preview_id/candidate_digest 与稳定幂等键。服务和连接就绪状态由
`service.status` / `repositories.check` 返回，门面不自行推断已同步或已验收。
RFC 工具签名为 `rfc_request(action, payload=None)`、`rfc_capabilities()`、
`rfc_status(rfc_id)`；后者调用 `rfcs.status`，返回不含源正文和候选列表的紧凑视图。
`rfc_request` 同样转发 `chat.*`、`rfcs.work` 的人工实现记录，以及 `rfcs.graph` 的
可逆节点分组。聊天调用先返回任务 ID，完整候选需通过 preview/apply 明确确认；
本门面不执行模型工具循环，也不能自行通过验收或修改授权。

Task completion exposes `knowledge_agreement(action="status"|"show"|"revoke")`
through the lazy agreement SDK. It has no accept action, signer, attestation,
credential or endpoint argument. Because revoke changes private local state, the
tool declares `readOnlyHint=false`, `openWorldHint=false`. Status/show do not
create acceptance; only the human interactive CLI can accept actual terms.

Task completion also exposes `knowledge_learning(action, task_id, repo,
source_pin, lessons, packet_digest, proposals)`, `knowledge_feedback(task_id,
delivery_id, ratings)`, read-only `knowledge_feedback_report(repo, limit, scope)`
and `knowledge_feedback_sync(action="flush"|"status", limit=100)`.
Installed skills and server instructions FIRST check agreement status before
extra learning tokens, distillation, grading or uploads. Default missing/revoked/
renewal-required/unavailable agreement means skip optional work and finish the
requested task; offer the actual terms and human signing command once, without
repeated per-task reminders. The host may never accept for the user. After
acceptance, the host model distills locally; the server never imports a transcript
or calls another model. Triggering is before the final task response, not during
progress/status turns. Publication requires current acknowledgement, configured
local or draft-PR mode and both write flags; it never merges. Operator settings
are not consent. Mutating tools declare truthful annotations. Feedback
recording/reporting/sync can contact the configured collector and declare
open-world access. Local persistence precedes upload; failed uploads stay
pending. Report scope auto uses central when configured and never silently
falls back to local on remote failure. Sync status makes no network request.
Neither a destination nor a credential is accepted through tool arguments.

With current agreement, legacy review, doc reads/searches and adaptive packets return
independent `knowledge_feedback` receipts even with containment disabled. Without
agreement, feedback metadata reports `agreement_required` and empty targets with
no delivery ID, while source content remains available. Optional
`task_id` on legacy review/read/search groups follow-up deliveries. Actual
delivery does not imply use: grades require a delivered target, explicit
`used=true`, integer -1/+1, reason and observed evidence. Feedback is private
runtime state, separate from containment and semantic knowledge approval.

## 不变量（**C1**、**C2**、**D1**）
- **Direct 在这个 server 里不跑任何模型。** 它返回知识路由和一份治理契约；阅读由
  **宿主自己的模型**完成。执行主脊完全不参与。
- **路由表与机制已迁出**（上一版预告的拆分点已经发生）：`_direct_*` 全家
  现在**住在 `direct_routing.py`**、完整 bundle 由 `direct_review_plan`
  生成、经 `contract.py` 作为兼容公开面再导出；
  本模块以下划线别名 import 它们，保持既有调用点/测试不变，**只向下**
  委托 —— 没有任何东西从那两个模块向上 import 回 server。下面关于
  quick-map fail-closed、路由不静默替换、仓库守卫先跑的不变量**仍然为真**，
  但其实现体在 `direct_routing.py`（规范见其页）。
- `review` 的可选 `diff` 只对 Direct 生效：原样交给 `direct_review_plan`，
  由其生成 `untested_public_api` 候选（#164）；仍然零模型。
- **Strict 分支透传快照绑定**：`_strict_review_request` 把
  `expected_head_sha`、`repo_path`、`idempotency_key` 一并送进内部请求；
  `review()` 的 Strict 路径按 `strict_readiness(repo, repo_path)`（两参，
  按调用校验 —— `configure_strict_repo` 的进程全局突变已删除）预检。
- **治理靠数据，因为 server 管不住宿主。** "该怎么审"被编码成随返回值一起下发的结构化
  字段：≤3 条路由（内嵌 `quick_map`，3.5k 封顶）、一个硬性的 `execution_budget`、
  一份 checklist，以及 `mandatory_review_guides` —— 跨 owner 的强制评审程序
  （`_DIRECT_MANDATORY_REVIEW_GUIDES`），**失败即关闭**：路径解析不了就报错，
  而不是悄悄发一份少了它的契约。它们也计入 `execution_budget` 的
  `knowledge_file_reads`，所以下发一份读不完的预算是不可能的。
- **`validate_direct_review` 检查的是结构，不是证据真伪**：恰好一条最终评论、
  `subtraction_signal` 的自洽性（`none` 不得附带证据；`triggered` 需要减法项或最小性
  证明）、以及证明本次评审读的是固定提交的 `evidence_head_sha`。
  **它不能也没有**去验证被引用的证据是否真实 —— 声称它能，比不声称更糟。
  启用 containment 后，legacy 和 adaptive Direct 计划另带实际 provider 签发登记的
  `knowledge_usage`；完成工具把该收据原样交给公开 `direct_completion_result`，
  后者按当前签名策略重查知识可用性。缺失、伪造、过期或被 hold 的来源均返回
  `partial_review` / `publish_ready=false`，要求重新取用可用知识并复核。
  这项来源与可用性校验独立于评审结论的证据真伪；未启用时旧返回形状不变。
- **路由绝不静默替换。** `title`/`body` 选 owner；`changed_files` 通常只做范围校验。
  它们只在**最后手段**下选路（当存活路由无一命中它们推导出的 owner 时），且该情况是
  **显式的**：`status="scope_fallback"`、`selected_by="title_body+changed_files"`，
  且每条这样的路由都会说明理由。
- **仓库守卫先跑。** 不支持的仓库在任何路由推导**之前**就返回
  `unsupported_exact_router` —— 否则一个没有描述的、来自陌生仓库的 PR，会被喂上
  本仓库的 owner 知识。
- `_knowledge_path` 阻止逃出知识根；`_guard` 把异常转成 `{"error": ...}` 值返回，
  而不是协议层错误。**唯一的例外是 `_contributing_entry`**：贡献入口随文档搬到了
  `doc/knowledge/`，已经不在知识根内，所以它按设计不走 `_knowledge_path`，而是
  在源码树和 wheel 旁的两个候选路径里定位，都找不到才抛。它是一个写死的常量路径，
  不接受调用方输入 —— 逃逸防护针对的是后者。
- **Strict 绝不启动注定失败的 run**：Strict 分支先查 `strict_readiness`，
  改为返回缺失项。
- `update_knowledge` 只返回知识贡献入口 —— 它**不是** `imupdate` 的发版审计器。
- **每个工具都声明 `ToolAnnotations`，且提示必须真实。** 审批门控的宿主（codex 对
  无注解工具逐次弹批准框，headless 下自动取消，见 #86）靠这些提示放行只读面：
  `review` 保留状态变更（预留 Strict run）与触网（Strict 子进程）；
  `rfc_request` 能写工作区与排队发布，因此 `readOnlyHint=false`、
  `destructiveHint=true`、`idempotentHint=false`。其余十三个工具全部
  `readOnlyHint=true`，相对于源码、知识内容和 forge 都只读；知识会话接口
  会写主机私有运行账本。RFC 能力与进度工具声明 `idempotentHint=true`。
  把一个会修改知识或源码的工具标成只读，比不标更糟。
  （要求 `mcp>=1.8`，注解类型自该版本起可用。）
- **RFC 工作区与身份来自宿主环境。** 每次调用用 `RFCClient.from_env()` 解析
  `RFC_SERVICE_URL` / `RFC_STATE_DIR` 与 `RFC_TOKEN`；工具参数不接受 endpoint 或
  token。共享应用在每次动作及后台执行时重新检查当前权限；远端失败不能切换本地库。
  `RFCClientError` 返回结构化的 `error.code/message`，不暴露凭据。
- **RFC 变更不通过 review run。** 本门面不实现 RFC 状态机、不调用模型、不改验收；
  `rfc_request` 委托共享应用，发布要求明确的 `post=true`、预览摘要和幂等键。
  返回排队操作不能当作发布成功，必须查询其终态。

## 边界 —— 不属于这里
Direct 路径里不调模型；不含 Strict 后台机器（`mcp_server.py`）；不定义策略
（`mcp_policy.py`）。

## 依赖（允许）
stdlib + `mcp` extra + `.direct_routing`（下划线别名 re-import）+
`.adapters` + `.config` + `.intent.resolve_repo_alias` +
`.knowledge_docs` + `.mcp_policy` + `.mcp_server` + 惰性导入 `.sdk.v1.rfc`。

## 测试
`test_thin_mcp_server.py`（42 例，别名保持调用点不变）、`test_thin_mcp.py`、
`test_imreview_output_contract.py`；外加 `test_contract.py`
（`_direct_*` 的公开家与再导出仍然成立）与 `test_e2e_strict_mock.py`
（Strict 快照绑定端到端）。
RFC 工具签名、动作委托、工具注解及宿主身份：`test_rfc_integration.py`。
应用权限与生命周期由 [rfc_service.md](rfc_service.md) 及其测试约束。

## 重构备注
拆分**已发生**（→ `contract.py` / `direct_routing.py`，约 1420 → 627 行）；
留在这里的是 Strict 桥接与工具接线；checklist/progress 已随完整 policy bundle
迁到 `direct_routing`，避免 MCP 和 Python SDK 各拼一份协议。
拆分保住了"server 不跑模型"—— 它仍是 Direct 模式的产品承诺；
后续增长优先落到 `direct_routing`/adapter 数据面，不回到这里。

## 2026-09-28 知识视图
知识根改为每次调用经 `KnowledgeView.current()` 解析；`_KNOWLEDGE` 仅为惰性别名。

## 2026-10-08 Adaptive containment delivery

Raw adaptive MCP responses are actual host-model context delivery. Initial plans and nonempty read/search/related follow-ups record `injected=True` using the privately issued exact packet; JSON-fenced source text is decoded for unit attribution. Metadata-only budget expansion does not claim injected content. All deliveries are bound to the same durable provider-private session, so final validation of the original plan receipt includes later follow-up pages and fails closed if any become held.

With enforcement enabled, raw `doc_read` and `doc_search` additionally require the optional `knowledge_usage` parameter carrying the original plan receipt. They resolve its protected issuance and pinned view, then register actual delivered content in the same journal before returning an updated receipt. Omitting provenance fails closed; activating another snapshot does not switch an existing review's follow-up root. Raw legacy initial delivery records only its actual quickmap/background fragments as injected at the MCP return boundary. Internal SDK calls to the raw planner do not claim injection. Disabled tool signatures remain backward compatible through the optional parameter.
