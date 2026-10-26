# 最终候选确定性整合验收（2026-10-16）

当前6629/B939配对候选的确定性整合已完成：完整双仓源码、七组安装包、基础消费依赖以及当前候选的实际 namespace／便携资源补验通过。框架交付保持收口，完整真实质量、恢复／后端矩阵及发布验收继续开放。

后续评估计数修正已交付：[尝试计数归一](single-capability-eval-attempt-reporting.md)。仅评估 helper／测试／格式说明变化，打包输入不变；新 helper 有独立定向验收，旧完整整合报告保留原评估快照身份。

完整十层质量的当前证据、缺口及下一轮条件见 [质量范围索引](single-capability-quality-readiness-index.md)。它包含实际 maintenance 回执的身份元数据核对，不批准新调用或降低原政策。

Copilot wheel：`6629eb96da1c10ed9fb8fe86938685722b85923fded26930398f8c808757f9af`。ReviewBot wheel：`9399980b86403b3d83682ba06bce56da9512145ba1a13b127b9584e4f520e23b`。本切片未改变产品代码、wheel 或原始运行身份。

| 验收范围 | 终态证据及限制 |
| --- | --- |
| 双仓完整源码 | Copilot6179项、ReviewBot2783项，失败／错误均为零，pytest exit_code=0，源码和测试无漂移。 |
| 三个源码环境 skips | 本机 namespace 不可用及两项安装资源测试；同候选、同测试源字节在 Personal-Agent 实际环境通过。原始 no-skip gate=false、驱动 exit1 及原 XML 保留。 |
| 七组安装包 | main、foundation、depth、recovery、ownership、policy、evaluation 全部终态通过，无失败／错误／跳过，产品从 site-packages 加载。 |
| 安装身份 | pip 来源、wheel RECORD、全部2433个包资源内容逐项核验；全部组完成后再次核对安装包字节通过。 |
| 基础消费依赖 | 新环境只安装真实消费依赖，未安装 MCP／dev extras；离线解析后按 hash 锁安装。公开 SDK 正常加载，非法 schema 在创建执行目录和派发前被拒绝，pip check 通过。 |
| 实际 namespace／便携资源 | Personal-Agent 的隔离、producer scope、便携校验器全部通过，无源码路径注入；不冒充其他后端或完整 native review 的验收。 |

模型／GitHub 在上述整合套件中是受控边界，没有实际发布。整合核验脚本还核对最终冻结源码、每组原始报告 hash、跳过项的同名／同源字节，以及候选 wheel 身份，见 `final6629-validation-closure-20261016/result.json`。

## 审阅与复现

确定性整合证据包：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final6629-validation-closure-20261016/final6629-deterministic-integration-review.tar.gz`。SHA256 `b98705827c0f25cb0112dad0d5d9548f9f5b27ea33874fabc993a677b9b760ef`，49个成员逐项校验。包含原始源码与安装包报告、补验证据、基础依赖锁和生成／核验脚本。

配对代码仍为 [框架正式交付](single-capability-framework-stage-acceptance.md) 所指的 `framework-stage-review.tar.gz`：完整双仓补丁、最后 overlay、精确 wheels 和离线依赖。无需为本次证据重建或更换产品包。原交付创建时的验证限制不改写，本报告是同一候选随后完成的整合证据。

首次安装来源检查失败保留在 `final6629-installed-20261016/main.xml`：按包名／hash锁安装没有生成 direct_url.json。新建独立环境 `final6629-installed-v2-20261016/env`，以相同依赖锁安装，再由 pip 显式安装同一 wheel 文件，正常生成来源元数据。原检查和全部七组随后通过；没有手写元数据或删除检查。

Namespace 输入 manifest 的 scope 句子沿用了旧2d27模板；实际 wheel_sha256、全部安装内容核验和结果均绑定6629/B939。`final6629-namespace-20261016/manifest-scope-clarification.json` 明确列出原 manifest hash 与实际身份，原文件及封存包不改写。

## 实际质量与零模型诊断

[独立 screening 比较](single-capability-screening-crossdoc-adjudication.md) 已实际完成八次原生调用，两侧都生成了支持的跨文档候选并得到 pass；candidate 最终因冻结额度耗尽而 blocked，完整质量配对仍不通过。两个额外 gateway 尝试有明确未派发档案，不计为实际原生调用。保守额度205／206保留，旧失败及未知调用均未重放。

随后用同四份已记录的回复，在全新 fixture 中分别注入三种第五次 gateway 错误，零模型诊断结果如下：

| 相同错误契约 | 当前应用 | 原始应用 |
| --- | --- | --- |
| 原始 account Refused，明确未派发 | blocked | 直接抛出 Refused，进程 exit1，保留原失败 |
| ModelUnavailable，明确未派发 | dry_run，第三项 unjudged | 相同 dry_run／verdicts |
| ModelUnavailable，模拟未知原生结果 | blocked，保留未决 | dry_run，第三项 unjudged |

这证明共享机制保留了已知不可用时的 advisory 策略，并且当前实现对未知结果的拒绝更严格。真实 capped 比较的差异涉及 legacy gateway 的错误契约投影，不能据此放宽当前拒绝或修改生产审查规则。诊断不是新的原生质量、archive 恢复或原未知执行的结算证据。

诊断的原 orchestrator 因预期所有子进程 exit0 而 exit1；实际 baseline 原始 Refused 未被捕获，原始失败与堆栈均保留，未重跑以抹平结果。解释和证据在 `screening-refusal-replay-20261016/interpretation.json`，审阅包 `refusal-replay-review.tar.gz` SHA256 `0e6d5c42c7c83e2bec581a1c4047e635aea0fe1492db583cc90d72359772be53`。原调用账本、attempt 元数据和实际应用报告逐项核对未改变，本诊断 native dispatch 为零。

## 后续工作

1. 评估术语修正已完成，见上方补充；后续评估使用版本2格式，冻结原报告及账本保留原样，实际派发数继续由账本／原生事实证明。
2. 后续真实质量测量须覆盖实际候选上界及修复额度，重新冻结预算和输入；不能删减候选、改标签或追加本次额度来获得通过。累计预算从当前205开始，不从旧197重新计算。[调用分配边界](single-capability-quality-allowance-bound.md)已用两侧安装应用核验该无 seed／fallback Skeleton 的14次／侧路径，实际模型0；不是新准入，其他 strata 仍需单独核算。
3. 继续 Direct／Strict、各 knowledge 路径的实际质量、完整恢复／预算／并发及发布回执矩阵。其他后端按用户决定后验，原 Strict／Discovery 未知结果仍需原身份对账。
4. 完成实际代码审阅、配对 release／pin、回滚和旧环境保留／排空及部署验收。当前未 commit、push、发布或部署，完整目标未标记完成。
