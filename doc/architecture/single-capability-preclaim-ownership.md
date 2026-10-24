# 领取前恢复：执行归属只由 SDK 决定

框架交付保持完成，生产配对仍为Provider6629／Bot3bbf。此前观察到两次包装进程启动，不能直接推出重复付费。现在已用真实SDK子进程证明产品的两个必要约束分别覆盖并发和晚到：RunLock保护并发执行，claim_for_execution保护已完成run不再回到planning。它们同属SDK应用层，处理不同约束，不能删掉其中一个。

## 本次移除的独立阻挡

评估durable_worker原本将evaluation-worker.json作为单一不可变进程身份文件，第二个合法启动会在产品入口之前因FileExistsError退出。这不是SDK执行状态，却实际挡住SDK已允许的领取前恢复。新反例让首个包装进程在领取前退出，确认child_pid仍null、模型账本尚未创建；SDK复用同一个run重新启动后，旧helper仍把它留在interrupted。原失败及console记录保留。

修改仅在评估helper：每次启动写evaluation-workers/<startup_id>.json，继续保留首次evaluation-worker.json的原字节作为兼容记录。后续启动不再因首次元数据已存在而拒绝执行，由SDK锁／原子状态领取决定是否执行。当前执行者以run_status.json.child_pid为准，再匹配对应启动凭证；首次包装PID不能冒充当前执行者。README已更新。没有加入新的重试、dispatch或业务循环，也没有修改SDK生产代码。

## 实际进程验证

| 时序 | 实际结果 |
| --- | --- |
| 两个worker在领取前等待，再同时进入 | 一方执行；另一方在赢家的首个脚本模型调用尚未返回时因锁退出3。赢家状态没有被败方reap改为失败，随后正常done。 |
| 第二个worker在原run完成后才进入 | 它退出0并保持已完成状态，没有再次执行评审。锁已释放，此时验证的是原子状态领取。 |
| 首个包装进程在产品领取前退出 | 无模型派发、child_pid为空；SDK原键复用同run恢复，由第二个worker执行一次并done。 |

三种情况都保留两个启动凭证和首次兼容记录，worker-launch字节与run ID不变，五条脚本native回执全部complete，调用来自同一个胜出PID。测试最终不绕过评估身份写入，也不patch产品锁、claim或status。初始控制曾仅绕过旧评估阻挡以单独验证产品机制，原preclaim-results.xml与后续未绕过的结果分别保留。新增验证与此前父SDK重开和worker准入回归共19项通过，安装包／RECORD／provenance逐字节核验通过。

第一次新增日志路径实现遗漏创建目录，导致启动前FileNotFoundError；已修复，原失败日志保留。没有将此失败改写为产品问题或模型unknown。

这里是实际SDK／OS子进程、脚本模型／forge／image边界，不是实际模型或Docker质量测试。之前冻结的helper、manifest、原未知执行和环境完全未改写；仅供后续新协议冻结使用。生产行／AST增量0，评估helper／测试逐文件增量见result.json；旧235分配不变，无新真实调用、commit、push、pin、发布或部署。

历史基线预算契约、真正controller进程丢失及更多模型已派发后的恢复、Strict真实质量与其余knowledge层、其他后端和发布验收继续开放。证据：output/single-capability-completion-20261012/strict-preclaim-race-20261017。
