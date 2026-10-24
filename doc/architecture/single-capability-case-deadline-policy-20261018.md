# 后续质量阶段：唯一逐案例 deadline 策略 · 2026-10-18

框架保持[已交付](single-capability-framework-final-handoff-20261018.md)，当前产品仍为 a10／24ac。本轮修复评测应用组合的真实缺口，生产行数／AST 变化 0、模型调用 0，不改变原 ledger／manifest／unknown／环境。

## 问题与实施

[真实 PR4870 控制](single-capability-pr4870-session-controls-20261018.md)显示 SSH 配置／bridge 准备耗时 137 秒，而旧草案 native budget 为 120 秒。仅调 host wait 无法给 SDK session 的同一 absolute deadline 增加模型执行空间；全局增大 host 又会使 Foundation 的 32-call workflow 超过上限。

现在已有 shared_execution.plan_for_case 选择可选 case_deadlines，与 profile／knowledge 共用同一入口。每个计划 case 必须完整声明 native_timeout_s、host_timeout_s、workflow_timeout_s，缺项／多项／bool／非有限值／未选中非法 case 均在 identity control 或 reserve 前拒绝。

共享 native_deadline／host_deadline／workflow_deadline 是唯一约束实现。删除 arm_runtime 的独立 native／host／workflow 校验主体和 codex_guard 的重复 native cap；旧函数只选择 case 后委托。SDK settings、guard／sandbox transport、legacy CLI settings、legacy RPC process／event socket 以及 coordinator phase admission 全部接到同一策略。ledger 仍绑定原完整 plan，不以选中副本开新账户或重试。

没有新表时，原 native cap 180 秒和旧默认行为保留。有显式新表时 native cap 600 秒；host 覆盖 native，workflow 覆盖 call_limit × host ＋30、且不超过 7200 秒。新配置不能回填旧运行：原 full plan digest／request identity 仍拒绝改写恢复。

## 新草案的明确预算

| 案例 | native | host | workflow |
| --- | ---: | ---: | ---: |
| Direct | 300s | 360s | 1000s |
| Strict | 300s | 360s | 5000s |
| 六个 active init case，各自独立 | 120s | 180s | 5790s |

全局旧 120／180／5790 不变。Foundation 最小 workflow 为 5790，Strict 为 4710，Direct 为 750；各自 phase／outer stage 均覆盖选中 wait 与 host 准备。既有 Direct／Strict 真实 PR4870、完整知识、私有标签隔离和精确 worktree destination 保留；Discovery／Module-card 仍为明确 deferred，不绕过领域独立 family 政策。

新完整 manifest (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-deadline-paired-draft-20261018/manifest.json`)是新冻结草案，旧 manifest 原样保留。静态结果 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-deadline-paired-draft-20261018/static-results.json`)覆盖逐案例策略、16 份 phase／38 个 phase、History 显式留存及 4830 文件绑定。完整准入仍按 retained allowance 拒绝，未创建 live ledger。

## 验证与可审阅代码

- 相关核心回归 217 项、入口／durable worker／installed driver 等补验 111 项均 pass，0 fail／error／skip。是受控 provider／process 边界验证，不宣称真实模型结果。
- 新测试覆盖 case native300 到 SDK／gateway、legacy host360、workflow wait、原 ledger digest、未修改原全局 plan；非法或 dormant case 在控制和 reserve 前拒绝；Direct／Strict phase 短于选中 host＋workflow 时拒绝 stage 创建；旧计划仍拒绝 native300。
- 首次 216 项中，新测试发现 legacy_settings 仅认全局 host_timeout_s 而忽略纯 case 表；原失败／JUnit 保留。修复判断后完成上述回归，没有修改测试预期来掩盖该缺口。
- 增量 patch (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-case-deadline-policy-20261018/case-deadline-evaluation.patch`)相对前一 case-knowledge slice；before／after 及哈希均保留，git apply --check 与独立临时树的逐字节重建通过。原产品 wheels 无需重建。
- 行数／AST (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-case-deadline-policy-20261018/code-delta.json`)：评测机制净＋35 行／＋260 AST，生产增量 0。另有相关测试和协议，不用净减行作为验收门槛。

## 剩余验收

此次修复使 review 可以声明独立有界预算，并证明配置传递正确；不证明模型一定能在 300 秒完成。原 180 秒真实 preparation 控制仍保留其精确范围，没有换标签为新 300 秒完整模型验收。

519 的 retained allowances／spare 未改，新增 182 次仍仅提案；实际账号／proxy 生命周期、正式新配对预算准入和模型质量比较未完成。原 PR1 Strict reserved、Discovery unknown、History 外层 ack 及其环境不结算、不重发。深层恢复、其他后端与最终发布目标继续保留；未 commit／push／GitHub 写入／部署。
