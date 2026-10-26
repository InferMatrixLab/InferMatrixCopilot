# Process survivor propagation (2026-10-16)

本切片完成结算后真实存活工作到待对账结果的运行期传播，不缩减完整目标。

旧实现的四个反例均已复现：已记录 detached 子进程因注入权限错误而继续存活，supervisor 却正常返回；observer 自身失败时未带回该事实；直接／observer 包装的清理错误进入 ModelGateway 后都允许 fallback。当前保持错误与 pending，不启动第二模型。

| 层 | 当前归属与行为 |
| --- | --- |
| process_tree | terminate_tree 返回存活 PID；OwnedProcessTree 关联原快照 birth，未知 birth 保留 None，不形成猜测 kill 权限 |
| process_execution | 有界 wait 后收集未结束的 leader；存活工作经现有 ProcessCleanupError 通道报告，附 OwnedProcessCleanupError 的运行期身份、部分输出与 leader_returncode。leader 的真实 returncode 未知时明确为 None，兼容诊断 ProcessOutcome 使用 -1，不能作为完成回执 |
| I/O 与外部资源 | 未决进程即使持有输出管道／堵塞 stdin，supervisor 仍能返回待对账错误；外部 cleanup 继续调用，多种错误均保留；observer 原异常类型保留并附清理错误 |
| providers/outcomes | 唯一判断同时识别 ProcessCleanupError 和 observer 附带的清理错误 |
| providers/completion | 委托上述判断，传递 native_outcome_pending 与预算 pending 事实，禁止 fallback；删除独立 pending-flag-only 判断 |
| 应用与 SDK | 复用现有注入策略；Direct／Strict 清理错误保持 interrupted，原业务键／run 不产生重复 child 模型执行。Bot 生产未改 |

源码和全新精确 wheel 验证通过：真实 detached／leader 进程、被拒绝的 TERM/KILL、管道与阻塞输入、原 observer 类型和输出、PID／birth 安全、成功 command 后台契约、provider 和预算 pending/no-fallback、Direct／Strict E2E 及公共 SDK。离线 hash-required 安装、pip check、RECORD 与全部生产 Python 源绑定通过；无产品源路径注入。模型／GitHub 为替身，无真实模型调用、旧账本或原环境改写。

首轮源码原始报告保留一项计时测试失败：idle .1 可在冷启动时早于 absolute .2 合法触发，测试却固定断言 absolute。测试的 absolute 场景现在令 idle .5，并显式加入 .15 启动延迟；另两个场景仍验证心跳重置 idle 和 idle 先超时。产品超时配置与判断优先级没有改变。source-v2.xml 与 installed.xml 是当前范围的通过证据，不覆盖该原始报告。

Copilot wheel：`a8f4da3b76d6b8c13ed73638c36d54f14c3fba00602699f7317042d27a7df799`；Bot 仍为 `9399980b86403b3d83682ba06bce56da9512145ba1a13b127b9584e4f520e23b`。生产相对 b79：四文件净 41 行／260 AST 节点；一个新测试模块及相应 SDK／计时用例另计。

证据与代码：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/process-survivor-propagation-20261016`。result.json、overlay.patch、delta.json、精确 wheel 与原始报告可审阅。依次应用框架609dfaaf、共享 pending bd61edae、退出结算83d80ad6，然后应用本 overlay；每层原始包与报告保持原身份，最终整合时再生成完整配对包。

未完成：运行期 survivor 身份／诊断的跨崩溃持久化及恢复协议；观察窗口中的未记录／重新归属进程；其他平台的实际清理；更深故障／预算／回执矩阵；原未知付费调用按原身份对账；真实模型质量及 release／pins／部署。本切片不证明完整复原或发布验收，不把未知工作改记为已完成。未 commit、push 或部署。
