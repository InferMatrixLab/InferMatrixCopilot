# 框架后恢复验收：knowledge commit intent

已修复真实复现的窗口：Git commit成功，但在 `remember_unpublished` 之前worker强制退出。原提交仍在，恢复却重新进入模型边界。两种交付模式的原失败用例保留，模型是fixture，没有真实付费。

## 唯一归属与实现

恢复归属ReviewBot `KnowledgeBatchRecovery`。在Git commit前，先通过原ledger metadata事务持久化commit intent，绑定原clone、branch、parent、staged tree、intake rows及随机事务nonce；提交trailer保存整个manifest的SHA-256，避免只修改rows等字段就给另一批次借用原提交。模型、验证、repair实现仍在Copilot，不新建模型恢复循环。

恢复在任何clone清理／新模型调用之前运行：原分支仍为冻结parent时，进入原有有界业务重试；若分支已前进，只接受parent／tree及事务摘要精确匹配的原提交，原子写旧shape的unpublished receipt并清空intent。匹配失败、不明分支或另一个未导出批次均保留pending，不能自动覆盖或生成替代内容。正常commit也委托同一核对函数，legacy无intent包装保留原record格式。

整批执行复用现有 `single_writer` 内核文件锁，在ledger目录及专用clone工作目录分别取得独立batch lease；同一路径去重。第二个进程不能在原commit活跃时清除其intent，也不能先领取行或调用模型。强制退出由内核释放锁，不用持久PID猜测存活。没有执行内核／SDK／领域依赖回流。

## 验证及代码

源码120项相关恢复、distiller、intake、knowledge整合及SDK边界通过；安装wheel86项通过，均零fail／error／skip。安装消费者不含产品src注入，复用依赖，不称fresh最小安装。一个源码AST检查仅在源码树运行。真实Git／SQLite与 `os._exit(137)` 覆盖commit前、commit后及receipt前后；故障注入覆盖写receipt失败、原manifest各绑定不一致、已有另一批次和两类锁竞争。已提交原批次恢复零新增fixture模型／attempt；未提交前的已证明本地状态可走原有业务重试，不冒充零调用resume或真实模型unknown已结算。

原首次命令误写不存在的test_single_writer.py，未执行用例，错误日志保留；之后实际相关套件通过。prototype及pre-final报告保持各自代码范围，最终source／installed报告单列，不改写旧失败。

当前Bot wheel **0362632c7ce469a8341fb5af92309feb46204b511975199225b4565fc3e07ab6**，相对f359只改变knowledge_distiller.py、knowledge_batch_recovery.py和RECORD；129安装成员匹配。Copilot749a wheel未改，不继承新完整双仓／远端或真实质量结论。

代码／wheel／证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/knowledge-commit-intent-20261017T073556Z/knowledge-commit-intent-review.tar.gz`)；原结果与命令 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/knowledge-commit-intent-20261017T073556Z/result.json`)；Bot overlay (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/knowledge-commit-intent-20261017T073556Z/reviewbot-overlay.patch`)；精确代码重建 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/knowledge-commit-intent-20261017T073556Z/code-identity.json`)。

生产变化2个文件，净+112行／+1021 AST节点，明细见production-delta.json。包含协议校验与宿主序列化，不以减行数为门槛。原framework封存、70能力／35删除记录与此前代码链保持独立。

## 尚未关闭的发布门禁

新pending intent不能交给不识别此协议的旧宿主处理。需要在最终release／rollback准入中协商或阻断这种降级，保留原协议感知release及状态直到完成对账；本切片**未认证该降级路径，不能直接部署**。旧无intent记录的向前读取兼容不等于旧binary可以安全读取新pending状态。

这也不解决真实模型unknown或任意孤儿validator。完整质量、其他后端、深层剩余窗口和不可变release／pins／在途保留／回滚／rollout继续开放。未commit、push、PR、生产修改或部署。

应用[原框架full patches](single-capability-framework-closeout-current-20261017.md)，再按此前overlay链直到[clone崩溃恢复](single-capability-knowledge-clone-crash-recovery.md)，最后应用本包对应仓库overlay一次。完整目标保持活动，框架阶段不重开。
