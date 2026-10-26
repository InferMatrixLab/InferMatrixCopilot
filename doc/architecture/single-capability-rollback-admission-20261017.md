# 回滚 admission 恢复：失败保留原环境和证据

框架阶段保持收口。本轮修复ReviewBot宿主部署路径的真实缺口：cleanup吞掉恢复flags／启动服务失败后仍删除staging；start_services的后一次成功还会掩盖第一次失败。另复现了journal缺失／空列表／错误目标／错误flags及主配置缺失仍返回成功的路径。

唯一owner继续是ReviewBot部署宿主：start_services逐个传播失败；cleanup只在原恢复操作成功后继续，失败则保留原pair／staging／worker记录，重设maintenance并调用同一pause函数，不自动重试或停止可能已启动的服务。原exit非零继续保留；原为0而恢复失败时改为非零。未开始drain的早期失败保留原配置和已有maintenance，不伪造恢复。

strict_drain_state用一个saved_instances读取／校验原journal；两个目标及全部原flags在写任一配置之前检查，主配置不能被静默跳过。prepare和恢复使用同一实例映射。缺失／损坏拒绝恢复；pause无法持久化时保留明确错误及staging，不能把它描述为已验证的完整恢复。没有新增provider或模型循环，也没有移走Bot业务账本。

完整双仓补丁、配对wheels及证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/rollback-admission-20261017T112850Z/rollback-admission-review.tar.gz`)、验证范围 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/rollback-admission-20261017T112850Z/validation.json`)、代码身份 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/rollback-admission-20261017T112850Z/code-identity.json`)、增量patch (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/rollback-admission-20261017T112850Z/repair.patch`)。各完整patch对原647e6e02／1e70e983应用一次，不叠加旧overlay。真实index未变，没有commit／push／发布／部署。

当前Bot完整套件2881项和最终受影响109项通过，均无skip。复用现有run_process监督的原进程写入exit0／reason null凭证，冻结输入及日志／spec哈希匹配。bash语法与diff空白检查通过。旧4项失败、journal8项失败、缺配置2项失败及中间2项兼容断言失败全部保留；后两项改为验证“缺证据拒绝恢复，未变flags与原legacy intent仍保留”，没有删除原安全断言。

测试实际运行shell函数、临时SQLite及配置；服务启动、SDK活动查询等边界由fixture控制，未操作生产systemd或真实模型。release packaging边界55项在最终journal收紧前通过；本轮最后的完整Bot套件覆盖当前所有资源。两份Python wheel仍为Copilot1b36／Bot2db4，src内容逐文件未变，沿用[精确配对验证](single-capability-current-pair-integration-20261017.md)，未重跑未变Copilot完整套件或重建镜像。此前报告仍绑定其原资源，不改名成当前补丁结果。

生产资源仅server-deploy.sh：净+41行，src Python／AST无变化；5段嵌入Python的AST节点净+290单列，未把shell内Python漏算成0。统计见code-delta.json，净减行不是门槛。

这关闭的是回滚恢复错误被吞掉及其证据丢失路径，不是完整崩溃／多主机恢复或真实上线验收。原519及此前unknown／闲置保留，真实质量、其他后端、真实release／pins、在途保留或排空及rollout继续开放；不重开框架交付。
