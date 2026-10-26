# 独立跨文档 screening 比较（2026-10-16）

两侧均已实际生成跨文档候选，并有一项获得原生审查 pass；完整配对质量验收仍未通过。框架交付保持收口，未追加额度或重放任何旧样本。

本次沿用 [准备记录](single-capability-screening-crossdoc-preparation.md) 的独立源树、冻结私有标签及 canonical Skeleton 入口。两侧使用相同模型／effort、当前6629执行机制及冻结 provider／沙箱配置，分别加载原始与当前应用包。标签没有在看到结果后修改，宿主判断为非盲评，不替代原生审查结果。

| 结果 | Candidate | Baseline |
| --- | --- | --- |
| 原生派发／完成 | 4次，均有完成回执 | 4次，均有完成回执 |
| 生成规则 | 3项 | 3项 |
| 已审查结果 | 第一项 pass，第二项 unsure | 前两项 pass |
| 第三项 | 冻结额度在审查前耗尽，阶段 blocked | 同样额度拒绝，被旧宿主包装为不可用，保留 unjudged，阶段 dry_run |
| 完整产物 | 未完成，无 tree | 有 tree；第三项没有质量批准，完成后 resume 未增加调用 |

第一项规则将相同 key／cost 重试与活动 row 容量、quota、sequence 关联，符合冻结的跨文档正例机会。第二项 candidate 审查保留了“无 ticket”与“无 active ticket”的证据差别；baseline 的另一份采样回复获得 pass。不能由单个采样差异推导实现回归，也不能覆盖 unsure。两侧第三项均没有原生质量批准。

原 coordinator 已终态退出0，其 executed／stage complete 表示子进程及原生回执核验结束，不表示应用或质量通过。Candidate 的 blocked 使完整配对质量门禁保持 false。

## 预算与待诊断差异

权威调用账本和十个 attempt 档案一致：八次实际 sent／completed，另外两次明确 sent=False、completed=False、pending=False，错误为 frozen case native call ceiling exhausted。本次没有新增未知原生结果。此前未知 Strict／Discovery 与其环境／账本没有被修改或结算。

保留此前197次额度，加本次8次，总保守分配205，低于原206次上限。没有释放旧额度；次数不是货币，原生 USD cost 缺失保持 unknown。

需要先做零模型诊断的两项：

1. 额度拒绝的类型投影不同。当前 native account 的 Refused 携带 call_budget_failure，核心保留域异常；旧 provider host 将异常包装后，旧应用视为 ModelUnavailable。需用记录回复及已证实未派发的拒绝复现，明确是评估组合差异还是需要改变领域策略。不能为了配对通过而放宽未知结果的拒绝或批量捕获所有错误。
2. 冻结 helper 的 native_calls 实际是 gateway records 数，每侧5，包含一次未派发拒绝。实际原生派发每侧4。后续修正维护中的报告术语及测试，原报告保持原样，不把十条 gateway 记录当成十次付费派发。

继续真实质量工作前需要冻结足够的调用分配与新 manifest；不改本次 profile、额度、原始运行或标签，不重放本次已完成调用。

原始证据：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-screening-crossdoc-20261016/adjudication.json`。可审阅证据包 `screening-crossdoc-review.tar.gz` SHA256 `079d1fdd80256d3e7faa1e8f7734b020dbed36f3e0619d9f17ed6dcd8260ab87`，113个成员逐项校验。包含冻结配置、应用报告、模型 JSON 回复、账本／档案摘要及生成脚本；私有标签仅供审阅，不能作为 worker mount。没有复制账户缓存或 auth-probe 目录。

完整十层质量比较、恢复／后端矩阵及发布验收继续开放，未提交、发布或部署。

后续零模型诊断确认：相同已知 ModelUnavailable 在两侧均保持 advisory，当前对未知结果继续拒绝；不能因此放宽生产规则。维护中的报告计数已修正并有独立验收，见 [最终候选验收](single-capability-final-candidate-validation.md) 与 [尝试计数归一](single-capability-eval-attempt-reporting.md)。本次冻结原始结果不改写。

准入时引用的安装包 partial 随原验证进程更新为完整结果；现已从终态超集恢复其精确历史字节，SHA256 与不可变准入中的原 hash 完全一致。旁证 `crossdoc-admission-input-proof-20261016/proof.json` 和 `installed-partial-at-admission.json` 保存在同输出根下，原准入和原验证结果均未修改。
