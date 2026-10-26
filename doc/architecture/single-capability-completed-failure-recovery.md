# 已完成原生调用的失败 archive 恢复 — 2026-10-16

框架阶段交付保持收口。本后续切片补齐恢复矩阵中的一个缺口：原生调用已经返回、输出校验失败且失败 trace 已落盘，但进程在 attempt 结算前退出。

`kb recover-attempt --attempt ID --record ID --offline-confirmed --outcome failed` 复用原 TraceStore 恢复实现，将 archive 结算为 failed，保留原错误、回复、用量、费用、身份及 trace 绑定。默认仍为成功恢复。只接受原 trace 明确已派发、已完成且非 pending 的失败；不推测旧记录，不恢复未知／未完成传输，不触发新模型调用，不新增 trace，也不结算领域预算或质量 checkpoint。

测试通过独立 Python 进程执行实际质量工作流，用 fixture transport 产生一次回复，校验失败后在 NativeCallArchive.finish 强制退出。随后真实 CLI 在另一解释器恢复；有／无 SQLite 索引均通过，重复恢复保留 attempt 字节、journal 字节及一次派发计数。pending、未完成、未派发、缺事实、无错误、篡改事件、冲突终态、未停写确认等在写入前拒绝。旧成功恢复、共享质量审查、cost/review 恢复和 Direct/Strict SDK 流程通过。

源码与全新离线安装包验证、精确 wheel hash、修复前失败及生产行数／AST 增量见输出目录 `completed-failure-recovery-20261016/result.json`。新包仅作本切片定向验收，上一候选的全量报告不作为新包全量证明。没有真实模型／GitHub 调用，没有改动原付费未知记录、环境、pin、发布或部署。

更深的失败传输恢复、预算／回执矩阵、真实模型质量和其他后端仍按完整计划推进，不影响既有框架交付。
