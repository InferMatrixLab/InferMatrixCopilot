# 独立 lease-store Direct 原生比较

框架阶段已交付；本轮推进后续 Codex 质量验收，不改变框架完成条件。新 lease-store PR 源码、独立私有标签、知识快照、CI／discussion／carried-finding 证据在模型派发前冻结；新源码与旧失败样本不同。模型看不到私有标签，不能把本轮当作原未知执行的替代或重编号重跑。

两臂调用现有 review_case_arm：原 Bot 计算流程和当前 Copilot ReviewRuntime；使用原始及当前安装包，不注入产品源码。当前臂保留两次 reserve 同 run、find/get_result 恢复检查。原生调用经共同 evaluation guard 委托 canonical Copilot provider，再由沙箱抽象运行于 Personal-Agent Docker。Direct 使用只读源快照工具配置，关闭 apps／web 等继承配置；不套用禁止读取源文件的 knowledge structured-only 策略。

显式新协议 lease-direct-20261017 保留此前全部233配额，仅增加每臂1、共2次 Codex CLI invocation，总保守上限235。旧233及更早协议不变，恢复不能扩大每臂额度；费用金额与 vendor 内部回合不等同 CLI 次数。130项准入／账本定向检查通过。此前两臂完整 driver 的有效／畸形输出控制各恰好一次脚本CLI，支持此范围内的调用边界；不是原生模型质量证据。

固定 native180秒／host300秒／workflow330秒／phase630秒／outer660秒，两臂串行，发布关闭。二进制628e3548与所选Codex账号控制、固定出口CONNECT/TLS及越界拒绝已通过，均无模型请求；这些控制不证明推理权限或质量。静态预检先因一个旧external evidence未绑定而拒绝，草稿保留，补回原hash并明确Direct case profile后重新冻结；没有派发后改写manifest。当前身份以live-admission.json为准。

本轮准入与原始执行目录：output/single-capability-completion-20261012/native-quality-lease-direct-20261017。first-dispatch只启动一次，原coordinator工具句柄60677；工具观测超时不能重启。结果须区分native完成、应用校验及宿主语义判定；若应用失败，保留输出并仅在原回执证明终态后显式对账，不重跑同一臂。质量结论另记。原Discovery／Strict未知身份、账本、环境和代理保留。生产wheel仍为Provider6629／Bot3bbf；无commit／push／pin／真实发布或部署。完整十层质量及完整目标未完成。

## 原执行终态及判定

原coordinator60677已返回0；两臂各1次原生调用、两个完整类型化应用结果，没有新unknown或重派发。当前臂原driver还验证两次reserve同run，以及关闭／重开runtime后的find/get_result一致。5225项绑定输入保持原hash。旧233配额全部保留，本轮2次用完，总保守分配235；没有因实际使用少于额度而回收旧分配。

| 观察 | 当前臂 | 原基线臂 |
| --- | --- | --- |
| 新缺陷 | 找到旧token删除新lease，store.py:18 | 同样找到，定位一致 |
| carried finding | 零时长问题fixed，未当成现存缺陷重复 | 同样fixed |
| 关系／CI | 两条new、无虚构thread，CI保持不可用 | 一致 |
| 实际源动作 | 读取Git／README，Python复现旧token删除、零时长拒绝 | 同样有原生完成事件和复现输出 |
| CLI记录耗时 | 133.69秒 | 100.79秒 |

两边都额外给出缺Test Plan／Result的P1。冻结Direct prompt明确要求这一提示及优先级，未将它计为虚构源缺陷；本轮不改变业务策略。两边核心bug也标P1，高于标签的名义P2；标签允许有理由的更高优先级。宿主认为具体复现展示正常替换生命周期下当前lease被无条件删除，足以支持此案例内的较高影响判断；这不等于证明部署范围紧急性，广泛优先级校准仍待验收。当前臂含新版minimality proof，原结果序列化为null，原字节保留。

宿主按冻结源码／oracle／私有标签与实际输出判定本案例的缺陷、定位和fixed-recheck语义满足要求。判定不是外部盲审，也不从单样本推出整体无回归。两臂都遇到沙箱没有pytest，保留失败事件；实际证据是直接Python复现，不冒称pytest通过。native事件没有served-model attestation，仍为null；token usage保留原事实，USD未知而非0。CLI耗时差异是一次观测，不宣称性能／费用结论。

首次只读audit在数据库／WAL物理文件集合稳定断言处失败；其前已核对完整回执和输入hash。原前后值未留存，因此不推断具体原因。随后显式检查发现空WAL，数据库／WAL／SHM跨只读读取hash稳定；未改audit或原回执，按原脚本再次通过。draft-audit-note.txt保留此限制。没有借此重跑模型或结算原未知调用。

完整Direct分布质量、其余knowledge层、Strict预算与原unknown、其他后端及实际配对发布仍开放。最终结果以terminal-observation.json、receipt-audit.json及adjudication.json为准；旧live-observation.json仅为历史运行中观察。
