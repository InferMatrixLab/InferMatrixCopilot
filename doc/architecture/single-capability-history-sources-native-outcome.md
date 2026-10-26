# 34aa History持久来源回归：实际终态

原coordinator PID1458811／exec19817已终态0，两侧共10次原生调用complete、pending／unknown0。manifest 7f76ec3feadea3ec474ce6da52bc09a2332b70a65e35489eae9e2d1a59a085fa 的5659绑定、原四PR标签、原生archive及请求／回复留存均核验不变。进程0不等于业务成功。

| 实际范围 | Candidate34aa | Baseline647 |
|---|---|---|
| 原生路径 | 四提取＋一次aggregate；无额外语义修复 | 四提取＋一次aggregate |
| 提案与应用 | 201／202／203各owner提案进入prepared；204缺模型页明确不采用 | 201／202提案含未归属component的tests引用，被原应用拒绝；203进入prepared；204不采用 |
| 聚合与最终树 | request_changes：TOY-I4缺保留test_records回归检查要求；blocked，没有最终导出树 | approve／dry_run，只最终导出203 |
| 完整案例 | 未通过；prepared不能算最终采用 | 未通过；缺201／202 |
| 完成后resume | 未执行完成后resume，不能写成零调用通过 | 原报告确认完成后resume零新增调用 |

候选201包含revision-before-capacity及失败不改values／revision的具体验收；202包含sorted distinct copy、duplicates、原输入不变与idempotent repeat，但没有明确保留回归检查的要求；203保持模型归属、token-major两通道和回归检查。候选quick map已把TOY-I3连到pkg/revisions.py，持久source row写入实际prepared规则，不能从这项机械修复推导完整内容质量。aggregate本次正确阻止了遗漏内容的导出，不能绕过或给拒绝加宿主repair循环。

最终正例导出为candidate0／baseline1，是本冻结案例的输出回退；两侧完整案例均失败，不能宣称整体提升或无回归。原标签由已知源码作者冻结，不是独立／盲测语料、提示消融或统计质量结论。原479／459等失败不改名或复用。

每臂实际5调用，留存的20新增上限使用10／闲置10；全部旧479含闲置继续保留，累计499。没有释放旧余量、回放原unknown或部署。两侧实际served model和USD均未返回，保持unknown；requested角色不代填。token accounting及含cache关系详见adjudication.json，耗时不作为模型服务速度或因果比较。

原执行、语义裁定及留存审计 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-history-sources-20261017`)保留真实请求／回复、application record、完整Git diff/base/head、所有原调用及校验结果。结果包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-history-sources-20261017/history-sources-outcome-review.tar.gz`)封存上述报告与对应身份；不触碰原账本或原生archive。

后续P1是生成内容完整性及各层质量验收，按阶段推进，不因本例失败重开框架、自动追加调用或放宽owner／aggregate政策。[确定性整合](single-capability-current-paired-34aa-integration.md)与[恢复合同证据](single-capability-recovery-contract-evidence.md)各自保持范围。十层质量、深层恢复、其他后端与最终release／pins／在途保留／回滚／发布仍开放，完整目标未完成。
