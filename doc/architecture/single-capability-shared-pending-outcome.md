# Shared pending outcome follow-up (2026-10-16)

框架交付保持冻结；本切片处理其上线前 P1 中 Strict handler 错误丢失未决事实的问题。真实模型质量、深层恢复及发布目标不变。

旧代码四种未决错误（pending、sent/not completed、SandboxPending、无派发事实的旧 host timeout）经 Strict SDK 都成为 blocked；明确 not-sent 控制正确保持 blocked。当前四种均为 interrupted，经真实 SDK result 原样进入 Bot 四个 guard 后保持 pending。同业务键／原 run 重复 child 不执行第二次。

| 边界 | 当前实现／删除 |
| --- | --- |
| Transport 事实判断 | providers/outcomes.requires_reconciliation 复用 budgeting 的中立 native facts；处理沙箱 pending 和旧 timeout 的保守语义 |
| 通用执行内核 | Executor 只提供注入的 on_handler_error 回调，不导入 provider／knowledge／improve 业务逻辑 |
| 应用组合 | WorkflowExecution 为每个 run 独立观察未决错误，投影 interrupted；并发 run 不共享该状态 |
| 持久化 | ExecutionResult 携带状态，Core 保留 typed result 到 status 写入；整数 exit API 继续委托 |
| Direct / MoA | 删除 Direct 的独立判断、runtime pending flag 和状态改写；MoA 调用同一判断，正常已知故障 fallback 保留 |
| ReviewBot | 生产实现未改；四个既有 guard 接受真实 SDK 未决 payload 并拒绝消耗业务 attempt |

源码 Direct/Strict E2E、应用、内核、CLI/MCP/SDK 与依赖边界检查通过；新精确 wheel 在独立环境离线 hash-required 安装、pip check、RECORD／源码字节、公开 SDK smoke、Direct/Strict/MoA 和 SDK→Bot 桥接通过。仅模型／GitHub 边界使用替身，无真实模型调用、原账本改写或发布。

当前 Copilot wheel：`a45fc528570e34a25c8bd86a66295b899b82df8556bc8006a3512a563697ab53`，Bot 保持 `9399980b86403b3d83682ba06bce56da9512145ba1a13b127b9584e4f520e23b`。与冻结框架相比生产变化：6 个文件、净 27 行／AST 节点 114；不以减行数作为验收标准。

证据与增量代码包：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/shared-pending-outcome-20261016`。result.json、delta.json、overlay.patch、精确 wheel 与原始报告可独立审阅。先应用 framework-closeout-20261016 完整交付，再应用 overlay.patch；旧框架包 SHA609dfaaf 保持不变，不把旧报告改名为新包报告。当前切片不宣称完整配对全量／远端 namespace／真实模型质量验收。

未关闭项：超时脱离子进程清理不稳定性（原失败与一次通过均保留）；原未知付费执行按原身份对账，不创建替代派发；handler 之外更深的崩溃、预算和结算窗口；真实质量及其他后端；release／pins／部署。下一项 P1 是超时进程清理。没有 commit、push、发布或部署。
