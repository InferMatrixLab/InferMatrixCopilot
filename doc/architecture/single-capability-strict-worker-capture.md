# Strict 耐久 worker 预算恢复与捕获型失败对账

框架交付保持完成，生产配对仍是Provider6629／Bot3bbf。本次添加实际SDK子进程验证，并修复评估协调器的一条失败对账路径；无生产代码或wheel变化，没有真实模型请求或新额度准入，旧235保守分配不变。

## 已开始执行后的父端重开

使用标准StrictRuntime、实际RunService队列、受信worker_prefix、现有durable_worker注入包装及产品`-m infermatrix_copilot --execute-strict-reserved`。测试bootstrap只安装forge／image／native边界替身，然后委托原worker，没有业务重试或第二套workflow。代码和模型边界之外，真实Git、run claim、标准playbook、provider、budget hooks和SQLite回执均为产品路径。

确认第一次脚本native边界已开始、子进程PID仍存活后，让父端观察超时并关闭SDK；随后新SDK以同键find／start恢复原run。额度4在逐评论验证前blocked，额度5正常done。两种情况下都只有一个原worker启动，模型边界调用全部来自该PID；原launch字节、run ID和全部完成回执保持不变。子进程中deep／round2／verify／zero-yield选项均为true。这里关闭／重开的是SDK服务，不是杀死整个父Python进程，后者仍需独立验证。

新增测试明确要求installed artifact；源路径产品导入时两例会跳过，因为原durable_worker本身拒绝源码注入。本次在当前精确wheel环境执行，无跳过；这个环境限定不能当成任意source环境通过。

初稿bootstrap重复传入worker脚本名，参数解析失败，原报告保留。更细的launch计数随后发现：若在evaluation-worker标记刚写完、产品尚未claim时关闭SDK，重开会出现两个包装进程启动。原标记早于实际执行，不能把PID存在直接等同于已开始评审；这份失败报告保留为queued-preclaim-results.xml。测试包装会拒绝第二身份，没有证明一般环境里的重复付费，也没有关闭领取前竞争这个验收点。最终验证明确观察第一实际边界后再测已开始执行的恢复，不将两种时序混为一谈。

## 捕获型驱动的已知原生失败

Strict `--capture`会保存blocked结果后正常退出0。如果原生调用已有明确turn.failed和非零native exit，旧协调器仍把该stage留在needs_reconciliation，但显式对账只允许非零stage exit，导致已知终态无法结算。新回归在原helper上实际失败，原报告保留。

修复复用既有进程／输出证明与canonical Codex terminal_failure判断。对stage0只有原生失败终态及原正常进程记录都成立才允许显式对账；全部候选回执先验证，再结算原reserved行。保存captured_stage_proof，在恢复时重新核对。native_complete=false原样保留，stage标reconciled_failed，重复run不再次派发；不把已知失败升级为成功或质量通过。

没有回执、未知native、缺进程／reason、超时、signal、exit不匹配或恢复时删除proof都继续拒绝，并保留原calls和pending stage。普通应用失败和旧已结算证据继续走原路径。此修改只用于后续新协议；未修改已冻结的旧helper、旧manifest或原未知执行，也未用它重放任何付费调用。

联合验证覆盖协调器、质量准入、阶段期限和实际SDK worker；原失败、当前结果与wheel／RECORD／provenance证明分开保留。128项通过不代表完整模型质量。当前生产行／AST增量均0；评估helper和测试逐文件增量在result.json。

证据目录：output/single-capability-completion-20261012/strict-budget-worker-20261017。完整目标继续开放：历史基线预算契约、领取前竞争、真正父进程崩溃、Strict原生质量与其余知识层、原unknown、其他后端及发布验收。

后续：领取前竞争已由 single-capability-preclaim-ownership.md 独立验证，评估wrapper额外身份阻挡已移除；本页及其原交付包保留先前范围，不改写当时开放项或旧报告。
