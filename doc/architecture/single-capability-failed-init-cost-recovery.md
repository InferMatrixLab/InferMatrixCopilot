# 已完成但输出失败的 init 费用恢复 — 2026-10-16

框架交付保持收口。本后续切片连接已完成失败的 archive 与 init 预算对账，解决付费回复被 schema 拒绝且进程退出后，实际用量无法通过成功专用入口结算的问题。

`recover-attempt --outcome failed` 后，可执行 `reconcile-init-cost --repo REPO --stage STAGE --record TRACE --offline-confirmed --outcome failed`。默认仍只接受成功调用。TraceStore.validate_native_attempt_outcome 是唯一的显式终态检查，archive 恢复和 init 对账均调用它；上一切片恢复方法中的独立事实检查主体已迁出。诊断读取仍可通过原 raw validator 查看 pending，不具有结算授权。

失败对账要求完整且一致的原 archive、原记录明确已派发／已完成／非 pending、唯一且匹配的原预留及计费模式。init Budget.reconcile 与 discovery journal 继续拥有计费规则和账本，不新增 ledger、业务循环或模型派发。actual 模式记录实际超支；费用未知保留原保守值；fixed 模式保留原额度。费用结算不会批准失败回复或完成质量 checkpoint。

源码与新离线安装包覆盖 PR-history／feature-discovery 的 actual、超支、未知和 fixed；原模型回复后 schema 失败，在 archive.finish 前由独立解释器强制退出，经原 archive 恢复和真实 CLI 对账。有／无质量成功入口、重复执行、discovery journal 写入后 mirror 保存前崩溃均验证。pending、未完成、缺事实、金额不符、重复 trace、inflight archive、未停写确认等在账户写入前拒绝；journal、archive、调用次数保持原状。

精确 wheel、源码／安装包／SDK 原始报告、六文件 overlay 重建证明和生产行数／AST 增量见 `failed-init-cost-recovery-20261016/result.json`。新包只有本切片必要定向验收，不宣称已完成新候选的全量 paired suites。没有真实模型或 GitHub 调用，没有更改原付费未知运行、环境、release pin、发布或部署。真实质量、完整恢复／对账矩阵及最终发布验收仍开放。
