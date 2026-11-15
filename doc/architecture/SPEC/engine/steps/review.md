# engine/steps/review/ —— 规范

<!-- verified-against: 2026-11-14 -->

`patch gate + PR review + quality（9 个源文件） · step 库（评审） · refactor-status: ok`

## 职责
条件式 patch 门 + PR 评审 agent step + 有界的 PR review-readiness 质量 step，
以及它们仓库中立的 prompt 体系。
Pre-push patch 门必须审查显式 base 到当前 HEAD 的已提交变更；空范围、脏的
tracked checkout、不可解析的 ref 或超过 reviewer 容量的 diff 都阻塞推送。
通过后记录批准的 base/head，后续推送必须验证当前 HEAD 仍匹配。
它曾是一个 341 行的模块；现在将提交范围的 mutation gate、只读 PR 评审
agent、评审结果的有界精炼、评测调优过的 prompt 数据和确定性 helper 分开。

## 包内布局（一个文件一个关注点）
- `__init__.py` —— import `patch_gate`、`quality` 和 `steps` 以触发各自的
  `@step` 注册副作用；再导出公开契约（见下）。无逻辑。
- `prompts.py` —— 由评测得出的 prompt 数据：`_REVIEW_SYSTEM`、`_REVIEW_LENSES`、
  `_REVIEW_MERGE`。prompt 文本**移出控制流之外**。
- `utils.py` —— 确定性、无 LLM 的 helper：`_sweep_targets`、`_render_review_md`、
  `_SEVERITY_ORDER`。
- `patch_gate.py` —— `review.patch_gate`（validation/read）：捕获已提交的
  base/head 范围、计算触发条件、审查精确 diff 并记录批准的 head；不依赖
  PR review agent 的 prompt/render 控制流。
- `steps.py` —— `agent.review_diff`（agent/read）：冻结 evidence pack、
  选择评审深度、调用评审 passes 并组装最终结果；不批准或推送 mutation。
- `refinement.py` —— 对 draft 结果进行覆盖补充、定向第二轮、逐评论核验；
  接受 handler 提供的 frozen diff、工具和 trace，不注册 step 或发布评论。
- `anchor.py` —— 基于代码片段的评论锚定（2026-08 新增）。
- `repo_tools.py` —— 只读的变更考古工具组（2026-08 新增）。
- `quality.py` —— `agent.assess_pr_quality`：一次 tool-less、只读模型调用，
  把机械规则当可错提示，并输出 `ready|concerns|needs_rework` 的结构化结果。

## 公开契约（可从 `engine.steps.review` import）
`_REVIEW_LENSES`、`_render_review_md`、`_sweep_targets` —— 由包的 `__init__` 再导出，
使拆分前的 import 路径保持不变。

## 不变量
- patch 门：廉价摘要**常开**，只有触发时才跑 LLM 评审；**fail-closed**（**C6**）；
  高风险模块来自 adapter，settings 只作兜底（**A5**）。
- **取舍是字段，不是句子**：每条候选评论带 `disposition`
  （`publish` | `excluded` | `duplicate` | `resolved` | `no_issue`）。
  `finalize_review_dispositions` 在**评论预算之前**执行——被撤下的候选
  因此占不掉八个可发布名额——也在**任何渲染器之前**执行，于是 summary、
  正文、行内评论三者都由同一个终局集合渲染。缺字段=发布（该字段是增量的）；
  无法识别的取值也发布，但记进 `finding_dispositions.declared`，
  让异常可见而不是静默丢掉真实发现。`no_issue` 走 summary 的
  「Checked, no defect found」注记：保留证据，去掉诉求，不带优先级。
  在此之前取舍只活在模型的 `summary` 散文里，而发布读的是列表，于是
  summary 说要丢弃的请求照样行内发出（#141）。
- **终局摘要不能复用过期取舍**：覆盖补充、第二轮、核验、disposition 或评论预算
  改变评论的成员、主张、位置或严重度后，清除 reducer 的 draft summary，
  由最终可发布集合生成摘要；无发现时正文明确写 `No actionable findings.`。
- **事实成立不等于需要改动**：覆盖补充、第二轮和逐评论核验都携带 ensemble
  reducer 的拒绝候选及原因（`_rejected_review_comments`）；重新提出同一关切
  必须有推翻原原因的新证据。核验须指出当前 head 的具体触发条件及坏结果，
  或明确未满足的要求。无影响的未使用可选值、依赖未来假设代码变更的问题
  属于 `not_actionable`，与 `refuted` 一样丢弃；无法核验仍保留并降级，
  调用失败仍保留原评论。
- **`[resolved]` 是记录，不是诉求**：findings 里的 `[resolved]` 行从不变成评论。
  残留问题若需要贡献者处理，由评审者自己另发一条 `publish` 评论（file:line、
  要做的改动、`evidence` 引用该 `[resolved]` 行）；覆盖补充（coverage editor）
  的输入也剔除 `[resolved]` 行。此前按关键词从散文里“提拔”残留，会把明确
  写着不再追究的行发成 P2（#155）。
- **未测公开函数（#164）**：evidence 带 `untested_public_api`——`ut_coverage.analyze`
  在 PR-time worktree 上找出无测试引用的新增公开函数，由 lens 判断间接覆盖/trivial/
  真缺口；评审者把缺测试评论标为 `kind: untested_api`；finalize 之后 `cap_gap_comments`
  只对这类评论至多保留 3 条，其余名字追加到 summary，其他发现不受影响。
- **`review_verdict` 是发布的 state 字段，不是散文**：由
  `_review_verdict(review_comments, pr_state)` 计算（**与渲染器同一个
  helper，绝不第二份校准规则**），随 `review_text`/`review_summary`/
  `review_comments` 一起进 `state_updates`（B2）——
  `contract.build_review_result` 读的 `verdict` 就是它；此前裁决只活在
  Markdown 的 `**Verdict:**` 散文里，机器消费方只能刮取。
- **planner 失职是显式 `capability_gap`**：`plan.planner_error` 非空且不匹配
  `_EXPECTED_PLANNER_CAUSES`（`unavailable` / `rejected_depth:`）时记
  trace 缺口 —— 配置了的 planner 答非所问（transport / 空回复 / 不可解析）
  与"没配"和"守卫在履职"是三件事（见 `review/planner.md` 的 5 种 cause）。
- 评审：领域 checklist 由 adapter 的 `knowledge.review_checklist` 或 profile 的
  `review.md` 扩展，**注入上限 8k 字符**（页面写满上限时新增门禁会被静默截断）；`_sweep_targets` 以
  `repo.language` 为键，**诚实降级**；裁决遵循下述校准规则；
  确定性的按严重度排序的评论上限。
- prompt 是仓库中立的（**A5**）。
- 质量 step **fail-closed**：枚举/JSON/模型不可用即 BLOCKED；
  `needs_rework` 少于两条完整依据会机械降为低置信度 `concerns`。短描述、标题、
  体量、文件数或没有测试修改任何一项都不得单独成为 needs-rework 理由；其中体量
  可以单独成为 `concerns` 依据（非测试新增超 1000 行的预算，排除项与已声明的
  拆分计划/豁免随 prompt 一起下发，因为该 step 读不到知识树）。
- **要引用，不要行号**（`anchor.py`）。模型给错行号的频率高到发布时必须降级该发现；
  修法是**换一个问题** —— 让模型引用它在说的代码，位置由程序自己算。校验器**仍然最后
  跑**，所以"绝不发布错锚点"的保证不变；变的是**只有行号错**的发现能保住自己的 inline
  位置，而不是被降级进正文。
- **裁决校准**：只有**已验证**的 blocker/major 才阻塞；minor 降级为 COMMENT，
  自述不确定的发现**永不阻塞**。
- **带着评论的升级会被抢救成一次成功的 REQUEST CHANGES** —— **找到缺陷本来就是一次成功
  的评审**。
- **考古工具是召回机器，不是便利设施**（`repo_tools.py`）。wave-2 取证测出：基线能做而
  我们的 pass 做不到的那些判官决定性动作，**全都是只读工具集当时恰好缺少的一条命令**
  （`git diff --stat base..HEAD` 证明被要求删除的东西压根没删；读 merge-base 版本的
  文件；`git show` / `git log -S`）。
- **覆盖率驱动的第二轮**由该 run 自己的覆盖空洞播种；**验证账本**会把这次 run 其实已经
  写下来的残余提升为发现 —— 取证发现，**漏掉的东西往往已经被赞许地记下来了，只是没被
  提出来**。
- **重复发现会合并到最丰富的那条陈述，而不是被丢弃**（`_dedupe_comments` + `_richness`，
  v20）。丢掉后出现的那条重复，会丢掉其中证据更好的那一份 —— 所以去重必须是**合并**，
  因为两份副本很少同样有据。
- 主体打分会把路径标识符坍缩成 basename（`_topic`），于是"用完整路径"和"用裸文件名"
  陈述的同一个发现会被识别为一条。
- **只有当一条评论确实同时具备主张和论证时，才发出 claim headline**
  （`_split_claim`，v21 及其修复）。只有一句话的评论会渲染成朴素的
  `file:line [sev] — text` 形状，而不是一个残缺标题；且 headline **永不截断**：
  第一版截到 160 字符，结果 49 条里有 34 条停在悬空的连接词上，
  **读起来比它所替代的那段还差**。那次提交里记下了一条长期教训 ——
  主张/论证的切分**无法从并非按此写就的散文里确定性地还原**，
  要可靠地拿到它，需要 reducer 直接产出一个 title 字段，
  **那是模型层的改动，不是渲染层的**。

## 边界 —— 不属于这里
不含 agent 运行时治理（那是 `agent_runtime`）；不写 adapter/profile。

## 依赖（允许）
`review/*`、`engine/step`、`.._common`、`..agent_runtime`、`profiles/languages`。

## 测试
`test_review_step.py`、`test_agent_ensemble.py`、
`test_profile_steps.py::test_review_guidance_from_profile`。

## 精简 —— **K2**（共享语言规则，已应用）
`_sweep_targets` 消费 `profiles/languages.py::sweep_re` —— 那是"按语言规则"三份旧副本
之一（另两份在 `profiles/establish`、`repo_map`），现已收敛到那个唯一来源。
未知语言时的诚实降级（只做文件级 sweep）**被保留**。

## Carried finding rechecks

Direct 1.1 / Strict 1.3 accept typed `CarriedFinding` inputs (unique IDs, source head, severity, location, title and evidence). Direct binds the carried set into its issued context and validates `FindingRecheck` answers. Strict passes the set through the policy allowlist, performs a read-only recheck against the frozen PR head, and persists answers for the public structured result. Every carried ID needs an explicit outcome: `fixed`, `still_affected`, or `unverified`. Missing evidence is never a fix.

## 自进化接入（2026-10-04）

影子评审继承严格工具 scope；PR 文件、基础版本与历史工具只能读取冻结仓库，涉及生产写入的步骤由执行器拒绝。


## Peer-claim verification contract

Each enabled per-comment verifier receives `sibling_draft_comments`: the other
candidates' available file, line, severity, comment and disposition fields,
excluding itself, evidence blocks and routing metadata. They are unpublished
hypotheses from this review, never PR-author statements or independent evidence.
The verifier is instructed to resolve disagreements against PR-time source,
repair its own false premise, and discard correction-only/duplicate requests
while preserving the supported underlying defect once. CI-absence claims must
check directory/glob collection, markers and uploader gates; selection is
separate from whether a lane ran or passed on the head.

This supplies context to the existing independent verification calls; it adds
input tokens but no calls or deterministic semantic deduplication. Sibling
verifiers do not share their returned verdicts, so the guidance is not a hard
guarantee that duplicate or contradictory findings will be eliminated.
`test_review_peer_evidence.py` checks evidence projection, input nonmutation,
existing explicit repair/removal behavior and disabled-verifier behavior with a
scripted runner. Oversized peer and own-comment evidence must retain stable
full-content references across the verifier fan-out; the runtime archives by
name and content hash. Tests exercise the real evidence builder after all
competing writes, plus concurrent same/different-content and cap controls.
These tests do not measure model quality.
