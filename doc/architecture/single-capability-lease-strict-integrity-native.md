# 当前292e独立Strict质量比较

已冻结并准入独立lease-store Strict协议，manifest `1f1434ded8a83a7946599f81c8f070979eefe88bff28a479f44304ee81c71a04`；保留此前279次保守额度，新增44（每arm22），revision lease-strict-integrity-20261017，总上限323。单位为provider调用，不是USD或vendor内部turn数；22不是全图最坏调用量，保留完整round／repair／verification。

当前292e／3bbf、image749／releasec661、native628e与gpt-6-sol/high请求均绑定。baseline完整driver控制6次、额度2负控5项agent失败而旧app仍done，98项冻结辅助检查、实际账户metadata／bridge／proxy控制通过（均零模型）。不把done或就绪当作质量批准，actual served model及USD缺失仍保留未知。

原协调器handle94110运行，candidate原run `run-20261017-041445-627c4b`。已观察首个native_complete回执及下一预留，仍需原结果／冻结标签判定；无质量通过结论。原失败lease调用、未知booking Strict／Discovery及旧额度均不重置，不发布。所有10分层保留，inactive evidence不重新绑定到新候选。

准入包 `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-lease-strict-integrity-20261017/lease-strict-integrity-admission-review.tar.gz`，SHA256 `c8e9709db6a683934f797579e865c28e04a337aab65e6473ee443076c5a4b3b3`，98成员逐项校验；不导出活跃payload／请求／回复缓存、账本或凭据。只能观察／对账原执行，不能重复first dispatch。完整目标及模型质量尚未完成。


## 原执行终态与冻结质量判定

两侧原run均done，各7个native_complete，共14；新预留／未知为0，原账本逻辑行与输入bindings不变。两侧都找到store.py:18新缺陷，原始recheck都正确标fixed，但两侧均将该步设needs_review，既定安全门按ESCALATE拒收，因此typed rechecks_complete=false，冻结质量不通过。不能凭native或app done认定quality通过；不是映射丢字段，也不放宽接受条件。双方blocker级别缺少本toy有界语境中关键生产影响的支持；candidate摘要另有line17/正确inline18的引用不一致。

同请求gpt-6-sol/high、同CLI／image／provider；actual served model未可观测，USD未知，不用请求model或0替代。候选input648828/output18254，baseline input663073/output17642，input含cache；SDK墙时各约462s/507s只是单次观察，不作成本／延迟因果比较。当前44额度实际14，unused30不释放，保守总323仍保留；原未知及旧失败不重派发。

终态归档 `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-lease-strict-integrity-20261017/lease-strict-integrity-outcome-review.tar.gz`，SHA256 `8016630c5cfe17336985624b80d4d72e580408476531750ff2b28760fdbca6b5`，27成员校验。下一步只改共享任务状态指引，明确完成assigned task与对象/PR verdict不同，原fail-closed门保留；严重性／引用质量继续登记，不改写这两个失败样本。完整目标与十层质量仍未完成。
