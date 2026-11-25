# intent.py —— 规范

<!-- verified-against: 2026-11-25 -->

`LOC ~379 · 任务层 · refactor-status: ok`

## 职责
把一条自然语言命令分类成 `TaskSpec`，或者一个澄清问题；并把复合命令切成有序列表。

## 功能
LLM 把命令（kind / pr / issue / flags）分类成 `TaskSpec`；低于置信度门或显式要求澄清时
→ 提问，**绝不猜测**。`parse_intents` 按连接词切分，并延续前一段的 PR/issue
（"… then review it"）—— 那是**切分**，不是分类。

## 公开契约
`parse_intent(text, llm?, default_repo, model?) -> IntentResult`；
`parse_intents(...) -> list[IntentResult]`；`IntentResult(spec?, clarify)`。

## 不变量
- **确定性优先，然后才是 LLM。** 预解析层用纯代码解决 GitHub URL 和无歧义的
  `<verb> pr/issue N` 命令，**不花一次 LLM 调用**；只有真正自由形式的输入才进分类器。
  （本规范此前写着"仅 LLM，无确定性解析器"——那描述的是 2026-08 之前的设计，已不成立。）
- **仓库身份被完整校验。** URL 必须 owner **和** repo 都匹配；同名但异主的仓库会被
  **拒绝**，而不是静默地跑在本地 checkout 上。`resolve_repo_alias` 同时服务 Strict MCP
  入口。
- **双路径 `mode` 在这里设定**：默认 `eco`，只有用户显式要求时才是 `performance`
  （确定性短语正则 **或** 分类器的 `performance` 标志）。
  **成本敏感的决策绝不靠猜。**
- **分类调用的补全预算必须容得下"思考"**（两处调用都是 `max_tokens=8_000`）。
  推理模型会先把预算花在思考上，500 的上限下它**一个 JSON 字符都吐不出来**，于是一条
  完全清楚的命令被答成"I couldn't parse that"。`review/planner.py` 踩过同一个坑并定在
  8000；**修复轮同样要给足**——那正是被饿死的模型会落到的地方。
- PR 评审可以按明确请求附加数值评分，例如 `review PR 7 with a score`。
  省略或否定评分请求时保留默认值。
- 空命令或未配置 LLM → 澄清（绝不猜测）。
- **有歧义就澄清 —— 绝不猜测**：LLM 置信度 < 0.7、显式澄清、畸形回复、未知 kind，
  全都 → 澄清。
- **C7**：只有终端输入到达这里；抓取回来的 GitHub 文本**永远不会**。抗注入靠的是
  **通道分离** + LLM 的低置信度信号（可疑命令会澄清，绝不执行）。

## 边界 —— 不属于这里
不执行、不规划、不读仓库/网络。**只做 文本 → TaskSpec。** `parse_intents` 里的
切分与延续是**断句**，不是分类（分类归 LLM）。

## 依赖（允许）
`llm.py`、`task_spec.py`。

## 扩展点
新 kind → 加进 `TaskKind`/`KIND_TIER`（`task_spec.py`）以及这里 `_LLM_SYSTEM` prompt
的 kind 列表。**没有 hint 表需要维护。**

## 测试
`test_intent_taskspec.py`（LLM 路径契约：映射、置信度门、澄清透传、无 LLM、畸形回复）、
`test_phase_b.py`（用假分类器测复合切分 + 延续）。

## 重构备注
`default_repo="vllm-omni"` 这个默认参数是被允许的 2 个仓库字面量之一（有泄漏上限）
—— 保留它们，**不要再加**。生产路径总会传入 LLM（`cli/`）；intent 现在**需要**一个
—— 没有离线快路径（这是"仅 LLM"的刻意代价）。如果将来 LLM 也要负责复合切分，
`parse_intents` 可以直接返回列表，`_COMPOUND_SPLIT` 与延续逻辑一并消失 ——
那是更大的改动，目前不需要。

## 自进化接入（2026-10-04）

LLM 意图分类允许 `workflow_improve`，表示 trace 工作流的改进周期；权限仍由 TaskSpec 的 kind 推导。
