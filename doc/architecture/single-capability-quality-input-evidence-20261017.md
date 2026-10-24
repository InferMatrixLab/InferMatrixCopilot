# 当前候选的质量输入核验与源码oracle补证 · 2026-10-17

当前产品仍为Copilot a10c3484…／ReviewBot24ac7cfb…；本轮没有生产代码修改（行数／AST增量0），框架不重开，未派发模型。

## 冻结标签与角色限制

已用当前安装SDK复核原完整十层质量描述符：12份私有label文件仍与原manifest-v8 hash完全相同，未借新模型输出重写旧标签。该十层集合已有覆盖actionable defect与fixed carried finding的完整合成reservation案例；真实PR5636／6062是额外输入准备，不替代原完整范围。

当前Codex-only角色预检在discovery拒绝同模型family的提取／审查，在module-card拒绝把既有unlimited ZCode GLM-5.3提取合同改为Codex。依用户“其他后端后验”的选择，这两项保留到后续，不绕过领域策略、不算验收通过。其他八层仅通过角色约束检查，**不是已冻结新profile／driver或已获派发准入**；没有重用旧admission，原519额度及spare不变。

## 真实PR4870标签的独立源码补证

在冻结的base0798b0bf…／head231b3ddb…中读取原Git对象，使用实际未修改的赋值AST与三个payload函数、真实CPU torch.Tensor，已复现既有latent-gap标签的反例；未填充输入对照正确。另从原prior fc40c1b2…与head提取真实streaming分支赋值，head八种标量组合均符合既有合同，prior的原缺口被观察到。原标签文件没有改写。

这些仅验证具体源码机制与标量分支，不运行完整vLLM服务、GPU执行、上游全套或模型review，不把它升级为整例质量通过。PR4870是已有training replay，不声称未见过或统计代表性。原源码／Git对象未修改。

全部具体输出、标签索引与hash放在宿主private目录；它们不是模型输入、知识页或stage参数，未来不得mount／forward本证据根及其父共享输出根。后续新比较必须把这些路径作为host-only标签重新绑定并验证可见范围。

## 当前完整回归与剩余工作

针对最新两处session准备修复，已从[完整代码交付](single-capability-session-deadline-delivery-20261017.md)的full patch重建冻结源码tree1092d774…，与wheel核对377个对应Python成员；完整Copilot test套件正在运行，不能宣称其已通过。

原进程与最终结果目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-full-source-20261017`)，pid及命令见process.json。若协调端丢失观察，只检查原进程／日志／结果，不因超时重新执行。完整Bot旧结果仍限其未改动版本范围，本轮不重复运行。

后续继续：观察原完整回归至terminal；冻结当前不可变配对的driver／profile、完整案例容差与新预算准入，再执行实际比较。原paid unknown、History外层ack、其余深层恢复、非Codex后端与最终release／rollout保留。

私有源码oracle及核验归档 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-quality-input-evidence-20261017/a10-quality-input-evidence.tar.gz`)只供宿主审阅，不能给模型worker；其中进程观察是封包时历史快照，最终状态以原process.json及JUnit为准。
