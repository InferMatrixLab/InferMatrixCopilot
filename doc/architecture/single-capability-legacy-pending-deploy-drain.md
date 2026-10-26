# 框架后恢复验收：旧 pending 阻止部署停止原环境

本切片补齐业务恢复与部署排空之间的连接，不重开已封存框架。修复前，两个实例的真实SQLite夹具均保留 `legacy-review-pending:42`，但零运行计数／无provider ID让部署gate返回0并实际执行模拟停止命令。原失败和两个停止效果记录保留。

现在 `deploy/server-deploy.sh` 的既有排空快照读取任一实例的持久pending标记；prepare／status／持锁gate都明确拒绝，停止命令不会执行。不解析原标记payload、不看bot行是否已终态、不删除标记、不轮询／重派模型。恢复原准入开关仍可执行。业务判断／标记创建继续归属ReviewBot `persistence/review_recovery.py`；部署宿主只消费这个持久协议，不加入vendor调用或另一套恢复循环。直接读取metadata协议使检查兼容尚无新SDK辅助函数的旧宿主。

## 必要验证及代码

代码与证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/legacy-pending-deploy-drain-20261017T072042Z/legacy-pending-drain-review.tar.gz`)含相对框架交付的双仓overlay、before／after、原始JUnit、命令、基线tree和重建证明；结果 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/legacy-pending-deploy-drain-20261017T072042Z/result.json`)／独立diff (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/legacy-pending-deploy-drain-20261017T072042Z/reviewbot-overlay.patch`)。

- 修复前两个负例失败；两个夹具的停止标记均实际存在。
- 修复后171项相关部署／排空／回滚／provider恢复及SDK边界测试全部通过，无skip。覆盖两个实例、原标记保留、无provider调用及恢复开关；既有无标记旧schema与远程worker路径继续通过。
- bash语法及git diff检查通过；测试使用本地受控宿主、真实SQLite和子进程。不是实际生产部署或真实模型验收。
- 只改1个生产shell文件：+7／−0行；Python生产文件AST变化0，shell内嵌Python AST净+22，另更新测试和README。两runtime wheel完全未改，无需为此构造新wheel；未来真实release bundle必须从含此脚本的不可变提交构建。

## 范围与后续

这只证明**已记录的旧pending不能被部署停止流程忽略**，不证明原unknown已结算或所有旧生产行均有标记。原生产无协议／无身份记录仍需在原环境取得原始结果／派发事实再对账；不能删除标记或用新paid run替代。真实服务、混合旧watcher、跨宿主全部故障及最终release验收仍开放。

[框架交付](single-capability-framework-closeout-current-20261017.md)及其564d／a57代码包保持原范围。该框架包应用一次后，先应用本包reviewbot-overlay.patch；Copilot文档overlay只更新后续证据入口。原质量失败、预算分配和unknown记录均未改；无commit、push、PR或生产修改／部署，完整目标保持开放。
