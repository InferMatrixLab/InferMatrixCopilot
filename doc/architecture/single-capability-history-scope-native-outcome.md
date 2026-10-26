# 710d四PR终态与实际裁定

原coordinator PID313288／exec84112已正常结束，两臂各5次调用全部complete，pending／新unknown为0。两侧应用均dry_run、aggregate approve；**完整冻结案例两侧均未通过**，实际只采用203模型规则。

| 合同 | 实际提案与应用 | 裁定 |
|---|---|---|
| 201普通VersionedMap | 两侧均选对core，写明revision冲突先于capacity、两类失败保持revision／values；同时引用实现和未路由的tests/test_revisions.py，整条被拒绝。 | 最终树缺失必要合同。不再是旧Engine被当model的拒绝；本次不能证明v1提示的因果改善。 |
| 202工具canonicalize | 两侧均选对tooling，保持排序副本、重复项、输入、distinct object和idempotence；同时引用未路由tests/test_records.py，整条被拒绝。 | 最终树缺失必要合同。 |
| 203 QuartzDecoder | 两侧均采用model:quartz-decoder，token-major两channel及回归／输入保持得到实现与测试支持。 | 这条满足；模型owner允许其提供的model scope，不能泛化成component测试证据也可越界。 |
| 204缺OpalEncoder页 | 两侧说明模型页缺失，最终无规则；候选另提core输入保持规则，被测试证据边界拒绝。 | 最终negative满足。候选额外提案及拒绝原因保留，不能说未知模型语义guard已独立证实。 |
| aggregate | 两侧正确绑定仅203已采用commit／rule的完整diff及最终base/head。 | approve不代表所有已选PR的正向合同覆盖。 |

已核验5427项冻结绑定、10份规范原生请求／回复归档、模型回复与应用记录多重集合、两侧source pin／tree／PR commits、实际Git完整diff与最终head。完成后resume零新增调用；校验前后原ledger逻辑行及trace／artifact字节保持。私有标签不在模型包，原标签未改。

问题是生成／验证的引用可用范围契约没有对齐：current_source提供tests/上下文，提取模型输出有事实支持的test citation，但component owner前缀不包含这些路径，现有canonical predicate拒绝。不能删除真实失败、靠放宽owner policy、宿主硬造rule或新的付费循环制造覆盖。下一P1应由同一owner规则产出明确citation eligibility供生成使用，校验继续调用同一canonical机制；保留context-only test witness、单模型限制和原batch／policy身份。更具体的rejection diagnostics可随同实现。

| 原生用量 | 候选 | 基线 |
|---|---:|---:|
| 输入token（含cache） | 31367 | 30622 |
| 输出token | 1662 | 999 |
| cache-read输入 | 17920 | 14336 |
| reasoning输出 | 776 | 382 |

两臂各4提取＋1aggregate，无语法修复／语义纠正调用。SDK记录的call elapsed合计候选162.917秒、基线262.563秒，包含legacy RPC和transport开销，不能当server推理速度或因果优势。10笔USD和served model均unknown，不能拿requested gpt-6-sol补齐。

旧439全部保留；新20使用10、余10保留，累计459。未复用闲置额度、未重派旧unknown，框架及710d确定性整合保持原范围。完整十层、深层恢复、其他vendor和最终发布验收仍开放，无commit／push／发布／部署。

终态、实际内容与审阅包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-history-scope-20261017`)包含terminal-audit、native-retention-audit、adjudication、model-evidence、timing-and-corrections和原日志；outcome-artifact.json列校验。原准入包保持准入时范围，不能代替此结果。
