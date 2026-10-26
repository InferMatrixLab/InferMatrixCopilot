# 最终配对验收：原始进程记录 · 2026-10-18

框架交付已关闭。本记录属于后续完整验收，不覆盖或替换已封存的框架归档。

最终配对：Copilot98554010／ReviewBot24ac7cfb。完整测试使用冻结树ec81d927／841e653e；CP套件显式使用冻结源码，Bot套件仅使用自身冻结源码和安装后9855 SDK。测试依赖复用；不属于fresh-install。未新增模型调用或改动预算。

原始ReviewBot完整进程3142674已terminal exit0：2925通过，0 failure／error／skip；使用安装后9855 SDK。原始Copilot进程3142657仍在运行，不声称通过。终态以各自process.json、JUnit和log为准。无论观察超时，都只续查原始PID／handle，不再次启动。

| 套件 | 原始PID | 工具handle | 权威记录 |
| --- | --- | --- | --- |
| Copilot | 3142657 | 84963 | process.json (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/direct-scoped-schema-compat-20261018/full-copilot/process.json`) |
| ReviewBot | 3142674 | 68636 | process.json (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/direct-scoped-schema-compat-20261018/full-reviewbot/process.json`) |

## 质量草案准备

新manifest (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/direct-evaluation-candidate-draft-v2-20261018/manifest.json`)绑定最终9855环境、Personal-Agent Docker镜像a0006918、当前评测helper和完整PR4870知识。候选Direct显式使用public ReviewRuntime和受信execution_config；原baseline保留原业务，通过声明的canonical scoped bridge transport运行。原Strict baseline的MCP描述由原SDK工作树地址／工具清单导出，未复制生产机制。

第一次草案构造未完成，旧基线没有新source_fingerprint模块；原半成品保留。v2只用原SDK已有导出收集基线metadata，构造已完成，4827项绑定。静态质量／phase／留存／4827项hash检查已完成：16个phase文件／38个phase通过，六个init原deadline保留；16个case／arm执行profile和canonical host binding检查通过。完整准入仍按原规则拒绝519＋新增182，没有创建live ledger。摘要见草案static-results.json与static-profile-results.json。额外检查最初误以为旧SDK工作树路径包含完整SHA；改为核对原SDK计算的精确路径后通过，未改业务路径。

无质量派发或live ledger。新182次仍只是未分配提案；保留原519、spare和unknown身份，完整准入应继续拒绝。原paid pending不因本草案换ID或重发。

[最终安装后主流程](single-capability-final-installed-mainflow-20261018.md)：114个不同选定用例闭环（首轮98通过、16缺fixture setup error；补齐冻结fixture后同16通过）。发布public SDK smoke与wheel RECORD核对通过。无产品变化／真实模型／pin更新。

## 最终wheel的真实namespace与资源补验

Personal-Agent独立安装精确9855／24ac wheels，2467个payload成员匹配；真实Linux namespace隔离、producer scope及wheel validators／schema资源5项全部pass、0skip。无产品源码导入路径，依赖复用；原本完整源套件环境skip若发生仍保留，不能抹除。证据：namespace归档 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-namespace-complements-20261018/namespace-review.tar.gz`)，SHA256 `de752e1a3e6f1dac5e2a7cf83f9a9180fa1519d319c073bd4d182f68b3267a23`。无真实模型调用、凭据或proxy挂载。

## 实际Direct入口的无模型控制（仍在运行）

控制目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-direct-preparation-control-20261018`)记录真实PR4870 source／host／knowledge，从public SDK预留进入Direct应用，经canonical结构化session准备SSH Docker策略和受限bridge，绑定预算钩子在模型派发前明确拒绝。首次混用config和legacy overrides在预留前被SDK拒绝，保留failure.json；修正为环境配置后原始PID3224322／handle55115运行中。不能因观察超时重启；此检查不代表typed completed review／真实质量。终态以result.json或resume-failure.json为准。

[实际Direct无模型控制已terminal通过](single-capability-final-direct-preparation-control-20261018.md)：最终SSH Docker配置／bridge准备完成，随后预算钩子按设计拒绝，工作流blocked／exit3、sent:false／pending:false，用时193.43s。原handle55115已exit0；无模型execution，不能算完成review或质量通过。

[全新venv安装验收已通过](single-capability-final-fresh-pair-20261018.md)：未安装MCP时minimal public SDK probe通过；之后114个不同主流程全部pass、release SDK smoke和pip check通过。无旧site-packages链接，最终双包2467个产品成员逐字节匹配。

[最终冻结源码统计](single-capability-final-source-metrics-20261018.md)已记录原upstream及pre-completion两套基准；净减行不是gate。

[实际Direct local准备及原attempt核对](single-capability-final-direct-local-control-20261018.md)已完成。原控制的错误postcondition使exit1记录保留；只读检查原取消attempt、空execution目录及sent:false／pending:false通过，没有重跑。实际local／SSH均走共享session准备并在模型前停止。

[最终离线认证／代理控制](single-capability-final-auth-proxy-controls-20261018.md)：local／SSH的completion admission与context核对通过；允许目标TLS／拒绝目标403通过，两临时proxy已关闭。不是session账号／服务端认证／模型质量通过。新增182次额度问题已提出，尚未答复时不派发。

## 最终完整终态

[最终整合报告](single-capability-final-paired-integration-20261018.md)：原CP3142657 exit0，6023 pass＋4环境skip；原Bot3142674 exit0，2925 pass。测试后产品源／安装成员再次匹配最终wheels。前文running状态仅表示当时观察，不再表示当前未结束。完整目标仍open。

[最终恢复合同证据](single-capability-recovery-contract-evidence.md)逐项绑定最终冻结测试Git blob与原完整JUnit：12个具体合同均有实际已通过参数实例；没有重跑／模型派发，仍不声称所有深层恢复组合或生产对账完成。

[离线host候选安装包](single-capability-final-offline-candidate-20261018.md)已完成：39个精确wheel与SHA256 lock，另一全新venv中--no-index／--require-hashes安装、pip check、public SDK smoke通过。不是published release，pin未更新，无模型调用。

[七条知识路径的最终确定性证据](single-capability-final-quality-policy-evidence-20261018.md)已核对冻结源码、原完整JUnit和具体参数实例；共享机制及各stage保留策略分列。没有追加测试或模型，不宣称完整真实质量／全部故障组合完成。

## 用户决定 · 2026-10-18

用户明确选择“暂不新增，保留为后续验收”。新增182次未获批准，累计701上限未生效；真实质量比较按用户决定延期，不再标为等待预算答复。原519、spare、paid unknown及原执行身份保持不变。框架与已交付确定性成果不重开，完整目标仍未完成；不重复请求额度，待用户明确重开该阶段。
