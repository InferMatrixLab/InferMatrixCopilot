# 知识服务 —— Copilot 全权负责知识库

> **一览**
> | | |
> |---|---|
> | **状态** | 🚧 分阶段落地。已合并：Direct 路由多仓库化与按请求知识视图（#203）、Knowledge Ops API 2.0 与 L1（#204）、generated-baseline 发版审计（#205）、服务核心（#206）、intake 与质量门（#207）；本 PR 加入合并流程、快照激活、发版巡检与调度器；本页随后续 PR 更新 |
> | **做什么** | 知识的创建、维护、退役、删除与发版巡检全部在 Copilot；每次变更经质量门，通过后由发布器在本地门禁复核合并结果再合并；ReviewBot 只读消费 |
> | **怎么开关** | 每个仓库在 `adapters/<repo>/manifest.yaml` 的 `knowledge_lifecycle`（人工审阅的高风险段）中设置 `enabled` 与 `mode: shadow\|auto_merge` |
> | **设计** | 经 GPT-6 sol 评审批准的设计文档（知识库管理重构设计，多仓库，v8） |
> | **硬边界** | bot 主机不持有任何 GitHub 写凭据；所有 GitHub 写动作由 GPU 盒上的发布器以 owner 账号执行，且只执行服务签名、未过期、代际有效的 outbox 项；私有上游仓库只能 shadow，不产生任何公开产物 |

## 组成

| 部分 | 位置 | 职责 |
|---|---|---|
| 仓库配置与注册表 | `kb_service/config.py` | 解析每个 adapter 的 `knowledge_lifecycle`；`general/` 由服务配置 |
| 账本 | `kb_service/ledger.py` | 单个 SQLite（`$KB_STATE_DIR/kb.db`），所有表按 `repo` 分区；单实例租约；代际号 |
| 签名 | `knowledge_service/signing.py` | Ed25519 信封，按用途（判定、outbox、控制记录、回执）隔离签名 |
| outbox | `kb_service/outbox.py` | 服务写签名项与控制记录；发布器执行前用 `check_item` 复核 |
| CLI | `infermatrix-copilot kb …` | `keygen`、`status`、`pause`、`resume`、`control` |

## 代际与暂停

任何暂停、熔断、回滚或切回 `shadow` 都会使该仓库（或全局 `*`）代际 +1，并立即重签
控制记录。发布器只执行代际与当前一致、未过期（`merge` 30 分钟，其余 24 小时）且控制记录签发
不超过 10 分钟的项；`close` 与 `open_revert_pr` 总是可执行。
只有 `auto_merge` 仓库会产生非停止类写动作：`shadow` 只记录“将要做什么”，不开 PR、
不发评论；模式变化本身也会使代际 +1。
发布器是知识 PR 唯一的合并方，所以暂停只需停止下发 `merge`，并在合并前复读控制记录：
不需要出队、转 draft、暂停标签或 GitHub 端的暂停清单。

## 运维

PR-history 抽取默认使用 `KB_INIT_HISTORY_PAYLOADS=omit`，不记录请求和回复。
需要主机侧耐久执行归档时，可显式配置 `KB_INIT_HISTORY_PAYLOADS=retain-private`：
原始请求和回复保留在运行记录中，不生成原始 PR 证据知识页。该选择由应用层处理，
写入原批次身份和 trace；中途切换会阻止重抽取，恢复时须使用原选择。
配置化 native 沙箱的耐久执行要求显式留存：`omit` 会在模型派发前返回 blocked，
不会自动开启记录或启动替代运行。沙箱 profile 本身不代表留存授权。

```bash
infermatrix-copilot kb keygen --out /etc/infermatrix-kb/service.pem   # 公钥交给发布器（KB_SERVICE_PUBKEY）
export KB_SIGNING_KEY=/etc/infermatrix-kb/service.pem KB_STATE_DIR=/var/lib/infermatrix-kb
infermatrix-copilot kb status
infermatrix-copilot kb pause --repo vllm-omni --reason "rollback drill"
infermatrix-copilot kb resume --repo vllm-omni
```

依赖：`pip install "infermatrix-copilot[kb]"`（cryptography）。

## Intake 与质量门

```bash
infermatrix-copilot kb run --playbook kb-intake --repo vllm-omni   # 一次 intake（shadow 下只记录）
infermatrix-copilot kb calibrate --repo vllm-omni                  # 评审模型校准；不达标不得切 auto_merge
```

模型：`KB_GENERATOR`（默认 `claude-code:claude-opus-5-5`）、`KB_JUDGE`（默认
`codex:gpt-6-sol:medium`）。生成器可显式配置一个备用模型：

```bash
export KB_GENERATOR_FALLBACK=zcode:GLM-5.3
```

主生成器缺少 CLI、未登录、调用失败、超时或回复无法解析时，同一次请求尝试备用生成器一次；
两者都不可用则事件继续排队。评审模型不切换，备用生成器也必须与评审来自不同家族。
每次尝试记录实际 provider/model，备用调用还记录 `fallback_from`；变更集注明实际生成器。
未设置此变量时只调用主模型。schema 错误仍由原有修复流程处理；带花费阈值的调用不切换，
因为预算只为主模型预留且 Zcode 无法执行阈值。
调用记录位于 `$KB_STATE_DIR/traces/records/`。

`KB_JUDGE` 可把评审切到其他后端（如 `zcode:GLM-5.3`，与 GPT-6 Sol 并列的受支持后端）。
zcode 评审的 `:effort` 后缀无效——推理档位由全局 `ZCODE_REASONING_LEVEL`（默认 `max`）决定；
zcode 无花费阈值能力，评审超时或不可解析按既有 fail-closed 语义记 `human`，绝不自动合并。
同一后端既起草又评审（GLM 生成 + GLM 评审）时，必须显式设置 `KB_JUDGE_FAMILY_WAIVER=1`：
每次启动记录警告，且该评审标签的校准记录必须重跑（`kb calibrate`），否则 auto_merge 保持
`calibration_required`。评审侧的可进化面在 `kb_service/judge_tuning.py`（提示词、rubric 措辞、
聚合策略、上下文预算）；`gate.py` 是受保护文件，只经人审 PR 变更。

运行经验（来源②）：`pr_debug` 的已验证修复记录经 `KB_BUGFIX_DIR`（同机，与运行侧 `KNOWLEDGE_INTAKE_DIR`
配为同一目录）与 `KB_BUGFIX_MAILBOX`（异机，`owner/repo#N`，只接受 `KB_BUGFIX_AUTHORS` 列出的作者）进入 intake。

## 调度、合并、激活与巡检

```bash
infermatrix-copilot kb serve                 # 常驻调度器（systemd），持有单写入租约
infermatrix-copilot kb activate              # 立即激活知识仓库 main 的快照
infermatrix-copilot kb rollback --to <sha>   # 回指到较早的快照（下一请求生效）
```

合并流程（v8）：`open_pr` 回执 → 为该 PR 的精确 head 签发判定，随 `merge` 项交给 GPU 盒发布器 → 发布器在
`main` 与 PR head 的合并树上运行本地门禁，通过才 `gh pr merge`（每仓库同时至多 1 个）→ 合并后激活新快照。
本地门禁拒绝的变更从不合并：上下文变化则在当前 main 上重建；服务自己的变更被质量门或本地门禁拒绝时，由生成器按
理由精炼后整体复检，至多 2 轮，仍失败则留在人工队列（不合并）。他人的知识 PR 每天检查一次：结果写在该 PR 上唯一一条
“知识质量门”评论里（原地更新），未通过则不合并，作者修改后次日复检。
有人绕过门禁直接合并/推送知识变更时，服务立即暂停该仓库、停止激活新快照、告警，并开出撤回 PR（不自动合并）；
合并精确的撤回 PR 并 `kb resume` 后恢复，被撤回的内容会作为新候选重新过门禁；也可以用 `kb accept-unknown <sha>` 让该提交本身完整通过门禁后保留它（通过才算处置，仍需 `kb resume`）。发版巡检在上游新 release 后运行
T1/T2/T3 与 purge，每个规则页一个变更集，全部经过质量门。

质量门已完成、但账本落盘失败时，可运行
`infermatrix-copilot kb --state-dir STATE_DIR reconcile-review CHANGESET_ID`，
根据原始 `gate-results/` 回执恢复该变更集。命令取得服务的单写租约，校验原始草稿、
意图和仓库 generation；重复运行不会重复写入 verdict，也不会调用模型或发布 PR。
服务仍持有租约时需要先正常停止服务。缺失结果、草稿变化、generation 变化、缺少恢复绑定的
旧巡检/维护记录，以及未保存 companion 编辑内容的旧回执会保留待处理状态，不能据此重跑付费调用。
新回执保存 companion 的完整编辑内容；恢复时，companion 记录和父变更集的等待状态在同一事务中落盘。
该命令仍不发布 PR，后续由服务原有的发布流程处理。

生成阶段若明确报告调用已发出但结果未知，intake 将源事件记录为 `draft_unknown`，
不会在下次轮询自动重跑。`kb status` 的 `unresolved_draft_events` 列出这些事件和错误。
硬中断可能发生在事件状态落盘之前；`unresolved_draft_checkpoints` 同时列出 intake、refine、
sweep 共用生成检查点中保留的调用意图，包含原始 cursor、输入摘要、base SHA 和 generation。
损坏的检查点标为 `invalid_checkpoint`，原始记录保留不动。查看状态不重跑生成，也不清除调用意图。
它们还没有完整质量门回执，因此不能用 `reconcile-review` 处置，也不能把状态改回 pending
来代替核对原始调用结果。

foundation 初始化恢复时，若原始 worker 的完整结果已保存，而对应调用意图仍保留，
服务先核验结果、输入绑定和 native 审批回执，再通过共用结算流程清除匹配意图。
这不会重新调用模型，也不会重写已接受内容。不同 page、不同输入或仍标记结果未知的回执
不能解除意图；缺少明确完成标记的旧记录同样保持待核对。

初始化、增加 owner 目录、修改顶层入口或校验器时，负责人合并的 PR 可能包含普通自动规则门禁不接受的路径。
`kb reconcile-reviewed` 是这种已合并历史的显式操作员接收流程：以服务密钥签名，先生成可检查的计划，再重新核验后写入不可变回执。
要求当前 main 的完整 SHA、当前 active 的连续 first-parent 历史、每个知识提交对应的唯一真实已合并 PR、
允许的负责人 merger 与认证操作员，以及精确 PR head 上指定的当前成功检查。保留实际 review 状态，不虚构 APPROVED。
在固定目标树上必须通过两个知识校验器与真实快照加载、格式、哈希和路由检查；缺失、失败或进行中的检查均拒绝。

```bash
infermatrix-copilot kb reconcile-reviewed --plan reconciliation.json \
  --target <full-main-sha> --allow-merger <owner-login> --require-check suite \
  --reason 'Owner-authorized recovery of merged initialization and administration'
# 检查计划中的提交、PR、操作员、检查和校验哈希后，再明确执行：
infermatrix-copilot kb reconcile-reviewed --apply reconciliation.json
infermatrix-copilot kb status
infermatrix-copilot kb resume --all
infermatrix-copilot kb activate
```

需提供 `KB_GITHUB_READ_TOKEN` 以核验认证操作员（沿用负责人的现有只读 API 凭据）。计划本身不产生信任；
apply 重新核验所有证据，main 或 active 改变、证据变化时要求新计划。两个 pin 不变时重复 apply 返回同一回执。
回执在 `$KB_STATE_DIR/reconciliations/`，应随恢复备份保留；审计、激活和控制记录每次直接校验其签名与完整提交范围，
不缓存此类信任。新知识提交仍会阻塞激活；删除或破坏回执不能续签旧信任。
接收不写 `disposed:`，不解除暂停，不更改 shadow/auto_merge，也不替代自动候选的完整质量门。
已有异步接受、撤回或待 intake 候选的提交必须先处理完，不能由此流程绕过。

评审服务读取知识：将 `KNOWLEDGE_ROOT` 指向 `$KB_STATE_DIR/active`。


## 本地门禁（取代 v7 的仓库端 `kb-gate`）

仓库端没有知识门禁的工作流、验证包、CODEOWNERS、合并队列或 ruleset：发布器在合并前于
`merge-tree(main, head)` 上运行 `knowledge_service.gate_verifier.verify_change`（签名判定、清单一致、L1、
逐块与一致性判定、上下文未变），通过才合并，合并后再检查落地的提交。只接受 `auto` 判定，
没有 `human-approved` 路径：改动受治理页面以外路径的知识 PR 不会被自动合并，作者会在发现评论里被要求拆分。
绕过发布器直接合并或推送的知识变更由每轮审计发现、暂停并撤回（见上文）。
规则对上游的声明（引用的 PR 已合并、写全的上游路径与 `path::Symbol` 存在）在质量门中对钉住的上游 SHA 证明并签入判定；
发布器合并前用自己的上游镜像与 `gh api` 逐条复核，不一致或上游不可读则不合并，连续 24 小时失败转人工。

切换前的负责人步骤：`main` 必须允许发布器直接合并（不开合并队列、不开自动合并）；
按设计 §P3 的顺序关闭 v7 遗留的自动合并并卸载 v7 发布器。

## 发布器（GPU 盒）

发布器以仓库负责人现有的 `gh` 登录执行服务签名的 outbox 项，写回签名回执。凭据不离开 GPU 盒。

```bash
infermatrix-copilot kb keygen --out ~/.infermatrix-copilot/kb-publisher.pem   # 发布器密钥；公钥给服务的 KB_PUBLISHER_PUBKEY
export KB_SERVICE_PUBKEY=/path/to/service.pub          # 服务公钥（kb keygen 输出的一行）
export KB_PUBLISHER_KEY=~/.infermatrix-copilot/kb-publisher.pem
export KB_PUBLISHER_GIT_AUTHOR='Name <email>'         # 知识 PR 提交的作者
export KB_PUBLISHER_STATE=/data/<owner>/kb-publisher  # 必须在负责人目录下（gh 包装器按目录选择登录）
infermatrix-copilot kb publish --remote bot-host:/path/to/kb-state --once   # 只记录（dry-run）
ALLOW_POST=1 ALLOW_PUSH=1 infermatrix-copilot kb publish --remote bot-host:/path/to/kb-state   # 常驻
```

每轮还会拉取 bot 主机上每周生成的留痕归档（`$KB_STATE_DIR/archive/`），哈希校验通过后保存到
`$KB_PUBLISHER_STATE/archive/`，作为离机副本。

没有 `ALLOW_POST=1` 时只把将要执行的动作写入 `$KB_PUBLISHER_STATE/traces/publisher.jsonl`；推送分支还需要
`ALLOW_PUSH=1`。控制记录超过 10 分钟未更新（服务可能宕机）时整轮不执行任何动作。

## 留痕、回放与数据集（trace/1）

知识服务的每次模型调用（含失败）、每个质量门判定与每个事后结果都写入 `$KB_STATE_DIR/traces/`
（JSONL + 内容寻址 blob + SQLite 索引，写入前脱敏）。RB 通过 `sdk.v1.TraceStore` 使用同一 schema。

```bash
infermatrix-copilot kb traces --changeset <id>                      # 查询记录
infermatrix-copilot kb replay --record <id> --model codex:gpt-6-mini:low   # 用便宜模型重放一次调用
infermatrix-copilot kb export --out judge.jsonl                     # 导出数据集（自动剔除校准集相关调用）
```

若进程在成功调用的 JSONL 记录落盘后、原生 archive 完成标记写入前退出，先停止该
trace store 的所有写入者，再使用原始 attempt 和 trace ID 完成归档：

```bash
infermatrix-copilot kb --state-dir /path/to/kb recover-attempt \
  --attempt <native-attempt-id> --record <trace-id> --offline-confirmed
```

该命令只从唯一匹配的原始成功记录恢复 archive，不调用模型，不新增调用记录，也不结算领域预算。
重复执行保留原完成时间；缺失、失败、重复或内容不一致的记录会被拒绝。SQLite 索引修复和
工作流 checkpoint／预算结算仍是独立步骤；此命令不会把未知结果变成成功结果。

若原生调用明确已完成，但其输出校验失败，可在同一命令添加 `--outcome failed`，
从原始失败 trace 结算 archive 为 `failed`。要求原记录明确声明已派发、已完成且非 pending，
并保留原错误、回复及用量；重复执行不改变完成时间。默认仍只恢复成功记录。
缺少这些事实的旧失败、传输未完成、超时、清理未决及冲突状态均拒绝，不能借此授权重试。

init 调用的 trace 位于 `$KB_STATE_DIR/init/traces/`，因此补全其 archive 时将
`recover-attempt` 的 `--state-dir` 指向 `$KB_STATE_DIR/init`。archive 完整后，停止
该状态目录下的 init 和 trace 写入者，可对原始预算预留执行离线结算：

```bash
infermatrix-copilot kb --state-dir /path/to/kb reconcile-init-cost \
  --repo demo --stage pr-history --record <trace-id> --offline-confirmed
```

结算要求唯一的成功原生 trace、完整且一致的 archive，以及匹配的预留 ID、金额和原始计费模式。
实际费用模式保留已知超支，未知费用保留原保守金额；固定 judge 计费继续使用原约定额度。
缺少原始计费模式的旧记录不会被推测补全。重复执行不重复计费，也不会调用模型。

对于明确已完成但输出校验失败的原始调用，先用 `recover-attempt --outcome failed` 完成
失败 archive，再对 `reconcile-init-cost` 添加 `--outcome failed`。两者共用终态事实检查；
对账仍使用原预留绑定及领域计费策略。实际费用保留超支，未报告费用保留保守预留，固定费用
保持原额度。此选项只结算费用，不批准失败回复、不完成质量 checkpoint，也不授权新调用。
未完成／pending、缺事实、损坏或冲突的记录在账本写入前拒绝；默认仍只接受成功调用。
discovery 新预算日志使用 `discovery-budget/2` 作为金额权威，init 记录只镜像总额；旧日志
首次迁移仍保留原有的保守最大值规则。此命令不恢复质量审查结果，也不清除其待确认 checkpoint。

PR-history 的 aggregate review 可单独恢复原生调用已完成、但结果 checkpoint 尚未落盘的情况：

```bash
infermatrix-copilot kb --state-dir /path/to/kb reconcile-init-review \
  --repo demo --stage pr-history --record <trace-id> --offline-confirmed
```

该操作复用正常审查的结果校验与序列化，要求原运行时指纹、reviewer、策略版本、输入绑定和尝试编号一致。
它先幂等结算原预留，再恢复绑定到原 head 的结果；`request_changes` 仍然是拒绝结果。
不会重新调用模型或发布 PR。`--stage knowledge-deepen` 可恢复原 facet review checkpoint，
复用 depth 的独立结果格式：Strict 要求完整有效的 facet packet，lightweight 保留独立有效的结果。
恢复只保存原判断，后续 resume 仍执行该阶段的内容应用规则；它不会直接接受或发布知识页面。
新 depth checkpoint 绑定原 reviewer、facet acceptance modes 和 validation kinds；缺少这些字段的旧调用不能猜测恢复。
其他阶段仍需注册自己的结果策略，不会套用 aggregate 或 depth 的格式。
缺少原包内容指纹的旧 trace 不能通过版本号猜测恢复策略。

`reconcile-init-review` 也支持持久化在 init record 中的 `init-block` 判断（例如 modules
和 advisory rule review）。该策略绑定原 block identity、reviewer 和输入摘要，并复用正常
block judge 的 dimensions 校验与 verdict 转换；`unsure` 仍然需要人工处理。恢复后 normal
resume 会复用该判断，不会重跑模型，也不会把 block 判断写成 aggregate PR review。
Foundation 并行任务自己持有的 checkpoint 不在这个 record 适配器范围内。

新的并行 foundation 任务会在原 dispatch checkpoint 中保存任务输入，并引用一份共享的
原始渲染上下文（head、日期、tags、pin、模型角色、预算设置、owner 和 coverage policy）。
所有 init 和 trace writer 停止后，可用原 `foundation_jobs.inflight` 中的 dispatch key 和 attempt：

```bash
infermatrix-copilot kb --state-dir /path/to/kb reconcile-foundation \
  --repo REPO --dispatch ORIGINAL_DISPATCH_SHA256 --attempt ORIGINAL_ATTEMPT --offline-confirmed
```

此命令只读取保留的本地 upstream Git mirror，不 fetch、不调用模型、不发布内容。它检查完整
native attempt 清单，用原始输入重放现有 worker，结算原 reservation，并在保存 task result 时
清除原 intent。之后 normal resume 负责应用结果。重复执行不会创建新 reservation；中断后的
未知结果仍保留，不能触发替代付费调用。旧任务缺少原始上下文、原 runtime 不匹配、归档不完整
或本地 pin 不可读取时拒绝恢复，不会从当前配置猜测补齐。

Feature discovery 的原始任务可通过同一个离线结算机制恢复：

```bash
infermatrix-copilot kb --state-dir /path/to/kb reconcile-discovery \
  --repo REPO --dispatch ORIGINAL_DISPATCH_SHA256 --attempt ORIGINAL_ATTEMPT --offline-confirmed
```

使用保留的 index snapshot、原 task/context、native archives 和 discovery budget journal，
重放 extraction、candidate review、boundary/consolidation audit 或 repair 的原验收规则。
先幂等结算原 reservation，再将结果与 intent 的清除保存到同一个 init record checkpoint。
无独立 reservation 的订阅生成仍不写入 judge USD 账本。其他任务的已完成结果保留；
后续任务和整个 audit 的完成标记由正常 resume 推进，不在恢复命令内派发。

此路径与 foundation 共用 account/result/intent 结算顺序。结算或落盘中断后可重跑相同命令，
不会重新调用模型、创建 reservation 或发布内容。它不 fetch 源码、不重建缺失的 index，
也不从当前配置补齐原上下文。原包内容指纹、输入、index pin/hash、native outcome 或账本
不匹配时保留 pending。包含 failed/malformed native calls 的完整 correction replay 仍是独立验收项。
