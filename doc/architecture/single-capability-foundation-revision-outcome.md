# Foundation 独立原生比较结果（2026-10-17）

两臂已按原协议执行结束，协调器exit0仅表示阶段过程终结，不代表应用或质量通过。每臂2生成＋9审查，共22次实际Codex原生调用，全部回执完整，无新增unknown；两臂均生成core五facet、tooling四facet，省略相同三个facet。保留配额仍为旧205＋新28＝233，未释放六次未使用额度，未重试／重编号旧样本。

| 观察 | 当前6629应用 | 原基线应用 |
|---|---|---|
| 最终应用状态 | dry_run，tree校验通过 | blocked，tree校验拒绝 |
| native judge | 7pass／1unsure／1fail | 8pass／1unsure |
| 最终可用tree中的facet | 7 | 0，整个batch被阻断 |
| completed resume | 应用内部恢复检查通过，无新调用 | 未测；blocked批次不进入该helper的completed-resume分支 |

当前core/configuration的字节记账表述没有引用payload类型检查行，judge保留unsure；tooling/api也仅引用len(payload)却声称byte length，被拒绝。两项没有进入最终tree。宿主逐项复核当前接受的七段 prose，其范围限于进程内cache、revision、snapshot和helper，未见持久化／跨进程协调／已执行测试／benchmark断言；这只是本次样本的人工复核，不是一般模型质量保证。

基线模型在core prose生成`RevisionCache`（目标 `pkg/core.py`）、pkg/util.py、pkg/__init__.py等相对Markdown链接。从知识页位置解析目标不存在，canonical validator正确阻断batch，不能把八个judge pass当作八个最终发布facet。基线tooling/features的byte length表述也缺少明确类型证据，宿主保留证据支撑缺口；不覆盖原native pass，更不把整个blocked batch改成通过。

两臂首个生成system、配置model／effort及解析后的结构化payload相同，prompt序列化文本不同。输出措辞／引用与原生judge各为一次采样，无法据此确定软件导致的质量变化。两臂都未显式展开旧revision先于capacity判断、所有失败写入保持记账等完整正向契约；validation及部分其他facet仍未覆盖。完整Foundation、unlimited correction与十层质量验收继续未通过，不通过补写、额外调用或调整标签制造pass。

原生证据核验：22条ledger结果SHA及完整终态、实际model reply对象与应用records一一计数匹配、phase返回状态、最终tree中只出现pass facet、5207个冻结文件字节全部未变。旧基线records没有新dispatch flags，使用原生ledger完整回执和reply对象匹配证明，未补写旧字段；基线blocked没有tree，审计按真实状态处理，未要求它出现pass标记。两次审计脚本假设修正属于报告兼容处理，不是产品失败或付费重跑。

全部22条记录未提供served-model身份，保持null；不以requested model代填。token usage保留原记录，订阅USD费用仍未知，不记为零。公开审阅包导出只含合成源码／模型回复、类型化结果和安全元数据，原native stdout/stderr仅保留hash引用，不打包认证缓存、代理内容或原始账本。原始本地证据保持原身份可核查。

结果：`output/single-capability-completion-20261012/native-quality-foundation-revision-20261017/adjudication.json`；审阅包为同目录foundation-revision-outcome-review.tar.gz。完整重构目标仍开放，无生产wheel变更、commit、push、pin、发布或部署。
