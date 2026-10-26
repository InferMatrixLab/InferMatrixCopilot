# 独立 lease-store Strict：协议及派发前失败

框架已交付，完整目标仍开放。此页属于后续P1质量／运行诊断，不修改框架交付结论。

原冻结manifest SHA256为 `1e9a41030314d12a062e1b63f1a908e0f4c9550c91523134e78517e3b23af28d`。revision lease-strict-20261017保留旧235、新增44，总上限279；每arm22是硬上限，非完整工作流最坏调用量证明。保留正常二轮、验证、修复及carried recheck，不裁剪工作流来适配额度。所有十项质量分层保持登记，其他后端按用户决定后续验证。

评估输入修复：每arm独立memory_db，初始为空，保留正常检索和写入；不继承共享用户debug memory。事实报告委托canonical RunTrace.events，区分未知memory计数和零，记录agent成功／失败，不另造质量完成谓词。旧SDK零模型负控证明多个必要agent失败后仍可能done，因此不能直接把done当作质量通过。最终评估工具定向检查62项通过，非真实质量验收。

真实Personal-Agent零模型检查通过了当前immutable容器、原生CLI指纹、账户元数据和只读bridge能力；不是实际模型质量证据。

原协调器PID3696353已停止；SDK `run-20261017-024240-69f298` 为blocked，原因 `source changed during snapshot`。phase driver捕获错误返回0，stage保留needs_reconciliation。该case未生成calls.sqlite和native-sessions目录；baseline未执行。现有证据显示在快照准备阶段失败，但原错误未保存发生变化的文件名，不能判定具体根因。不能据此释放旧额度或重派发原case。下一步以零模型诊断定位变化路径，并保留证据变化拒绝策略。

证据包 `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/strict-predispatch-closure-20261017/strict-predispatch-review.tar.gz`，SHA256 `9217e38a9d4eb28da355505a1bf0aedf36f87d4c7a93ab43f5ce1c6e6b5081bb`。99个成员逐项hash核验，包含冻结协议、准入、白名单控制结果、五个eval helper／测试补丁及行数／AST变化；不导出认证缓存、请求payload或原运行数据库。生产wheel仍为6629／3bbf，未改生产实现、commit、push或部署。
