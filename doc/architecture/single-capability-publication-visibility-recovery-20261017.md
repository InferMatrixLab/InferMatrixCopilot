# 发布响应丢失后的原结果对账 · 2026-10-17

框架阶段保持收口。本轮修复一个可重复发布的真实缺口：GitHub已接受review，但进程在保存本地回执前退出；重启后一次marker空读取会触发第二次POST。marker读取报错还可能把原待对账attempt记为失败。新的回归测试在旧实现上四项失败，原日志保留。

## 唯一归属与迁移

发布副作用、intent和receipt继续归ReviewBot宿主；不移到Copilot执行内核。现有Ledger新增PublicationRepositoryMixin，使用同一metadata表和BEGIN IMMEDIATE事务，只有一个发布者能取得POST权限。intent不超时消失；pending、损坏或不可读的原状态不能解释为未发布。只有已识别的HTTP非接受响应允许重新claim；传输错误、408及5xx保留未知状态。

Direct／Strict通过现有ReviewPublisher使用同一机制。Bot注入现有job ledger；独立发布宿主可在稳定repository state_dir或artifact旁使用同一个Ledger实现，没有第二套发布／重试循环。没有耐久存储的调用在POST前返回deferred。

正向远端marker或实际写响应可结算原receipt；本地已知receipt可在后续远端读取暂不可见时复用。读取为空不能重发原未知POST。ReviewModeRouter.finalize现统一拒绝把deferred当终态，保留原attempt、provider身份和lease，防止下一次claim另建模型attempt。

本轮删除的是未知写结果被当成可重新POST／终态失败的旧分支，没有新增模型循环。GitHub最终head和knowledge guard仍位于实际副作用边界前；显式拒绝的错误继续保留原错误与业务重试政策。

## 当前代码和验证范围

证据目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/publication-uncertain-visibility-20261017`)包含修复前失败、过程中发现的deferred收尾问题、测试替身缺少新接口的原失败、完整patch及配对wheel。

完整双仓补丁、配对wheel和当前定向证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/publication-uncertain-visibility-20261017/publication-visibility-review.tar.gz`)绑定当前完整Bot套件及安装验证；这是本次恢复修复的交付包，尚不代表完整重构或发布验收。

生产改动仅Bot五个src文件，净+146行／+1100 AST节点；Copilot产品代码不变。归属／调用方清单中原“GitHub delivery and business receipts”条目补充了唯一持久化owner，能力不另起一套。

当前Bot wheel SHA256 `2634b317edafbe2829dba1493ec64bd72eee60948c8aecd741b6a9a16b1fd9f6`，与未变Copilot `1b36ed44ab9fc649ec364aa51c0747f25aeda4e9c07cf024366db721910309a9`配对。新Bot130个产品成员逐字节匹配wheel、正常安装及冻结源码。

必要定向验证通过：真实进程退出后的Direct／Strict发布对账、成功响应但未存本地receipt、marker暂空／报错、重复恢复、单模型调用和单review效果、并发claim、损坏receipt、不可重试的不确定HTTP结果，以及原knowledge publication guard。安装测试以-I从源码目录外运行，清除PYTHONPATH／PYTHONHOME；依赖复用，不称fresh安装；pip check通过。上游／模型／GitHub边界均使用受控测试适配器，无真实模型调用或真实GitHub写入。

首个冻结完整Bot套件中的两组旧替身没有新发布账本合同，原失败保留。E2E替身现委托真实Ledger，knowledge guard替身明确提供宿主state_dir，原单次模型／单次review及原身份断言保留并通过。随后另三项原provider恢复测试暴露共用测试替身的无参构造兼容遗漏，已保留该无参用法：仅涉及provider的替身不创建发布状态，发布用例明确提供真实账本目录；原find_review-only及禁止replacement断言不变。最终测试输入已另行冻结；当前原监督进程已成功结束：完整Bot套件2905项通过，无失败／错误／skip；当前安装验证215项通过。原returncode0／reason null与spec／日志哈希均匹配，324份冻结输入及当前工作树文件逐字节一致。最终树为5bf47d8798fef6120373ae1412f9002662d1f7ac；前两次原完整失败及全部定向修复前失败仍保留。各次监督进程均已终态，没有因观察超时重启。详见validation.json、terminal-confirmation.json和post-suite-proof.json。

## 未完成范围

这关闭的是新协议intent已经持久化后的重复发布／错误终态路径，不声称能替原版本生成缺失的发布证据。没有intent的原历史任务、真正的远端一致性、跨环境在途保留、完整崩溃／并发矩阵及真实上线仍按原计划验收；不自动改写原任务或重派。本次完整源码与安装验证已关闭；不据此关闭以下真实质量／深层恢复／发布门槛。

补充只读核查：head漂移后的原回执复现 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/publication-receipt-reconciliation-probe-20261017/observation.json`)确认远端原marker可读时，现有stale返回仍留下pending receipt。冻结的恢复验收场景 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/publication-receipt-reconciliation-probe-20261017/acceptance-cases.json`)定义后续宿主对账入口；不会通过重算review或重新发布解决。该缺口单独登记，不改写当前防重修复及原失败。

真实质量、独立标签、其他后端、原519分配及unknown、最终release／pins和rollout门槛继续保留。没有commit、push、模型准入或部署。[完整计划](single-capability-completion-plan.md)保持开放。
