# 框架收口结果（2026-10-16）

当前正式交付与最后修复验证见 [框架阶段正式收口](single-capability-framework-stage-acceptance.md)。下文是较早537a候选的原始收口记录；当前候选为6629/B939，历史验证身份不变。

框架阶段完成，完整目标和发布验收保留。[架构及归属](single-capability-framework-handoff.md)／[逐项调用方与删除清单](single-capability-framework-inventory.json)／[完整计划](single-capability-completion-plan.md)。

| 主流程／必要验证 | 当前结果 |
| --- | --- |
| 入口 → SDK → 应用 → provider → 类型化结果 → 发布准备 | 当前 main/foundation/depth/recovery/ownership 五组通过；模型／GitHub 为受控替身，无真实发布 |
| Direct／Strict、local／remote、兼容序列化、公共 SDK 与边界 | 当前安装包及源码定向检查通过 |
| Knowledge 共享质量审查及阶段策略 | 当前安装包 foundation、depth、ownership、recovery 组通过；机制通过不等于真实模型质量通过 |
| Direct 未决事实传递 | 当前安装包实际 SDK reservation → main/executor → status/result 为 interrupted；Bot 四个 poll/result guard 保持 pending，同业务键返回原 run |
| MoA 未决调用 | 首次调用和 retry 共六个旧实现回归失败；当前源码／安装包通过；未决或显式禁止 fallback 不切模型，正常已知故障仍可 fallback |
| 包与依赖 | 当前 wheel RECORD／源码字节绑定、干净安装、pip check、公开 SDK smoke 通过；产品从 site-packages 加载 |
| 补充 policy / evaluation | policy 原始有一个超时子进程清理失败；一次定向复核通过，仍登记未关闭的不稳定性。原始整组 gate 保持失败，evaluation 未继续执行 |
| 之前的完整源码／远端 namespace | EB67/B939 历史完整运行零失败／错误，三个原始环境 skips 有明确补验；不冒充本次最后两处修复的全量／远端证明 |

Copilot wheel SHA256：`537a8afd40c80117f75a8a606957d69852c42ab9aa87076bb5c212ee779338ba`。ReviewBot：`9399980b86403b3d83682ba06bce56da9512145ba1a13b127b9584e4f520e23b`。

当前结果：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/framework-closeout-20261016/result.json`；本次原始报告及失败记录复制在同目录 `installed-*`，完整运行工作目录为 `/var/tmp/codex-singlecap-framework-closeout-20261016`。当前定向源码、安装包 MoA、实际 SDK 到 Bot 的未决结果桥接证据也在交付目录。没有额外真实模型调用，没有改写历史账本。

可审阅代码包：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/framework-closeout-20261016/paired-framework-review.tar.gz`。包含相对 Copilot647e6e02／ReviewBot1e70e983 的完整双仓补丁（含新增及删除）、精确 wheels、离线依赖输入、架构与清单、必要验证及生产行数／AST 指标；`patch-reconstruction.json` 校验每个文件和删除，包 SHA256 见旁侧 `artifact.json`。无需叠加旧 overlay；旧交付包保持原身份。

当前最后修复后的框架无待接主流程、无待迁移 Bot vendor／repair 主体。P1 保留 Strict 等组合路径中未决状态持久投影的端到端审计和原未知结果对账，作为上线前条件；真实模型质量、深层恢复、其他后端及 release/pin/部署按 [handoff 优先级](single-capability-framework-handoff.md) 后续完成。未提交、推送、发布或部署。

后续 P1 handler 未决状态切片已完成源码和精确新 wheel 的定向验收：[共享结果补充](single-capability-shared-pending-outcome.md)。Direct／Strict／MoA 复用判断，Bot 保持 pending；原框架包与失败 gate 不变。更深恢复和超时子进程问题仍保留。
