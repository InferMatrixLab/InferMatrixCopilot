# engine/steps/pr/ —— 规范

<!-- verified-against: 2026-11-25 -->

`LOC ~1988（6 个文件） · step 库（PR） · refactor-status: ok`

## 职责
受守卫的推送、只读的 PR 抓取/门禁、PR rebase、PR debug、受门禁的评审发布。
它曾是一个 484 行的模块；现在是一个按关注点拆开的包。

## 包内布局（一个文件一个关注点）
- `__init__.py` —— 对各子模块做副作用 import（`@step`/`register_step` 注册）；
  再导出 `extract_signature`。无逻辑。
- `fetch.py` —— 只读抓取：`pr.fetch_diff`、`pr.gate_check`。
- `rebase.py` —— `pr.checkout_branch`、`pr.rebase_onto_base`、`pr.analyze_diff`、
  `agent.verify_module`。
- `debug.py` —— `pr.fetch_ci_failures`（+ `_enrich_ci_logs`）、`pr.group_failures`、
  `agent.debug_group`、`pr.harvest_debug_knowledge`。
- `publish.py` —— 对外写入（risk=push）：`ci.push`、`pr.post_review`。
- `utils.py` —— 纯函数 `extract_signature`（及其正则）。

## Steps（12 个）
`ci.push`（script/push）；`pr.fetch_diff`、`pr.gate_check`、
`pr.analyze_diff`、`pr.fetch_ci_failures`、`pr.group_failures`（deterministic/read）；
`pr.checkout_branch`（deterministic/write_workspace）；
`pr.harvest_debug_knowledge`（deterministic/knowledge）；
`pr.rebase_onto_base`、`agent.debug_group`（agent/write_workspace）；
`agent.verify_module`（validation/read）；`pr.post_review`（script/push）。

`pr-debug` 的 report-only 路径不 checkout PR 分支。诊断组数受 playbook 参数和
settings 安全上限共同约束；执行组共享 checkout 时必须顺序修改。`agent.debug_group`
只有在观察到新 commit、干净的 tracked checkout、根因和验证证据时才算成功。
`pr-rebase` 的 report-only 路径在按 run 分键的私有 detached worktree 内本地
rebase，不修改配置 checkout 的分支或文件，不推送。该树是一次 run 的可变 scratch，
不是 `pr.fetch_diff` 使用的、按 head 分键的不可变评审快照。
`agent.verify_module` 的咨询结论是 `verified | problem | inconclusive`，未知文本
不能标成已验证。`ci.push` 对 PR debug/rebase 还需同一 HEAD 的 patch-gate 批准。
知识 intake 的 canonical 记录使用 schema v2、完整仓库名和稳定 event ID；缺少
完整身份时保留 v1 兼容记录，由消费者显式解析别名。

## 公开契约（可从 `engine.steps.pr` import）
`extract_signature`（由 `utils` 再导出；供测试使用）。

## 不变量
- `ci.push` 对 `pr_review` / `pr_quality` 无条件拒绝推送；其他任务仍委托给 `guard_push`（**C4**）。
- **`pr.fetch_diff`：一个 head 统治一切**（PR2 重构后）。head 由
  `_resolve_pr_head` **恰好解析一次**，stale 门、fetch、diff、worktree 全部
  从这一个答案推导（`_fetch_at_one_head`）。解析请求明确包含 `headRefOid`，
  不能用可能截断为前 100 条的 `commits` 列表末项推断 head；权威字段缺失时
  仍按既有未解析 head 路径处理，钉住的请求拒绝继续。fetch 走 run 域强制目的 ref
  `refs/imx/<run_id>/{base,head}`（取代旧的机会主义 tracking ref +
  `FETCH_HEAD` —— 对着可能陈旧的 `origin/<base>` 取 merge-base 会把无关的
  上游漂移当成 PR 的工作），且 `_fetch_pinned` 把取到的 head 与 API 报告的
  head 复核、不符即**响亮失败**（"head 在 view 与 fetch 之间动了"）。
  `_pinned_diff` 是**主**diff 路径；`gh pr diff` 只是回退（已合并 PR /
  未钉请求的钉取失败）。worktree 的身份/验证/存活模型整体委托
  `engine/worktrees.py`（见其页）；经 `state_updates` 发布
  `repo_path`/`checkout_note`/`pr_head_sha`，于是**每个 lens 调查的都是 PR
  那一刻的代码**。未钉请求失败时仍降级回 live checkout 并带响亮注记 +
  `capability_gap` trace。
- **`expected_head_sha` 是快照绑定的硬门**：spec 携带它时，解析出的 head
  不符 → 在任何 fetch/物化**之前**就以 BLOCKED 停下（`_stale()`，trace
  事件 `expected_head_mismatch`，`contract.build_review_result` 据此上报
  stale）；且每条"降级回 live checkout"的路径都变成硬 BLOCK —— 钉了快照
  还静默评错树，比停下更糟。
- **`pr.post_review` 始终使用 `event="COMMENT"`**，模型 verdict 不授予批准或阻塞 PR 的权限。
- **`pr.post_review` 只发一条 GitHub review + inline thread**，且先把每条发现的位置
  对照已抓取的 diff 校验过 —— **绝不是一串独立评论**。
  写入前同样通过 `headRefOid` 验证当前 head，不能使用截断 commit 列表；
  缺少权威 head 时不提交 review。
- `diff_text` 和 `gate_report` 都可以经 state 注入，因此网络之下的每条路径都可离线测试；
  `gh` 不可用时降级为 BLOCKED，**绝不崩溃**。
- `pr.checkout_branch` 把推导出的 `PushPolicy` **序列化后发布**（**B2**）——
  在推送处 resume 时**绝不能**看到那个 deny-all 的默认值。
- `pr.checkout_branch` 的 report-only 模式只在私有树里 checkout，并经
  `state_updates` 发布私有 `repo_path` 和配置 checkout 路径；resume 时所有后续
  rebase/analyze/verify step 核对私有树身份、仓库归属及 detached HEAD，树若已被 reaper 清理就
  BLOCKED，绝不回退到配置 checkout。
- `pr.rebase_onto_base`：由受治理 agent 解冲突，或 abort+升级（**工作区始终被还原**）。
  判断 rebase 是否完成时通过 `git rev-parse --git-path` 定位每棵 worktree
  自己的元数据；不能假设 `.git` 是目录。抓取 base 或生成 diff 失败时 BLOCKED。
- `pr.fetch_ci_failures` 经 profile 选定的 CI provider 富化日志，否则记一条
  `capability_gap`（**E2**）；`pr.group_failures` 按**归一化后**的签名分组。
- `pr.post_review` 是**双闸**的（**C5**）。
- 启用知识撤回协议后，`pr.post_review` 在实际 GitHub 写入处持有消费端发布锁，
  复查本次固定知识回执的可用性。缺失、失效或命中撤回的回执会阻止发布并要求重评；
  策略安装与发布共用该锁，确认回执不能越过尚未结束的旧代际写入。
- `pr.harvest_debug_knowledge` **只在真实推送后落盘**（push 输出存在且非 dry-run，
  且至少一组修复同时有 root_cause 与 verification —— 与 debug memory 同一门槛），
  投递到两个**彼此独立**的 sink：`settings.knowledge_intake_dir`（同主机落盘）与
  `settings.knowledge_intake_issue`（跨主机 mailbox），各自配置了才尝试，任一失败
  不压制另一个；两者都空 = 关闭（默认）。mailbox 投递（`_publish_intake_record`）把同一条记录以
  `<!-- infermatrix-copilot:bugfix-record:v1 -->` + 一个 fenced JSON 块通过 `gh api`
  贴为该 issue 的评论；它是对外写入，按不变量 5 双闸：配置了 mailbox **且**
  `ALLOW_POST=1`，否则只 trace `knowledge_intake_publish_skipped` 作 dry-run；贴失败
  trace `knowledge_intake_publish_failed` 并写 outputs `intake_publish_error`，成功写
  `intake_comment`。它是**刻意 fail-open**
  的：两处都未配、dry-run、无已验证修复、写失败（trace
  `knowledge_intake_write_failed` 后吞掉）都返回 ok —— 关闭学习回路
  **绝不能**让已落地的修复失败。它消费 executor 维护的 `state["outputs"]`，
  该表在 resume 时由 checkpoint 恢复（见 `test_knowledge_harvest.py` 的
  crash-then-resume 用例）。设计记录：`doc/RFC-knowledge-intake.md`。

## 边界 —— 不属于这里
不含推送授权逻辑（那是 `push`）；不含 CI 日志抓取机制（那是 `ci/providers`）；
不含 agent 治理（那是 `agent_runtime`）。

## 依赖（允许）
`scopes`、`push`、`ci/*`、`adapters/base`（analyze）、`engine/step`、
`engine/worktrees`、`.._common`、
`..agent_runtime`。

## 测试
`test_pr_steps.py`（含截断 commit 列表下的权威 head 解析、钉 ref run 域隔离、head 移动检测、stale expected_head
BLOCK、worktree 分键/拒外来树、report-only rebase 的隔离与失效树拦截）、
`test_push_and_steps.py`、
`test_knowledge_harvest.py`（harvest step + executor crash-then-resume）、
`test_ci_and_repo_map.py`（注意：`test_ci_and_repo_map` monkeypatch 的是
`pr.debug._gh`，即 `pr.fetch_ci_failures` 绑定 `gh` 的那个子模块）；
端到端：`test_thin_mcp_server.py`（评审跑在钉住的 worktree 上、head 移动
在评审前停下）。

## 重构备注
拆分**已完成**。各子模块只共享 `._common` 的 helper，所以拆分**没有制造交叉 import**。
读/写轴是显式的：`fetch` 只读，`publish` 装着两个 risk=push 的 step。
K3/K4/K7 的精简（require_repo/published/from_state）已在各子模块落地。
`_enrich_ci_logs` 保持为通往 `ci/providers` 的**薄接缝**。

## 自进化接入（2026-10-04）

影子模式下 `pr.fetch_diff`、gate 和 CI 取数从可信控制器准备的 `pr_snapshot` 读取；快照缺少必要字段即阻塞，不回退到实时网络。基线和候选共用冻结的 base/head 与上下文。

Strict review fetches the PR description, commit subjects and linked issue
acceptance criteria before source investigation. Its initial GitHub query omits
PR discussion and review threads. Full PR quality assessments may still include
discussion. The checkpoint records whether the fetched context is independent of
feedback, so reviews resumed from older combined bundles can exclude discussion.
