# 当前配对与剩余验收 · 2026-10-17

> 本次框架交付统一入口：[最终收口](single-capability-framework-closeout-current-20261017.md)。下方质量准备是后续阶段，不阻挡框架交付。

框架已收口；当前配对是Copilot `e98e4c08…`／ReviewBot `24ac7cfb…`。本页统一说明当前代码、证据及下一步，不改写原计划的A–G、十层质量矩阵和发布条件，也不是完整目标已完成的证明。本次相对2ddf修改三个bridge知识路径成员；前次Direct修复保留，未调用真实模型。

## 已有证据与未完成项

| 要求 | 当前可证明的范围 | 后续工作 |
| --- | --- | --- |
| 唯一归属／调用方迁移／旧实现删除 | [机器清单](single-capability-framework-inventory.json)：70项能力、41项初始删除记录、24项已闭环连接、0项登记的未接通框架连接；后续宿主lookup／只读transport迁移记录在相应能力条目中。 | 保持边界守卫；不因后续质量缺口重开框架。 |
| 确定性整合 | Copilot原386份src中相对1b36累计五个成员变更；本次88项bridge／预检测试及安装MCP回归通过，前次Direct40保留原范围。原完整套件不冒充新配对全量结果。6313份原test／eval／playbook／validator输入相同是前次审计范围。本次eval输入合同／测试已变更，见下方后续进展；保留原6008项及4项环境skip／对应补证的版本范围，不重绑定为当前全部输入验收。当前Bot326份冻结输入匹配，完整2925项、安装235项通过。 | 最终发布按不可变配对重新审核证据匹配；旧成功不自动绑定改动后的产品。 |
| 安装与包边界 | 当前配对fresh最小安装 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/strict-first-request-capture-20261017/installed/installed-proof.json`)：新建venv，离线hash锁定安装，无环境复制、无MCP、无site-package symlink；2337／130产品成员完全匹配。Bot兼容prompt与SDK同对象、完整成员匹配、实际Direct首请求捕获通过；旧SDK／curation和operator证据保留原1b36范围。 | 这是实际安装／预派发证据，不是模型、MCP服务或全部运行环境的验收。 |
| 原发布回执恢复 | [宿主对账入口](single-capability-publication-original-reconciliation-20261017.md)：不重算、不POST、不恢复jobs；原head漂移后可结算回执，空／坏／缺失状态不允许重发。 | 该已复现连接已关闭；不等于所有深层恢复完成。 |
| 原native unknown／精细对账 | 原执行身份、位置、版本、profile及519累计分配／未用额度保留；原admission hash未变。已实现防重和若干原结果恢复合同。 | 原unknown、旧History外层ack、完整崩溃／并发与精细结算矩阵继续验收，不能用“不会重发”替代原结果对账。 |
| 十层真实质量 | 以前的成功、失败和unknown保留其原artifact范围；当前配对的完整十层质量验收未完成。两组真实上游oracle提案已有canonical请求／finding-ID、共享knowledge和指定路径local／SSH控制证据，见下文。 | 完成完整独立标签／新finding裁定与容差、完整prompt比较（PR-context部分已量测）、实际profiles和预算准入，再执行并裁定。 |
| 后端／认证 | 当前Codex及本地／SSH沙箱控制保留其确切版本证据；其他原生后端按用户指示后验。 | 保留独立model-family政策；不能用同家族profile绕过Discovery／Module-card的准入要求。 |
| 发布／rollout | 当前代码可由完整配对补丁和wheels (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/strict-first-request-capture-20261017/strict-bridge-pin-review.tar.gz`)重建；本任务未执行发布或部署。 | 不可变发布／consumer pins、受控shadow、在途重新盘点、原环境保留或排空及rollout／rollback证据仍须完成。 |

前次1b36核对记录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/current-acceptance-audit-20261017/readiness.json`)保留原范围。当前2ddf的两个生产成员变更、行数／AST及新验证见Direct交付，不用测试累计数量代表整个目标进度。

## 真实质量输入的新证据

历史peer oracle准备 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/historical-peer-oracles-20261017/preparation.json`)冻结两组真实上游记录：

- PR5636：Gaohan123在原commit提出两条P1问题，随后在最终head `62aeffd1…` 明确APPROVED。同一审查者接受最终head，为两条carried finding提供外部判断依据；将其映射为fixed仍是明确标注的推断和待审提案。
- PR6062：不同于PR作者的审查者在head `688143b1…` APPROVED并留下边界处理说明。其CPU／GPU测试声明仅记录为reviewer-reported，未作为本地执行结果；批准不是“无任何其他缺陷”的标签。

每组输入包括原Git bundle、原base/head、GitHub merge-base diff、精确两revision diff及过滤后的历史反馈。反馈边界选在被留出的最终批准前；晚于边界或后来编辑的记录不作为可见历史正文。该构造是受控replay子集，不声称恢复了当时完整GitHub API；可变PR标题／正文也不声称在边界时未变化。没有可验证的历史CI视图，所以CI显式unavailable，不能借用后来读取的绿色结果。

模型候选可见目录为visible；最终批准、相应原文／ID及映射提案放在private。已核对已知held-out ID／批准正文没有进入可见JSON／diff，文件无跨根symlink。父目录与private绝不能作为worker mount或stage参数；实际mount、canonical admission和请求适配尚未执行，布局检查本身不授权派发。

这些是特定carried finding／机制的外部oracle提案，不是完整独立case labels。新finding的判断不能因旧head被批准而自动记为false positive；它仍需独立、基于源码的裁定。公开历史材料也不保证训练污染隔离或统计代表性。没有用这些提案改写原native失败或宣布质量通过。

本机上游开发依赖与这些历史PR的pin不同；本轮只做源码和环境检查，没有宣称运行其CPU／GPU测试，也没有把运行上游全套新增为Copilot主流程门槛。

## 后续阶段的当前进展与下一项

[真实PR请求／diff合同](single-capability-review-pr-input-contract-20261017.md)与[共享knowledge绑定](single-capability-review-shared-knowledge-20261017.md)已准备canonical host packet、finding-ID、同一完整原知识及两侧SDK一致的计划文档读取。[真实worker可见范围控制](single-capability-review-peer-worker-isolation-20261017.md)进一步使用两组实际源／host／knowledge，在相同Docker backend的local及SSH位置完成四项原Python控制：指定私有根不可读、输入只读、完整知识验证、无账号／proxy，原结果可读且拒绝重复execute。没有模型调用，也不是完整Direct／Strict执行或全侧信道证明。

local实际控制exit0，首次SCP取件失败被保留；只读取回原结果，没有重跑。PR6062已有知识回归的限制保留。下一项是完整labels／新finding裁定、baseline／candidate渲染差异、真实profiles与不可变预算准入，再派发真实比较。其余八层与原unknown不删除，框架不重开。

最新渲染证据：[冻结forge类型／作者修复及原始渲染比较](single-capability-review-forge-context-20261017.md)。新比较显式选择context v2；原版本保持，PR-context裁剪／元数据差异不被伪装成同一完整模型输入。

当前产品增量：[共享Direct prompt修复与完整首次请求捕获](single-capability-direct-pr-prompt-delivery-20261017.md)。三点PR diff和知识去重已验证，权威标签／custom保护保留；Strict及后续阶段prompt与新native image／profile尚待完成，原1b36环境保留。

当前增量：[Strict首输入与native bridge知识路径固定](single-capability-strict-bridge-pin-delivery-20261017.md)。真实MCP旧读不到／新正确读取的回归成立；默认非沙箱Codex仍被能力预检阻止，fixture请求捕获不等同原生准入或审查完成。

后续运行准备：[当前e98e镜像与local／SSH实测](single-capability-bridge-pin-runtime-20261017.md)。新镜像0085a291…已验证包身份、真实MCP与进程控制，completion／session预检通过；未挂账号／proxy，未执行native配置探测或模型，不能称为真实准入。原1b36证据范围不变。

最新原生准备：[当前镜像真实Codex配置及登录识别](single-capability-current-native-configuration-20261017.md)：local／SSH实际effective配置与managed requirements均通过，原结果可读且重复控制拒绝；容器临时凭据通道的login status确认ChatGPT登录。没有模型／网络调用，不证明服务端认证或完整session／质量验收，旧admission不重用。

当前候选已更新为a10c3484…／24ac7cfb…：[session准备截止时间修复](single-capability-session-deadline-delivery-20261017.md)。相对e98e仅两个产品成员改变，完整真实PR／知识输入的local／SSH生产准备均通过；原e98e控制失败／pending保留。上文旧候选证据仍限原范围；新候选没有真实模型质量／服务端认证验收。

当前质量准备：[十层标签核验与PR4870源码补证](single-capability-quality-input-evidence-20261017.md)。原12份labels未变；真实CPU函数反例及carried分支补证成立，不能称整例质量通过。当前Codex-only保留discovery／module-card角色限制；a10冻结完整源码套件正在运行，最终结果以原进程／JUnit为准。模型调用0，旧admission／519不变。

2026-10-18：[八层阶段输入准备](single-capability-eight-strata-preparation-20261018.md)。16个phase文件／38个阶段经现有loader验证，20个双臂入口经实际bootstrap --help验证；旧Foundation edit合同只在新输入改为prose，原数据保留。仍非paired派发准入，182次调用仅提案，519未变；原a10完整源码套件继续运行。

当前a10完整源码结果：[确定性回归与补验](single-capability-a10-deterministic-readiness-20261018.md)：原进程exit0，6013 pass＋4环境skip；当前安装配对在Personal-Agent的5项对应资源／namespace补验全部通过。双臂五类前置准备共10项通过，真实模型0；当前SDK与Bot完整配对测试已启动，原进程仍需观察至terminal。旧版本结果与剩余目标保留。

当前a10／24ac完整确定性配对已完成：[原进程最终结果](single-capability-a10-full-paired-integration-20261018.md)。Copilot6013 pass＋4环境skip，对应当前安装namespace／资源补验5 pass；Bot在安装a10 SDK下完整2925 pass，0 skip。原两进程exit0，安装成员再次匹配。模型质量／深层恢复／其他后端／发布仍未关闭，原519与unknown未改。

PR-history下一次比较：[源批次与留存准备](single-capability-history-batch-preparation-20261018.md)。两侧run_stage冻结相同PR101／102 source evidence；当前omit在factory前拒绝、model trace0；retain两侧均已知predispatch拒绝。18文件留存原型通过同一validator并拒绝篡改，未创建live manifest／ledger。旧SDKrecord_payload=false差异与不同KBbase保留，仍不声称完整请求相同或真实留存／质量通过。模型0、519未变。

配对草案与预算连接：[保留额度校验修复](single-capability-retained-budget-manifest-20261018.md)。源批次／十层标签描述／16阶段／实际schema等已汇总；现有组件检查通过，完整准入因519保留＋182提案超过当前519被正确拒绝。共享eval账本新增单处累计校验，100测试通过；生产代码未改，旧台账与额度不动，账号／proxy及真实质量仍未准入。

当前版本认证／代理机制：[a10 local／SSH canonical admission控制](single-capability-a10-auth-proxy-controls-20261018.md)均通过，代理匿名TLS允许／拒绝条件成立；临时proxy全部关闭。local清理断言原失败保留，原socket只读确认关闭后仅清理自己的节点，未重跑认证。不是服务端推理权限或live campaign准入；模型0、519未变、旧环境／proxy未动。
