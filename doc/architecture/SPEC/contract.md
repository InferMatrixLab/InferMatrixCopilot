# contract.py —— 规范

<!-- verified-against: 2026-11-25 -->

`旧版跨仓库契约兼容层 · refactor-status: compatibility-shim`

## 职责
本 copilot 的**旧版对外消费契约兼容层**。新宿主只允许 import
`infermatrix_copilot.sdk.v1`；本模块继续服务既有 MCP 与尚未迁移的调用者。
它的存在源于一次真实事故：
下游仓库曾经用 `importlib` 伸进 `thin_mcp_server` 拿四个 `_direct_*` 私有名，
一次重命名就让另一个仓库在运行期断裂、且没有任何构建期信号。

## 公开契约（`__all__`）
`SDK_API_VERSION` / `DIRECT_API_VERSION` / `STRICT_API_VERSION` /
`QUALITY_API_VERSION` / `KNOWLEDGE_API_VERSION`（SDK、Quality 为 `"1.0.0"`，Direct 为 `"1.1.0"`，Strict 为 `"1.5.0"`，Knowledge 为 `"1.1.0"`，且都从 `sdk.v1.models`
取唯一值）。`capabilities(max_strict_workers=1,
supports_file_locking=True) -> dict` 委托 SDK typed handshake 再投影为兼容
dict；它包含 distribution/SDK/Direct/Strict/Quality/Knowledge 版本、resource
revision、supported repositories，以及 `supports_expected_head`、
`supports_structured_result`、`supports_post_false`、`supports_file_locking`、
`supports_idempotent_strict_start`、`supports_knowledge_curation`、
`max_strict_workers`（服务真实的并发 worker 数，即 `STRICT_MAX_WORKERS`，
同一 checkout+PR 的 run 仍逐个执行）。其余兼容导出包括 `build_review_result(run_dir) -> dict`（结构化评审
结果：`contract_version`、`run_id`、`state`、`reviewed_head_sha`、`verdict`、
`summary_markdown`、`comments`、`finding_dispositions`、`findings`、本次运行记录的
`review_claim_evidence` 及按请求生成的 `review_score`、
`stale`/`expected_head_sha`/`actual_head_sha`、`diagnostics`）；`unknown_run_result(run_id)`（显式
`state: unknown`，绝不抛错 —— 丢响应和还在跑必须可区分）；
`sanitize_comments` 与 `COMMENT_FIELDS`，以及 `finding_id` / `build_findings`；另有从 `direct_routing` 再导出的
`direct_knowledge_routes` / `direct_execution_budget` /
`direct_completion_result` / `direct_mandatory_review_guides` / 完整的
`direct_review_plan`。
质量面另有 `build_quality_result` / `unknown_quality_result`：返回
`reviewed_head_sha`、`ready|concerns|needs_rework`、置信度、摘要及白名单化的
`criterion/evidence/path/line` reasons；能力握手暴露
`quality_api_version` 与 `supports_quality_review`。

## 不变量
- **依赖方向单向、由测试钉住**：本模块 import 数据层（`run_status`、
  `run_trace`），**绝不** import 任何 MCP server 模块；server 向下 import
  这里。`test_contract.py::test_contract_imports_no_server_module` 钉住。
- `build_review_result` **只读 run 已持久化的东西**：对已完成、进行中、
  评审步之前就死掉的 run 都成立；JSON 读取 fail-soft（早死的 run 合法地
  缺文件）。
- verdict 是**字段**不是 Markdown 刮取；stale 是**数据**
  （`stale`/`expected`/`actual`），不是从散文推断。
- `sanitize_comments` 按 `COMMENT_FIELDS` 白名单
  （file/line/severity/comment/evidence/suggestion）——内部记账键
  （`_verified`、`corroborated_by` 等）绝不泄进消费方输出。
- `comments` 是**终局集合**，不是候选集合：评审自己的取舍已在评审步
  应用完毕（见 `engine/steps/review` 页）。`finding_dispositions` 是这件事的
  凭据——每个候选一条 `{anchor, disposition, declared}`，按
  `DISPOSITION_FIELDS` 白名单投影，被撤下的评论正文本身绝不过界。
  消费方因此无需把散文里的取舍和手上的列表对账（#141）。
- `findings` 按发布顺序对每条最终评论输出
  `{finding_id, severity, anchor, head_sha}`。identity 取文件路径与完整评论文本
  （合并空白并 casefold）的 SHA-256 前 16 个 hex，不包含行号；anchor 是
  `{path, line}`，无文件时为 `None`。severity 使用最终发布值，head 绑定
  `pr_head_sha`。保留重复 identity，不与按 anchor 记账的 disposition 做 join，
  避免同位置的撤下候选污染已发布 finding。评审前失败的 run 返回空列表。
- `finding_rechecks` 独立返回每个 carried ID 的 `{finding_id, head_sha, outcome, evidence}`；
  outcome 为 `fixed` / `still_affected` / `unverified`。缺失、重复、错 head 或无证据的结果
  不构成修复，`rechecks_complete=false` 并在 `recheck_missing` 暴露缺口。
- 本模块自身保持仓库中立；仓库专属的 Direct 路由表住在
  `direct_routing.py`（见其页）。
- 能力身份的权威实现在 `sdk.v1.get_capabilities`；这里不维护第二份版本或
  resource-revision 算法。

## 边界 —— 不属于这里
不调模型、不执行 run、不做策略强制（`mcp_policy`）、不实现 Direct 路由
（委托 `direct_routing`）。

## 依赖（允许）
`run_status`、`run_trace`、`direct_routing` —— 只向下。消费方：
`mcp_server.py`（capabilities / build_review_result / unknown_run_result）
与仓库外的 reviewbot（`direct_*` 四件套）。

## 扩展点
消费方需要的新字段 → 加进 `build_review_result` 并递增 API 版本；
新的 Direct helper → 在 `direct_routing` 实现、在这里再导出。

## 测试
`test_contract.py`（覆盖：verdict 字段化、评论白名单、stale 即终局事实、
早死 run 的降级结果、unknown-run 显式化、能力上报、import 方向、
本模块仓库中立、finding identity/最终 severity/head 绑定、重复保留与 disposition 隔离）。

## 重构备注
新模块（PR2 mixed-mode contract 拆分）。保持它薄：任何"顺手在这里实现"
的诱惑都在重造 thin_mcp_server 的巨石。

## 2026-09-28 知识快照
`build_review_result` 从 `<run>/knowledge.json` 读取钉住的知识，在 `diagnostics` 中给出 `knowledge_snapshot`
与 `knowledge_tree_sha256`，供宿主（RB）与 review 一起记录。
