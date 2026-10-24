# Foundation测试源码与执行provenance分离 · 2026-10-17

框架阶段保持已交付；这是独立P1输入修复，不重开框架或改写旧实验。

已封存的9375 Foundation原生样本中，生成器把test_context中的“tests have not been executed”写入validation知识，并只引用测试源码。原judge正确标为unsure并排除。共享生产者其实只做源码定位，没有观察任何执行，不能声明测试未执行。

related_test_sources现在只标识“direct import/symbol localization; source text only, no execution outcome evidence”。owner／feature、capped／subscription继续使用同一生产者；源码、行区间、优先级、字节预算和历史SYSTEM v3／v4／v5字符串不变。没有通过正文关键词删句来假装质量通过，也不放宽judge验收。

输入选项foundation_test_evidence_version2对两种计费模式都绑定新语义。旧请求／回执不迁写；模拟旧无版本及版本1的unknown任务在新派发前拒绝，检查点字节不变，保留原环境／身份对账。历史模板兼容不等于旧未决任务可以用新环境重派。


Final verification: source initial 104 cases retained 5 old-wording assertion failures (all five expected the removed nonexecution sentence). Corrected related-test/owner/feature and both simulated prior-version unknown checks: 12 pass. Full relevant normal-installed selection 105 pass, zero failures/errors/skips. Includes independent native generator/judge trace binding with controlled transports, unchanged historical system strings and bounded/offered evidence context. Both products payload bytes checked against exact wheels, no product-source injection; dependencies reused, not fresh minimal.

Wheel 59a55d929e5e64aaa4a6ad64a4db8dea1b33cc132f62650f93976a8fde15a63c; baseline10775; Bot3bbf unchanged. Two production files +1lines/-9AST nodes; all other wheel payload bytes unchanged. changes.patch applies once to the10775 checkout; isolated private Git-index reconstruction verified. New test assertions replace old metadata expectations and extend unknown refusal to explicit version1. No old request, paid ledger, native sample, judge verdict or budget allocation was edited. No new native/model calls, commit/push/pins/deployment.

代码、wheel、原始报告及重建证明 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/foundation-test-provenance-20261017/README.md`)；精确验证 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/foundation-test-provenance-20261017/result.json`)。

剩余P1：生成策略仍需进一步区分可复用源码事实与阶段运行元信息，完整案例的顺序／拒写／记账解释缺口、真实质量比较及深层恢复仍开放。此修复消除一个无依据输入事实，不证明模型不再生成provenance，也不把旧失败算作新通过；不原样重跑旧样本，不重放unknown。其他后端与最终配对／发布验收继续按完整计划分阶段推进。
