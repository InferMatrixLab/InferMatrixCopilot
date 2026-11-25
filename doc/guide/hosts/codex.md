# 在 Codex 里使用 InferMatrixCopilot

> **宿主端页面 —— 你要找的可能是另一篇。** 这里讲 Codex 作为**宿主**：copilot 跑在
> Codex **里面**（Direct 模式），由 Codex 自己的模型读代码。如果你要的是让 copilot
> 把 `codex exec` 作为子进程拉起来执行一个 Strict agent step，那是 Codex 作为
> **后端**，见 [`../backends.md`](../backends.md)。对照表见 [`README.md`](README.md)。
>
> 一句话判据：**宿主提供模型给你用；后端是 copilot 拿去用的模型。**

默认的 MCP 是一个**知识提供方**。Codex 用它当前选中的模型审代码，所以这条路径
**不需要 API Key，也不需要配置模型**。

## 安装

```text
# Windows
install.cmd

# macOS / Linux
./install-mcp.sh
```

引导程序会自动识别 Codex。它与 Claude、Cursor 及其他兼容宿主使用**同一份** MCP
描述符和 Agent Skill。

重启 Codex，然后粘贴：

```text
Use InferMatrixCopilot to review
https://github.com/vllm-project/vllm-omni/pull/5172.
```

插件还会装上 `imreview`、`imdesign`、`imcoding`、`imcifix`、`imupdate` 等 skill，所以最短的
评审写法是：

```text
$imreview https://github.com/vllm-project/vllm-omni/pull/5172
```

Codex 的 skill 用 `$name` 而不是 `/name`；没看到就先跑 `/skills`。修 issue 用：

```text
$imcifix https://github.com/vllm-project/vllm-omni/issues/5023
```

`imcifix` 是**宿主 agent 的本地补丁流程**。它不意味着 MCP 服务端暴露了什么
`issue_fix` 后台工具，也不会 commit、push、开 PR 或发评论 —— 除非你明确要求。

实现功能或已有设计用：

```text
$imcoding 给 scheduler 加抢占开关，沿用现有配置接口并验证默认行为
$imcoding 实现刚才确认的 imdesign 设计，完成集成和针对性验证
```

`imcoding` 由 Codex 在目标仓库中完成实际代码修改，读取相关知识和已有实现，
接好调用方与配置，运行相关检查并汇报结果。已有本地改动会保留；commit、push
和开 PR 按用户授权执行。

Codex 调用 `review`，拿到本地 `knowledge/AGENTS.md` 路径和一份精简的首轮评审
checklist，然后**自己**按那份文档的路由图走。MCP 不猜哪个 owner 适用，也不注入
完整规则页。

固定快照之后，Codex 会**立即**在宿主对话里报告 pinned head SHA、当前 CI、
mergeability 和初步发现。这一步发生在读知识、搜源码、跑测试**之前**，并且不等
CI 跑完、不等 mergeability 落定。随后 Codex 用收集到的 title、body 和 changed
files 调用一次 Direct，使用每条精确路由里内嵌的 `quick_map`（不打开完整规则页），
并让知识/源码与验证两条线并行推进。它复用同一份评审内证据包，并在 pytest 之前先
跑一次 import/版本兼容性预检。验证结果绑定到 head SHA 和环境指纹。

那条进度更新**不是** GitHub 上的中间评论。在唯一一条最终评审评论之前，Codex 会调用
`validate_direct_review`。常规小修改用 `subtraction_signal="none"`，不需要最小性
证明。只有当 diff 新增或扩张了 helper、类、fallback、兼容分支或公开行为时才用
`"triggered"`，并且必须提供减法证据。

工具返回**刻意做得很小**：

```json
{
  "mode": "direct",
  "knowledge_entry": "C:\\...\\knowledge\\repos\\vllm-omni\\components\\serving\\rules.md",
  "knowledge_routes": [
    {
      "owner": "serving",
      "path": "C:\\...\\knowledge\\repos\\vllm-omni\\components\\serving\\rules.md",
      "reason": "title/body: endpoint, request",
      "quick_map": "## Direct 代码快速入口\n...",
      "read_required": false
    }
  ],
  "navigation_policy": {
    "progress_before_knowledge": true,
    "use_embedded_quick_maps": true,
    "open_route_file_only_for_concrete_ambiguity": true,
    "max_routes": 3,
    "stop_after_routes": true
  },
  "execution_budget": {
    "profile": "code",
    "knowledge_file_reads": 0,
    "validation_commands": 4,
    "total_command_calls": 20,
    "hard_ceiling": true,
    "extension_command_calls": 4
  },
  "first_review_checklist": ["...", "Run subtraction only when the diff has a subtraction signal ..."],
  "progress_update": {
    "deadline_seconds": 60,
    "channel": "host_conversation",
    "required_fields": ["head_sha", "ci_status", "mergeability", "early_findings"],
    "early_findings_status": "preliminary",
    "continue_review": true,
    "github_comment": false
  },
  "completion_gate": {
    "tool": "validate_direct_review",
    "subtraction_signal": {
      "none": "no subtraction evidence required",
      "triggered": "require subtraction evidence"
    },
    "triggered_require_one_of": [
      "subtraction[{anchor, action, risk}]",
      "minimality_proof{scope_ledger, abstraction_census, why_no_safe_deletion}"
    ],
    "final_comment_count": 1,
    "if_missing": "partial_review"
  }
}
```

Strict 用的是**同一个**已安装的 MCP。wheel 里已经带了它需要的 playbook、adapter
和 skill。安装时配置本地 checkout：

```text
install.cmd --repo-path D:\path\to\vllm-omni
```

然后在 `~/.infermatrix-copilot/.env` 里填 `ANTHROPIC_API_KEY` 或 `OPENAI_API_KEY`
之一。两个 provider 各自还有可选的 `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL`，用于
代理或兼容网关。只填一个 key 时自动选择 provider；两个都填时用
`LLM_PROVIDER=anthropic` 或 `LLM_PROVIDER=openai` 指定。重启 Codex，然后说：

```text
Use InferMatrixCopilot in Strict mode to review
https://github.com/vllm-project/vllm-omni/pull/5172.
Do not return a review until the strict run is complete.
```

Strict 会用它配置的模型运行打包好的 `pr-review` playbook，带进度跟踪、报告生成和
发布门禁。轮询 `get_review_result` 直到 run 进入终态。

**Strict 永不发布。** MCP 侧一律不对外写：一个 PR 的评审标记和 head 门禁必须只有
一个发布者，所以 `mode="strict"` 配 `post=true` 会被直接拒绝（不是静默忽略）。
读 `get_review_result` 返回的结构化结果，自己发布；需要人工发布时走 CLI
（`--yes` 加 `ALLOW_POST=1`）。

安装后重启 Codex。用 `/mcp` 或 `codex mcp list` 确认 `infermatrix-copilot` 已连接，
用 `/skills` 确认 `imreview`、`imdesign`、`imcoding`、`imcifix`、`imupdate` 等可用。

## 默认 MCP 暴露了什么

- `review(target, repo?, mode="direct", post=false, title="", body="",
  changed_files=[], review_depth="", repo_path="", expected_head_sha="",
  idempotency_key="", include_score=false)`：Strict 可按要求在结论之后给出百分制参考分数。
  在宿主发出进度更新之后，
  Direct 用 title/body 返回**至多三条**精确的 owner/model 路由，各自带内嵌的精简
  `quick_map` 摘录。changed files 通常只做范围校验；但当返回的路由**没有一条**命中
  它们推导出的 owner 时，改由 changed files 选路，并在响应里明说
  （`status: scope_fallback`）。宿主不打开完整规则页 —— 除非有具体歧义卡住了源码
  评审：`quick_map_status` 不是 `ok` 的路由（`unavailable`，或章节超过摘录上限时的
  `truncated`）正是这种情况，预算会为每一条放行一次读取。返回的 docs/code
  `execution_budget` 是**硬顶**；唯一一次有界扩展，只留给一个明确陈述的、尚未解决的
  P1/高风险契约。Strict 分支启动打包好的工作流，接受 `review_depth`，并可通过
  `repo_path` 指定本次 run 绑定的本地 checkout（会校验它确实是该仓库的 clone、
  且位于允许的根目录下）。`expected_head_sha` 传完整 40 位 head，PR 在此期间发生
  移动就以 stale 停下、绝不改审别的快照；`idempotency_key` 传一个稳定的 attempt
  id，重试会拿回**同一个 run**（包括已完成的，直接读它的结果），而不是再评一遍。
  `post` 只对 Direct 有效，Strict 永不发布。
- `validate_direct_review(subtraction_signal, subtraction?, minimality_proof?,
  final_comment_count=1, evidence_head_sha)`：`evidence_head_sha` 必须是**本次固定的
  head 提交**——每一个被引用的源文件和验证结果都读自它；读自其他本地版本的证据
  不能完成评审。`none` 用于收尾常规小修改，不需要最小性证明；`triggered` 需要带锚点
  的减法动作，或"所审范围已经最小"的具体证据。
- `update_knowledge(repo?)`：`repo` 仅为调用兼容保留，返回
  `doc/knowledge/CONTRIBUTING.md`；由宿主模型照那份文档自己改 Markdown。
- `get_review_result(run_id, offset?)`：轮询并分页读取 Strict 报告。
- `get_review_status(run_id)`：返回 Strict run 的持久化进度。
- `doc_search(query, repo?)`：查找更深的模型/组件规则。
- `doc_read(path, repo?)`：读取选中的知识页。

Direct 模式**不跑第二个模型、不改知识、不发评论、不推代码**。它的确定性路由器从 PR
描述里选出有界的知识 owner；范围校验和被引用代码证据的真伪，仍然由 Codex 自己负责。
完成校验器检查的是**评审结构**。

## 已知行为：MCP 工具审批

Codex 会对 MCP 工具调用弹出**逐次批准**对话框（elicitation）。两个后果：

- **交互式（TUI）**：第一次调用 `review` 时会先看到批准框，批准后才执行。若你的
  Codex 版本在这个批准框上崩溃，请升级 Codex —— server 侧此时**尚未开始**任何工作
  （run 目录都不会创建），崩溃发生在 Codex 自己的审批 UI 里。
- **headless（`codex exec`，approval=never）**：需要审批的调用会被自动取消，
  报 `user cancelled MCP tool call` —— 这不是 server 错误，而是 Codex 把
  "从不询问"解释为"取消一切需要询问的调用"。

server 已为每个工具声明 MCP `ToolAnnotations`：除 `review`（预留 Strict run、
子进程触网）以外，全部 `readOnlyHint=true`，让按注解放行只读面的 Codex 版本可以
自动批准。另注意首次启动经由 `uvx` 拉包较慢——安装器已把该 server 的
`startup_timeout_sec` 设为 120。

## 可选：autonomous BYOK 工作流

autonomous 工作流有独立的配置和文档：
[`../autonomous-workflow.md`](../autonomous-workflow.md)。
