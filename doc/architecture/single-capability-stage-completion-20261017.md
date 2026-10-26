# 外层完成凭证与 coverage 恢复：后续 P1

框架交付保持收口。本切片让新评估运行能在控制器丢失后凭原完成证据恢复，也修复全套检查发现的quick-map策略持久化回归。没有重跑原519模型调用或改写其账本。

## 机制与归属

评估宿主拥有stage ledger及完成证据；Copilot继续拥有进程监督、immutable写入和跨进程锁。新`quality-stage/2`必须冻结`stage_completion.py`，复用原phase执行循环，在所有原phase正常结束后写`quality-completion.json`。凭证绑定原config、phase packet、phase-process及stdout／stderr。schema1保持原合同。

`run_paired --reconcile-completed-stage POSITION`显式校验原manifest／stage身份、原完成证据和全部native终态，不运行任何command。控制器和新wrapper共用现有file_lock机制，活跃owner拒绝对账；恢复后状态为`reconciled_complete`，原returncode为NULL时仍为NULL，不伪造外层exit0。完成不表示质量合格。重复对账／proof写入后崩溃可复用同一immutable证明，resume重新校验后才可继续未启动的stage。旧失败对账接口保留。

完成证据的构造及复验共用`_completion_evidence`，没有第二套判定主体或provider循环。正常孤立Python入口沿用`arm_entry.py`，只增加评估helper路径，产品仍从安装包导入。

## 本轮发现并修复的主流程回归

此前quick-map新策略2写入coverage，但modules／deepen／harvest的共享metrics替换未保留它，depth还有两处直接覆盖；恢复可能把原策略2误当成legacy1，触发输入变化拒绝。修复唯一`init_generation.replace_coverage`，同时保留generation／quality journals和策略字段；depth两处直接赋值删除，迁移为同一委托。现有旧记录缺字段仍按原兼容合同处理，不猜测或升级已保留批次。

机器清单新增1条删除记录、列出这2处旧赋值；现有70项能力归属不变。生产仅2文件变化，行数净+0／AST节点-1；评估机制另列，见code-delta.json。

## 验证与交付

当前完整双仓补丁、wheels及证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/stage-completion-20261017T103645Z/stage-completion-review.tar.gz`)、验证记录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/stage-completion-20261017T103645Z/validation.json`)、代码身份 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/stage-completion-20261017T103645Z/code-identity.json`)、CLI实际探针 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/stage-completion-20261017T103645Z/cli-proof.json`)。补丁分别对647e6e02／1e70e983应用一次，不叠加旧overlay；真实index未改变。当前Copilot wheel SHA256 `1b36ed44ab9fc649ec364aa51c0747f25aeda4e9c07cf024366db721910309a9`，Bot保持2db4。产品成员集合不变，仅2个Python成员变化，全部安装成员与wheel一致；当前eval源码也与隔离测试快照逐文件一致。

正常安装环境运行完整评估套件434项、产品恢复定向67项，均通过且无skip。使用`-I`及源码外的fixtures／eval副本，无产品src注入；依赖复用，不称fresh。源码定向修复92项通过。真实SIGKILL探针证明控制器死于外层ack写入前时，原工作流凭证可恢复并保留未知外层exit；native边界为脚本替身，不是真实模型或Docker验收。

首轮新测试缺少标准arm_entry组合导致隔离导入失败，纠正fixture后通过，原失败JUnit保留。首次源码全套的depth失败定位了上述产品回归；6项installed-only原skip保留，后续当前安装全套覆盖。原日志没有擦除，也没有把未结束进程当成失败重启。

## 原519及后续

只读前后逻辑行hash一致：candidate complete／0，baseline running／NULL，10个native调用保留。原baseline是schema1且缺外层receipt，不能用本协议补造旧完成凭证，继续pending；原质量整例失败不变，所有519分配／闲置／unknown保留。

本轮没有真实模型调用、commit、push、PR或部署。当前完整产品双仓／远端／真实质量未重验；完整目标保持开放。后续继续当前版本的整合、真实质量与预算／深层恢复验收、其他后端和最终配对发布门禁；这些不重开已收口的框架阶段。
