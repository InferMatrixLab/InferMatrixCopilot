# 7095 Foundation原生比较终态 · 2026-10-17

原session75210／PID3346472已正常结束，两臂各11笔原生调用均complete，新增pending／unknown为0。两臂应用都产出有效dry-run树并完成无新增派发的resume，但**完整冻结案例均未通过宿主质量验收**。框架保持交付，完整目标继续分阶段完成。

原manifest `b3a02b1ddc57335c8c5b77724ae00c3ef3b11920cda0b5d74ba4e746e629c3d9`、5215项绑定未变；原生回复与应用记录多重集一致，接受标记与原verdict对应、私有标签未进入模型包，阶段均exit0。协调器成功及judge计数不是质量通过。

| 源码正向合同 | 候选7095实际接受内容 | 原始647实际接受内容 |
| --- | --- | --- |
| 旧revision优先于容量验证 | configuration明确旧版本／相同写入在容量检查前返回False，容量错误不改变条目／计数，满足本条 | 描述revision检查和容量失败，但缺旧版本＋超大payload组合的明确优先关系。 |
| 同revision相同写入／冲突及失败不变性 | 相同写入及容量拒绝不变性有描述；冲突字节拒绝与状态保持未进入接受树，仍不完整 | tradeoffs描述同revision冲突拒绝，但缺相同写入语义及失败后条目／记账保持，仍不完整。 |
| 脱离快照与当前记账 | architecture／features／validation连接旧快照payload_size与当前stats，满足本条 | 有不可变旧快照及计数概述，但旧观察／当前记账关系解释不足。 |
| 观察revision用于删除及格式化边界 | 说明观察revision委托与避免删除新替换的目标，保留owner-local未提供drop实现的 caveat | API／features结合guide解释compare-and-delete，同样保留owner-local caveat。 |

候选validation描述测试源码中的断言，并说明该源码没有提供执行结果；没有虚构测试实际运行／未运行。它仍是输入范围说明，不能当作独立测试执行证明。候选core API因遗漏容量异常被原judge拒绝，tooling API因来源不足以证明bytes单位被拒绝，均未计入接受覆盖。原verdict不修改。

候选原judge为7pass／2fail，基线5pass／2fail／2unsure，两侧各省略3facet；这些计数只描述应用行为，不替代上述内容判断。候选存在相关测试输入并启用generation policy1，原始基线没有这些输入／策略；同一已知合成语料、各一次运行且宿主未盲，不作因果、统计或一般质量提升结论。capped结果不认证unlimited／独立family纠正流程。

| 原生报告用量 | 候选 | 基线 |
| --- | --- | --- |
| 输入token（已含cache） | 63331 | 62150 |
| 输出token | 4692 | 4812 |
| cache-read输入token | 39424 | 39424 |
| reasoning输出token | 1672 | 2043 |

所有22笔served model及实际USD继续unknown，不用requested model或固定领域预算费用填充。独立CLI调用数为22，不代表厂商内部turn或货币账单。保留全部旧399分配及本轮28分配，本轮用22、余6不释放／不复用，保守累计427；旧Discovery／Strict unknown及环境不动。

原终态审计 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-foundation-policy-20261017/terminal-audit.json`)、宿主内容裁定 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-foundation-policy-20261017/adjudication.json`)和model-evidence.json保留实际输入、输出与接受树。终态审阅包及逐文件校验见同目录outcome-artifact.json。

本轮不再围绕该样本重派Foundation或改标签；完整性缺口登记后续质量阶段，不重开框架。接下来为已准备的PR-history单独冻结artifact／profile／留存／上限及正式准入。其他阶段、原unknown原位对账、深层恢复、其他vendor、最终精确配对与immutable release／rollout仍开放；没有commit、push、发布或部署。
