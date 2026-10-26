# improve/ —— 规范（元改进引擎）

<!-- verified-against: 2026-10-09 -->

`设计：/data/zhoutaichang/copilot/meta-improvement-engine-design.md v1（GPT-6 sol 批准 2026-09-29） · refactor-status: building (P0–P4 已落地)`

## 职责
面向任意 trace/1 工作流的"取证—候选—可信目标实验—自动采用—上线观察／自动回滚"循环。使用入口和数据契约见 [自进化使用指南](../../guide/self-evolution.md)。引擎的写权限限于追加自己的 trace/1 记录、
写自己的账本和源码制品目录、在 OS 隔离环境跑候选；自主模式仅采用已评估制品，并在无凭据沙箱执行；生产写权限仍归宿主。PR 是可选审计，旧金标／人审模式保留。

新增 `artifacts.py`（不可变源码及补丁）、`isolation.py/worker.py`（无凭据沙箱与可信预算代理）、`drivers.py`（PR/知识/元基准驱动及父进程评分）、`evolution.py`（周度候选状态机）、`coordinator.py`（统一续跑周期）、`annotations.py`（历史 trace 导出与人工标签导入）、`evolution_publish.py`（独立 `evolve-outbox/1` 与部署跟踪）。`IMPROVE_EVOLVE_ENABLED` 默认关闭，`IMPROVE_ENABLED` 保留总停止作用。

## 自主目标与制品运行时

`objectives.py` 是受保护的父进程目标控制器（`objective/1`）：自动导入可信 replay 输入、生成有因果见证的故障／负例、固定一个 API 模型、预注册三次配对及 item 聚类功效、核验实际源码与输入、独立计算分数及护栏。见证不传生成器或候选；候选数值分数无效。PR 与知识指标仅证明结构契约、产物保留与资源收益，`semantic_quality_claim=false`。引擎候选每次只修改归因或 lint；lint 既有正例及负例受保护。功效不足会存档并等待新鲜样本，不重放完成的付费调用。

Replay envelope 保留完整文件与引用，由 TraceStore 脱敏、压缩和按内容寻址；未压缩 UTF-8 JSON 上限为 64,000,000 bytes。超限只记录 `replay_capture_deferred` 的大小、上限和摘要并抛出专用 `ReplayCaptureLimit`，不保存截断输入，也不进入 replay 数据集。知识起草无活动进化工作流时继续原生固定生成器；活动工作流（含已禁用项）缺少完整 replay 时保持事件 pending。其他捕获错误不能走此延期路径。

`runtime.py` 重新应用补丁核验实际评估树，原子采用本地不可变 release，原生 PR 评审、知识草稿、lint 和归因在活动制品的沙箱中运行。宿主记录运行指纹才更新部署基线。首次八个不同 item 与上一版比较，七日缺证据自动恢复；保留后每周最多八个新 item 持续比较。契约、凭据／隔离／预算、指纹失败或资源显著退化自动恢复上一版；恢复制品不可验证则禁用执行。执行／切换锁及持久化激活、回滚意图支持重启恢复。

默认 `IMPROVE_EVALUATION_MODE=objective`、`IMPROVE_PROMOTION_MODE=automatic`，不要求人工标签、判官或 performance 模型。总开关仍关闭，固定模型取 `IMPROVE_EVOLVE_MODEL` 或 eco；每周一个候选、一次修复及共享 20 美元预算不变。`gold/pr` 模式保留原有双家族取证及人工流程。无隔离生产驱动的机械工作流仍可 lint，但不自动采用。当前主机命名空间关闭，代码候选实际执行延期。

## 模块
| 文件 | 阶段 | 作用 |
|---|---|---|
| `enroll.py` | P0 | 工作流声明（`workflows/*.yaml` + `IMPROVE_WORKFLOWS_DIRS`）：kind/unit/item_key/fingerprint.covers/capture/shadow_tools；未声明 = Tier 1 |
| `fingerprint.py` | P0 | 声明式配置指纹（playbook sha、settings、prompt 文件哈希、routing、`resolved_models`、tools、knowledge_snapshot、copilot_sha）；覆盖项缺失 → 无指纹、记 `fingerprint_missing` |
| `shadow.py` | P0 | 影子四层边界：`harden_scope`（严格 extra、读围栏）、`shadow_env`/`make_executables_dir`/`assert_boundaries`（凭据与可执行白名单）、`make_shadow_clone`（`--shared` 独立克隆、无 remote、钉住的 base ref）、`smoke` |
| `staging.py` | P0 | 隔离前的条目快照：`stage_item` 复用 `pr.fetch_diff` 的钉住取数，写 `item_staged` 记录；任一必需输入不可得即拒绝 |
| `reader.py` | P1 | trace/1 → 工作单元（按 `context.unit_id`，RB/知识服务记录回退到 `run_id:step`）；窗口按 JSONL 日期文件读取 |
| `lints.py` | P1 | Tier 1 的 17 条确定性 lint（L01–L17，每条带版本与出处）；`Baseline` 稳健 z（MAD 下限 = 中位数的 10%）；L13 隔离 |
| `ledger.py` | P1 | 每工作流 JSON 账本：周期统计、基线样本、提案状态机（open → experiment-registered → supported/neutral/refuted/underpowered → landed/closed；30 天无人触碰 → stale）、hold、通道活性；每次变更同时写 `decision` |
| `cycle.py` | P1 | 周期：preflight（`improve_enabled` 是 kill switch）→ lints + 基线 → 账本 + Tier 1 提案（lint 恶化 ≥1.5× 且 ≥3 单元）→ stale 清扫 → 报告（`reports/cycle-*.md/json`）+ `decision(type=cycle)`；`is_due`/`maybe_run_weekly` 是周度槽位 |
| `adapters/__init__.py` | P2 | 结果适配器契约：金标矩阵（`match()` → recall 与阶段归因）与 finding 级有效性（`findings()` → precision）两个数据模型不混用；评审级判官分数（`review_scores()`）原样保留；`scores_from` 给每个数字标来源 |
| `gold.py` | P2 | 策展金标集：`gold_id = sha256(item+path+规范化 concern)[:12]`，`draft` 由原始评论生成、只有 `curated` 才被使用，改措辞即新条目，文件 sha 即版本 |
| `judges.py` | P2 | `run_judge`：API 判官走 `LLM.create`，CLI 判官走 `governed_subprocess`（预留判官次数、无工具、空工作区、流中出现工具调用即作废），都写 `model_call` |
| `stats.py` | P2 | 配对、按 item 聚类的 t 区间（移植 `paired_analysis.py`）、所需 item 数、五种标签、Cohen κ |
| `adapters/review_eval.py` | P2 | eval 评审适配器：导入 `judge_val` 配对 verdict 为 `judge_verdict` outcome；`gold_match` 判官（3 票多数、hit 须逐字引用）写 `gold_match` outcome；`match()` 只读记录 |
| `adapters/rb_review.py` | P2 | RB 生产适配器：PR 线程代理标签（accepted/disputed/silent）→ finding 级有效性，`descriptive_only`，无金标、不注册实验 |
| `adapters/kb_intake.py` | P2+ | 知识起草（`kb-intake.draft`）适配器：结果 = 生产质量门本身写的 `gate_block`/`gate_summary` outcome；finding 级有效性 = L2 判定（pass=valid、fail=invalid、unsure=unlabeled），评审级分数 = `net_pass`/`gate_score`/`precision`/`yield_pass`/`fail`/`human`/`empty`；金标 = 该 item 上在位生成器通过门禁的规则（`gold_from_incumbent` → `write_gold`，按内容版本化），`gold_match` 判官 3 票多数、hit 须逐字引用，空稿不调判官直接 miss |
| `workflows/kb-intake.yaml` | P2+ | 起草步骤的声明：static/step_call，item `{repo}#{pr}`，指纹覆盖 `kb_service/intake.py`、`KB_GENERATOR`/`KB_DRAFT_STRATEGY`、`zcode_reasoning_level`、copilot_sha；`kb_service.runtime` 为每次起草盖上 workflow/unit_id/item/fingerprint |
| `forensics.py` | P2 | 覆盖矩阵（确定性）、S0–S10 阶段分类法、取证 agent（只读 trace 工具、全部输出围栏为不可信数据、必须引用记录 id）、双家族交叉复核（不一致 = disputed）、整改清单、测量健康 |
| `meta.py` | P2 | 冻结元基准：`eval/dataset/meta/cases`（trace + 人工阶段标签）与 `meta/lints`（注入缺陷样本）的导出与加载 |
| `budget.py` | P3 | 周包络（美元 + 判官次数，按 ISO 周持久化、跨进程加锁）：每次模型调用发出前按最坏情况预留（输入 = 可见请求字节数与精确模型上下文上限的较大值，按最高输入／缓存单价计；输出 = `max_tokens`；无价格即拒绝）、返回后按实际用量结算；`结算 > 预留` 记 `budget_breach` 并中止；`governed()`/`current_governor()` 供 `LLM.create` 与 `run_judge` 使用 |
| `experiments.py` | P3 | 预注册（Tier 2 且非 descriptive-only、API 后端、指纹 diff 非空且不触及元基准、每个 item 有金标、按历史 sd 算 `n_required`、成本预留）→ 影子运行（stage → 每 item×replicate×臂 一个子进程、`PR_SNAPSHOT_FILE` 交接、指纹核对、L13/预算/崩溃隔离）→ 配对判定（`n_retained` 重算功效、五种标签、`experiment_verdict` 记录、提案状态推进） |
| `publish.py` | P4 | 提案发布：引用只以记录 id + blob 哈希（`excerpt_for` 逐字摘录 ≤20 行、脱敏、`verify_excerpt` 可由哈希复原）；`lint_proposal` 拒绝任何不可解析引用（Tier 1 ≥3 条记录，Tier 2 S1–S9 + 损失量）；固定 issue 模板（主张/阶段/损失/证据/建议预注册/账本/marker）；`ProposalOutbox` 文件协议 `improve-outbox/1`（`actions/` 引擎写、`acks/` 与 `inbox/` maintainer routine 写）；`plan`/`publish`（open/update/close，hold 暂停发布、待 ack 不重发）；`sync`（ack → issue 与已发布状态；inbox → 人类触碰、`maintainer: hold`、合并 PR → landed、GitHub 关闭 → closed；Tier 1 landed 在后一周期 lint 率低于开单时才 close） |
| `adapters/meta_bench.py` | P4 | 引擎自身的结果适配器（§11.2）：金标 = 元基准 case 的人工阶段标签；outcome = `meta_eval` 记录；hit = 标签一致、disputed = 双家族不一致、S0 = unlabeled；`review_scores` = κ、lint 召回；`human_labelled`（无判官） |
| `workflows/workflow-improve.yaml` | P4 | 引擎自登记：`workflow-improve.improve.forensics`，item `{improve_item}`（周期为 `cycle`、自实验为 `meta:<case>`），指纹覆盖取证/lint/统计/适配器源码与模型 |
| `cli.py` | P0–P4 | `improve migrate-index|rebuild-index|rollback-index|compare-index|verify-index|cycle|ledger|lints|gold draft/check|meta lint-check/bench|budget|experiment register/run/list|publish [--dry-run]|sync` |

## 接入点
- 执行器：`settings.trace_store_root` 非空时绑定 store 并为每步绑定单元上下文；`settings.improve_shadow` 下拒绝非 read/report 步骤。
- `tools.dispatch` / `LLM.create` / `HarnessLLM.create` / `run_harness_step` / MCP bridge：全保真采集。
- `kb serve` 调度器：每 tick 调 `_improve_cycle()`，周度槽位到达即跑一次周期（与知识服务同一租约、异常隔离）。
- 任务种类 `workflow_improve`（L2，READ_ONLY_KINDS）；playbook `workflow-improve`（mode → preflight → sync → lint → experiments → forensics → ledger → publish → report）；`improve.publish` 是唯一 `risk=push` 步骤，与 `pr.post_review` 同样过"TaskSpec `post` 且 ALLOW_POST=1"双门，否则 dry-run 打印将写的 outbox 动作。
- `LLM.create`：绑定了 governor 时先预留再发请求，失败释放、成功结算；`improve.experiments` 与 `improve.forensics` 都在 `governed()` 内运行。
- `improve.forensics` 步骤：对每个有结果适配器的工作流建覆盖矩阵、用两个模型家族归因每个 miss、产出整改清单并按阶段各开一条 Tier 2 提案（`cycle.open_tier2_proposals`，带建议预注册；descriptive-only 工作流的提案标 proxy、无建议）；单元数低于 `tier2_min_items` 或无 LLM 时跳过并说明。`--task-param meta_case=<case>` 时（`improve.mode` 发布 `improve_meta`）只跑取证步骤：对一个冻结 case 归因并写 `meta_eval` outcome——这就是自实验的影子单元（`experiments.meta_run_unit` / `_meta_stage` 只把 case 与 lint 样本复制进影子目录，不带任何叙述材料）。
- `improve.sync`（read）读回 acks/inbox；`improve.publish`（**`risk="push"`**，唯一外向写）：无 `post` 意图只预览、有意图但 `ALLOW_POST=0` 为 dry-run、二者俱备才写 `improve_outbox_dir` 下的动作文件（`improve_proposal_repo` 必填）；影子运行被执行器在步骤前拒绝。
- 自实验：`experiments.run` 对 `human_labelled` 适配器不要求判官；`_find_unit` 优先取声明工作流的单元。

## 不变量
- 既有提案的外向写是 `improve.publish`，代码候选另由 `improve.evolve_publish` 经 post/push 双门写独立 outbox；引擎不持有 GitHub 令牌，issue 的开/改/关由 maintainer routine 执行并以 ack 回报。
- 未通过 `lint_proposal` 的提案永不发布（记 `proposal_lint_failed`）；hold 只暂停发布，lint 与实验照常。
- 元基准只读：任何触及 `eval/dataset/meta` 的指纹 diff 或 `IMPROVE_*` 覆盖在注册时被拒。
- 实验子进程的周预算、账本目录、`IMPROVE_INPUT_CONTEXT_LIMITS` 和 `IMPROVE_NATIVE_CALL_LIMITS` 由父 governor 固定，不接受候选或运行臂自己的配置覆盖。修复估算后操作员可归档并清除已核对的预留故障，实际结算金额不回退。
- 原生调用不再仅因 harness 类型跳过美元准入。completion／session 使用共享一次性 prepare 边界；领域在 provider 预检后、模型前按事实选择原次数政策、已绑定订阅政策或受支持的美元预留。美元 completion 预留停止阈值与一请求超额余量，原 journal 结算；未发出退款，未知费用保留预留，超支先记录再中止。原生 session 没有该 completion 停止参数时必须明确订阅准入，否则拒绝美元执行。宿主必须自行验证声明的最大请求费用；固定 API fixture 不是厂商账单或真实账户证明。
- 引擎自身的记录（`playbook=workflow-improve`）是下一周期的 Tier 1 单元（自登记）。
- lint 只读 trace，不调模型；无证据形态的 lint 不猜。

## 测试

原生美元预留与原 attempt 的模型、输入、上下文、环境及 trace root 在同一 journal 写入中绑定，原 settlement 保留该绑定。受保护的 native completion 先建立 durable archive，不能通过省略 payload 获得可恢复的美元派发。
停止 writers 后可运行 `infermatrix-copilot improve budget --ledger-dir DIR --reconcile-native RESERVATION_ID --offline-confirmed`，只读取原 attempt／receipt 并结算原预留；命令没有模型调用或替代派发路径。未确认停写、缺失／重复记录、绑定漂移、缺失 blob、尚未完成的 archive 或 pending worker 结果均拒绝。旧未绑定预留不能猜配或重置。必要时先用原 native-attempt 恢复服务完成 archive，再结算账户；两者保留各自 authority。
schema 拒绝后的已发生用量仍是账务事实。已知费用按原 journal 结算；未知费用保留原预留额度，真实超支先写入 breach 再拒绝。重复恢复使用同一 settlement，不能重复扣费。USD 路径的这些证明不代表判官次数账户、所有 backend／host 或完整崩溃矩阵已验收。

`test_improve_p0.py`、`test_improve_p0b.py`、`test_improve_p1.py`、`test_improve_p2.py`、`test_improve_p3.py`、`test_improve_p4.py`、`test_improve_gateway_budget.py`、`test_improve_kb_intake_adapter.py`、`test_evolution.py`、`test_autonomous_evolution.py`。

判官次数账户沿用 eager reserve 和旧 `judge_tokens` 字符串／匿名计数格式。
模型前 prepare 将原 token 绑定到 canonical attempt；绑定前崩溃保留原未绑定预留，不猜配。
停止 writers 后可运行 `infermatrix-copilot improve budget --ledger-dir DIR --reconcile-judge TOKEN --offline-confirmed`。
该命令与美元恢复共用 schema、archive／blob／identity、pause／lock 校验，只结算原次数，不调用模型或修复索引。
已知 terminal failure 仍计一次；pending worker 或未完成收据保留原预留。并发恢复遵循现有 nonblocking lock，拒绝者只能重试原恢复身份。
`judge-calls/v1` 的兼容别名不是美元账单；原生 `cost_usd` 继续作为独立事实记录。
这些确定性和真进程崩溃检查不代表完整厂商账户、所有 host／workflow 或真实模型质量验收。
