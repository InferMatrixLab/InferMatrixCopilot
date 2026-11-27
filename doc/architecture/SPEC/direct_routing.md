# direct_routing.py —— 规范

<!-- verified-against: 2026-11-15 -->

`LOC ~880 · Direct 模式完整策略包与仓库中立的知识路由 · refactor-status: stable`

## 职责
Direct 模式的知识路由**机制**。模块中不出现任何被服务仓库的名字：每个仓库的
owner/model 路由表是知识数据 `knowledge/repos/<repo>/_routes.yaml`，仓库身份和别名
优先来自同一固定视图的 `_repositories.yaml`，旧 adapter 别名保持兼容。
公开面由 `contract.py` 再导出。

## 公开契约

`direct_review_plan` 另返回 `related_knowledge`：从本次 `KnowledgeView` 按 diff 路径和描述
检索的有限解释性正文。`navigation_policy.related_document_read_paths` 只列截断背景页，
每页增加一次 `execution_budget.knowledge_file_reads`；完整内嵌内容不需要再读文件。
owner/model 路由仍先按描述选择，路径仍用于路由范围校验；背景检索不增补硬规则路由。
经 `contract.py` 再导出的五个名字：`direct_review_plan`（完整、一次性的
Direct policy bundle）、`direct_knowledge_routes`、
`direct_execution_budget`、`direct_completion_result`、
`direct_mandatory_review_guides`。其余全部下划线私有 —— 仅供
`thin_mcp_server` 既有调用点/测试使用（它继续直接 import 下划线名）。

## 不变量
- **Direct negative-claim evidence**: the canonical `first_review_checklist`
  requires tracing concrete production callers, subclass/keyword forwarding
  and the consumer before claiming a missing input or effect. Cleanup claims
  inspect the actual library callee at the applicable version/platform and
  check success/exception ownership before proposing potentially duplicate,
  non-idempotent cleanup. Unreadable source or unverified dependencies remain
  explicit evidence gaps, not proof of absence; independently demonstrated
  reachable defects remain reportable. This is host guidance carried by the
  raw plan and SDK serialization, not a new completion field or semantic gate.
- **repo 守卫最先跑**：不支持的仓库在任何路由计算之前被拒 —— 修的是一个
  真实历史 bug（守卫曾排在空 intent 提前返回之后，向不支持的仓库泄漏
  owner 知识）。
- **quick map fail-closed**：`_direct_quick_map(path)` 读文件后委托给 `_direct_quick_map_text(text)`（同一提取逻辑；kb init 用后者在落盘前检查它要路由的页面）返回内嵌代码地图与状态
  `{ok, truncated, unavailable}`，`truncated` 不是装饰 —— 把残图当全图
  与缺图同罪、且更难察觉；`_direct_route` 据此置
  `read_required = status != "ok"`（"自己去打开"是真回退，"什么都不给
  又不许看"不是）。
- adapter-backed changed-file 路由同样拆开 `(quick_map, status)`，绝不把 tuple
  当成文本跨边界，也绝不把 unavailable 误报成无需读取。
- **每请求一个 `KnowledgeView`**：入口函数在开始时解析一次视图（未设置
  `KNOWLEDGE_ROOT` 时为打包知识；设置时为激活快照的真实目录并按清单校验），
  本请求内所有读取经同一视图。模块不再在导入期缓存知识根；`_KNOWLEDGE` 只保留
  为模块级 `__getattr__` 惰性别名。
- **路由选择按数据而非仓库名**：仓库有非空 `_routes.yaml` → title/body owner
  路由 + 模型路由 + scope fallback；否则有 adapter → changed-file 路由；否则
  `unsupported_exact_router`。已注册但未生成 owner routes 的仓库返回显式
  `description_unrouted`，仍可检索自己的说明知识；不得借用其他仓库路由。
  `_routes.yaml` 引用的页面不存在时 fail-closed。
- `repo` 必填：空值抛 `ValueError`，不再默认某个仓库；形似路径的仓库名直接
  视为不支持。
- **changed files 校验选择、绝不静默替换选择**：title/body 选 owner，
  diff 只报告支持或矛盾；scope-fallback 是最后手段且永远显式
  （`status="scope_fallback"`）。
- `_direct_execution_budget` 是**硬顶**预算字典（`hard_ceiling=True`、
  一次有界扩展）；docs-only PR 走更便宜的 profile。
- `_direct_completion_result` 是**机械结构门**，不校验证据真假：
  单条最终评论、`subtraction_signal ∈ {none, triggered}`、
  `evidence_head_sha` 7–40 位十六进制、`existing_feedback_status` 枚举、
  `finding_dispositions` 的 anchor/disposition/existing_thread/
  head_recheck 约束。
- **未测公开函数候选（#164）**：`direct_review_plan(..., diff="")` 有 diff 时带
  `untested_public_api`（`status=ok` + 候选 + 评审规则），只基于 diff 自身的测试
  文件——provider 没有 PR head 的 checkout，树内测试搜索由 agent 按 checklist 完成；
  无 diff 为 `{"status": "no_diff"}`，adapter 关闭时为 `disabled`。
- 叶子模块：**绝不** import 任何 server 模块
  （`test_contract.py::test_direct_routing_does_not_import_a_server_module`）。

## 边界 —— 不属于这里
不执行、不调模型；只有机制，路由表在知识数据里。多 adapter 桥接经
`_normalize_repo`/`_adapter_for_repo` 走 `adapters/`。

## 依赖（允许）
stdlib + PyYAML + `.adapters`（AdapterError / AdapterRegistry / RepoAdapter）+
`.knowledge_view` + `.sdk._resources`。
位于 `contract.py` 和 `thin_mcp_server.py` 之下。

## 扩展点
新 owner 路由/模型规则 → 改对应仓库的 `_routes.yaml`（schema_version 1：
`owners[{owner, path, signals, scope_prefixes}]`、可选 `models{dir, page}`）；
新仓库 → 提供固定视图 registry 与 `_routes.yaml`，不要求打包 adapter、不改 `src/`。
已有 adapter 仓库继续兼容。自适应 SDK 通过统一会话对本模块选出的规则/导航再计量；
旧 bundle 的 6,000 字符相关背景预算保持不变。

## 测试
`test_sdk_v1.py::test_direct_plan_carries_negative_claim_tracing_guidance`
checks that the public SDK plan and its serialized checklist retain the
canonical caller/callee, cleanup-safety and evidence-limit guidance. This
is a prompt-delivery regression, not proof of model compliance or a measured
review-quality improvement.

`test_knowledge_view_routing.py`（150 个真实 vllm-omni PR 的黄金路由输出、
第二仓库经自身 `_routes.yaml` 路由、快照切换下一请求生效、快照篡改 fail-closed、
SDK 完成校验钉住计划时快照、模块源码不含仓库名）；`test_contract.py`（公开家、import 方向、中立性豁免）；
`test_thin_mcp_server.py` / `test_thin_mcp.py`（经新家继续锻炼全部
下划线函数：路由、预算、完成门）。

## 重构备注
原 `_DIRECT_OWNER_ROUTES`/`_REPO_ALIASES` 已外置（`known-debt` 清零），
`test_v2_p0.py` 与 `test_repo_vocabulary.py` 中本模块的泄漏上限随之移除。


## 2026-10-08 Signed containment

Route-table validation checks unused navigation targets for integrity without serving their content. Selected owner/model reads pass `KnowledgeView.path` and current containment. Quickmap extraction first removes retired rules. Related retrieval excludes specifically held pages; invalid or stale policy still fails the request closed.

With containment enabled, the raw `direct_review_plan` also returns a private-registry-backed `knowledge_usage` receipt for its selected route, procedure, and related-document dependencies. It binds a private delivery session for later raw document reads/searches without claiming model injection inside the planner. The public `direct_completion_result` requires that receipt and rechecks all original and follow-up issued resources against the current policy. Missing, forged, stale, or held provenance returns `partial_review` with `publish_ready=false`; the original structural evidence checks remain in force. The SDK forwards its own issued context receipt to the same gate. Disabled deployments retain the existing bundle and completion result shape.

