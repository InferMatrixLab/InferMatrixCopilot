# knowledge_context.py —— 规范

<!-- verified-against: 2026-10-29 -->

## 职责与公开契约

`KnowledgeContextService` 是 SDK、MCP 与 Strict 共用的、固定快照的知识上下文服务。
`ContextBudget` 声明模型窗口与留白；`ContextError` 拒绝身份、权限或预算不一致。
`open_session` / `status` / `expand` 管理会话；`related` / `search` / `read` / `inject`
返回同一累计预算下的实际正文和引用。服务不调用模型、不改知识页、不宣称评审准确率。

With current human agreement, a delivery additionally issues a `knowledge_feedback`
receipt for the exact post-budget fragments, using the session's review ID as task identity. Cached
requests retain a successfully issued receipt. After a transient storage failure,
a cached retry can repair feedback metadata from the exact saved model content,
after the original integrity and availability checks. The cache digest is updated
without rerunning retrieval, spending tokens or changing delivered source content.
Storage failures expose only `feedback_storage_unavailable`, never raw exceptions.
Prior containment receipts remain valid while the updated packet gets its own
issuance record. No omitted sibling rule becomes eligible from
another fragment of its page. Feedback state is outside the knowledge tree and
works independently of optional signed containment. Neither issuance nor a
retrieval increments positive/negative counts; an evidenced use rating does.

Learning and feedback are disabled by default. Without current acceptance,
feedback metadata is `{status: "agreement_required", targets: []}` with no delivery
ID, and the source delivery remains available. Cached deliveries recheck agreement;
old receipts do not permit grading or uploads after revocation. Host contracts
check status before extra distillation/grading tokens. Agreement version or
destination changes require human renewal, independent of containment and context
budget. Local status/reports and source reads do not require acceptance.

## 身份与权限

会话身份绑定快照树哈希、仓库 ID、知识切片、源码 pin、目录和政策哈希、PR 源码 pin、
review ID、计量器及预算配置。已验证快照每次读取复核 manifest；开发目录绑定真实文件
内容摘要。恢复复用固定根，不能悄悄切到当前 active。来源 pin 支持完整 SHA-1 / SHA-256。

仓库从固定视图的 `_repositories.yaml` 解析，旧目录只保留精确切片兼容，未知版本不猜测。
跨仓库读取同时要求主机 `allowed_repositories` 授权与当前仓库中 `status: confirmed`
的依赖；目标源码和目录哈希必须匹配。工具参数只能选择目标，不能授予权限；所有目标
共享同一个累计预算。

## 预算与正文

默认初始 24,000、最高 64,000；模型上下文默认配置 131,072，源码留白 32,000、输出
留白 8,192，还可预留其他 prompt。可用上限取知识上限和模型剩余窗口的较小值。
扩大必须给出原因，不能占用留白。这些是主机配置，不能冒称探测到 served model 窗口。

默认以 UTF-8 字节数作保守计量上界，`token_accounting` 明示此单位；主机可传稳定 ID
的 tokenizer。`actual_provider_usage` 保持 unknown。计量包括正文、来源字段、JSON
转义与 untrusted 围栏。仅 `model_content` 用于注入；`documents` 是不带额外正文的引用。
片段截断清空完整已注入维度及对应 basis/mode/validation 标签；可用维度仍可补读。

SQLite 运行账本必须位于知识树之外。事务串行记账，精确来源单元去重，重复请求幂等；
扩大后允许继续未交付片段。缓存命中复核请求收据、正文摘要、计量和源文件；这属于完整性
检查，不是针对可任意改写整个主机账本的签名。搜索与阅读重叠片段不保证全局语义去重。

## 接入与验证

新版 SDK 显式 `plan_adaptive` / 会话接口使用本服务；旧 `plan` 保留两页、6,000 字符契约。
MCP 暴露同一 open/read/search/related/expand；Strict 在注册表快照默认 adaptive，旧树
默认 legacy。规则与 briefing 经 `inject`，只接收真实文档 ID，不接收任意宿主散文。

`test_knowledge_context.py`、`test_knowledge_retrieval.py` 验证累计预算、并发、恢复、
留白、跨仓库权限、缓存篡改、实际注入标签以及 SDK/Strict 的固定快照接入。


## 2026-10-08 Signed containment

With explicit `knowledge_maintenance` configuration, session/cache identity additionally binds current containment generation and policy digest. Every delivery rechecks this identity and verifies document admissibility before cached content is returned; old contexts require reassessment after containment changes. Disabled configuration preserves the previous session identity.

Every nonempty adaptive delivery also enters the provider-private durable session issuance journal before it is returned, including cached reads and search/related continuations after a budget expansion. Earlier plan receipts resolve that complete journal during final availability checks; a newly held follow-up page therefore blocks completion even after SDK restart. Budget expansion alone delivers no source content and creates no injected-usage claim.
