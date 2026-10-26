# 配对候选与分阶段验收

当前未发布候选为Copilot `1b36ed44ab9fc649ec364aa51c0747f25aeda4e9c07cf024366db721910309a9`／ReviewBot `2db4f086ebc5549ff9ad4c11e6d9402679f7524e0171c8dee775d69fe21d8175`。框架阶段已交付；[当前验证入口](single-capability-current-pair-verification-20261017.md)记录安装主流程、全新最小消费者环境、公开SDK契约与真实local／SSH Docker控制。当前完整源码套件及精确环境补验已通过，见[当前整合闭环](single-capability-current-pair-integration-20261017.md)。真实质量、深层恢复与发布批准仍未完成。

[完整计划](single-capability-completion-plan.md)与[归属／迁移／删除清单](single-capability-framework-inventory.json)继续有效。真实质量、深层恢复／预算、其他后端、不可变发布／消费pin和在途保留／rollout分别验收。没有commit、push、发布或部署；现有部署pin未改变。

下面保留2026-10-16历史候选记录。其中“current／当前”均指当时的537a／939或其后明确命名的旧切片，不指本节的1b36／2db4。

## 历史候选记录（2026-10-16）

Updated 2026-10-16. Framework delivered; full objective and release acceptance remain open. Current authoritative handoff: [architecture / ownership / priorities](single-capability-framework-handoff.md), [current validation and artifacts](single-capability-framework-closeout.md), [full migration / deletion inventory](single-capability-framework-inventory.json).

Current exact wheels: Copilot `537a8afd40c80117f75a8a606957d69852c42ab9aa87076bb5c212ee779338ba`; ReviewBot `9399980b86403b3d83682ba06bce56da9512145ba1a13b127b9584e4f520e23b`. Current full paired patches and offline installation inputs are in `framework-closeout-20261016/paired-framework-review.tar.gz` under `/data/zhoutaichang/copilot/output/single-capability-completion-20261012`. Both source HEADs remain original baselines; they are not release commits. Older candidate archives and reports retain their original identities.

Current validation covers five passing installed main-flow groups, the retained raw policy failure and isolated passing recheck, SDK/resource integrity, Direct pending propagation and MoA no-fallback regressions. Previous EB67 full-source and remote namespace proof is historical; it is not rebound to the last two fixes. No full native quality, complete crash matrix, commit, release pin, publication or deployment is claimed. See [acceptance audit](single-capability-acceptance-audit.md) for retained failures/unknowns.

Offline install: extract `offline-inputs.tar.gz`; with fresh supported Python3.12, create a venv, install using `--no-index --find-links wheels --require-hashes -r requirements.lock`, run `pip check`, then the included `verify-release-sdk.py`. Preserve old execution environments and original identities; unresolved outcomes cannot authorize replacement dispatch.

后续 P1 handler 未决状态切片已完成源码和精确新 wheel 的定向验收：[共享结果补充](single-capability-shared-pending-outcome.md)。Direct／Strict／MoA 复用判断，Bot 保持 pending；原框架包与失败 gate 不变。更深恢复和超时子进程问题仍保留。

后续进程清理 P1 的两项稳定复现已修复并通过源码／新 wheel 定向验收：[结算补充](single-capability-process-exit-settlement.md)。原 policy 失败报告不改写，完整清理矩阵与真实 survivor 状态传播仍待验收。

真实 survivor 的运行期传播已通过源码／精确新 wheel 验证：[传播补充](single-capability-process-survivor-propagation.md)。待对账错误保留部分输出／身份，模型不 fallback，Direct／Strict 保持 interrupted；跨崩溃持久化和完整恢复仍开放。

清理诊断已复用既有 trace/attempt 持久保存，原门禁在新解释器中拒绝投影 pending：[诊断补充](single-capability-durable-cleanup-evidence.md)。inner worker 帧携带及完整恢复仍待验收；旧交付和原账本不变。

最新整合候选／worker 诊断连接见 [当前交付](single-capability-worker-cleanup-frames.md)：完整双仓补丁与精确 wheel，定向安装集成通过；完整源码另启，不覆盖旧的原始报告／failed gate。

当前3f27/B939 的双仓源码与七组安装集成均已终态完成，无失败／错误／源码测试漂移；源码三项环境 skips 有逐项同字节补验，原 strict no-skip gate 仍为 false。闭环见 `integration-worker-cleanup-closure-20261016/result.json`，不构成完整质量／恢复／发布验收。
