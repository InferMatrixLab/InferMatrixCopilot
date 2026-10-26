# 强制 worker receipt 的 pending 事实修复 — 2026-10-16

真实冻结源码调用使用协议8，必须验证完整 worker receipt。当前2d27在绝对超时后拒绝无完整 receipt 的结果，但抛出的 SandboxPending.native_outcome_pending=False 与已派发事实矛盾。allow_fallback=False 和原 binding 已经防止重派发，本修复没有发现或宣称既有代码执行了重复付费调用。

唯一 decoder ContainerExecution._decode_worker_result 的两条拒绝分支现在显式 prior_execution=True。live／恢复读取及所有调用方共用此 decoder；错误类型、消息、禁止 fallback、协议版本和旧 live-exit 兼容行为不变，没有新增业务循环或状态账本。

修复前以真实Docker／当前2d27镜像、冻结Git/source输入复现协议8错误标记，并保留原请求、停止回执、输出和失败报告。单元覆盖必需 receipt 缺失／损坏及旧协议恢复。修复后新wheel／新镜像／新独立环境实际覆盖 local与SSH：complete和idle有有效receipt；absolute和cancel缺失完整receipt保持pendingTrue，原结果读取仍pendingTrue，不能把容器已停止当作模型完成，重复执行拒绝，binding不变。两个位置各四个协议8 start-intent，原2d27复现目录和付费／未知执行不修改。

源码和全新离线安装包的相关容器／provider／worker／accounting及Direct／Strict SDK检查通过。精确包hash、原始XML、实际两位置报告、原始controller／worker回执、两文件overlay重建和生产行数／AST增量见输出目录 `required-worker-receipt-20261016/result.json`。这是当前必要定向验收，不以此前2d27完整源码／七组整合报告为新wheel全量证明。

同轮普通命令协议3和idle协议7的真实进程控制另见 `single-capability-sandbox-process-conformance.md`；它们不能替代这里的强制receipt协议8验证。没有模型／GitHub调用、账户凭据挂载或既有环境／pin／发布／部署变更。框架交付保持收口，完整目标及真实模型质量、完整恢复／backend和发布验收仍开放。
