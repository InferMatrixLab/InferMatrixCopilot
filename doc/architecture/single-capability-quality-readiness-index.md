# 完整质量范围与下一轮准入条件（2026-10-16）

已将原计划十个分层逐项对照保留证据及其 hash，没有从验收中删除失败或延期项。本索引不是新执行 manifest，不批准模型调用，也不将历史结果改名为当前候选通过。

| 分层 | 已有证据 | 下一轮必须解决 |
| --- | --- | --- |
| Direct | 保留原始结果及 candidate disposition 失败 | 独立冻结 PR／snapshot／labels，核算初始、repair、recheck 全路径；不重放原失败调用。 |
| Strict | 保留原 baseline unknown 与旧 candidate 派发前失败 | 原身份对账保留；另行准备不同的比较，不能用新结果替代原未知执行。 |
| Foundation | 保留 native generation 的覆盖缺口及单独 correction 控制 | 完整 generation、bounded correction、覆盖、accepted peers 与 resume；scripted generation 不算原生生成质量。 |
| PR-history | 保留拒绝的 aggregate／quick-map 样本 | 精确 base/head、原始 source、完整 aggregate、显式私有 payload 留存及正确产物，重新冻结后才可准入。 |
| Screening | 当前两侧有正例 native pass，candidate 最终因额度拒绝 blocked | 无 seed／fallback 形状按14次／侧准备；新案例和预算另冻，不修改原 capped 样本。 |
| Maintenance | 两侧原生调用均完成，但 observed served identity 缺失，领域拒绝 | 先有可信的所选 native contract/profile 身份报告支持证据，不能用 requested model 或合成字段代替。 |
| Lightweight depth | 一个原冻结分层通过 | 保留原 artifact／profile 身份及局限；最终 artifact 的认证须另核对。 |
| Strict depth | 一个原冻结分层通过，缺 test witness 保持 unsure | 原 host oracle 不是模型执行测试；不推导所有分层或最终 artifact 质量通过。 |
| Discovery | Codex-only 下 canonical 独立 family 规则拒绝 | 按用户决定后验其他 backend；不得添加同 family 豁免以获得通过。 |
| Module card | unlimited subscription 要求 ZCode generator＋独立 Codex judge | 后端阶段完成；不得用 capped／同 family 流程代替原要求。 |

实际调用、generation、judgment、补充实验、宿主 oracle 和确定性测试继续分别记账。当前6629/B939产品的确定性整合已通过；本索引不重新绑定历史质量样本至当前包，也不新增任何 stratum 的质量批准。

## Maintenance 身份证据核对

只读检查原 maintenance 的两条完整原生回执：每条有 thread.started、turn.started、两条 item.completed 和 turn.completed；递归扫描事件的所有 model 类字段名均为空。当前安装包的 canonical Codex decoder 对这些原始事件保持 served_model unknown，未用 requested/configured model 填充。

在内存副本的 terminal event 插入明确 model 字段后，同一 decoder 保留该字段且 text／usage 不变。这只是合成 decoder 正例，不证明 CLI／服务实际提供身份，不是 native archive 恢复或推理试验。`supports_model_override` 说明请求选择能力，不能当作实际 served identity 证据；回复正文自行声称的模型名也不是原生身份元数据。

原 database／WAL 的核验前后 hash 相同。没有新模型调用、账本结算或原结果改写。当前缺口是所选 native profile 的身份支持尚未认证，不是已证明的 decoder 丢字段，也不宣称整个 Codex 后端永远不支持。保留领域的响应后身份校验、actual cost 和 unknown 语义，不改生产策略来绕过门禁。

## 分配与交付状态

最新不可变准入的保守累计仍为205，当前上限206，没有释放旧额度。Screening 的成对28次仅是该具体形状的分配准备，其他分层和各层 deadline 仍须分别核算。下一轮还需完整新案例／labels、artifact/profile、角色、metadata、留存、预算和控制证据；本索引没有可执行派发或 live-admission。

其余完整目标保持开放：原 Strict／Discovery 原位对账，深层恢复／预算／并发及 publication receipts 矩阵，其他后端 native 验收，实际 immutable revisions、paired pins 与可恢复 rollout。框架阶段保持收口。

机器索引及证据：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/quality-readiness-index-20261016/result.json`；审阅包 `quality-readiness-review.tar.gz` SHA256 `c12353e4f39a7d2d30fd43c247b48edc008109bdeb64e1607c1af8ac079c4d22`，7个成员逐项核验。它绑定十层保留证据、canonical SDK 角色限制、实际 metadata 观测和预算来源，model calls／新准入均为零。

## 后续运行中：Foundation revision-cache（2026-10-17）

新增独立Foundation原生配对已准入并启动，见 single-capability-foundation-revision-protocol.md。明确新软件协议修订保留旧205并仅增加28，总233；原206协议和所有旧样本不改写。当前尚无完整阶段／质量结论，初次宿主控制不等同推理验收。旧索引和artifact继续作为原冻结证据，不能因这轮在运行就升级为通过。

## Foundation revision-cache 已终态（2026-10-17）

两臂22原生调用均complete，无新unknown；当前dry_run有效tree接受7facet，基线因模型相对源码链接被validator阻断。两臂同样省略3facet，完整语义覆盖／unlimited correction仍未验收。不可把协调器exit0、judge pass数或当前有效tree推广为一般质量通过。见 single-capability-foundation-revision-outcome.md。前节“运行中”为历史观察，当前状态以本节及adjudication.json为准。


## 2026-10-17：独立 lease-store Direct 终态

新显式协议235保留原233，仅新增2次，两臂原执行均complete，无新unknown。宿主独立冻结案例的语义检查满足：新缺陷准确定位、旧finding fixed、无虚构CI／关系，源Python复现有原生事件。该案例不替代旧Direct失败样本，不计为完整Direct或十层质量验收。优先级校准、pytest不可用、served_model与USD未知均记录；其他层原状态保留。见 single-capability-lease-direct-native.md。
