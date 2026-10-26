# 真实PR质量样本准备 · 2026-10-17

框架阶段保持收口。本轮为后续真实质量验收准备四组真实历史PR的独立源码和宿主证据，没有修改生产代码、派发模型、创建预期标签或操作发布。完整十层质量矩阵仍开放；这些样本不是独立审阅通过的验收集。

## 已冻结的输入

| PR（来自GitHub实际读取） | 样本主题 | 变更文件 | 实际审查评论 |
| --- | --- | --- | --- |
| 6062 | Wan spatial reshard边界 | 2 | 2 |
| 5636 | 跨rank KV接收一致性 | 3 | 4 |
| 6031 | NumPy图片输出归一化 | 1 | 0 |
| 5981 | 模型归一化 | 1 | 0 |

输入目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/real-pr-quality-preparation-20261017`)保留最初本地squash源码提案和之后从GitHub实际冻结的原PR证据，两者不混用。可从以下证据逐项复核：

- 宿主证据 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/real-pr-quality-preparation-20261017/host-evidence/manifest.json`)：四组PR的metadata、issue comments、review comments、reviews、threads、check runs及commit statuses。32份原始响应hash已核对，分页和thread comments完整，读取前后head/base/updated_at及评论数量稳定。空讨论是真实读取结果，不把未读取当空；CI保留原结论，不把全部记录推断成通过。
- 原PR源码快照 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/real-pr-quality-preparation-20261017/actual-pr-sources/manifest.json`)：复用现有freeze_review_repository工具，逐组绑定GitHub实际base/head，保存独立bundle，fsck通过且不依赖object alternates。原仓库没有checkout、fetch或ref写入，网络fetch只发生在输出目录内的staging仓库。
- 宿主文件列表 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/real-pr-quality-preparation-20261017/host-files/manifest.json`)：实际读取全部7个changed files，均有patch；读取后再次确认metadata稳定。
- 源码／宿主一致性 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/real-pr-quality-preparation-20261017/source-host-parity/manifest.json`)：7个文件的Git blob、增删行和patch语义均匹配；6条真实review comment引用的original commit均存在于独立快照。没有运行这些上游测试，也没有将resolved线程自动标注为fixed。

## 输入差异与原失败保留

四组PR的原head均不同于本地squash commit，因此本地合并结果不能替代原PR评审快照。PR6062的merge-base还不同于其冻结的base SHA；其实际PR diff按merge-base→head计算，与GitHub files一致。source freezer原有review.diff继续保留其精确两revision差异合同，不把它改名成GitHub patch。后续评估应绑定source-host-parity中的github-equivalent.diff及宿主files原始响应。

首次完整patch字节比较因GitHub与本地Git使用不同的hunk函数名注释而失败。原失败和脚本保留在parity-attempt-1.json及audit_real_pr_source_host_parity-before.py；第一次诊断误选同后缀测试文件的记录另存，明确不作为失败依据。最终比较使用当前已安装Copilot的唯一review.diff parser，要求hunk坐标／计数、行内容／类型／行号及完整性一致，只排除函数名注释和末尾换行；同时独立校验完整源码blob hash。不是放宽源码内容或省略不匹配行。

## 下一步准入条件

1. 独立审阅这些真实源码／讨论，冻结预期结论、反例、容差及carried finding归属；标签必须在观察新模型输出之前完成，并位于模型不可见目录。当前没有创建标签，现有源作者合成标签不充当独立标签。
2. 为Direct／Strict及History选择完整业务证据和knowledge快照；若选carried finding，绑定实际原commit及当前源码，不仅依据线程resolved状态。其余七层仍按完整计划逐项准备，不用这四PR替代全部覆盖。
3. 冻结当前候选／原baseline、模型profile、schema、完整workflow命令和限制。保持Discovery／Module cards等独立模型家族要求；其他vendor按用户决定后验。
4. 如果开始新的付费比较，另建不可变准入和预算版本。原519分配、未用额度、失败及unknown全部保留，不自动复用或重派。当前没有模型准入。

所有model-quality／深层恢复／其他后端／发布门槛继续按[完整计划](single-capability-completion-plan.md)推进；不重开[框架阶段交付](single-capability-framework-closeout-20261017.md)。
