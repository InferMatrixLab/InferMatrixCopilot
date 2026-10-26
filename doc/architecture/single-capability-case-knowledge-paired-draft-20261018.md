# 后续质量阶段：逐案例知识与完整配对草案 · 2026-10-18

框架保持[已交付](single-capability-framework-final-handoff-20261018.md)，当前产品 wheels 仍为 a10／24ac。本轮修复评测应用组合并将独立 PR4870 接入新的完整配对草案；生产代码／AST 增量 0，模型调用 0，原预算、paid unknown 和旧环境不变。

## 实际缺口与唯一实现

原八层草案只有全局 knowledge_by_arm，指向 16 文件的合成 review fixture。真实 PR 不能以这份替代完整知识；若直接全局切换，又会改变其他 init fixture 的输入。

现在现有 shared_execution.plan_for_case 同时选择 profile 与可选 native_knowledge_by_case。逐案例表必须覆盖全部计划 case，且每项显式含 baseline／candidate 的绝对路径；即使未选中的 case 非法，也在 identity controls／reserve 前拒绝。省略新字段保留原全局语义，原 manifest／账本身份不迁移。新表中的 null 也拒绝，避免 SDK 默认值隐式带入未冻结知识；省略字段的旧行为不改。

SDK session composition 和 legacy native host 使用同一个 selector；ledger 仍绑定原完整 plan，不以 selected copy 另建账户。quality_plan 也调用该 selector，将所有逐案例知识目录纳入私有标签／retained evidence 的 containment 检查。没有第二份 provider 调用、校验解析或重试循环。

评测修复 patch (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-pr4870-paired-draft-20261018/case-knowledge-evaluation.patch`)相对前一份冻结草案的四个 Python 文件及其原协议；包内同时保留 before／after 内容与 hash。机制净 +16 行／+221 AST 节点，另有相应行为测试，生产增量 0。代码仍在 eval，不改变产品公开 API 或 wheel。

## 新草案的实际输入

新完整 manifest (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-pr4870-paired-draft-20261018/manifest.json`)显式使用独立 PR4870；原 PR1 未决调用不视为已恢复／结算，旧草案未改。新 request／host／diff 与[上一轮独立案例](single-capability-review-case-transition-20261018.md)完全一致。它是已有训练 replay，不是 blind case；synthetic discussion、unavailable CI 与禁止发布声明保留。

- Direct／Strict 的两侧 host KNOWLEDGE_ROOT 都使用完整冻结 snapshot 根；native knowledge 使用其 knowledge/ 子目录。共 1702 文件，tree SHA256 d338c32ba22af684429186f31ab822a3912aa9d6afefad32160ce599c2abe509。
- 其他六个 active init case 保留其原 fixture 知识。Discovery／Module-card 仍为明确 deferred，未绕过独立 family 政策。
- 两侧 Strict worktree destination 都由各自安装 SDK 的 dest_for 计算，只授权精确目的地；没有保留 PR1 worktree 或放开 HOME／worktrees 父目录。
- 可见 source 的 2375 个 tracked 文件、request／host、完整 review 知识、实际安装 schema 及 helpers 都绑定到当前草案。private labels 位于 worker roots 外；不挂载整个输出根。
- 新草案首次静态检查发现 12 个新 phase 的 hash 只更新了 bindings，未同步 quality inputs。失败及原 draft 原样保留，随后只修正这 12 个新 phase 输入引用；没有改标签、旧 manifest 或产品来绕过拒绝。

## 已执行验证及范围

评测应用组合、共享执行、quality plan、coordinator 和 mixed-provider fixture 最终版本共 148 项通过，0 fail／error／skip。其中新增测试覆盖逐案例知识到 SDK settings 的传递、原 plan 不变、无 reserve，非法／缺项／未选中非法路径在 controls 前拒绝，以及私有标签经逐案例知识挂载时在 stage 创建前拒绝。

新草案的 quality descriptor、16 份 phase／38 个 phase、显式 History retention 和全部 4830 文件绑定通过各自 canonical validator。完整 admission 按预期因 retained allowance 拒绝；这不是可派发结论。没有 live calls.sqlite 或 worker 创建。

当前 a10 与原 baseline 安装 SDK 分别在源码目录外以 -I 执行公共 DirectClient.plan／read_document：同一完整 snapshot 验证通过，7 份规划文档的内容 hash／字节数及 routes 一致。过程中禁止 provider subprocess 和网络。这个探针只证明 Direct 规划与读取，不代表 Strict 完整执行、真实模型质量或服务端认证。

## 剩余准入与后续目标

旧保守额度仍 519，新增 182 仅未分配提案；保守总量计算为 701，不是新批准上限。实际 profile／proxy 生命周期、服务端认证、真实完整 source 准备 deadline、完整整例裁定与容差仍需验证。不能因静态 validator 或文档读取通过而派发。

原 PR1 Strict reserved、Discovery unknown 与 History 外层 ack 保留各自原身份与环境。本轮没有实现或宣称跨 campaign 全局 unknown 扫描；只用显式独立案例移除这份新草案对原 PR1 的依赖。真实质量、深层恢复、非 Codex 后端和最终发布目标继续开放，不重新阻挡框架交付。

## 最终补验与归档

null 拒绝规则后的最终 scoped suites 为 82＋66 项，均 0 fail／error／skip；初版 81＋66 的报告保留历史范围，不累计冒称更多行为覆盖。评测 patch 在独立临时目录经 git apply --check、实际应用后，五份 after 文件逐字节重建一致。最终草案的相同 canonical 静态验证重新通过，预算拒绝保持。

最终评测增量与配对草案包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-pr4870-paired-draft-20261018/case-knowledge-final-review.tar.gz`)含最终 patch、before／after、JUnit、静态结果、两侧公共 SDK 读取证据及完整 manifest。它按原绝对路径／hash引用 source 和知识，不是独立可重放准入包。initial-artifact 记录和最初 archive 保持原字节；最终 archive 另命名，没有把旧校验冒充最终校验。未 commit、push、GitHub 写入或部署。
