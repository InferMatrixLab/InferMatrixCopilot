# 恢复修复后的精确配对整合：749a / 41cc

当前Copilot **749ab461a2ca35dc1e2868f414c99da8a43fcd13119892f80bbf676aae0c6c25**／ReviewBot **41cceff52a2458ef2cfd9ea039f538a09b2331ce0da9dcc04127a1173b6d8bda** 的本次确定性整合已终态。框架继续封存；完整模型质量、其他后端与最终发布仍开放，不把这里的通过当成完整目标已完成。

## 接线与当前验证

原exec70959内的Copilot PID2430959已终态0，5973项零失败／错误；原始4个环境skip保留。同名同源码用例在当前精确wheels正常安装、Personal-Agent实际namespace／资源检查中补验通过，raw no-skip仍false。Bot原PID2430962完整2852项、评测工具原PID2430956完整417项都零失败／错误／skip；均实际执行当前配对，没有沿用旧Bot全套结论。

安装入口→SDK→应用工作流→provider→类型化结果→发布准备通过，覆盖Direct／Strict、local／remote、stale-head拒绝及原身份恢复；安装CP87／Bot196、边界24／23通过。fresh、hash-locked、无MCP消费者通过，2336／129产品成员匹配，整个安装site-packages无软链，无产品src注入。依赖来自已保留固定缓存，不是含全部依赖的离线installer。

实际远端正常安装及Docker镜像 `sha256:7e52b80e5e622b9650dfda5ffc9daaf692617d6fc49018455c211023aef1419c` 匹配当前Copilot wheel。源码、controller、fresh、远端和容器的init policy digest一致：`70c135744fd997b12b608e04b446418a541d3a8919bc6a45a08d6c31c8bdad87`。真实local／SSH stdio、manifest绑定及进程控制已验证，无凭据挂载或模型调用。

## 原始Docker错误与精确对账，分别报告

**初始local absolute探针出现ProcessCleanupError，原始结果没有改成通过，raw_initial_runtime_probe_all_passed=false。** 原容器在自己的deadline退出124，外层kill竞争未确认清理。通过原容器ID／daemon／label与原binding核对，读回可信worker receipt，得到原 `absolute / 124`，确认终止且再次execute被拒绝，所有原绑定hash不变；原absolute没有再次派发。streaming／idle原结果保留，之后只补做尚未执行的cancel／prestart用例。SSH原四项通过。

原trace、原始bindings／worker receipt、当前状态及独立对账结果都在包内。第一次readonly helper误用dispatch准备入口也被拒绝并保留日志；改用for_dispatch=False后只对账原对象，不创建替代执行。这里关闭的是已声明的停止／原结果恢复／防重合同，不是假称原始探针从未失败，也不涉及旧真实模型unknown的结算。

## 可审阅代码与所有权

完整当前双仓代码／wheel／验证包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/paired-recovery-integration-20261017T075627Z/paired-recovery-integration-review.tar.gz`) 包含相对原Copilot647e6e02／Bot1e70e983的完整binary patch、新增／删除、精确wheels、JUnit／命令、输入manifest、资源／安装及实际隔离证据。每个full patch对原始基线只应用一次，无需串联之前overlay；不能叠加到已有变更树。

逐项结果 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/paired-recovery-integration-20261017T075627Z/deterministic-closure.json`)／代码身份 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/paired-recovery-integration-20261017T075627Z/code-identity.json`)／原worker对账 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/paired-recovery-integration-20261017T075627Z/runtime-controls/local-absolute-reconciliation.json`)。

能力归属和架构沿用[框架交付](single-capability-framework-closeout-current-20261017.md)：Copilot拥有provider／review／knowledge／共享质量机制，ReviewBot拥有调度、GitHub业务状态、发布／回执及本地Git事务恢复。执行内核不引入业务策略；部署与手动batch复用原kernel lease，codec／部署协商由同一个host协议descriptor约束。当前登记70能力／35删除条目，框架未接连接为空，不等于所有验收已通过。

相对原Git的src统计：CP244文件净+13016行／+96588 AST；Bot40文件净-1389行／-1872 AST。相对重构前保留源码：CP174文件+9765行／+76430 AST，Bot18文件+230行／+1824 AST。包括此前保留工作，范围及逐文件hash在报告内；deploy shell／内嵌Python另在对应切片计数，没有混进src Python AST。减行数不是门槛。

9375个冻结输入逐字节复核，与源码套件使用的snapshot和工作树一致。之后只更新架构报告／索引，产品与测试输入不变。完整补丁经private index重建，真实index不变。此前progress包保存“原CP仍运行”的真实快照，当前final包另行封存，不重写旧包。

## 仍需完成

P1：当前版本的真实Codex质量与原失败裁定、原native unknown按原身份对账、其余深层恢复／预算／回执矩阵。P2：其他vendor原生／认证／Docker认证及served identity／实际费用精度。最终发布：不可变配对releases／pins、真实生产在途保留或排空、受控shadow、回滚与rollout。已有Foundation／History真实失败不改，499分配及闲置保留；本轮零新模型，无commit／push／PR／生产修改或部署。完整完成计划保持有效。
