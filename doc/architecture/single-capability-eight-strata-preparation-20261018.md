# 八层后续比较的阶段输入准备 · 2026-10-18

框架保持收口，产品仍为Copilot a10c3484…／ReviewBot24ac7cfb…；本轮无生产代码／AST变更，无模型调用。

## 可复用的准备结果

已冻结当前eval helper文件及16个baseline／candidate阶段文件，包含38个完整工作流或明确标注的补充判断阶段。它们沿用现有quality-stage/2、quality_stage_arm.load_phases与arm_entry，不新建模型调用、判断、修复或阶段重试循环。

16个文件均通过现有loader的命令／helper hash与block-input合同检查。使用真正arm_entry bootstrap，在原baseline及当前安装解释器中检查20个唯一入口的--help，全部通过；这证明入口可加载，**不证明工作流／模型已经执行或通过**。最初绕过bootstrap的-I直接执行导致helper import错误，原检查保留，最终检查使用实际入口方式。

当前controller安装的2337个产品成员逐字节匹配a10 wheel，pip check通过；依赖从已有dev环境复用，不称fresh安装。baseline环境未升级。

## 静态检查实际拦截的输入问题

旧manifest-v8的Foundation两个supplement使用op=edit；当前canonical block合同要求prose/prose，旧输入因此被正确拒绝。本次只在新目录复制并改为op=prose；text、page、evidence等其他字段完全一致，manifest checksum重新冻结，原数据和原失败记录未改写。没有降低loader的限制来取得通过。

Direct／Strict阶段的workflow timeout分别明确为900／3600秒，原生单调用／宿主超时仍需在新主manifest中分别冻结。这是新比较的显式配置变化，不能重绑定旧实验结果。

## 八层范围、调用上限及尚缺连接

准备范围是Direct、Strict、Screening、Foundation、Strict depth、Lightweight depth、PR-history与Maintenance。Discovery／Module-card因当前Codex-only角色与既有领域策略冲突继续后验；完整十层目标和旧pending／失败／未执行状态保留，未将两层算为通过。

草案按现有各域内部修复上限提出新比较最多182次native call（双臂合计）；这是**尚未分配、尚未获准入的提案**。没有修改预算revision、519累计分配、spare或旧台账，没有创建run_paired live manifest、ledger或模型stage输出。

draft-stage-index.json (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-eight-strata-preparation-20261018/draft-stage-index.json`)保存具体可审阅命令与hash；它不是可派发的paired manifest。仍须完成：

1. 新比较的per-arm source／knowledge／schema与host-only标签完整绑定、可见范围检查和明确容差。
2. 各完整workflow的installed fixture准备；PR-history显式留存与冻结batch。
3. 当前镜像／proxy／credential context、服务端连接条件及不可变版本准入。
4. 保留旧519／spare／unknown的明确新预算修订，以及完整manifest的静态准入。

这些连接完成前不能派发模型；阶段loader成功不替代它们。

## 原完整源码回归

a10冻结源码的完整Copilot回归仍运行在原pid1587075；原进程与最终结果 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-full-source-20261017`)。没有因观察超时重新执行，也未宣称最终通过。其冻结tree／wheel身份保持，当前新增仅准备文件和文档。

本轮准备归档 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-eight-strata-preparation-20261018/a10-eight-strata-preparation.tar.gz`)含旧拒绝、修正后输入、phase文件、loader结果、入口日志与安装证明。没有commit／push、GitHub写入或部署；完整目标仍未完成。
