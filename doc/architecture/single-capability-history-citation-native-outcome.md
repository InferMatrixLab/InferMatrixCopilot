# fd8b引用契约真实回归终态

原coordinator PID853253／exec62736正常结束，两臂各5次调用全部complete，新pending／unknown为0。10份规范原生请求／回复留存验证通过，5543项冻结绑定及实际应用回复多重集合、完整Git diff/base/head、source pin／PR heads均核验。模型阶段退出0只表示测量完成；**完整冻结案例两侧均未通过**。

| 合同 | 候选实际结果 | 基线实际结果 |
|---|---|---|
| 201 VersionedMap | core owner、仅引用pkg/revisions.py；revision先于capacity及两种失败不变的完整规则进入prepared批次。 | 正确提案同时引用未归属tests/test_revisions.py，原校验拒绝，最终缺失。 |
| 202 canonicalize | tooling owner、仅引用实现；排序独立副本、duplicates、input、distinct及idempotence完整规则进入prepared批次。 | 正确提案引用未归属tests/test_records.py，原校验拒绝，最终缺失。 |
| 203 QuartzDecoder | 正确模型owner、token-major两channel及shape回归，进入prepared批次。 | 同类规则得到aggregate approve并导出。 |
| 204缺模型页 | 不提规则，明确解释OpalEncoder页缺失。 | 同样no_upgrade并解释。 |
| aggregate／最终结果 | request_changes → blocked，无最终可导出知识树。 | approve → dry_run，仅导出203；完成后resume零调用。 |

候选prepared覆盖了三个目标合同，但共享quick-map渲染器只扫描正文反引号；201的已校验来源只在record.evidence中，渲染成`TOY-I3 | —`。aggregate因此请求补充源码发现入口，同时提出旧engine／tick触发词问题。实际文件离线重现并在同一pin重新校验pkg/revisions.py证据hash，原回复／结果未改。表中原有trigger已经包含VersionedMap，因此触发词建议仍需按路由合同判断，不能据模型一句话就改变owner routing。

这是生成／验证引用契约对齐后暴露的下游数据连接缺口。不能把prepared规则当最终采用，也不能以更多中间覆盖宣称整体改善或无回归：当前候选最终导出0，基线导出1，候选被质量闸正确阻止，整例双方均失败。blocked候选没有执行“完成后resume”，该项记未验证；基线dry_run的零调用恢复已验证。

下一P1：把已验证的逐规则来源接入唯一quick-map渲染机制，覆盖各init调用方；保持手写map不改、容量／spill一致、来源可耐久保存及旧批次／策略身份保护。先用本次实际文件做确定性回归，不放宽owner／source验证、不加宿主生成或repair循环、不豁免aggregate、不更改原标签或把旧闲置当新额度。框架交付及fd8b确定性整合保持各自原范围，完整目标继续开放。

原生用量：候选输入34126（含cache14336）、输出1576、reasoning833；基线输入30640（含cache17920）、输出1038、reasoning375。cache是输入子集，不能重复相加。实际served model与USD全部unknown，不以requested填充。应用记录耗时包含transport和legacy RPC，不是server推理速度或因果性能结论。两侧各4提取＋1aggregate，无输出修复或语义纠正调用。

全部旧459分配保留；新增20使用10、闲置10保留，累计上限479。原unknown不重派、原环境和旧失败不改。其他九层、独立标签、深层恢复、其他vendor及最终release／pins／在途保留／回滚／rollout仍未完成。无commit、push、发布或部署。

实际内容、终态审计和审阅包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-history-citation-20261017`)保留adjudication.json、model-evidence.json、原始native留存核验、quick-map-reproduction、原coordinator与manifest。原准入包只表示准入时范围，不代替本次失败裁定。
