# 知识使用追踪

追踪把执行步骤、命中的知识条目、实际提供的内容、规则检查与证据关联起来。
完整设计见 [knowledge_trace](../architecture/SPEC/knowledge_trace.md)。

## 开关和存储

通过进程环境配置；不需要模型凭据，也不会增加模型调用：

```bash
export KNOWLEDGE_TRACE_MODE=best_effort
# 可选：将 Direct 和 Strict 的记录放入同一个运行状态目录
export KNOWLEDGE_TRACE_ROOT=/path/to/runtime/knowledge-traces
```

模式为 `off`、`best_effort`（默认）或 `required`。关闭时不采集记录；
best_effort 的存储故障单独标为 degraded；required 在关键记录失败时阻止继续
宣称追踪完整。它不撤回此前已发生的动作，也不改变审查规则本身。

Direct 默认使用用户运行状态目录中的 `knowledge-traces/`。Strict 未设置
`KNOWLEDGE_TRACE_ROOT` 时使用 `<run-dir>/knowledge_trace/`，运行结束生成
`KNOWLEDGE_TRACE.json` 和 `KNOWLEDGE_TRACE.md`；JSON 中包含 trace ID 和存储位置。
内容通过 TraceStore 脱敏后保存为内容寻址 blob，记录不进入产品知识树，也不会
自动发布到 GitHub。

## 查看与导出

```bash
infermatrix-copilot knowledge-trace show <trace-id> --root <trace-root>
infermatrix-copilot knowledge-trace show <trace-id> --root <trace-root> --json
infermatrix-copilot knowledge-trace export <trace-id> --root <trace-root> \
  --format markdown --out KNOWLEDGE_TRACE.md
infermatrix-copilot knowledge-trace export <trace-id> --root <trace-root> \
  --format json --out knowledge-trace.json
infermatrix-copilot knowledge-trace query --root <trace-root> \
  --repo vllm-omni --rule DIFF-1b --limit 100
```

`show --json` 的原始事件列表支持 `--cursor` 和 `--limit`；报告中的步骤投影
覆盖该会话。`export` 收集所有事件页。`query` 返回有界的匹配事件摘要。
即使采集模式为 off，也可以读取已有记录。

## Direct 宿主接入

Direct review 的返回值增加 `knowledge_trace`，SDK plan 也带追踪元数据。
随版本提供的 `imreview` 工作流会携带固定的 head、trace ID 和仓库信息，按步骤
批量记录实际使用，并在结束时关闭追踪；旧宿主需采用更新后的工作流才能记录这些回执。
服务返回知识时记录 `provider_response`；宿主确认提供到 agent 上下文时才记录
`host_context`。SDK `DocumentRef.excerpt` 是实际提供内容的一部分，即使宿主
没有再调用文档读取工具，也会被记录。

自适应知识会话只记录累计预算后 `model_content` 中实际提供的正文，
不会为追踪重新读取 `documents` 引用或增加片段。检索元数据保留来源仓库、
source pins 和上下文会话；continuation 或无法确认坐标的片段标为区间未知，
预算截断明确保留。上下文服务已授权的跨仓库内容继续正常提供；单仓库追踪
为其记录观测缺口。原用户协议、反馈回执和 containment 校验仍然生效。

MCP 提供四个工具：

- `start_knowledge_trace`：显式启动一次追踪。
- `record_knowledge_usage`：批量提交宿主回执、证据和规则检查。
- `finish_knowledge_trace`：结束追踪。
- `get_knowledge_trace`：读取报告。

文档读取或搜索携带 `trace_id` 时使用会话固定的知识版本。过期或不可用的上下文
明确报错，不会切换到新快照。调用方必须使用同一个仓库；trace ID 不赋予额外访问权限。

嵌入式调用使用 `sdk.v1.KnowledgeTraceClient`。以下示例只演示宿主提交契约：

```python
from infermatrix_copilot.sdk.v1 import KnowledgeTraceClient

client = KnowledgeTraceClient(root="/path/to/runtime/knowledge-traces")
report = client.get_report(trace_id, repository="vllm-omni")
exposure = report["exposures"][0]
client.record_usage(trace_id, [{
    "event_type": "content_provided",
    "step_id": "review.defaults",
    "exposure": {"source_exposure_id": exposure["exposure_id"]},
}], repository="vllm-omni", idempotency_key="defaults-delivery-1")
```

宿主回执只能引用既有内容，不能覆盖来源内容、哈希或规则身份。
assessment 引用版本固定的 `knowledge_ref_id`，或无歧义的 `document_id` 与
`rule_id`；支持/反驳结论必须关联证据。没有运行证据时记录 unresolved。

## 如何解释报告

- retrieved：条目被选择；不代表正文已经提供。
- provided：具体片段在指定边界出现；不证明模型注意到它。
- assessed：agent 显式记录适用性、检查与证据；引用有效不等于结论已独立证明。
- 未评估：没有应用声明，不能推断为“不适用”或“未使用”。

quick map 中出现规则 ID 只证明提供了引用，不证明完整规则正文已提供。
截断、无命中、失败读取、缺失证据和旧 head 的证据会保留在报告中。

Strict API 可以观测提交给模型的请求；harness 记录提供给外部 CLI 的 prompt，
外部 CLI 自带的文件读取和内部模型请求仍可能不可观测。skills/debug memory
来自独立存储时，保留检索元数据并标注与知识文档引用的区别。

报告支持从 finding 追溯规则和证据，但不据此宣称知识导致了该 finding。
计数描述记录覆盖范围，不能当作规则正确率。
