# 框架后恢复验收：knowledge专用clone的进程崩溃恢复

修复了真实复现的宿主恢复缺口：SDK已写入候选后，worker在validator入口用 `os._exit(137)` 强制退出，不执行Python异常清理。工作树／暂存区都可能留下未验证规则。此后上游更新同一规则页，旧 `_prepare_clone` 先merge、后清理，导致恢复报“daily knowledge branch conflicts with upstream”，尚未调用fixture模型。两个原始失败保留。

恢复仍归属ReviewBot，未给SDK增加业务账本或第二套curation循环。现有专用publication clone先 `reset --hard HEAD`／`clean -fd` 丢弃未提交残留，再checkout和merge；删除原来merge后的清理调用。HEAD和已提交的daily／export-resume分支保留，未重置到origin而丢失旧批次。此操作仅针对宿主专用clone，不针对live SDK checkout。

## 验证与交付

源码117项knowledge distiller／intake／promotion／整合及public SDK边界通过，无fail／error／skip。安装wheel的70项distiller／进程恢复／knowledge端到端通过；一个源码AST边界用例只在源码检查，不注入产品src去伪装安装消费者。两种强制退出残留均恢复为新的上游精确字节，恢复前先清理，原两条intake行完成；fixture模型执行一次新的已计数业务尝试，而不是声称零调用resume。既有已提交批次的export resume仍由原journal和实现处理，其零模型调用用例继续通过。

当前Bot wheel **f3594226e689bf6872129c2c399856db8ff20e7aeeb25f257ebfc90be850ecb2** 相对a57仅knowledge_distiller.py／RECORD改变；129个安装成员逐字节匹配。Copilot749a wheel未改。依赖复用、正常venv导入，无产品src路径注入，不称fresh最小消费者或新完整配对／远端验收。

代码、wheel与证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/knowledge-clone-crash-recovery-20261017T073050Z/knowledge-clone-crash-review.tar.gz`)；结果 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/knowledge-clone-crash-recovery-20261017T073050Z/result.json`)；独立Bot overlay (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/knowledge-clone-crash-recovery-20261017T073050Z/reviewbot-overlay.patch`)；精确代码基线与重建 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/knowledge-clone-crash-recovery-20261017T073050Z/code-identity.json`)。

生产变化1个Python文件，+7／−2行、AST净0；变更是清理位置与Git参数，非新增执行循环。无本次模型／GitHub调用、commit、push、PR、生产数据修改或部署。此前框架代码包及恢复切片保持原范围。

## 剩余范围

本例是validator尚未派发的精确崩溃窗口，不证明任意SIGKILL及孤儿validator的处理，也不将SDK单独使用变成耐久多文件事务。原调用执行可能仍存活时须保留原环境，不能据此授权覆盖／重派。另一个明确未关闭窗口是Git commit成功但unpublished journal尚未持久化；需要在同一宿主恢复owner内处理，不能靠模型重新生成充当原结果。真实模型unknown身份对账、质量、其他vendor和最终release／pins／在途保留／回滚／rollout仍开放。

先应用[框架交付](single-capability-framework-closeout-current-20261017.md)full patches，再依次应用[旧pending部署守卫](single-capability-legacy-pending-deploy-drain.md)、[SIGINT回滚](single-capability-curation-interrupt-rollback.md)各自overlay，最后应用本包对应仓库overlay；每个只应用一次。完整目标继续保持活动，不将此定向检查当作最终验收。
