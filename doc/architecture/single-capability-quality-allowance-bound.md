# 后续 screening 调用分配边界（2026-10-16）

已核对实际安装的原始／当前应用，而不是从一次单规则 fixture 推算配额。该结果仅为后续质量准备；没有新 native 准入、额度变更或模型调用。

## 适用条件与调用上界

仅适用于当前无 seed、无 generator fallback、文档非空的 Skeleton 形状：一次 map generation、一次 doc-rule generation，`MAX_RULES_PER_CALL=12` 限制候选数量，每个通过确定性前置检查的候选调用一次 advisory judge。该路径没有自动 generation／schema repair；generator fallback 必须显式为空。当前执行器只自动重试 RETRYABLE，而 init phase 的相关失败返回 BLOCKED，不放大该调用数。

因此该形状应按每侧14次、成对28次 provider invocation 准备完整分配。完成后的 dry-run resume 不增加调用。这个上界不适用于有 seed、fallback、其他 init 阶段或外部显式重试；也不等于 CLI 内部网络请求次数或美元开销。

## 两侧实际应用控制

使用已安装原始／当前应用和同一 public fixture，脚本化 transport 提供13条结构上可进入筛选的合成规则；现有应用截取12条。所有 fake judge 均明确拒绝，不把合成内容当作支持的知识或真实质量样本。

| 两侧各自结果 | 观测 |
| --- | --- |
| Generation | 2次脚本化 provider invocation |
| Judge | 12次脚本化 provider invocation |
| 第13条候选 | 被现有应用上限截断，未进入 judge |
| 最终状态及 resume | dry_run；完成后 resume 新增0次 invocation |
| 实际模型／native service | 0次，未调用 vendor CLI／服务 |

同时核对了安装版本的 `_to_candidates`、Skeleton `_build`／`_screen`、`generate`、`review_call`、ModelGateway fallback 和当前 executor 的 retry 分支。控制和源文件 hash 保存在输出中；这不是更广恢复矩阵、真实 native latency、模型质量或新 fixture 的验收。

## 当前预算边界

本次读取的最新不可变准入保留累计205次额度，现有协议上限仍为206。若后续独立案例采用上述成对28次分配，累计将为233，比当前上限高27。没有释放旧阶段未用额度，也没有把未知调用当作未花费。

现有 `plan_call_limit` 对未批准的233上限实际返回拒绝；没有修改协议 whitelist、创建调用账本或 live-admission。后续必须先完成新案例／标签、所有前置能力和 deadline 核验，并显式冻结足够的分配及新 manifest，不能修改已完成的 capped 样本来获得通过。其他 strata 的 correction／generation 上限还需分别核算；28不是整个剩余验收的总额。

证据：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/screening-allowance-bound-20261016/result.json`。审阅包 `screening-allowance-bound-review.tar.gz` SHA256 `ad9f59df8b6ee8545a5d58bb7285e6dad1c9efe664b02e926bd754ff07e2bf4e`，11个成员逐项验证，包含两侧结果、日志、脚本和假设。产品包、现有原生回执及预算状态未修改，原完整质量不通过的结论保留。

同轮恢复路径审计没有发现需要立即改动的新缺口：foundation 的 error 保留判定／settle 抛出和 discovery 的非预期异常会由共享 journal 恢复原 intent；未据此宣称全部恢复要求完成。完整恢复、实际质量、其他后端及发布验收仍按原计划开放。
