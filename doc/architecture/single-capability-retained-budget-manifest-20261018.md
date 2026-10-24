# 配对manifest汇总与保留额度校验修复 · 2026-10-18

框架保持交付，当前产品仍为a10／24ac；本轮只修改评测账本、相关测试和协议，生产代码行数／AST增量0。模型调用0、旧台账／519／spare未改。

## 实际修复

此前ledger.plan_call_limit仅检查显式revision允许的ceiling，没有把retained_prior_call_allowances加入总量。冻结的旧函数对“保留519＋新配对182、ceiling519”仍返回519；本轮用纯函数重现，没有打开DB。

现在同一个plan_call_limit在声明保留额度时检查非负整数、显式正数case limits，并要求retained＋2×sum(per-arm limits)不超过ceiling。协调器和直接Ledger入口都调用它，在创建stage或DB之前拒绝无效计划。没有第二份预算计算或新的重试循环。没有新增budget revision、释放旧allowance、修改ledger identity或迁移原记录。

100项相关ledger／coordinator测试通过，0 fail／error／skip；包括旧合法revision的恢复／并发／case限额和新拒绝路径。测试证明声明额度的算术约束，不宣称重新审计了所有历史实际用量或未知费用。

## 已汇总的具体草案

已生成单个paired manifest：八个Codex兼容case、双臂16 stage、38 phase，十层quality descriptor完整保留；Discovery／Module-card明确deferred，旧private labels保持hash。冻结source／knowledge／实际安装SDK schema路径、canonical helper、角色、精确worktree目的地及History显式留存。Foundation实际prose输入替代旧无效edit输入；没有把未消费旧文件冒充当前capsule。

现有quality descriptor、phase loader、retention validator及756份文件绑定检查通过。实际schema路径来自两侧安装环境；计划中的Strict worktree路径由两侧SDK的dest_for计算，没有授权整个用户HOME或共享worktree父目录。

完整准入按预期拒绝：519保留＋182提案需要保守总量701，而当前recognized ceiling仍519。**701只是所需总量计算，未配置／批准／分配为新上限。** 没有创建live ledger、stage目录或模型运行。

具体manifest与拒绝结果 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-paired-manifest-draft-20261018/manifest.json`)；它的execution_kind=live只是coordinator格式字段，不构成准入。profile目前仍使用无credential／proxy的控制配置，真实账号／代理／服务端条件未批准；不能删除retained字段或复用旧spare来执行它。

## 验证范围与后续

评测修复增量patch、测试、草案及原始结果 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-retained-budget-guard-20261018/a10-retained-budget-manifest-review.tar.gz`)。from-a10-evaluation.patch相对上次冻结tree1092d774…，只改eval ledger／两个测试／协议；当前完整双仓生产代码与wheels保持原交付。

该归档引用原绝对input路径和hash，复制归档本身不授权改路径重放。新主manifest仍需实际profile／proxy／credential context、可见输入及旧unknown原身份的最终核验；新增保守预算必须通过明确新revision与正式准入，不能用本次拒绝作为允许执行的证据。完整模型质量、其他vendor、深层恢复及发布目标继续保留，当前完整确定性配对结果不被夸大为它们完成。

没有GitHub写入、commit／push或部署；完整目标未完成。
