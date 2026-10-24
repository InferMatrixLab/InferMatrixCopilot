# Native recovery refusal before mutation (2026-10-16)

本切片修复原 native 恢复／校验路径的六个已复现缺口，不添加模型或新的恢复业务循环。

五个旧反例中，pending=true、pending=字符串、sent=整数、completed=None 的矛盾／损坏记录会被原 recover-attempt 写为 complete；带 cleanup 但缺少 pending 标记的矛盾记录会在最终报错前改写 archive。另一个反例中，共享 receipt 包装层把不完整原 archive 转成允许 fallback 的 ModelUnavailable。

当前所有读写调用方仍委托 TraceStore.validate_native_attempt：对 record/attempt 中出现的三个派发字段要求真实 bool；cleanup 事实走既有 canonical validator 且必须绑定 pending=true；成功恢复路径拒绝 pending。校验在任何恢复写入前执行。allow_failed 的只读诊断仍可读合法 pending 记录，不把它转成成功；没有清除原状态、创建新 attempt 或派发新模型。

quality_review.verify_archived_receipt 保留原错误文字，统一设置 allow_fallback=false 和 native_outcome_pending=true。所有 init 调用方继续复用该包装层；删除旧的默认允许 fallback 转换，禁止用替代模型“修复”不完整原付费证据。

源码与精确新 wheel 定向验收通过：六项反例、正常成功／缺失 index／重复恢复控制、原 archive 和 journal 在拒绝后逐字节不变；成本／review 恢复、共享质量 runner、durable/worker/hosted receipt 与 Direct/Strict SDK 流程。新环境离线 hash-required 安装、pip check、RECORD／生产 Python 源绑定、公共 SDK smoke 通过，无产品源路径注入、真实模型调用、原历史账本或环境改写。

当前 Copilot wheel `0a31af6a980b478540657f082d872401060f3c016a687dc8f3c799bd9389452f`；Bot 仍为 `9399980b86403b3d83682ba06bce56da9512145ba1a13b127b9584e4f520e23b`。相对3f27，生产两文件净 11 行／107 AST 节点；测试新增另计。

证据：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-recovery-refusal-20261016`，含 result.json、delta.json、原始 XML、精确 wheel 与 overlay.patch。补丁以完整3f27双仓整合代码包 e3db48b2 为基底，private apply proof 校验每个变更文件。原3f27全量源码／七组安装／真实 namespace 闭环保持原身份，不改名为新 wheel 证明；当前新 wheel 只声明上述定向验收。

后续仍需：已知失败回执的明确原身份恢复与更深崩溃／结算／发布回执矩阵、原实际 unknown 的原位对账、完整 native/backend 与真实质量比较、最终新候选整合及发布/pins/部署。不从一个成功 refusal 推导原模型执行已完成。未 commit、push、发布或部署。
