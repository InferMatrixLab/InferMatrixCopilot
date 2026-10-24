# 框架后发布验收：knowledge commit协议边界

已补齐[commit intent切片](single-capability-knowledge-commit-intent.md)登记的本地准入／回滚缺口。修复前三个负例显示：两实例的pending intent没有阻止选择旧host；健康失败的cleanup还会把app切到不能恢复该状态的旧host。原失败记录保留。

## 协议与边界

ReviewBot `knowledge_batch_recovery.COMMIT_INTENT_PROTOCOL_VERSION` 同时用于原codec／producer及目标宿主能力声明。deploy的既有 `knowledge_protocol_check` 使用**选定release自己的Python**读取两份共享SQLite ledger（mode=ro），并与实际待恢复schema集合精确匹配。当前reader只支持一个schema，不把更大的版本号推定为兼容；损坏DB、坏schema、缺失或错误类型的descriptor均拒绝。没有pending或intent已清空时不再施加此要求，既有containment latch继续独立检查。检查不解析／重建业务commit或清除pending，业务恢复仍只有原owner。

同一个checker覆盖candidate预检、最终停止屏障及健康失败rollback。最终屏障在两份SQLite claim锁及原ledger batch lease下重查：重用函数声明生成只供本stage调用的gate，没有另一套判断实现；不兼容时专用返回码表明尚未执行任何stop，恢复开关、保留原服务。原claim锁和batch lease保持到两个stop命令完成。活跃knowledge CLI会使gate退回等待；之后手动batch取得原lease后也会尊重maintenance，不领取行、不派模型、不结算原intent。锁机制仍复用single_writer。

健康失败时若旧host不兼容，保留current app、maintenance及stage，不启动旧host。兼容旧host的原回滚路径继续可用。共享state、原commit intent及旧worker／release均不被此检查删除。

## 当前证据与代码

源码209项部署／排空／回滚／knowledge恢复及SDK边界检查通过，安装wheel88项通过，均无fail／error／skip。实际旧f359安装环境拒绝schema1 pending、当前安装接受；清空intent后旧环境通过这项gate，SQLite字节在只读检查前后不变。部署shell来自当前源码，目标host模块来自各自正常安装venv，无产品src注入；这是本地选定release检查，不是生产部署。

测试覆盖两实例、精确版本／未来或混合schema、坏数据库、预检后才出现intent、活跃batch与stop期间持锁、maintenance下手动调用、兼容及不兼容健康回滚。模型／GitHub和实际服务边界为fixture，没有真实付费或服务停止。一个source AST guard只在源码树运行，不放进安装消费者。早期及pre-final报告保留原范围，最终结果单列。

Bot wheel **41cceff52a2458ef2cfd9ea039f538a09b2331ce0da9dcc04127a1173b6d8bda** 相对0362只改变knowledge_batch_recovery.py、knowledge_distiller.py及RECORD，129成员匹配。部署shell不在wheel内，未来release bundle还必须包含本切片deploy脚本。Copilot749a未改；当前无新完整双仓／真实Docker／质量结论。

代码、wheel与证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/knowledge-commit-protocol-deploy-20261017T074623Z/knowledge-commit-protocol-review.tar.gz`)；结果 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/knowledge-commit-protocol-deploy-20261017T074623Z/result.json`)；Bot overlay (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/knowledge-commit-protocol-deploy-20261017T074623Z/reviewbot-overlay.patch`)；原安装协议探针 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/knowledge-commit-protocol-deploy-20261017T074623Z/installed-protocol-gate.json`)。

生产3文件净+53行；Python文件AST净+31，shell中两段Python另计+284，不把shell流程混入Python文件AST。完整逐文件统计保留，不以减行数为门槛。

## 保留范围

本次关闭的是所列**本地协议准入、停止竞争和健康rollback**验收，不表示生产rollout已批准。最终仍要核实真实生产配置／在途原环境保留、当前不可变配对全套与shadow质量、原native unknown、其他后端及完整发布／回滚操作。混跑不遵守共同lease／maintenance的旧writer不属于此证明；新deployer必须随配对release安装后再开放admission，正常部署流程在解除maintenance前完成此步骤。

[框架封存](single-capability-framework-closeout-current-20261017.md)及此前代码链保持原范围。按前一切片overlay链应用到commit intent，再应用本包对应仓库overlay一次。无commit、push、PR、生产数据改动或部署，完整目标保持活动。
