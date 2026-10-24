# Durable cleanup evidence (2026-10-16)

本切片持久保存 controller/native-call 层已有的清理诊断，修复原执行门禁在重启后可能投影 pending 记录的问题。完整目标与后续验收保持不变；不新增自动回收、派发或账本。

| 能力／边界 | 唯一实现与迁移 |
| --- | --- |
| 诊断捕获与格式 | process_execution 的 process_cleanup_facts / validate_cleanup_diagnostics；记录 schema owned-process-cleanup/1、生产方 boot_id/PID namespace、PID/birth、明确未知的 leader_returncode、错误类型。只存类型，不复制错误消息为诊断字段 |
| 部分输出 | ProcessCleanupError.outcome，以及保留原类型的 observer error.process_outcome；继续通过既有 native stdout/stderr blob 机制存储 |
| Provider | complete_native 委托上述 codec，显式保留 payload 时附加事实；不从接收方重新推断生产方 namespace |
| Trace 与 attempt | 两种 recorder 共用 native_process_fields；NativeCallArchive 存同一诊断，validate_native_attempt 调用同一 validator 并比较 record/archive，旧记录缺省字段仍兼容 |
| 原执行门禁 | run_original_native 在原 intent/attempt 下验证记录；显式 native_outcome_pending 不允许结果投影、写 derived result 或再次派发 |

三个旧实现反例成立：直接／observer 清理错误没有归档身份及部分输出；新解释器恢复时旧门禁投影了 unknown 记录。当前源码与精确安装包的反例通过；保留未知 birth/leader 值，修改 attempt 中 birth 会拒绝。foreign producer scope 在安装包用例中保持原值；新解释器继续持有原 intent 和唯一 attempt，dispatch 总数不增加，不调用 project，不产生终端 derived result。实际 observer 输出补验也通过。

验证还覆盖 provider completion、hosted/native workflow、既有 native attempt recovery、原存活进程/no-fallback，以及新 wheel 的 Direct／Strict E2E 与公共 SDK smoke。安装为全新离线 hash-required 环境，pip check、RECORD 和全部生产 Python 与冻结源字节绑定通过，无产品源路径注入。模型/GitHub 为替身，未调用真实模型；旧 paid/unknown 账本、旧运行环境及交付包不变。

Copilot wheel：`79d319c57ac59779d85528fff3074d37ff55d583d50aea1143b302ea212881d0`；Bot 仍为 `9399980b86403b3d83682ba06bce56da9512145ba1a13b127b9584e4f520e23b`。相对 a8f4，生产四文件净 104 行／993 AST 节点；新增测试另计。证据和增量代码包：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/durable-cleanup-evidence-20261016`。result.json、overlay.patch、delta.json、精确 wheel 及原始报告可审阅；overlay 接在 process-survivor e94f416f 及其原冻结交付链之后，旧报告不冒用为新全量报告。

scope、PID、birth 仅为原始诊断，不能凭这些字段发送信号或批准新付费调用；即使已无活进程，也不等于模型已完成或费用已结算。可用事实来自原生 receipt／原执行位置、版本、profile、业务键等既有权威。缺失 boot/namespace 时保持 None，不补成本机，不新增虚假恢复权限。

尚未完成：inner completion worker 错误经跨进程帧传到 controller 时完整携带同一诊断（下一步复用本 codec）；写 record/attempt 间更深崩溃窗口；其他平台及观察窗口；真实原未知调用的完成回执对账；真实质量、其他后端与最终配对全量／release/pins/部署。本切片验证的是持久诊断和拒绝不安全恢复，未宣称模型任务已成功恢复。未 commit、push、发布或部署。
