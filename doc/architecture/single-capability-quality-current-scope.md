# 当前质量验收范围 · 2026-10-17

当前框架交付为Copilot6a8e／Bot2db4；本轮新增模型调用为零，当前artifact的十层整例质量验收仍未完成。历史已通过的限定样本不能改名为当前版本通过。

当前十层证据核对 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-framework-pair-integration-20261017T085721Z/remaining-quality-acceptance.json`)绑定交付wheel、原证据文件及哈希，明确每层是否能证明当前版本。框架交付与当前全套确定性验证见[主计划](single-capability-completion-plan.md)；它们均不能替代真实质量。

以下保留原34aa阶段的实测范围与限制，全部按原artifact解释。其他后端按用户决定后验，原未知结果和499及此前分配不重置，也不自动派发新比较。

## 历史实测范围（原34aa阶段记录）

当前代码候选34aa／Bot3bbf，框架封存保持。下表保留每个实际执行的软件／输入范围，不把旧样本改名为当前版本验收。十层原证据哈希均已核验，无新模型调用或付费账本修改。

| 分层 | 已有真实证据及限制 | 下一项 |
|---|---|---|
| Direct | b7e6原两臂一个lease-store案例接受；旧失败保留。 | 补足所需语料／修复与重检，绑定最终artifact。 |
| Strict | e3eb候选一个案例满足标签；基线不当严重度和独立原unknown保留。 | 剩余语料／模式及当前artifact；新案例不能结算原unknown。 |
| Foundation | 7095原22笔complete、pending0，partial树有效但完整案例未通过，9375失败保留。 | 完整性／失败不变量缺口，不追加旧额度。 |
| PR-history | 499原两臂终态10complete0pending，候选来源已连接，但因TOY-I4遗漏回归检查要求被aggregate阻止；基线仅203最终导出，整例均失败。479等原失败保持。 | 内容完整性列后续P1；保留全部499分配，不自动追加同例付费运行或放宽aggregate。 |
| Screening | 原capped局部正例，候选后续额度边界，整例未通过。 | 新形状独立冻结，不追加原额度或窄化为部分成功。 |
| Maintenance | 两侧原生回复缺可信served identity，领域拒绝。 | 后端身份能力先核对；不能用requested、正文自报或合成事件代替。 |
| Lightweight depth | 一个旧冻结分层通过。 | 保留旧范围，补最终artifact及更广覆盖。 |
| Strict depth | 一个旧冻结分层通过，缺test witness保持unsure。 | 宿主oracle不能冒充模型执行测试。 |
| Discovery | Codex同family被canonical角色策略拒绝。 | 按用户决定后验其他后端，不豁免独立family。 |
| Module cards | 完整unlimited合同要求ZCode generator＋独立Codex judge。 | 其他后端阶段后验，不以capped／同family替代。 |

当前机器核对 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/current-paired-34aa-integration-20261017/remaining-acceptance-audit.json`)列原证据路径和hash、当前不被证明的项。当前完整整合进度见[34aa原执行](single-capability-current-paired-34aa-integration.md)，真实比较见[准备状态](single-capability-history-sources-comparison-preparation.md)。这些不替代深层恢复、预算／回执、其他vendor及最终release／pins／原环境保留／回滚／rollout验收。原质量历史记录按原版本解释，完整目标开放。

当前499原生终态详见[single-capability-history-sources-native-outcome.md](single-capability-history-sources-native-outcome.md)。整例失败不改变框架交付状态。
