# engine/agent_runtime/ —— 规范

<!-- verified-against: 2026-11-04 -->

`LOC ~1690（7 个文件） · 引擎（受治理的 agent 运行时） · refactor-status: ok`

## 职责

含 `_repositories.yaml` 的固定快照默认采用 `knowledge_context_profile="adaptive"`；
旧快照默认 legacy，也可显式选择。`doc_search` / `doc_read` / `doc_related`、规则与
`doc_context_briefing` 共用 run 私有累计会话，`doc_context_expand` 必须给原因且保留
源码/输出留白。实际注入正文和哈希写 trace，runner直接注入已围栏的 `model_content`。
跨仓库 target 只选择已确认依赖，授权由 Settings 固定传入；模型工具不能授予权限。


开启 `profile_briefing_enabled` 且证据包含 `pr_diff` 时，runner 使用既有 diff parser 提取
路径，通过 `doc_related` 在首个模型调用前注入同一有界知识背景。内容放入 untrusted
围栏并转义标签；`review_knowledge_context` 记录命中页面、状态和正文长度。
关闭 briefing 时自动背景注入也关闭；无关路径不补入知识。Agent 可按需调用该只读工具。
每个 `kind == "agent"` step 的**唯一**受治理入口，外加评审质量 ensemble。
它是全库信息密度最高、杠杆最大的子系统 —— 曾经是一个 685 行的模块，
现在是一个把底座（dispatch/knowledge/utils）与两个入口（runner/ensemble）分开的包。

## 包内布局（一个文件一个关注点）
- `__init__.py` —— 只做公开再导出（见下方公开面）；无逻辑。
- `dispatch.py` —— agent 的**输入契约**：`AgentDispatchContext`（及其 `render()`）
  和 `BASE_OUTPUT_SCHEMA`。prompt 形状，与控制流隔离。
- `knowledge.py` —— 按仓库限定的知识：`_resolve_adapter`、`_ScopedKnowledge`、
  `_knowledge_stores`、`_retrieve_skills`、`_retrieve_memories`、
  `_knowledge_tools`、`_repo_map_tool`。
- `utils.py` —— 无状态 helper：`_build_evidence`、`_permissions_view`、
  `_coerce_output`、`_to_step_result`（+ 状态→FailureKind 映射）。
- `runner.py` —— `run_agent_step`：组装 dispatch context、打包证据、检索知识、
  跑工具循环、收敛输出、全程记 trace。
- `ensemble.py` —— `run_agent_step_ensemble`：视角多样的 lens 扇出 + verify-and-merge
  归约。
- `moa.py` —— Mixture-of-Agents（2026-07 新增）：lens/draft 提案可以跑在异构的
  `LLM_MIXTURE` 成员上，而 verify-and-merge reducer 仍留在本次 run 的档位模型上。

## 公开契约（可从 `engine.agent_runtime` import）
`run_agent_step(...) -> (StepResult, output)`；`run_agent_step_ensemble(...)`；
`AgentDispatchContext`；`BASE_OUTPUT_SCHEMA`；`_resolve_adapter`、
`_retrieve_skills`（供 step/测试使用）。再导出的 `__init__` 让拆分前的 import 路径
（`from ..agent_runtime import X`）保持不变。

## 不变量（**B4**）
- **唯一入口**：agent step 只能经 `run_agent_step` 做 agentic 工作 ——
  **不允许**为了调查而临时 `ctx.llm.create()`。
- 证据逐项封顶 + 归档 + `<untrusted_data>` 围栏（**C7**）。
- `_ScopedKnowledge`：读取三层有序——runtime（已学得）→ adapter seed → 共享池，
  重名时 runtime 获胜；**写入只落 runtime 侧**（Rev 8 §10：adapter 树在运行时
  只读——提案进仓库 runtime candidates，seed skill 的使用计数走 runtime usage
  journal，seed 文件保持逐字节不变），**且提案只能是 candidate**（**D1/D2**）。
- `_repo_map_tool`：按需拉取，**绝不作为散文注入**；语言不支持 → `capability_gap`。
- briefing 只在 `profile_briefing_enabled` 时进 prompt（消融开关）。
- 输出：base+extension schema、一轮修复、状态→FailureKind；预算耗尽会**强制最终答复**。
  全程 trace（`agent_dispatch`/`agent_output`）。
- ensemble reducer：逐编号候选做 keep/drop/dup、确定性组装、**未提及即保留**
  （fail-open）、共识门控的快路径。
- **零产出的 lens 单独重问一次**（`ensemble_zero_yield_retry`），
  而不是整组重跑；lens 启动**错峰**（`ensemble_stagger_seconds`）。
- **非空的最终答复会被抢救，绝不丢弃**：修复轮之后仍无法解析的输出，
  变成 `needs_review` + `_raw_text`。
- **MoA 预算在代码里强制，不在 prompt 里**（`moa.py`）：每个成员请求先原子
  `reserve()` 一个保守上界，结算时替换为实际用量，**已结算花费永不超过 `moa_max_usd`**
  —— 预约失败就降级到档位模型或跳过该成员，因此**不可能出现不封顶的请求**。
  成员身份是 `model@host`；**密钥只经 env 变量名引用，永不进入日志或 trace**。
- **按 pass 的后端路由**（`review_lens_backends`）可以把某个 seat 放到另一个 provider 上。
  路由必须**在重试中保持**，无法履约时必须**大声回退** ——
  一个被静默改道的 seat，会让这条 arm **不再是它标签所声称的配置**。
  判据是 seat **有没有跑起来**，不是它有没有产出：
  - **跑了但零产出**（安静的 seat）→ 重问**保持原路由**。
  - **压根没跑起来**（proven dead）→ 改跑档位模型，并记 `moa_member_fallback`
    （带 `phase`/`member`/`effective`/`reason`），绝不再问一次已死的成员。
  死亡有**两种形态**，两种都必须接住：裸 API 成员**抛异常**，而 harness transport
  会把一次死掉的会话转成**类型化的非 OK 结果**（"a dead harness is an outcome"）。
  **路由判据是 `member_unreachable`（seat 压根没跑，传输级死亡），不是
  `outcome_blocked`（"有没有产出"—— 对死掉的和被截断但活着的 seat 都答 True）**：
  用后者做路由决定，正是 2026-08-16 把 18/28 个 holdout seat 静默改道的缺陷。
  首轮与**零产出重问**统一走 `_attempt`/`_member_died` 守卫路径（重问那一处
  曾经完全没有守卫，于是一个 403 的成员直接穿过 `asyncio.gather` 把整个 step
  打成 BLOCKED）；`_member_died` 还比对 `member == tier_model`（trace 的
  `same_model` 字段）—— 解析成同一个模型的回退在 trace 里可见为 no-op。
  回退每处至多一次，档位模型自己再失败就按类型化失败返回，**不递归**。
- MoA 成员本身也可以骑上 provider 注册表里的某个 harness（`transport_for_id`），
  与本次 run 自己的 `STRICT_BACKEND` 无关。

## 内部依赖规则
`runner`/`ensemble` 依赖 `dispatch`/`knowledge`/`utils`，**反向绝不允许**；
`ensemble` 依赖 `runner`（反之不成立）。底座文件不 import 任何兄弟入口，因此**无环**。

## 边界 —— 不属于这里
不含任务/规划逻辑；不含仓库字面量（system prompt 仓库中立）。
step 专属的 prompt/lens 住在各自的 step 文件里（例如 `steps/review/prompts.py`）。

## 依赖（允许）
`agent_loop`、`llm`、`memory/*`、`adapters/base`、`profiles/repo_map`、`scopes`、
`tools`、`engine/step`。

## 扩展点
新的"列表输出型" agent step 通过传入 lenses + merge_guidance 来采用 ensemble；
新的知识来源 → 在 `knowledge.py` 里扩展 `_ScopedKnowledge`/`_knowledge_tools`。

## 测试
`test_agent_runtime.py`（参数化 dispatch —— `kind=="agent"` ⇒ 受治理运行时；
无法解析输出的抢救）、`test_agent_ensemble.py`（裁决校准、零产出重问、lens 后端路由）、
`test_moa.py`（预算台账封顶；密钥永不进 trace）、`test_review_step.py`。

## 重构备注
拆分**已完成**（它曾是内聚性的头号目标）。那些内联的评测引用注释是**机构记忆** ——
它们**跟着各自的代码**搬进了兄弟文件，一条都没丢。`ensemble.py`（约 387 行）仍是最大的
文件；它的 reducer 是一个内聚的单一算法，**不是**进一步拆分的目标。

这个包吸收了 v3 期的大部分变化（自 2026-07-12 起 10 个提交：深度评审 pass、
cheap-seat 与按 pass 路由、MoA harness 成员、考古工具）。有两条教训被编码在上面的不变量
里而不是写成散文：**预算保证属于代码，不属于 prompt**；以及**无法履约的路由决策必须
大声失败** —— 因为本轮战役已经测到过三条与其标签不符的 arm。

## 自进化接入（2026-10-04）

影子 agent 使用严格只读 scope，知识检索与 repo map 受读根约束，拒绝知识写入工具。API 与 harness 调用均记录 trace/1；工作流的采集和配置指纹由执行器绑定，普通生产运行保持既有工具能力。

## 2026-11-04 知识使用追踪

`knowledge_trace.py` 是 Strict 到共享 `knowledge_trace/` 协议的唯一桥接：
每次执行尝试建立一份会话，并行步骤和 MoA 成员保留完整的限定 step ID。
`dispatch` 增加可选 `knowledge_assessments`，把规则、具体检查、证据和 finding
关联；缺失声明保持未评估。每次模型调用的证据 ID 独立，重复 step 不覆盖旧证据。

文档工具与 briefing 先记录检索，再排队候选内容；runner 在最终请求组装和
裁剪后确认实际存在的片段，才记录 `model_request` 提供边界。自适应服务返回的
`documents` 只作引用；`adaptive.packet_fragments` 仅解析实际 `model_content`
中完整、哈希一致的 provider 围栏，不再读取源文档。保留 repo、source pins 和
上下文会话元数据；continuation 或变换片段的源区间未知，预算截断明确保留。
已授权的跨仓库自适应内容保持原执行行为，由单仓库追踪记录
`context_cross_repository_unobserved` 缺口，不扩大追踪权限。累计预算、反馈回执、
containment 与用户协议门继续由原知识上下文服务拥有。

API 模式可以观测模型请求；harness 模式记录提供给外部 CLI 的 prompt 边界，
外部 CLI 的原生读取和内部请求不能据此宣称已观测。skill/debug memory 保留
检索身份并说明其缺少 curated-document 来源。`required` 采集失败阻止后续受控
工作，`best_effort` 保持执行并标为 degraded；终结时写 JSON/Markdown 报告。
验证见 `test/test_knowledge_trace_strict.py`、
`test/test_knowledge_trace_adaptive_strict.py`。
