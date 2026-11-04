# InferMatrixCopilot —— 规范（逐文件）

一份**规范性的逐文件**说明。`doc/architecture/SPEC/` 这棵树镜像
`src/infermatrix_copilot/`：**一个源文件一页规范**，相对路径保持一致
（`engine/steps/pr/` → `doc/architecture/SPEC/engine/steps/pr.md`）。每页覆盖一组
固定的视角。两份跨切文档放在根部，被每一页逐文件规范**引用**（而不是重复）。

这份 SPEC 是为**驱动代码库重构**而写的：对你要改的任何源文件，它的规范会告诉你
这个文件必须继续保证什么、什么不属于它、哪些全局约束绑定着它、以及它目前哪里乱。

## 跨切文档（先读）

- **[_ARCHITECTURE.md](_ARCHITECTURE.md)** —— 分层、依赖方向规则、功能（7 种任务
  kind）、scope、数据与产物、安全模型、仓库不变性契约。每一页逐文件规范都默认你
  已经知道这张"大图"。
- **[_CONSTRAINTS.md](_CONSTRAINTS.md)** —— 全局编程约束（A 结构、B 契约、C 安全、
  D 知识、E 可观测）+ 不变量目录。逐文件规范用 id 引用它们（例如 `A2`、`C4`），
  而不是重述。
- **[_CONCISION.md](_CONCISION.md)** —— 把代码库变小的计划：一份有优先级、有 grep
  支撑的死代码、重复和样板清单（K1–K7），每条都给出要引入的共享 helper 和必须保留
  的不变量。"精简"重构就照它执行；某一页存在对应机会时，会带一个 **Concision** 备注。

## 逐文件规范模板（那些视角）

镜像树下的每个 `*.md` 都按这个顺序使用这些标题：

1. **头行** —— `LOC ~N · 角色 · refactor-status`。
   `refactor-status ∈ {ok, oversized, split-candidate, shim-to-retire, trivial}`。
2. **职责** —— 这个文件独占的那一件事。
3. **功能** —— 它实际做什么（行为），简短。
4. **公开契约** —— 其他代码可以使用的导出符号 + 它们的保证。
5. **不变量** —— 每条路径上都必须成立的性质（"不能破坏"清单），各自标注它实现的
   约束 id。
6. **边界 —— 不属于这里** —— 明确**不**属于这个文件的东西。
7. **依赖（允许）** —— 这个文件可以有的 import（强制 §_ARCHITECTURE 的依赖规则）。
   其余一律是分层违规。
8. **扩展点** —— 在这里增加能力的正规方式。
9. **测试** —— 钉住这些不变量的护栏测试。
10. **重构备注** —— 体量/内聚/耦合的坏味道，以及具体建议的搬移/拆分
    （**内聚**视角 —— 提升可读性，会增加文件数）。
11. **精简**（仅在存在机会时出现）—— 这个文件里哪些是死的、重复的或样板，
    以及 `_CONCISION.md` 的哪一条（K1–K7）能去掉它。这是"精简"重构消费的视角；
    与内聚拆分冲突时，**以精简为准**。

## 重构时怎么用它

- **改一个文件之前**：先读它的规范。如果你的改动增加了规范里没列的职责，那这个
  改动属于**另一个文件**（或一个新文件）—— 先改规范。
- **拆分一个文件时**：建出新的规范文件，把相关视角搬过去，并让"依赖（允许）"保持
  诚实 —— 一次制造出规则禁止的交叉 import 的拆分，不算完成。
- **删除一个 shim 时**（`refactor-status: shim-to-retire`）：先把 importer 迁移到
  真正的模块（规范里列了谁 import 它），然后把代码和它的规范一起删掉。
- **不变量就是契约**：重构可以自由搬动代码，但**必须保留**规范里点名的每一条不变量
  和每一个护栏测试。如果某个测试必须改，那就是不变量变了 —— 请把它显式说出来。

## 文件索引（镜像 `src/infermatrix_copilot/`）

| 分层 | 规范文件 |
|---|---|
| 接口 / 任务 | `task_spec` `intent` `cli` `chat` `ui` `config` |
| 引擎底座 | `engine/step` `engine/registry` `engine/executor` `engine/planner` `engine/agent_runtime` `agent_loop` `tools` `scopes` `llm` |
| Step 库 | `engine/steps/__init__` `engine/steps/_common` `engine/steps/{workspace,review,report,pr,issue,profile,rebase_v3,rebase_knowledge}` |
| 规划数据 | `playbooks/store` `playbooks/PLAYBOOKS`（yaml） |
| 边缘 —— 语言 | `profiles/languages`（按语言的规则，共享） |
| 边缘 | `adapters/base` `ci/normalize` `ci/providers` `ci/buildkite` |
| 安全原语 | `scopes` `push` |
| Profile | `profiles/store` `profiles/establish` `profiles/repo_map` `profiles/consolidate` |
| 跨切 | `review/{diff_summary,triggers,reviewer}` `memory/{debug_memory,skills}` `run_trace` `notify` `metrics` |
| 后端 | `providers/{registry,base,claude_code,codex,cursor,deepseek,zcode,audit,harness_llm}` `tool_bridge` |
| MCP 面 | `thin_mcp_server` `mcp_server` `mcp_policy` `run_status` `knowledge_docs` `__main__` |
| 共享服务 | `kb_service` `rfc_service` `knowledge_trace`（包规范覆盖各自实现模块） |

平凡的 `__init__.py` 再导出文件不单独出规范
（`engine/steps/__init__` 例外，因为它定义了 `register_builtin_steps`）。
