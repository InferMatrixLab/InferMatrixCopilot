# History v4实际结果：原生调用完成，整例未通过

本次原manifest`eff28175d94fd002f7de6b306a2ffab82601f375fa032f21266454f004a65072`的两臂共10次native调用全部complete、模型unknown0；原request／reply和native attempt archives均由canonical TraceStore核验，未重派模型。候选6a8e／History scope4与原647、四PR source pin420904和原private标签保持。完整案例两臂均未通过，519全部分配含闲置保留。

## 实际内容与最终导出

候选确实准备了201／202／203三个正确owner规则，并明确保留原202缺失的回归检查义务；204正确不越owner采纳。但完整aggregate返回request_changes，应用blocked，最终导出为空：它要求201／202的测试上下文证明，并指出新VersionedMap规则加入Direct表格后，lead仍只有engine／tick触发词。

基线aggregate批准，但最终只导出203的QuartzDecoder规则，缺两个必需正例，故也不通过。候选prepared内容正确不能抵消最终未导出；不改变原标签／聚合门槛，不用人工结论强行批准。

原始实际model/application证据 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-history-obligations-20261017/model-evidence.json`)／逐条人工裁定 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-history-obligations-20261017/adjudication.json`)／native原始留存核验 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-history-obligations-20261017/native-retention-audit.json`)／完整结果包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-history-obligations-20261017/history-obligations-native-outcome-review.tar.gz`)。

此为source作者知晓标签的既有合成案例回归，不是独立／盲测、因果或统计证据。served model和USD仍缺失，按每个原receipt保留unknown，未用requested／估计值填补。完成后零模型resume仅基线实际执行；候选blocked不声称完成后resume。

## 协调器丢失与原worker结果，分别报告

最初PID3432217因评测sibling-module导入在main前失败，模型派发0；证明和原错误保留。更正启动包装后，原PID3439682首次进入模型流程。随后tool session75898返回143，原coordinator消失且未写终态记录；不能据此推断其具体exit code或原因。

原baseline worker PID3462030／3462040当时仍存活，继续同一manifest／guard config／账本；从未启动替代worker或coordinator。它后来结束，原内部native-workflow/phase-process.json持久记录exit0、模型5次complete，最终应用结果及基线零调用resume均保留。当前所有原PID均不存在，10份原native receipts和两份内部phase收据均可核验。

**外层stage ledger的baseline仍为running／returncode null，完成ack缺失。** 未伪造stage-process.json、未修改旧行，也未把内部exit0冒充外层coordinator成功。逻辑结果与模型回执完成不等于外层业务ack已结算；这是独立P1恢复缺口。原进程／收据观察 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-history-obligations-20261017/coordinator-observation-result.json`)绑定实际原记录，最后只读SQLite backups随包保留。旧Strict／Discovery unknown与本次无关，未查询或替代。

## 已定位的后续实现工作

1. **聚合证据传递**：原提取packet给201／202提供了tests/test_revisions.py／tests/test_records.py上下文，component_evidence_owners为空；aggregate只携带已被规则引用的owned证据，未携带这些context-only tests。源码_review载荷和原packet均证实此差异。下一步复用canonical pinned-source读者，将审查上下文与可引用证据分开传递，保留owner限制／容量／输入身份及旧版本合同。
2. **Direct入口更新**：共享quick-map renderer用route signals生成lead，新增规则表行后未反映VersionedMap。核对导航合同并集中修正canonical renderer，保留旧append-only／批次字节行为；不加一个模型生成或宿主repair循环。
3. **外层完成ack恢复**：保留原stage pending，补可验证的durable完成收据及原结果对账机制；不能从PID消失推断成功，也不能重新执行付费工作。

框架交付保持封存，未改生产代码、未发布或部署。其他九层／独立标签与家族、精细预算／回执、其他vendor及最终release／在途保留／rollout仍开放。本次20个新分配使用10，余10保留，旧499全部保留，不自动复用闲置或追加同例模型运行。完整目标未完成。
